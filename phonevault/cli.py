"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence

from . import __version__
from .backends import open_backend
from .backup import BackupRunner
from .chunker import Chunker
from .config import ROOT_PRESETS, Config, Profile, apply_setting, get_setting
from .errors import BackupPaused, PhonevaultError
from .progress import ConsoleProgress, format_bytes, format_duration
from .prune import RetentionPolicy, check, forget, prune
from .repo import Repository
from .restore import AdbTarget, LocalTarget, RestoreRunner, list_directory, matches_any
from .snapshot import Snapshot
from .sources import open_source

# -- plumbing --------------------------------------------------------------


def _load_config(args) -> Config:
    return Config.load(getattr(args, "config", None))


def _profile(args, config: Config) -> Profile:
    profile = config.profile(getattr(args, "profile", None))
    # Command-line flags win over the stored profile for this run only.
    for flag, field in (
        ("repository", "repository"),
        ("device", "device"),
        ("adb_path", "adb_path"),
        ("source", "source"),
    ):
        value = getattr(args, flag, None)
        if value:
            setattr(profile, field, value)
    if getattr(args, "no_root", False):
        profile.use_root = False
    if getattr(args, "no_batch_read", False):
        profile.batch_read = False
    return profile


def _open_repo(profile: Profile, *, create: bool = False, **init_kwargs) -> Repository:
    problems = [p for p in profile.validate() if "repository" in p or "source" in p]
    if not profile.repository:
        raise PhonevaultError(problems[0] if problems else "no repository configured")
    backend = open_backend(profile.repository, **profile.backend_options())
    if create:
        return Repository.init(backend, **init_kwargs)
    return Repository.open(backend)


def _install_pause_handler(runner, progress) -> None:
    """Make Ctrl+C pause the backup instead of throwing the work away.

    The first Ctrl+C asks the engine to stop at the next file boundary, so
    everything already read is flushed and checkpointed. A second one is
    taken to mean "stop now", and the default behaviour is restored so the
    user is never trapped.
    """
    import signal

    try:
        original = signal.getsignal(signal.SIGINT)
    except (ValueError, AttributeError):  # pragma: no cover - odd hosts
        return

    def handler(signum, frame):
        signal.signal(signal.SIGINT, original)
        progress.warn("Pausing after the current file... (press Ctrl+C again to stop now)")
        runner.request_pause()

    try:
        signal.signal(signal.SIGINT, handler)
    except ValueError:
        pass  # not on the main thread; the web UI has its own pause button


def _progress(args) -> ConsoleProgress:
    return ConsoleProgress(enabled=not getattr(args, "quiet", False))


def _print(*parts: str) -> None:
    print(*parts)


