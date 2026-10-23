from PySide6.QtCore import QProcess
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QProgressBar, QPushButton
from PySide6.QtWidgets import QApplication
import os

_SYSTEM32 = os.path.join(os.environ["SystemRoot"], "System32")
POWERSHELL = os.path.join(_SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe")
CMD = os.path.join(_SYSTEM32, "cmd.exe")

lazy from ui.editor_widgets import styled_label
lazy from ui.styles import set_style_role


class UpdateProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Updating REasy…"))
        self.resize(720, 420)

        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_ready_read)
        self._process.finished.connect(self._on_finished)
        
        self._script_path = None
        self._target_dir = None
        self._exe_name = 'REasy.exe'
        self._staged_path = None

        self._log = QTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setLineWrapMode(QTextEdit.NoWrap)
        set_style_role(self._log, mono=True)

        self._status = styled_label(self.tr("Updating REasy…"), self, heading="section")

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)
        self._progress.setValue(0)

        self._cancel_button = QPushButton(self.tr("Cancel"), self)
        self._cancel_button.clicked.connect(self._on_cancel)

        self._close_button = QPushButton(self.tr("Close"), self)
        self._close_button.setEnabled(False)
        set_style_role(self._close_button, variant="primary")
        self._close_button.clicked.connect(self.accept)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        btns.addStretch(1)
        btns.addWidget(self._cancel_button)
        btns.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._log, 1)
        layout.addSpacing(2)
        layout.addWidget(self._status)
        layout.addWidget(self._progress)
        layout.addSpacing(2)
        layout.addLayout(btns)
        self.setLayout(layout)

    def start_with_powershell_script(self, script_path: str, cwd: str, target_dir: str, exe_name: str):
        self._script_path = script_path
        self._target_dir = target_dir
        self._exe_name = exe_name or 'REasy.exe'
        program = POWERSHELL
        args = [
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", script_path,
            "-Target", target_dir,
        ]
        self._process.setWorkingDirectory(cwd)
        self._process.start(program, args)

    def _on_ready_read(self):
        data = self._process.readAllStandardOutput()
        text = bytes(data).decode(errors='replace')
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if len(text) and not text.endswith("\n"):
            text += "\n"
        for line in text.splitlines(True):
            if self._handle_output_line(line):
                return

    def _handle_output_line(self, line: str) -> bool:
        if line.startswith("PROGRESS "):
            try:
                pct = int(line.split()[1])
                self._progress.setRange(0, 100)
                self._progress.setValue(max(0, min(100, pct)))
            except Exception:
                pass
            return False

        if line.startswith("STAGED "):
            self._staged_path = line.split(" ", 1)[1].strip()
            if self._staged_path and self._script_path and self._target_dir:
                self._on_apply()
                return True
            return False

        if line.startswith("TARGET ") or line.strip() == "READY":
            return False
        if line.strip():
            self._log.moveCursor(QTextCursor.End)
            self._log.insertPlainText(line)
            self._log.moveCursor(QTextCursor.End)
        return False

    def _on_finished(self, exit_code: int, exit_status):
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._cancel_button.setEnabled(False)
        self._close_button.setEnabled(True)
        if exit_code == 0:
            message = self.tr("Staging completed.")
            set_style_role(self._status, tone="success")
        else:
            message = self.tr("Update failed with code {exit_code}. See log above for details.").format(
                exit_code=exit_code
            )
            set_style_role(self._status, tone="danger")
        self._status.setText(message)
        self._log.append("\n" + message)

    def _on_cancel(self):
        if self._process and self._process.state() == QProcess.Running:
            self._process.kill()
        self.reject()

    def _on_apply(self):
        if not self._script_path or not self._target_dir or not self._staged_path:
            return
        args = [
            "/c",
            "start",
            "REasy Updater",
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-NoExit",
            "-File", self._script_path,
            "-Apply",
            "-Staged", self._staged_path,
            "-Target", self._target_dir,
            "-AppPid", str(os.getpid()),
            "-ExeName", self._exe_name,
            "-Relaunch"
        ]
        QProcess.startDetached(CMD, args, self._target_dir)
        app = QApplication.instance()
        if app:
            app.quit()
