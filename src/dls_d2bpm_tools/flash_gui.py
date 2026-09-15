"""Qt GUI for flashing D2AFE / D2PTD boards."""

from __future__ import annotations

import subprocess
import sys
from argparse import ArgumentParser
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent, QFont, QIcon
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
    QVBoxLayout,
    QWidget,
)

from .firmware import (
    PROGRESS_RE,
    Artifact,
    Device,
    FirmwareSource,
    LogFn,
    build_command,
    latest_release,
)
from .sources import FIRMWARE_BASE, GitLabSource, LocalSource, SourceError

__all__ = ["FlashWindow", "main"]

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
QRadioButton, QCheckBox { padding: 2px; }
"""


GITLAB_SOURCE = "GitLab releases"
FILESYSTEM_SOURCE = "Filesystem"


def default_sources(base: Path = FIRMWARE_BASE) -> dict[str, FirmwareSource]:
    """The sources offered in the GUI, in the order they are listed."""
    return {GITLAB_SOURCE: GitLabSource(), FILESYSTEM_SOURCE: LocalSource(base)}


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
        except (SourceError, OSError) as e:
            self.output.emit(f"Could not prepare the firmware: {e}")
            self.done.emit(-1)
            return

        self.output.emit(f"$ {' '.join(cmd)}")
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
        self.source_error = ""

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
        outer.addWidget(self._build_firmware_box())
        outer.addWidget(self._build_run_box())

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

    def _with_browse(self, edit: QLineEdit, handler: object) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse…")
        button.clicked.connect(handler)  # type: ignore[arg-type]
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

    def _connect_signals(self) -> None:
        self.rb_afe.toggled.connect(self.refresh_releases)
        self.cb_source.currentIndexChanged.connect(self.refresh_releases)
        self.cb_via.stateChanged.connect(self.update_preview)
        self.cb_address.currentIndexChanged.connect(self.update_preview)
        self.le_ip.textChanged.connect(self.update_preview)
        self.le_port.textChanged.connect(self.update_preview)
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
        source = self.source
        refresh = getattr(source, "refresh", None)
        if callable(refresh):
            refresh()
        self.refresh_releases()

    def refresh_releases(self) -> None:
        """Repopulate the release lists for the current source and device."""
        self.lbl_source.setText(self.source.description)
        try:
            releases = self.source.list_releases(self.device)
            self.source_error = ""
        except SourceError as e:
            releases = []
            self.source_error = str(e)

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

    def current_command(self, log: LogFn | None = None) -> list[str]:
        """The command the current settings would run. May raise.

        With no `log` the artifacts are only located, not downloaded, so this
        is safe to call on the GUI thread to render the preview. Pass a `log`
        to also make the files exist, which may hit the network.
        """
        script = self.script_artifact()
        binary = self.binary_artifact()
        script_path = script.obtain(log) if log else script.path
        binary_path = binary.obtain(log) if log else binary.path
        return build_command(
            script_path,
            binary_path,
            # Downloaded scripts have no executable bit, so always go through
            # an interpreter rather than exec'ing the file.
            python=sys.executable,
            d2afe_address=self.cb_address.currentText(),
            ip=self.le_ip.text(),
            port=self.le_port.text(),
            via_ptg=self.cb_via.isChecked(),
        )

    def update_preview(self) -> None:
        """Show the command that would run, or why it cannot be built yet."""
        try:
            self.lbl_command.setText(" ".join(self.current_command()))
            self.btn_run.setEnabled(self.worker is None)
        except (SourceError, OSError, FileNotFoundError) as e:
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
            self.current_command()
        except (SourceError, OSError, FileNotFoundError) as e:
            QMessageBox.critical(self, "Cannot flash", str(e))
            return

        self.log.clear()
        self.lbl_status.setText("Running")
        self.lbl_phase.setText("")
        self.progress.setValue(0)
        self.btn_run.setEnabled(False)

        self.worker = FlashWorker(self.current_command)
        self.worker.output.connect(self.handle_output)
        self.worker.done.connect(self.handle_finished)
        # Only release our reference once the thread has actually exited.
        self.worker.finished.connect(self.release_worker)
        self.worker.start()

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

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Don't let Qt tear down a running flash underneath us."""
        worker = self.worker
        if worker is None or not worker.isRunning():
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
        """Stop any running flash and wait for its thread to exit.

        Safe to call more than once. Qt aborts the process if a QThread is
        destroyed while still running, so this must happen before teardown.
        """
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
        "--firmware-base",
        type=Path,
        default=FIRMWARE_BASE,
        help=f"root of the filesystem release tree (default: {FIRMWARE_BASE})",
    )
    argv = list(args) if args is not None else sys.argv[1:]
    parsed, qt_args = parser.parse_known_args(argv)

    app = QApplication([sys.argv[0] if sys.argv else "", *qt_args])
    app.setWindowIcon(QIcon())
    window = FlashWindow(
        default_sources(parsed.firmware_base),
        default=GITLAB_SOURCE if parsed.source == "gitlab" else FILESYSTEM_SOURCE,
    )
    # quit() can be reached without ever passing through closeEvent.
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
