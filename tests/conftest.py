"""Shared fixtures and helpers for the test suite."""

import importlib
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

# Qt must be able to start without a display for the GUI tests to run in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _qt_import_error() -> str:
    """Why Qt cannot be imported here, or "" if it can.

    A dev box without Qt's system libraries (libGL and friends) should still be
    able to run everything that isn't the GUI, so the tests that do need it are
    skipped rather than left to fail collection for the whole suite.
    """
    try:
        importlib.import_module("PySide6.QtWidgets")
    except ImportError as e:
        return str(e)
    return ""


QT_IMPORT_ERROR = _qt_import_error()

#: Decorator for tests that cannot run without a working Qt.
requires_qt = pytest.mark.skipif(
    bool(QT_IMPORT_ERROR), reason=f"Qt is unavailable: {QT_IMPORT_ERROR}"
)


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
def qapp() -> Iterator["QApplication"]:
    """A single QApplication shared by every GUI test.

    Imported here rather than at module scope so that the tests which need no
    GUI still run where Qt's system libraries are missing.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    yield app
