"""Shared fixtures and helpers for the test suite."""

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

# Qt must be able to start without a display for the GUI tests to run in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def make_release(
    base: Path,
    name: str,
    subdir: str | None,
    files: Iterable[str] = ("fw.bin", "cli.py"),
) -> Path:
    """Create a fake firmware release tree and return the artifact directory."""
    from dls_d2bpm_tools.firmware import TARGET

    target = base / name / TARGET
    out = target / subdir if subdir else target
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        (out / f).write_text("")
    return out


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    """A single QApplication shared by every GUI test."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    yield app
