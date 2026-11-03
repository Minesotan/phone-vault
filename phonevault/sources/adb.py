"""Reading a phone over ADB.

Notes on the awkward parts, because they drive most of the design here:

*Quoting.* An ``adb shell`` argument is re-parsed by a shell on the device, so
anything built by string concatenation breaks on the first filename with a
quote or a space in it. Every command is therefore base64-encoded on this side
and decoded into ``sh`` on the far side, which removes the problem entirely.

*Binary safety.* ``adb shell`` translates newlines on some platforms and will
happily corrupt a JPEG. ``adb exec-out`` does not, so all file content goes
through that.

*Process overhead.* Spawning one ``adb exec-out cat`` per file costs tens of
milliseconds, which is an hour of pure overhead across fifty thousand photos.
The batch reader streams many files through a single ``tar`` instead, and
falls back to per-file reads if the device's tar does not cooperate.

*Root.* A rooted phone can read ``/data``, which is where app data lives. Root
is detected once by asking ``su`` for its uid and is optional throughout.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import IO

from ..errors import AdbError, BatchReadError, DeviceLostError
from ..snapshot import KIND_DIR, KIND_FILE, KIND_SYMLINK
from .base import Matcher, Source, SourceFile, is_forbidden

#: Field separator for the listing. Chosen because it cannot appear in a
#: Linux filename, unlike every printable character.
SEP = "\x1f"

#: The virtual root that exposes raw partition images. It is not a directory
#: on the phone: naming it asks for the partitions listed in
#: :data:`DEVICE_UNIQUE_PARTITIONS` to be imaged into the snapshot as files.
PARTITION_ROOT = "/partitions"

#: Partitions that are unique to one handset and cannot be re-created from a
#: firmware image.
#:
#: This is the gap a file-level backup cannot cover. Everything under /data
#: can be copied back after a wipe, but these live outside any filesystem, and
#: if a flash corrupts them the phone does not come back: no cellular (the
#: modem's calibration and IMEI are in modemst/fsg), no working sensors or
#: fingerprint reader (persist), or, on a Sony, a broken camera and DRM state
#: (TA). They total a hundred megabytes or so and deduplicate to nothing
#: between runs, so imaging them every backup costs essentially nothing.
#:
#: Deliberately absent: boot, system, vendor, product and the other partitions
#: that a firmware download restores byte for byte, and ``metadata``, which
#: only describes an encrypted /data that a wipe has already destroyed.
#:
#: One caveat on verification: ``persist`` is a mounted, writable ext4
#: filesystem, so its image is crash-consistent rather than checksum-stable.
#: Comparing a stored copy against the live partition later gives a different
#: hash because the partition moved, not because the read was wrong. The rest
#: are raw data and do hash identically.
DEVICE_UNIQUE_PARTITIONS = (
    # Qualcomm modem: calibration, IMEI, carrier provisioning.
    "modemst1",
    "modemst2",
    "fsg",
    "fsc",
    # Sensor and camera calibration, fingerprint enrolment data.
    "persist",
    "persistbak",
    # Sony: DRM keys, camera tuning, device configuration.
    "TA",
    "LTALabel",
    # Device identity and security state.
    "devinfo",
    "secdata",
    # Samsung and MediaTek equivalents, harmless where absent.
    "efs",
    "efs1",
    "efs2",
    "nvram",
    "nvdata",
    "nvcfg",
    "proinfo",
)

#: The virtual root that images the OS itself, from the slot now booted.
#:
#: Separate from :data:`PARTITION_ROOT`, and deliberately not part of any
#: preset, because it is fifteen-odd gigabytes to read every run. It exists
#: for one job: before flashing a new Android version, take a copy of the
#: build you are on so you can go back to it. Vendors stop serving old
#: firmware, so by the time an upgrade goes wrong the official image of what
#: you were running is often no longer downloadable -- but it is still sitting
#: on the phone.
FIRMWARE_ROOT = "/firmware"

#: Base names imaged for :data:`FIRMWARE_ROOT`. Each is tried bare and with
#: the active slot suffix, so an A/B phone gets the half it actually booted.
#: ``super`` is the big one: on a device with dynamic partitions it contains
#: system, vendor, product, system_ext and odm.
FIRMWARE_PARTITIONS = (
    "super",
    "boot",
    "init_boot",
    "vendor_boot",
    "dtbo",
    "vbmeta",
    "vbmeta_system",
    "recovery",
    "oem",
    "modem",
)

#: Longest command line to hand to adb. Windows CreateProcess hard-caps at
#: 32767 characters and reports anything longer as "the filename or extension
#: is too long"; staying well under leaves room for the executable path.
#: Payloads bigger than this must be pushed to the device as a file.
MAX_COMMAND_CHARS = 24000

_WINGET_ADB = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
    r"\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"
)


def find_adb(explicit: str | None = None) -> str:
    """Locate the adb executable, or explain how to install it."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if env := os.environ.get("ADB_PATH"):
        candidates.append(env)
    if found := shutil.which("adb"):
        candidates.append(found)
    candidates.append(_WINGET_ADB)
    for home_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if home := os.environ.get(home_var):
            candidates.append(os.path.join(home, "platform-tools", "adb.exe" if os.name == "nt" else "adb"))
    candidates.append(r"C:\platform-tools\adb.exe")
    candidates.append("/usr/bin/adb")

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)  # type: ignore[return-value]

    raise AdbError(
        "adb was not found. Install it with 'winget install Google.PlatformTools' "
        "(Windows) or your package manager, then re-run. If it is installed "
        "somewhere unusual, set ADB_PATH or pass --adb-path."
    )


