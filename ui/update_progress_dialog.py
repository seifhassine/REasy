from PySide6.QtCore import Qt, QProcess
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QProgressBar, QPushButton
from PySide6.QtWidgets import QApplication
import os


class UpdateProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Updating REasy…")
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

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)
        self._progress.setValue(0)

        self._cancel_button = QPushButton("Cancel", self)
        self._cancel_button.clicked.connect(self._on_cancel)

        self._close_button = QPushButton("Close", self)
        self._close_button.setEnabled(False)
        self._close_button.clicked.connect(self.accept)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self._cancel_button)
        btns.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._log)
        layout.addWidget(self._progress)
        layout.addLayout(btns)
        self.setLayout(layout)

    def start_with_powershell_script(self, script_path: str, cwd: str, target_dir: str, exe_name: str):
        self._script_path = script_path
        self._target_dir = target_dir
        self._exe_name = exe_name or 'REasy.exe'
        program = "powershell"
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
        try:
            text = bytes(data).decode(errors='replace')
        except Exception:
            text = str(data)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if len(text) and not text.endswith("\n"):
            text += "\n"
        for line in text.splitlines(True):
            if line.startswith("PROGRESS "):
                try:
                    pct = int(line.split()[1])
                    self._progress.setRange(0, 100)
                    self._progress.setValue(max(0, min(100, pct)))
                except Exception:
                    pass
            elif line.startswith("STAGED "):
                self._staged_path = line.split(" ", 1)[1].strip()
                if self._staged_path and self._script_path and self._target_dir:
                    self._on_apply()
                    return
            elif line.startswith("TARGET "):
                pass
            elif line.strip() == "READY":
                pass
            else:
                if line.strip():
                    self._log.moveCursor(QTextCursor.End)
                    self._log.insertPlainText(line)
                    self._log.moveCursor(QTextCursor.End)

    def _on_finished(self, exit_code: int, exit_status):
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._cancel_button.setEnabled(False)
        self._close_button.setEnabled(True)
        if exit_code == 0:
            self._log.append("\nStaging completed.")
        else:
            self._log.append(f"\nUpdate failed with code {exit_code}. See log above for details.")

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
            "powershell.exe",
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
        QProcess.startDetached("cmd.exe", args, self._target_dir)
        app = QApplication.instance()
        if app:
            app.quit()