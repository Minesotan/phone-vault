"""Snapshots and the file trees they point at.

A snapshot is a small JSON object listing metadata plus the chunk digests of
its *tree*. The tree itself is JSON Lines -- one record per path, sorted --
and it is stored as ordinary blobs in the pack store.

Putting the tree through the same chunker as file data is what makes
versioning cheap. Two consecutive snapshots of a phone differ by a handful of
lines out of a hundred thousand, so almost every tree chunk deduplicates
against the previous run and a new snapshot costs kilobytes.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .errors import RepoError

SNAPSHOT_PREFIX = "snapshots"
SNAPSHOT_VERSION = 1

KIND_FILE = "f"
KIND_DIR = "d"
KIND_SYMLINK = "l"


@dataclass(slots=True)
class FileEntry:
    """One path in a snapshot, with the chunks that reconstruct it."""

    path: str
    kind: str = KIND_FILE
    size: int = 0
    mtime: float = 0.0
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    target: str = ""
    chunks: list[str] = field(default_factory=list)

    @property
    def is_file(self) -> bool:
        return self.kind == KIND_FILE

    @property
    def is_dir(self) -> bool:
        return self.kind == KIND_DIR

    @property
    def is_symlink(self) -> bool:
        return self.kind == KIND_SYMLINK

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def content_matches(self, other: FileEntry | None) -> bool:
        """Whether ``other`` can stand in for this entry without re-reading it.

        Size plus modification time is the standard heuristic. It is what lets
        an incremental run skip reading a hundred gigabytes of photos off the
        phone. The mtime comparison is deliberately loose by one second:
        several Android filesystems and the adb transport report whole-second
        timestamps, so demanding exactness would defeat the cache.
        """
        if other is None or not other.is_file or not self.is_file:
            return False
        if self.size != other.size:
            return False
        return abs(self.mtime - other.mtime) <= 1.0

    # -- serialisation ----------------------------------------------------

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"p": self.path, "t": self.kind}
        if self.kind == KIND_FILE:
            rec["s"] = self.size
            if self.chunks:
                rec["c"] = self.chunks
        elif self.kind == KIND_SYMLINK:
            rec["l"] = self.target
        if self.mtime:
            rec["m"] = round(self.mtime, 3)
        if self.mode:
            rec["o"] = self.mode
        if self.uid:
            rec["u"] = self.uid
        if self.gid:
            rec["g"] = self.gid
        return rec

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> FileEntry:
        return cls(
            path=rec["p"],
            kind=rec.get("t", KIND_FILE),
            size=int(rec.get("s", 0)),
            mtime=float(rec.get("m", 0.0)),
            mode=int(rec.get("o", 0)),
            uid=int(rec.get("u", 0)),
            gid=int(rec.get("g", 0)),
            target=rec.get("l", ""),
            chunks=list(rec.get("c", ())),
        )


def serialize_tree(entries: Iterable[FileEntry]) -> Iterator[bytes]:
    """Render entries as JSON Lines, one ``bytes`` block per entry."""
    for entry in entries:
        yield json.dumps(entry.to_record(), separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        ) + b"\n"


def parse_tree(blocks: Iterable[bytes]) -> Iterator[FileEntry]:
    """Parse JSON Lines back into entries, tolerating arbitrary block splits.

    Blocks arrive as chunk-sized pieces from the pack store, so a record is
    routinely split across two of them.
    """
    # Split each block once and carry only the trailing partial record.
    #
    # The obvious version -- find the next newline, then reassign
    # buf = buf[newline + 1:] -- copies the whole remaining buffer for every
    # single line. At a few megabytes per block and tens of thousands of
    # records inside it, that is quadratic, and it does not look slow until
    # the tree gets big: parsing a 286,000-entry tree took 224 seconds, while
    # fetching the same 65 MB over SMB took 0.35.
    tail = b""
    for block in blocks:
        lines = (tail + block).split(b"\n")
        tail = lines.pop()  # whatever followed the last newline, possibly empty
        for line in lines:
            if line.strip():
                yield FileEntry.from_record(json.loads(line))
    if tail.strip():
        yield FileEntry.from_record(json.loads(tail))


@dataclass(slots=True)
class Snapshot:
    """Metadata for one backup run."""

    id: str
    time: float
    roots: list[str]
    tree: list[str] = field(default_factory=list)
    source: str = "local"
    device: str = ""
    hostname: str = ""
    parent: str | None = None
    tags: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    excludes: list[str] = field(default_factory=list)
    version: int = SNAPSHOT_VERSION

    @staticmethod
    def new_id() -> str:
        return os.urandom(16).hex()

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def datetime_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "time": self.time,
            "roots": self.roots,
            "tree": self.tree,
            "source": self.source,
            "device": self.device,
            "hostname": self.hostname,
            "parent": self.parent,
            "tags": self.tags,
            "stats": self.stats,
            "excludes": self.excludes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        version = int(data.get("version", SNAPSHOT_VERSION))
        if version > SNAPSHOT_VERSION:
            raise RepoError(
                f"snapshot {data.get('id', '?')} was written by a newer version of "
                f"phonevault (format {version}, this build understands {SNAPSHOT_VERSION})"
            )
        return cls(
            id=data["id"],
            time=float(data["time"]),
            roots=list(data.get("roots", ())),
            tree=list(data.get("tree", ())),
            source=data.get("source", "local"),
            device=data.get("device", ""),
            hostname=data.get("hostname", ""),
            parent=data.get("parent"),
            tags=list(data.get("tags", ())),
            stats=dict(data.get("stats", {})),
            excludes=list(data.get("excludes", ())),
            version=version,
        )

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), indent=1, ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> Snapshot:
        return cls.from_dict(json.loads(data.decode("utf-8")))


def snapshot_object_path(snapshot_id: str) -> str:
    return f"{SNAPSHOT_PREFIX}/{snapshot_id}.json"


def local_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return "unknown"
