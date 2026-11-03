"""User configuration: named profiles describing what to back up, and where.

A profile is everything one backup job needs, so that routine use is
``phonevault backup`` with no arguments and the scheduler has nothing to
remember.

Secrets are deliberately awkward to store here. The config file is plain
JSON in the user's profile directory, so an SMB password written into it is
readable by anything running as that user. The password is therefore read
from the environment first, and writing one into the file requires saying so
explicitly.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import PhonevaultError

CONFIG_VERSION = 1

#: Checked before the config file, so a scheduled task can supply a password
#: without it ever being written to disk.
SMB_PASSWORD_ENV = "PHONEVAULT_SMB_PASSWORD"


def _file_stamp(path: Path) -> tuple[int, int] | None:
    """Cheap identity for a file's contents: modification time and size."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def default_config_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "phonevault" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "phonevault" / "config.json"


#: Sensible starting point for a rooted Android phone. ``/sdcard`` holds
#: photos, downloads and documents -- the things people actually mind losing.
#: ``/data`` needs root and is opt-in because it is large, full of caches, and
#: only restorable to the same device.
DEFAULT_ROOTS = ["/sdcard"]

#: Everything on the phone that the phone does not regenerate by itself.
#:
#: Naming the parts rather than just ``/data`` is deliberate. ``/data/media``
#: is the same tree as ``/sdcard`` and ``/data/user`` is the same tree as
#: ``/data/data`` -- both are bind mounts, so a plain ``/data`` walks a
#: quarter of a million paths twice for nothing. The rest of ``/data`` is
#: rebuilt by the OS (``dalvik-cache``, ``apex``, ``preloads``, ``rollback``)
#: or is crash debris (``tombstones``), and would add churn to every run.
#:
#: An SD card is not here because its path contains a per-card UUID. The
#: uncovered-volume warning names it at the start of a backup.
EVERYTHING_ROOTS = [
    "/sdcard",  # internal shared storage; the same tree as /data/media/0
    "/data/data",  # app private data; the same tree as /data/user/0
    "/data/app",  # installed apps
    "/data/user_de",  # app data readable before first unlock
    "/data/adb",  # root/Magisk modules
    "/data/misc",  # wifi, bluetooth pairings, VPN profiles
    "/data/system_ce",  # per-user system data
    "/data/system",  # accounts, settings, package records
    "/data/vendor",  # vendor-specific device state
    "/partitions",  # raw images of the partitions a firmware flash cannot restore
]

ROOT_PRESETS: dict[str, list[str]] = {
    "media": ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies", "/sdcard/Music"],
    "sdcard": ["/sdcard"],
    "sdcard+appdata": ["/sdcard", "/data/data"],
    "everything": list(EVERYTHING_ROOTS),
}


@dataclass
class RetentionConfig:
    last: int | None = None
    hourly: int | None = None
    daily: int | None = 7
    weekly: int | None = 4
    monthly: int | None = 12
    yearly: int | None = 3
    within_days: float | None = None
    keep_tags: list[str] = field(default_factory=list)

    def to_policy(self):
        from .prune import RetentionPolicy

        return RetentionPolicy(
            last=self.last,
            hourly=self.hourly,
            daily=self.daily,
            weekly=self.weekly,
            monthly=self.monthly,
            yearly=self.yearly,
            within_days=self.within_days,
            keep_tags=tuple(self.keep_tags),
        )


@dataclass
class Profile:
    """One backup job."""

    name: str = "phone"
    repository: str = ""
    source: str = "adb"
    roots: list[str] = field(default_factory=lambda: list(DEFAULT_ROOTS))
    excludes: list[str] = field(default_factory=list)
    use_default_excludes: bool = True
    tags: list[str] = field(default_factory=list)

    device: str | None = None
    adb_path: str | None = None
    use_root: bool = True
    batch_read: bool = True

    smb_username: str | None = None
    smb_password: str | None = None

    schedule: str = ""
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    prune_after_forget: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("name", None)
        return data

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Profile:
        retention = RetentionConfig(**{**asdict(RetentionConfig()), **data.get("retention", {})})
        known = {f for f in asdict(cls()) if f != "retention"}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.pop("name", None)
        return cls(name=name, retention=retention, **kwargs)

    # -- derived ----------------------------------------------------------

    def resolved_smb_password(self) -> str | None:
        return os.environ.get(SMB_PASSWORD_ENV) or self.smb_password

    def backend_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.smb_username:
            options["username"] = self.smb_username
        password = self.resolved_smb_password()
        if password:
            options["password"] = password
        return options

    def source_options(self) -> dict[str, Any]:
        if self.source == "adb":
            return {
                "serial": self.device,
                "adb_path": self.adb_path,
                "use_root": self.use_root,
                "batch_read": self.batch_read,
            }
        return {"label": self.name}

    def validate(self) -> list[str]:
        problems = []
        if not self.repository:
            problems.append(
                f"profile '{self.name}' has no repository; "
                f"set one with: phonevault config set repository <path>"
            )
        if not self.roots:
            problems.append(f"profile '{self.name}' has no source paths to back up")
        if self.source == "adb":
            from .sources.adb import looks_like_host_path

            for root in self.roots:
                if looks_like_host_path(root):
                    problems.append(
                        f"profile '{self.name}' has '{root}' as a source path, which is a "
                        f"path on this computer, not on the phone -- it would back up nothing"
                    )
        if self.source not in ("adb", "local"):
            problems.append(f"profile '{self.name}' has unknown source type '{self.source}'")
        return problems


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8723
    open_browser: bool = True