def _emit_json(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


# -- commands ---------------------------------------------------------------


def cmd_init(args) -> int:
    config = _load_config(args)
    profile = _profile(args, config)
    chunker = Chunker(
        algorithm=args.chunker,
        avg_size=_parse_size(args.chunk_size) if args.chunk_size else Chunker().avg_size,
    )
    from .blobstore import DEFAULT_PACK_SIZE

    repo = _open_repo(
        profile,
        create=True,
        chunker=chunker,
        pack_size=_parse_size(args.pack_size) if args.pack_size else DEFAULT_PACK_SIZE,
        compression=args.compression,
        compression_level=args.compression_level,
    )
    _print(f"Initialised repository at {repo.backend}")
    _print(f"  id          {repo.id}")
    _print(f"  chunker     {chunker.algorithm}, average {format_bytes(chunker.avg_size)}")
    _print(f"  compression {args.compression} (level {args.compression_level})")
    if args.save:
        config.add_profile(profile)
        config.save()
        _print(f"  saved to profile '{profile.name}' in {config.path}")
    repo.close()
    return 0


def cmd_backup(args) -> int:
    config = _load_config(args)
    profile = _profile(args, config)
    if args.roots:
        profile.roots = args.roots
    if args.preset:
        profile.roots = ROOT_PRESETS[args.preset]
    for problem in profile.validate():
        raise PhonevaultError(problem)

    progress = _progress(args)
    source = open_source(profile.source, **profile.source_options())
    repo = _open_repo(profile)
    try:
        if profile.source == "adb" and getattr(source, "root_active", False):
            progress.message("Root access confirmed")
        elif profile.source == "adb" and profile.use_root:
            progress.warn(
                "root is not available; /data will not be readable. "
                "Approve the su request on the phone, or pass --no-root to silence this."
            )

        runner = BackupRunner(repo, source, progress)
        _install_pause_handler(runner, progress)
        snapshot = runner.run(
            profile.roots,
            excludes=list(profile.excludes) + list(args.exclude or []),
            use_default_excludes=profile.use_default_excludes,
            tags=list(profile.tags) + list(args.tag or []),
            rescan=args.rescan,
            dry_run=args.dry_run,
            resume=not args.no_resume,
        )
        _report_backup(snapshot, args.dry_run)

        if args.forget and not args.dry_run:
            policy = profile.retention.to_policy()
            if not policy.is_empty():
                _print(f"\nApplying retention: {policy.describe()}")
                _, removed = forget(repo, policy, progress=progress)
                if removed and profile.prune_after_forget:
                    stats = prune(repo, progress=progress)
                    _print(f"Pruned {format_bytes(stats.bytes_freed)}")
    finally:
        source.close()
        repo.close()
    return 0


def _report_backup(snapshot: Snapshot, dry_run: bool) -> None:
    stats = snapshot.stats
    label = "Would back up" if dry_run else f"Snapshot {snapshot.short_id}"
    _print(f"\n{label}")
    _print(f"  device        {snapshot.device}")
    _print(f"  paths         {', '.join(snapshot.roots)}")
    _print(
        f"  files         {stats.get('files_total', 0)} "
        f"({stats.get('files_new', 0)} new, {stats.get('files_changed', 0)} modified, "
        f"{stats.get('files_unchanged', 0)} unchanged)"
    )
    if stats.get("files_resumed"):
        _print(
            f"  resumed       {stats['files_resumed']} file(s) recovered from an "
            f"interrupted run (not re-read)"
        )
    _print(f"  total size    {format_bytes(stats.get('bytes_total', 0))}")
    _print(f"  read          {format_bytes(stats.get('bytes_read', 0))}")
    if not dry_run:
        _print(
            f"  stored        {format_bytes(stats.get('bytes_stored', 0))} "
            f"in {stats.get('packs_written', 0)} pack(s)"
        )
        before = stats.get("bytes_before_compression", 0)
        after = stats.get("bytes_after_compression", 0)
        if before:
            saved = before - after
            _print(
                f"  compression   {format_bytes(before)} -> {format_bytes(after)}"
                f"  ({100 * saved / before:.1f}% saved)"
            )
        _print(
            f"  chunks        {stats.get('chunks_new', 0)} new, "
            f"{stats.get('chunks_duplicate', 0)} already stored"
        )
    _print(f"  took          {format_duration(stats.get('duration', 0))}")
    failed = stats.get("files_failed", 0)
    errors = stats.get("error_count", 0)
    if failed or errors:
        _print(f"  problems      {failed} file(s) unreadable, {errors} message(s)")
        for line in stats.get("errors", [])[:5]:
            _print(f"                {line}")
        if errors > 5:
            _print(f"                ...and {errors - 5} more")


def cmd_snapshots(args) -> int:
    config = _load_config(args)
    repo = _open_repo(_profile(args, config))
    try:
        snapshots = repo.list_snapshots()
        if args.json:
            _emit_json([s.to_dict() for s in snapshots])
            return 0
        if not snapshots:
            _print("No snapshots yet. Run 'phonevault backup'.")
            return 0
        _print(f"{'ID':<14}{'WHEN':<21}{'FILES':>9}{'SIZE':>12}  TAGS / PATHS")
        for snapshot in snapshots:
            stats = snapshot.stats
            tags = ",".join(snapshot.tags)
            _print(
                f"{snapshot.short_id:<14}{snapshot.datetime_str:<21}"
                f"{stats.get('files_total', 0):>9}"
                f"{format_bytes(stats.get('bytes_total', 0)):>12}  "
                f"{tags + '  ' if tags else ''}{', '.join(snapshot.roots)}"
            )
        _print(f"\n{len(snapshots)} snapshot(s)")
    finally:
        repo.close()
    return 0


def cmd_ls(args) -> int:
    config = _load_config(args)
    repo = _open_repo(_profile(args, config))
    try:
        snapshot = repo.resolve_snapshot(args.snapshot)
        entries = list(repo.read_tree(snapshot))
        if args.long or args.recursive:
            selected = [e for e in entries if matches_any(e.path, [args.path] if args.path else [])]
            if args.json:
                _emit_json([_entry_json(e) for e in selected])
                return 0
            for entry in selected if args.recursive else list_directory(entries, args.path or "/"):
                _print(_format_entry(entry))
            return 0
        children = list_directory(entries, args.path or "/")
        if args.json:
            _emit_json([_entry_json(e) for e in children])
            return 0
        if not children:
            _print(f"nothing under {args.path or '/'} in snapshot {snapshot.short_id}")
        for entry in children:
            _print(_format_entry(entry))
    finally:
        repo.close()
    return 0


def _entry_json(entry) -> dict:
    return {
        "path": entry.path,
        "kind": entry.kind,
        "size": entry.size,
        "mtime": entry.mtime,
        "mode": entry.mode,
        "target": entry.target,
        "chunks": len(entry.chunks),
    }


def _format_entry(entry) -> str:
    kind = {"d": "d", "l": "l", "f": "-"}.get(entry.kind, "?")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime)) if entry.mtime else ""
    size = format_bytes(entry.size) if entry.is_file else ""
    suffix = f" -> {entry.target}" if entry.is_symlink else ""
    return f"{kind}{entry.mode:04o}  {size:>10}  {when:<16}  {entry.path}{suffix}"


