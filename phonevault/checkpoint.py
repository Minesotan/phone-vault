"""Resume state for interrupted backups.

A backup of a full phone can run for hours, and a USB cable does not have to
survive that. Without help, an interruption is expensive in a subtle way: the
stored chunks are not lost -- they are already in packs, and deduplication
would recognise them -- but with no snapshot written there is no record of
*which files were finished*, so the next run has to read every byte off the
phone again just to rediscover chunks it already has.

A checkpoint fixes that by recording the finished files, with their chunk
lists, as the run goes along. A later run reuses those entries exactly as it
would reuse a parent snapshot's.

The ordering rule that makes this safe: a file is only ever written into a
checkpoint once every chunk it references sits in a pack that has been
flushed to the NAS. The checkpoint is therefore always a truthful, if
slightly stale, view -- it can never promise data that is not there.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Sequence

from . import compress
from .backends.base import Backend
from .snapshot import FileEntry, parse_tree, serialize_tree

CHECKPOINT_PREFIX = "checkpoints"
CHECKPOINT_VERSION = 1


def checkpoint_key(repo_id: str, source: str, roots: Sequence[str], excludes: Sequence[str]) -> str:
    """A stable id for "this job against this repository".

    Roots and excludes are part of it so that resuming only ever happens
    against a run that was doing the same work. A checkpoint from a
    ``/sdcard`` backup must not be applied to a ``/data`` one.
    """
    material = "\x00".join(
        [repo_id, source, *sorted(roots), "--", *sorted(excludes)]
    ).encode("utf-8")
    return hashlib.blake2b(material, digest_size=12).hexdigest()


def checkpoint_path(key: str) -> str:
    return f"{CHECKPOINT_PREFIX}/{key}.ckpt"


class Checkpoint:
    """Reads and writes one job's resume state."""

    def __init__(self, backend: Backend, key: str, *, codec: int | None = None) -> None:
        self.backend = backend
        self.key = key
        self.path = checkpoint_path(key)
        self.codec = compress.default_codec() if codec is None else codec
        self.saves = 0

    # -- reading ----------------------------------------------------------

    def load(self) -> tuple[dict, dict[str, FileEntry]]:
        """Return ``(metadata, entries_by_path)``; empty if there is none."""
        if not self.backend.exists(self.path):
            return {}, {}
        try:
            raw = self.backend.get(self.path)
            payload = compress.decompress(raw[1:], raw[0])
            header, _, body = payload.partition(b"\n")
            meta = json.loads(header.decode("utf-8"))
            if int(meta.get("version", 0)) != CHECKPOINT_VERSION:
                return {}, {}
            entries = {entry.path: entry for entry in parse_tree([body])}
            return meta, entries
        except Exception:
            # A corrupt checkpoint is never worth failing a backup over: the
            # worst case is that the run re-reads the phone, which is exactly
            # what would happen without checkpoints at all.
            return {}, {}

    # -- writing ----------------------------------------------------------

    def save(self, entries: Iterable[FileEntry], meta: dict) -> None:
        header = {
            "version": CHECKPOINT_VERSION,
            "saved": time.time(),
            **meta,
        }
        body = b"".join(serialize_tree(entries))
        payload = json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + body
        used, blob = compress.compress(payload, self.codec)
        self.backend.put(self.path, bytes([used]) + blob)
        self.saves += 1

    def delete(self) -> None:
        self.backend.remove(self.path)


def list_checkpoints(backend: Backend) -> list[str]:
    """Keys of every checkpoint left behind in the repository."""
    keys = []
    for path in backend.list(CHECKPOINT_PREFIX):
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".ckpt"):
            keys.append(name[: -len(".ckpt")])
    return keys
