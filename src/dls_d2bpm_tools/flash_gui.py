"""Qt GUI for flashing D2AFE / D2PTD boards."""

from __future__ import annotations

import subprocess
import sys
import threading
from argparse import ArgumentParser
from collections.abc import Callable, Mapping, Sequence
from copy import copy
from pathlib import Path
from queue import Empty, Queue

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .console import ConsoleError, LineEnding, RawConsole
from .firmware import (
    PROGRESS_RE,
    Artifact,
    Device,
    FirmwareSource,
    LogFn,
    build_command,
    format_command,
    latest_release,
)
from .sources import (
    FIRMWARE_BASE,
    GITLAB_REPOSITORY,
    GitLabSource,
    LocalSource,
    SourceError,
)

__all__ = ["ConsoleWorker", "FlashWindow", "main"]

DEFAULT_IP = "172.23.241.15"
DEFAULT_PORT = "7003"
DEFAULT_D2AFE_ADDRESS = "2"

STYLESHEET = """
QWidget {
    background: #1e2128;
    color: #dfe3ea;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #333843;
    border-radius: 8px;
    margin-top: 16px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #8f9bb3;
}
QLineEdit, QComboBox, QPlainTextEdit {
    background: #272b34;
    border: 1px solid #3a4150;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #3d7eff;
}
QLineEdit:focus, QComboBox:focus {
    border-color: #3d7eff;
}
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #272b34;
    border: 1px solid #3a4150;
    selection-background-color: #3d7eff;
}
QPushButton {
    background: #2f3540;
    border: 1px solid #3a4150;
    border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover { background: #3a4150; }
QPushButton:pressed { background: #262b33; }
QPushButton#run {
    background: #3d7eff;
    border: none;
    color: white;
    font-weight: 600;
    padding: 10px;
    font-size: 14px;
}
QPushButton#run:hover { background: #5590ff; }
QPushButton#run:disabled { background: #39404d; color: #7b8496; }
QProgressBar {
    background: #272b34;
    border: 1px solid #3a4150;
    border-radius: 6px;
    height: 18px;
    text-align: center;
}
QProgressBar::chunk { background: #3d7eff; border-radius: 5px; }
QLabel#command {
    background: #15171c;
    border: 1px solid #333843;
    border-radius: 6px;
    padding: 8px;
    color: #9fd0a6;
}
QLabel#hint { color: #8f9bb3; }
QLabel#warn { color: #e0b070; }
QTabWidget::pane {
    border: 1px solid #333843;
    border-radius: 8px;
    top: -1px;
}
QTabBar::tab {
    background: #272b34;
    border: 1px solid #333843;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 7px 18px;
    margin-right: 2px;
    color: #8f9bb3;
}
QTabBar::tab:selected {
    background: #1e2128;
    color: #dfe3ea;
    font-weight: 600;
}
QRadioButton, QCheckBox { padding: 2px; }
"""


GITLAB_SOURCE = "GitLab releases"
FILESYSTEM_SOURCE = "Filesystem"


def default_sources(
    base: Path = FIRMWARE_BASE, firmware_repo: str = GITLAB_REPOSITORY
) -> dict[str, FirmwareSource]:
    """The sources offered in the GUI, in the order they are listed."""
    return {
        GITLAB_SOURCE: GitLabSource.from_repository(firmware_repo),
        FILESYSTEM_SOURCE: LocalSource(base),
    }


def _list_releases(
    source: FirmwareSource,
    device: Device,
    refresh: bool,
    results: Queue[tuple[FirmwareSource, list[str], str]],
) -> None:
    """Discover releases without accessing Qt or keeping a window alive."""
    try:
        if refresh:
            refresh_source = getattr(source, "refresh", None)
            if callable(refresh_source):
                refresh_source()
        results.put((source, source.list_releases(device), ""))
    except (SourceError, OSError, ValueError) as e:
        results.put((source, [], str(e)))


