"""Pluggable compression.

Blobs record which codec produced them, so a repository written by a machine
with zstd stays readable on one without it. Codec ids are stored in pack
indexes as small integers and must never be renumbered.
"""

from __future__ import annotations

import zlib

CODEC_NONE = 0
CODEC_ZLIB = 1
CODEC_ZSTD = 2

_zstd_compress = None
_zstd_decompress = None

try:  # Python 3.14+ ships zstd in the standard library (PEP 784).
    from compression import zstd as _stdzstd

    def _zstd_compress(data: bytes, level: int) -> bytes:
        return _stdzstd.compress(data, level)

    def _zstd_decompress(data: bytes) -> bytes:
        return _stdzstd.decompress(data)

except ImportError:  # pragma: no cover - depends on interpreter build
    try:
        import zstandard as _zstandard

        def _zstd_compress(data: bytes, level: int) -> bytes:
            return _zstandard.ZstdCompressor(level=level).compress(data)

        def _zstd_decompress(data: bytes) -> bytes:
            return _zstandard.ZstdDecompressor().decompress(data)

    except ImportError:
        pass


def zstd_available() -> bool:
    return _zstd_compress is not None


#: Codecs usable for writing, best first.
def default_codec() -> int:
    return CODEC_ZSTD if zstd_available() else CODEC_ZLIB


# Data that is already compressed (jpeg, mp4, heic, zip...) makes up most of a
# phone's bytes. Trying to squeeze it wastes far more CPU than it saves space,
# so a blob is only kept compressed when it actually got meaningfully smaller.
_MIN_GAIN = 0.95


def compress(data: bytes, codec: int, level: int = 3) -> tuple[int, bytes]:
    """Compress ``data``, returning the codec actually used and the payload.

    Falls back to :data:`CODEC_NONE` when compression does not pay for itself.
    """
    if codec == CODEC_NONE or not data:
        return CODEC_NONE, data

    if codec == CODEC_ZSTD:
        if _zstd_compress is None:
            codec = CODEC_ZLIB
        else:
            out = _zstd_compress(data, level)
            return (CODEC_ZSTD, out) if len(out) < len(data) * _MIN_GAIN else (CODEC_NONE, data)

    if codec == CODEC_ZLIB:
        out = zlib.compress(data, min(level + 2, 9))
        return (CODEC_ZLIB, out) if len(out) < len(data) * _MIN_GAIN else (CODEC_NONE, data)

    raise ValueError(f"unknown codec id {codec!r}")


def decompress(data: bytes, codec: int) -> bytes:
    if codec == CODEC_NONE:
        return data
    if codec == CODEC_ZLIB:
        return zlib.decompress(data)
    if codec == CODEC_ZSTD:
        if _zstd_decompress is None:
            raise RuntimeError(
                "this repository contains zstd-compressed data but no zstd "
                "implementation is available; pip install zstandard"
            )
        return _zstd_decompress(data)
    raise ValueError(f"unknown codec id {codec!r}")
