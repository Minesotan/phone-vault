"""Restoring files out of a snapshot.

Two destinations are supported: a folder on this machine, and the phone
itself over adb. Restoring to a folder first and checking it is the safer
habit, so that is the default everywhere.

Windows needs care. Android filenames routinely contain characters that
NTFS rejects -- ``:`` above all -- and paths can exceed the legacy limit.
Rather than fail late, halfway through a restore, offending names are
sanitised up front and every substitution is reported.
"""

from __future__ import annotations

import os
import posixpath
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import RestoreError
from .progress import Progress
from .repo import Repository
from .snapshot import KIND_DIR, FileEntry, Snapshot

#: Characters NTFS refuses in a filename. The control range is included
#: because Linux permits it and Windows does not.
_WINDOWS_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')

#: Names Windows reserves regardless of extension.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass
class RestoreStats:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_written: int = 0
    renamed: int = 0
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["error_count"] = len(self.errors)
        data["errors"] = self.errors[:100]
        return data


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    """Whether ``path`` is selected by any of ``patterns``.

    A pattern selects a path when it equals it, is a parent directory of it,
    or matches it as a glob. That makes ``--include /sdcard/DCIM`` restore the
    whole folder, which is what people mean by it.
    """
    if not patterns:
        return True
    import fnmatch

    for pattern in patterns:
        pattern = pattern.rstrip("/") or "/"
        if path == pattern or path.startswith(pattern + "/"):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def sanitize_component(name: str) -> tuple[str, bool]:
    """Make one path component safe for this platform."""
    if os.name != "nt":
        return name, False
    cleaned = _WINDOWS_ILLEGAL.sub("_", name)
    # Windows silently strips trailing dots and spaces, which would make two
    # distinct Android files collide.
    stripped = cleaned.rstrip(" .")
    if stripped != cleaned:
        cleaned = stripped + "_"
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    return (cleaned or "_"), cleaned != name