class Config:
    """The whole configuration file."""

    def __init__(self, path: Path | None = None, data: dict[str, Any] | None = None) -> None:
        self.path = Path(path) if path else default_config_path()
        #: Identifies the file contents this object was built from, so a
        #: long-lived holder (the web server) can notice an edit made
        #: elsewhere. ``None`` means "not read from a file".
        self._stamp: tuple[int, int] | None = None
        self._apply(data or {})

    def _apply(self, data: dict[str, Any]) -> None:
        self.version = int(data.get("version", CONFIG_VERSION))
        self.default_profile = data.get("default_profile", "phone")
        self.web = WebConfig(**{**asdict(WebConfig()), **data.get("web", {})})
        self.profiles: dict[str, Profile] = {
            name: Profile.from_dict(name, payload)
            for name, payload in data.get("profiles", {}).items()
        }
        if not self.profiles:
            self.profiles = {"phone": Profile(name="phone")}

    # -- load / save --------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Config:
        target = Path(path) if path else default_config_path()
        if not target.exists():
            return cls(target)
        # Stamp before reading, never after: a write landing between the two
        # would otherwise be recorded as already-seen and never picked up.
        stamp = _file_stamp(target)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PhonevaultError(f"cannot read config at {target}: {exc}") from exc
        if int(data.get("version", CONFIG_VERSION)) > CONFIG_VERSION:
            raise PhonevaultError(
                f"config at {target} was written by a newer version of phonevault"
            )
        config = cls(target, data)
        config._stamp = stamp
        return config

    def reload_if_changed(self) -> bool:
        """Re-read the file if another process has written it since we last did.

        The web server builds one :class:`Config` and keeps it for the life of
        the process. Without this, a ``phonevault config set roots ...`` run in
        a terminal stays invisible to an open page -- and the page would go on
        starting backups from the root list it happened to load at startup,
        reporting success over a much smaller set of files than the user asked
        for. Returns True if anything was re-read.
        """
        stamp = _file_stamp(self.path)
        if stamp is None or stamp == self._stamp:
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Most likely a half-written file. Keep what we have; the stamp is
            # left alone so the next call tries again.
            return False
        self._apply(data)
        self._stamp = stamp
        return True

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CONFIG_VERSION,
            "default_profile": self.default_profile,
            "web": asdict(self.web),
            "profiles": {name: p.to_dict() for name, p in self.profiles.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        self._stamp = _file_stamp(self.path)
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)  # it may hold an SMB password
            except OSError:
                pass
        return self.path

    # -- access -------------------------------------------------------------

    def profile(self, name: str | None = None) -> Profile:
        wanted = name or self.default_profile
        if wanted not in self.profiles:
            known = ", ".join(sorted(self.profiles)) or "none"
            raise PhonevaultError(f"no profile named '{wanted}' (configured: {known})")
        return self.profiles[wanted]

    def add_profile(self, profile: Profile) -> None:
        self.profiles[profile.name] = profile

    def remove_profile(self, name: str) -> None:
        if name not in self.profiles:
            raise PhonevaultError(f"no profile named '{name}'")
        if len(self.profiles) == 1:
            raise PhonevaultError("cannot remove the only profile")
        del self.profiles[name]
        if self.default_profile == name:
            self.default_profile = next(iter(self.profiles))


#: Fields settable through ``phonevault config set``, with their parsers.
def _as_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise PhonevaultError(f"expected true or false, got {value!r}")


def _as_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _as_optional_int(value: str) -> int | None:
    lowered = value.strip().lower()
    if lowered in ("", "none", "null", "off"):
        return None
    try:
        return int(lowered)
    except ValueError as exc:
        raise PhonevaultError(f"expected a whole number, got {value!r}") from exc


SETTABLE: dict[str, Any] = {
    "repository": str,
    "source": str,
    "roots": _as_list,
    "excludes": _as_list,
    "use_default_excludes": _as_bool,
    "tags": _as_list,
    "device": str,
    "adb_path": str,
    "use_root": _as_bool,
    "batch_read": _as_bool,
    "smb_username": str,
    "smb_password": str,
    "schedule": str,
    "prune_after_forget": _as_bool,
    "retention.last": _as_optional_int,
    "retention.hourly": _as_optional_int,
    "retention.daily": _as_optional_int,
    "retention.weekly": _as_optional_int,
    "retention.monthly": _as_optional_int,
    "retention.yearly": _as_optional_int,
    "retention.keep_tags": _as_list,
}


def apply_setting(profile: Profile, key: str, raw: str) -> Any:
    """Set one dotted key on a profile, parsing ``raw`` to the right type."""
    if key not in SETTABLE:
        known = ", ".join(sorted(SETTABLE))
        raise PhonevaultError(f"unknown setting '{key}'. Try one of: {known}")
    parser = SETTABLE[key]
    value = parser(raw)
    if key == "roots" and profile.source == "adb":
        # Reject a mangled path here, not only when a backup later uses it.
        # MSYS rewrites '/sdcard' to 'C:/Program Files/Git/sdcard' before this
        # process sees it, and a config that quietly holds the wrong root is a
        # backup that reports success over an empty directory.
        from .sources.adb import reject_host_path

        for root in value:
            reject_host_path(root)
    if key.startswith("retention."):
        setattr(profile.retention, key.split(".", 1)[1], value)
    else:
        setattr(profile, key, value)
    return value


def get_setting(profile: Profile, key: str) -> Any:
    if key.startswith("retention."):
        return getattr(profile.retention, key.split(".", 1)[1], None)
    return getattr(profile, key, None)
