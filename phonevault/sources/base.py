"""Where files come from.

A source enumerates paths and hands back their bytes. That is the whole
contract, and it is what lets the identical backup engine run on a PC reading
a phone over adb, or inside Termux reading the phone's own filesystem.
"""

from __future__ import annotations

import abc
import fnmatch
import stat as statmod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import IO

from ..snapshot import KIND_DIR, KIND_FILE, KIND_SYMLINK, FileEntry


@dataclass(slots=True)
class SourceFile:
    """One path as the source sees it, before any chunking."""

    path: str
    kind: str = KIND_FILE
    size: int = 0
    mtime: float = 0.0
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    target: str = ""
    #: Skip the "unchanged since last time" check for this path.
    #:
    #: The check is size plus modification time, which a block device does not
    #: have. Partition images are therefore given a fixed mtime, and that
    #: makes every one of them look unchanged forever -- the first image would
    #: be kept for good while the partition moved underneath it. Reading them
    #: every run is the only honest option; they are small, and the chunks
    #: deduplicate away when the contents really have not changed.
    always_read: bool = False

    @property
    def is_file(self) -> bool:
        return self.kind == KIND_FILE

    @property
    def is_dir(self) -> bool:
        return self.kind == KIND_DIR

    @property
    def is_symlink(self) -> bool:
        return self.kind == KIND_SYMLINK

    @classmethod
    def from_stat_mode(cls, path: str, raw_mode: int, **kwargs) -> SourceFile:
        if statmod.S_ISDIR(raw_mode):
            kind = KIND_DIR
        elif statmod.S_ISLNK(raw_mode):
            kind = KIND_SYMLINK
        else:
            kind = KIND_FILE
        return cls(path=path, kind=kind, mode=statmod.S_IMODE(raw_mode), **kwargs)

    def to_entry(self) -> FileEntry:
        return FileEntry(
            path=self.path,
            kind=self.kind,
            size=self.size if self.kind == KIND_FILE else 0,
            mtime=self.mtime,
            mode=self.mode,
            uid=self.uid,
            gid=self.gid,
            target=self.target,
        )


#: Paths that cost time and disk but are worthless in a backup. Applied unless
#: the user replaces them. Android regenerates every one of these on demand.
DEFAULT_EXCLUDES = [
    "**/cache/**",
    "**/Cache/**",
    "**/code_cache/**",
    "**/.thumbnails/**",
    "**/.trashed-*",
    "**/lost+found/**",
    "**/*.tmp",
    "**/Android/data/*/cache/**",
    "**/Android/media/*/cache/**",
    "**/.Trash-*/**",
]

#: Kernel and runtime pseudo-filesystems. Descending into these on a rooted
#: phone wastes time at best and blocks forever at worst, so they are refused
#: even when the user asks for them explicitly.
FORBIDDEN_ROOTS = ("/proc", "/sys", "/dev", "/acct", "/sys/kernel/debug")


class Matcher:
    """Glob-based include/exclude filter over absolute posix paths."""

    def __init__(self, excludes: Iterable[str] | None = None, *, use_defaults: bool = True) -> None:
        patterns = list(DEFAULT_EXCLUDES) if use_defaults else []
        patterns.extend(excludes or ())
        self.patterns = [p for p in patterns if p.strip()]

    def excluded(self, path: str, is_dir: bool = False) -> bool:
        candidates = (path, path + "/") if is_dir else (path,)
        name = path.rsplit("/", 1)[-1]
        for pattern in self.patterns:
            for candidate in candidates:
                # fnmatch treats '*' as crossing '/', which is wrong for globs
                # but conveniently makes '**/x' behave as "x anywhere". Both
                # the full path and the bare name are tested so that a plain
                # '*.tmp' works the way people expect.
                if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(name, pattern):
                    return True
        return False

    def __bool__(self) -> bool:
        return bool(self.patterns)


def is_forbidden(path: str) -> bool:
    normalised = "/" + path.strip("/")
    return any(normalised == f or normalised.startswith(f + "/") for f in FORBIDDEN_ROOTS)


class Source(abc.ABC):
    """Enumerates files and produces their content."""

    #: Recorded in the snapshot so restore knows what it is looking at.
    name: str = "local"

    @abc.abstractmethod
    def describe(self) -> str:
        """A human label for the device, e.g. ``Pixel 8 (1A2B3C)``."""

    @abc.abstractmethod
    def walk(self, roots: Iterable[str], matcher: Matcher) -> Iterator[SourceFile]:
        """Yield every path under ``roots`` that ``matcher`` does not exclude."""

    @abc.abstractmethod
    def open(self, path: str, size: int = -1) -> IO[bytes]:
        """Open a file for reading. The caller closes it."""

    def close(self) -> None:
        """Release device connections."""

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def normalise_root(path: str) -> str:
    """Put a source path into the one form the rest of the program uses.

    Trees always store forward-slash paths, so a root written with Windows
    separators has to be converted before it is compared with, or recorded
    alongside, the entries it produced -- otherwise browsing a snapshot by
    its own root finds nothing.
    """
    cleaned = path.strip().replace("\\", "/")
    while len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    return cleaned or "/"
