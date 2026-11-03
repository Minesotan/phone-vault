"""Backup and restore, end to end against a real repository."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from phonevault.backends import open_backend
from phonevault.errors import RestoreError
from phonevault.repo import Repository
from phonevault.restore import (
    LocalTarget,
    RestoreRunner,
    list_directory,
    matches_any,
    sanitize_component,
)
from phonevault.snapshot import KIND_DIR


def _restored(out: Path, name: str) -> Path:
    matches = list(out.rglob(name))
    assert matches, f"{name} was not restored into {out}"
    return matches[0]


# -- backup ----------------------------------------------------------------


def test_first_backup_records_everything(runner, repo, source_tree):
    snapshot = runner.run([str(source_tree)])
    stats = snapshot.stats

    assert stats["files_total"] == 6  # the .tmp and cache files are excluded
    assert stats["files_new"] == 6
    assert stats["files_unchanged"] == 0
    assert stats["bytes_read"] > 0
    assert snapshot.parent is None
    assert snapshot.device == "test-phone"

    paths = {e.path for e in repo.read_tree(snapshot)}
    assert any(p.endswith("/DCIM/Camera/a.jpg") for p in paths)
    assert not any(p.endswith("scratch.tmp") for p in paths), "default excludes must apply"
    assert not any("/cache/" in p for p in paths), "cache must be excluded"


def test_identical_files_are_stored_once(runner, repo, source_tree):
    """a.jpg and a-copy.jpg have the same bytes."""
    snapshot = runner.run([str(source_tree)])
    tree = {e.path: e for e in repo.read_tree(snapshot)}
    original = next(e for p, e in tree.items() if p.endswith("/a.jpg"))
    copy = next(e for p, e in tree.items() if p.endswith("/a-copy.jpg"))
    assert original.chunks == copy.chunks
    assert snapshot.stats["chunks_duplicate"] >= 1


def test_second_backup_reads_nothing(runner, repo, source_tree):
    runner.run([str(source_tree)])
    second = runner.run([str(source_tree)])
    assert second.stats["bytes_read"] == 0
    assert second.stats["files_unchanged"] == 6
    assert second.stats["files_new"] == 0
    assert second.parent is not None


def test_changes_are_picked_up(runner, repo, source_tree):
    first = runner.run([str(source_tree)])
    time.sleep(1.1)  # mtime resolution on some filesystems is one second
    (source_tree / "Documents" / "notes.txt").write_text("changed\n", encoding="utf-8")
    (source_tree / "DCIM" / "Camera" / "new.jpg").write_bytes(os.urandom(50_000))
    (source_tree / "DCIM" / "Camera" / "b.jpg").unlink()

    second = runner.run([str(source_tree)])
    assert second.stats["files_new"] == 1
    assert second.stats["files_changed"] == 1
    paths = {e.path for e in repo.read_tree(second)}
    assert any(p.endswith("new.jpg") for p in paths)
    assert not any(p.endswith("/b.jpg") for p in paths), "deleted files must leave the tree"
    assert first.id != second.id


def test_rescan_rereads_everything(runner, source_tree):
    runner.run([str(source_tree)])
    rescanned = runner.run([str(source_tree)], rescan=True)
    assert rescanned.stats["bytes_read"] > 0
    assert rescanned.stats["files_unchanged"] == 0


def test_dry_run_writes_nothing(runner, repo, source_tree):
    snapshot = runner.run([str(source_tree)], dry_run=True)
    assert snapshot.id == "(dry-run)"
    assert repo.list_snapshots() == []


def test_empty_file_has_no_chunks(runner, repo, source_tree):
    snapshot = runner.run([str(source_tree)])
    entry = next(e for e in repo.read_tree(snapshot) if e.path.endswith("empty.bin"))
    assert entry.size == 0
    assert entry.chunks == []


def test_custom_excludes_are_honoured(runner, repo, source_tree):
    snapshot = runner.run([str(source_tree)], excludes=["*.jpg"])
    paths = {e.path for e in repo.read_tree(snapshot)}
    assert not any(p.endswith(".jpg") for p in paths)
    assert any(p.endswith("notes.txt") for p in paths)


def test_backup_refuses_with_no_paths(runner):
    from phonevault.errors import PhonevaultError

    with pytest.raises(PhonevaultError, match="nothing to back up"):
        runner.run([])


def test_tags_are_recorded(runner, source_tree):
    snapshot = runner.run([str(source_tree)], tags=["nightly", "phone"])
    assert snapshot.tags == ["nightly", "phone"]


def test_snapshot_tree_dedupes_between_runs(runner, repo, source_tree):
    """Consecutive snapshots of the same data must cost almost nothing.

    The assertion is about stored bytes rather than an exact chunk count:
    NTFS updates a directory's own timestamp lazily, so a scan taken moments
    after the files were created can see a different directory mtime than
    the next one. That rewrites a single small tree chunk, which is
    immaterial -- what must not happen is any file data being stored again.
    """
    first = runner.run([str(source_tree)])
    second = runner.run([str(source_tree)])

    assert second.stats["bytes_read"] == 0, "no file may be read a second time"
    assert second.stats["chunks_new"] <= second.stats["tree_chunks"]
    assert second.stats["bytes_stored"] < 8 * 1024, "a repeat snapshot should cost kilobytes"
    # The file entries themselves must point at exactly the same chunks.
    files_first = {e.path: e.chunks for e in repo.read_tree(first) if e.is_file}
    files_second = {e.path: e.chunks for e in repo.read_tree(second) if e.is_file}
    assert files_first == files_second


# -- restore ---------------------------------------------------------------


def test_restore_reproduces_bytes_exactly(runner, repo, source_tree, tmp_path):
    snapshot = runner.run([str(source_tree)])
    out = tmp_path / "out"
    stats = RestoreRunner(repo).restore(snapshot, LocalTarget(out))

    assert stats.failed == 0
    assert stats.files == 6
    for name in ("a.jpg", "b.jpg", "notes.txt", "empty.bin"):
        original = next(source_tree.rglob(name))
        assert _restored(out, name).read_bytes() == original.read_bytes(), name


def test_restore_handles_unicode_names(runner, repo, source_tree, tmp_path):
    snapshot = runner.run([str(source_tree)])
    out = tmp_path / "out"
    RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    restored = _restored(out, "unicode 中文.txt")
    assert restored.read_text(encoding="utf-8") == "中文\n"


def test_selective_restore(runner, repo, source_tree, tmp_path):
    snapshot = runner.run([str(source_tree)])
    out = tmp_path / "out"
    posix_root = str(source_tree).replace("\\", "/")
    stats = RestoreRunner(repo).restore(
        snapshot, LocalTarget(out), include=[f"{posix_root}/Documents"]
    )
    assert stats.files == 3  # notes.txt, empty.bin, unicode
    assert not list(out.rglob("a.jpg"))


def test_restore_skips_identical_files_on_a_second_pass(runner, repo, source_tree, tmp_path):
    snapshot = runner.run([str(source_tree)])
    out = tmp_path / "out"
    RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    again = RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    assert again.files == 0
    assert again.skipped == 6


def test_overwrite_always_rewrites(runner, repo, source_tree, tmp_path):
    snapshot = runner.run([str(source_tree)])
    out = tmp_path / "out"
    RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    again = RestoreRunner(repo).restore(snapshot, LocalTarget(out), overwrite="always")
    assert again.files == 6
    assert again.skipped == 0


def test_restore_rejects_an_unknown_overwrite_policy(runner, repo, source_tree, tmp_path):
    snapshot = runner.run([str(source_tree)])
    with pytest.raises(RestoreError, match="overwrite policy"):
        RestoreRunner(repo).restore(snapshot, LocalTarget(tmp_path / "o"), overwrite="maybe")


def test_restore_of_nothing_is_an_error(runner, repo, source_tree, tmp_path):
    snapshot = runner.run([str(source_tree)])
    with pytest.raises(RestoreError, match="contains nothing"):
        RestoreRunner(repo).restore(
            snapshot, LocalTarget(tmp_path / "o"), include=["/nowhere/at/all"]
        )


def test_read_file_streams_one_file(runner, repo, source_tree):
    snapshot = runner.run([str(source_tree)])
    target = str(source_tree / "Documents" / "notes.txt").replace("\\", "/")
    content = b"".join(RestoreRunner(repo).read_file(snapshot, target))
    assert content == b"hello\n"

    with pytest.raises(RestoreError, match="not in snapshot"):
        list(RestoreRunner(repo).read_file(snapshot, "/no/such/file"))


def test_restore_detects_corruption(runner, repo, source_tree, tmp_path):
    """A damaged pack must fail the file, not produce silent garbage."""
    from phonevault.blobstore import pack_object_path

    snapshot = runner.run([str(source_tree)])
    entry = next(e for e in repo.read_tree(snapshot) if e.path.endswith("b.jpg"))
    location = repo.index.locate(entry.chunks[0])
    pack = Path(repo.backend.root) / pack_object_path(location.pack).replace("/", os.sep)
    raw = bytearray(pack.read_bytes())
    raw[location.offset] ^= 0xFF
    pack.write_bytes(bytes(raw))

    stats = RestoreRunner(repo).restore(snapshot, LocalTarget(tmp_path / "out"))
    assert stats.failed == 1
    assert any("b.jpg" in e for e in stats.errors)


# -- browsing helpers ------------------------------------------------------


def test_list_directory_shows_immediate_children(runner, repo, source_tree):
    snapshot = runner.run([str(source_tree)])
    entries = list(repo.read_tree(snapshot))
    root = str(source_tree).replace("\\", "/")
    names = [e.name for e in list_directory(entries, root)]
    assert names == ["DCIM", "Documents"]  # directories first, alphabetical
    assert all(e.kind == KIND_DIR for e in list_directory(entries, root))

    docs = list_directory(entries, f"{root}/Documents")
    assert {e.name for e in docs} == {"notes.txt", "empty.bin", "unicode 中文.txt"}


@pytest.mark.parametrize(
    "path,patterns,expected",
    [
        ("/a/b/c.txt", [], True),
        ("/a/b/c.txt", ["/a/b"], True),
        ("/a/b/c.txt", ["/a/b/c.txt"], True),
        ("/a/b/c.txt", ["/a/b/"], True),
        ("/a/bc.txt", ["/a/b"], False),
        ("/a/x.jpg", ["*.jpg"], True),
        ("/a/x.jpg", ["/z"], False),
    ],
)
def test_matches_any(path, patterns, expected):
    assert matches_any(path, patterns) is expected


@pytest.mark.skipif(os.name != "nt", reason="Windows filename rules")
@pytest.mark.parametrize(
    "name,expected",
    [
        ("plain.txt", "plain.txt"),
        ("has:colon.txt", "has_colon.txt"),
        ("q?mark*star", "q_mark_star"),
        ("CON.txt", "_CON.txt"),
        ("trailing. ", "trailing_"),
    ],
)
def test_sanitize_component(name, expected):
    assert sanitize_component(name)[0] == expected


def test_restore_refuses_to_escape_the_target(tmp_path):
    from phonevault.snapshot import FileEntry

    target = LocalTarget(tmp_path / "out")
    with pytest.raises(RestoreError, match="outside the restore folder"):
        target.map_path("/../../../etc/passwd")
    # A normal path still works.
    assert target.map_path("/sdcard/a.jpg").name == "a.jpg"
    assert isinstance(FileEntry("/x"), FileEntry)


# -- multiple roots --------------------------------------------------------


def test_multiple_roots(tmp_path):
    from phonevault.backup import BackupRunner
    from phonevault.sources.local import LocalSource

    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a.txt").write_text("a")
    (two / "b.txt").write_text("b")

    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")
    snapshot = BackupRunner(repo, LocalSource()).run([str(one), str(two)])
    paths = {e.path for e in repo.read_tree(snapshot)}
    assert any(p.endswith("/a.txt") for p in paths)
    assert any(p.endswith("/b.txt") for p in paths)

    out = tmp_path / "out"
    RestoreRunner(repo).restore(snapshot, LocalTarget(out))
    assert _restored(out, "a.txt").read_text() == "a"
    assert _restored(out, "b.txt").read_text() == "b"
    repo.close()