def cmd_restore(args) -> int:
    config = _load_config(args)
    profile = _profile(args, config)
    repo = _open_repo(profile)
    progress = _progress(args)
    source = None
    try:
        snapshot = repo.resolve_snapshot(args.snapshot)
        if args.to_phone:
            source = open_source("adb", **profile.source_options())
            target = AdbTarget(source.device, args.target or "/sdcard/Restored")
            if not args.yes:
                _print(f"About to write files onto {target.describe()}")
                if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                    _print("Cancelled.")
                    return 1
        else:
            if not args.target:
                raise PhonevaultError("give a destination folder, or use --to-phone")
            target = LocalTarget(args.target)

        stats = RestoreRunner(repo, progress).restore(
            snapshot,
            target,
            include=args.include or [],
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            verify=not args.no_verify,
        )
        _print(f"\n{'Would restore' if args.dry_run else 'Restored'} from {snapshot.short_id}")
        _print(f"  files      {stats.files}  ({format_bytes(stats.bytes_written)})")
        _print(f"  folders    {stats.dirs}")
        if stats.symlinks:
            _print(f"  symlinks   {stats.symlinks}")
        if stats.skipped:
            _print(f"  skipped    {stats.skipped} already present")
        if stats.renamed:
            _print(f"  renamed    {stats.renamed} (characters this platform rejects)")
        if stats.failed:
            _print(f"  failed     {stats.failed}")
            for line in stats.errors[:5]:
                _print(f"             {line}")
        _print(f"  took       {format_duration(stats.duration)}")
        return 1 if stats.failed else 0
    finally:
        if source is not None:
            source.close()
        repo.close()


def cmd_dump(args) -> int:
    config = _load_config(args)
    repo = _open_repo(_profile(args, config))
    try:
        snapshot = repo.resolve_snapshot(args.snapshot)
        out = sys.stdout.buffer
        for block in RestoreRunner(repo).read_file(snapshot, args.path):
            out.write(block)
        out.flush()
    finally:
        repo.close()
    return 0


def cmd_forget(args) -> int:
    config = _load_config(args)
    profile = _profile(args, config)
    repo = _open_repo(profile)
    progress = _progress(args)
    try:
        policy = _policy_from_args(args, profile)
        if policy.is_empty():
            raise PhonevaultError(
                "no retention rules given. Use --keep-last/--keep-daily/... or configure "
                "them once with: phonevault config set retention.daily 7"
            )
        _print(f"Retention: {policy.describe()}")
        kept, removed = forget(repo, policy, dry_run=args.dry_run, progress=progress)
        _print(f"{'Would keep' if args.dry_run else 'Kept'} {len(kept)}, "
               f"{'would remove' if args.dry_run else 'removed'} {len(removed)}")
        if removed and args.prune and not args.dry_run:
            stats = prune(repo, progress=progress)
            _print(f"Pruned {format_bytes(stats.bytes_freed)} from {stats.packs_deleted} pack(s)")
    finally:
        repo.close()
    return 0


