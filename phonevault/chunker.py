"""Splitting file content into deduplicable chunks.

Two strategies are offered because they suit different data:

``fixed``
    Cut every ``avg_size`` bytes. Very fast (no per-byte work in Python) and
    the default, because the overwhelming majority of a phone's bytes are
    photos and videos that are written once and never edited in place. Files
    that only ever grow -- logs, append-only sqlite databases -- still dedupe
    well, since their unchanged prefix keeps landing on the same boundaries.

``cdc``
    Content-defined chunking with a gear hash (FastCDC-style normalisation).
    Boundaries follow the content, so an insertion in the middle of a large
    file only rewrites the chunks around it instead of shifting every
    subsequent boundary. Costs a Python-level loop over every byte, so it is
    roughly an order of magnitude slower. Worth enabling for repositories
    dominated by large files that are edited in place.

Both yield ``(offset, data)`` pairs covering the stream exactly once.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator
from typing import IO

KIB = 1024
MIB = 1024 * 1024

DEFAULT_MIN = 256 * KIB
DEFAULT_AVG = 4 * MIB
DEFAULT_MAX = 16 * MIB

_MASK64 = (1 << 64) - 1


def _build_gear_table() -> tuple[int, ...]:
    # A fixed seed keeps chunk boundaries -- and therefore deduplication --
    # identical across machines and versions. Never change this.
    rng = random.Random(0x9E3779B97F4A7C15)
    return tuple(rng.getrandbits(64) for _ in range(256))


GEAR = _build_gear_table()


def _mask_for(bits: int) -> int:
    return (1 << bits) - 1


def _bits_for(avg_size: int) -> int:
    return max(1, avg_size.bit_length() - 1)


class Chunker:
    """Configuration for one chunking strategy."""

    def __init__(
        self,
        algorithm: str = "fixed",
        min_size: int = DEFAULT_MIN,
        avg_size: int = DEFAULT_AVG,
        max_size: int = DEFAULT_MAX,
    ) -> None:
        if algorithm not in ("fixed", "cdc"):
            raise ValueError(f"unknown chunker algorithm {algorithm!r}")
        if not 0 < min_size <= avg_size <= max_size:
            raise ValueError("chunk sizes must satisfy 0 < min <= avg <= max")
        self.algorithm = algorithm
        self.min_size = min_size
        self.avg_size = avg_size
        self.max_size = max_size

        bits = _bits_for(avg_size)
        # Normalised chunking: demand more matching bits before the average
        # size is reached and fewer after it, which tightens the size
        # distribution around avg_size.
        self._mask_small = _mask_for(min(bits + 2, 63))
        self._mask_large = _mask_for(max(bits - 2, 1))

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "min_size": self.min_size,
            "avg_size": self.avg_size,
            "max_size": self.max_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Chunker:
        return cls(
            algorithm=data.get("algorithm", "fixed"),
            min_size=int(data.get("min_size", DEFAULT_MIN)),
            avg_size=int(data.get("avg_size", DEFAULT_AVG)),
            max_size=int(data.get("max_size", DEFAULT_MAX)),
        )

    # -- boundary finding -------------------------------------------------

    def _cut_point(self, buf: bytes) -> int:
        """Return how many bytes of ``buf`` form the next chunk."""
        n = len(buf)
        if self.algorithm == "fixed":
            return min(n, self.avg_size)

        if n <= self.min_size:
            return n
        limit = min(n, self.max_size)
        normal = min(limit, self.avg_size)

        gear = GEAR
        fp = 0
        i = self.min_size
        mask = self._mask_small
        while i < normal:
            fp = ((fp << 1) + gear[buf[i]]) & _MASK64
            if not fp & mask:
                return i + 1
            i += 1
        mask = self._mask_large
        while i < limit:
            fp = ((fp << 1) + gear[buf[i]]) & _MASK64
            if not fp & mask:
                return i + 1
            i += 1
        return limit

    # -- streaming --------------------------------------------------------

    def split_stream(self, stream: IO[bytes], read_size: int = 0) -> Iterator[tuple[int, bytes]]:
        """Yield ``(offset, data)`` chunks read from a binary ``stream``."""
        read_size = read_size or max(self.max_size, 8 * MIB)
        buf = b""
        offset = 0
        eof = False
        while True:
            # Keep at least max_size buffered so a cut point is never forced
            # early by a short read.
            while not eof and len(buf) < self.max_size:
                block = stream.read(read_size)
                if not block:
                    eof = True
                    break
                buf += block
            if not buf:
                return
            if eof and len(buf) <= self.min_size:
                yield offset, buf
                return
            cut = self._cut_point(buf)
            yield offset, buf[:cut]
            offset += cut
            buf = buf[cut:]
            if eof and not buf:
                return

    def split_bytes(self, data: bytes) -> Iterator[tuple[int, bytes]]:
        import io

        yield from self.split_stream(io.BytesIO(data))

    def split_iter(self, blocks: Iterable[bytes]) -> Iterator[tuple[int, bytes]]:
        """Chunk an iterable of byte blocks without materialising all of it.

        Used for serialised file trees, which are produced record by record
        and can reach tens of megabytes on a full phone.
        """
        buf = bytearray()
        offset = 0
        for block in blocks:
            buf += block
            while len(buf) >= self.max_size:
                cut = self._cut_point(bytes(buf))
                yield offset, bytes(buf[:cut])
                offset += cut
                del buf[:cut]
        while buf:
            data = bytes(buf)
            if len(data) <= self.min_size:
                yield offset, data
                return
            cut = self._cut_point(data)
            yield offset, data[:cut]
            offset += cut
            del buf[:cut]
