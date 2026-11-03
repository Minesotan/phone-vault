"""Configuration, the command line, the scheduler, and adb plumbing.

The adb tests here deliberately avoid needing a phone: they exercise the
parsing and quoting, which is where the bugs that silently corrupt a backup
would live.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from phonevault.cli import _parse_size, build_parser, main
from phonevault.config import ROOT_PRESETS, Config, Profile, apply_setting, get_setting
from phonevault.errors import PhonevaultError
from phonevault.scheduler import build_command, describe_schedule, parse_schedule
from phonevault.sources.adb import (
    SEP,
    _group_by_budget,
    _parse_stat_line,
    _sh_quote,
    looks_like_lost_device,
    reject_host_path,
)
from phonevault.sources.base import SourceFile

# -- config ----------------------------------------------------------------


def test_config_round_trips(tmp_path):
    config = Config(tmp_path / "c.json")
    profile = config.profile()
    profile.repository = "//NAS/backup/phone"
    profile.roots = ["/sdcard", "/data/data"]
    profile.retention.daily = 14
    config.save()

    reloaded = Config.load(tmp_path / "c.json")
    assert reloaded.profile().repository == "//NAS/backup/phone"
    assert reloaded.profile().roots == ["/sdcard", "/data/data"]
    assert reloaded.profile().retention.daily == 14


def test_missing_config_gives_defaults(tmp_path):
    config = Config.load(tmp_path / "nope.json")
    assert config.profile().name == "phone"
    assert config.profile().source == "adb"
    assert config.profile().roots == ["/sdcard"]


def test_unknown_profile_lists_the_known_ones(tmp_path):
    config = Config(tmp_path / "c.json")
    with pytest.raises(PhonevaultError, match="configured: phone"):
        config.profile("nope")


def test_profiles_can_be_added_and_removed(tmp_path):
    config = Config(tmp_path / "c.json")
    config.add_profile(Profile(name="tablet"))
    assert sorted(config.profiles) == ["phone", "tablet"]
    config.remove_profile("tablet")
    assert sorted(config.profiles) == ["phone"]
    with pytest.raises(PhonevaultError, match="only profile"):
        config.remove_profile("phone")


def test_removing_the_default_profile_picks_another(tmp_path):
    config = Config(tmp_path / "c.json")
    config.add_profile(Profile(name="tablet"))
    config.default_profile = "tablet"
    config.remove_profile("tablet")
    assert config.default_profile == "phone"


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("repository", "//nas/x", "//nas/x"),
        ("roots", "/sdcard, /data", ["/sdcard", "/data"]),
        ("use_root", "false", False),
        ("use_root", "yes", True),
        ("batch_read", "off", False),
        ("retention.daily", "10", 10),
        ("retention.daily", "none", None),
    ],
)
def test_apply_setting(key, raw, expected):
    profile = Profile()
    assert apply_setting(profile, key, raw) == expected
    assert get_setting(profile, key) == expected


def test_apply_setting_rejects_nonsense():
    profile = Profile()
    with pytest.raises(PhonevaultError, match="unknown setting"):
        apply_setting(profile, "nope", "x")
    with pytest.raises(PhonevaultError, match="true or false"):
        apply_setting(profile, "use_root", "perhaps")
    with pytest.raises(PhonevaultError, match="whole number"):
        apply_setting(profile, "retention.daily", "lots")


def test_profile_validation():
    assert Profile(repository="").validate()
    assert not Profile(repository="//nas/x").validate()
    assert Profile(repository="//nas/x", roots=[]).validate()
    assert Profile(repository="//nas/x", source="carrier-pigeon").validate()


def test_smb_password_prefers_the_environment(monkeypatch):
    profile = Profile(smb_password="from-file")
    assert profile.resolved_smb_password() == "from-file"
    monkeypatch.setenv("PHONEVAULT_SMB_PASSWORD", "from-env")
    assert profile.resolved_smb_password() == "from-env"
    assert profile.backend_options()["password"] == "from-env"


# -- CLI -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("1024", 1024), ("4K", 4096), ("48M", 48 * 1024**2), ("1G", 1024**3), ("4MiB", 4 * 1024**2)],
)
def test_parse_size(text, expected):
    assert _parse_size(text) == expected


def test_parse_size_rejects_nonsense():
    with pytest.raises(PhonevaultError, match="cannot read"):
        _parse_size("big")


def test_repository_flag_works_before_and_after_the_subcommand():
    parser = build_parser()
    assert parser.parse_args(["-r", "X", "snapshots"]).repository == "X"
    assert parser.parse_args(["snapshots", "-r", "X"]).repository == "X"
    # Omitting it after the subcommand must not wipe the global one.
    assert parser.parse_args(["-r", "X", "backup"]).repository == "X"


def test_cli_reports_errors_without_a_traceback(tmp_path, capsys):
    code = main(["--config", str(tmp_path / "c.json"), "-r", str(tmp_path / "nope"), "snapshots"])
    assert code == 1
    assert "phonevault:" in capsys.readouterr().err


def test_cli_init_and_backup_and_restore(tmp_path, capsys):
    source = tmp_path / "phone"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    config = tmp_path / "c.json"
    repo = tmp_path / "nas"

    assert main(["--config", str(config), "init", "-r", str(repo), "--save"]) == 0
    saved = json.loads(config.read_text())
    assert saved["profiles"]["phone"]["repository"] == str(repo)

    assert main(["--config", str(config), "config", "set", "source", "local"]) == 0
    assert main(["--config", str(config), "config", "set", "roots", str(source)]) == 0
    assert main(["--config", str(config), "-q", "backup"]) == 0
    assert "Snapshot" in capsys.readouterr().out

    assert main(["--config", str(config), "snapshots"]) == 0
    assert "1 snapshot" in capsys.readouterr().out

    out = tmp_path / "out"
    assert main(["--config", str(config), "-q", "restore", "latest", str(out)]) == 0
    assert next(out.rglob("a.txt")).read_bytes() == b"hello"

    assert main(["--config", str(config), "check"]) == 0
    assert "healthy" in capsys.readouterr().out


def test_cli_refuses_to_store_a_password_in_the_clear(tmp_path, capsys):
    config = tmp_path / "c.json"
    code = main(["--config", str(config), "config", "set", "smb_password", "hunter2"])
    assert code == 1
    assert "plain text" in capsys.readouterr().err


def test_cli_repo_show(tmp_path, capsys):
    config = tmp_path / "c.json"
    main(["--config", str(config), "init", "-r", str(tmp_path / "nas"), "--save"])
    capsys.readouterr()
    assert main(["--config", str(config), "repo", "show"]) == 0
    out = capsys.readouterr().out
    assert "compression" in out and "pack size" in out


# -- scheduler -------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,kind",
    [
        ("hourly", "hourly"),
        ("daily@02:30", "daily"),
        ("weekly@sun:03:00", "weekly"),
        ("monthly@1:04:00", "monthly"),
    ],
)
def test_parse_schedule(spec, kind):
    assert parse_schedule(spec)["kind"] == kind
    assert describe_schedule(spec)


@pytest.mark.parametrize(
    "spec", ["", "yearly@x", "daily@99:00", "weekly@funday:01:00", "monthly@31:04:00"]
)
def test_parse_schedule_rejects_nonsense(spec):
    with pytest.raises(PhonevaultError):
        parse_schedule(spec)


def test_build_command_includes_the_config(tmp_path):
    command = build_command("phone", tmp_path / "c.json", forget=True)
    assert "--profile phone" in command
    assert "backup" in command and "--forget" in command
    assert str(tmp_path / "c.json") in command


# -- adb plumbing ----------------------------------------------------------


def _stat_line(mode: str, size: str, path: str) -> str:
    return SEP.join([mode, size, "1690000000", "1000", "1000", path])


@pytest.mark.parametrize(
    "mode,kind",
    [("81a4", "f"), ("41ed", "d"), ("a1ff", "l")],
)
def test_parse_stat_line_kinds(mode, kind):
    entry = _parse_stat_line(_stat_line(mode, "2048", "/sdcard/x"))
    assert entry is not None
    assert entry.kind == kind


def test_parse_stat_line_keeps_awkward_filenames():
    path = "/sdcard/it's a |pipe| and \"quotes\".jpg"
    entry = _parse_stat_line(_stat_line("81a4", "10", path))
    assert entry is not None and entry.path == path


@pytest.mark.parametrize(
    "line",
    ["garbage", _stat_line("81a4", "not-a-number", "/x")],
)
def test_parse_stat_line_rejects_the_unusable(line):
    assert _parse_stat_line(line) is None


@pytest.mark.parametrize(
    "mode,what",
    [("c1b0", "socket"), ("11b0", "fifo"), ("21b0", "character device"), ("61b0", "block device")],
)
def test_special_files_are_skipped_but_still_counted(mode, what):
    """Skipping must be distinguishable from a listing that broke.

    find counts every path; the parser drops kernel objects. If the
    completeness check compares those two numbers directly, a directory full
    of sockets looks permanently truncated.
    """
    from phonevault.sources.adb import SKIPPED_SPECIAL

    assert _parse_stat_line(_stat_line(mode, "0", f"/data/vendor/x.{what}")) is SKIPPED_SPECIAL


def test_sh_quote_survives_single_quotes():
    assert _sh_quote("/sdcard/it's here") == "'/sdcard/it'\\''s here'"


@pytest.mark.parametrize(
    "path,ok",
    [
        ("/sdcard/DCIM", True),
        ("/data/data", True),
        ("/sdcard/Git-backup", True),
        ("C:/Users/x", False),
        ("C:/Program Files/Git/sdcard/DCIM", False),
        ("/mingw64/sdcard", False),
    ],
)
def test_host_paths_are_refused(path, ok):
    if ok:
        reject_host_path(path)
    else:
        with pytest.raises(PhonevaultError, match="path on this computer"):
            reject_host_path(path)


def test_batching_respects_the_budget():
    files = [SourceFile(f"/f{i}", size=30_000_000) for i in range(6)]
    assert [len(g) for g in _group_by_budget(files, 64_000_000, 400)] == [2, 2, 2]


def test_a_huge_file_gets_its_own_batch():
    files = [
        SourceFile("/a", size=10),
        SourceFile("/big", size=10**10),
        SourceFile("/b", size=10),
    ]
    assert [len(g) for g in _group_by_budget(files, 64_000_000, 400)] == [1, 1, 1]


def test_file_count_limit_is_honoured():
    files = [SourceFile(f"/f{i}", size=1) for i in range(10)]
    assert [len(g) for g in _group_by_budget(files, 10**9, 3)] == [3, 3, 3, 1]


@pytest.mark.parametrize(
    "message,lost",
    [
        ("adb: device not found", True),
        ("error: closed", True),
        ("device offline", True),
        ("cat: /data/x: Permission denied", False),
        ("No such file or directory", False),
        ("", False),
    ],
)
def test_lost_device_detection(message, lost):
    assert looks_like_lost_device(message) is lost


# -- symlinked roots -------------------------------------------------------
#
# /sdcard is a symlink to /storage/self/primary on every modern Android.
# A plain `find /sdcard` reports the link and descends into nothing, so the
# backup "succeeds" having stored zero files. That is the worst failure a
# backup tool can have, so the traversal and the fallback are both pinned
# down here.


class _FakeDevice:
    """Stands in for AdbDevice, replaying canned `find` output."""

    def __init__(self, responses: dict[str, str], has_root: bool = True) -> None:
        self.responses = responses
        self.has_root = has_root
        self.scripts: list[str] = []

    def popen(self, script: str, **_kw):
        self.scripts.append(script)
        key = "-H" if "find -H " in script else "plain"
        return _FakeProc(self.responses.get(key, ""))

    def shell(self, script, **_kw):
        return ""

    def describe(self) -> str:
        return "Fake Phone"


class _FakeProc:
    """Real file objects, so the production reaping code runs unmodified."""

    def __init__(self, text: str) -> None:
        body = "".join(line + "\n" for line in text.splitlines() if line)
        self.stdout = io.BytesIO(body.encode())
        self.stderr = io.BytesIO(b"")

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _row(mode: str, path: str, size: str = "100") -> str:
    return SEP.join([mode, size, "1690000000", "0", "0", path])


def _adb_source(responses, has_root=True):
    from phonevault.sources.adb import AdbSource

    source = AdbSource.__new__(AdbSource)  # bypass __init__, which needs a real device
    source.device = _FakeDevice(responses, has_root)
    source.batch_read = False
    source.batch_bytes = 1
    source.batch_files = 1
    source.errors = []
    source._tar_broken = False
    return source


def test_find_uses_H_so_a_symlinked_root_is_followed():
    from phonevault.sources.base import Matcher

    followed = "\n".join(
        [_row("41ed", "/sdcard"), _row("41ed", "/sdcard/DCIM"), _row("81a4", "/sdcard/DCIM/a.jpg")]
    )
    source = _adb_source({"-H": followed, "plain": _row("a1ff", "/sdcard")})
    entries = list(source.walk(["/sdcard"], Matcher()))

    assert "find -H " in source.device.scripts[0], "the first attempt must follow the root symlink"
    assert [e.path for e in entries if e.is_file] == ["/sdcard/DCIM/a.jpg"]
    assert source.errors == []


def test_a_root_that_only_yields_its_own_symlink_is_reported():
    """If -H is unsupported and find still will not descend, say so loudly."""
    from phonevault.sources.base import Matcher

    source = _adb_source({"-H": _row("a1ff", "/sdcard"), "plain": _row("a1ff", "/sdcard")})
    list(source.walk(["/sdcard"], Matcher()))
    assert any("symlink" in note for note in source.errors), source.errors


def test_an_unreadable_root_is_reported_and_mentions_root_access():
    from phonevault.sources.base import Matcher

    source = _adb_source({"-H": "", "plain": ""}, has_root=False)
    assert list(source.walk(["/data/data"], Matcher())) == []
    assert any("nothing readable" in note for note in source.errors)
    assert any("root access" in note for note in source.errors)


def test_falls_back_when_the_H_attempt_finds_nothing():
    from phonevault.sources.base import Matcher

    plain = "\n".join([_row("41ed", "/data/x"), _row("81a4", "/data/x/f.bin")])
    source = _adb_source({"-H": "", "plain": plain})
    entries = list(source.walk(["/data/x"], Matcher()))
    assert [e.path for e in entries if e.is_file] == ["/data/x/f.bin"]
    # It tries both listing strategies before giving up on -H, so the exact
    # attempt count is not the point -- that it reached a non-H form is.
    assert len(source.device.scripts) > 1, "it should have retried"
    assert any("find -H " not in s for s in source.device.scripts), "should have dropped -H"


# -- command-line length ---------------------------------------------------
#
# Windows CreateProcess caps a command line at 32767 characters and reports
# anything longer as WinError 206, "the filename or extension is too long".
# Staging a 400-path tar file list by echoing base64 blew straight past that,
# which silently demoted every backup to one adb process per file -- 155 ms
# each instead of 9 ms. These pin the limit and the chunking.


def test_wrap_refuses_an_over_long_command():
    from phonevault.sources.adb import MAX_COMMAND_CHARS, AdbDevice

    device = AdbDevice.__new__(AdbDevice)
    # Comfortably inside the limit is fine.
    assert device._wrap("echo hi", root=False)
    with pytest.raises(PhonevaultError, match="too long"):
        device._wrap("x" * MAX_COMMAND_CHARS, root=False)


def test_wrap_limit_leaves_room_below_the_windows_cap():
    from phonevault.sources.adb import MAX_COMMAND_CHARS

    assert MAX_COMMAND_CHARS < 32767, "must stay under the CreateProcess cap"


def test_chunk_script_respects_the_budget_and_loses_nothing():
    from phonevault.sources.adb import _chunk_script

    lines = [f"printf 'line-{i}'" for i in range(5000)]
    chunks = list(_chunk_script(lines, 24000))
    assert len(chunks) > 1
    assert all(len(c) <= 24000 for c in chunks)
    assert sum(c.count("printf") for c in chunks) == len(lines)


def test_chunk_script_edge_cases():
    from phonevault.sources.adb import _chunk_script

    assert list(_chunk_script([], 100)) == []
    # A single line longer than the budget is still emitted rather than dropped.
    assert len(list(_chunk_script(["x" * 5000], 100))) == 1


def test_symlink_resolution_chunks_its_script():
    """A phone with thousands of symlinks must not build one giant command."""
    from phonevault.sources.adb import MAX_COMMAND_CHARS

    source = _adb_source({})
    calls: list[str] = []

    def record(script, **_kw):
        calls.append(script)
        return ""

    source.device.shell = record
    entries = [SourceFile(f"/sdcard/link-{i:05d}", kind="l") for i in range(3000)]
    list(source._resolve_symlinks(entries))

    assert len(calls) > 1, "one script for 3000 symlinks would exceed the limit"
    assert all(len(c) < MAX_COMMAND_CHARS for c in calls)


def test_tar_list_is_pushed_not_echoed(monkeypatch):
    """The file list must go via adb push, which has no command-line limit."""
    from phonevault.sources.adb import AdbDevice

    device = AdbDevice.__new__(AdbDevice)
    device.adb = "adb"
    device.serial = "TEST"
    device.timeout = 60
    pushed = {}

    class _Ok:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, timeout=None):
        assert args[3] == "push", args
        pushed["local"] = args[4]
        pushed["remote"] = args[5]
        with open(args[4], "rb") as fh:
            pushed["content"] = fh.read()
        return _Ok()

    device._run = fake_run
    payload = ("/sdcard/" + "p" * 200 + "\n") * 400  # ~80 KB, far over any limit
    device.push_bytes(payload.encode(), "/data/local/tmp/pv-list")

    assert pushed["remote"] == "/data/local/tmp/pv-list"
    assert pushed["content"] == payload.encode()
    assert not os.path.exists(pushed["local"]), "the staging file must be cleaned up"


# -- storage coverage ------------------------------------------------------
#
# A phone with an SD card keeps most of its data on /storage/<UUID>, not
# /sdcard. Backing up only /sdcard there misses the bulk of the device and
# still reports success. These pin the detection and the warning.


def _volume(path, used_gb, external=True):
    return {
        "path": path,
        "mount": path if path != "/sdcard" else "/storage/emulated",
        "label": "SD card (TEST)" if external else "internal storage",
        "external": external,
        "total": 500 * 1000**3,
        "used": int(used_gb * 1000**3),
        "available": 100 * 1000**3,
    }


def _source_with_volumes(volumes):
    source = _adb_source({})
    source.device.storage_volumes = lambda: volumes
    return source


def test_an_uncovered_sd_card_is_detected():
    source = _source_with_volumes([_volume("/sdcard", 16, False), _volume("/storage/ABCD", 224)])
    missed = source.uncovered_volumes(["/sdcard"])
    assert [v["path"] for v in missed] == ["/storage/ABCD"]


def test_a_covered_volume_is_not_flagged():
    source = _source_with_volumes([_volume("/sdcard", 16, False), _volume("/storage/ABCD", 224)])
    assert source.uncovered_volumes(["/sdcard", "/storage/ABCD"]) == []
    # A subdirectory of the volume counts as covering it.
    assert source.uncovered_volumes(["/sdcard", "/storage/ABCD/DCIM"]) == []


def test_backing_up_root_covers_everything():
    source = _source_with_volumes([_volume("/sdcard", 16, False), _volume("/storage/ABCD", 224)])
    assert source.uncovered_volumes(["/"]) == []


def test_an_empty_volume_is_not_worth_warning_about():
    source = _source_with_volumes([_volume("/storage/ABCD", 0.01)])
    assert source.uncovered_volumes(["/sdcard"]) == []


def test_the_mount_path_also_counts_as_coverage():
    """/sdcard and /storage/emulated are the same volume under two names."""
    source = _source_with_volumes([_volume("/sdcard", 16, False)])
    assert source.uncovered_volumes(["/storage/emulated"]) == []


def test_backup_warns_about_an_uncovered_volume(tmp_path):
    """The warning must reach the user, with the fix spelled out."""
    from phonevault.backends import open_backend
    from phonevault.backup import BackupRunner
    from phonevault.progress import Progress
    from phonevault.repo import Repository
    from phonevault.sources.local import LocalSource

    class Recorder(Progress):
        def __init__(self):
            self.warnings = []

        def warn(self, text):
            self.warnings.append(text)

    class SourceWithSdCard(LocalSource):
        def uncovered_volumes(self, roots, **_kw):
            return [_volume("/storage/ABCD", 224)]

    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "a.txt").write_bytes(b"hi")

    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")
    progress = Recorder()
    BackupRunner(repo, SourceWithSdCard(), progress).run([str(source_dir)])

    assert progress.warnings, "an uncovered 224 GB volume must produce a warning"
    warning = progress.warnings[0]
    assert "/storage/ABCD" in warning
    assert "NOT include" in warning
    assert "config set roots" in warning, "the warning should say how to fix it"
    repo.close()


def test_a_broken_volume_probe_never_breaks_the_backup(tmp_path):
    from phonevault.backends import open_backend
    from phonevault.backup import BackupRunner
    from phonevault.repo import Repository
    from phonevault.sources.local import LocalSource

    class ExplodingSource(LocalSource):
        def uncovered_volumes(self, roots, **_kw):
            raise RuntimeError("df fell over")

    source_dir = tmp_path / "phone"
    source_dir.mkdir()
    (source_dir / "a.txt").write_bytes(b"hi")
    repo = Repository.init(open_backend(str(tmp_path / "nas")), cache_root=tmp_path / "cache")
    snapshot = BackupRunner(repo, ExplodingSource()).run([str(source_dir)])
    assert snapshot.stats["files_total"] == 1  # diagnostics must not be fatal
    repo.close()


# -- truncated listings ----------------------------------------------------
#
# Android's toybox `find` runs only the FIRST `-exec ... {} +` batch and then
# stops. On /data/data that returned 2,319 of 517,586 paths -- a backup that
# reported success while missing 99.5% of app data. It went unnoticed because
# small directories fit in one batch, so every small-scale test passed.
#
# The listing now uses `-print0 | xargs -0 stat`, and cross-checks the result
# against an independent path count so a truncation can never again look like
# a complete backup.


class _CountingFakeDevice(_FakeDevice):
    """Fake device that reports a path count and can truncate a strategy."""

    def __init__(self, *, count: int, per_strategy: dict[str, int], has_root: bool = True):
        super().__init__({}, has_root)
        self.count = count
        self.per_strategy = per_strategy
        self.shell_calls: list[str] = []

    def shell(self, script, **_kw):
        self.shell_calls.append(script)
        if "wc -l" in script:
            return f"{self.count}\n"
        return ""

    def popen(self, script: str, **_kw):
        self.scripts.append(script)
        kind = "xargs" if "xargs" in script else "exec-one"
        rows = "\n".join(
            _row("81a4", f"/data/data/app/f{i:06d}") for i in range(self.per_strategy.get(kind, 0))
        )
        return _FakeProc(rows)


def _counting_source(**kwargs):
    from phonevault.sources.adb import AdbSource

    source = AdbSource.__new__(AdbSource)
    source.device = _CountingFakeDevice(**kwargs)
    source.batch_read = False
    source.batch_bytes = 1
    source.batch_files = 1
    source.errors = []
    source._tar_broken = False
    source._batch_disabled = False
    return source


def test_listing_uses_xargs_not_exec_plus():
    """The broken form must not be what we reach for first."""
    source = _counting_source(count=1000, per_strategy={"xargs": 1000})
    source._list_root("/data/data")
    first = source.device.scripts[0]
    assert "xargs -0 stat" in first
    assert "{} +" not in first, "the batching form that truncates must not be used"


def test_a_truncated_listing_falls_back_to_the_other_strategy():
    # xargs returns a fraction; exec-one returns everything.
    source = _counting_source(count=10000, per_strategy={"xargs": 2300, "exec-one": 10000})
    entries = source._list_root("/data/data")
    assert len(entries) == 10000
    assert any("2300 of 10000" in note for note in source.errors), source.errors


def test_an_irrecoverably_truncated_listing_is_reported_as_incomplete():
    """If every strategy truncates, say so in the loudest terms available."""
    source = _counting_source(count=500000, per_strategy={"xargs": 2319, "exec-one": 2319})
    entries = source._list_root("/data/data")
    assert len(entries) == 2319, "it still returns what it managed to read"
    assert any("INCOMPLETE" in note for note in source.errors), source.errors


def test_a_complete_listing_produces_no_warning():
    source = _counting_source(count=1000, per_strategy={"xargs": 1000})
    source._list_root("/data/data")
    assert source.errors == []


def test_a_small_shortfall_is_tolerated():
    """Files really do vanish mid-scan on a live phone."""
    source = _counting_source(count=1000, per_strategy={"xargs": 995})
    source._list_root("/data/data")
    assert source.errors == [], "a 0.5% shortfall must not cry wolf"


def test_listing_stops_at_the_first_complete_strategy():
    source = _counting_source(count=1000, per_strategy={"xargs": 1000, "exec-one": 1000})
    source._list_root("/data/data")
    assert len(source.device.scripts) == 1, "no reason to try a second strategy"


def test_path_count_failure_does_not_block_the_listing():
    """If the cross-check cannot run, use the listing rather than nothing."""
    source = _counting_source(count=-1, per_strategy={"xargs": 4049})
    source.device.shell = lambda script, **kw: "not-a-number"
    entries = source._list_root("/sdcard")
    assert len(entries) == 4049


# -- short reads -----------------------------------------------------------
#
# A live phone rewrites log and crashlytics files constantly, so the size
# stat reported a moment ago is routinely not the size that comes back.
# Treating that as a lost device triggered eight pointless reconnect cycles
# in one run. `cat` exiting 0 means it reached end of file, so the read is
# complete whatever the byte count says.


def _reader(read_bytes: int, expected: int, exit_code: int, stderr: bytes = b""):
    import io

    from phonevault.sources.adb import _ProcessReader

    class _P:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * read_bytes)
            self.stderr = io.BytesIO(stderr)

        def wait(self, timeout=None):
            return exit_code

        def kill(self):
            pass

    reader = _ProcessReader(_P(), "/data/data/app/live.log", expected)
    reader.read()
    return reader


def test_a_file_that_grew_is_not_a_lost_device():
    """'expected 0 bytes, got 201' -- a log file being written right now."""
    reader = _reader(read_bytes=201, expected=0, exit_code=0)
    reader.close()  # must not raise
    assert reader.size_changed


def test_a_file_that_shrank_is_not_a_lost_device():
    reader = _reader(read_bytes=178, expected=207, exit_code=0)
    reader.close()
    assert reader.size_changed


def test_an_exact_read_is_not_flagged_as_changed():
    reader = _reader(read_bytes=500, expected=500, exit_code=0)
    reader.close()
    assert not reader.size_changed


def test_a_cut_off_transfer_is_still_a_lost_device():
    """Partial data plus a non-zero exit is a genuine truncation."""
    from phonevault.errors import DeviceLostError

    reader = _reader(read_bytes=100, expected=5000, exit_code=1)
    with pytest.raises(DeviceLostError, match="cut off"):
        reader.close()


def test_an_unreadable_file_is_a_plain_error_not_a_lost_device():
    from phonevault.errors import AdbError, DeviceLostError

    reader = _reader(read_bytes=0, expected=10, exit_code=1, stderr=b"Permission denied")
    with pytest.raises(AdbError) as caught:
        reader.close()
    assert not isinstance(caught.value, DeviceLostError)


def test_transport_errors_are_still_detected():
    from phonevault.errors import DeviceLostError

    reader = _reader(read_bytes=0, expected=10, exit_code=1, stderr=b"adb: device not found")
    with pytest.raises(DeviceLostError):
        reader.close()


def test_config_refuses_a_root_that_msys_rewrote():
    """A mangled root must be caught when it is stored, not only when used.

    Git Bash turns `config set roots /sdcard` into a Windows path. Accepting
    it leaves a config that looks plausible and backs up an empty directory.
    """
    profile = Profile(name="phone", source="adb")
    with pytest.raises(PhonevaultError):
        apply_setting(profile, "roots", "C:/Program Files/Git/sdcard,/data/data")
    assert profile.roots == ["/sdcard"], "the bad value must not be partially applied"


def test_config_reports_an_already_stored_host_path():
    profile = Profile(name="phone", source="adb", repository="//nas/r",
                      roots=["C:/Git/sdcard"])
    assert any("not on the phone" in p for p in profile.validate())


def test_local_profiles_may_use_windows_paths():
    profile = Profile(name="pc", source="local")
    apply_setting(profile, "roots", r"C:/Users/me/Documents")
    assert profile.roots == ["C:/Users/me/Documents"]


def test_everything_preset_does_not_walk_a_bind_mount_twice():
    """/data/media is /sdcard and /data/user is /data/data, verified on device.

    Listing plain /data alongside /sdcard walks a quarter of a million paths
    twice. Deduplication makes it harmless to the stored result, but it
    doubles the slowest part of a backup for nothing.
    """
    roots = ROOT_PRESETS["everything"]
    assert "/data" not in roots
    assert "/data/media" not in roots, "same tree as /sdcard"
    assert "/data/user" not in roots, "same tree as /data/data"
    assert "/sdcard" in roots and "/data/data" in roots


def test_everything_preset_skips_what_the_phone_rebuilds():
    regenerated = ("/data/dalvik-cache", "/data/apex", "/data/preloads",
                   "/data/rollback", "/data/tombstones")
    assert not set(ROOT_PRESETS["everything"]) & set(regenerated)


# -- partition images --------------------------------------------------------


class _FakePartitionDevice:
    """Just enough AdbDevice to exercise the partition path without a phone."""

    has_root = True

    def __init__(self, table=None, contents=None):
        self.table = table if table is not None else [
            {"name": "TA", "device": "/dev/block/sda1", "size": 8},
            {"name": "persist", "device": "/dev/block/sda3", "size": 4},
        ]
        self.contents = contents or {}
        self.asked_for = []

    def describe(self):
        return "Fake (Android 13) [X]"

    def list_partitions(self, names=None):
        self.asked_for.append(names)
        if names is None:
            return list(self.table)
        return [p for p in self.table if p["name"] in names]


def _partition_source(device):
    from phonevault.sources.adb import AdbSource

    source = AdbSource.__new__(AdbSource)
    source.device = device
    source.errors = []
    source._partitions = {}
    return source


def test_partitions_are_listed_as_image_files():
    from phonevault.sources.adb import PARTITION_ROOT
    from phonevault.sources.base import Matcher

    source = _partition_source(_FakePartitionDevice())
    found = list(source._walk_partitions(Matcher(use_defaults=False)))

    files = {f.path: f for f in found if f.is_file}
    assert set(files) == {f"{PARTITION_ROOT}/TA.img", f"{PARTITION_ROOT}/persist.img"}
    assert files[f"{PARTITION_ROOT}/TA.img"].size == 8
    assert any(f.is_dir and f.path == PARTITION_ROOT for f in found)


def test_partition_images_carry_no_mtime():
    """A block device has no mtime; inventing one would defeat deduplication."""
    from phonevault.sources.base import Matcher

    source = _partition_source(_FakePartitionDevice())
    files = [f for f in source._walk_partitions(Matcher(use_defaults=False)) if f.is_file]
    assert files and all(f.mtime == 0.0 for f in files)


def test_partitions_without_root_are_reported_not_silently_skipped():
    from phonevault.sources.base import Matcher

    device = _FakePartitionDevice()
    device.has_root = False
    source = _partition_source(device)

    assert list(source._walk_partitions(Matcher(use_defaults=False))) == []
    assert any("needs root" in note for note in source.errors)


def test_a_device_without_these_partitions_says_so():
    from phonevault.sources.base import Matcher

    source = _partition_source(_FakePartitionDevice(table=[]))
    assert list(source._walk_partitions(Matcher(use_defaults=False))) == []
    assert any("no partitions" in note for note in source.errors)


def test_everything_preset_includes_partition_images():
    from phonevault.sources.adb import PARTITION_ROOT

    assert PARTITION_ROOT in ROOT_PRESETS["everything"]


def test_partition_names_cover_the_vendors_we_care_about():
    from phonevault.sources.adb import DEVICE_UNIQUE_PARTITIONS

    # Qualcomm modem identity, Sony trim area, sensor calibration.
    for name in ("modemst1", "modemst2", "fsg", "TA", "persist"):
        assert name in DEVICE_UNIQUE_PARTITIONS
    # Things a firmware image restores byte for byte must NOT be here.
    for name in ("boot", "system", "vendor", "product", "userdata", "metadata"):
        assert name not in DEVICE_UNIQUE_PARTITIONS


def test_restoring_a_partition_image_onto_the_phone_is_refused():
    """Copying an image to /sdcard would look like it worked and restore nothing."""
    from phonevault.errors import RestoreError
    from phonevault.restore import AdbTarget
    from phonevault.sources.adb import PARTITION_ROOT

    target = AdbTarget(_FakePartitionDevice())
    with pytest.raises(RestoreError, match="fastboot"):
        target.map_path(f"{PARTITION_ROOT}/TA.img")

    # ordinary paths are unaffected
    assert target.map_path("/sdcard/DCIM/IMG_0001.jpg").endswith("/sdcard/DCIM/IMG_0001.jpg")


class _FakeSlotDevice(_FakePartitionDevice):
    """An A/B phone where some names exist both bare and slot-suffixed."""

    def __init__(self, slot="_a"):
        super().__init__(table=[
            {"name": "super", "device": "/dev/block/sda99", "size": 100},
            {"name": "boot", "device": "/dev/block/sda10", "size": 20},
            {"name": "boot_a", "device": "/dev/block/sda10", "size": 20},
            {"name": "boot_b", "device": "/dev/block/sda11", "size": 20},
        ])
        self.slot = slot

    def active_slot(self):
        return self.slot

    def firmware_partitions(self):
        from phonevault.sources.adb import AdbDevice

        return AdbDevice.firmware_partitions(self)

    def prop(self, name, default=""):
        return self.slot if name == "ro.boot.slot_suffix" else default


def test_firmware_images_only_the_slot_that_is_booted():
    from phonevault.sources.adb import FIRMWARE_ROOT
    from phonevault.sources.base import Matcher

    source = _partition_source(_FakeSlotDevice(slot="_a"))
    files = [f for f in source._walk_firmware(Matcher(use_defaults=False)) if f.is_file]
    names = {f.path for f in files}

    assert f"{FIRMWARE_ROOT}/boot_a.img" in names
    assert f"{FIRMWARE_ROOT}/boot_b.img" not in names, "the other slot is not what you booted"
    assert f"{FIRMWARE_ROOT}/super.img" in names, "super has no slot suffix"


def test_a_partition_reachable_under_two_names_is_imaged_once():
    """boot and boot_a resolve to the same block device on this phone."""
    from phonevault.sources.base import Matcher

    source = _partition_source(_FakeSlotDevice(slot="_a"))
    files = [f for f in source._walk_firmware(Matcher(use_defaults=False)) if f.is_file]
    assert len(files) == 2, [f.path for f in files]


def test_firmware_is_not_in_any_preset():
    """15 GB per run is opt-in; it is for the night before a flash."""
    from phonevault.sources.adb import FIRMWARE_ROOT

    for name, roots in ROOT_PRESETS.items():
        assert FIRMWARE_ROOT not in roots, f"preset {name} would re-read 15 GB every night"


def test_restoring_a_firmware_image_onto_the_phone_is_refused():
    from phonevault.errors import RestoreError
    from phonevault.restore import AdbTarget
    from phonevault.sources.adb import FIRMWARE_ROOT

    target = AdbTarget(_FakeSlotDevice())
    with pytest.raises(RestoreError, match="fastboot"):
        target.map_path(f"{FIRMWARE_ROOT}/super.img")


def test_a_directory_of_sockets_is_not_called_incomplete(monkeypatch):
    """The real case: /data/vendor holds 979 sockets and 6 real files.

    Comparing parsed entries against find's raw path count made a complete
    listing report "927 of 1906 paths -- INCOMPLETE" on every single run. A
    completeness guard that cries wolf trains you to ignore it, which costs
    more than not having one.
    """
    from phonevault.sources.adb import AdbSource

    source = AdbSource.__new__(AdbSource)
    source.errors = []

    real = [_parse_stat_line(_stat_line("81a4", "100", f"/data/vendor/f{i}")) for i in range(6)]
    dirs = [_parse_stat_line(_stat_line("41ed", "0", "/data/vendor"))]
    entries = [e for e in real + dirs if e is not None]

    monkeypatch.setattr(AdbSource, "_count_paths", lambda self, root, follow: 986)
    monkeypatch.setattr(
        AdbSource, "_find_script", lambda self, root, follow, strategy="xargs": "script"
    )
    # 979 sockets skipped + 7 usable entries == the 986 find saw.
    monkeypatch.setattr(AdbSource, "_run_find", lambda self, script: (entries, 979, ""))

    result = source._list_root("/data/vendor")

    assert result == entries
    assert not any("INCOMPLETE" in note for note in source.errors), source.errors


def test_a_genuinely_truncated_listing_is_still_reported(monkeypatch):
    """The guard must keep working for what it was built to catch."""
    from phonevault.sources.adb import AdbSource

    source = AdbSource.__new__(AdbSource)
    source.errors = []
    entries = [_parse_stat_line(_stat_line("81a4", "100", f"/data/data/f{i}")) for i in range(20)]
    entries = [e for e in entries if e is not None]

    monkeypatch.setattr(AdbSource, "_count_paths", lambda self, root, follow: 5000)
    monkeypatch.setattr(
        AdbSource, "_find_script", lambda self, root, follow, strategy="xargs": "script"
    )
    monkeypatch.setattr(AdbSource, "_run_find", lambda self, script: (entries, 0, ""))

    source._list_root("/data/data")
    assert any("INCOMPLETE" in note for note in source.errors), source.errors


def test_partition_images_are_always_re_read():
    """A fixed mtime would make an image look unchanged for ever.

    Block devices have no modification time, so these entries carry a
    constant one. Without an explicit override the size-plus-mtime check
    matches on every later run, and the first image captured would be kept
    permanently while the partition moved underneath it -- persist, which is
    a mounted filesystem, demonstrably changes within minutes.
    """
    from phonevault.sources.adb import PARTITION_ROOT
    from phonevault.sources.base import Matcher

    source = _partition_source(_FakePartitionDevice())
    files = [f for f in source._walk_partitions(Matcher(use_defaults=False)) if f.is_file]
    assert files
    assert all(f.always_read for f in files), [f.path for f in files if not f.always_read]
    assert all(f.path.startswith(PARTITION_ROOT) for f in files)


def test_firmware_images_are_always_re_read():
    from phonevault.sources.base import Matcher

    source = _partition_source(_FakeSlotDevice())
    files = [f for f in source._walk_firmware(Matcher(use_defaults=False)) if f.is_file]
    assert files
    assert all(f.always_read for f in files)


def test_ordinary_files_are_not_always_re_read():
    """The override must not leak into the normal path and kill incrementals."""
    entry = _parse_stat_line(_stat_line("81a4", "2048", "/sdcard/DCIM/IMG_0001.jpg"))
    assert entry is not None
    assert not entry.always_read


# -- a truncated partition image must never be stored silently ---------------


class _TruncatedProc:
    """A finished adb subprocess that produced fewer bytes than promised."""

    def __init__(self, data: bytes, returncode: int = 0):
        import io

        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def _read_all(reader):
    while reader.read(65536):
        pass
    reader.close()


def test_a_short_read_of_a_partition_is_an_error():
    """This is what produced a 1.8 GiB image of a 13.5 GiB partition.

    cat exited 0, so the short read was filed as "the file changed size",
    which is routine for a log on a live phone and meaningless for a block
    device. The run then reported zero unreadable files.
    """
    from phonevault.errors import DeviceLostError
    from phonevault.sources.adb import _ProcessReader

    reader = _ProcessReader(
        _TruncatedProc(b"x" * 1000), "/partitions/super.img", 10_000, exact=True
    )
    with pytest.raises(DeviceLostError, match="fixed-size"):
        _read_all(reader)


def test_an_empty_read_of_a_partition_is_an_error():
    """vbmeta_a came back as zero bytes and was stored as an empty image."""
    from phonevault.errors import DeviceLostError
    from phonevault.sources.adb import _ProcessReader

    reader = _ProcessReader(_TruncatedProc(b""), "/partitions/vbmeta_a.img", 65_536, exact=True)
    with pytest.raises(DeviceLostError):
        _read_all(reader)


def test_a_complete_read_of_a_partition_is_accepted():
    from phonevault.sources.adb import _ProcessReader

    reader = _ProcessReader(_TruncatedProc(b"x" * 10_000), "/partitions/TA.img", 10_000, exact=True)
    _read_all(reader)
    assert not reader.size_changed


def test_an_ordinary_file_may_still_change_size():
    """The strictness must not leak: live logs legitimately shrink mid-read."""
    from phonevault.sources.adb import _ProcessReader

    reader = _ProcessReader(_TruncatedProc(b"x" * 1000), "/data/data/app/log.txt", 10_000)
    _read_all(reader)  # must not raise
    assert reader.size_changed