class LocalTarget:
    """Writes restored files into a directory on this machine."""

    def __init__(self, base: str | os.PathLike) -> None:
        self.base = Path(base).resolve()
        self.renames: list[tuple[str, str]] = []

    def describe(self) -> str:
        return str(self.base)

    def map_path(self, device_path: str) -> Path:
        parts = [p for p in device_path.split("/") if p and p != "."]
        # Reject '..' outright rather than trying to resolve it. A snapshot
        # is data from a phone, and a path that climbs out of the restore
        # folder has no legitimate meaning. On Windows the sanitiser happens
        # to neutralise it; on Linux and in Termux it would not.
        if any(part == ".." for part in parts):
            raise RestoreError(
                f"refusing to write outside the restore folder: {device_path}"
            )
        safe: list[str] = []
        changed = False
        for part in parts:
            cleaned, was_changed = sanitize_component(part)
            changed = changed or was_changed
            safe.append(cleaned)
        target = self.base.joinpath(*safe) if safe else self.base
        if changed:
            self.renames.append((device_path, str(target)))
        # Belt and braces: a crafted snapshot must not write outside the
        # restore directory.
        resolved = Path(os.path.normpath(target))
        if self.base != resolved and self.base not in resolved.parents:
            raise RestoreError(f"refusing to write outside the restore folder: {device_path}")
        return resolved

    def make_dir(self, entry: FileEntry) -> None:
        path = self.map_path(entry.path)
        path.mkdir(parents=True, exist_ok=True)

    def write_file(self, entry: FileEntry, blocks: Iterable[bytes]) -> int:
        path = self.map_path(entry.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        tmp = path.with_name(path.name + ".pv-part")
        with open(tmp, "wb") as fh:
            for block in blocks:
                fh.write(block)
                written += len(block)
        os.replace(tmp, path)
        return written

    def make_symlink(self, entry: FileEntry) -> None:
        path = self.map_path(entry.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        try:
            os.symlink(entry.target, path)
        except (OSError, NotImplementedError) as exc:
            # Windows needs Developer Mode or admin rights for symlinks.
            # A text placeholder loses nothing that was not already lost.
            path.write_text(f"symlink -> {entry.target}\n", encoding="utf-8")
            raise RestoreError(f"stored {entry.path} as a placeholder: {exc}") from exc

    def apply_metadata(self, entry: FileEntry) -> None:
        path = self.map_path(entry.path)
        try:
            if entry.mtime:
                os.utime(path, (entry.mtime, entry.mtime), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass
        if os.name != "nt" and entry.mode:
            try:
                os.chmod(path, entry.mode)
            except OSError:
                pass

    def exists(self, entry: FileEntry) -> bool:
        return self.map_path(entry.path).exists()

    def matches(self, entry: FileEntry) -> bool:
        """Whether what is already there looks like what we would write."""
        path = self.map_path(entry.path)
        try:
            st = path.stat()
        except OSError:
            return False
        return st.st_size == entry.size and abs(st.st_mtime - entry.mtime) <= 1.0

    def close(self) -> None:
        pass


class AdbTarget:
    """Pushes restored files back onto the phone.

    Content is staged in a local temporary file and then pushed, rather than
    piped through a shell. ``adb push`` handles large binaries, resumes
    nothing, and reports failure honestly, which piping into ``cat`` does not.
    """

    def __init__(self, device, base: str = "/sdcard/Restored", *, use_root: bool = True) -> None:
        self.device = device
        self.base = "/" + base.strip("/") if base.strip("/") else "/"
        self.use_root = use_root
        self.renames: list[tuple[str, str]] = []
        self._tmp_dir = None

    def describe(self) -> str:
        return f"{self.device.describe()}:{self.base}"

    def map_path(self, device_path: str) -> str:
        from .sources.adb import FIRMWARE_ROOT, PARTITION_ROOT

        for virtual in (PARTITION_ROOT, FIRMWARE_ROOT):
            if device_path == virtual or device_path.startswith(virtual + "/"):
                break
        else:
            virtual = ""
        if virtual:
            # Copying the image to /sdcard would look like it worked and
            # restore nothing. Writing it to the block device instead is not
            # something this tool will do unprompted: the wrong image, or the
            # right one written while Android is running, bricks the phone.
            raise RestoreError(
                f"{device_path} is a partition image, and partitions are not restored "
                f"by copying files onto a running phone.\n"
                f"Restore it to a folder instead (phonevault restore --target <dir>), "
                f"then flash it deliberately from fastboot -- after checking it is the "
                f"image for this exact handset."
            )
        rel = device_path.lstrip("/")
        return posixpath.join(self.base, rel) if self.base != "/" else "/" + rel

    def make_dir(self, entry: FileEntry) -> None:
        from .sources.adb import _sh_quote

        self.device.shell(f"mkdir -p {_sh_quote(self.map_path(entry.path))}", check=False)

    def write_file(self, entry: FileEntry, blocks: Iterable[bytes]) -> int:
        import subprocess
        import tempfile

        from .sources.adb import _no_window, _sh_quote

        remote = self.map_path(entry.path)
        self.device.shell(f"mkdir -p {_sh_quote(posixpath.dirname(remote))}", check=False)

        written = 0
        fd, staged = tempfile.mkstemp(prefix="pv-restore-")
        try:
            with os.fdopen(fd, "wb") as fh:
                for block in blocks:
                    fh.write(block)
                    written += len(block)
            # Push to a world-writable staging path first: adb push cannot
            # write into root-owned directories even when su is available.
            staging = f"/data/local/tmp/pv-push-{os.urandom(6).hex()}"
            proc = subprocess.run(
                [self.device.adb, "-s", self.device.serial, "push", staged, staging],
                capture_output=True,
                timeout=3600,
                creationflags=_no_window(),
            )
            if proc.returncode != 0:
                raise RestoreError(
                    f"adb push failed for {entry.path}: "
                    f"{proc.stderr.decode('utf-8', 'replace').strip()}"
                )
            self.device.shell(
                f"cat {_sh_quote(staging)} > {_sh_quote(remote)} && rm -f {_sh_quote(staging)}",
                check=True,
            )
        finally:
            try:
                os.unlink(staged)
            except OSError:
                pass
        return written

    def make_symlink(self, entry: FileEntry) -> None:
        from .sources.adb import _sh_quote

        remote = self.map_path(entry.path)
        self.device.shell(
            f"mkdir -p {_sh_quote(posixpath.dirname(remote))} && "
            f"ln -sf {_sh_quote(entry.target)} {_sh_quote(remote)}",
            check=False,
        )

    def apply_metadata(self, entry: FileEntry) -> None:
        from .sources.adb import _sh_quote

        if entry.mtime:
            stamp = time.strftime("%Y%m%d%H%M.%S", time.localtime(entry.mtime))
            self.device.shell(
                f"touch -t {stamp} {_sh_quote(self.map_path(entry.path))}", check=False
            )

    def exists(self, entry: FileEntry) -> bool:
        from .sources.adb import _sh_quote

        out = self.device.shell(
            f"[ -e {_sh_quote(self.map_path(entry.path))} ] && echo yes", check=False
        )
        return "yes" in out

    def matches(self, entry: FileEntry) -> bool:
        return False  # too slow to check per file over adb; rely on overwrite policy

    def close(self) -> None:
        pass


class RestoreRunner:
    def __init__(self, repo: Repository, progress: Progress | None = None) -> None:
        self.repo = repo
        self.progress = progress or Progress()

    def restore(
        self,
        snapshot: Snapshot,
        target: LocalTarget | AdbTarget,
        *,
        include: Sequence[str] = (),
        overwrite: str = "different",
        dry_run: bool = False,
        verify: bool = True,
    ) -> RestoreStats:
        """Restore ``snapshot`` into ``target``.

        ``overwrite`` is one of ``always``, ``never``, or ``different`` --
        the last skips files already present at the right size and time.
        """
        if overwrite not in ("always", "never", "different"):
            raise RestoreError(f"unknown overwrite policy {overwrite!r}")

        started = time.time()
        stats = RestoreStats()
        self.repo.sync_index()
        reader = self.repo.blob_reader()

        selected = [e for e in self.repo.read_tree(snapshot, reader) if matches_any(e.path, include)]
        if not selected:
            what = f" matching {', '.join(include)}" if include else ""
            raise RestoreError(f"snapshot {snapshot.short_id} contains nothing{what}")

        total_bytes = sum(e.size for e in selected if e.is_file)
        total_files = sum(1 for e in selected if e.is_file)
        self.progress.start("Restoring", total_files, total_bytes)
        self.progress.message(
            f"{total_files} files ({_fmt(total_bytes)}) from snapshot "
            f"{snapshot.short_id} into {target.describe()}"
        )

        if dry_run:
            for entry in selected:
                self.progress.message(f"would restore {entry.path}")
                if entry.is_file:
                    stats.files += 1
                elif entry.is_dir:
                    stats.dirs += 1
                else:
                    stats.symlinks += 1
            stats.duration = time.time() - started
            self.progress.finish(stats.as_dict())
            return stats

        # Directories first, deepest last, so children always have a parent.
        for entry in sorted((e for e in selected if e.is_dir), key=lambda e: e.path):
            try:
                target.make_dir(entry)
                stats.dirs += 1
            except (OSError, RestoreError) as exc:
                stats.failed += 1
                stats.errors.append(f"{entry.path}: {exc}")
                self.progress.warn(f"could not create {entry.path}: {exc}")

        for entry in selected:
            if entry.is_dir:
                continue
            if entry.is_symlink:
                try:
                    target.make_symlink(entry)
                    stats.symlinks += 1
                except (OSError, RestoreError) as exc:
                    stats.errors.append(f"{entry.path}: {exc}")
                    self.progress.warn(str(exc))
                continue

            self.progress.file_started(entry.path, entry.size)
            try:
                if self._should_skip(entry, target, overwrite):
                    stats.skipped += 1
                    self.progress.advance_bytes(entry.size)
                    self.progress.file_done(entry.path, "skipped", entry.size)
                    continue
                written = target.write_file(entry, self._blocks(entry, reader, verify))
                target.apply_metadata(entry)
                stats.files += 1
                stats.bytes_written += written
                self.progress.file_done(entry.path, "restored", entry.size)
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"{entry.path}: {exc}")
                self.progress.warn(f"failed to restore {entry.path}: {exc}")
                self.progress.file_done(entry.path, "failed", entry.size)

        stats.renamed = len(getattr(target, "renames", ()))
        if stats.renamed:
            self.progress.warn(
                f"{stats.renamed} name(s) contained characters this platform rejects "
                f"and were written with '_' substituted"
            )
        stats.duration = time.time() - started
        self.progress.finish(stats.as_dict())
        return stats

    def _should_skip(self, entry: FileEntry, target, overwrite: str) -> bool:
        if overwrite == "always":
            return False
        exists = target.exists(entry)
        if not exists:
            return False
        if overwrite == "never":
            return True
        return target.matches(entry)

    def _blocks(self, entry: FileEntry, reader, verify: bool) -> Iterator[bytes]:
        for digest in entry.chunks:
            block = reader.get(digest, verify=verify)
            self.progress.advance_bytes(len(block))
            yield block

    # -- browsing ----------------------------------------------------------

    def read_file(self, snapshot: Snapshot, path: str, *, verify: bool = True) -> Iterator[bytes]:
        """Stream one file's content out of a snapshot."""
        reader = self.repo.blob_reader()
        for entry in self.repo.read_tree(snapshot, reader):
            if entry.path == path:
                if not entry.is_file:
                    raise RestoreError(f"{path} is not a regular file in this snapshot")
                for digest in entry.chunks:
                    yield reader.get(digest, verify=verify)
                return
        raise RestoreError(f"{path} is not in snapshot {snapshot.short_id}")


def list_directory(entries: Iterable[FileEntry], prefix: str) -> list[FileEntry]:
    """The immediate children of ``prefix``, for a browsable view."""
    # Device paths are absolute posix, but a Windows-style root can reach
    # here from a local-source backup, so only normalise the trailing slash.
    prefix = prefix.rstrip("/") or "/"
    base = "/" if prefix == "/" else prefix + "/"
    children: dict[str, FileEntry] = {}
    for entry in entries:
        if entry.path == prefix or not entry.path.startswith(base):
            continue
        remainder = entry.path[len(base) :]
        if not remainder:
            continue
        head, slash, _tail = remainder.partition("/")
        if not slash:
            children[head] = entry
        elif head not in children:
            # An implied directory: present in the tree only through its
            # descendants, which happens when a root was excluded.
            children[head] = FileEntry(path=base + head, kind=KIND_DIR)
    return sorted(children.values(), key=lambda e: (e.kind != KIND_DIR, e.name.lower()))


def _fmt(count: float) -> str:
    from .progress import format_bytes

    return format_bytes(count)
