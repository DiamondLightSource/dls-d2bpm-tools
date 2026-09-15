"""Smoke tests for the GUI, run offscreen so they work headless."""

import os
import stat
import sys
import time
import urllib.error
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from .conftest import QT_IMPORT_ERROR

# Every import below reaches Qt, so skip the whole module where its system
# libraries are missing rather than failing collection for the other tests.
if QT_IMPORT_ERROR:  # pragma: no cover - depends on the host's Qt libraries
    pytest.skip(f"Qt is unavailable: {QT_IMPORT_ERROR}", allow_module_level=True)

from PySide6.QtCore import qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dls_d2bpm_tools.firmware import TARGET, Device, FirmwareSource  # noqa: E402
from dls_d2bpm_tools.flash_gui import (  # noqa: E402
    FILESYSTEM_SOURCE,
    GITLAB_SOURCE,
    FlashWindow,
)
from dls_d2bpm_tools.sources import LocalSource  # noqa: E402

from .conftest import make_release  # noqa: E402
from .test_sources import FakeGitLab  # noqa: E402


def local_window(base: Path) -> FlashWindow:
    """A window backed only by the filesystem, for tests that don't want HTTP."""
    sources: dict[str, FirmwareSource] = {FILESYSTEM_SOURCE: LocalSource(base)}
    return FlashWindow(sources)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "firmware"
    make_release(root, "1.0.0", Device.D2AFE.subdir)
    make_release(root, "1.1.0", Device.D2PTD.subdir)
    for name in ("1.0.0", "1.1.0"):
        stamp = time.time()
        os.utime(root / name, (stamp, stamp))
    return root


