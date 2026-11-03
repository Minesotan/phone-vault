"""Reading and writing blobs, grouped into pack files.

Chunks are not stored one-file-per-chunk. A phone holds tens of thousands of
photos, which would become hundreds of thousands of small files on the NAS --
SMB pays a round trip per file, so that layout makes both backup and restore
crawl. Instead blobs are appended into pack files of a few tens of megabytes
and located by a side index, so writing is sequential and reading any single
blob is one ranged read.

Pack layout::

    magic   8 bytes   "PVPACK\\x01\\x00"
    blob    stored bytes, back to back, in index order
    footer  the same encoding as the pack's ``index/`` object
    length  4 bytes, big endian, the footer's length
    magic   8 bytes   "PVPIDX\\x01\\x00"

Offsets in the index are absolute within the file, so the header can grow
later without invalidating anything.

The footer repeats what the ``index/`` object says, which looks redundant
until one of them is lost. Without it a pack is an opaque run of concatenated
compressed blobs -- nothing in the file marks where one ends and the next
begins -- so a missing index object would strand every byte of an otherwise
intact 48 MB pack. With it, :mod:`phonevault.repair` rebuilds the index object
from the pack alone. Packs written before the footer existed still read
correctly; they simply cannot be re-indexed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable

from . import compress, hashing
from .backends.base import Backend
from .errors import IntegrityError
from .index import (
    BlobLocation,
    ChunkIndex,
    decode_index,
    encode_index,
    index_object_path,
)

PACK_MAGIC = b"PVPACK\x01\x00"
#: Marks the self-describing footer at the very end of a pack. The trailer is
#: fixed width so the footer can be found with one small read from the tail.
PACK_FOOTER_MAGIC = b"PVPIDX\x01\x00"
PACK_FOOTER_TRAILER = 4 + len(PACK_FOOTER_MAGIC)
PACK_PREFIX = "packs"
DEFAULT_PACK_SIZE = 48 * 1024 * 1024


def new_pack_id() -> str:
    return os.urandom(16).hex()


def pack_object_path(pack_id: str) -> str:
    # Fan out on the first two characters: a flat directory with thousands of
    # packs is slow to list over SMB.
    return f"{PACK_PREFIX}/{pack_id[:2]}/{pack_id}.pack"


class PackWriter:
    """Accumulates blobs and flushes them to the backend as pack files.

    Deduplication against blobs already in the repository is the caller's job
    (it holds the index); this class only guarantees it will not write the
    same digest twice within a single pack.
    """

    def __init__(
        self,
        backend: Backend,
        index: ChunkIndex,
        *,
        pack_size: int = DEFAULT_PACK_SIZE,
        codec: int | None = None,
        compression_level: int = 3,
        on_pack_written: Callable[[str, int], None] | None = None,
    ) -> None:
        self.backend = backend
        self.index = index
        self.pack_size = pack_size
        self.codec = compress.default_codec() if codec is None else codec
        self.compression_level = compression_level
        self.on_pack_written = on_pack_written

        self._buffer = bytearray(PACK_MAGIC)
        self._pending: list[BlobLocation] = []
        self._pending_digests: set[str] = set()
        self._pack_id = new_pack_id()

        self.packs_written = 0
        self.bytes_written = 0
        self.blobs_written = 0
        # Compression accounting, so a run can report what it actually saved
        # rather than leaving the user to guess.
        self.raw_bytes = 0
        self.stored_bytes = 0

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer) - len(PACK_MAGIC)

    @property
    def compression_ratio(self) -> float:
        """Stored bytes as a fraction of the bytes fed in (1.0 = no saving)."""
        return (self.stored_bytes / self.raw_bytes) if self.raw_bytes else 1.0

    def contains_pending(self, digest: str) -> bool:
        return digest in self._pending_digests

    def add(self, digest: str, data: bytes) -> bool:
        """Stage one blob. Returns False if it was already staged or stored."""
        if digest in self._pending_digests or self.index.has(digest):
            return False
        self.stage(digest, data)
        return True

    def stage(self, digest: str, data: bytes) -> None:
        """Append a blob unconditionally, skipping the deduplication check.

        Prune uses this when rewriting a pack: the blob is still listed in
        the index under the *old* pack, so :meth:`add` would refuse it.
        """
        used_codec, body = compress.compress(data, self.codec, self.compression_level)
        self.raw_bytes += len(data)
        self.stored_bytes += len(body)
        offset = len(self._buffer)
        self._buffer += body
        self._pending.append(
            BlobLocation(
                digest=digest,
                pack=self._pack_id,
                offset=offset,
                stored_length=len(body),
                raw_length=len(data),
                codec=used_codec,
            )
        )
        self._pending_digests.add(digest)
        self.blobs_written += 1

        if self.pending_bytes >= self.pack_size:
            self.flush()

    def flush(self) -> str | None:
        """Write the staged pack and its index. Returns the pack id, if any."""
        if not self._pending:
            return None

        pack_id = self._pack_id
        data = bytes(self._buffer)

        # Order matters: the pack must be durable before the index that
        # advertises it. The reverse would let a crash leave an index pointing
        # at a pack that does not exist, which looks like corruption rather
        # than like an interrupted run.
        # The same listing goes in two places: appended to the pack, so the
        # pack can describe itself, and as the index object, so the usual read
        # path never has to touch a pack to find out what is in it.
        encoded = encode_index(pack_id, self._pending, self.codec)
        data += encoded + len(encoded).to_bytes(4, "big") + PACK_FOOTER_MAGIC

        self.backend.put(pack_object_path(pack_id), data)
        self.backend.put(index_object_path(pack_id), encoded)
        self.index.add_pack(pack_id, self._pending)

        self.packs_written += 1
        self.bytes_written += len(data)
        if self.on_pack_written:
            self.on_pack_written(pack_id, len(data))

        self._buffer = bytearray(PACK_MAGIC)
        self._pending = []
        self._pending_digests = set()
        self._pack_id = new_pack_id()
        return pack_id

    def __enter__(self) -> PackWriter:
        return self

    def __exit__(self, exc_type, *_exc) -> None:
        # On a clean exit, keep whatever was staged. On an error, flushing
        # anyway is still worth it: the blobs are valid and a later run will
        # reuse them instead of re-reading the phone.
        self.flush()


def read_pack_footer(backend: Backend, pack_id: str) -> list[BlobLocation] | None:
    """Recover a pack's blob list from the pack itself, or None if it has none.

    Reads the fixed-width trailer first so only the footer crosses the network,
    rather than pulling a whole 48 MB pack to read its last few kilobytes.
    """
    path = pack_object_path(pack_id)
    try:
        total = backend.size(path)
    except Exception:
        return None
    if total < len(PACK_MAGIC) + PACK_FOOTER_TRAILER:
        return None
    try:
        trailer = backend.get_range(path, total - PACK_FOOTER_TRAILER, PACK_FOOTER_TRAILER)
    except Exception:
        return None
    if len(trailer) != PACK_FOOTER_TRAILER or trailer[4:] != PACK_FOOTER_MAGIC:
        return None  # written before footers existed, or truncated
    length = int.from_bytes(trailer[:4], "big")
    start = total - PACK_FOOTER_TRAILER - length
    if length <= 0 or start < len(PACK_MAGIC):
        return None
    try:
        stored_pack_id, blobs = decode_index(backend.get_range(path, start, length))
    except Exception:
        return None
    if stored_pack_id != pack_id:
        return None  # not the pack we asked for; refuse to index it as one
    return blobs


class BlobReader:
    """Fetches blobs by digest, with a small cache of recently used packs.

    Restores walk files in tree order, and a file's chunks usually landed in
    the same pack during backup, so even a one-entry cache turns most restores
    into one read per pack instead of one per chunk. The cache is bounded by
    bytes, not entries, so a large pack cannot blow up memory.
    """

    def __init__(self, backend: Backend, index: ChunkIndex, *, cache_bytes: int = 128 * 1024 * 1024):
        self.backend = backend
        self.index = index
        self.cache_bytes = cache_bytes
        self._cache: dict[str, bytes] = {}
        self._cache_size = 0
        self.reads = 0
        self.cache_hits = 0

    def _pack_bytes(self, pack_id: str) -> bytes:
        cached = self._cache.get(pack_id)
        if cached is not None:
            self.cache_hits += 1
            return cached
        data = self.backend.get(pack_object_path(pack_id))
        if len(data) <= self.cache_bytes:
            while self._cache_size + len(data) > self.cache_bytes and self._cache:
                _, evicted = self._cache.popitem()
                self._cache_size -= len(evicted)
            self._cache[pack_id] = data
            self._cache_size += len(data)
        return data

    def get(self, digest: str, *, verify: bool = True) -> bytes:
        location = self.index.locate(digest)
        if location is None:
            raise IntegrityError(f"blob {digest} is not in the index")
        return self.get_at(location, verify=verify)

    def get_at(self, location: BlobLocation, *, verify: bool = True) -> bytes:
        self.reads += 1
        if location.pack in self._cache:
            blob = self._slice_cached(location)
        elif location.stored_length > self.cache_bytes // 4:
            # Big enough that a ranged read beats pulling the whole pack, and
            # caching it would evict most of what is already there.
            blob = self.backend.get_range(
                pack_object_path(location.pack), location.offset, location.stored_length
            )
        else:
            blob = self._slice_cached(location)

        try:
            data = compress.decompress(blob, location.codec)
        except Exception as exc:
            # A codec blowing up on stored bytes means the pack is damaged;
            # report that rather than leaking zlib/zstd internals upwards.
            raise IntegrityError(
                f"blob {location.digest} could not be decompressed "
                f"(pack {location.pack} at offset {location.offset}): {exc}"
            ) from exc
        if len(data) != location.raw_length:
            raise IntegrityError(
                f"blob {location.digest} decompressed to {len(data)} bytes, "
                f"expected {location.raw_length}"
            )
        if verify and hashing.hash_bytes(data) != location.digest:
            raise IntegrityError(f"blob {location.digest} failed its hash check")
        return data

    def _slice_cached(self, location: BlobLocation) -> bytes:
        pack = self._pack_bytes(location.pack)
        blob = pack[location.offset : location.offset + location.stored_length]
        if len(blob) != location.stored_length:
            raise IntegrityError(
                f"pack {location.pack} is truncated: blob {location.digest} needs "
                f"{location.stored_length} bytes at offset {location.offset}, "
                f"pack is {len(pack)} bytes"
            )
        return blob

    def get_many(self, digests: Iterable[str], *, verify: bool = True) -> Iterable[bytes]:
        for digest in digests:
            yield self.get(digest, verify=verify)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_size = 0
