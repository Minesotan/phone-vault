"""Storage backend interface.

A backend is a flat key/value store addressed by ``/``-separated relative
paths. The repository layer never assumes more than what is declared here, so
adding SFTP or WebDAV later means implementing this class and nothing else.

Ranged reads matter: pack files are tens of megabytes and restoring one photo
must not download the whole pack.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from typing import IO


class Backend(abc.ABC):
    #: Human-readable location, shown in the UI and logs.
    location: str = ""

    @abc.abstractmethod
    def put(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path``, atomically, creating parents as needed."""

    @abc.abstractmethod
    def get(self, path: str) -> bytes:
        """Return the whole object at ``path``."""

    @abc.abstractmethod
    def get_range(self, path: str, offset: int, length: int) -> bytes:
        """Return ``length`` bytes starting at ``offset``."""

    @abc.abstractmethod
    def exists(self, path: str) -> bool: ...

    @abc.abstractmethod
    def size(self, path: str) -> int: ...

    @abc.abstractmethod
    def list(self, prefix: str = "") -> Iterator[str]:
        """Yield every object path beneath ``prefix``, recursively."""

    @abc.abstractmethod
    def remove(self, path: str) -> None:
        """Delete ``path``. Missing objects are not an error."""

    def put_stream(self, path: str, stream: IO[bytes]) -> int:
        """Write a stream to ``path``; returns bytes written."""
        data = stream.read()
        self.put(path, data)
        return len(data)

    def close(self) -> None:
        """Release connections. Safe to call more than once."""

    def __enter__(self) -> Backend:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __str__(self) -> str:
        return self.location or self.__class__.__name__