def _policy_from_args(args, profile: Profile) -> RetentionPolicy:
    explicit = any(
        getattr(args, name, None) is not None
        for name in ("keep_last", "keep_hourly", "keep_daily", "keep_weekly",
                     "keep_monthly", "keep_yearly", "keep_within_days")
    )
    if not explicit:
        return profile.retention.to_policy()
    return RetentionPolicy(
        last=args.keep_last,
        hourly=args.keep_hourly,
        daily=args.keep_daily,
        weekly=args.keep_weekly,
        monthly=args.keep_monthly,
        yearly=args.keep_yearly,
        within_days=args.keep_within_days,
        keep_tags=tuple(args.keep_tag or ()),
    )


def cmd_prune(args) -> int:
    config = _load_config(args)
    repo = _open_repo(_profile(args, config))
    progress = _progress(args)
    try:
        stats = prune(
            repo,
            dry_run=args.dry_run,
            repack_threshold=args.repack_threshold,
            progress=progress,
        )
        verb = "Would free" if args.dry_run else "Freed"
        _print(f"\n{verb} {format_bytes(stats.bytes_freed)}")
        _print(f"  live blobs      {stats.blobs_live}")
        _print(f"  dead blobs      {stats.blobs_dead}")
        _print(f"  packs deleted   {stats.packs_deleted} of {stats.packs_scanned}")
        _print(f"  packs rewritten {stats.packs_rewritten}")
        _print(f"  took            {format_duration(stats.duration)}")
        for line in stats.errors[:10]:
            _print(f"  ! {line}")
    finally:
        repo.close()
    return 0


def cmd_check(args) -> int:
    config = _load_config(args)
    repo = _open_repo(_profile(args, config))
    progress = _progress(args)
    try:
        problems = check(repo, read_data=args.read_data, progress=progress)
        if not problems:
            _print("Repository is healthy.")
            return 0
        _print(f"Found {len(problems)} problem(s):")
        for line in problems[:100]:
            _print(f"  {line}")
        return 2
    finally:
        repo.close()


def cmd_repair(args) -> int:
    from .repair import repair

    config = _load_config(args)
    repo = _open_repo(_profile(args, config))
    progress = _progress(args)
    try:
        report = repair(
            repo, discard_damaged=args.discard_damaged, progress=progress
        )
    finally:
        repo.close()

    _print(
        f"\n{report.packs_in_repo} pack(s), {report.index_objects} index object(s), "
        f"{report.local_index_entries} blob(s) in the local index"
    )
    if report.reindexed:
        _print(f"  rebuilt {len(report.reindexed)} index object(s) from pack footers")
    if report.removed:
        _print(f"  removed {len(report.removed)} unusable object(s)")
    if report.locks:
        _print(f"  note: {len(report.locks)} lock(s) held; see 'phonevault unlock'")

    problems = report.problems
    if not problems:
        _print("\nRepository and local index agree." if not report.repaired_anything
               else "\nRepaired. Repository and local index now agree.")
        return 0

    _print(f"\n{len(problems)} problem(s) remain:")
    for line in problems[:50]:
        _print(f"  {line}")
    if len(problems) > 50:
        _print(f"  ...and {len(problems) - 50} more")

    if report.damaged_files:
        _print(
            f"\n{len(report.damaged_files)} file(s) in those snapshots are missing data. "
            f"Run a backup to store them again from the phone."
        )
        shown = report.damaged_files if args.list_damaged_files else report.damaged_files[:10]
        for line in shown:
            _print(f"  {line}")
        if not args.list_damaged_files and len(report.damaged_files) > 10:
            _print(f"  ...and {len(report.damaged_files) - 10} more (--list-damaged-files)")

    if not args.discard_damaged and (report.orphan_index_objects or report.unreadable_packs
                                     or report.temp_objects):
        _print("\nRe-run with --discard-damaged to clear out what cannot be recovered.")
    return 2