class FlashWorker(QThread):
    """Runs the flash command in the background, streaming its output.

    Note that ``done`` reports the exit code but is emitted from *inside*
    ``run``; the thread is only truly finished once QThread's own ``finished``
    signal arrives. Callers must not drop their reference to the worker before
    then, or Qt will destroy a still-running thread and abort the process.
    """

    output = Signal(str)
    done = Signal(int)

    def __init__(self, prepare: Callable[[LogFn], Sequence[str]]) -> None:
        super().__init__()
        #: Called in this thread to fetch the firmware and build the command.
        #: Downloading a release can be slow, so it must not run on the GUI
        #: thread.
        self.prepare = prepare
        self._proc: subprocess.Popen[str] | None = None

    def run(self) -> None:
        try:
            cmd = self.prepare(self.output.emit)
        except (SourceError, OSError, ValueError) as e:
            self.output.emit(f"Could not prepare the firmware: {e}")
            self.done.emit(-1)
            return

        self.output.emit(f"$ {format_command(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            self.output.emit(f"Failed to start: {e}")
            self.done.emit(-1)
            return

        self._proc = proc
        try:
            # `with` closes the stdout pipe and reaps the child on the way out.
            with proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.output.emit(line.rstrip())
            self.done.emit(proc.returncode)
        finally:
            self._proc = None

    def stop(self) -> None:
        """Ask the running command to terminate, if there is one."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()


class ConsoleWorker(QThread):
    """Opens a `RawConsole` and pumps it, so the GUI never blocks on a socket.

    Connecting can take as long as the connect timeout and reading blocks by
    design, neither of which the GUI thread can afford to do.
    """

    received = Signal(str)
    opened = Signal()
    #: Emitted once, with the reason, or empty for a disconnect we asked for.
    closed = Signal(str)

    def __init__(self, console: RawConsole) -> None:
        super().__init__()
        self.console = console
        self._stopping = False

    def run(self) -> None:
        try:
            self.console.open()
        except ConsoleError as e:
            self.closed.emit(str(e))
            return

        self.opened.emit()
        while True:
            text = self.console.read()
            if text is None:
                break
            if text:
                self.received.emit(text)
        self.console.close()
        self.closed.emit("" if self._stopping else "The far end closed the connection")

    def stop(self) -> None:
        """Ask the console to disconnect, unblocking the read in `run`."""
        self._stopping = True
        self.console.close()


class FlashWindow(QMainWindow):
    """Main window: pick a device and a release, then flash it."""

    def __init__(
        self,
        sources: Mapping[str, FirmwareSource] | None = None,
        default: str | None = None,
    ) -> None:
        super().__init__()
        self.sources: dict[str, FirmwareSource] = dict(
            sources if sources is not None else default_sources()
        )
        self.worker: FlashWorker | None = None
        self.console: ConsoleWorker | None = None
        self.source_error = ""
        self._release_results: Queue[tuple[FirmwareSource, list[str], str]] = Queue()
        self._source_notice = ""
        self._release_timer = QTimer(self)
        self._release_timer.setInterval(25)
        self._release_timer.timeout.connect(self._finish_releases)

        self.setWindowTitle("D2 Programmer")
        self.setMinimumWidth(720)
        self.resize(780, 860)
        self.setStyleSheet(STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        outer.addWidget(self._build_target_box())

        # The two modes share one endpoint and cannot both hold it, so they
        # are tabs rather than another box stacked on an already tall window.
        flash_page = QWidget()
        flash_layout = QVBoxLayout(flash_page)
        flash_layout.setContentsMargins(0, 12, 0, 0)
        flash_layout.setSpacing(12)
        flash_layout.addWidget(self._build_firmware_box())
        flash_layout.addWidget(self._build_run_box())

        self.tabs = QTabWidget()
        self.tabs.addTab(flash_page, "Flash")
        self.tabs.addTab(self._build_console_box(), "Console")
        outer.addWidget(self.tabs)

        if default and default in self.sources:
            self.cb_source.setCurrentText(default)

        self._connect_signals()
        self.refresh_releases()

    # ---------------- Construction ----------------

    def _build_target_box(self) -> QWidget:
        box = QGroupBox("Target")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        self.rb_afe = QRadioButton("D2AFE")
        self.rb_ptd = QRadioButton("D2PTD")
        self.rb_afe.setChecked(True)
        self.cb_via = QCheckBox("Route via PTG")

        device_row = QHBoxLayout()
        device_row.addWidget(self.rb_afe)
        device_row.addWidget(self.rb_ptd)
        device_row.addSpacing(20)
        device_row.addWidget(self.cb_via)
        device_row.addStretch()
        form.addRow("Device", device_row)

        self.cb_address = QComboBox()
        self.cb_address.addItems([str(i) for i in range(1, 16)])
        self.cb_address.setCurrentText(DEFAULT_D2AFE_ADDRESS)
        self.cb_address.setFixedWidth(80)

        self.le_ip = QLineEdit(DEFAULT_IP)
        self.le_port = QLineEdit(DEFAULT_PORT)
        self.le_port.setFixedWidth(90)

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Board address"))
        conn_row.addWidget(self.cb_address)
        conn_row.addSpacing(16)
        conn_row.addWidget(QLabel("IP"))
        conn_row.addWidget(self.le_ip, 1)
        conn_row.addWidget(QLabel("Port"))
        conn_row.addWidget(self.le_port)
        form.addRow("Connection", conn_row)

        return box

    def _build_firmware_box(self) -> QWidget:
        box = QGroupBox("Firmware")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        self.cb_source = QComboBox()
        self.cb_source.addItems(list(self.sources))
        self.lbl_source = QLabel()
        self.lbl_source.setObjectName("hint")
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.reload_releases)
        source_row = QHBoxLayout()
        source_row.addWidget(self.cb_source)
        source_row.addWidget(btn_refresh)
        source_row.addWidget(self.lbl_source, 1)
        form.addRow("Source", source_row)

        gitlab = self.sources.get(GITLAB_SOURCE)
        repository = (
            f"{gitlab.url}/{gitlab.project}"
            if isinstance(gitlab, GitLabSource)
            else GITLAB_REPOSITORY
        )
        self.le_repository = QLineEdit(repository)
        self.le_repository.setToolTip(
            "GitLab project URL. Press Apply or Enter to load its releases."
        )
        self.btn_repository = QPushButton("Apply")
        self.btn_repository.clicked.connect(self.apply_repository)
        self.le_repository.returnPressed.connect(self.apply_repository)
        self.lbl_repository = QLabel()
        self.lbl_repository.setWordWrap(True)
        self.le_repository.textEdited.connect(
            lambda: self.lbl_repository.setText("Press Apply to use this repository.")
        )
        repository_row = QHBoxLayout()
        repository_row.addWidget(self.le_repository, 1)
        repository_row.addWidget(self.btn_repository)
        form.addRow("Repository URL", repository_row)
        form.addRow("", self.lbl_repository)

        self.cb_release = QComboBox()
        self.cb_script = QComboBox()
        form.addRow("Firmware release", self.cb_release)
        form.addRow("Script version", self.cb_script)

        self.le_binary = QLineEdit()
        self.le_binary.setPlaceholderText("(use the selected release)")
        form.addRow(
            "Binary override", self._with_browse(self.le_binary, self.browse_binary)
        )

        self.le_script = QLineEdit()
        self.le_script.setPlaceholderText("(use the selected release)")
        form.addRow(
            "Script override", self._with_browse(self.le_script, self.browse_script)
        )

        return box

    def _with_browse(self, edit: QLineEdit, handler: Callable[[], None]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse…")
        button.clicked.connect(handler)
        layout.addWidget(button)
        return row

    def _build_run_box(self) -> QWidget:
        box = QGroupBox("Run")
        layout = QVBoxLayout(box)
        layout.setSpacing(10)

        self.lbl_command = QLabel()
        self.lbl_command.setObjectName("command")
        self.lbl_command.setWordWrap(True)
        self.lbl_command.setFont(QFont("monospace", 10))
        self.lbl_command.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lbl_command.setMinimumHeight(64)
        layout.addWidget(self.lbl_command)

        self.btn_run = QPushButton("Flash")
        self.btn_run.setObjectName("run")
        self.btn_run.clicked.connect(self.start_flash)
        layout.addWidget(self.btn_run)

        status_row = QHBoxLayout()
        self.lbl_status = QLabel("Idle")
        self.lbl_phase = QLabel("")
        self.lbl_phase.setObjectName("hint")
        status_row.addWidget(self.lbl_status)
        status_row.addStretch()
        status_row.addWidget(self.lbl_phase)
        layout.addLayout(status_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace", 10))
        self.log.setMinimumHeight(140)
        self.log.setPlaceholderText("Output from the flash command appears here")
        layout.addWidget(self.log)

        return box

    def _build_console_box(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        warning = QLabel(
            "⚠  The RS485 bus is shared: everything sent here reaches every "
            "board on this port, and their replies come back interleaved. The "
            "board address above does not apply."
        )
        warning.setObjectName("warn")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        self.console_view = QPlainTextEdit()
        self.console_view.setReadOnly(True)
        self.console_view.setFont(QFont("monospace", 10))
        self.console_view.setMinimumHeight(260)
        self.console_view.setPlaceholderText(
            "Connect to open a raw TCP session on the flashing endpoint"
        )
        layout.addWidget(self.console_view, 1)

        self.le_console = QLineEdit()
        self.le_console.setFont(QFont("monospace", 10))
        self.le_console.setPlaceholderText("Type a command and press Enter")
        self.le_console.setEnabled(False)
        self.btn_send = QPushButton("Send")
        self.btn_send.setEnabled(False)

        send_row = QHBoxLayout()
        send_row.addWidget(self.le_console, 1)
        send_row.addWidget(self.btn_send)
        layout.addLayout(send_row)

        self.btn_console = QPushButton("Connect")
        self.cb_echo = QCheckBox("Local echo")
        self.cb_echo.setChecked(True)
        self.cb_echo.setToolTip(
            "Show what you send. Half-duplex RS485 usually will not echo it back."
        )
        self.cb_ending = QComboBox()
        for ending in LineEnding:
            self.cb_ending.addItem(ending.label, ending)
        # Set explicitly: without this the combo would show whichever member
        # happens to be declared first, so reordering the enum would silently
        # change what the console sends.
        self.cb_ending.setCurrentText(LineEnding.CRLF.label)
        self.cb_ending.setFixedWidth(90)
        self.cb_ending.setToolTip("The D2AFE console terminates a line on CRLF")
        self.lbl_console_status = QLabel("Not connected")
        self.lbl_console_status.setObjectName("hint")

        control_row = QHBoxLayout()
        control_row.addWidget(self.btn_console)
        control_row.addWidget(self.lbl_console_status, 1)
        control_row.addWidget(self.cb_echo)
        control_row.addWidget(QLabel("Line ending"))
        control_row.addWidget(self.cb_ending)
        layout.addLayout(control_row)

        return page

    def _connect_signals(self) -> None:
        self.rb_afe.toggled.connect(self.refresh_releases)
        self.cb_source.currentIndexChanged.connect(self.refresh_releases)
        self.cb_via.toggled.connect(self.update_preview)
        self.cb_address.currentIndexChanged.connect(self.update_preview)
        self.le_ip.textChanged.connect(self.update_preview)
        self.le_port.textChanged.connect(self.update_preview)
        self.btn_console.clicked.connect(self.toggle_console)
        self.btn_send.clicked.connect(self.send_to_console)
        self.le_console.returnPressed.connect(self.send_to_console)
        self.cb_release.currentIndexChanged.connect(self.update_preview)
        self.cb_script.currentIndexChanged.connect(self.update_preview)
        self.le_binary.textChanged.connect(self.update_preview)
        self.le_script.textChanged.connect(self.update_preview)

    # ---------------- Logic ----------------

    @property
    def device(self) -> Device:
        return Device.D2AFE if self.rb_afe.isChecked() else Device.D2PTD

    @property
    def source(self) -> FirmwareSource:
        return self.sources[self.cb_source.currentText()]

    def reload_releases(self) -> None:
        """Re-read the source from scratch, discarding anything it cached."""
        self.refresh_releases(refresh_cache=True)

    def apply_repository(self) -> None:
        """Validate an edited URL before replacing the current GitLab source."""
        try:
            source = GitLabSource.from_repository(self.le_repository.text())
        except ValueError as e:
            self.lbl_repository.setText(f"⚠  {e}")
            return
        self.sources[GITLAB_SOURCE] = source
        self.le_repository.setText(f"{source.url}/{source.project}")
        self.lbl_repository.clear()
        self.cb_source.blockSignals(True)
        if self.cb_source.findText(GITLAB_SOURCE) < 0:
            self.cb_source.addItem(GITLAB_SOURCE)
        self.cb_source.setCurrentText(GITLAB_SOURCE)
        self.cb_source.blockSignals(False)
        self.refresh_releases()

    def refresh_releases(
        self, *, refresh_cache: bool = False, notice: str = ""
    ) -> None:
        """Discover releases in the background, including on network mounts."""
        self._source_notice = notice
        self.source_error = "Loading releases…"
        self.lbl_source.setText(
            f"{notice}{self.source.description} — Loading releases…"
        )
        for combo in (self.cb_release, self.cb_script):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self.update_preview()

        # Each request owns its queue and source snapshot. Switching source or
        # device discards old results, including late failures, without waiting.
        # A daemon thread can finish an HTTP request after the window closes;
        # it never touches Qt, and must not hold application exit hostage.
        self._release_results = Queue()
        threading.Thread(
            target=_list_releases,
            args=(copy(self.source), self.device, refresh_cache, self._release_results),
            daemon=True,
        ).start()
        self._release_timer.start()

    def _finish_releases(self) -> None:
        try:
            source, releases, error = self._release_results.get_nowait()
        except Empty:
            return
        self._release_timer.stop()
        if error and isinstance(source, GitLabSource):
            if FILESYSTEM_SOURCE in self.sources:
                self.cb_source.blockSignals(True)
                self.cb_source.setCurrentText(FILESYSTEM_SOURCE)
                self.cb_source.blockSignals(False)
                self.refresh_releases(notice=f"⚠  {error}. Using filesystem. ")
                return

        self.sources[self.cb_source.currentText()] = source
        self.source_error = error
        status = f"⚠  {error}" if error else source.description
        if not error and not releases:
            status += " — No releases found"
        self.lbl_source.setText(f"{self._source_notice}{status}")
        self._populate_releases(releases)

    def _populate_releases(self, releases: list[str]) -> None:
        """Apply a completed lookup on the GUI thread."""
        newest = latest_release(releases)
        for combo in (self.cb_release, self.cb_script):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(releases)
            if newest:
                combo.setCurrentText(newest)
            combo.blockSignals(False)

        self.update_preview()

    def _artifact(self, override: str, combo: QComboBox, suffix: str) -> Artifact:
        """What to flash: an explicit path, or the selected release's file."""
        override = override.strip()
        if override:
            path = Path(override)
            if path.suffix != suffix or not path.is_file():
                raise FileNotFoundError(f"Not a readable {suffix} file: {override}")
            return Artifact(name=path.name, path=path, ensure=lambda _log=None: path)

        if self.source_error:
            raise FileNotFoundError(self.source_error)
        release = combo.currentText()
        if not release:
            raise FileNotFoundError(f"No release selected for the {suffix} file")
        return self.source.artifact(release, self.device, suffix)

    def script_artifact(self) -> Artifact:
        return self._artifact(self.le_script.text(), self.cb_script, ".py")

    def binary_artifact(self) -> Artifact:
        return self._artifact(self.le_binary.text(), self.cb_release, ".bin")

    def prepare_flash(self) -> Callable[[LogFn | None], list[str]]:
        """Capture every setting on the GUI thread before doing slow work."""
        ip, port = self.connection()
        script = self.script_artifact()
        binary = self.binary_artifact()
        address = self.cb_address.currentText()
        via_ptg = self.cb_via.isChecked()

        def prepare(log: LogFn | None = None) -> list[str]:
            return build_command(
                script.obtain(log) if log else script.path,
                binary.obtain(log) if log else binary.path,
                python=sys.executable,
                d2afe_address=address,
                ip=ip,
                port=port,
                via_ptg=via_ptg,
            )

        return prepare

    def current_command(self, log: LogFn | None = None) -> list[str]:
        """The command the current settings would run. May raise.

        With no `log` the artifacts are only located, not downloaded, so this
        is safe to call on the GUI thread to render the preview. Pass a `log`
        to also make the files exist, which may hit the network.
        """
        return self.prepare_flash()(log)

    def connection(self) -> tuple[str, str]:
        """The board's endpoint, checked here rather than by the programmer.

        An empty field would otherwise build an address like ``:7003`` and
        fail somewhere inside d2afe-cli.py, long after the mistake was made.
        """
        ip = self.le_ip.text().strip()
        port = self.le_port.text().strip()
        if not ip:
            raise ValueError("Enter the IP address of the board")
        if not port:
            raise ValueError("Enter the port to connect to")
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"Port must be a number from 1 to 65535, not {port!r}")
        return ip, port

    def update_preview(self) -> None:
        """Show the command that would run, or why it cannot be built yet."""
        try:
            self.lbl_command.setText(format_command(self.current_command()))
            self.btn_run.setEnabled(self.worker is None)
        except (SourceError, OSError, ValueError) as e:
            self.lbl_command.setText(f"⚠  {e}")
            self.btn_run.setEnabled(False)

    # ---------------- Browsing ----------------

    def _browse(self, title: str, subdir: str, filter_: str) -> str:
        candidates = [Path.home() / subdir]
        source = self.source
        if isinstance(source, LocalSource):
            candidates.append(source.base)
        start = next((c for c in candidates if c.is_dir()), Path.home())
        path, _ = QFileDialog.getOpenFileName(self, title, str(start), filter_)
        return path

    def browse_binary(self) -> None:
        path = self._browse(
            "Select firmware binary", "work/d2afe-firmware", "Binary files (*.bin)"
        )
        if path:
            self.le_binary.setText(path)

    def browse_script(self) -> None:
        path = self._browse(
            "Select firmware script",
            "work/d2afe-firmware/scripts",
            "Python files (*.py)",
        )
        if path:
            self.le_script.setText(path)

    # ---------------- Running ----------------

    def start_flash(self) -> None:
        if self.worker is not None:
            return
        try:
            # Locate everything up front so mistakes surface before we start,
            # but leave any downloading to the worker thread.
            prepare = self.prepare_flash()
        except (SourceError, OSError, ValueError) as e:
            QMessageBox.critical(self, "Cannot flash", str(e))
            return

        if self.console is not None:
            # One endpoint, one holder: the device server gives the serial
            # port to whoever is connected, so the console has to let go.
            self.console_view.appendPlainText(
                "*** Disconnected so the flash can use the port"
            )
            self.disconnect_console()

        self.log.clear()
        self.lbl_status.setText("Running")
        self.lbl_phase.setText("")
        self.progress.setValue(0)
        self.btn_run.setEnabled(False)

        self.worker = FlashWorker(prepare)
        self.worker.output.connect(self.handle_output)
        self.worker.done.connect(self.handle_finished)
        # Only release our reference once the thread has actually exited.
        self.worker.finished.connect(self.release_worker)
        self.worker.start()
        self._update_console_controls()

    # ---------------- Console ----------------

    def toggle_console(self) -> None:
        """Connect the console, or disconnect it if it is already up."""
        if self.console is not None:
            self.disconnect_console()
            return

        try:
            ip, port = self.connection()
        except ValueError as e:
            QMessageBox.critical(self, "Cannot connect", str(e))
            return

        console = RawConsole(ip, int(port))
        self.console_view.appendPlainText(f"*** Connecting to {console.endpoint}…")
        self.console = ConsoleWorker(console)
        self.console.received.connect(self.handle_console_output)
        self.console.opened.connect(self.handle_console_opened)
        self.console.closed.connect(self.handle_console_closed)
        self.console.finished.connect(self.release_console)
        self.console.start()
        self._update_console_controls(connecting=True)

    def disconnect_console(self) -> None:
        """Drop the console session, if there is one."""
        console = self.console
        if console is None:
            return
        console.stop()
        if not console.wait(5000):
            console.terminate()
            console.wait(1000)

    def handle_console_opened(self) -> None:
        self.console_view.appendPlainText("*** Connected")
        self._update_console_controls()
        self.le_console.setFocus()

    def handle_console_output(self, text: str) -> None:
        """Append received text, which arrives in chunks, not lines."""
        cursor = self.console_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.console_view.setTextCursor(cursor)

    def handle_console_closed(self, reason: str) -> None:
        self.console_view.appendPlainText(f"*** {reason or 'Disconnected'}")

    def release_console(self) -> None:
        """Drop the finished worker, once it is safe to destroy it."""
        console = self.console
        self.console = None
        if console is not None:
            console.wait()
            console.deleteLater()
        self._update_console_controls()

    def send_to_console(self) -> None:
        console = self.console
        if console is None:
            return
        text = self.le_console.text()
        ending = self.cb_ending.currentData()
        try:
            console.console.send(text, ending)
        except ConsoleError as e:
            self.console_view.appendPlainText(f"*** {e}")
            return
        if self.cb_echo.isChecked():
            self.console_view.appendPlainText(text)
        self.le_console.clear()

    def _update_console_controls(self, connecting: bool = False) -> None:
        """Keep the console controls in step with the connection and the flash."""
        flashing = self.worker is not None
        connected = self.console is not None and not connecting
        self.btn_console.setEnabled(not flashing)
        self.btn_console.setText("Disconnect" if self.console else "Connect")
        self.le_console.setEnabled(connected)
        self.btn_send.setEnabled(connected)
        if flashing:
            status = "Held by the flash"
        elif connecting:
            status = "Connecting…"
        elif connected:
            status = "Connected"
        else:
            status = "Not connected"
        self.lbl_console_status.setText(status)

    def handle_output(self, line: str) -> None:
        self.log.appendPlainText(line)
        if "Flashing temporal application" in line:
            self.lbl_phase.setText("Flashing application")
            self.progress.setValue(0)
        elif "Flashing bootloader" in line:
            self.lbl_phase.setText("Flashing bootloader")
            self.progress.setValue(0)

        match = PROGRESS_RE.search(line)
        if match:
            sent, total = (int(g) for g in match.groups())
            if total:
                self.progress.setValue(int(sent * 100 / total))

    def handle_finished(self, rc: int) -> None:
        """Report the outcome. The thread may still be winding down here."""
        self.lbl_status.setText("Done" if rc == 0 else f"Failed (exit {rc})")
        if rc == 0:
            self.progress.setValue(100)

    def release_worker(self) -> None:
        """Drop the finished worker, once it is safe to destroy it."""
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self.update_preview()
        self._update_console_controls()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Don't let Qt tear down a running flash underneath us."""
        worker = self.worker
        if worker is None or not worker.isRunning():
            self.disconnect_console()
            event.accept()
            return

        answer = QMessageBox.question(
            self,
            "Flash in progress",
            "A flash is still running. Stopping it part way through may leave "
            "the board unusable.\n\nStop it and quit anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self.shutdown()
        event.accept()

    def shutdown(self) -> None:
        """Stop any running flash and console, and wait for their threads.

        Safe to call more than once. Qt aborts the process if a QThread is
        destroyed while still running, so this must happen before teardown.
        """
        self.disconnect_console()
        worker = self.worker
        self.worker = None
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(5000):
                worker.terminate()
                worker.wait(1000)


def main(args: Sequence[str] | None = None) -> int:
    """Launch the flash GUI."""
    parser = ArgumentParser(description="Flash D2AFE / D2PTD boards")
    parser.add_argument(
        "--source",
        choices=["gitlab", "filesystem"],
        default="gitlab",
        help="where to read firmware releases from (default: gitlab)",
    )
    parser.add_argument(
        "--firmware-repo",
        default=GITLAB_REPOSITORY,
        metavar="URL",
        help=f"GitLab project providing firmware (default: {GITLAB_REPOSITORY})",
    )
    parser.add_argument(
        "--firmware-base",
        type=Path,
        default=FIRMWARE_BASE,
        help=f"root of the filesystem release tree (default: {FIRMWARE_BASE})",
    )
    argv = list(args) if args is not None else sys.argv[1:]
    parsed, qt_args = parser.parse_known_args(argv)
    try:
        sources = default_sources(parsed.firmware_base, parsed.firmware_repo)
    except ValueError as e:
        parser.error(f"--firmware-repo: {e}")

    app = QApplication([sys.argv[0] if sys.argv else "", *qt_args])
    window = FlashWindow(
        sources,
        default=GITLAB_SOURCE if parsed.source == "gitlab" else FILESYSTEM_SOURCE,
    )
    # quit() can be reached without ever passing through closeEvent.
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
