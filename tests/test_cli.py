import os
import subprocess
import sys
from collections.abc import Sequence

import pytest

from dls_d2bpm_tools import __version__
from dls_d2bpm_tools.__main__ import main

from .conftest import requires_qt


def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the module entry point.

    The timeout matters: if argument forwarding breaks again, `d2afe-flash
    --help` stops printing help and opens the GUI instead, and without a
    deadline the suite would hang rather than fail.
    """
    return subprocess.run(
        [sys.executable, "-m", "dls_d2bpm_tools", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


def test_cli_version() -> None:
    cmd = [sys.executable, "-m", "dls_d2bpm_tools", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__


def test_no_command_prints_help() -> None:
    result = run([])
    assert result.returncode == 0
    assert "d2afe-flash" in result.stdout


@requires_qt
def test_gui_flags_reach_the_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the subcommand's own flags used to be thrown away.

    `d2afe-flash` forwarded a hardcoded empty list, so every GUI option was
    unreachable through `python -m dls_d2bpm_tools` and the top-level parser
    rejected flags the tool genuinely has.
    """
    seen: list[Sequence[str]] = []

    def fake_gui(args: Sequence[str] | None = None) -> int:
        seen.append(list(args or []))
        return 0

    monkeypatch.setattr("dls_d2bpm_tools.flash_gui.main", fake_gui)

    assert main(["d2afe-flash", "--source", "filesystem"]) == 0
    assert seen == [["--source", "filesystem"]]

    seen.clear()
    assert main(["d2afe-flash"]) == 0
    assert seen == [[]]


@requires_qt
def test_gui_help_is_the_gui_s_own() -> None:
    result = run(["d2afe-flash", "--help"])
    assert result.returncode == 0
    assert "--source" in result.stdout
    assert "--firmware-base" in result.stdout


def test_unknown_flag_without_a_subcommand_still_errors() -> None:
    result = run(["--bogus"])
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
