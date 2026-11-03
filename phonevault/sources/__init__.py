"""Backup sources: where the files being backed up come from."""

from .base import DEFAULT_EXCLUDES, Matcher, Source, SourceFile
from .local import LocalSource

__all__ = ["DEFAULT_EXCLUDES", "LocalSource", "Matcher", "Source", "SourceFile", "open_source"]


def open_source(kind: str, **options) -> Source:
    """Build a source by name: ``adb`` or ``local``."""
    kind = (kind or "local").lower()
    if kind == "local":
        return LocalSource(**options)
    if kind == "adb":
        from .adb import AdbSource  # lazy: only the PC side needs it

        return AdbSource(**options)
    raise ValueError(f"unknown source type {kind!r}")
