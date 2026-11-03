"""Every command printed in the documentation must actually work.

Three commands in the guide used flags the CLI has never had, including the
one for capturing the firmware images before a flash. Nothing caught it:
the docs are prose to the test suite, and the flags were plausible. A reader
following the guide got "unrecognized arguments" at the exact moment they
were trying to protect their phone.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from phonevault.cli import build_parser

DOCS = [
    Path(__file__).parent.parent / "docs" / "HOW-TO-USE.md",
    Path(__file__).parent.parent / "README.md",
]

#: Placeholders a reader is meant to substitute, not literal arguments.
PLACEHOLDER = re.compile(r"^<.+>$|^\.\.\.$")


def documented_commands() -> list[tuple[Path, str]]:
    """Every `phonevault ...` line in the docs, as the shell would see it.

    Trailing `# explanations` and shell redirections are part of how the
    examples read, not part of the command, so they come off first.
    """
    found = []
    for doc in DOCS:
        if not doc.exists():
            continue
        for line in doc.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("phonevault ") or "|" in stripped:
                continue
            stripped = stripped.split("#", 1)[0]
            stripped = re.split(r"\s[>&]", stripped, maxsplit=1)[0]
            found.append((doc, stripped.strip()))
    return found


def test_the_docs_contain_commands_to_check():
    """Guard the guard: a broken scraper would make this file vacuous."""
    assert len(documented_commands()) > 20


@pytest.mark.parametrize("doc,command", documented_commands(), ids=lambda v: str(v)[-60:])
def test_documented_command_is_real(doc: Path, command: str):
    """Hand the line to the real parser rather than matching strings.

    Matching flag names against --help text cannot tell an option from the
    value of the option before it, so `--profile tablet backup` looked like a
    command called "tablet". argparse already knows the difference.
    """
    words = [w for w in command.split()[1:] if not PLACEHOLDER.match(w)]
    if not words or "--version" in words:
        return

    parser = build_parser()
    try:
        parser.parse_args(words)
    except SystemExit as exit_code:
        # argparse has already written the reason to stderr; re-run it
        # captured so the failure says which flag was wrong.
        detail = subprocess.run(
            [sys.executable, "-m", "phonevault", *words, "--help"],
            capture_output=True, text=True,
        )
        message = (detail.stderr or detail.stdout).strip().splitlines()
        reason = message[-1] if message else f"exit {exit_code.code}"
        pytest.fail(f"{doc.name}: {command!r} does not parse -- {reason}")


def test_the_console_script_is_declared():
    """The docs say `phonevault ...`, so the entry point has to exist."""
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.scripts]" in pyproject
    assert "phonevault = " in pyproject
