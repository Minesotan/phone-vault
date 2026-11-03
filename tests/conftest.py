"""Shared fixtures.

Everything here runs against a real repository on a temporary filesystem --
no mocks of the storage layer. Backup bugs live in the seams between
chunking, packing, indexing and tree building, and mocking any of those
hides exactly the failures worth catching.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from phonevault.backends import open_backend
from phonevault.backup import BackupRunner
from phonevault.repo import Repository
from phonevault.sources.local import LocalSource


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """A small tree standing in for a phone's storage."""
    root = tmp_path / "phone"
    (root / "DCIM" / "Camera").mkdir(parents=True)
    (root / "Documents").mkdir()
    (root / "cache").mkdir()

    (root / "DCIM" / "Camera" / "a.jpg").write_bytes(b"A" * 300_000)
    (root / "DCIM" / "Camera" / "b.jpg").write_bytes(os.urandom(200_000))
    # Identical content under a different name: must deduplicate.
    (root / "DCIM" / "Camera" / "a-copy.jpg").write_bytes(b"A" * 300_000)
    # Written as bytes on purpose: text mode would turn "\n" into "\r\n" on
    # Windows, so a test comparing restored bytes would be checking the
    # platform's newline handling rather than the backup.
    (root / "Documents" / "notes.txt").write_bytes(b"hello\n")
    (root / "Documents" / "empty.bin").write_bytes(b"")
    (root / "Documents" / "unicode 中文.txt").write_bytes("中文\n".encode())
    (root / "Documents" / "scratch.tmp").write_bytes(b"junk")  # excluded by default
    (root / "cache" / "junk.bin").write_bytes(b"junk")  # excluded by default
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    repository = Repository.init(
        open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache"
    )
    yield repository
    repository.close()


@pytest.fixture
def runner(repo: Repository) -> BackupRunner:
    return BackupRunner(repo, LocalSource(label="test-phone"))


@pytest.fixture
def posix_path():
    """Compare paths the way the repository stores them."""
    return lambda p: str(p).replace("\\", "/")
