"""Running backups automatically.

On Windows this registers a Task Scheduler entry, which is the right tool:
it survives reboots, runs without a console window, and can be told to catch
up on a run that was missed while the machine was asleep -- all things a
long-lived Python daemon would have to reimplement badly.

Elsewhere it prints the systemd timer or crontab line to install, rather than
editing system files behind the user's back.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .errors import PhonevaultError

TASK_PREFIX = "Phonevault"

_WEEKDAYS = {
    "mon": "MON", "tue": "TUE", "wed": "WED", "thu": "THU",
    "fri": "FRI", "sat": "SAT", "sun": "SUN",
}


def parse_schedule(spec: str) -> dict[str, str]:
    """Parse a schedule string into its parts.

    Accepted forms::

        hourly
        daily@02:30
        weekly@sun:03:00
        monthly@1:04:00        (day of month)
    """
    spec = spec.strip().lower()
    if not spec:
        raise PhonevaultError("empty schedule")

    kind, _, rest = spec.partition("@")
    kind = kind.strip()
    rest = rest.strip()

    if kind == "hourly":
        return {"kind": "hourly", "minute": rest or "0"}

    if kind == "daily":
        return {"kind": "daily", "time": _valid_time(rest or "02:00")}

    if kind == "weekly":
        day, _, clock = rest.partition(":")
        day = day.strip()[:3] or "sun"
        if day not in _WEEKDAYS:
            raise PhonevaultError(f"unknown weekday {day!r}; use mon..sun")
        return {"kind": "weekly", "day": _WEEKDAYS[day], "time": _valid_time(clock or "03:00")}

    if kind == "monthly":
        day, _, clock = rest.partition(":")
        try:
            day_number = int(day or 1)
        except ValueError as exc:
            raise PhonevaultError(f"cannot read {day!r} as a day of the month") from exc
        if not 1 <= day_number <= 28:
            # 29-31 silently never fires in some months; refuse rather than
            # let a monthly backup quietly skip February.
            raise PhonevaultError("monthly day must be between 1 and 28")
        return {"kind": "monthly", "day": str(day_number), "time": _valid_time(clock or "04:00")}

    raise PhonevaultError(
        f"cannot read schedule {spec!r}. Examples: hourly, daily@02:30, "
        f"weekly@sun:03:00, monthly@1:04:00"
    )


def _valid_time(text: str) -> str:
    hour, _, minute = text.partition(":")
    try:
        hour_number, minute_number = int(hour), int(minute or 0)
    except ValueError as exc:
        raise PhonevaultError(f"cannot read {text!r} as a time of day (use HH:MM)") from exc
    if not (0 <= hour_number < 24 and 0 <= minute_number < 60):
        raise PhonevaultError(f"{text!r} is not a valid time of day")
    return f"{hour_number:02d}:{minute_number:02d}"


def describe_schedule(spec: str) -> str:
    parts = parse_schedule(spec)
    if parts["kind"] == "hourly":
        return f"every hour at minute {parts['minute']}"
    if parts["kind"] == "daily":
        return f"every day at {parts['time']}"
    if parts["kind"] == "weekly":
        return f"every {parts['day'].title()} at {parts['time']}"
    return f"on day {parts['day']} of each month at {parts['time']}"


def task_name(profile: str) -> str:
    return f"{TASK_PREFIX}-{profile}"


def _launcher() -> list[str]:
    """How to invoke this program from a scheduled task.

    ``pythonw.exe`` is preferred so no console window appears; it sits beside
    ``python.exe`` in every standard CPython install on Windows.
    """
    executable = Path(sys.executable)
    if os.name == "nt":
        quiet = executable.with_name("pythonw.exe")
        if quiet.exists():
            executable = quiet
    return [str(executable), "-m", "phonevault"]


def build_command(profile: str, config_path: Path | None, forget: bool) -> str:
    parts = _launcher()
    if config_path:
        parts += ["--config", str(config_path)]
    parts += ["--profile", profile, "--quiet", "backup"]
    if forget:
        parts.append("--forget")
    return subprocess.list2cmdline(parts)


def install_task(
    profile: str, spec: str, *, config_path: Path | None = None, forget: bool = True
) -> str:
    parts = parse_schedule(spec)
    if os.name != "nt":
        return _posix_instructions(profile, parts, config_path, forget)

    name = task_name(profile)
    command = build_command(profile, config_path, forget)

    args = ["schtasks", "/Create", "/F", "/TN", name, "/TR", command]
    if parts["kind"] == "hourly":
        args += ["/SC", "HOURLY", "/ST", f"00:{int(parts['minute']):02d}"]
    elif parts["kind"] == "daily":
        args += ["/SC", "DAILY", "/ST", parts["time"]]
    elif parts["kind"] == "weekly":
        args += ["/SC", "WEEKLY", "/D", parts["day"], "/ST", parts["time"]]
    else:
        args += ["/SC", "MONTHLY", "/D", parts["day"], "/ST", parts["time"]]

    result = _run(args)
    if result.returncode != 0:
        raise PhonevaultError(
            f"could not create the scheduled task: {_text(result)}\n"
            f"Creating a task usually needs an elevated prompt -- try running this "
            f"command from a terminal started with 'Run as administrator'."
        )
    note = _make_task_reliable(name)
    return (
        f"Scheduled task '{name}' created.\n"
        f"  runs: {command}\n"
        f"  note: the phone must be reachable (plugged in, or paired over Wi-Fi) "
        f"when the task fires."
        + (f"\n  warning: {note}" if note else "")
    )


#: Settings schtasks cannot express, and which its defaults get wrong for a
#: backup. Windows defaults a new task to "do not start on battery", "stop if
#: the machine goes onto battery" and "do not run a missed schedule later" --
#: sensible for a maintenance chore, wrong for the thing that protects your
#: data. A laptop left unplugged overnight simply skips its backup, and the
#: only evidence is a last-result code of 0x800710E0, which reads as "the
#: operator or administrator has refused the request" and does not mention
#: power at all.
_RELIABILITY_SCRIPT = """
$ErrorActionPreference = 'Stop'
$s = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -MultipleInstances IgnoreNew
Set-ScheduledTask -TaskName '{name}' -Settings $s | Out-Null
"""


def _make_task_reliable(name: str) -> str:
    """Relax the power conditions schtasks bakes in. Returns a warning, or ''.

    Failure here is not fatal: the task exists and will run whenever the
    machine is awake and on mains power. It is reported rather than raised so
    a scheduling command does not fail over a setting that only matters on a
    laptop.
    """
    result = _run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            _RELIABILITY_SCRIPT.format(name=name.replace("'", "''")),
        ]
    )
    if result.returncode != 0:
        return (
            "could not relax the task's power settings, so Windows will skip "
            "the backup while on battery and will not retry a missed run: "
            + _text(result).strip().splitlines()[0][:160]
        )
    return ""


def remove_task(profile: str) -> str:
    if os.name != "nt":
        return f"Remove the systemd timer or crontab entry for profile '{profile}' by hand."
    name = task_name(profile)
    result = _run(["schtasks", "/Delete", "/F", "/TN", name])
    if result.returncode != 0:
        raise PhonevaultError(f"could not remove task '{name}': {_text(result)}")
    return f"Removed scheduled task '{name}'."


def list_tasks() -> list[str]:
    if os.name != "nt":
        return ["Scheduled tasks are only managed automatically on Windows."]
    result = _run(["schtasks", "/Query", "/FO", "LIST", "/V"])
    if result.returncode != 0:
        return [f"could not query tasks: {_text(result)}"]

    lines: list[str] = []
    block: dict[str, str] = {}
    for raw in _text(result).splitlines():
        if not raw.strip():
            if block.get("TaskName", "").lstrip("\\").startswith(TASK_PREFIX):
                lines.append(
                    f"  {block.get('TaskName', '').lstrip(chr(92))}\n"
                    f"      next run : {block.get('Next Run Time', '?')}\n"
                    f"      last run : {block.get('Last Run Time', '?')}"
                    f"  ({block.get('Last Result', '?')})\n"
                    f"      status   : {block.get('Status', '?')}"
                )
            block = {}
            continue
        key, sep, value = raw.partition(":")
        if sep:
            block[key.strip()] = value.strip()
    return lines or ["No phonevault tasks are scheduled."]


def _posix_instructions(profile: str, parts: dict[str, str], config_path, forget: bool) -> str:
    command = build_command(profile, config_path, forget)
    if parts["kind"] == "hourly":
        cron = f"{int(parts['minute'])} * * * *"
    else:
        hour, minute = parts["time"].split(":")
        if parts["kind"] == "daily":
            cron = f"{int(minute)} {int(hour)} * * *"
        elif parts["kind"] == "weekly":
            weekday = list(_WEEKDAYS.values()).index(parts["day"])
            cron = f"{int(minute)} {int(hour)} * * {weekday}"
        else:
            cron = f"{int(minute)} {int(hour)} {parts['day']} * *"
    return (
        "Automatic scheduling is only wired up for Windows Task Scheduler.\n"
        "On this system, add the following crontab line instead "
        "(run 'crontab -e'):\n\n"
        f"  {cron}  {command}\n"
    )


def _run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PhonevaultError(f"could not run {args[0]}: {exc}") from exc


def _text(result: subprocess.CompletedProcess) -> str:
    return (result.stdout + result.stderr).decode("utf-8", "replace").strip()
