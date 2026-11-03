"""Reading the filesystem this process is running on.

Two uses: running inside Termux on the phone, where ``/sdcard`` is simply a
local path, and testing the engine on a PC without a device attached.
"""

from __future__ import annotations

import os
import stat as statmod
from collections.abc import Iterable, Iterator
from typing import IO

from ..errors import SourceError
from ..snapshot import KIND_DIR, KIND_FILE, KIND_SYMLINK
from .base import Matcher, Source, SourceFile, is_forbidden


class LocalSource(Source):
    name = "local"

    def __init__(self, *, follow_symlinks: bool = False, label: str = "") -> None:
        self.follow_symlinks = follow_symlinks
        self.label = label
        self.errors: list[str] = []

    def describe(self) -> str:
        if self.label:
            return self.label
        import platform

        return f"{platform.node()} ({platform.system()})"

    def _note(self, message: str) -> None:
        # Unreadable paths are the norm on a phone, not an exception. Collect
        # them for the run summary rather than aborting a 100 GB backup.
        self.errors.append(message)

    def walk(self, roots: Iterable[str], matcher: Matcher) -> Iterator[SourceFile]:
        seen_dirs: set[tuple[int, int]] = set()
        for root in roots:
            root = _normalise(root)
            if is_forbidden(root):
                self._note(f"refusing to descend into pseudo-filesystem {root}")
                continue
            if not os.path.exists(root):
                self._note(f"root does not exist: {root}")
                continue
            yield from self._walk_one(root, matcher, seen_dirs)

    def _walk_one(
        self, root: str, matcher: Matcher, seen_dirs: set[tuple[int, int]]
    ) -> Iterator[SourceFile]:
        try:
            root_stat = os.lstat(root)
        except OSError as exc:
            self._note(f"cannot stat {root}: {exc}")
            return
        if statmod.S_ISDIR(root_stat.st_mode):
            yield _to_source_file(root, root_stat)
        else:
            entry = _to_source_file(root, root_stat)
            if not matcher.excluded(root):
                yield entry
            return

        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as exc:
                self._note(f"cannot list {current}: {exc}")
                continue
            for dirent in entries:
                path = _normalise(dirent.path)
                try:
                    st = dirent.stat(follow_symlinks=False)
                except OSError as exc:
                    self._note(f"cannot stat {path}: {exc}")
                    continue
                is_dir = statmod.S_ISDIR(st.st_mode)
                if matcher.excluded(path, is_dir) or is_forbidden(path):
                    continue
                if is_dir:
                    # st_dev/st_ino guards against a directory loop made with
                    # bind mounts; symlink loops cannot arise because symlinks
                    # are recorded, never followed. scandir leaves both fields
                    # zero on Windows, so the guard only applies where the
                    # numbers are real -- otherwise every directory after the
                    # first would look like a repeat visit.
                    key = (st.st_dev, st.st_ino)
                    if key != (0, 0):
                        if key in seen_dirs:
                            continue
                        seen_dirs.add(key)
                    yield _to_source_file(path, st)
                    stack.append(path)
                else:
                    yield _to_source_file(path, st)

    def open(self, path: str, size: int = -1) -> IO[bytes]:
        try:
            return open(path, "rb")
        except OSError as exc:
            raise SourceError(f"cannot read {path}: {exc}") from exc


def _normalise(path: str) -> str:
    path = path.replace("\\", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _to_source_file(path: str, st: os.stat_result) -> SourceFile:
    if statmod.S_ISDIR(st.st_mode):
        kind = KIND_DIR
    elif statmod.S_ISLNK(st.st_mode):
        kind = KIND_SYMLINK
    else:
        kind = KIND_FILE
    target = ""
    if kind == KIND_SYMLINK:
        try:
            target = os.readlink(path).replace("\\", "/")
        except OSError:
            target = ""
    return SourceFile(
        path=path,
        kind=kind,
        size=st.st_size if kind == KIND_FILE else 0,
        mtime=st.st_mtime,
        mode=statmod.S_IMODE(st.st_mode),
        uid=getattr(st, "st_uid", 0),
        gid=getattr(st, "st_gid", 0),
        target=target,
    )
