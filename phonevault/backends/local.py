"""Filesystem backend.

Covers the ordinary cases: a mapped drive, a UNC path straight to the NAS, a
local folder, or -- when running inside Termux -- an SMB share mounted by the
operating system.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import IO

from ..errors import BackendError
from .base import Backend


class LocalBackend(Backend):
    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        self.location = str(self.root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(f"cannot create repository directory {self.root}: {exc}") from exc

    # -- helpers ----------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise BackendError(f"unsafe backend path: {path!r}")
        return self.root.joinpath(*pure.parts)

    # -- interface --------------------------------------------------------

    def put(self, path: str, data: bytes) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target so the rename stays on one filesystem, then
        # replace: a reader never sees a half-written object, and an
        # interrupted backup leaves a stray .tmp rather than a corrupt pack.
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".pv-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            _silent_unlink(tmp)
            raise BackendError(f"write failed for {path}: {exc}") from exc

    def put_stream(self, path: str, stream: IO[bytes]) -> int:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".pv-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                shutil.copyfileobj(stream, fh)
                written = fh.tell()
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            _silent_unlink(tmp)
            raise BackendError(f"write failed for {path}: {exc}") from exc
        return written

    def get(self, path: str) -> bytes:
        try:
            return self._resolve(path).read_bytes()
        except OSError as exc:
            raise BackendError(f"read failed for {path}: {exc}") from exc

    def get_range(self, path: str, offset: int, length: int) -> bytes:
        try:
            with open(self._resolve(path), "rb") as fh:
                fh.seek(offset)
                data = fh.read(length)
        except OSError as exc:
            raise BackendError(f"ranged read failed for {path}: {exc}") from exc
        if len(data) != length:
            raise BackendError(
                f"short read for {path}: wanted {length} bytes at {offset}, got {len(data)}"
            )
        return data

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    def size(self, path: str) -> int:
        try:
            return self._resolve(path).stat().st_size
        except OSError as exc:
            raise BackendError(f"stat failed for {path}: {exc}") from exc

    def list(self, prefix: str = "") -> Iterator[str]:
        base = self._resolve(prefix) if prefix else self.root
        if not base.exists():
            return
        for dirpath, _dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(self.root)
            for name in filenames:
                if name.startswith(".pv-") and name.endswith(".tmp"):
                    continue  # an interrupted write, not a real object
                yield (rel_dir / name).as_posix()

    def remove(self, path: str) -> None:
        try:
            self._resolve(path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise BackendError(f"delete failed for {path}: {exc}") from exc


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