class AdbDevice:
    """A connection to one device, and the primitives built on it."""

    def __init__(
        self,
        serial: str | None = None,
        adb_path: str | None = None,
        *,
        use_root: bool = True,
        timeout: int = 120,
    ) -> None:
        self.adb = find_adb(adb_path)
        self.timeout = timeout
        self.serial = serial or self._pick_serial()
        self._root_checked = False
        self._has_root = False
        self._want_root = use_root
        self._props: dict[str, str] = {}

    # -- low-level adb ----------------------------------------------------

    def _base(self, *args: str) -> list[str]:
        return [self.adb, "-s", self.serial, *args]

    def _run(self, args: Sequence[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(args),
            capture_output=True,
            timeout=timeout or self.timeout,
            creationflags=_no_window(),
        )

    @classmethod
    def list_devices(cls, adb_path: str | None = None) -> list[dict[str, str]]:
        """Every attached device and its state."""
        adb = find_adb(adb_path)
        try:
            proc = subprocess.run(
                [adb, "devices", "-l"], capture_output=True, timeout=30, creationflags=_no_window()
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbError(f"could not run adb: {exc}") from exc

        devices = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            info = {"serial": parts[0], "state": parts[1] if len(parts) > 1 else "unknown"}
            for token in parts[2:]:
                if ":" in token:
                    key, _, value = token.partition(":")
                    info[key] = value
            devices.append(info)
        return devices

    def _pick_serial(self) -> str:
        devices = self.list_devices(self.adb)
        usable = [d for d in devices if d["state"] == "device"]
        if not usable:
            if any(d["state"] == "unauthorized" for d in devices):
                raise AdbError(
                    "the device is connected but not authorised. Unlock the phone and "
                    "accept the 'Allow USB debugging' prompt, then try again."
                )
            if any(d["state"] == "offline" for d in devices):
                raise AdbError(
                    "the device is offline. Unplug and replug it, or re-run "
                    "'adb connect' for a wireless device."
                )
            raise AdbError(
                "no device found. Enable Developer options then USB debugging on the "
                "phone, connect it, and accept the authorisation prompt. "
                "Run 'phonevault devices' to check."
            )
        if len(usable) > 1:
            serials = ", ".join(d["serial"] for d in usable)
            raise AdbError(f"several devices are connected ({serials}); pass --device to choose one")
        return usable[0]["serial"]

    # -- remote execution -------------------------------------------------

    def _wrap(self, script: str, root: bool) -> str:
        """Encode a shell script so the device shell cannot misparse it."""
        payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
        runner = "su -c sh" if root else "sh"
        command = f"echo {payload} | base64 -d | {runner}"
        if len(command) > MAX_COMMAND_CHARS:
            # Windows CreateProcess refuses a command line over 32 KB, and it
            # reports that as "the filename or extension is too long", which
            # is thoroughly unhelpful. Anything this large should have been
            # pushed as a file instead.
            raise AdbError(
                f"device command is too long ({len(command)} characters, limit "
                f"{MAX_COMMAND_CHARS}); this is a bug -- large payloads must be "
                f"pushed to the device as a file, not passed on the command line"
            )
        return command

    def push_bytes(self, data: bytes, remote_path: str) -> None:
        """Place ``data`` at ``remote_path`` on the device.

        Goes through ``adb push`` so the size is bounded by the filesystem
        rather than by how long a command line the host will allow.
        """
        fd, staged = tempfile.mkstemp(prefix="pv-push-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            proc = self._run(self._base("push", staged, remote_path), timeout=180)
            if proc.returncode != 0:
                message = (proc.stderr + proc.stdout).decode("utf-8", "replace").strip()
                if looks_like_lost_device(message):
                    raise DeviceLostError(f"the device went away during push: {message}")
                raise AdbError(f"adb push to {remote_path} failed: {message}")
        finally:
            try:
                os.unlink(staged)
            except OSError:
                pass

    def shell(
        self, script: str, *, root: bool | None = None, timeout: int | None = None, check: bool = True
    ) -> str:
        """Run a script on the device and return its stdout as text."""
        proc = self._run(
            self._base("exec-out", self._wrap(script, self._resolve_root(root))), timeout
        )
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace").strip()
        if looks_like_lost_device(err):
            raise DeviceLostError(f"the device went away: {err}")
        if check and proc.returncode != 0:
            raise AdbError(f"device command failed: {err or out.strip() or 'no output'}")
        return out

    def popen(self, script: str, *, root: bool | None = None) -> subprocess.Popen:
        """Start a script on the device with its stdout as a binary pipe."""
        return subprocess.Popen(
            self._base("exec-out", self._wrap(script, self._resolve_root(root))),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_no_window(),
        )

    def _resolve_root(self, root: bool | None) -> bool:
        if root is False:
            return False
        if root is True:
            if not self.has_root:
                raise AdbError("root was required but 'su' is not usable on this device")
            return True
        return self._want_root and self.has_root

    # -- device facts -----------------------------------------------------

    @property
    def has_root(self) -> bool:
        if not self._root_checked:
            self._root_checked = True
            self._has_root = False
            if self._want_root:
                try:
                    proc = self._run(self._base("exec-out", "su -c id"), timeout=25)
                    self._has_root = b"uid=0" in proc.stdout
                except (OSError, subprocess.TimeoutExpired):
                    # A Magisk prompt left unanswered on the phone shows up as
                    # a timeout, so say so rather than silently backing off.
                    self._has_root = False
        return self._has_root

    def prop(self, name: str, default: str = "") -> str:
        if name not in self._props:
            try:
                self._props[name] = self.shell(
                    f"getprop {name}", root=False, timeout=25, check=False
                ).strip()
            except (OSError, subprocess.TimeoutExpired, AdbError):
                self._props[name] = ""
        return self._props[name] or default

    def describe(self) -> str:
        model = self.prop("ro.product.model", "Android device")
        release = self.prop("ro.build.version.release")
        label = f"{model} (Android {release})" if release else model
        return f"{label} [{self.serial}]"

    def list_partitions(self, names: Sequence[str] | None = None) -> list[dict]:
        """Locate the device-unique partitions and their sizes.

        Only partitions named in ``names`` (default
        :data:`DEVICE_UNIQUE_PARTITIONS`) are looked for, and any the phone
        does not have are quietly skipped -- the list spans several vendors,
        so most phones match only part of it.
        """
        wanted = list(names or DEVICE_UNIQUE_PARTITIONS)
        script = (
            f"for p in {' '.join(_sh_quote(n) for n in wanted)}; do "
            'd=$(readlink -f "/dev/block/by-name/$p" 2>/dev/null) || continue; '
            '[ -b "$d" ] || continue; '
            's=$(blockdev --getsize64 "$d" 2>/dev/null) || continue; '
            f"printf '%s{SEP}%s{SEP}%s\\n' \"$p\" \"$d\" \"$s\"; "
            "done"
        )
        found = []
        try:
            out = self.shell(script, root=True, check=False, timeout=120)
        except (AdbError, OSError, subprocess.TimeoutExpired):
            return found

        for line in out.splitlines():
            parts = line.strip().split(SEP)
            if len(parts) != 3:
                continue
            name, device, raw_size = parts
            try:
                size = int(raw_size)
            except ValueError:
                continue
            if size > 0:
                found.append({"name": name, "device": device, "size": size})
        return found

    def active_slot(self) -> str:
        """``_a``/``_b`` on an A/B device, empty string on a single-slot one."""
        return self.prop("ro.boot.slot_suffix", "").strip()

    def firmware_partitions(self) -> list[dict]:
        """The booted slot's OS partitions, newest-first by nothing in particular.

        Each base name is probed bare and slot-suffixed, and duplicates are
        collapsed by the block device they resolve to, so a device that has
        both ``boot`` and ``boot_a`` pointing at the same place is imaged once.
        """
        suffix = self.active_slot()
        candidates = []
        for base in FIRMWARE_PARTITIONS:
            candidates.append(base)
            if suffix:
                candidates.append(base + suffix)

        seen: dict[str, dict] = {}
        for part in self.list_partitions(candidates):
            # Prefer the suffixed name in the snapshot: it says which slot the
            # image came from, which matters when flashing it back.
            existing = seen.get(part["device"])
            if existing is None or (suffix and part["name"].endswith(suffix)):
                seen[part["device"]] = part
        return list(seen.values())

    def storage_volumes(self) -> list[dict]:
        """Every place user data can live, with how full it is.

        A phone with an SD card keeps the bulk of its photos and video there,
        under ``/storage/<UUID>``, not under ``/sdcard``. Backing up only
        ``/sdcard`` on such a phone quietly misses most of the data and still
        reports success, so the volumes are enumerated and the backup warns
        about any that its roots do not cover.
        """
        script = (
            "for v in /storage/*; do "
            '[ -d "$v" ] || continue; '
            'case "$v" in */self) continue;; esac; '
            'df -k "$v" 2>/dev/null | tail -1 | '
            "while read -r fs blocks used avail pct mount; do "
            f'printf \'%s{SEP}%s{SEP}%s{SEP}%s\\n\' "$v" "$blocks" "$used" "$avail"; '
            "done; done"
        )
        volumes = []
        try:
            out = self.shell(script, root=False, check=False, timeout=90)
        except (AdbError, OSError, subprocess.TimeoutExpired):
            return volumes

        for line in out.splitlines():
            parts = line.strip().split(SEP)
            if len(parts) != 4:
                continue
            path, blocks, used, avail = parts
            try:
                total_b, used_b, avail_b = (int(x) * 1024 for x in (blocks, used, avail))
            except ValueError:
                continue
            name = path.rsplit("/", 1)[-1]
            emulated = name == "emulated"
            volumes.append(
                {
                    # /storage/emulated is what /sdcard points at; naming it
                    # that way keeps the advice actionable.
                    "path": "/sdcard" if emulated else path,
                    "mount": path,
                    "label": "internal storage" if emulated else f"SD card ({name})",
                    "external": not emulated,
                    "total": total_b,
                    "used": used_b,
                    "available": avail_b,
                }
            )
        return volumes

    def storage_summary(self) -> list[dict[str, str]]:
        """``df`` output for the interesting mount points, for the UI."""
        out = self.shell("df -h /data /storage/emulated 2>/dev/null", root=False, check=False)
        rows = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 6:
                rows.append(
                    {
                        "filesystem": parts[0],
                        "size": parts[1],
                        "used": parts[2],
                        "available": parts[3],
                        "use_percent": parts[4],
                        "mounted": parts[5],
                    }
                )
        return rows

    # -- wireless ---------------------------------------------------------

    def enable_tcpip(self, port: int = 5555) -> str:
        proc = self._run(self._base("tcpip", str(port)), timeout=30)
        return proc.stdout.decode("utf-8", "replace").strip()

    def ip_address(self) -> str:
        out = self.shell(
            "ip -f inet addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*' | cut -d' ' -f2",
            root=False,
            check=False,
        )
        return out.strip().splitlines()[0].strip() if out.strip() else ""

    @staticmethod
    def connect(address: str, adb_path: str | None = None) -> str:
        adb = find_adb(adb_path)
        proc = subprocess.run(
            [adb, "connect", address], capture_output=True, timeout=45, creationflags=_no_window()
        )
        text = proc.stdout.decode("utf-8", "replace").strip()
        if "connected" not in text.lower():
            raise AdbError(f"could not connect to {address}: {text or 'no response'}")
        return text

    def wait_until_ready(self, timeout: float = 600.0) -> bool:
        """Block until this device is usable again, or the timeout expires.

        ``adb wait-for-device`` returns as soon as the transport exists, which
        is a moment before the shell reliably works, so readiness is
        confirmed with an actual command. For a Wi-Fi device the transport
        has to be re-established explicitly, because adb does not reconnect
        on its own.
        """
        deadline = time.monotonic() + timeout
        wireless = ":" in self.serial
        while time.monotonic() < deadline:
            if wireless:
                try:
                    self.connect(self.serial, self.adb)
                except AdbError:
                    pass
            try:
                subprocess.run(
                    self._base("wait-for-device"),
                    capture_output=True,
                    timeout=min(30, max(5, deadline - time.monotonic())),
                    creationflags=_no_window(),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                if "ok" in self.shell("echo ok", root=False, timeout=20, check=False):
                    self._root_checked = False  # su has to be re-established
                    return True
            except (AdbError, OSError, subprocess.TimeoutExpired):
                pass
            time.sleep(2.0)
        return False

    @staticmethod
    def pair(address: str, code: str, adb_path: str | None = None) -> str:
        """Android 11+ wireless debugging pairing."""
        adb = find_adb(adb_path)
        proc = subprocess.run(
            [adb, "pair", address, code],
            capture_output=True,
            timeout=90,
            creationflags=_no_window(),
        )
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        if "successfully" not in text.lower():
            raise AdbError(f"pairing with {address} failed: {text or 'no response'}")
        return text


class AdbSource(Source):
    name = "adb"

    def __init__(
        self,
        device: AdbDevice | None = None,
        *,
        serial: str | None = None,
        adb_path: str | None = None,
        use_root: bool = True,
        batch_read: bool = True,
        batch_bytes: int = 64 * 1024 * 1024,
        batch_files: int = 400,
    ) -> None:
        self.device = device or AdbDevice(serial, adb_path, use_root=use_root)
        self.batch_read = batch_read
        self.batch_bytes = batch_bytes
        self.batch_files = batch_files
        self.errors: list[str] = []
        self._tar_broken = False
        self._batch_disabled = False
        #: name -> {name, device, size}, filled in by walking PARTITION_ROOT.
        self._partitions: dict[str, dict] = {}

    def describe(self) -> str:
        return self.device.describe()

    def wait_until_ready(self, timeout: float = 600.0) -> bool:
        # A fresh transport may well support tar again -- unless batching was
        # disabled deliberately because a stream broke.
        if not self._batch_disabled:
            self._tar_broken = False
        return self.device.wait_until_ready(timeout)

    def disable_batch_read(self) -> None:
        """Stop using the tar batch path for the rest of this run."""
        self._batch_disabled = True
        self._tar_broken = True

    def uncovered_volumes(self, roots: Sequence[str], *, min_bytes: int = 1 << 30) -> list[dict]:
        """Storage volumes holding real data that ``roots`` will not reach.

        Answers the question a backup tool must never get wrong: "is there
        anything on this device I am not looking at?"
        """
        covered = [r.rstrip("/") or "/" for r in roots]
        missed = []
        for volume in self.device.storage_volumes():
            if volume["used"] < min_bytes:
                continue
            paths = {volume["path"].rstrip("/"), volume["mount"].rstrip("/")}
            # A volume counts as known about if a root sits above it, on it,
            # or anywhere inside it. Naming a folder within a volume is a
            # deliberate choice to back up part of it, and nagging about the
            # rest every run would train the user to ignore the warning --
            # which is the one thing it cannot afford.
            if any(
                root == "/"
                or any(
                    p == root or p.startswith(root + "/") or root.startswith(p + "/")
                    for p in paths
                )
                for root in covered
            ):
                continue
            missed.append(volume)
        return missed

    @property
    def root_active(self) -> bool:
        return self.device.has_root

    def _note(self, message: str) -> None:
        self.errors.append(message)

    # -- listing ----------------------------------------------------------

    def walk(self, roots: Iterable[str], matcher: Matcher) -> Iterator[SourceFile]:
        for root in roots:
            reject_host_path(root)
            root = "/" + root.strip().strip("/")
            if root == PARTITION_ROOT:
                yield from self._walk_partitions(matcher)
                continue
            if root == FIRMWARE_ROOT:
                yield from self._walk_firmware(matcher)
                continue
            if is_forbidden(root):
                self._note(f"refusing to descend into pseudo-filesystem {root}")
                continue
            yield from self._walk_root(root, matcher)

    def _walk_partitions(self, matcher: Matcher) -> Iterator[SourceFile]:
        """Present each device-unique partition as one file to be imaged."""
        if not self.device.has_root:
            self._note(
                f"{PARTITION_ROOT} needs root, and 'su' is not usable on this device; "
                f"partition images are being skipped"
            )
            return

        partitions = self.device.list_partitions()
        if not partitions:
            self._note(f"no partitions from {PARTITION_ROOT} were found on this device")
            return

        self._partitions = {p["name"]: p for p in partitions}
        yield SourceFile(path=PARTITION_ROOT, kind=KIND_DIR, mode=0o755)
        for part in partitions:
            path = f"{PARTITION_ROOT}/{part['name']}.img"
            if matcher.excluded(path):
                continue
            # A block device has no meaningful mtime, and using the phone's
            # clock would make every snapshot look changed. Zero keeps
            # consecutive backups deduplicating to nothing.
            yield SourceFile(
                path=path, kind=KIND_FILE, size=part["size"], mtime=0.0, mode=0o600,
                always_read=True,
            )

    def _walk_firmware(self, matcher: Matcher) -> Iterator[SourceFile]:
        """Present the booted slot's OS partitions as images.

        This is the rollback copy. Vendors stop hosting old builds, so the
        official image of the version you are running now is often gone by the
        time you want it back -- while the phone still has it.
        """
        if not self.device.has_root:
            self._note(
                f"{FIRMWARE_ROOT} needs root, and 'su' is not usable on this device; "
                f"firmware images are being skipped"
            )
            return

        partitions = self.device.firmware_partitions()
        if not partitions:
            self._note(f"no partitions from {FIRMWARE_ROOT} were found on this device")
            return

        self._partitions.update({p["name"]: p for p in partitions})
        yield SourceFile(path=FIRMWARE_ROOT, kind=KIND_DIR, mode=0o755)
        for part in partitions:
            path = f"{FIRMWARE_ROOT}/{part['name']}.img"
            if matcher.excluded(path):
                continue
            yield SourceFile(
                path=path, kind=KIND_FILE, size=part["size"], mtime=0.0, mode=0o600,
                always_read=True,
            )

    #: How stat gets one line per path. Kept identical across strategies so
    #: :func:`_parse_stat_line` only ever has one format to understand.
    STAT_FORMAT = f"'%f{SEP}%s{SEP}%Y{SEP}%u{SEP}%g{SEP}%n'"

    def _find_script(self, root: str, follow_root: bool, strategy: str = "xargs") -> str:
        quoted = _sh_quote(root)
        # Pruning the pseudo-filesystems matters on a rooted '/' walk:
        # descending into /proc can block indefinitely. Canonical prune form
        # -- group the paths, prune the group, then act on everything else.
        prune = " -o ".join(f"-path {_sh_quote(p)}" for p in ("/proc", "/sys", "/dev", "/acct"))
        # '-H' follows a symlink only when it is the path named on the command
        # line. That is exactly what is wanted here: '/sdcard' is itself a
        # symlink to /storage/self/primary on every modern Android, so without
        # it find reports the link and descends into nothing -- a backup that
        # "succeeds" with zero files. Symlinks *inside* the tree are still
        # recorded rather than followed, so nothing is duplicated.
        flag = "-H " if follow_root else ""
        walk = f"find {flag}{quoted} \\( {prune} \\) -prune -o"

        if strategy == "xargs":
            # Android's toybox find runs only the FIRST '-exec ... {} +'
            # batch and then stops. On a directory of half a million files
            # that silently returned about two thousand of them -- a backup
            # that looked successful while missing 99.5% of app data. xargs
            # does the batching properly, invoking stat as many times as it
            # takes, so the count is complete.
            return (
                f"{walk} -print0 2>/dev/null | "
                f"xargs -0 stat -c {self.STAT_FORMAT} 2>/dev/null"
            )
        if strategy == "exec-one":
            # One stat process per path: slow, but it cannot batch, so it
            # cannot truncate. The last resort if xargs is unavailable.
            return f"{walk} -exec stat -c {self.STAT_FORMAT} {{}} \\; 2>/dev/null"
        raise ValueError(f"unknown listing strategy {strategy!r}")

    def _walk_root(self, root: str, matcher: Matcher) -> Iterator[SourceFile]:
        entries = self._list_root(root)
        if entries is None:
            return
        symlinks: list[SourceFile] = []
        for entry in entries:
            if is_forbidden(entry.path) or matcher.excluded(entry.path, entry.is_dir):
                continue
            if entry.is_symlink:
                symlinks.append(entry)
                continue
            yield entry
        # Symlink targets need a second pass; there is no portable stat format
        # that gives an unambiguous target alongside the name.
        if symlinks:
            yield from self._resolve_symlinks(symlinks)

    def _count_paths(self, root: str, follow_root: bool) -> int:
        """How many paths the device believes are under ``root``.

        Cheap (find alone, no stat) and used purely as a cross-check: if the
        listing comes back with far fewer entries than this, something
        truncated it and the backup must not proceed as if it were complete.
        """
        flag = "-H " if follow_root else ""
        try:
            out = self.device.shell(
                f"find {flag}{_sh_quote(root)} 2>/dev/null | wc -l", check=False, timeout=1800
            )
            return int(out.strip().split()[0])
        except (AdbError, OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return -1

    #: A listing is accepted when it accounts for at least this fraction of
    #: the paths ``find`` alone could see. Files genuinely come and go on a
    #: live phone, so exactness is not achievable; anything well below this
    #: means the listing itself broke.
    COMPLETE_ENOUGH = 0.98

    def _list_root(self, root: str) -> list[SourceFile] | None:
        """Enumerate one root, retrying until the listing looks complete.

        Two things can go wrong and both have been seen on real hardware:
        ``find`` refusing to descend a symlinked root, and ``find`` silently
        truncating a large listing. So each combination is tried, the result
        is checked against a cheap independent path count, and the largest
        usable listing wins.
        """
        best: list[SourceFile] = []
        best_expected = 0
        best_skipped = 0
        last_error = ""
        saw_only_root_symlink = False

        for follow_root in (True, False):
            expected = self._count_paths(root, follow_root)
            for strategy in ("xargs", "exec-one"):
                entries, skipped, last_error = self._run_find(
                    self._find_script(root, follow_root, strategy)
                )
                # find counts every path; the parser drops sockets, fifos and
                # device nodes. Comparing the two directly made any directory
                # full of sockets look permanently truncated -- /data/vendor
                # on this phone holds 979 wpa_supplicant control sockets, so a
                # complete listing reported 927 of 1906 and cried INCOMPLETE
                # every single run. A guard that cries wolf is worse than none.
                covered = len(entries) + skipped
                # Exactly one symlink means find reported the root itself and
                # descended into nothing. Another strategy will not help;
                # the other -H setting might.
                if len(entries) == 1 and entries[0].is_symlink:
                    saw_only_root_symlink = True
                    break
                if not entries:
                    continue
                if len(entries) > len(best):
                    best, best_expected, best_skipped = entries, expected, skipped
                if expected <= 0 or covered >= expected * self.COMPLETE_ENOUGH:
                    return entries
                self._note(
                    f"listing {root} with the '{strategy}' strategy returned "
                    f"{covered} of {expected} paths; trying another way"
                )

        if best:
            covered = len(best) + best_skipped
            if best_expected > 0 and covered < best_expected * self.COMPLETE_ENOUGH:
                self._note(
                    f"WARNING: only listed {covered} of {best_expected} paths under "
                    f"{root} -- the backup of {root} is INCOMPLETE"
                )
            return best

        if saw_only_root_symlink:
            self._note(
                f"{root} is a symlink and this device's find would not follow it; "
                f"back up its target instead"
            )
        else:
            self._note(
                f"nothing readable under {root}"
                + (f": {last_error}" if last_error else "")
                + (" (root access may be required)" if not self.device.has_root else "")
            )
        return None

    def _run_find(self, script: str) -> tuple[list[SourceFile], int, str]:
        """Parse a listing into (entries, paths deliberately skipped, stderr)."""
        proc = self.device.popen(script)
        found: list[SourceFile] = []
        skipped = 0
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "surrogateescape").rstrip("\r\n")
                if not line:
                    continue
                entry = _parse_stat_line(line)
                if entry is SKIPPED_SPECIAL:
                    skipped += 1
                elif entry is not None:
                    found.append(entry)
            stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
        finally:
            _reap(proc)
        return found, skipped, stderr.strip()

    def _resolve_symlinks(self, entries: list[SourceFile]) -> Iterator[SourceFile]:
        by_path = {e.path: e for e in entries}
        lines = [
            f"printf '%s{SEP}%s\\n' {_sh_quote(p)} \"$(readlink {_sh_quote(p)} 2>/dev/null)\""
            for p in by_path
        ]
        # Split into scripts that comfortably fit a command line. A phone with
        # a few thousand symlinks would otherwise build one script far past
        # what the host will accept. Base64 inflates by 4/3, hence the margin.
        budget = int(MAX_COMMAND_CHARS * 0.6)
        for script in _chunk_script(lines, budget):
            try:
                out = self.device.shell(script, check=False, timeout=180)
            except (AdbError, OSError, subprocess.TimeoutExpired) as exc:
                self._note(f"could not read some symlink targets: {exc}")
                continue
            for line in out.splitlines():
                path, _, target = line.rstrip("\r\n").partition(SEP)
                if path in by_path:
                    by_path[path].target = target
        yield from by_path.values()

    # -- reading ----------------------------------------------------------

    def open(self, path: str, size: int = -1) -> IO[bytes]:
        if path.startswith(PARTITION_ROOT + "/") or path.startswith(FIRMWARE_ROOT + "/"):
            return self._open_partition(path, size)
        proc = self.device.popen(f"cat -- {_sh_quote(path)}")
        return _ProcessReader(proc, path, size)

    def _open_partition(self, path: str, size: int = -1) -> IO[bytes]:
        root = FIRMWARE_ROOT if path.startswith(FIRMWARE_ROOT + "/") else PARTITION_ROOT
        name = path[len(root) + 1 :].removesuffix(".img")
        part = self._partitions.get(name)
        if part is None:
            # A restore or a resumed run can ask for a partition without
            # having walked first, so look it up rather than failing.
            found = self.device.list_partitions([name])
            if not found:
                raise AdbError(f"this device has no partition named {name!r}")
            part = found[0]
        # cat, not dd: a block device returns EOF at its own end, and
        # _ProcessReader's "exit 0 means a complete read" rule is written
        # around cat's exit semantics.
        proc = self.device.popen(f"cat -- {_sh_quote(part['device'])}", root=True)
        return _ProcessReader(
            proc, path, part["size"] if size < 0 else size, exact=True
        )

    def read_batch(self, files: Sequence[SourceFile]) -> Iterator[tuple[SourceFile, IO[bytes]]]:
        """Yield ``(file, stream)`` for many files using as few processes as possible.

        Each stream is only valid until the next one is requested, because
        they are slices of a single tar stream. Callers must consume one
        fully before moving on -- which is exactly what chunking does.
        """
        if not files:
            return

        # Partition images have no path on the phone for tar to open, so they
        # always go through open() one at a time. There are a dozen of them at
        # most, so the batching they miss out on does not matter.
        virtual = (PARTITION_ROOT + "/", FIRMWARE_ROOT + "/")
        images = [f for f in files if f.path.startswith(virtual)]
        if images:
            files = [f for f in files if not f.path.startswith(virtual)]
            yield from self._read_one_by_one(images)
            if not files:
                return

        if not self.batch_read or self._tar_broken:
            yield from self._read_one_by_one(files)
            return

        for group in _group_by_budget(files, self.batch_bytes, self.batch_files):
            produced = False
            try:
                for item in self._read_via_tar(group):
                    produced = True
                    yield item
            except _TarUnusable as exc:
                if produced:
                    # A partial group cannot be safely retried: some files
                    # were already handed to the caller and chunked.
                    raise AdbError(
                        f"the device's tar failed midway through a batch ({exc}); "
                        f"re-run with --no-batch-read"
                    ) from exc
                self._tar_broken = True
                self._note(f"batched reads unavailable ({exc}); falling back to one file at a time")
                yield from self._read_one_by_one(group)

    def _read_one_by_one(
        self, files: Sequence[SourceFile]
    ) -> Iterator[tuple[SourceFile, IO[bytes]]]:
        for meta in files:
            stream = self.open(meta.path, meta.size)
            try:
                yield meta, stream
            finally:
                stream.close()

    def _read_via_tar(self, files: Sequence[SourceFile]) -> Iterator[tuple[SourceFile, IO[bytes]]]:
        by_name = {_tar_name(f.path): f for f in files}
        listing = "".join(f.path + "\n" for f in files)
        remote_list = f"/data/local/tmp/pv-list-{os.urandom(6).hex()}"

        # The list is pushed as a file rather than echoed through a shell
        # command. Hundreds of paths do not fit on a command line -- Windows
        # caps CreateProcess at 32 KB, and the device shell has limits of its
        # own -- and blowing that limit silently demoted every backup to
        # one adb process per file.
        try:
            self.device.push_bytes(listing.encode("utf-8", "surrogateescape"), remote_list)
        except (AdbError, OSError, subprocess.TimeoutExpired) as exc:
            raise _TarUnusable(f"cannot stage the file list on the device: {exc}") from exc

        proc = self.device.popen(f"tar -c -f - -T {remote_list} 2>/dev/null; rm -f {remote_list}")
        assert proc.stdout is not None
        seen: set[str] = set()
        try:
            try:
                archive = tarfile.open(fileobj=proc.stdout, mode="r|")
            except tarfile.TarError as exc:
                raise _TarUnusable(f"tar produced no readable archive: {exc}") from exc
            try:
                while True:
                    try:
                        member = archive.next()
                    except tarfile.TarError as exc:
                        raise _TarUnusable(f"tar stream is malformed: {exc}") from exc
                    if member is None:
                        break
                    if not member.isfile():
                        continue
                    meta = by_name.get(member.name.lstrip("./"))
                    if meta is None:
                        continue
                    stream = archive.extractfile(member)
                    if stream is None:
                        continue
                    seen.add(meta.path)
                    # The caller reads this stream after we yield, so a
                    # truncation surfaces in *their* stack, outside every
                    # try block here. Wrapping it is the only way to turn
                    # that into an error the engine can act on.
                    yield meta, _TarMemberStream(stream, meta.path)
            finally:
                archive.close()
        finally:
            _reap(proc)

        if not seen:
            raise _TarUnusable("tar returned no files")
        for meta in files:
            if meta.path not in seen:
                # Recoverable: tar skipped one file, so read that one alone
                # rather than abandoning the whole batch.
                stream = self.open(meta.path, meta.size)
                try:
                    yield meta, stream
                finally:
                    stream.close()


class _TarUnusable(Exception):
    """The device's tar cannot be used; fall back to per-file reads."""


class _TarMemberStream:
    """One file inside a tar stream, with tar errors typed on the way out."""

    def __init__(self, stream: IO[bytes], path: str) -> None:
        self._stream = stream
        self._path = path

    def read(self, size: int = -1) -> bytes:
        try:
            return self._stream.read() if size is None or size < 0 else self._stream.read(size)
        except tarfile.TarError as exc:
            raise BatchReadError(
                f"the batched read stream broke while reading {self._path}: {exc}. "
                f"This usually means a file changed size while it was being "
                f"archived; the remaining files will be read individually."
            ) from exc

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self._stream.close()
        except Exception:
            pass


class _ProcessReader:
    """A read-only binary stream backed by an adb subprocess.

    ``cat`` on a file it cannot open writes to stderr and exits non-zero while
    producing no data, which would otherwise be indistinguishable from an
    empty file. Both the exit status and the byte count are checked on close.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        path: str,
        expected_size: int = -1,
        *,
        exact: bool = False,
    ) -> None:
        self._proc = proc
        self._path = path
        self._expected = expected_size
        self._read = 0
        self._closed = False
        #: Refuse a short read instead of treating it as the file shrinking.
        #:
        #: A file on a live phone legitimately changes size between the stat
        #: and the read, so a shortfall there is normal. A block device does
        #: not: its size is fixed, so a shortfall means the transport dropped
        #: data. Storing that silently produced a 1.8 GiB image of a 13.5 GiB
        #: partition, and the run reported no unreadable files.
        self._exact = exact
        #: Set when the file turned out to be a different size than stat said.
        self.size_changed = False
        self._stderr = b""
        # stderr must be drained concurrently or a chatty su will fill the
        # pipe buffer and deadlock the transfer.
        self._drain = threading.Thread(target=self._collect_stderr, daemon=True)
        self._drain.start()

    def _collect_stderr(self) -> None:
        if self._proc.stderr is not None:
            try:
                self._stderr = self._proc.stderr.read()
            except (OSError, ValueError):
                self._stderr = b""

    def read(self, size: int = -1) -> bytes:
        assert self._proc.stdout is not None
        data = self._proc.stdout.read() if size is None or size < 0 else self._proc.stdout.read(size)
        self._read += len(data)
        return data

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        code = _reap(self._proc)
        self._drain.join(timeout=5)
        err = self._stderr.decode("utf-8", "replace").strip()
        if looks_like_lost_device(err):
            raise DeviceLostError(f"the device went away while reading {self._path}: {err}")

        if code == 0:
            # cat only exits 0 once it has read to end of file, so whatever
            # arrived is the whole file *as it was at that moment*. A size
            # that disagrees with the earlier stat means the file changed in
            # between -- routine for logs and app databases on a live phone,
            # and emphatically not a lost device. The engine records the
            # bytes actually read, so the snapshot stays self-consistent.
            short = self._expected >= 0 and self._read != self._expected
            if short and self._exact:
                # A partition is a fixed-size object, so this is lost data and
                # not a file that changed underneath us. Reported as a lost
                # device so the engine retries rather than storing a truncated
                # image, which is what it used to do without a word.
                raise DeviceLostError(
                    f"{self._path} is a fixed-size image but only {self._read:,} of "
                    f"{self._expected:,} bytes arrived; the connection dropped data"
                )
            self.size_changed = short
            return

        if self._read == 0:
            raise AdbError(f"cannot read {self._path}: {err or f'cat exited {code}'}")
        # Non-zero exit after partial data: the transfer really was cut off.
        raise DeviceLostError(
            f"transfer of {self._path} was cut off after {self._read} of "
            f"{self._expected} bytes (cat exited {code})" + (f": {err}" if err else "")
        )

    def __enter__(self) -> _ProcessReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# -- helpers --------------------------------------------------------------


def _no_window() -> int:
    # Keep a console window from flashing up for every adb call when the
    # scheduler or the web UI runs a backup.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _reap(proc: subprocess.Popen) -> int | None:
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    try:
        return proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            return None


def _sh_quote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def _tar_name(path: str) -> str:
    return path.lstrip("/")


#: Returned for a path that exists but holds nothing to back up.
SKIPPED_SPECIAL = SourceFile(path="\x00skipped")


def _parse_stat_line(line: str) -> SourceFile | None:
    parts = line.split(SEP, 5)
    if len(parts) != 6:
        return None
    raw_mode, size, mtime, uid, gid, path = parts
    try:
        mode = int(raw_mode, 16)
        entry = SourceFile(
            path=path,
            size=int(size),
            mtime=float(mtime),
            mode=mode & 0o7777,
            uid=int(uid),
            gid=int(gid),
        )
    except ValueError:
        return None
    file_type = mode & 0o170000
    if file_type == 0o040000:
        entry.kind = KIND_DIR
        entry.size = 0
    elif file_type == 0o120000:
        entry.kind = KIND_SYMLINK
        entry.size = 0
    elif file_type == 0o100000:
        entry.kind = KIND_FILE
    else:
        # Sockets, fifos and device nodes are kernel objects, not data. They
        # are skipped -- but reported as SKIPPED_SPECIAL rather than None, so
        # the completeness check can tell "deliberately not backed up" apart
        # from "the listing broke". By count alone the two look identical.
        return SKIPPED_SPECIAL
    return entry


def _group_by_budget(
    files: Sequence[SourceFile], max_bytes: int, max_files: int
) -> Iterator[list[SourceFile]]:
    group: list[SourceFile] = []
    total = 0
    for meta in files:
        # A file bigger than the whole budget goes in a batch by itself
        # rather than dragging others into an oversized transfer.
        if group and (total + meta.size > max_bytes or len(group) >= max_files):
            yield group
            group, total = [], 0
        group.append(meta)
        total += meta.size
    if group:
        yield group


#: Directories that only exist on the PC. Seeing one as an Android path almost
#: always means a Unix-style shell rewrote the argument on its way in.
_MSYS_MARKERS = ("/git/", "/mingw64/", "/msys64/", "/program files/", "/usr/bin/")


def looks_like_host_path(root: str) -> bool:
    """True if this is a path on this computer rather than on the phone."""
    lowered = root.replace("\\", "/").lower()
    looks_like_windows = len(root) > 1 and root[1] == ":"
    return looks_like_windows or any(marker in lowered for marker in _MSYS_MARKERS)


def reject_host_path(root: str) -> None:
    """Catch a PC path that was meant to be a path on the phone.

    Git Bash and MSYS rewrite a leading-slash argument into a Windows path
    before the program ever sees it, so ``--roots /sdcard/DCIM`` silently
    becomes ``C:/Program Files/Git/sdcard/DCIM``. Backing that up finds
    nothing and reports success, which is the worst possible outcome for a
    backup tool -- so it is refused with an explanation instead.
    """
    if looks_like_host_path(root):
        raise AdbError(
            f"{root!r} is a path on this computer, not on the phone.\n"
            f"Android paths start at /sdcard or /data.\n"
            f"If you are in Git Bash or MSYS, it rewrote the path for you -- "
            f"prefix the command with MSYS_NO_PATHCONV=1, double the slash "
            f"(//sdcard/DCIM), or use PowerShell."
        )


#: Fragments adb prints when the transport, rather than one file, has failed.
_LOST_DEVICE_SIGNS = (
    "device not found",
    "device offline",
    "no devices/emulators found",
    "device still authorizing",
    "error: closed",
    "protocol fault",
    "connection reset",
    "adb: device unauthorized",
    "cannot connect to daemon",
    "transport endpoint is not connected",
)


def looks_like_lost_device(message: str) -> bool:
    lowered = (message or "").lower()
    return any(sign in lowered for sign in _LOST_DEVICE_SIGNS)


def _chunk_script(lines: Sequence[str], budget: int) -> Iterator[str]:
    """Group shell lines into scripts no longer than ``budget`` characters."""
    current: list[str] = []
    size = 0
    for line in lines:
        cost = len(line) + 1
        if current and size + cost > budget:
            yield "\n".join(current)
            current, size = [], 0
        current.append(line)
        size += cost
    if current:
        yield "\n".join(current)
