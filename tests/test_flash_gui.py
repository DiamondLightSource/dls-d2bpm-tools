"""Smoke tests for the GUI, run offscreen so they work headless."""

import os
import stat
import sys
import threading
import time
import urllib.error
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from .conftest import QT_IMPORT_ERROR

# Every import below reaches Qt, so skip the whole module where its system
# libraries are missing rather than failing collection for the other tests.
if QT_IMPORT_ERROR:  # pragma: no cover - depends on the host's Qt libraries
    pytest.skip(f"Qt is unavailable: {QT_IMPORT_ERROR}", allow_module_level=True)

from PySide6.QtCore import QTimer, qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dls_d2bpm_tools.console import LineEnding  # noqa: E402
from dls_d2bpm_tools.firmware import TARGET, Device, FirmwareSource, LogFn  # noqa: E402
from dls_d2bpm_tools.flash_gui import (  # noqa: E402
    FILESYSTEM_SOURCE,
    GITLAB_SOURCE,
    FlashWindow,
    default_sources,
)
from dls_d2bpm_tools.sources import GitLabSource, LocalSource  # noqa: E402

from .conftest import make_release  # noqa: E402
from .test_console import FakeServer  # noqa: E402
from .test_sources import FakeGitLab  # noqa: E402


@pytest.fixture(autouse=True)
def close_windows(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Retain windows until their threads and queued signals have finished."""
    windows: list[FlashWindow] = []
    original = FlashWindow.__init__

    def initialize(
        window: FlashWindow,
        sources: Mapping[str, FirmwareSource] | None = None,
        default: str | None = None,
    ) -> None:
        original(window, sources, default)
        windows.append(window)

    monkeypatch.setattr(FlashWindow, "__init__", initialize)
    yield
    for window in windows:
        window.shutdown()
        assert _pump(
            qapp, lambda window=window: window.console is None and window.worker is None
        )
        window.close()
        window.deleteLater()
    qapp.processEvents()


def local_window(base: Path) -> FlashWindow:
    """A window backed only by the filesystem, for tests that don't want HTTP."""
    sources: dict[str, FirmwareSource] = {FILESYSTEM_SOURCE: LocalSource(base)}
    win = FlashWindow(sources)
    wait_for_releases(win)
    return win


def wait_for_releases(win: FlashWindow) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    assert _pump(app, lambda: win.source_error != "Loading releases…")


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
    wait_for_releases(win)
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
    wait_for_releases(win)
    assert win.cb_source.currentText() == GITLAB_SOURCE
    assert win.cb_release.currentText() == "0.9.3b"
    assert "gitlab.example" in win.lbl_source.text()


def test_custom_repository_preserves_filesystem_fallback(tmp_path: Path) -> None:
    sources = default_sources(tmp_path, "https://other.example/group/firmware")
    assert sources[GITLAB_SOURCE].description == (
        "GitLab: https://other.example/group/firmware/-/releases"
    )
    local = sources[FILESYSTEM_SOURCE]
    assert isinstance(local, LocalSource)
    assert local.base == tmp_path


def test_repository_field_starts_with_configured_url(
    qapp: QApplication, tmp_path: Path
) -> None:
    win = FlashWindow(
        default_sources(tmp_path, "https://other.example/group/firmware"),
        default=FILESYSTEM_SOURCE,
    )
    assert win.le_repository.text() == "https://other.example/group/firmware"


@pytest.mark.parametrize("press_enter", [False, True])
def test_applying_repository_selects_and_loads_new_source(
    qapp: QApplication,
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    press_enter: bool,
) -> None:
    requests: list[str] = []

    def list_releases(source: GitLabSource, device: Device) -> list[str]:
        requests.append(source.api)
        return []

    monkeypatch.setattr(GitLabSource, "list_releases", list_releases)
    win = local_window(base)
    win.le_repository.setText("https://other.example/team/firmware.git")
    assert win.cb_source.currentText() == FILESYSTEM_SOURCE
    assert not requests
    if press_enter:
        win.le_repository.returnPressed.emit()
    else:
        win.btn_repository.click()
    wait_for_releases(win)
    assert requests == ["https://other.example/api/v4/projects/team%2Ffirmware"]
    assert win.cb_source.currentText() == GITLAB_SOURCE
    assert win.le_repository.text() == "https://other.example/team/firmware"
    assert "other.example/team/firmware" in win.lbl_source.text()


def test_invalid_repository_field_preserves_current_source(
    qapp: QApplication, base: Path
) -> None:
    win = local_window(base)
    source = win.source
    release = win.cb_release.currentText()
    win.le_repository.setText("not-a-url")
    win.btn_repository.click()
    assert "HTTP(S) GitLab project URL" in win.lbl_repository.text()
    assert win.source is source
    assert win.cb_release.currentText() == release
    assert win.btn_run.isEnabled()


def test_gui_repository_override_falls_back_when_unreachable(
    qapp: QApplication, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(source: GitLabSource, device: Device) -> list[str]:
        raise OSError("repository unreachable")

    monkeypatch.setattr(GitLabSource, "list_releases", fail)
    win = local_window(base)
    win.le_repository.setText("https://other.example/team/firmware")
    win.btn_repository.click()
    wait_for_releases(win)
    assert win.cb_source.currentText() == FILESYSTEM_SOURCE
    assert win.cb_release.currentText() == "1.0.0"
    assert "repository unreachable" in win.lbl_source.text()
    assert win.le_repository.text() == "https://other.example/team/firmware"


def test_switching_source_relists_releases(
    qapp: QApplication, both_sources: dict[str, FirmwareSource]
) -> None:
    win = FlashWindow(both_sources)
    win.cb_source.setCurrentText(FILESYSTEM_SOURCE)
    wait_for_releases(win)
    assert win.cb_release.currentText() == "9.9.9"
    win.cb_source.setCurrentText(GITLAB_SOURCE)
    wait_for_releases(win)
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
    win = FlashWindow(both_sources)
    wait_for_releases(win)

    downloads = [r for r in gitlab.requests if "/artifacts/" in r]
    assert downloads == []


def test_flash_downloads_then_runs(
    qapp: QApplication, both_sources: dict[str, FirmwareSource], tmp_path: Path
) -> None:
    """The GitLab download happens in the worker, and feeds the command."""
    gitlab = both_sources[GITLAB_SOURCE]
    assert isinstance(gitlab, FakeGitLab)
    win = FlashWindow(both_sources)
    wait_for_releases(win)

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
    wait_for_releases(win)
    assert win.cb_source.currentText() == FILESYSTEM_SOURCE
    assert win.cb_release.currentText() == "9.9.9"
    assert win.btn_run.isEnabled()
    assert "no route to host" in win.lbl_source.text()
    assert "Using filesystem" in win.lbl_source.text()

    gitlab.fail = None
    win.cb_source.setCurrentText(GITLAB_SOURCE)
    wait_for_releases(win)
    assert win.cb_release.currentText() == "0.9.3b"
    assert "Using filesystem" not in win.lbl_source.text()


def test_refresh_reloads_the_cached_release_list(
    qapp: QApplication, both_sources: dict[str, FirmwareSource]
) -> None:
    win = FlashWindow(both_sources)
    wait_for_releases(win)
    gitlab = win.source
    assert isinstance(gitlab, FakeGitLab)
    gitlab.releases_payload = []
    win.reload_releases()
    wait_for_releases(win)
    assert win.cb_release.count() == 0
    assert not win.btn_run.isEnabled()
    assert len(gitlab.requests) == 2


@pytest.mark.parametrize("late_failure", [False, True])
def test_slow_discovery_keeps_gui_responsive_and_ignores_stale_results(
    qapp: QApplication,
    both_sources: dict[str, FirmwareSource],
    monkeypatch: pytest.MonkeyPatch,
    late_failure: bool,
) -> None:
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    original = FakeGitLab.list_releases

    def slow_list(source: FakeGitLab, device: Device) -> list[str]:
        entered.set()
        try:
            assert resume.wait(5)
            if late_failure:
                raise OSError("late outage")
            return original(source, device)
        finally:
            finished.set()

    monkeypatch.setattr(FakeGitLab, "list_releases", slow_list)
    win = FlashWindow(both_sources)
    try:
        assert entered.wait(1)
        assert "Loading" in win.lbl_source.text()
        assert not win.btn_run.isEnabled()
        ticked: list[bool] = []
        QTimer.singleShot(0, lambda: ticked.append(True))
        assert _pump(qapp, lambda: bool(ticked), timeout=1)
        win.cb_source.setCurrentText(FILESYSTEM_SOURCE)
        wait_for_releases(win)
        assert win.cb_release.currentText() == "9.9.9"
    finally:
        resume.set()
        assert finished.wait(1)
    qapp.processEvents()
    win._finish_releases()  # pyright: ignore[reportPrivateUsage]
    assert win.cb_source.currentText() == FILESYSTEM_SOURCE
    assert win.cb_release.currentText() == "9.9.9"
    assert "late outage" not in win.lbl_source.text()


def test_outage_with_empty_filesystem_disables_flash(
    qapp: QApplication, both_sources: dict[str, FirmwareSource], tmp_path: Path
) -> None:
    gitlab = both_sources[GITLAB_SOURCE]
    assert isinstance(gitlab, FakeGitLab)
    gitlab.fail = urllib.error.URLError("offline")
    both_sources[FILESYSTEM_SOURCE] = LocalSource(tmp_path / "missing")
    win = FlashWindow(both_sources)
    wait_for_releases(win)
    assert win.cb_source.currentText() == FILESYSTEM_SOURCE
    assert not win.btn_run.isEnabled()
    assert "No releases found" in win.lbl_source.text()


def test_flash_keeps_settings_selected_before_download(
    qapp: QApplication,
    both_sources: dict[str, FirmwareSource],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, resume = threading.Event(), threading.Event()
    original = FakeGitLab._download  # pyright: ignore[reportPrivateUsage]

    def slow_download(
        source: FakeGitLab, url: str, target: Path, log: LogFn | None
    ) -> Path:
        entered.set()
        assert resume.wait(5)
        return original(source, url, target, log)

    monkeypatch.setattr(FakeGitLab, "_download", slow_download)
    win = FlashWindow(both_sources)
    wait_for_releases(win)
    stub = tmp_path / "stub.py"
    stub.write_text("import sys\nprint(repr(sys.argv))\n")
    win.le_script.setText(str(stub))
    expected = win.current_command()
    win.start_flash()
    try:
        assert entered.wait(1)
        win.cb_address.setCurrentText("9")
        win.cb_via.setChecked(True)
        win.le_ip.setText("other-host")
        win.le_port.setText("1234")
        win.le_script.clear()
        win.rb_ptd.setChecked(True)
    finally:
        resume.set()
        assert _pump(qapp, lambda: win.worker is None)
    assert repr(expected[1:]) in win.log.toPlainText()
    assert win.lbl_status.text() == "Done"


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


# ---------------------------------------------------------------- Console


@pytest.fixture
def server() -> Iterator[FakeServer]:
    """A loopback stand-in for the serial device server."""
    fake = FakeServer()
    yield fake
    fake.close()


def console_window(base: Path, server: FakeServer) -> FlashWindow:
    """A window pointed at the fake device server."""
    win = local_window(base)
    win.le_ip.setText("127.0.0.1")
    win.le_port.setText(str(server.port))
    return win


def connected_window(qapp: QApplication, base: Path, server: FakeServer) -> FlashWindow:
    win = console_window(base, server)
    win.toggle_console()
    assert _pump(qapp, lambda: "*** Connected" in win.console_view.toPlainText())
    return win


def test_console_starts_disconnected(qapp: QApplication, base: Path) -> None:
    win = local_window(base)
    assert win.console is None
    assert not win.le_console.isEnabled()
    assert not win.btn_send.isEnabled()
    assert win.btn_console.text() == "Connect"
    assert win.lbl_console_status.text() == "Not connected"


def test_console_connects_and_disconnects(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    assert win.le_console.isEnabled()
    assert win.btn_send.isEnabled()
    assert win.btn_console.text() == "Disconnect"
    assert win.lbl_console_status.text() == "Connected"

    win.toggle_console()
    assert _pump(qapp, lambda: win.console is None)
    assert not win.le_console.isEnabled()
    assert win.btn_console.text() == "Connect"
    assert "*** Disconnected" in win.console_view.toPlainText()


def test_console_refuses_an_empty_endpoint(
    qapp: QApplication, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint is validated here, as it is for flashing."""
    win = local_window(base)
    win.le_ip.setText("")
    complaints: list[str] = []

    def complain(parent: object, title: str, text: str) -> None:
        complaints.append(text)

    monkeypatch.setattr("dls_d2bpm_tools.flash_gui.QMessageBox.critical", complain)

    win.toggle_console()
    assert win.console is None
    assert complaints == ["Enter the IP address of the board"]


def test_console_sends_a_crlf_by_default(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    win.le_console.setText("status")
    win.send_to_console()

    assert _pump(qapp, lambda: bytes(server.received) == b"status\r\n")
    assert win.le_console.text() == "", "the input should be cleared once sent"


def test_console_honours_the_line_ending(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    win.cb_ending.setCurrentText(LineEnding.CR.label)
    win.le_console.setText("go")
    win.send_to_console()

    assert _pump(qapp, lambda: bytes(server.received) == b"go\r")


def test_console_local_echo_can_be_turned_off(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    """Half duplex will not echo, so the GUI does — unless it is told not to."""
    win = connected_window(qapp, base, server)
    win.cb_echo.setChecked(False)
    win.le_console.setText("quiet")
    win.send_to_console()

    assert _pump(qapp, lambda: bytes(server.received) == b"quiet\r\n")
    assert "quiet" not in win.console_view.toPlainText()


def test_console_shows_what_the_board_replies(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    server.send(b"D2AFE> ")
    assert _pump(qapp, lambda: "D2AFE> " in win.console_view.toPlainText())


def test_console_reports_the_far_end_hanging_up(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    worker = win.console
    assert worker is not None
    server.hang_up()

    assert _pump(qapp, lambda: win.console is None)
    assert not worker.console.is_open
    assert "far end closed" in win.console_view.toPlainText()
    assert win.btn_console.text() == "Connect"


def test_console_reports_a_refused_connection(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = console_window(base, server)
    server.close()  # Nothing listening on that port any more.

    win.toggle_console()
    assert _pump(qapp, lambda: win.console is None)
    assert "Could not connect" in win.console_view.toPlainText()


def test_flashing_takes_the_port_from_the_console(
    qapp: QApplication, tmp_path: Path, server: FakeServer
) -> None:
    """One endpoint, one holder: the flash needs the console to let go."""
    base = tmp_path / "fw"
    _make_runnable(base, "print('started', flush=True)\ntime.sleep(60)\n")
    win = connected_window(qapp, base, server)

    win.start_flash()
    assert _pump(qapp, lambda: win.console is None), "the console still held the port"
    assert "so the flash can use the port" in win.console_view.toPlainText()
    assert not win.btn_console.isEnabled()
    assert win.lbl_console_status.text() == "Held by the flash"

    assert _pump(qapp, lambda: "started" in win.log.toPlainText())
    win.shutdown()
    assert _pump(qapp, lambda: win.btn_console.isEnabled())


def test_shutdown_closes_the_console(
    qapp: QApplication, base: Path, server: FakeServer, qt_messages: list[str]
) -> None:
    """A live QThread at teardown aborts the process, so it must be stopped."""
    win = connected_window(qapp, base, server)
    win.shutdown()

    assert _pump(qapp, lambda: win.console is None)
    assert not any("still running" in m for m in qt_messages), qt_messages


def test_console_hex_view_shows_the_bytes(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    """The point of the hex view: seeing the terminator the board sends."""
    win = connected_window(qapp, base, server)
    win.cb_hex_view.setChecked(True)
    server.send(b"OK\r\n")

    assert _pump(qapp, lambda: "4F 4B 0D 0A" in win.console_view.toPlainText())


def test_console_text_view_is_the_default(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    assert not win.cb_hex_view.isChecked()
    server.send(b"OK\r\n")

    assert _pump(qapp, lambda: "OK" in win.console_view.toPlainText())
    assert "4F 4B" not in win.console_view.toPlainText()


def test_console_says_which_view_it_switched_to(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    """Otherwise a hex dump looks like the board started talking nonsense."""
    win = connected_window(qapp, base, server)
    win.cb_hex_view.setChecked(True)
    assert "*** Hex view" in win.console_view.toPlainText()
    win.cb_hex_view.setChecked(False)
    assert "*** Text view" in win.console_view.toPlainText()


def test_console_sends_raw_hex_with_nothing_appended(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    win.cb_hex_send.setChecked(True)
    win.le_console.setText("02 41 03")
    win.send_to_console()

    assert _pump(qapp, lambda: bytes(server.received) == b"\x02\x41\x03")
    assert win.le_console.text() == ""


def test_console_echoes_hex_as_hex(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    win.cb_hex_send.setChecked(True)
    win.le_console.setText("0d0a")
    win.send_to_console()

    assert _pump(qapp, lambda: bytes(server.received) == b"\r\n")
    assert "0D 0A" in win.console_view.toPlainText()


def test_console_keeps_bad_hex_so_it_can_be_corrected(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    win = connected_window(qapp, base, server)
    win.cb_hex_send.setChecked(True)
    win.le_console.setText("0g")
    win.send_to_console()

    assert "g is not hex" in win.console_view.toPlainText()
    assert win.le_console.text() == "0g", "the typo should still be there to fix"
    assert bytes(server.received) == b"", "nothing should have been sent"


def test_console_hex_send_disables_the_line_ending(
    qapp: QApplication, base: Path, server: FakeServer
) -> None:
    """Raw bytes go exactly as typed, so the ending would be a lie."""
    win = connected_window(qapp, base, server)
    assert win.cb_ending.isEnabled()
    win.cb_hex_send.setChecked(True)
    assert not win.cb_ending.isEnabled()
    win.cb_hex_send.setChecked(False)
    assert win.cb_ending.isEnabled()
