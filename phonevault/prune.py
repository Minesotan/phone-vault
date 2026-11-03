"""Retention policy and space reclamation.

These are two separate operations on purpose, and the split matters:

``forget``
    Decides which snapshots to keep and deletes the rest. Cheap, reversible
    until prune runs, and it frees no space at all.

``prune``
    Walks what is left, works out which blobs nothing references any more,
    and reclaims them. Irreversible.

Prune is the only code that deletes data, so it is deliberately conservative:
it takes an exclusive lock, builds the live set from every remaining
snapshot, and writes replacement packs before removing the originals. A crash
at any point leaves the repository readable, at worst with some garbage still
in it.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from .blobstore import pack_object_path
from .errors import RepoError
from .index import index_object_path
from .progress import Progress
from .repo import Repository
from .snapshot import Snapshot


@dataclass
class RetentionPolicy:
    """Which snapshots survive a ``forget``.

    A snapshot is kept if *any* rule keeps it. ``last`` counts the most
    recent snapshots; the calendar rules each keep the newest snapshot within
    their period, counting back from the most recent snapshot.
    """

    last: int | None = None
    hourly: int | None = None
    daily: int | None = None
    weekly: int | None = None
    monthly: int | None = None
    yearly: int | None = None
    within_days: float | None = None
    keep_tags: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return all(
            value in (None, 0)
            for value in (
                self.last,
                self.hourly,
                self.daily,
                self.weekly,
                self.monthly,
                self.yearly,
                self.within_days,
            )
        ) and not self.keep_tags

    def describe(self) -> str:
        bits = []
        for name in ("last", "hourly", "daily", "weekly", "monthly", "yearly"):
            value = getattr(self, name)
            if value:
                bits.append(f"{name}={value}")
        if self.within_days:
            bits.append(f"within={self.within_days}d")
        if self.keep_tags:
            bits.append(f"tags={','.join(self.keep_tags)}")
        return ", ".join(bits) or "keep everything"

    def select(self, snapshots: Sequence[Snapshot]) -> tuple[list[Snapshot], list[Snapshot]]:
        """Split snapshots into ``(keep, remove)``."""
        if self.is_empty():
            return list(snapshots), []

        ordered = sorted(snapshots, key=lambda s: s.time, reverse=True)
        keep: dict[str, Snapshot] = {}
        reasons: dict[str, list[str]] = defaultdict(list)

        def mark(snapshot: Snapshot, reason: str) -> None:
            keep[snapshot.id] = snapshot
            reasons[snapshot.id].append(reason)

        if self.last:
            for snapshot in ordered[: self.last]:
                mark(snapshot, "last")

        if self.within_days and ordered:
            cutoff = ordered[0].time - self.within_days * 86400
            for snapshot in ordered:
                if snapshot.time >= cutoff:
                    mark(snapshot, "within")

        if self.keep_tags:
            wanted = set(self.keep_tags)
            for snapshot in ordered:
                if wanted.intersection(snapshot.tags):
                    mark(snapshot, "tag")

        buckets = (
            ("hourly", self.hourly, "%Y-%m-%d-%H"),
            ("daily", self.daily, "%Y-%m-%d"),
            ("weekly", self.weekly, "%G-W%V"),
            ("monthly", self.monthly, "%Y-%m"),
            ("yearly", self.yearly, "%Y"),
        )
        for name, count, fmt in buckets:
            if not count:
                continue
            seen: set[str] = set()
            for snapshot in ordered:  # newest first, so the first per period wins
                key = time.strftime(fmt, time.localtime(snapshot.time))
                if key in seen:
                    continue
                seen.add(key)
                mark(snapshot, name)
                if len(seen) >= count:
                    break

        kept = [s for s in ordered if s.id in keep]
        removed = [s for s in ordered if s.id not in keep]
        for snapshot in kept:
            snapshot.stats.setdefault("_keep_reasons", sorted(set(reasons[snapshot.id])))
        return kept, removed


@dataclass
class PruneStats:
    snapshots_removed: int = 0
    snapshots_kept: int = 0
    blobs_live: int = 0
    blobs_dead: int = 0
    packs_scanned: int = 0
    packs_deleted: int = 0
    packs_rewritten: int = 0
    bytes_freed: int = 0
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def forget(
    repo: Repository,
    policy: RetentionPolicy,
    *,
    dry_run: bool = False,
    progress: Progress | None = None,
) -> tuple[list[Snapshot], list[Snapshot]]:
    """Apply a retention policy. Returns ``(kept, removed)``."""
    progress = progress or Progress()
    snapshots = repo.list_snapshots()
    if not snapshots:
        raise RepoError("this repository has no snapshots yet")

    kept, removed = policy.select(snapshots)
    if not removed:
        progress.message(f"Nothing to forget: {len(kept)} snapshot(s) all match {policy.describe()}")
        return kept, removed

    for snapshot in removed:
        progress.message(
            f"{'would forget' if dry_run else 'forgetting'} "
            f"{snapshot.short_id}  {snapshot.datetime_str}  {','.join(snapshot.tags) or '-'}"
        )
        if not dry_run:
            repo.delete_snapshot(snapshot.id)
    return kept, removed


def prune(
    repo: Repository,
    *,
    dry_run: bool = False,
    repack_threshold: float = 0.25,
    progress: Progress | None = None,
) -> PruneStats:
    """Delete blobs no snapshot references any more.

    ``repack_threshold`` is the fraction of a pack that must be dead before
    it is worth rewriting. Packs below it are left alone: rewriting costs a
    full read and write of the live remainder, which is rarely worth it to
    reclaim a few percent.
    """
    progress = progress or Progress()
    started = time.time()
    stats = PruneStats()

    with repo.lock(exclusive=True, note="prune"):
        repo.sync_index(full=True)

        # -- 1. what is still referenced -----------------------------------
        progress.start("Finding live data", 0, 0)
        snapshots = repo.list_snapshots()
        stats.snapshots_kept = len(snapshots)
        live: set[str] = set()
        reader = repo.blob_reader()
        for snapshot in snapshots:
            live.update(snapshot.tree)
            try:
                for entry in repo.read_tree(snapshot, reader):
                    if entry.chunks:
                        live.update(entry.chunks)
            except Exception as exc:
                # Refusing to continue is the only safe response: an
                # unreadable tree means an unknown set of live blobs, and
                # pruning on a partial set would destroy referenced data.
                raise RepoError(
                    f"cannot read the tree of snapshot {snapshot.short_id} ({exc}). "
                    f"Prune aborted -- no data was deleted. Run 'phonevault check' first, "
                    f"and 'phonevault forget' that snapshot if it is beyond repair."
                ) from exc
        reader.clear_cache()
        stats.blobs_live = len(live)

        # -- 2. classify packs ----------------------------------------------
        by_pack: dict[str, list] = defaultdict(list)
        for location in repo.index.iter_blobs():
            by_pack[location.pack].append(location)
        stats.packs_scanned = len(by_pack)
        stats.blobs_dead = sum(
            1 for locations in by_pack.values() for loc in locations if loc.digest not in live
        )

        to_delete: list[str] = []
        to_repack: list[str] = []
        for pack_id, locations in by_pack.items():
            total = sum(loc.stored_length for loc in locations)
            dead = sum(loc.stored_length for loc in locations if loc.digest not in live)
            if dead == total:
                to_delete.append(pack_id)
            elif total and dead / total >= repack_threshold:
                to_repack.append(pack_id)

        progress.message(
            f"{stats.blobs_live} blobs live, {stats.blobs_dead} dead across "
            f"{stats.packs_scanned} packs: {len(to_delete)} to delete, "
            f"{len(to_repack)} to rewrite"
        )

        if dry_run:
            stats.packs_deleted = len(to_delete)
            stats.packs_rewritten = len(to_repack)
            stats.bytes_freed = sum(
                loc.stored_length
                for pack_id in to_delete
                for loc in by_pack[pack_id]
            )
            stats.duration = time.time() - started
            progress.finish(stats.as_dict())
            return stats

        # -- 3. rewrite partially-dead packs ---------------------------------
        progress.start("Rewriting packs", len(to_repack), 0)
        if to_repack:
            writer = repo.pack_writer()
            rewritten: list[str] = []
            for pack_id in to_repack:
                survivors = [loc for loc in by_pack[pack_id] if loc.digest in live]
                try:
                    for location in survivors:
                        # add() consults the index, which still lists this
                        # blob in the old pack, so stage it directly.
                        if not writer.contains_pending(location.digest):
                            writer.stage(location.digest, reader.get_at(location, verify=True))
                except Exception as exc:
                    stats.errors.append(f"pack {pack_id}: {exc}")
                    progress.warn(f"leaving pack {pack_id} in place: {exc}")
                    continue
                rewritten.append(pack_id)
                progress.file_done(pack_id, "rewritten")

            # Flush once, after every survivor is staged, so the new packs
            # are full-sized instead of one per rewritten pack. Every new
            # pack is durable and indexed before anything is deleted below,
            # and the index now points each moved blob at its new home -- so
            # forgetting the old pack drops only the dead entries.
            writer.flush()
            to_delete.extend(rewritten)
            stats.packs_rewritten = len(rewritten)

        # -- 4. delete ---------------------------------------------------------
        progress.start("Deleting packs", len(to_delete), 0)
        for pack_id in to_delete:
            path = pack_object_path(pack_id)
            try:
                size = repo.backend.size(path)
            except Exception:
                size = 0
            try:
                # Index first: a pack with no index is invisible, whereas an
                # index with no pack looks like corruption.
                repo.backend.remove(index_object_path(pack_id))
                repo.backend.remove(path)
            except Exception as exc:
                stats.errors.append(f"pack {pack_id}: {exc}")
                progress.warn(f"could not delete pack {pack_id}: {exc}")
                continue
            repo.index.forget_pack(pack_id)
            stats.packs_deleted += 1
            stats.bytes_freed += size
            progress.file_done(pack_id, "deleted")

        repo.sync_index(full=True)

    stats.duration = time.time() - started
    progress.finish(stats.as_dict())
    return stats


def check(
    repo: Repository,
    *,
    read_data: bool = False,
    progress: Progress | None = None,
) -> list[str]:
    """Verify the repository. Returns a list of problems, empty if healthy.

    Without ``read_data`` this only confirms that every blob a snapshot
    references is indexed and its pack exists, which is fast. With it, every
    blob is fetched and hashed, which is slow but catches silent corruption
    on the NAS.
    """
    progress = progress or Progress()
    problems: list[str] = []
    repo.sync_index(full=True)

    snapshots = repo.list_snapshots()
    progress.message(f"Checking {len(snapshots)} snapshot(s)")
    referenced: set[str] = set()
    reader = repo.blob_reader()

    for snapshot in snapshots:
        referenced.update(snapshot.tree)
        try:
            entries = list(repo.read_tree(snapshot, reader))
        except Exception as exc:
            problems.append(f"snapshot {snapshot.short_id}: tree unreadable ({exc})")
            continue
        for entry in entries:
            referenced.update(entry.chunks)

    missing = [digest for digest in referenced if not repo.index.has(digest)]
    for digest in missing[:50]:
        problems.append(f"blob {digest[:16]} is referenced but not in any pack")
    if len(missing) > 50:
        problems.append(f"...and {len(missing) - 50} more missing blobs")

    packs_needed = {
        loc.pack for loc in (repo.index.locate(d) for d in referenced if repo.index.has(d)) if loc
    }
    for pack_id in packs_needed:
        if not repo.backend.exists(pack_object_path(pack_id)):
            problems.append(f"pack {pack_id[:16]} is indexed but missing from the repository")

    if read_data:
        total = len(referenced)
        progress.start("Verifying blob contents", total, 0)
        for index, digest in enumerate(sorted(referenced)):
            if not repo.index.has(digest):
                continue
            try:
                reader.get(digest, verify=True)
            except Exception as exc:
                problems.append(f"blob {digest[:16]}: {exc}")
            progress.file_done(digest, "checked")
            if index % 500 == 0:
                reader.clear_cache()

    progress.finish()
    return problems


def unreferenced_index_objects(repo: Repository) -> list[str]:
    """Index objects whose pack has gone missing, left by an interrupted prune."""
    stale = []
    for path in repo.backend.list("index"):
        if not path.endswith(".idx"):
            continue
        pack_id = path.rsplit("/", 1)[-1][:-4]
        if not repo.backend.exists(pack_object_path(pack_id)):
            stale.append(path)
    return stale
