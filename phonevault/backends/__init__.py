"""Storage backends and the destination syntax used to pick one."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from ..errors import BackendError
from .base import Backend
from .local import LocalBackend

__all__ = ["Backend", "LocalBackend", "open_backend"]


def open_backend(target: str, **options) -> Backend:
    """Open a backend from a destination string.

    Accepted forms::

        D:/backups/phone                  local path or mapped drive
        //NAS/backup/phone                UNC path to an SMB share
        /mnt/nas/phone                    posix path
        file:///mnt/nas/phone             explicit local
        smb://user@nas/backup/phone       direct SMB, no OS mount needed
    """
    if not target:
        raise BackendError("no backup destination configured")

    lowered = target.lower()
    if lowered.startswith("smb://"):
        from .smb import SmbBackend  # lazy: needs the smbprotocol package

        return SmbBackend.from_url(target, **options)
    if lowered.startswith("file://"):
        parsed = urlparse(target)
        path = unquote(parsed.path)
        if parsed.netloc:  # file://server/share -> UNC
            return LocalBackend("//" + parsed.netloc + path)
        # file:///C:/x arrives as /C:/x on Windows
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return LocalBackend(path)
    return LocalBackend(target)
