"""Repository mechanics, retention and space reclamation."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from phonevault import hashing
from phonevault.backends import open_backend
from phonevault.blobstore import BlobReader, PackWriter, pack_object_path
from phonevault.errors import IntegrityError, LockError, RepoError
from phonevault.index import ChunkIndex
from phonevault.prune import RetentionPolicy, check, forget, prune
from phonevault.repo import Repository
from phonevault.restore import LocalTarget, RestoreRunner
from phonevault.snapshot import Snapshot

# -- repository ------------------------------------------------------------


def test_cannot_init_twice(tmp_path):
    Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "c").close()
    with pytest.raises(RepoError, match="already exists"):
        Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "c")


def test_open_missing_repository_explains_itself(tmp_path):
    with pytest.raises(RepoError, match="phonevault init"):
        Repository.open(open_backend(str(tmp_path / "empty")))


def test_future_format_is_refused(tmp_path):
    import json

    backend = open_backend(str(tmp_path / "nas"))
    Repository.init(backend, cache_root=tmp_path / "c").close()
    config = json.loads(backend.get("config.json"))
    config["version"] = 99
    backend.put("config.json", json.dumps(config).encode())
    with pytest.raises(RepoError, match="Upgrade phonevault"):
        Repository.open(backend)


def test_snapshot_resolution(repo):
    # Distinct leading characters, so a short prefix is unambiguous, plus a
    # deliberately colliding pair to exercise the ambiguity check.
    ids = ["a" * 32, "b" * 32, "cc" + "0" * 30, "cd" + "0" * 30]
    snapshots = []
    for i, snapshot_id in enumerate(ids):
        snapshot = Snapshot(id=snapshot_id, time=1000 + i, roots=["/sdcard"])
        repo.save_snapshot(snapshot)
        snapshots.append(snapshot)

    assert repo.resolve_snapshot("latest").id == ids[-1]
    assert repo.resolve_snapshot("last").id == ids[-1]
    assert repo.resolve_snapshot(ids[0]).id == ids[0]
    assert repo.resolve_snapshot("aaaa").id == ids[0]
    assert repo.resolve_snapshot("cc").id == ids[2]
    with pytest.raises(RepoError, match="no snapshot matches"):
        repo.resolve_snapshot("ffffffff")
    with pytest.raises(RepoError, match="ambiguous"):
        repo.resolve_snapshot("c")


def test_latest_snapshot_filters_by_roots_and_source(repo):
    repo.save_snapshot(Snapshot(id="a" * 32, time=1, roots=["/sdcard"], source="adb"))
    repo.save_snapshot(Snapshot(id="b" * 32, time=2, roots=["/data"], source="adb"))
    repo.save_snapshot(Snapshot(id="c" * 32, time=3, roots=["/sdcard"], source="local"))

    assert repo.latest_snapshot(roots=["/sdcard"], source="adb").id == "a" * 32
    assert repo.latest_snapshot(roots=["/data"], source="adb").id == "b" * 32
    assert repo.latest_snapshot(roots=["/nope"], source="adb") is None


def test_locks_exclude_correctly(repo):
    shared_one = repo.lock(note="backup").acquire()
    shared_two = repo.lock(note="ls").acquire()
    assert len(repo.list_locks()) == 2

    with pytest.raises(LockError, match="locked by"):
        repo.lock(exclusive=True, note="prune").acquire()

    shared_one.release()
    shared_two.release()

    exclusive = repo.lock(exclusive=True, note="prune").acquire()
    with pytest.raises(LockError):
        repo.lock(note="backup").acquire()
    exclusive.release()
    assert repo.list_locks() == []


def test_stale_locks_are_cleared(repo):
    import json

    from phonevault.repo import STALE_LOCK_SECONDS

    repo.backend.put(
        "locks/old.lock",
        json.dumps(
            {"id": "old", "exclusive": True, "time": time.time() - STALE_LOCK_SECONDS - 60,
             "host": "gone", "pid": 1, "note": "prune"}
        ).encode(),
    )
    # A lock older than the stale threshold must not block forever.
    repo.lock(note="backup").acquire().release()


def test_break_locks(repo):
    repo.lock(note="backup").acquire()
    assert repo.break_locks() == 1
    assert repo.list_locks() == []


def test_repo_settings_can_change_but_chunking_cannot(repo):
    repo.update_config(compression="zlib", compression_level=6, pack_size=1024 * 1024)
    assert repo.compression_level == 6
    assert repo.pack_size == 1024 * 1024

    reopened = Repository.open(repo.backend, cache_root=repo.cache_dir.parent)
    assert reopened.config["compression"] == "zlib"

    with pytest.raises(RepoError, match="chunking cannot be changed"):
        repo.update_config(chunker={"algorithm": "cdc"})
    with pytest.raises(RepoError, match="between 1 and 19"):
        repo.update_config(compression_level=50)
    with pytest.raises(RepoError, match="not a changeable"):
        repo.update_config(nonsense=1)


# -- pack store ------------------------------------------------------------


def test_index_rebuilds_from_the_repository(tmp_path):
    backend = open_backend(str(tmp_path / "nas"))
    index = ChunkIndex(tmp_path / "a.sqlite")
    blobs = {hashing.hash_bytes(f"blob-{i}".encode() * 900): f"blob-{i}".encode() * 900
             for i in range(30)}
    writer = PackWriter(backend, index, pack_size=8192)
    for digest, data in blobs.items():
        writer.add(digest, data)
    writer.flush()

    fresh = ChunkIndex(tmp_path / "b.sqlite")
    assert fresh.count() == 0
    fresh.sync(backend)
    assert fresh.count() == index.count()
    reader = BlobReader(backend, fresh)
    assert all(reader.get(d) == data for d, data in blobs.items())


def test_pack_writer_dedupes(tmp_path):
    backend = open_backend(str(tmp_path / "nas"))
    index = ChunkIndex(tmp_path / "i.sqlite")
    writer = PackWriter(backend, index)
    digest = hashing.hash_bytes(b"x" * 100)
    assert writer.add(digest, b"x" * 100) is True
    assert writer.add(digest, b"x" * 100) is False  # staged already
    writer.flush()
    assert writer.add(digest, b"x" * 100) is False  # indexed already


def test_corrupt_blob_is_detected(tmp_path):
    backend = open_backend(str(tmp_path / "nas"))
    index = ChunkIndex(tmp_path / "i.sqlite")
    writer = PackWriter(backend, index)
    data = os.urandom(5000)
    digest = hashing.hash_bytes(data)
    writer.add(digest, data)
    writer.flush()

    location = index.locate(digest)
    pack = Path(backend.root) / pack_object_path(location.pack).replace("/", os.sep)
    raw = bytearray(pack.read_bytes())
    raw[location.offset] ^= 0xFF
    pack.write_bytes(bytes(raw))

    with pytest.raises(IntegrityError):
        BlobReader(backend, index).get(digest)


# -- retention -------------------------------------------------------------


def _daily_snapshots(count: int) -> list[Snapshot]:
    now = time.time()
    return [Snapshot(id=f"{i:032x}", time=now - i * 86400, roots=["/sdcard"]) for i in range(count)]


@pytest.mark.parametrize(
    "policy,kept",
    [
        (RetentionPolicy(last=3), 3),
        (RetentionPolicy(daily=7), 7),
        (RetentionPolicy(within_days=5), 6),
        (RetentionPolicy(), 30),  # an empty policy keeps everything
    ],
)
def test_retention_selection(policy, kept):
    snapshots = _daily_snapshots(30)
    keep, remove = policy.select(snapshots)
    assert len(keep) == kept
    assert len(keep) + len(remove) == 30


def test_retention_keeps_newest_first():
    snapshots = _daily_snapshots(10)
    keep, _ = RetentionPolicy(last=2).select(snapshots)
    assert {s.id for s in keep} == {snapshots[0].id, snapshots[1].id}


def test_tagged_snapshots_survive():
    now = time.time()
    old_tagged = Snapshot(id="a" * 32, time=now - 900 * 86400, roots=["/x"], tags=["keep"])
    recent = Snapshot(id="b" * 32, time=now, roots=["/x"])
    keep, remove = RetentionPolicy(last=1, keep_tags=("keep",)).select([old_tagged, recent])
    assert len(keep) == 2
    assert remove == []


def test_retention_describe():
    assert "last=3" in RetentionPolicy(last=3).describe()
    assert RetentionPolicy().describe() == "keep everything"


# -- prune -----------------------------------------------------------------


def _repo_with_garbage(tmp_path) -> tuple[Repository, Path]:
    from phonevault.backup import BackupRunner
    from phonevault.sources.local import LocalSource

    src = tmp_path / "phone"
    src.mkdir()
    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")
    runner = BackupRunner(repo, LocalSource(label="p"))
    for _ in range(4):
        # Rewriting the same file each round makes the previous content
        # garbage as soon as its snapshot is forgotten.
        (src / "rolling.bin").write_bytes(os.urandom(3_000_000))
        (src / "keeper.bin").write_bytes(b"stable")
        time.sleep(1.1)
        runner.run([str(src)])
    return repo, src


def _repo_size(repo: Repository) -> int:
    return sum(f.stat().st_size for f in Path(repo.backend.root).rglob("*") if f.is_file())


def test_prune_reclaims_space_and_keeps_data_restorable(tmp_path):
    repo, _src = _repo_with_garbage(tmp_path)
    assert check(repo) == []

    forget(repo, RetentionPolicy(last=1))
    before = _repo_size(repo)
    stats = prune(repo)

    assert stats.packs_deleted > 0
    assert stats.bytes_freed > 0
    assert _repo_size(repo) < before
    assert check(repo, read_data=True) == []

    out = tmp_path / "out"
    result = RestoreRunner(repo).restore(repo.resolve_snapshot("latest"), LocalTarget(out))
    assert result.failed == 0
    assert result.files == 2
    repo.close()


def test_prune_dry_run_changes_nothing(tmp_path):
    repo, _ = _repo_with_garbage(tmp_path)
    forget(repo, RetentionPolicy(last=1))
    before = _repo_size(repo)
    stats = prune(repo, dry_run=True)
    assert stats.packs_deleted > 0  # it reports what it would do
    assert _repo_size(repo) == before  # but does not do it
    repo.close()


def test_prune_refuses_when_a_live_tree_is_unreadable(tmp_path):
    repo, _ = _repo_with_garbage(tmp_path)
    snapshot = repo.resolve_snapshot("latest")
    location = repo.index.locate(snapshot.tree[0])
    (Path(repo.backend.root) / pack_object_path(location.pack).replace("/", os.sep)).unlink()

    before = _repo_size(repo)
    with pytest.raises(RepoError, match="Prune aborted"):
        prune(repo)
    assert _repo_size(repo) == before, "nothing may be deleted when the live set is unknown"
    repo.close()


def test_forget_on_an_empty_repository(repo):
    with pytest.raises(RepoError, match="no snapshots"):
        forget(repo, RetentionPolicy(last=1))


def test_check_reports_a_missing_pack(tmp_path):
    repo, _ = _repo_with_garbage(tmp_path)
    snapshot = repo.resolve_snapshot("latest")
    entry = next(e for e in repo.read_tree(snapshot) if e.chunks)
    location = repo.index.locate(entry.chunks[0])
    (Path(repo.backend.root) / pack_object_path(location.pack).replace("/", os.sep)).unlink()

    problems = check(repo)
    assert problems, "a missing pack must be reported"
    assert any("missing" in p for p in problems)
    repo.close()
