"""The repository: configuration, locking, snapshots, and object access.

Layout on the NAS::

    config.json              repository id and chunking parameters
    packs/<xx>/<id>.pack     blob data
    index/<id>.idx           which blobs are in which pack, and where
    snapshots/<id>.json      one per backup run
    locks/<id>.lock          advisory locks held by running operations

Everything except ``config.json`` is write-once: backup only ever adds
objects. Deletion happens solely in :mod:`phonevault.prune`, which is the only
place that has to reason about what is still referenced.
"""

from __future__ import annotations

import getpass
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import REPO_VERSION, compress
from .backends.base import Backend
from .blobstore import DEFAULT_PACK_SIZE, BlobReader, PackWriter
from .chunker import Chunker
from .errors import LockError, RepoError
from .index import ChunkIndex
from .snapshot import (
    SNAPSHOT_PREFIX,
    FileEntry,
    Snapshot,
    parse_tree,
    snapshot_object_path,
)

CONFIG_PATH = "config.json"
LOCK_PREFIX = "locks"

#: A lock older than this is assumed to belong to a crashed run.
STALE_LOCK_SECONDS = 6 * 3600


def default_cache_root() -> Path:
    """Where to keep rebuildable local state."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "phonevault" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) / "phonevault" if xdg else Path.home() / ".cache" / "phonevault"


class RepoLock:
    """An advisory lock object in the repository.

    Advisory is the honest word for it: two processes racing on the same
    millisecond can both take an exclusive lock. That is tolerable because
    concurrent *backups* only risk storing a blob twice, which prune later
    reclaims. It exists to stop the genuinely dangerous overlap -- a prune
    deleting packs while a backup is writing snapshots that reference them.
    """

    def __init__(self, backend: Backend, exclusive: bool, note: str = "") -> None:
        self.backend = backend
        self.exclusive = exclusive
        self.note = note
        self.id = os.urandom(8).hex()
        self.path = f"{LOCK_PREFIX}/{self.id}.lock"
        self._held = False

    def _read_locks(self) -> list[dict[str, Any]]:
        locks = []
        for path in self.backend.list(LOCK_PREFIX):
            if not path.endswith(".lock") or path == self.path:
                continue
            try:
                info = json.loads(self.backend.get(path).decode("utf-8"))
            except Exception:
                continue  # unreadable lock: treat as absent rather than wedge
            info["_path"] = path
            locks.append(info)
        return locks

    def acquire(self) -> RepoLock:
        now = time.time()
        for info in self._read_locks():
            age = now - float(info.get("time", 0))
            if age > STALE_LOCK_SECONDS:
                self.backend.remove(info["_path"])
                continue
            if info.get("exclusive") or self.exclusive:
                who = info.get("host", "?")
                pid = info.get("pid", "?")
                what = info.get("note") or ("exclusive" if info.get("exclusive") else "shared")
                raise LockError(
                    f"repository is locked by {what} operation on {who} (pid {pid}), "
                    f"{int(age)}s ago; wait for it to finish, or run "
                    f"'phonevault unlock' if that process is gone"
                )
        payload = {
            "id": self.id,
            "exclusive": self.exclusive,
            "time": now,
            "host": _hostname(),
            "user": _username(),
            "pid": os.getpid(),
            "note": self.note,
        }
        self.backend.put(self.path, json.dumps(payload).encode("utf-8"))
        self._held = True
        return self

    def release(self) -> None:
        if self._held:
            self.backend.remove(self.path)
            self._held = False

    def __enter__(self) -> RepoLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


class Repository:
    """An open repository, bound to a backend and a local index cache."""

    def __init__(self, backend: Backend, config: dict[str, Any], cache_dir: Path) -> None:
        self.backend = backend
        self.config = config
        self.cache_dir = cache_dir
        self.chunker = Chunker.from_dict(config.get("chunker", {}))
        self.pack_size = int(config.get("pack_size", DEFAULT_PACK_SIZE))
        self.compression_level = int(config.get("compression_level", 3))
        self._codec_name = config.get("compression", "auto")
        self.index = ChunkIndex(cache_dir / "index.sqlite")
        self._index_synced = False

    # -- lifecycle --------------------------------------------------------

    @classmethod
    def init(
        cls,
        backend: Backend,
        *,
        chunker: Chunker | None = None,
        pack_size: int = DEFAULT_PACK_SIZE,
        compression: str = "auto",
        compression_level: int = 3,
        cache_root: Path | None = None,
    ) -> Repository:
        if backend.exists(CONFIG_PATH):
            raise RepoError(f"a repository already exists at {backend}")
        config = {
            "version": REPO_VERSION,
            "id": os.urandom(16).hex(),
            "created": time.time(),
            "created_by": f"{_username()}@{_hostname()}",
            "chunker": (chunker or Chunker()).to_dict(),
            "pack_size": pack_size,
            "compression": compression,
            "compression_level": compression_level,
        }
        backend.put(CONFIG_PATH, json.dumps(config, indent=1).encode("utf-8"))
        return cls(backend, config, cls._cache_dir_for(config["id"], cache_root))

    @classmethod
    def open(cls, backend: Backend, *, cache_root: Path | None = None) -> Repository:
        if not backend.exists(CONFIG_PATH):
            raise RepoError(
                f"no phonevault repository at {backend} -- run 'phonevault init' first"
            )
        try:
            config = json.loads(backend.get(CONFIG_PATH).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RepoError(f"repository config at {backend} is unreadable: {exc}") from exc

        version = int(config.get("version", 0))
        if version > REPO_VERSION:
            raise RepoError(
                f"repository at {backend} uses format {version}; this build understands "
                f"{REPO_VERSION}. Upgrade phonevault."
            )
        return cls(backend, config, cls._cache_dir_for(config["id"], cache_root))

    @staticmethod
    def _cache_dir_for(repo_id: str, cache_root: Path | None) -> Path:
        root = Path(cache_root) if cache_root else default_cache_root()
        path = root / repo_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def id(self) -> str:
        return self.config["id"]

    def codec(self) -> int:
        name = str(self._codec_name).lower()
        if name in ("auto", "zstd"):
            return compress.default_codec() if name == "auto" else compress.CODEC_ZSTD
        if name in ("zlib", "deflate"):
            return compress.CODEC_ZLIB
        if name in ("none", "off", "store"):
            return compress.CODEC_NONE
        raise RepoError(f"unknown compression setting {self._codec_name!r}")

    #: Settings that are safe to change on a repository that already holds
    #: data. Compression is recorded per blob, so changing it only affects
    #: what is written next. Chunking is deliberately absent: altering it
    #: would move every boundary and silently destroy deduplication against
    #: everything already stored.
    MUTABLE_SETTINGS = ("compression", "compression_level", "pack_size")

    def update_config(self, **changes: Any) -> dict[str, Any]:
        """Change repository-level settings and persist them."""
        for key in changes:
            if key not in self.MUTABLE_SETTINGS:
                if key == "chunker":
                    raise RepoError(
                        "chunking cannot be changed on an existing repository: new "
                        "chunk boundaries would not match anything already stored, so "
                        "the next backup would re-upload everything. Create a new "
                        "repository instead."
                    )
                raise RepoError(f"'{key}' is not a changeable repository setting")

        if "compression" in changes:
            name = str(changes["compression"]).lower()
            if name not in ("auto", "zstd", "zlib", "none", "off", "store"):
                raise RepoError(f"unknown compression setting {changes['compression']!r}")
            if name == "zstd" and not compress.zstd_available():
                raise RepoError(
                    "zstd is not available in this Python build; "
                    "use 'auto', or pip install zstandard"
                )
        if "compression_level" in changes:
            level = int(changes["compression_level"])
            if not 1 <= level <= 19:
                raise RepoError("compression_level must be between 1 and 19")
            changes["compression_level"] = level
        if "pack_size" in changes:
            changes["pack_size"] = int(changes["pack_size"])

        self.config.update(changes)
        self.backend.put(CONFIG_PATH, json.dumps(self.config, indent=1).encode("utf-8"))
        self._codec_name = self.config.get("compression", "auto")
        self.pack_size = int(self.config.get("pack_size", DEFAULT_PACK_SIZE))
        self.compression_level = int(self.config.get("compression_level", 3))
        return dict(changes)

    def sync_index(self, *, full: bool = False) -> int:
        loaded = self.index.sync(self.backend, full=full)
        self._index_synced = True
        return loaded

    def ensure_index(self) -> None:
        """Sync the index once per open repository, on first need.

        The index is a local cache, so any machine that has not run a backup
        against this repository -- or has had its cache cleared -- starts with
        an empty one. Without this, simply listing a snapshot's files fails
        with "blob is not in the index", which reads like corruption and is
        not.
        """
        if not self._index_synced:
            self.sync_index()

    def close(self) -> None:
        self.index.close()
        self.backend.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- locking ----------------------------------------------------------

    def lock(self, *, exclusive: bool = False, note: str = "") -> RepoLock:
        return RepoLock(self.backend, exclusive, note)

    def list_locks(self) -> list[dict[str, Any]]:
        out = []
        for path in self.backend.list(LOCK_PREFIX):
            if not path.endswith(".lock"):
                continue
            try:
                info = json.loads(self.backend.get(path).decode("utf-8"))
            except Exception:
                info = {"id": path, "unreadable": True}
            info["_path"] = path
            out.append(info)
        return out

    def break_locks(self) -> int:
        removed = 0
        for info in self.list_locks():
            self.backend.remove(info["_path"])
            removed += 1
        return removed

    # -- object access ----------------------------------------------------

    def pack_writer(self, **kwargs) -> PackWriter:
        kwargs.setdefault("pack_size", self.pack_size)
        kwargs.setdefault("codec", self.codec())
        kwargs.setdefault("compression_level", self.compression_level)
        return PackWriter(self.backend, self.index, **kwargs)

    def blob_reader(self, **kwargs) -> BlobReader:
        self.ensure_index()
        return BlobReader(self.backend, self.index, **kwargs)

    # -- snapshots --------------------------------------------------------

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self.backend.put(snapshot_object_path(snapshot.id), snapshot.to_bytes())

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        try:
            return Snapshot.from_bytes(self.backend.get(snapshot_object_path(snapshot_id)))
        except RepoError:
            raise
        except Exception as exc:
            raise RepoError(f"cannot read snapshot {snapshot_id}: {exc}") from exc

    def snapshot_ids(self) -> list[str]:
        ids = []
        for path in self.backend.list(SNAPSHOT_PREFIX):
            name = path.rsplit("/", 1)[-1]
            if name.endswith(".json"):
                ids.append(name[:-5])
        return ids

    def list_snapshots(self) -> list[Snapshot]:
        """All snapshots, oldest first. Unreadable ones are skipped loudly."""
        snapshots = []
        for snapshot_id in self.snapshot_ids():
            try:
                snapshots.append(self.load_snapshot(snapshot_id))
            except RepoError:
                continue
        snapshots.sort(key=lambda s: s.time)
        return snapshots

    def resolve_snapshot(self, ref: str) -> Snapshot:
        """Look up a snapshot by ``latest``, full id, or unambiguous prefix."""
        snapshots = self.list_snapshots()
        if not snapshots:
            raise RepoError("this repository has no snapshots yet")
        if ref in ("latest", "last", ""):
            return snapshots[-1]
        exact = [s for s in snapshots if s.id == ref]
        if exact:
            return exact[0]
        matches = [s for s in snapshots if s.id.startswith(ref)]
        if not matches:
            raise RepoError(f"no snapshot matches {ref!r}")
        if len(matches) > 1:
            ids = ", ".join(s.short_id for s in matches[:5])
            raise RepoError(f"{ref!r} is ambiguous; it matches {ids}")
        return matches[0]

    def latest_snapshot(self, *, roots: list[str] | None = None, source: str | None = None):
        """The newest snapshot worth comparing a new run against.

        The parent only has to overlap the roots being backed up, not match
        them exactly. Comparison is by path, so a root the parent never
        covered simply misses and gets read, while every root they share is
        still skipped if unchanged.

        Requiring an exact match instead -- which this used to do -- meant
        that adding one root to a profile silently re-read the entire phone,
        because no snapshot matched the new list. Adding a hundred megabytes
        of partition images should not cost a hundred and eighty gigabytes of
        re-reading.

        A candidate sharing no roots at all is rejected: it would miss on
        every path and only waste a tree fetch.
        """
        candidates = self.list_snapshots()
        if source is not None:
            candidates = [s for s in candidates if s.source == source]
        if roots is None:
            return candidates[-1] if candidates else None

        # Snapshots record normalised, forward-slash roots, so a caller's
        # native path has to go through the same normalisation or a Windows
        # root would never match the one it produced.
        from .sources.base import normalise_root

        wanted = {normalise_root(r) for r in roots}

        def score(snapshot) -> tuple[int, int, float]:
            theirs = {normalise_root(r) for r in snapshot.roots}
            # Prefer most overlap, then an exact match over a superset, then
            # the most recent. max() on this tuple picks the best candidate.
            return (len(wanted & theirs), 1 if theirs == wanted else 0, snapshot.time)

        usable = [s for s in candidates if score(s)[0] > 0]
        return max(usable, key=score) if usable else None

    def delete_snapshot(self, snapshot_id: str) -> None:
        self.backend.remove(snapshot_object_path(snapshot_id))

    # -- trees ------------------------------------------------------------

    def read_tree(self, snapshot: Snapshot, reader: BlobReader | None = None) -> Iterator[FileEntry]:
        self.ensure_index()
        reader = reader or self.blob_reader()
        yield from parse_tree(reader.get(digest) for digest in snapshot.tree)

    def tree_map(self, snapshot: Snapshot, reader: BlobReader | None = None) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.read_tree(snapshot, reader)}


def _hostname() -> str:
    import socket

    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return "unknown"


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getuser can raise on odd setups
        return "unknown"
