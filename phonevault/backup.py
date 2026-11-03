"""The backup engine.

One run does this:

1. Walk the source and collect every path.
2. Compare each file against the parent snapshot. Same size and mtime means
   the content is assumed unchanged and its chunk list is reused verbatim --
   nothing is read from the phone. This is what makes the second and every
   later backup fast.
3. Read whatever is left, split it into chunks, and store the chunks that the
   repository does not already have.
4. Serialise the resulting tree, chunk that too, and write a snapshot.

Step 2 is a heuristic and is documented as one: a file rewritten within the
same second, to the same length, with content changed, is missed. The
``--rescan`` option exists for when that matters.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

from . import hashing
from .checkpoint import Checkpoint, checkpoint_key
from .errors import (
    BackupPaused,
    BatchReadError,
    DeviceLostError,
    PhonevaultError,
    SourceError,
)
from .progress import Progress
from .repo import Repository
from .snapshot import KIND_DIR, KIND_FILE, FileEntry, Snapshot, local_hostname, serialize_tree
from .sources.base import Matcher, Source, SourceFile, normalise_root


class _PauseSignal(Exception):
    """Internal: unwinds out of the read loop when a pause is requested."""


@dataclass
class BackupStats:
    files_total: int = 0
    files_new: int = 0
    files_changed: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    files_resumed: int = 0
    dirs: int = 0
    symlinks: int = 0
    bytes_total: int = 0
    bytes_read: int = 0
    bytes_stored: int = 0
    bytes_before_compression: int = 0
    bytes_after_compression: int = 0
    chunks_new: int = 0
    chunks_duplicate: int = 0
    packs_written: int = 0
    tree_chunks: int = 0
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = asdict(self)
        # The error list can run to thousands of lines on a rooted whole-disk
        # walk; the snapshot keeps a sample and the count.
        data["error_count"] = len(self.errors)
        data["errors"] = self.errors[:100]
        return data


class BackupRunner:
    def __init__(
        self,
        repo: Repository,
        source: Source,
        progress: Progress | None = None,
    ) -> None:
        self.repo = repo
        self.source = source
        self.progress = progress or Progress()
        #: How many times to wait for a vanished device before giving up,
        #: counting only attempts that made no progress at all.
        self.max_reconnect_attempts = 5
        self.reconnect_timeout = 600.0
        #: Force a pack flush and checkpoint at least this often, so a slow
        #: link cannot go a long time with nothing recorded.
        self.checkpoint_interval = 120.0
        self._checkpoint_store: Checkpoint | None = None
        self._checkpoint_meta: dict = {}
        self._last_checkpoint = 0.0
        self._pack_writer = None
        #: Set by :meth:`request_pause` from another thread (the web UI) or a
        #: signal handler (Ctrl+C). Checked between files, never mid-file, so
        #: a paused run never leaves a half-read file in a snapshot.
        self._pause = threading.Event()
        #: Entries recovered from an interrupted run's checkpoint. Kept so the
        #: checkpoints this run writes still describe them.
        self._resumed_entries: dict[str, FileEntry] = {}

    def request_pause(self) -> None:
        """Ask the running backup to stop cleanly at the next file boundary."""
        self._pause.set()

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    def run(
        self,
        roots: Sequence[str],
        *,
        excludes: Iterable[str] = (),
        use_default_excludes: bool = True,
        tags: Iterable[str] = (),
        rescan: bool = False,
        dry_run: bool = False,
        resume: bool = True,
        parent: Snapshot | None = None,
    ) -> Snapshot:
        started = time.time()
        stats = BackupStats()
        self._pause.clear()
        matcher = Matcher(excludes, use_defaults=use_default_excludes)
        roots = [normalise_root(r) for r in roots if r.strip()]
        if not roots:
            raise PhonevaultError("no source paths given -- nothing to back up")

        self.progress.message(f"Source: {self.source.describe()}")
        self._warn_about_uncovered_storage(roots)
        self.repo.sync_index()

        if parent is None and not rescan:
            parent = self.repo.latest_snapshot(roots=list(roots), source=self.source.name)
        parent_tree: dict[str, FileEntry] = {}
        if parent is not None and not rescan:
            self.progress.message(f"Comparing against snapshot {parent.short_id}")
            try:
                parent_tree = self.repo.tree_map(parent)
            except PhonevaultError as exc:
                # A damaged parent tree must not stop a backup; it only costs
                # a full re-read of the phone.
                self.progress.warn(f"parent snapshot unreadable, backing up in full: {exc}")
                parent_tree = {}
                parent = None

        # An interrupted earlier run may have left a record of what it had
        # already stored. Its entries are as trustworthy as a parent
        # snapshot's -- both only promise chunks that reached a pack -- so
        # they are merged in, taking priority as the more recent of the two.
        resumed = 0
        resumed_paths: set[str] = set()
        if not dry_run:
            self._checkpoint_store = Checkpoint(
                self.repo.backend,
                checkpoint_key(self.repo.id, self.source.name, roots, matcher.patterns),
                codec=self.repo.codec(),
            )
            self._checkpoint_meta = {
                "roots": list(roots),
                "source": self.source.name,
                "device": self.source.describe(),
            }
            if resume and not rescan:
                _meta, saved = self._checkpoint_store.load()
                if saved:
                    parent_tree = {**parent_tree, **saved}
                    resumed_paths = set(saved)
                    resumed = len(saved)
                    # Carry the earlier run's entries into the checkpoints
                    # this run writes. Without this the first checkpoint
                    # overwrites the record of everything the interrupted run
                    # achieved -- so a second interruption would throw away
                    # both runs' work and re-read the whole device.
                    self._resumed_entries = dict(saved)
                    self.progress.message(
                        f"Resuming an interrupted backup: {resumed} file(s) were "
                        f"already stored"
                    )
                else:
                    self._warn_about_stranded_checkpoints(roots)

        # -- 1. enumerate --------------------------------------------------
        self.progress.start("Scanning", 0, 0)
        listing = self._scan(roots, matcher, stats)
        self.progress.message(
            f"Found {stats.files_total} files "
            f"({_fmt(stats.bytes_total)}), {stats.dirs} directories"
        )

        # -- 2. classify ---------------------------------------------------
        reused: dict[str, FileEntry] = {}
        to_read: list[SourceFile] = []
        for meta in listing:
            if not meta.is_file:
                continue
            candidate = meta.to_entry()
            previous = parent_tree.get(meta.path)
            if (
                not meta.always_read
                and candidate.content_matches(previous)
                and self._chunks_present(previous)
            ):
                candidate.chunks = list(previous.chunks)
                reused[meta.path] = candidate
                stats.files_unchanged += 1
                if meta.path in resumed_paths:
                    stats.files_resumed += 1
            else:
                to_read.append(meta)
                if previous is None:
                    stats.files_new += 1
                else:
                    stats.files_changed += 1

        pending_bytes = sum(m.size for m in to_read)
        self.progress.message(
            f"{stats.files_unchanged} unchanged, {stats.files_new} new, "
            f"{stats.files_changed} modified"
            + (f", {stats.files_resumed} recovered from the interrupted run"
               if stats.files_resumed else "")
            + f" -- reading {_fmt(pending_bytes)}"
        )

        if dry_run:
            stats.duration = time.time() - started
            self.progress.finish(stats.as_dict())
            return Snapshot(
                id="(dry-run)",
                time=started,
                roots=list(roots),
                source=self.source.name,
                device=self.source.describe(),
                hostname=local_hostname(),
                parent=parent.id if parent else None,
                tags=list(tags),
                stats=stats.as_dict(),
                excludes=list(matcher.patterns),
            )

        # -- 3. store file content ------------------------------------------
        self.progress.start("Backing up", len(to_read), pending_bytes)
        stored: dict[str, FileEntry] = {}
        with self.repo.lock(note="backup"):
            writer = self.repo.pack_writer()
            self._pack_writer = writer
            self._last_checkpoint = time.monotonic()
            try:
                self._store_files(to_read, writer, stored, stats)

                # -- 4. build the tree ---------------------------------------
                self.progress.start("Writing snapshot", 0, 0)
                entries = self._assemble_tree(listing, reused, stored)
                tree_chunks = []
                for _offset, chunk in self.repo.chunker.split_iter(serialize_tree(entries)):
                    digest = hashing.hash_bytes(chunk)
                    if writer.add(digest, chunk):
                        stats.chunks_new += 1
                    else:
                        stats.chunks_duplicate += 1
                    tree_chunks.append(digest)
                stats.tree_chunks = len(tree_chunks)
            finally:
                writer.flush()
                stats.packs_written = writer.packs_written
                stats.bytes_stored = writer.bytes_written
                stats.bytes_before_compression = writer.raw_bytes
                stats.bytes_after_compression = writer.stored_bytes

            stats.errors.extend(getattr(self.source, "errors", []))
            stats.duration = time.time() - started
            snapshot = Snapshot(
                id=Snapshot.new_id(),
                time=started,
                roots=list(roots),
                tree=tree_chunks,
                source=self.source.name,
                device=self.source.describe(),
                hostname=local_hostname(),
                parent=parent.id if parent else None,
                tags=list(tags),
                stats=stats.as_dict(),
                excludes=list(matcher.patterns),
            )
            self.repo.save_snapshot(snapshot)

            # The snapshot now records everything the checkpoint did, so the
            # checkpoint is redundant. Only remove it once the snapshot is
            # safely written.
            if self._checkpoint_store is not None:
                try:
                    self._checkpoint_store.delete()
                except Exception as exc:
                    self.progress.warn(f"could not clear the resume checkpoint: {exc}")
            self._pack_writer = None

        self.progress.finish(stats.as_dict())
        return snapshot

    # -- stages ------------------------------------------------------------

    def _warn_about_stranded_checkpoints(self, roots: Sequence[str]) -> None:
        """Say so when an interrupted run's progress cannot be picked up.

        A checkpoint is keyed on the roots and excludes it was made with,
        because resuming one job from another's progress would skip files
        that were never read. That is the right rule, but it fails silently:
        change one root after an interrupted run and the next backup quietly
        re-reads the whole phone with no hint as to why. The hour it costs is
        worth one line of explanation.
        """
        from .checkpoint import list_checkpoints

        try:
            others = [k for k in list_checkpoints(self.repo.backend) if k != self._checkpoint_store.key]
        except PhonevaultError:
            return
        if not others:
            return
        self.progress.warn(
            f"{len(others)} interrupted backup(s) are saved in this repository, but none "
            f"of them were backing up exactly these paths, so their progress cannot be "
            f"reused and everything will be read again. This happens when the paths "
            f"change between runs. Current paths: {', '.join(roots)}"
        )

    def _warn_about_uncovered_storage(self, roots: Sequence[str]) -> None:
        """Say so, loudly, if a whole storage volume is being skipped."""
        finder = getattr(self.source, "uncovered_volumes", None)
        if finder is None:
            return
        try:
            missed = finder(roots)
        except Exception:
            return  # never let a diagnostic break the backup
        for volume in missed:
            self.progress.warn(
                f"{volume['label']} at {volume['path']} holds "
                f"{_fmt(volume['used'])} that this backup will NOT include. "
                f"Add it with: phonevault config set roots "
                f"\"{','.join([*roots, volume['path']])}\""
            )

    def _scan(self, roots: Sequence[str], matcher: Matcher, stats: BackupStats) -> list[SourceFile]:
        listing: list[SourceFile] = []
        for meta in self.source.walk(roots, matcher):
            listing.append(meta)
            if meta.is_dir:
                stats.dirs += 1
            elif meta.is_symlink:
                stats.symlinks += 1
            else:
                stats.files_total += 1
                stats.bytes_total += meta.size
            if len(listing) % 500 == 0:
                self.progress.file_started(meta.path)
                self.progress.file_done(meta.path, "scanned")
        # Sorting keeps the serialised tree stable between runs, which is what
        # lets its chunks deduplicate against the previous snapshot.
        listing.sort(key=lambda m: m.path)
        return listing

    def _checkpoint(self, stored: dict[str, FileEntry], *, force: bool = False) -> None:
        """Record the finished files, if enough time has passed.

        Only files whose chunks are already in a flushed pack may be written
        out, so the pack writer is flushed first. That is why this is time
        limited rather than per file: flushing after every photo would
        produce a pack per photo and defeat the point of packing.
        """
        if self._checkpoint_store is None:
            return
        now = time.monotonic()
        if not force and now - self._last_checkpoint < self.checkpoint_interval:
            return
        self._last_checkpoint = now
        try:
            if self._pack_writer is not None:
                self._pack_writer.flush()
            # Everything known to be in a flushed pack: what an earlier
            # interrupted run stored, plus what this one has added.
            durable = {**self._resumed_entries, **stored}
            self._checkpoint_store.save(
                sorted(durable.values(), key=lambda e: e.path),
                {**self._checkpoint_meta, "files": len(durable)},
            )
        except Exception as exc:
            # Losing a checkpoint costs time on a retry, never data, so it
            # must not be allowed to fail the backup that is going fine.
            self.progress.warn(f"could not write the resume checkpoint: {exc}")

    def _chunks_present(self, entry: FileEntry | None) -> bool:
        """Whether every chunk a reused entry depends on is still stored."""
        if entry is None:
            return False
        if entry.size == 0:
            return not entry.chunks
        return bool(entry.chunks) and all(self.repo.index.has(c) for c in entry.chunks)

    def _store_files(
        self,
        to_read: Sequence[SourceFile],
        writer,
        stored: dict[str, FileEntry],
        stats: BackupStats,
    ) -> None:
        """Read and store every file, surviving the phone going away.

        A lost cable or a dropped Wi-Fi link kills the whole read stream, not
        just the file in flight, so the batch generator cannot simply be
        resumed. Instead the remaining files are recomputed and a fresh
        stream is opened once the device is back.
        """
        remaining = list(to_read)
        attempt = 0
        batching_disabled = False
        while remaining:
            try:
                self._store_pass(remaining, writer, stored, stats)
                return
            except _PauseSignal:
                done = len(stored)
                left = len([m for m in remaining if m.path not in stored])
                writer.flush()
                self._checkpoint(stored, force=True)
                raise BackupPaused(
                    f"Backup paused: {done} file(s) stored, {left} still to do. "
                    f"Run the backup again to carry on from here.",
                    {**stats.as_dict(), "files_stored": done, "files_remaining": left},
                ) from None
            except BatchReadError as exc:
                disable = getattr(self.source, "disable_batch_read", None)
                remaining = [m for m in remaining if m.path not in stored]
                if disable is None or batching_disabled:
                    # Already reading one file at a time and it still broke,
                    # so this is not something retrying will fix.
                    raise
                disable()
                batching_disabled = True
                stats.errors.append(str(exc))
                self.progress.warn(
                    f"{exc} {len(remaining)} file(s) left to read individually."
                )
                writer.flush()
                self._checkpoint(stored, force=True)

            except KeyboardInterrupt:
                writer.flush()
                self._checkpoint(stored, force=True)
                raise BackupPaused(
                    f"Backup interrupted: {len(stored)} file(s) stored. "
                    f"Run the backup again to carry on from here.",
                    {**stats.as_dict(), "files_stored": len(stored)},
                ) from None
            except (SourceError, OSError) as exc:
                done = {m.path for m in remaining if m.path in stored}
                progressed = len(done)
                remaining = [m for m in remaining if m.path not in stored]
                attempt = 0 if progressed else attempt + 1
                if attempt > self.max_reconnect_attempts:
                    raise SourceError(
                        f"lost the connection to the device and could not get it back "
                        f"after {self.max_reconnect_attempts} attempts: {exc}\n"
                        f"{len(stored)} file(s) are already stored; re-run the backup "
                        f"and it will carry on from here."
                    ) from exc

                stats.errors.append(f"connection lost after {len(stored)} file(s): {exc}")
                self.progress.warn(
                    f"lost the device ({exc}); {len(remaining)} file(s) left. "
                    f"Waiting for it to come back..."
                )
                # Everything read so far is worth keeping before we block.
                writer.flush()
                self._checkpoint(stored, force=True)
                if not self._wait_for_device():
                    raise SourceError(
                        f"the device did not come back. {len(stored)} file(s) are "
                        f"stored; re-run the backup to continue from there."
                    ) from exc
                self.progress.message(
                    f"Device is back -- continuing with {len(remaining)} file(s)"
                )

    def _wait_for_device(self) -> bool:
        waiter = getattr(self.source, "wait_until_ready", None)
        if waiter is None:
            return False
        try:
            return bool(waiter(self.reconnect_timeout))
        except Exception:
            return False

    def _store_pass(
        self,
        to_read: Sequence[SourceFile],
        writer,
        stored: dict[str, FileEntry],
        stats: BackupStats,
    ) -> None:
        batch_reader = getattr(self.source, "read_batch", None)
        if batch_reader is not None:
            pairs = batch_reader(to_read)
        else:
            pairs = self._sequential_pairs(to_read)

        for meta, stream in pairs:
            if self._pause.is_set():
                raise _PauseSignal()
            self.progress.file_started(meta.path, meta.size)
            try:
                entry = self._store_one(meta, stream, writer, stats)
            except (DeviceLostError, BatchReadError):
                # Neither is this one file's fault: a lost device needs a
                # reconnect, a broken tar stream needs batching turned off.
                # Marking the rest of the batch as failed would throw away
                # perfectly good files.
                raise
            except (SourceError, OSError) as exc:
                stats.files_failed += 1
                stats.errors.append(f"{meta.path}: {exc}")
                self.progress.warn(f"skipped {meta.path}: {exc}")
                self.progress.file_done(meta.path, "failed", meta.size)
                continue
            stored[meta.path] = entry
            self.progress.file_done(meta.path, "stored", meta.size)
            self._checkpoint(stored)

    def _sequential_pairs(self, to_read: Sequence[SourceFile]):
        for meta in to_read:
            stream = self.source.open(meta.path, meta.size)
            try:
                yield meta, stream
            finally:
                stream.close()

    def _store_one(self, meta: SourceFile, stream, writer, stats: BackupStats) -> FileEntry:
        entry = meta.to_entry()
        chunks: list[str] = []
        actual_size = 0
        for _offset, chunk in self.repo.chunker.split_stream(stream):
            digest = hashing.hash_bytes(chunk)
            if writer.add(digest, chunk):
                stats.chunks_new += 1
            else:
                stats.chunks_duplicate += 1
            chunks.append(digest)
            actual_size += len(chunk)
            stats.bytes_read += len(chunk)
            self.progress.advance_bytes(len(chunk))
        entry.chunks = chunks
        # A partition image has a size fixed by the block device, so reading
        # fewer bytes than that is a dropped connection, never drift. The
        # reader notices a short read too, but only when the stream is torn
        # down -- by which time this entry has already been recorded, and the
        # snapshot holds a truncated image described as a whole one. That is
        # the worst possible outcome: a backup that looks healthy and cannot
        # be flashed. Refuse it here, while it can still be retried.
        if meta.always_read and meta.size and actual_size != meta.size:
            raise DeviceLostError(
                f"{meta.path} is a fixed-size image but only {actual_size:,} of "
                f"{meta.size:,} bytes were read; the connection dropped data"
            )
        # Otherwise trust what was actually read over what stat claimed. Sizes
        # drift on a live phone, and a mismatch would make restore truncate.
        entry.size = actual_size
        return entry

    def _assemble_tree(
        self,
        listing: Sequence[SourceFile],
        reused: dict[str, FileEntry],
        stored: dict[str, FileEntry],
    ) -> list[FileEntry]:
        entries: list[FileEntry] = []
        for meta in listing:
            if meta.kind == KIND_FILE:
                entry = stored.get(meta.path) or reused.get(meta.path)
                if entry is None:
                    continue  # failed to read; leave it out rather than store an empty file
                entries.append(entry)
            elif meta.kind == KIND_DIR:
                entries.append(meta.to_entry())
            else:
                entries.append(meta.to_entry())
        return entries


def _fmt(count: float) -> str:
    from .progress import format_bytes

    return format_bytes(count)
