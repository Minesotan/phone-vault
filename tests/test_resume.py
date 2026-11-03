"""Surviving a device that disappears mid-backup.

These drive the engine through a source that fails on purpose, because the
behaviour worth testing is exactly what happens when the phone goes away at
the worst moment.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from phonevault.backends import open_backend
from phonevault.backup import BackupRunner
from phonevault.checkpoint import Checkpoint, checkpoint_key, list_checkpoints
from phonevault.errors import DeviceLostError, SourceError
from phonevault.repo import Repository
from phonevault.restore import LocalTarget, RestoreRunner
from phonevault.sources.local import LocalSource


class FlakySource(LocalSource):
    """A local source that drops the connection after N files.

    ``fail_after`` counts files actually opened. ``recover_after`` is how
    many disconnects happen before the device stays up.
    """

    name = "adb"  # pose as the adb source so resume keys line up

    def __init__(self, *, fail_after: int, recover_after: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_after = fail_after
        self.recover_after = recover_after
        self.disconnects = 0
        self.opened = 0        # reset on each disconnect, drives the fault injection
        self.total_opened = 0  # never reset, so tests can count real reads
        self.waits = 0

    def describe(self) -> str:
        return "Flaky Test Phone"

    def open(self, path: str, size: int = -1):
        if self.disconnects < self.recover_after and self.opened >= self.fail_after:
            self.disconnects += 1
            self.opened = 0
            raise DeviceLostError("simulated cable unplug")
        self.opened += 1
        self.total_opened += 1
        return super().open(path, size)

    def wait_until_ready(self, timeout: float = 600.0) -> bool:
        self.waits += 1
        return True


class DeadSource(FlakySource):
    """A device that never comes back."""

    def wait_until_ready(self, timeout: float = 600.0) -> bool:
        self.waits += 1
        return False


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    root = tmp_path / "phone"
    root.mkdir()
    for i in range(12):
        (root / f"IMG_{i:03d}.jpg").write_bytes(os.urandom(120_000))
    return root


def _repo(tmp_path: Path) -> Repository:
    return Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")


def test_backup_recovers_from_a_disconnect(tmp_path, photos):
    repo = _repo(tmp_path)
    source = FlakySource(fail_after=4, recover_after=1)
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0  # checkpoint at every opportunity

    snapshot = runner.run([str(photos)])

    assert source.disconnects == 1, "the test source should have dropped once"
    assert source.waits == 1, "the engine should have waited for the device"
    assert snapshot.stats["files_total"] == 12
    assert snapshot.stats["files_failed"] == 0
    entries = {e.path for e in repo.read_tree(snapshot) if e.is_file}
    assert len(entries) == 12
    repo.close()


def test_repeated_disconnects_still_complete(tmp_path, photos):
    repo = _repo(tmp_path)
    source = FlakySource(fail_after=3, recover_after=3)
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0

    snapshot = runner.run([str(photos)])

    assert source.disconnects == 3
    assert len([e for e in repo.read_tree(snapshot) if e.is_file]) == 12
    repo.close()


def test_a_dead_device_fails_but_keeps_what_it_read(tmp_path, photos):
    """The run must fail honestly, not pretend to have backed everything up."""
    repo = _repo(tmp_path)
    source = DeadSource(fail_after=5, recover_after=99)
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0

    with pytest.raises(SourceError, match="did not come back"):
        runner.run([str(photos)])

    # No snapshot: the backup genuinely did not finish.
    assert repo.list_snapshots() == []
    # But a checkpoint records what did get stored.
    assert list_checkpoints(repo.backend), "a resume checkpoint should remain"
    repo.close()


def test_a_later_run_resumes_instead_of_rereading(tmp_path, photos):
    repo = _repo(tmp_path)
    dead = DeadSource(fail_after=6, recover_after=99)
    first = BackupRunner(repo, dead)
    first.checkpoint_interval = 0
    with pytest.raises(SourceError):
        first.run([str(photos)])
    read_before_failure = dead.total_opened

    # Second run against a healthy device.
    healthy = FlakySource(fail_after=10**6)
    second = BackupRunner(repo, healthy)
    second.checkpoint_interval = 0
    snapshot = second.run([str(photos)])

    assert snapshot.stats["files_resumed"] >= 1, "checkpointed files should be reused"
    assert snapshot.stats["files_total"] == 12
    # The whole point: the recovered files were not read off the phone again.
    assert read_before_failure > 0, "the first run should have read something"
    assert healthy.total_opened < 12, "resumed files must not be re-read"
    assert healthy.total_opened + snapshot.stats["files_resumed"] >= 12
    repo.close()


def test_checkpoint_is_cleared_after_a_clean_run(tmp_path, photos):
    repo = _repo(tmp_path)
    runner = BackupRunner(repo, FlakySource(fail_after=10**6))
    runner.checkpoint_interval = 0
    runner.run([str(photos)])
    assert list_checkpoints(repo.backend) == []
    repo.close()


def test_resumed_data_restores_correctly(tmp_path, photos):
    """Recovery must not trade completeness for correctness."""
    repo = _repo(tmp_path)
    dead = DeadSource(fail_after=5, recover_after=99)
    runner = BackupRunner(repo, dead)
    runner.checkpoint_interval = 0
    with pytest.raises(SourceError):
        runner.run([str(photos)])

    healthy = FlakySource(fail_after=10**6)
    second = BackupRunner(repo, healthy)
    second.checkpoint_interval = 0
    snapshot = second.run([str(photos)])

    out = tmp_path / "restored"
    stats = RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    assert stats.failed == 0
    for original in sorted(photos.iterdir()):
        restored = next(out.rglob(original.name))
        assert restored.read_bytes() == original.read_bytes(), original.name
    repo.close()


def test_rescan_ignores_a_checkpoint(tmp_path, photos):
    repo = _repo(tmp_path)
    dead = DeadSource(fail_after=5, recover_after=99)
    runner = BackupRunner(repo, dead)
    runner.checkpoint_interval = 0
    with pytest.raises(SourceError):
        runner.run([str(photos)])

    healthy = FlakySource(fail_after=10**6)
    snapshot = BackupRunner(repo, healthy).run([str(photos)], rescan=True)
    assert snapshot.stats["files_resumed"] == 0
    assert healthy.total_opened == 12, "rescan should re-read every file"
    repo.close()


# -- the checkpoint object itself -----------------------------------------


def test_checkpoint_round_trips(tmp_path):
    from phonevault.snapshot import FileEntry

    backend = open_backend(str(tmp_path / "nas"))
    point = Checkpoint(backend, "abc123")
    entries = [
        FileEntry("/sdcard/a.jpg", size=10, mtime=1.0, chunks=["a" * 64]),
        FileEntry("/sdcard/b 中文.jpg", size=20, mtime=2.0, chunks=["b" * 64, "c" * 64]),
    ]
    point.save(entries, {"roots": ["/sdcard"]})
    meta, loaded = point.load()
    assert meta["roots"] == ["/sdcard"]
    assert list(loaded) == [e.path for e in entries]
    assert loaded["/sdcard/a.jpg"].chunks == ["a" * 64]

    point.delete()
    assert point.load() == ({}, {})


def test_corrupt_checkpoint_is_ignored_not_fatal(tmp_path):
    backend = open_backend(str(tmp_path / "nas"))
    point = Checkpoint(backend, "abc123")
    backend.put(point.path, b"\x00not a checkpoint at all")
    assert point.load() == ({}, {})


def test_checkpoint_key_depends_on_the_job(tmp_path):
    base = checkpoint_key("repo1", "adb", ["/sdcard"], [])
    assert base == checkpoint_key("repo1", "adb", ["/sdcard"], [])
    assert base != checkpoint_key("repo2", "adb", ["/sdcard"], [])
    assert base != checkpoint_key("repo1", "local", ["/sdcard"], [])
    assert base != checkpoint_key("repo1", "adb", ["/data"], [])
    assert base != checkpoint_key("repo1", "adb", ["/sdcard"], ["*.tmp"])
    # Order must not matter.
    assert checkpoint_key("r", "adb", ["/a", "/b"], []) == checkpoint_key("r", "adb", ["/b", "/a"], [])


# -- explicit pause --------------------------------------------------------


class PausingSource(FlakySource):
    """Asks the runner to pause once it has handed over ``pause_after`` files."""

    def __init__(self, *, pause_after: int, **kwargs) -> None:
        super().__init__(fail_after=10**6, **kwargs)
        self.pause_after = pause_after
        self.runner = None

    def open(self, path: str, size: int = -1):
        if self.runner is not None and self.total_opened >= self.pause_after:
            self.runner.request_pause()
        return super().open(path, size)


def test_pause_stops_cleanly_and_keeps_what_it_read(tmp_path, photos):
    from phonevault.errors import BackupPaused

    repo = _repo(tmp_path)
    source = PausingSource(pause_after=5)
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0
    source.runner = runner

    with pytest.raises(BackupPaused) as caught:
        runner.run([str(photos)])

    assert "paused" in str(caught.value).lower()
    assert caught.value.stats["files_stored"] >= 5
    assert caught.value.stats["files_remaining"] > 0
    # Paused, not finished: no snapshot, but a checkpoint to resume from.
    assert repo.list_snapshots() == []
    assert list_checkpoints(repo.backend)
    repo.close()


def test_a_paused_backup_resumes_without_rereading(tmp_path, photos):
    from phonevault.errors import BackupPaused

    repo = _repo(tmp_path)
    paused_source = PausingSource(pause_after=5)
    first = BackupRunner(repo, paused_source)
    first.checkpoint_interval = 0
    paused_source.runner = first
    with pytest.raises(BackupPaused):
        first.run([str(photos)])
    read_first = paused_source.total_opened

    second_source = FlakySource(fail_after=10**6)
    second = BackupRunner(repo, second_source)
    second.checkpoint_interval = 0
    snapshot = second.run([str(photos)])

    assert snapshot.stats["files_total"] == 12
    assert snapshot.stats["files_resumed"] >= 1
    assert second_source.total_opened < 12, "paused work must not be repeated"
    assert read_first + second_source.total_opened >= 12

    out = tmp_path / "restored"
    stats = RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    assert stats.failed == 0
    for original in sorted(photos.iterdir()):
        assert next(out.rglob(original.name)).read_bytes() == original.read_bytes()
    repo.close()


def test_pause_flag_resets_between_runs(tmp_path, photos):
    from phonevault.errors import BackupPaused

    repo = _repo(tmp_path)
    source = PausingSource(pause_after=3)
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0
    source.runner = runner
    with pytest.raises(BackupPaused):
        runner.run([str(photos)])

    assert runner.pause_requested, "the flag is set after a pause"
    source.runner = None  # stop asking for pauses
    snapshot = runner.run([str(photos)])  # a new run must clear it
    assert snapshot.stats["files_total"] == 12
    repo.close()


# -- a broken batch stream -------------------------------------------------
#
# The batched reader streams many files through one tar. If that stream
# truncates -- which happens when a live file (an app database, say) changes
# size while tar is archiving it -- the error surfaces in the *caller's*
# read, outside every try block in the reader. It escaped all the handlers
# and killed a 74 GB backup outright. It must instead drop to per-file reads
# and finish the job.


class BatchBreakingSource(FlakySource):
    """Yields a stream that raises BatchReadError partway through a batch."""

    def __init__(self, *, break_at: int, always: bool = False, **kwargs) -> None:
        super().__init__(fail_after=10**6, **kwargs)
        self.break_at = break_at
        self.always = always      # keep breaking, even after the fallback
        self.batch_calls = 0
        self.batch_enabled = True
        self.broken = False

    def disable_batch_read(self) -> None:
        self.batch_enabled = False

    def read_batch(self, files):
        if not self.batch_enabled and not self.always:
            # Per-file path, as the real source does once batching is off.
            for meta in files:
                stream = self.open(meta.path, meta.size)
                try:
                    yield meta, stream
                finally:
                    stream.close()
            return
        self.batch_calls += 1
        for index, meta in enumerate(files):
            if index == self.break_at and (self.always or not self.broken):
                self.broken = True
                yield meta, _ExplodingStream(meta.path)
                return
            stream = self.open(meta.path, meta.size)
            try:
                yield meta, stream
            finally:
                stream.close()


class _ExplodingStream:
    def __init__(self, path: str) -> None:
        self.path = path

    def read(self, size: int = -1) -> bytes:
        from phonevault.errors import BatchReadError

        raise BatchReadError(f"the batched read stream broke while reading {self.path}")

    def close(self) -> None:
        pass


def test_a_broken_batch_falls_back_and_still_completes(tmp_path, photos):
    repo = _repo(tmp_path)
    source = BatchBreakingSource(break_at=4)
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0

    snapshot = runner.run([str(photos)])

    assert source.broken, "the test should have broken a batch"
    assert not source.batch_enabled, "batching should have been switched off"
    assert snapshot.stats["files_total"] == 12
    assert snapshot.stats["files_failed"] == 0, "no file may be lost to a broken stream"
    assert len([e for e in repo.read_tree(snapshot) if e.is_file]) == 12
    repo.close()


def test_a_broken_batch_still_restores_correctly(tmp_path, photos):
    repo = _repo(tmp_path)
    runner = BackupRunner(repo, BatchBreakingSource(break_at=2))
    runner.checkpoint_interval = 0
    snapshot = runner.run([str(photos)])

    out = tmp_path / "restored"
    stats = RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    assert stats.failed == 0
    for original in sorted(photos.iterdir()):
        assert next(out.rglob(original.name)).read_bytes() == original.read_bytes()
    repo.close()


def test_a_batch_failure_with_no_fallback_is_reported(tmp_path, photos):
    """If per-file reads break too, fail honestly rather than loop forever."""
    from phonevault.errors import BatchReadError

    class AlwaysBroken(BatchBreakingSource):
        def disable_batch_read(self) -> None:
            pass  # pretend turning it off does not help

    repo = _repo(tmp_path)
    runner = BackupRunner(repo, AlwaysBroken(break_at=3, always=True))
    runner.checkpoint_interval = 0
    with pytest.raises(BatchReadError):
        runner.run([str(photos)])
    repo.close()


# -- adding a root must not cost a full re-read ------------------------------


def _sleep_past_mtime():
    import time as _t

    _t.sleep(1.1)


def test_adding_a_root_still_finds_the_previous_snapshot_as_parent(tmp_path):
    """Parent selection is by overlap, not exact equality.

    Requiring an exact root match meant adding one small root silently
    re-read the entire phone, because no snapshot matched the new list.
    """
    from phonevault.backends import open_backend
    from phonevault.repo import Repository

    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "f.txt").write_bytes(os.urandom(2048))

    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")
    runner = BackupRunner(repo, LocalSource(label="Test"))
    first = runner.run([str(a)])

    parent = repo.latest_snapshot(roots=[str(a), str(b)], source="local")
    assert parent is not None, "a snapshot covering one of the two roots must still be a parent"
    assert parent.id == first.id
    repo.close()


def test_a_snapshot_sharing_no_roots_is_not_used_as_parent(tmp_path):
    from phonevault.backends import open_backend
    from phonevault.repo import Repository

    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "f.txt").write_bytes(os.urandom(2048))

    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")
    BackupRunner(repo, LocalSource(label="Test")).run([str(a)])

    assert repo.latest_snapshot(roots=[str(b)], source="local") is None
    repo.close()


def test_an_exact_root_match_is_preferred_over_a_partial_one(tmp_path):
    from phonevault.backends import open_backend
    from phonevault.repo import Repository

    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "f.txt").write_bytes(os.urandom(2048))

    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")
    runner = BackupRunner(repo, LocalSource(label="Test"))
    exact = runner.run([str(a), str(b)])
    _sleep_past_mtime()
    runner.run([str(a)])  # newer, but covers less

    chosen = repo.latest_snapshot(roots=[str(a), str(b)], source="local")
    assert chosen.id == exact.id, "the snapshot covering both roots is the better parent"
    repo.close()


def test_a_checkpoint_from_different_roots_says_why_it_cannot_be_used(tmp_path):
    """Silently re-reading the whole phone is the failure mode to avoid."""
    from phonevault.backends import open_backend
    from phonevault.checkpoint import Checkpoint, checkpoint_key
    from phonevault.progress import Progress
    from phonevault.repo import Repository
    from phonevault.snapshot import FileEntry

    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "f.txt").write_bytes(os.urandom(2048))

    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")

    # A checkpoint left by a run over some *other* set of roots.
    stale = Checkpoint(
        repo.backend,
        checkpoint_key(repo.id, "local", ["/somewhere/else"], []),
        codec=repo.codec(),
    )
    stale.save([FileEntry(path="/somewhere/else/x", kind="file", size=1)],
               {"roots": ["/somewhere/else"]})

    warnings = []

    class Noisy(Progress):
        def warn(self, text):
            warnings.append(text)

    BackupRunner(repo, LocalSource(label="Test"), Noisy()).run([str(source_dir)])
    assert any("cannot be reused" in w for w in warnings), warnings
    repo.close()


def test_a_second_interruption_does_not_discard_the_first_run(tmp_path):
    """Two interruptions must accumulate, not reset.

    The checkpoint recorded only the files the *current* run stored, so its
    first write replaced the interrupted run's record. A backup stopped twice
    therefore re-read from scratch everything it had already read once.
    """
    from phonevault.backends import open_backend
    from phonevault.checkpoint import Checkpoint, checkpoint_key
    from phonevault.errors import BackupPaused
    from phonevault.progress import Progress
    from phonevault.repo import Repository
    from phonevault.sources.base import Matcher, normalise_root

    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    for i in range(8):
        (source_dir / f"f{i}.bin").write_bytes(os.urandom(40_000))

    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")

    # The key the runner will use -- roots are normalised before it is taken.
    key = checkpoint_key(
        repo.id, "local", [normalise_root(str(source_dir))], Matcher().patterns
    )

    # Stand in for a first run that was interrupted having stored two files.
    walked = [e for e in LocalSource(label="Test").walk([str(source_dir)], Matcher()) if e.is_file]
    earlier = [e.to_entry() for e in walked[:2]]
    Checkpoint(repo.backend, key, codec=repo.codec()).save(earlier, {"roots": [str(source_dir)]})
    already = {e.path for e in earlier}

    # Second run: resumes, then is interrupted again after its first file.
    runner = BackupRunner(repo, LocalSource(label="Test"))
    runner.checkpoint_interval = 0.0

    class PauseAfterOne(Progress):
        def __init__(self):
            self.seen = 0

        def file_done(self, path="", note="", size=0):
            self.seen += 1
            if self.seen >= 1:
                runner.request_pause()

    runner.progress = PauseAfterOne()
    with pytest.raises(BackupPaused):
        runner.run([str(source_dir)])

    _meta, saved = Checkpoint(repo.backend, key, codec=repo.codec()).load()
    assert saved, "the paused run must leave a checkpoint"
    assert already <= set(saved), (
        "the first run's files vanished from the checkpoint: "
        f"missing {sorted(already - set(saved))}"
    )
    repo.close()


class PartitionSource(LocalSource):
    """A source that presents one file the way the adb source presents a
    partition image, and truncates it on the first read(s).

    The truncation is silent -- the stream simply ends early, with no
    exception. That is what a dropped cable looks like from here: the real
    reader does notice the short read, but only when the stream is torn down,
    which happens after the engine has already recorded the entry.
    """

    name = "adb"

    def __init__(self, *, image: str, full_size: int, short_size: int,
                 truncate_times: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        self.image = image
        self.full_size = full_size
        self.short_size = short_size
        self.truncate_times = truncate_times
        self.truncations = 0
        self.reads = 0
        self.waits = 0

    def _is_image(self, path: str) -> bool:
        return path.replace("\\", "/").endswith(self.image)

    def walk(self, roots, matcher):
        for found in super().walk(roots, matcher):
            if found.is_file and self._is_image(found.path):
                found.always_read = True
                found.size = self.full_size
            yield found

    def open(self, path: str, size: int = -1):
        stream = super().open(path, size)
        if not self._is_image(path):
            return stream
        self.reads += 1
        if self.truncations < self.truncate_times:
            self.truncations += 1
            with stream:
                return io.BytesIO(stream.read(self.short_size))
        return stream

    def wait_until_ready(self, timeout: float = 600.0) -> bool:
        self.waits += 1
        return True


@pytest.fixture
def partition_dir(tmp_path: Path) -> Path:
    root = tmp_path / "firmware"
    root.mkdir()
    (root / "boot_a.img").write_bytes(os.urandom(40_000))
    (root / "super.img").write_bytes(os.urandom(900_000))
    return root


def test_a_truncated_partition_image_is_never_stored_as_complete(tmp_path, partition_dir):
    """A short read of a fixed-size image must be refused, then retried.

    This is the failure that matters most for firmware: the engine used to
    record however many bytes arrived and call the file complete, producing a
    snapshot that reported success and held an unflashable image.
    """
    repo = _repo(tmp_path)
    source = PartitionSource(
        image="super.img", full_size=900_000, short_size=150_000, truncate_times=1
    )
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0

    snapshot = runner.run([str(partition_dir)])

    assert source.truncations == 1, "the test source should have truncated once"
    assert source.waits >= 1, "a short read should be treated as a lost device"

    images = {e.path.replace("\\", "/").rsplit("/", 1)[-1]: e
              for e in repo.read_tree(snapshot) if e.is_file}
    assert images["super.img"].size == 900_000, (
        "the snapshot recorded a truncated partition image as a whole one: "
        f"{images['super.img'].size} bytes instead of 900,000"
    )
    assert snapshot.stats["files_failed"] == 0

    # And the bytes must really be there, not merely the recorded size.
    out = tmp_path / "restored"
    RestoreRunner(repo).restore(snapshot, LocalTarget(str(out)))
    restored = list(out.rglob("super.img"))
    assert restored, "super.img did not restore"
    assert restored[0].stat().st_size == 900_000
    assert restored[0].read_bytes() == (partition_dir / "super.img").read_bytes()
    repo.close()


def test_a_partition_image_that_never_reads_whole_fails_the_backup(tmp_path, partition_dir):
    """Better no snapshot than one holding an image that cannot be flashed."""
    repo = _repo(tmp_path)
    source = PartitionSource(
        image="super.img", full_size=900_000, short_size=150_000, truncate_times=99
    )
    runner = BackupRunner(repo, source)
    runner.checkpoint_interval = 0

    with pytest.raises(SourceError, match="could not get it back"):
        runner.run([str(partition_dir)])

    assert repo.list_snapshots() == [], "a failed firmware run must not leave a snapshot"
    repo.close()


def test_ordinary_files_may_still_shrink_between_stat_and_read(tmp_path):
    """The strict check must not spread to normal files.

    A log being written to can legitimately be shorter than stat said; only
    fixed-size block devices get the strict treatment.
    """
    root = tmp_path / "phone"
    root.mkdir()
    (root / "app.log").write_bytes(b"L" * 50_000)

    class ShrinkingSource(LocalSource):
        name = "adb"

        def walk(self, roots, matcher):
            for found in super().walk(roots, matcher):
                if found.is_file:
                    found.size = 50_000 * 4   # stat over-reports, as on a live phone
                yield found

    repo = _repo(tmp_path)
    snapshot = BackupRunner(repo, ShrinkingSource()).run([str(root)])

    entry = next(e for e in repo.read_tree(snapshot) if e.is_file)
    assert entry.size == 50_000, "a normal short read should record what arrived"
    assert snapshot.stats["files_failed"] == 0
    repo.close()
