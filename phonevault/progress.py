"""Progress reporting, shared by the CLI and the web UI.

Engines call these methods; what happens to the events is the front end's
business. The base class does nothing, so an engine never has to check
whether a reporter was supplied.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any


class Progress:
    """No-op reporter. Subclass and override what you care about."""

    def start(self, phase: str, total_files: int = 0, total_bytes: int = 0) -> None: ...

    def file_started(self, path: str, size: int = 0) -> None: ...

    def file_done(self, path: str, status: str, size: int = 0) -> None: ...

    def advance_bytes(self, count: int) -> None: ...

    def message(self, text: str) -> None: ...

    def warn(self, text: str) -> None: ...

    def finish(self, summary: dict[str, Any] | None = None) -> None: ...


class ConsoleProgress(Progress):
    """A single rewriting status line, plus warnings above it."""

    def __init__(self, stream=None, *, enabled: bool = True, interval: float = 0.15) -> None:
        self.stream = stream or sys.stderr
        self.enabled = enabled and self.stream.isatty()
        self.interval = interval
        self._lock = threading.Lock()
        self._last_draw = 0.0
        self._line_len = 0
        self.phase = ""
        self.total_files = 0
        self.total_bytes = 0
        self.done_files = 0
        self.done_bytes = 0
        self.current = ""
        self.started = time.monotonic()

    # -- events -----------------------------------------------------------

    def start(self, phase: str, total_files: int = 0, total_bytes: int = 0) -> None:
        self.phase = phase
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.started = time.monotonic()
        self._draw(force=True)

    def file_started(self, path: str, size: int = 0) -> None:
        self.current = path
        self._draw()

    def file_done(self, path: str, status: str, size: int = 0) -> None:
        self.done_files += 1
        self._draw()

    def advance_bytes(self, count: int) -> None:
        self.done_bytes += count
        self._draw()

    def message(self, text: str) -> None:
        self._emit(text)

    def warn(self, text: str) -> None:
        self._emit(f"warning: {text}")

    def finish(self, summary: dict[str, Any] | None = None) -> None:
        self._clear()

    # -- rendering ---------------------------------------------------------

    def _emit(self, text: str) -> None:
        with self._lock:
            self._clear_locked()
            self.stream.write(text + "\n")
            self.stream.flush()
        self._draw(force=True)

    def _clear(self) -> None:
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        if self.enabled and self._line_len:
            self.stream.write("\r" + " " * self._line_len + "\r")
            self.stream.flush()
            self._line_len = 0

    def _draw(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last_draw < self.interval:
            return
        with self._lock:
            self._last_draw = now
            line = self._compose()
            pad = max(0, self._line_len - len(line))
            self.stream.write("\r" + line + " " * pad)
            self.stream.flush()
            self._line_len = len(line)

    def _compose(self) -> str:
        parts = [self.phase]
        if self.total_files:
            parts.append(f"{self.done_files}/{self.total_files} files")
        elif self.done_files:
            parts.append(f"{self.done_files} files")
        if self.total_bytes:
            parts.append(f"{format_bytes(self.done_bytes)}/{format_bytes(self.total_bytes)}")
        elif self.done_bytes:
            parts.append(format_bytes(self.done_bytes))
        elapsed = time.monotonic() - self.started
        if elapsed > 1 and self.done_bytes:
            parts.append(f"{format_bytes(self.done_bytes / elapsed)}/s")
        if self.total_bytes and self.done_bytes and elapsed > 2:
            remaining = self.total_bytes - self.done_bytes
            rate = self.done_bytes / elapsed
            if rate > 0 and remaining > 0:
                parts.append(f"ETA {format_duration(remaining / rate)}")
        line = "  ".join(p for p in parts if p)
        if self.current:
            room = max(0, _term_width() - len(line) - 4)
            if room > 12:
                line += "  " + _shorten(self.current, room)
        return line[: _term_width() - 1]


class CallbackProgress(Progress):
    """Forwards a snapshot of engine state to a callable, for the web UI."""

    def __init__(self, callback, *, interval: float = 0.4) -> None:
        self.callback = callback
        self.interval = interval
        self._last = 0.0
        self.state: dict[str, Any] = {
            "phase": "",
            "total_files": 0,
            "total_bytes": 0,
            "done_files": 0,
            "done_bytes": 0,
            "current": "",
            "warnings": [],
            "messages": [],
        }

    def _push(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last >= self.interval:
            self._last = now
            self.callback(dict(self.state))

    def start(self, phase: str, total_files: int = 0, total_bytes: int = 0) -> None:
        self.state.update(
            phase=phase,
            total_files=total_files,
            total_bytes=total_bytes,
            done_files=0,
            done_bytes=0,
            current="",
        )
        self._push(force=True)

    def file_started(self, path: str, size: int = 0) -> None:
        self.state["current"] = path
        self._push()

    def file_done(self, path: str, status: str, size: int = 0) -> None:
        self.state["done_files"] += 1
        self._push()

    def advance_bytes(self, count: int) -> None:
        self.state["done_bytes"] += count
        self._push()

    def message(self, text: str) -> None:
        self.state["messages"].append(text)
        del self.state["messages"][:-200]
        self._push(force=True)

    def warn(self, text: str) -> None:
        self.state["warnings"].append(text)
        del self.state["warnings"][:-200]
        self._push(force=True)

    def finish(self, summary: dict[str, Any] | None = None) -> None:
        self.state["phase"] = "done"
        self.state["current"] = ""
        if summary:
            self.state["summary"] = summary
        self._push(force=True)


# -- formatting shared by every front end ---------------------------------


def format_bytes(count: float) -> str:
    step = 1024.0
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < step or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TiB"  # pragma: no cover


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _term_width() -> int:
    import shutil

    try:
        return max(40, shutil.get_terminal_size((100, 24)).columns)
    except Exception:  # pragma: no cover
        return 100


def _shorten(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 3:
        return text[-width:]
    return "..." + text[-(width - 3) :]