def cmd_unlock(args) -> int:
    config = _load_config(args)
    repo = _open_repo(_profile(args, config))
    try:
        locks = repo.list_locks()
        if not locks:
            _print("No locks held.")
            return 0
        for info in locks:
            age = format_duration(time.time() - float(info.get("time", 0)))
            _print(
                f"  {'exclusive' if info.get('exclusive') else 'shared':<10} "
                f"{info.get('note', '?'):<10} {info.get('host', '?')} "
                f"pid {info.get('pid', '?')}, {age} old"
            )
        if not args.force:
            _print("\nRe-run with --force to break these locks.")
            _print("Only do that if you are sure no backup or prune is running.")
            return 1
        _print(f"Broke {repo.break_locks()} lock(s).")
    finally:
        repo.close()
    return 0


def cmd_devices(args) -> int:
    from .sources.adb import AdbDevice, find_adb

    _print(f"adb: {find_adb(getattr(args, 'adb_path', None))}")
    devices = AdbDevice.list_devices(getattr(args, "adb_path", None))
    if not devices:
        _print("\nNo devices detected.")
        _print("  1. On the phone: Settings > About phone > tap 'Build number' seven times")
        _print("  2. Settings > System > Developer options > enable 'USB debugging'")
        _print("  3. Connect by USB and accept the authorisation prompt")
        return 1
    _print("")
    for info in devices:
        _print(f"  {info['serial']:<24} {info['state']:<14} {info.get('model', '')}")
        if info["state"] == "unauthorized":
            _print("      unlock the phone and accept 'Allow USB debugging'")
        elif info["state"] == "device":
            try:
                device = AdbDevice(info["serial"], getattr(args, "adb_path", None))
                _print(f"      {device.describe()}   root: {'yes' if device.has_root else 'no'}")
                for row in device.storage_summary():
                    _print(
                        f"      {row['mounted']:<22} {row['used']} used of {row['size']}"
                        f"  ({row['available']} free)"
                    )
            except PhonevaultError as exc:
                _print(f"      could not query: {exc}")
    return 0


def cmd_wifi(args) -> int:
    from .sources.adb import AdbDevice

    if args.pair:
        _print(AdbDevice.pair(args.pair, args.code, getattr(args, "adb_path", None)))
        return 0
    if args.connect:
        _print(AdbDevice.connect(args.connect, getattr(args, "adb_path", None)))
        return 0

    device = AdbDevice(args.device, getattr(args, "adb_path", None))
    address = device.ip_address()
    if not address:
        raise PhonevaultError(
            "could not read the phone's Wi-Fi address; make sure it is on Wi-Fi, "
            "not only mobile data"
        )
    _print(device.enable_tcpip(args.port))
    time.sleep(1.5)
    endpoint = f"{address}:{args.port}"
    _print(AdbDevice.connect(endpoint, getattr(args, "adb_path", None)))
    _print(f"\nThe phone is now reachable at {endpoint} without a cable.")
    _print(f"Pin it into the profile with: phonevault config set device {endpoint}")
    return 0


def cmd_config(args) -> int:
    config = _load_config(args)
    if args.action == "path":
        _print(str(config.path))
        return 0
    if args.action == "show":
        profile = config.profile(args.profile)
        data = profile.to_dict()
        if data.get("smb_password"):
            data["smb_password"] = "***"
        if args.json:
            _emit_json({"profile": profile.name, "path": str(config.path), **data})
            return 0
        _print(f"profile '{profile.name}'  ({config.path})")
        for key, value in data.items():
            _print(f"  {key:<22} {value}")
        return 0
    if args.action == "profiles":
        for name in sorted(config.profiles):
            marker = "*" if name == config.default_profile else " "
            _print(f" {marker} {name:<16} {config.profiles[name].repository}")
        return 0
    if args.action == "set":
        profile = config.profile(args.profile)
        if args.key == "smb_password" and not args.allow_plaintext_password:
            raise PhonevaultError(
                f"storing a password in {config.path} leaves it readable in plain text.\n"
                f"Prefer setting the {'PHONEVAULT_SMB_PASSWORD'} environment variable.\n"
                f"To store it anyway, add --allow-plaintext-password."
            )
        value = apply_setting(profile, args.key, args.value)
        config.add_profile(profile)
        config.save()
        shown = "***" if "password" in args.key else value
        _print(f"{profile.name}.{args.key} = {shown}")
        return 0
    if args.action == "get":
        _print(str(get_setting(config.profile(args.profile), args.key)))
        return 0
    if args.action == "add-profile":
        if args.name in config.profiles:
            raise PhonevaultError(f"profile '{args.name}' already exists")
        config.add_profile(Profile(name=args.name))
        config.save()
        _print(f"Added profile '{args.name}'. Configure it with:")
        _print(f"  phonevault --profile {args.name} config set repository <path>")
        return 0
    if args.action == "remove-profile":
        config.remove_profile(args.name)
        config.save()
        _print(f"Removed profile '{args.name}'")
        return 0
    if args.action == "default":
        if args.name not in config.profiles:
            raise PhonevaultError(f"no profile named '{args.name}'")
        config.default_profile = args.name
        config.save()
        _print(f"Default profile is now '{args.name}'")
        return 0
    raise PhonevaultError(f"unknown config action {args.action!r}")