def test_window_builds_command(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    assert win.device is Device.D2AFE
    assert win.cb_release.currentText() == "1.0.0"
    cmd = win.current_command()
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("cli.py")
    assert "--via-ptg" not in cmd
    assert TARGET in cmd
    assert win.btn_run.isEnabled()
    assert "cli.py" in win.lbl_command.text()


def test_switching_device_refreshes_releases(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    win.rb_ptd.setChecked(True)
    assert win.cb_release.currentText() == "1.1.0"
    assert win.device is Device.D2PTD


def test_via_ptg_reaches_command(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    win.cb_via.setChecked(True)
    assert "--via-ptg" in win.lbl_command.text()


def test_no_releases_disables_run(qapp: QApplication, tmp_path: Path) -> None:
    win = local_window(tmp_path / "empty")
    assert not win.btn_run.isEnabled()
    assert "⚠" in win.lbl_command.text()


def test_progress_parsing(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    win.handle_output("Flashing bootloader")
    win.handle_output("Sent 50 bytes of 200 bytes")
    assert win.lbl_phase.text() == "Flashing bootloader"
    assert win.progress.value() == 25
    win.handle_finished(0)
    assert win.progress.value() == 100
    assert win.lbl_status.text() == "Done"


def _make_runnable(base: Path, body: str) -> None:
    """Build a release whose 'cli' script is a real, executable program."""
    out = make_release(base, "2.0.0", Device.D2AFE.subdir, files=["app.bin"])
    script = out / "d2afe-cli.py"
    script.write_text("#!/usr/bin/env python3\nimport sys, time\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _pump(
    app: QApplication, predicate: Callable[[], bool], timeout: float = 20.0
) -> bool:
    """Spin the event loop until `predicate` holds or we give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def qt_messages() -> Iterator[list[str]]:
    """Capture Qt's own warnings, e.g. about mishandled QThreads."""
    captured: list[str] = []

    def handler(mode: object, context: object, message: str) -> None:
        captured.append(message)

    qInstallMessageHandler(handler)
    yield captured
    qInstallMessageHandler(None)


def test_repeated_flashes_release_the_worker(
    qapp: QApplication, tmp_path: Path, qt_messages: list[str]
) -> None:
    """Flashing repeatedly must settle back to idle and upset Qt not at all."""
    base = tmp_path / "fw"
    _make_runnable(base, "print('Sent 10 bytes of 10 bytes')\n")
    win = local_window(base)

    for _ in range(25):
        win.start_flash()
        assert _pump(qapp, lambda: win.worker is None), "worker was never released"
        assert win.lbl_status.text() == "Done"

    assert not [m for m in qt_messages if "QThread" in m], qt_messages
    assert win.progress.value() == 100
    assert win.btn_run.isEnabled()


def test_shutdown_stops_a_running_flash(qapp: QApplication, tmp_path: Path) -> None:
    base = tmp_path / "fw"
    _make_runnable(base, "print('started', flush=True)\ntime.sleep(60)\n")
    win = local_window(base)

    win.start_flash()
    assert win.worker is not None
    assert _pump(qapp, lambda: "started" in win.log.toPlainText())

    win.shutdown()
    assert win.worker is None


def test_shutdown_is_safe_when_idle(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    win.shutdown()
    win.shutdown()
    assert win.worker is None


def test_done_slot_keeps_the_worker_alive(qapp: QApplication, tmp_path: Path) -> None:
    """Regression: `done` must not release the worker, or Qt aborts the process.

    `done` is emitted from inside FlashWorker.run, so the thread has not
    returned yet when the slot runs. Dropping the last reference there let
    Python destroy a still-running QThread, which Qt turns into
    "QThread: Destroyed while thread is still running" and a dead process
    part way through a flash. The reference may only be released once
    QThread.finished says the thread has actually exited.
    """
    base = tmp_path / "fw"
    _make_runnable(base, "print('started', flush=True)\ntime.sleep(60)\n")
    win = local_window(base)

    win.start_flash()
    worker = win.worker
    assert worker is not None
    assert _pump(qapp, lambda: "started" in win.log.toPlainText())

    # Exactly what the thread does just before run() returns.
    win.handle_finished(0)
    assert win.worker is worker, "the worker was released while still running"
    assert worker.isRunning()

    win.shutdown()
    assert win.worker is None


# ------------------------------------------------------- firmware sources


@pytest.fixture
def both_sources(tmp_path: Path) -> dict[str, FirmwareSource]:
    local = tmp_path / "fs"
    make_release(local, "9.9.9", Device.D2AFE.subdir)
    return {
        GITLAB_SOURCE: FakeGitLab(cache_dir=tmp_path / "cache"),
        FILESYSTEM_SOURCE: LocalSource(local),
    }


def test_defaults_to_gitlab(
    qapp: QApplication, both_sources: dict[str, FirmwareSource]
) -> None:
    win = FlashWindow(both_sources)
    assert win.cb_source.currentText() == GITLAB_SOURCE
    assert win.cb_release.currentText() == "0.9.3b"
    assert "gitlab.example" in win.lbl_source.text()


def test_switching_source_relists_releases(
    qapp: QApplication, both_sources: dict[str, FirmwareSource]
) -> None:
    win = FlashWindow(both_sources)
    win.cb_source.setCurrentText(FILESYSTEM_SOURCE)
    assert win.cb_release.currentText() == "9.9.9"
    win.cb_source.setCurrentText(GITLAB_SOURCE)
    assert win.cb_release.currentText() == "0.9.3b"


def test_source_can_be_chosen_at_startup(
    qapp: QApplication, both_sources: dict[str, FirmwareSource]
) -> None:
    win = FlashWindow(both_sources, default=FILESYSTEM_SOURCE)
    assert win.cb_source.currentText() == FILESYSTEM_SOURCE


def test_preview_does_not_download(
    qapp: QApplication, both_sources: dict[str, FirmwareSource]
) -> None:
    """Rendering the command must never block the GUI thread on the network."""
    gitlab = both_sources[GITLAB_SOURCE]
    assert isinstance(gitlab, FakeGitLab)
    FlashWindow(both_sources)

    downloads = [r for r in gitlab.requests if "/artifacts/" in r]
    assert downloads == []


def test_flash_downloads_then_runs(
    qapp: QApplication, both_sources: dict[str, FirmwareSource], tmp_path: Path
) -> None:
    """The GitLab download happens in the worker, and feeds the command."""
    gitlab = both_sources[GITLAB_SOURCE]
    assert isinstance(gitlab, FakeGitLab)
    win = FlashWindow(both_sources)

    # Run a stub rather than the real programmer: no hardware is contacted.
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import sys\n"
        "fp = sys.argv[sys.argv.index('--filepath') + 1]\n"
        "print('read', len(open(fp,'rb').read()), 'bytes')\n"
    )
    win.le_script.setText(str(stub))

    win.start_flash()
    assert _pump(qapp, lambda: win.worker is None)

    log = win.log.toPlainText()
    assert "Downloading" in log
    assert f"read {len(gitlab.artifact_body)} bytes" in log
    assert win.lbl_status.text() == "Done"
    assert [r for r in gitlab.requests if "/artifacts/" in r]


def test_unreachable_source_is_survivable(
    qapp: QApplication, both_sources: dict[str, FirmwareSource]
) -> None:
    """A GitLab outage must not stop the GUI opening or offer a broken flash."""
    gitlab = both_sources[GITLAB_SOURCE]
    assert isinstance(gitlab, FakeGitLab)
    gitlab.fail = urllib.error.URLError("no route to host")

    win = FlashWindow(both_sources)
    assert win.cb_release.count() == 0
    assert not win.btn_run.isEnabled()
    assert "⚠" in win.lbl_command.text()

    # ...and the filesystem is still right there.
    win.cb_source.setCurrentText(FILESYSTEM_SOURCE)
    assert win.cb_release.currentText() == "9.9.9"
    assert win.btn_run.isEnabled()


# ------------------------------------------------- connection validation


@pytest.mark.parametrize(
    ("ip", "port", "message"),
    [
        ("", "7003", "IP address"),
        ("   ", "7003", "IP address"),
        ("172.23.241.15", "", "port"),
        ("172.23.241.15", "abc", "1 to 65535"),
        ("172.23.241.15", "0", "1 to 65535"),
        ("172.23.241.15", "70000", "1 to 65535"),
    ],
)
def test_bad_endpoint_blocks_the_flash(
    qapp: QApplication, base: Path, ip: str, port: str, message: str
) -> None:
    """Regression: an empty field used to build ':7003' and run anyway.

    The failure then surfaced deep inside d2afe-cli.py, a long way from the
    empty box that caused it.
    """
    win = local_window(base)
    win.le_ip.setText(ip)
    win.le_port.setText(port)

    with pytest.raises(ValueError, match=message):
        win.current_command()
    assert not win.btn_run.isEnabled()
    assert message in win.lbl_command.text()


def test_endpoint_is_trimmed(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    win.le_ip.setText("  172.23.241.15 ")
    win.le_port.setText(" 7003 ")
    assert "--address 172.23.241.15:7003" in " ".join(win.current_command())


def test_good_endpoint_still_runs(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    assert win.btn_run.isEnabled()
    assert "172.23.241.15:7003" in win.lbl_command.text()
