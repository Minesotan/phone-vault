"""Repairing a repository that has drifted out of sync with its index.

Every test here damages a real repository in a specific way and then asks
repair to deal with it, because the only interesting question about a repair
tool is what it does when something is actually broken.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from phonevault.backends import open_backend
from phonevault.backup import BackupRunner
from phonevault.blobstore import (
    PACK_FOOTER_MAGIC,
    PACK_FOOTER_TRAILER,
    pack_object_path,
    read_pack_footer,
)
from phonevault.errors import PhonevaultError
from phonevault.index import index_object_path
from phonevault.repair import repair
from phonevault.repo import Repository
from phonevault.restore import LocalTarget, RestoreRunner
from phonevault.sources.local import LocalSource


@pytest.fixture
def repo_with_data(tmp_path: Path):
    """A small repository with one snapshot, and the paths needed to break it."""
    source = tmp_path / "phone"
    (source / "DCIM").mkdir(parents=True)
    for i in range(6):
        (source / "DCIM" / f"IMG_{i:03d}.jpg").write_bytes(os.urandom(200_000))
    (source / "notes.txt").write_bytes(b"keep me\n")

    repo_dir = tmp_path / "nas"
    repo = Repository.init(open_backend(str(repo_dir)), cache_root=tmp_path / "cache")
    BackupRunner(repo, LocalSource(label="Test")).run([str(source)])
    repo.close()

    def reopen() -> Repository:
        return Repository.open(open_backend(str(repo_dir)), cache_root=tmp_path / "cache")

    return reopen, repo_dir, source


@pytest.fixture
def multi_pack_repo(tmp_path: Path):
    """A repository whose data spans several packs, so one can be lost alone.

    The default 48 MB pack would hold this whole tree, tree blobs included, so
    deleting it destroys the file list too -- which is a different failure.
    """
    source = tmp_path / "phone"
    (source / "DCIM").mkdir(parents=True)
    for i in range(12):
        (source / "DCIM" / f"IMG_{i:03d}.jpg").write_bytes(os.urandom(150_000))

    repo_dir = tmp_path / "nas"
    repo = Repository.init(
        open_backend(str(repo_dir)), pack_size=256 * 1024, cache_root=tmp_path / "cache"
    )
    BackupRunner(repo, LocalSource(label="Test")).run([str(source)])
    repo.close()

    def reopen() -> Repository:
        return Repository.open(open_backend(str(repo_dir)), cache_root=tmp_path / "cache")

    return reopen, repo_dir


def _pack_ids(repo_dir: Path) -> list[str]:
    return sorted(p.stem for p in (repo_dir / "packs").rglob("*.pack"))


# -- the healthy case --------------------------------------------------------


def test_repair_reports_a_healthy_repository_as_healthy(repo_with_data):
    reopen, repo_dir, _ = repo_with_data
    with reopen() as repo:
        report = repair(repo)
    assert report.healthy
    assert report.problems == []
    assert not report.repaired_anything
    assert report.packs_in_repo == report.index_objects == len(_pack_ids(repo_dir))


# -- the local cache, which is only a cache ---------------------------------


def test_a_deleted_local_index_is_rebuilt_from_the_repository(repo_with_data, tmp_path):
    """Losing the SQLite cache must cost a resync, not data."""
    reopen, _repo_dir, _ = repo_with_data
    with reopen() as repo:
        before = repo.index.count()
    for db in (tmp_path / "cache").rglob("*.db*"):
        db.unlink()

    with reopen() as repo:
        report = repair(repo)
    assert report.healthy
    assert report.local_index_entries == before


def test_restore_still_works_after_the_local_index_is_wiped(repo_with_data, tmp_path):
    reopen, _repo_dir, source = repo_with_data
    for db in (tmp_path / "cache").rglob("*.db*"):
        db.unlink()
    with reopen() as repo:
        repair(repo)
        target = tmp_path / "out"
        RestoreRunner(repo).restore(repo.resolve_snapshot("latest"), LocalTarget(target))
    original = (source / "DCIM" / "IMG_000.jpg").read_bytes()
    assert any(p.read_bytes() == original for p in target.rglob("IMG_000.jpg"))


# -- a lost index object, which the footer makes recoverable -----------------


def test_a_lost_index_object_is_rebuilt_from_the_pack_footer(repo_with_data):
    """This is the case that used to strand a whole pack."""
    reopen, repo_dir, _ = repo_with_data
    victim = _pack_ids(repo_dir)[0]
    (repo_dir / index_object_path(victim)).unlink()

    with reopen() as repo:
        report = repair(repo)

    assert victim in report.reindexed
    assert report.healthy, report.problems
    assert (repo_dir / index_object_path(victim)).exists()


def test_data_is_readable_again_after_an_index_object_is_rebuilt(repo_with_data, tmp_path):
    reopen, repo_dir, source = repo_with_data
    for pack_id in _pack_ids(repo_dir):
        (repo_dir / index_object_path(pack_id)).unlink()
    for db in (tmp_path / "cache").rglob("*.db*"):
        db.unlink()

    with reopen() as repo:
        report = repair(repo)
        assert report.healthy, report.problems
        target = tmp_path / "out"
        RestoreRunner(repo).restore(repo.resolve_snapshot("latest"), LocalTarget(target))

    for original in (source / "DCIM").iterdir():
        match = [p for p in target.rglob(original.name)]
        assert match, f"{original.name} was not restored"
        assert match[0].read_bytes() == original.read_bytes()


def test_a_pack_without_a_footer_is_reported_not_silently_accepted(repo_with_data):
    """Packs written by an older version cannot be re-indexed; say so."""
    reopen, repo_dir, _ = repo_with_data
    victim = _pack_ids(repo_dir)[0]
    path = repo_dir / pack_object_path(victim)
    data = path.read_bytes()
    assert data[-len(PACK_FOOTER_MAGIC):] == PACK_FOOTER_MAGIC
    length = int.from_bytes(data[-PACK_FOOTER_TRAILER:-len(PACK_FOOTER_MAGIC)], "big")
    path.write_bytes(data[: len(data) - PACK_FOOTER_TRAILER - length])  # strip the footer
    (repo_dir / index_object_path(victim)).unlink()

    with reopen() as repo:
        report = repair(repo)
    assert victim in report.unreadable_packs
    assert not report.healthy
    assert any("cannot be located" in p for p in report.problems)


# -- a lost pack, which is real data loss ------------------------------------


def test_a_missing_pack_is_traced_back_to_its_snapshot(repo_with_data):
    reopen, repo_dir, _ = repo_with_data
    victim = _pack_ids(repo_dir)[0]
    (repo_dir / pack_object_path(victim)).unlink()

    with reopen() as repo:
        report = repair(repo)

    assert victim in report.orphan_index_objects
    assert not report.healthy
    assert report.damaged_snapshots, "a lost pack must be traced to its snapshot"


def test_losing_the_tree_is_reported_as_worse_than_losing_files(repo_with_data):
    """A snapshot whose file list is gone cannot even be enumerated.

    Calling that "1 missing blob" would wildly understate it, so it gets its
    own wording.
    """
    reopen, repo_dir, _ = repo_with_data
    (repo_dir / pack_object_path(_pack_ids(repo_dir)[0])).unlink()

    with reopen() as repo:
        report = repair(repo)

    assert report.unlistable_snapshots
    assert any("its own file list is missing" in p for p in report.problems)
    assert not any("references 1 blob" in p for p in report.problems)


def test_losing_one_data_pack_names_the_files_it_cost(multi_pack_repo):
    """With the tree intact, repair must name the files that lost data."""
    reopen, repo_dir = multi_pack_repo
    with reopen() as repo:
        tree_packs = {
            loc.pack
            for snapshot in repo.list_snapshots()
            for d in snapshot.tree
            if (loc := repo.index.locate(d))
        }
    victim = next(p for p in _pack_ids(repo_dir) if p not in tree_packs)
    (repo_dir / pack_object_path(victim)).unlink()

    with reopen() as repo:
        report = repair(repo)

    assert not report.unlistable_snapshots, "the tree should still be readable"
    assert report.damaged_files, "the user needs to know which files to re-back-up"
    assert all("  " in line for line in report.damaged_files)


def test_repair_never_deletes_by_default(repo_with_data):
    reopen, repo_dir, _ = repo_with_data
    victim = _pack_ids(repo_dir)[0]
    (repo_dir / pack_object_path(victim)).unlink()

    with reopen() as repo:
        report = repair(repo)
    assert report.removed == []
    assert (repo_dir / index_object_path(victim)).exists()


def test_discard_damaged_clears_what_cannot_be_recovered(repo_with_data):
    reopen, repo_dir, _ = repo_with_data
    victim = _pack_ids(repo_dir)[0]
    (repo_dir / pack_object_path(victim)).unlink()

    with reopen() as repo:
        report = repair(repo, discard_damaged=True)
    assert not (repo_dir / index_object_path(victim)).exists()
    assert report.removed


def test_a_later_backup_restores_what_a_lost_pack_cost(repo_with_data, tmp_path):
    """The real remedy for lost data: repair, then back up again."""
    reopen, repo_dir, source = repo_with_data
    victim = _pack_ids(repo_dir)[0]
    (repo_dir / pack_object_path(victim)).unlink()

    with reopen() as repo:
        repair(repo, discard_damaged=True)
    with reopen() as repo:
        BackupRunner(repo, LocalSource(label="Test")).run([str(source)], rescan=True)
    with reopen() as repo:
        report = repair(repo)
        assert report.damaged_snapshots.get(repo.resolve_snapshot("latest").short_id) is None
        target = tmp_path / "out"
        RestoreRunner(repo).restore(repo.resolve_snapshot("latest"), LocalTarget(target))
    for original in (source / "DCIM").iterdir():
        match = [p for p in target.rglob(original.name)]
        assert match and match[0].read_bytes() == original.read_bytes()


# -- interrupted writes ------------------------------------------------------


def test_a_pack_left_by_a_crashed_run_is_adopted_not_discarded(repo_with_data):
    """A pack written just before a crash still holds good blobs.

    flush() writes the pack before the index object, so a crash between the
    two leaves exactly this state. The footer makes those blobs usable.
    """
    reopen, repo_dir, _ = repo_with_data
    victim = _pack_ids(repo_dir)[0]
    (repo_dir / index_object_path(victim)).unlink()

    with reopen() as repo:
        blobs = read_pack_footer(repo.backend, victim)
        assert blobs, "the pack must describe itself"
        report = repair(repo)
        assert victim in report.reindexed
        for blob in blobs:
            assert repo.index.has(blob.digest)


def test_temp_files_are_reported_and_cleared(repo_with_data):
    reopen, repo_dir, _ = repo_with_data
    (repo_dir / "packs" / "halfway.tmp").write_bytes(b"junk")

    with reopen() as repo:
        report = repair(repo)
    assert any(p.endswith("halfway.tmp") for p in report.temp_objects)

    with reopen() as repo:
        report = repair(repo, discard_damaged=True)
    assert not (repo_dir / "packs" / "halfway.tmp").exists()


# -- safety ------------------------------------------------------------------


def test_repair_refuses_to_delete_while_the_repository_is_locked(repo_with_data):
    reopen, repo_dir, _ = repo_with_data
    (repo_dir / pack_object_path(_pack_ids(repo_dir)[0])).unlink()

    with (
        reopen() as repo,
        repo.lock(exclusive=True, note="prune"),
        pytest.raises(PhonevaultError, match="lock"),
    ):
        repair(repo, discard_damaged=True)


def test_repair_refuses_while_a_backup_is_running(repo_with_data):
    """Rebuilding the index under a running backup would make it re-upload everything.

    A backup takes a shared lock and relies on that same local index to know
    what it has already stored, so repair must not clear it mid-run.
    """
    reopen, _repo_dir, _ = repo_with_data
    with (
        reopen() as repo,
        repo.lock(note="backup"),
        pytest.raises(PhonevaultError, match="backup"),
    ):
        repair(repo)


def test_repair_does_not_break_the_lock_it_refused_over(repo_with_data):
    reopen, _repo_dir, _ = repo_with_data
    with reopen() as repo, repo.lock(note="backup"):
        with pytest.raises(PhonevaultError):
            repair(repo)
        assert repo.list_locks(), "repair must not break a lock it only complained about"


def test_a_footer_is_not_trusted_if_it_names_a_different_pack(multi_pack_repo):
    """Guards against a renamed or misplaced file being indexed as its neighbour."""
    reopen, repo_dir = multi_pack_repo
    ids = _pack_ids(repo_dir)
    assert len(ids) >= 2, "fixture must produce several packs"
    a, b = ids[0], ids[1]
    (repo_dir / pack_object_path(a)).write_bytes((repo_dir / pack_object_path(b)).read_bytes())
    (repo_dir / index_object_path(a)).unlink()

    with reopen() as repo:
        assert read_pack_footer(repo.backend, a) is None