def cmd_repo(args) -> int:
    config = _load_config(args)
    profile = _profile(args, config)
    repo = _open_repo(profile)
    try:
        if args.action == "show":
            from .checkpoint import list_checkpoints

            snapshots = repo.list_snapshots()
            total = sum(
                repo.backend.size(p) for p in repo.backend.list("packs")
            )
            _print(f"repository    {repo.backend}")
            _print(f"  id          {repo.id}")
            _print(f"  format      {repo.config.get('version')}")
            _print(f"  created     {time.strftime('%Y-%m-%d %H:%M', time.localtime(repo.config.get('created', 0)))}")
            _print(f"  chunker     {repo.chunker.algorithm}, average "
                   f"{format_bytes(repo.chunker.avg_size)}  (fixed for the life of the repo)")
            _print(f"  compression {repo.config.get('compression', 'auto')} "
                   f"(level {repo.compression_level})")
            _print(f"  pack size   {format_bytes(repo.pack_size)}")
            _print(f"  snapshots   {len(snapshots)}")
            _print(f"  stored      {format_bytes(total)} on disk")
            pending = list_checkpoints(repo.backend)
            if pending:
                _print(f"  resume      {len(pending)} interrupted backup(s) can be continued")
            return 0
        if args.action == "set":
            if args.key is None or args.value is None:
                raise PhonevaultError("usage: phonevault repo set <setting> <value>")
            value = _parse_size(args.value) if args.key == "pack_size" else args.value
            changed = repo.update_config(**{args.key: value})
            for key, new in changed.items():
                _print(f"{key} = {new}")
            if args.key in ("compression", "compression_level"):
                _print("Applies to data written from now on; existing packs are unchanged.")
            return 0
        raise PhonevaultError(f"unknown repo action {args.action!r}")
    finally:
        repo.close()


def cmd_ui(args) -> int:
    from .web.server import serve

    config = _load_config(args)
    host = args.host or config.web.host
    port = args.port or config.web.port
    serve(config, host=host, port=port, open_browser=not args.no_browser)
    return 0


def cmd_schedule(args) -> int:
    from .scheduler import describe_schedule, install_task, list_tasks, remove_task

    config = _load_config(args)
    profile = config.profile(args.profile)
    if args.action == "list":
        for line in list_tasks():
            _print(line)
        return 0
    if args.action == "remove":
        _print(remove_task(profile.name))
        return 0
    if args.action == "install":
        spec = args.at or profile.schedule
        if not spec:
            raise PhonevaultError(
                "no schedule given. Example: phonevault schedule install --at daily@02:30"
            )
        profile.schedule = spec
        config.add_profile(profile)
        config.save()
        _print(install_task(profile.name, spec, config_path=config.path, forget=not args.no_forget))
        _print(f"  {describe_schedule(spec)}")
        return 0
    raise PhonevaultError(f"unknown schedule action {args.action!r}")


# -- argument parsing -------------------------------------------------------


