"""Bring a repository and its local index back into agreement.

Three things can drift apart, and they fail in very different ways:

* the **local SQLite index**, which is only a cache. Losing it is harmless --
  it is rebuilt by reading the repository's ``index/`` objects.
* the **index objects** in the repository, which say what is inside each pack.
  Losing one used to strand the whole pack, because a pack is otherwise an
  opaque run of concatenated compressed blobs. Packs now carry a footer with
  the same listing, so the index object can be rebuilt from the pack itself.
* the **pack files**, which hold the data. Losing one is real loss; no amount
  of repairing invents the bytes back. The most useful thing repair can do is
  say precisely which snapshots and which files are affected, so the next
  backup is known to be the thing that fixes them.

Repair therefore never removes anything by default. It rebuilds what can be
rebuilt and reports the rest, because a tool that quietly deletes the record
of what was lost is worse than one that leaves a mess behind.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .blobstore import PACK_PREFIX, pack_object_path, read_pack_footer
from .errors import PhonevaultError
from .index import INDEX_PREFIX, encode_index, index_object_path
from .progress import Progress
from .repo import Repository


@dataclass
class RepairReport:
    """What repair found, and what it did about it."""

    packs_in_repo: int = 0
    index_objects: int = 0
    local_index_entries: int = 0

    #: Packs whose index object was missing and was rebuilt from the footer.
    reindexed: list[str] = field(default_factory=list)
    #: Packs with no index object and no footer: the data cannot be located.
    unreadable_packs: list[str] = field(default_factory=list)
    #: Index objects naming a pack that is not in the repository.
    orphan_index_objects: list[str] = field(default_factory=list)
    #: Leftovers from an interrupted write.
    temp_objects: list[str] = field(default_factory=list)
    #: Snapshot short id -> how many of its blobs cannot be found.
    damaged_snapshots: dict[str, int] = field(default_factory=dict)
    #: Snapshots whose own file list is gone. Worse than a damaged snapshot:
    #: nothing in it can even be named, let alone restored.
    unlistable_snapshots: list[str] = field(default_factory=list)
    #: Files inside those snapshots, for telling the user what to re-back-up.
    damaged_files: list[str] = field(default_factory=list)
    #: Locks held while repair ran; repair reports but never breaks them.
    locks: list[dict] = field(default_factory=list)

    removed: list[str] = field(default_factory=list)

    @property
    def repaired_anything(self) -> bool:
        return bool(self.reindexed or self.removed)

    @property
    def problems(self) -> list[str]:
        """What is still wrong after repair did what it could."""
        out = []
        for pack_id in self.unreadable_packs:
            out.append(
                f"pack {pack_id[:16]} has no index object and no footer: "
                f"its contents cannot be located"
            )
        for pack_id in self.orphan_index_objects:
            out.append(f"index object for pack {pack_id[:16]} names a pack that is missing")
        for short_id in sorted(self.unlistable_snapshots):
            out.append(
                f"snapshot {short_id}: its own file list is missing, so nothing in it "
                f"can be listed or restored"
            )
        for short_id, count in sorted(self.damaged_snapshots.items()):
            if short_id in self.unlistable_snapshots:
                continue
            out.append(f"snapshot {short_id} references {count} blob(s) that are not stored")
        return out

    @property
    def healthy(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "packs_in_repo": self.packs_in_repo,
            "index_objects": self.index_objects,
            "local_index_entries": self.local_index_entries,
            "reindexed": self.reindexed,
            "unreadable_packs": self.unreadable_packs,
            "orphan_index_objects": self.orphan_index_objects,
            "temp_objects": self.temp_objects,
            "damaged_snapshots": self.damaged_snapshots,
            "unlistable_snapshots": self.unlistable_snapshots,
            "damaged_files": self.damaged_files[:200],
            "damaged_file_count": len(self.damaged_files),
            "locks": self.locks,
            "removed": self.removed,
            "problems": self.problems,
            "healthy": self.healthy,
            "repaired": self.repaired_anything,
        }


def _pack_ids_in_repo(repo: Repository) -> set[str]:
    found = set()
    for path in repo.backend.list(PACK_PREFIX):
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".pack"):
            found.add(name[:-5])
    return found


def _index_object_ids(repo: Repository) -> set[str]:
    found = set()
    for path in repo.backend.list(INDEX_PREFIX):
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".idx"):
            found.add(name[:-4])
    return found


def _temp_objects(repo: Repository) -> list[str]:
    return [p for p in repo.backend.list("") if p.endswith(".tmp") or p.endswith(".partial")]


def repair(
    repo: Repository,
    *,
    discard_damaged: bool = False,
    progress: Progress | None = None,
) -> RepairReport:
    """Reconcile the repository with its local index.

    With ``discard_damaged`` the things that are provably useless are also
    removed: index objects pointing at absent packs, packs whose contents
    cannot be located, and temporary files from an interrupted write. None of
    that recovers data -- it only stops the same complaints being reported
    every run.
    """
    progress = progress or Progress()
    report = RepairReport()

    report.locks = repo.list_locks()
    if report.locks:
        # Not just a courtesy. Repair rebuilds the local index from scratch,
        # and that index is the same SQLite file a running backup is using to
        # decide what it has already stored -- clearing it mid-run would make
        # the backup re-upload everything it had just finished. Reconciling
        # against a repository that is still being written to is meaningless
        # anyway, so refuse rather than produce a report that is stale before
        # it is printed.
        notes = ", ".join(sorted({str(lock.get("note", "?")) for lock in report.locks}))
        raise PhonevaultError(
            f"{len(report.locks)} lock(s) held on this repository ({notes}). "
            f"Repair rebuilds the local index, which a running backup is using -- "
            f"wait for it to finish. If you are certain nothing is running, clear "
            f"the locks with 'phonevault unlock --force' first."
        )

    progress.message("Reading the repository")
    pack_ids = _pack_ids_in_repo(repo)
    index_ids = _index_object_ids(repo)
    report.packs_in_repo = len(pack_ids)
    report.index_objects = len(index_ids)

    # 1. A pack with no index object. Before pack footers this was fatal for
    #    that pack; now it is usually recoverable, which is the whole point of
    #    writing the listing twice.
    missing_index = sorted(pack_ids - index_ids)
    if missing_index:
        progress.message(f"{len(missing_index)} pack(s) have no index object; reading their footers")
    for pack_id in missing_index:
        blobs = read_pack_footer(repo.backend, pack_id)
        if blobs is None:
            report.unreadable_packs.append(pack_id)
            continue
        repo.backend.put(index_object_path(pack_id), encode_index(pack_id, blobs, repo.codec()))
        repo.index.add_pack(pack_id, blobs)
        report.reindexed.append(pack_id)

    # 2. An index object whose pack is gone. The blobs it advertises are not
    #    retrievable, so leaving it in place would make the index lie.
    report.orphan_index_objects = sorted(index_ids - pack_ids)

    # 3. Rebuild the local cache from what the repository actually holds now.
    progress.message("Rebuilding the local index from the repository")
    repo.sync_index(full=True)
    for pack_id in report.orphan_index_objects:
        repo.index.forget_pack(pack_id)
    report.local_index_entries = repo.index.count()

    report.temp_objects = _temp_objects(repo)

    # 4. Say which snapshots lost something. This is the part a user acts on.
    snapshots = repo.list_snapshots()
    progress.message(f"Checking {len(snapshots)} snapshot(s) against the index")
    reader = repo.blob_reader()
    for snapshot in snapshots:
        missing = sum(1 for digest in snapshot.tree if not repo.index.has(digest))
        try:
            entries = list(repo.read_tree(snapshot, reader))
        except Exception:
            # The tree itself is unreadable, so the snapshot's files cannot be
            # named at all. Reporting that as "1 missing blob" would badly
            # understate it.
            report.unlistable_snapshots.append(snapshot.short_id)
            report.damaged_snapshots[snapshot.short_id] = max(missing, 1)
            continue
        for entry in entries:
            absent = [d for d in entry.chunks if not repo.index.has(d)]
            if absent:
                missing += len(absent)
                report.damaged_files.append(f"{snapshot.short_id}  {entry.path}")
        if missing:
            report.damaged_snapshots[snapshot.short_id] = missing

    # 5. Only now, with everything known, remove what is provably useless.
    if discard_damaged:
        with repo.lock(exclusive=True, note="repair"):
            for pack_id in report.orphan_index_objects:
                repo.backend.remove(index_object_path(pack_id))
                report.removed.append(index_object_path(pack_id))
            for pack_id in report.unreadable_packs:
                repo.backend.remove(pack_object_path(pack_id))
                report.removed.append(pack_object_path(pack_id))
            for path in report.temp_objects:
                repo.backend.remove(path)
                report.removed.append(path)
        report.orphan_index_objects = []
        report.unreadable_packs = []
        report.temp_objects = []

    progress.finish()
    return report
