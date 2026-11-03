"""Local SQLite index of where every blob lives.

This is a *cache*, never the source of truth. Everything in it can be rebuilt
from the ``index/`` objects in the repository, which is what
:meth:`ChunkIndex.sync` does. Keeping it local means a backup run answers
"have I stored this chunk before?" from a local B-tree instead of a round trip
to the NAS, which is the difference between a usable incremental backup and an
unusable one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import compress
from .backends.base import Backend

INDEX_PREFIX = "index"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class BlobLocation:
    """Where one blob sits inside one pack file."""

    digest: str
    pack: str
    offset: int
    stored_length: int
    raw_length: int
    codec: int


def index_object_path(pack_id: str) -> str:
    return f"{INDEX_PREFIX}/{pack_id}.idx"


def encode_index(pack_id: str, blobs: Iterable[BlobLocation], codec: int) -> bytes:
    payload = {
        "pack": pack_id,
        "blobs": [[b.digest, b.offset, b.stored_length, b.raw_length, b.codec] for b in blobs],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    used, body = compress.compress(raw, codec)
    return bytes([used]) + body


def decode_index(data: bytes) -> tuple[str, list[BlobLocation]]:
    if not data:
        raise ValueError("empty index object")
    payload = json.loads(compress.decompress(data[1:], data[0]).decode("utf-8"))
    pack_id = payload["pack"]
    return pack_id, [
        BlobLocation(digest, pack_id, offset, slen, rlen, codec)
        for digest, offset, slen, rlen, codec in payload["blobs"]
    ]


class ChunkIndex:
    """SQLite-backed map of blob digest to its location in a pack."""

    def __init__(self, db_path: str | os.PathLike) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        # This file is a rebuildable cache, so trading durability for speed is
        # the right call: a lost write costs a re-sync, not data.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blobs (
                hash   TEXT PRIMARY KEY,
                pack   TEXT NOT NULL,
                offset INTEGER NOT NULL,
                slen   INTEGER NOT NULL,
                rlen   INTEGER NOT NULL,
                codec  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS blobs_by_pack ON blobs(pack);
            CREATE TABLE IF NOT EXISTS packs (
                id        TEXT PRIMARY KEY,
                loaded_at INTEGER NOT NULL
            );
            """
        )
        row = self._db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),)
            )
        elif int(row[0]) != SCHEMA_VERSION:
            self.reset()

    # -- lookups ----------------------------------------------------------

    def has(self, digest: str) -> bool:
        return self._db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone() is not None

    def locate(self, digest: str) -> BlobLocation | None:
        row = self._db.execute(
            "SELECT hash, pack, offset, slen, rlen, codec FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
        return BlobLocation(*row) if row else None

    def known_digests(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT hash FROM blobs")}

    def iter_blobs(self) -> Iterator[BlobLocation]:
        for row in self._db.execute("SELECT hash, pack, offset, slen, rlen, codec FROM blobs"):
            yield BlobLocation(*row)

    def blobs_in_pack(self, pack_id: str) -> list[BlobLocation]:
        return [
            BlobLocation(*row)
            for row in self._db.execute(
                "SELECT hash, pack, offset, slen, rlen, codec FROM blobs WHERE pack=?", (pack_id,)
            )
        ]

    def pack_ids(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT id FROM packs")}

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]

    # -- mutation ---------------------------------------------------------

    def add_pack(self, pack_id: str, blobs: Iterable[BlobLocation]) -> None:
        rows = [(b.digest, b.pack, b.offset, b.stored_length, b.raw_length, b.codec) for b in blobs]
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO blobs(hash, pack, offset, slen, rlen, codec)"
                " VALUES(?,?,?,?,?,?)",
                rows,
            )
            self._db.execute(
                "INSERT OR REPLACE INTO packs(id, loaded_at) VALUES(?,?)",
                (pack_id, int(time.time())),
            )

    def forget_pack(self, pack_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM blobs WHERE pack=?", (pack_id,))
            self._db.execute("DELETE FROM packs WHERE id=?", (pack_id,))

    def reset(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM blobs")
            self._db.execute("DELETE FROM packs")
            self._db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- synchronisation with the repository ------------------------------

    def sync(self, backend: Backend, *, full: bool = False) -> int:
        """Load index objects from the repository. Returns packs newly loaded.

        Only unseen packs are fetched, so the usual cost of opening a
        repository is one directory listing.
        """
        if full:
            self.reset()
        known = self.pack_ids()
        loaded = 0
        for path in backend.list(INDEX_PREFIX):
            name = path.rsplit("/", 1)[-1]
            if not name.endswith(".idx"):
                continue
            pack_id = name[:-4]
            if pack_id in known:
                continue
            pack_id, blobs = decode_index(backend.get(path))
            self.add_pack(pack_id, blobs)
            loaded += 1
        return loaded

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> ChunkIndex:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