def _parse_size(text: str) -> int:
    """Parse ``64M``, ``4MiB``, ``1G`` and plain byte counts."""
    value = text.strip().lower().replace("ib", "").replace("b", "")
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    multiplier = 1
    if value and value[-1] in units:
        multiplier = units[value[-1]]
        value = value[:-1]
    try:
        return int(float(value) * multiplier)
    except ValueError as exc:
        raise PhonevaultError(f"cannot read {text!r} as a size") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phonevault",
        description="Deduplicating, versioned backups of an Android phone to a NAS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical first run:\n"
            "  phonevault devices\n"
            "  phonevault init --repository //NAS/backup/phone --save\n"
            "  phonevault backup\n"
            "  phonevault snapshots\n"
            "  phonevault ui\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"phonevault {__version__}")
    parser.add_argument("--config", help="path to the config file")
    parser.add_argument("-p", "--profile", help="which configured profile to use")
    parser.add_argument("-r", "--repository", help="override the repository location")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress display")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, *, repo_flag: bool = True, **kwargs):
        sp = sub.add_parser(name, help=help_text, description=help_text, **kwargs)
        sp.set_defaults(func=func)
        if repo_flag:
            # Also accept the common options after the subcommand, because
            # 'phonevault init --repository X' is what people actually type.
            # SUPPRESS keeps an omitted flag from overwriting the global one
            # with None.
            sp.add_argument(
                "-r", "--repository", default=argparse.SUPPRESS,
                help="repository location (overrides the profile)",
            )
            sp.add_argument("-p", "--profile", default=argparse.SUPPRESS)
            sp.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
        return sp

    # init
    sp = add("init", cmd_init, "Create a new repository")
    sp.add_argument("--chunker", choices=("fixed", "cdc"), default="fixed")
    sp.add_argument("--chunk-size", help="average chunk size, e.g. 4M")
    sp.add_argument("--pack-size", help="target pack file size, e.g. 48M")
    sp.add_argument("--compression", choices=("auto", "zstd", "zlib", "none"), default="auto")
    sp.add_argument("--compression-level", type=int, default=3,
                    help="1-19; higher compresses app data harder and costs CPU (default 3)")
    sp.add_argument("--save", action="store_true", help="also write this into the profile")

    # backup
    sp = add("backup", cmd_backup, "Back the phone up")
    sp.add_argument("roots", nargs="*", help="paths on the phone (defaults to the profile's)")
    sp.add_argument("--preset", choices=sorted(ROOT_PRESETS), help="use a predefined path set")
    sp.add_argument("--source", choices=("adb", "local"))
    sp.add_argument("--device", help="adb serial or host:port")
    sp.add_argument("--adb-path", help="path to the adb executable")
    sp.add_argument("--exclude", action="append", help="glob to skip (repeatable)")
    sp.add_argument("--tag", action="append", help="label this snapshot (repeatable)")
    sp.add_argument("--rescan", action="store_true", help="re-read every file, ignoring timestamps")
    sp.add_argument("--no-resume", action="store_true",
                    help="ignore the checkpoint left by an interrupted run")
    sp.add_argument("--no-root", action="store_true", help="do not use su on the phone")
    sp.add_argument("--no-batch-read", action="store_true", help="read one file at a time")
    sp.add_argument("--forget", action="store_true", help="apply the retention policy afterwards")
    sp.add_argument("-n", "--dry-run", action="store_true")

    # snapshots
    sp = add("snapshots", cmd_snapshots, "List snapshots")
    sp.add_argument("--json", action="store_true")

    # ls
    sp = add("ls", cmd_ls, "List files inside a snapshot")
    sp.add_argument("snapshot", nargs="?", default="latest")
    sp.add_argument("path", nargs="?", default="/")
    sp.add_argument("-l", "--long", action="store_true")
    sp.add_argument("-R", "--recursive", action="store_true")
    sp.add_argument("--json", action="store_true")

    # restore
    sp = add("restore", cmd_restore, "Restore files from a snapshot")
    sp.add_argument("snapshot", nargs="?", default="latest")
    sp.add_argument("target", nargs="?", help="destination folder on this computer")
    sp.add_argument("--include", action="append", help="only this path or glob (repeatable)")
    sp.add_argument("--to-phone", action="store_true", help="write back to the phone over adb")
    sp.add_argument("--device", help="adb serial or host:port")
    sp.add_argument("--adb-path")
    sp.add_argument(
        "--overwrite",
        choices=("different", "always", "never"),
        default="different",
        help="what to do when the file is already there (default: different)",
    )
    sp.add_argument("--no-verify", action="store_true", help="skip per-chunk hash checks")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask before writing to a phone")
    sp.add_argument("-n", "--dry-run", action="store_true")

    # dump
    sp = add("dump", cmd_dump, "Write one file from a snapshot to stdout")
    sp.add_argument("snapshot")
    sp.add_argument("path")

    # forget
    sp = add("forget", cmd_forget, "Remove snapshots according to a retention policy")
    sp.add_argument("--keep-last", type=int)
    sp.add_argument("--keep-hourly", type=int)
    sp.add_argument("--keep-daily", type=int)
    sp.add_argument("--keep-weekly", type=int)
    sp.add_argument("--keep-monthly", type=int)
    sp.add_argument("--keep-yearly", type=int)
    sp.add_argument("--keep-within-days", type=float)
    sp.add_argument("--keep-tag", action="append")
    sp.add_argument("--prune", action="store_true", help="reclaim the space immediately")
    sp.add_argument("-n", "--dry-run", action="store_true")

    # prune
    sp = add("prune", cmd_prune, "Reclaim space from forgotten snapshots")
    sp.add_argument("--repack-threshold", type=float, default=0.25)
    sp.add_argument("-n", "--dry-run", action="store_true")

    # check
    sp = add("check", cmd_check, "Verify repository integrity")
    sp.add_argument("--read-data", action="store_true", help="also re-hash every stored blob")

    # repair
    sp = add("repair", cmd_repair, "Bring the repository and its local index back in sync")
    sp.add_argument(
        "--discard-damaged",
        action="store_true",
        help="also delete index objects whose pack is gone, packs that cannot be "
        "located, and leftovers from an interrupted write",
    )
    sp.add_argument(
        "--list-damaged-files",
        action="store_true",
        help="print every file affected by missing data, not just a count",
    )

    # unlock
    sp = add("unlock", cmd_unlock, "Show or break repository locks")
    sp.add_argument("--force", action="store_true")

    # devices
    sp = add("devices", cmd_devices, "List attached Android devices", repo_flag=False)
    sp.add_argument("--adb-path")

    # wifi
    sp = add("wifi", cmd_wifi, "Set up adb over Wi-Fi so no cable is needed", repo_flag=False)
    sp.add_argument("--device", help="adb serial of the cabled phone")
    sp.add_argument("--port", type=int, default=5555)
    sp.add_argument("--connect", metavar="HOST:PORT", help="connect to an already-listening phone")
    sp.add_argument("--pair", metavar="HOST:PORT", help="Android 11+ wireless pairing endpoint")
    sp.add_argument("--code", help="the six-digit pairing code")
    sp.add_argument("--adb-path")

    # config
    sp = add("config", cmd_config, "Inspect and change configuration", repo_flag=False)
    sp.add_argument("-p", "--profile", default=argparse.SUPPRESS)
    sp.add_argument(
        "action",
        choices=("show", "set", "get", "path", "profiles", "add-profile", "remove-profile", "default"),
    )
    sp.add_argument("key", nargs="?", help="setting name, or profile name for profile actions")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--allow-plaintext-password", action="store_true")

    # repo
    sp = add("repo", cmd_repo, "Show or change repository-level settings")
    sp.add_argument("action", choices=("show", "set"))
    sp.add_argument("key", nargs="?", help="compression | compression_level | pack_size")
    sp.add_argument("value", nargs="?")

    # ui
    sp = add("ui", cmd_ui, "Start the web interface", repo_flag=False)
    sp.add_argument("-p", "--profile", default=argparse.SUPPRESS)
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--no-browser", action="store_true")

    # schedule
    sp = add("schedule", cmd_schedule, "Run backups automatically", repo_flag=False)
    sp.add_argument("-p", "--profile", default=argparse.SUPPRESS)
    sp.add_argument("action", choices=("install", "remove", "list"))
    sp.add_argument("--at", help="when to run, e.g. daily@02:30, hourly, weekly@sun:03:00")
    sp.add_argument("--no-forget", action="store_true", help="do not apply retention after backups")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 'config add-profile foo' reads more naturally than a --name flag, so the
    # positional is reused and normalised here.
    if args.command == "config" and args.action in (
        "add-profile",
        "remove-profile",
        "default",
    ):
        args.name = args.key

    try:
        return args.func(args) or 0
    except BackupPaused as exc:
        # Not an error: the data read so far is stored and checkpointed.
        print(f"\n{exc}", file=sys.stderr)
        return 0
    except PhonevaultError as exc:
        print(f"phonevault: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\nInterrupted. Everything already written to a pack is kept; "
            "re-run the backup to carry on.",
            file=sys.stderr,
        )
        return 130
    except BrokenPipeError:  # e.g. 'phonevault dump ... | head'
        try:
            sys.stdout.close()
        finally:
            os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
