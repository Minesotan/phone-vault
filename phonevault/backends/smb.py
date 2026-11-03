"""Direct SMB backend, for when the OS has not mounted the share.

On Windows a UNC path through :class:`~phonevault.backends.local.LocalBackend`
is simpler and faster, so prefer it there. This backend exists for Termux on
the phone, where mounting an SMB share needs root and a kernel module, but
talking SMB from userspace does not.

Requires ``smbprotocol``::

    pip install smbprotocol
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import IO
from urllib.parse import unquote, urlparse

from ..errors import BackendError
from .base import Backend

try:
    import smbclient
    import smbclient.path as smbpath
    from smbprotocol.exceptions import SMBOSError
except ImportError as exc:  # pragma: no cover - optional dependency
    raise BackendError(
        "SMB destinations need the 'smbprotocol' package: pip install smbprotocol"
    ) from exc


class SmbBackend(Backend):
    def __init__(
        self,
        server: str,
        share: str,
        base_path: str = "",
        username: str | None = None,
        password: str | None = None,
        port: int = 445,
        encrypt: bool = False,
    ) -> None:
        self.server = server
        self.share = share
        self.base_path = base_path.strip("/")
        self.location = f"smb://{server}/{share}/{self.base_path}".rstrip("/")

        try:
            smbclient.register_session(
                server, username=username, password=password, port=port, encrypt=encrypt
            )
        except Exception as exc:
            raise BackendError(f"cannot connect to {server}: {exc}") from exc

        self._root = "\\\\" + server + "\\" + share
        if self.base_path:
            self._root += "\\" + self.base_path.replace("/", "\\")
        self._makedirs(self._root)

    @classmethod
    def from_url(cls, url: str, **options) -> SmbBackend:
        """Parse ``smb://[user[:pass]@]server[:port]/share/path``."""
        parsed = urlparse(url)
        if not parsed.hostname:
            raise BackendError(f"no server in SMB destination {url!r}")
        parts = [p for p in unquote(parsed.path).split("/") if p]
        if not parts:
            raise BackendError(f"no share name in SMB destination {url!r}")
        return cls(
            server=parsed.hostname,
            share=parts[0],
            base_path="/".join(parts[1:]),
            username=options.pop("username", None) or parsed.username,
            password=options.pop("password", None) or parsed.password,
            port=options.pop("port", None) or parsed.port or 445,
            encrypt=options.pop("encrypt", False),
        )

    # -- helpers ----------------------------------------------------------

    def _resolve(self, path: str) -> str:
        parts = [p for p in path.split("/") if p]
        if any(p == ".." for p in parts):
            raise BackendError(f"unsafe backend path: {path!r}")
        return "\\".join([self._root, *parts]) if parts else self._root

    def _makedirs(self, unc: str) -> None:
        try:
            smbclient.makedirs(unc, exist_ok=True)
        except Exception as exc:
            raise BackendError(f"cannot create {unc}: {exc}") from exc

    @staticmethod
    def _parent_of(unc: str) -> str:
        return unc.rsplit("\\", 1)[0]

    # -- interface --------------------------------------------------------

    def put(self, path: str, data: bytes) -> None:
        target = self._resolve(path)
        self._makedirs(self._parent_of(target))
        tmp = target + ".pv-tmp"
        try:
            with smbclient.open_file(tmp, mode="wb") as fh:
                fh.write(data)
            # SMB rename refuses to clobber, so clear the way first. The
            # window this opens is harmless in practice: pack and index names
            # are unique per write, so only a retry of the very same object
            # can collide.
            if smbpath.exists(target):
                smbclient.remove(target)
            smbclient.rename(tmp, target)
        except Exception as exc:
            self._silent_remove(tmp)
            raise BackendError(f"write failed for {path}: {exc}") from exc

    def put_stream(self, path: str, stream: IO[bytes]) -> int:
        target = self._resolve(path)
        self._makedirs(self._parent_of(target))
        tmp = target + ".pv-tmp"
        try:
            with smbclient.open_file(tmp, mode="wb") as fh:
                shutil.copyfileobj(stream, fh)
                written = fh.tell()
            if smbpath.exists(target):
                smbclient.remove(target)
            smbclient.rename(tmp, target)
        except Exception as exc:
            self._silent_remove(tmp)
            raise BackendError(f"write failed for {path}: {exc}") from exc
        return written

    def get(self, path: str) -> bytes:
        try:
            with smbclient.open_file(self._resolve(path), mode="rb") as fh:
                return fh.read()
        except Exception as exc:
            raise BackendError(f"read failed for {path}: {exc}") from exc

    def get_range(self, path: str, offset: int, length: int) -> bytes:
        try:
            with smbclient.open_file(self._resolve(path), mode="rb") as fh:
                fh.seek(offset)
                data = fh.read(length)
        except Exception as exc:
            raise BackendError(f"ranged read failed for {path}: {exc}") from exc
        if len(data) != length:
            raise BackendError(
                f"short read for {path}: wanted {length} bytes at {offset}, got {len(data)}"
            )
        return data

    def exists(self, path: str) -> bool:
        try:
            return smbpath.exists(self._resolve(path))
        except Exception:
            return False

    def size(self, path: str) -> int:
        try:
            return smbclient.stat(self._resolve(path)).st_size
        except Exception as exc:
            raise BackendError(f"stat failed for {path}: {exc}") from exc

    def list(self, prefix: str = "") -> Iterator[str]:
        base = self._resolve(prefix)
        try:
            if not smbpath.exists(base):
                return
        except Exception:
            return
        stack: list[tuple[str, list[str]]] = [(base, [p for p in prefix.split("/") if p])]
        while stack:
            unc, rel = stack.pop()
            try:
                entries = list(smbclient.scandir(unc))
            except SMBOSError as exc:
                raise BackendError(f"list failed for {unc}: {exc}") from exc
            for entry in entries:
                if entry.is_dir():
                    stack.append((entry.path, [*rel, entry.name]))
                elif not entry.name.endswith(".pv-tmp"):
                    yield "/".join([*rel, entry.name])

    def remove(self, path: str) -> None:
        try:
            smbclient.remove(self._resolve(path))
        except FileNotFoundError:
            pass
        except SMBOSError as exc:
            if getattr(exc, "errno", None) == 2:
                return
            raise BackendError(f"delete failed for {path}: {exc}") from exc
        except Exception as exc:
            raise BackendError(f"delete failed for {path}: {exc}") from exc

    @staticmethod
    def _silent_remove(unc: str) -> None:
        try:
            smbclient.remove(unc)
        except Exception:
            pass

    def close(self) -> None:
        try:
            smbclient.delete_session(self.server)
        except Exception:
            pass
