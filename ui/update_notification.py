import threading
import requests
import os
import subprocess
import sys
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QMessageBox
from PySide6.QtGui import QDesktopServices

def is_windows() -> bool:
    return os.name == "nt"

def windows_version_str() -> str:
    if not is_windows():
        return ""
    v = sys.getwindowsversion()
    return f"{v.major}.{v.minor}.{v.build}"

class UpdateNotificationManager:
    def __init__(self, main_window, current_version):
        self.main_window = main_window
        self.current_version = current_version
        self.latest_version = None
        self.latest_release_url = None
        self._update_menu = None
        self._update_check_timer = None
        self._auto_update_prompted = False

        self._check_update_thread = threading.Thread(target=self.check_for_updates, daemon=True)
        self._check_update_thread.start()

        self._update_check_timer = QTimer(main_window)
        self._update_check_timer.timeout.connect(self._periodic_update_check)
        self._update_check_timer.start(10 * 60 * 1000)

        QTimer.singleShot(2000, self.update_update_menu)

    def check_for_updates(self):
        try:
            resp = requests.get(
                "https://api.github.com/repos/seifhassine/REasy/releases/latest",
                timeout=5,
                headers={"Accept": "application/vnd.github+json"}
            )
            if resp.status_code == 200:
                data = resp.json()
                self.latest_version = data.get("tag_name", "")
                self.latest_release_url = data.get("html_url", "https://github.com/seifhassine/REasy/releases")
        except Exception as e:
            print(f"Update check failed: {e}")

    def is_new_version_available(self):
        try:
            if self.latest_version and self.latest_version.lstrip("v") != self.current_version:
                return True
        except Exception:
            pass
        return False

    def open_latest_release_page(self):
        url = self.latest_release_url or "https://github.com/seifhassine/REasy/releases"
        QDesktopServices.openUrl(QUrl(url))

    def _periodic_update_check(self):
        threading.Thread(target=self.check_for_updates, daemon=True).start()
        QTimer.singleShot(2000, self.update_update_menu)
            
            
    def update_update_menu(self, force=False, menubar=None):
        if menubar is None:
            menubar = self.main_window.menuBar()
        if self._update_menu:
            menubar.removeAction(self._update_menu.menuAction())
            self._update_menu = None

        if self.is_new_version_available():
            if self._update_check_timer is not None:
                self._update_check_timer.stop()
                self._update_check_timer = None

            update_menu = QMenu("🔶 NEW VERSION AVAILABLE", self.main_window)

            update_action = QAction("View on Github", self.main_window)
            font = update_action.font()
            font.setBold(True)
            update_action.setFont(font)
            update_menu.setStyleSheet("QMenu { color: red; font-weight: bold; }")
            update_action.triggered.connect(self.open_latest_release_page)
            update_menu.addAction(update_action)

            if is_windows() and getattr(sys, 'frozen', False):
                auto_update_action = QAction("Auto-Download and Update", self.main_window)
                auto_update_action.triggered.connect(self._confirm_and_start_auto_update)
                update_menu.addAction(auto_update_action)

            menubar.addMenu(update_menu)
            self._update_menu = update_menu

            if not self._auto_update_prompted and is_windows() and getattr(sys, 'frozen', False):
                self._auto_update_prompted = True
                QTimer.singleShot(500, self._prompt_auto_update_dialog)

    def _prompt_auto_update_dialog(self):
        try:
            if not is_windows() or not getattr(sys, 'frozen', False):
                return
            version_str = self.latest_version or "a new version"
            reply = QMessageBox.question(
                self.main_window,
                "Auto Update",
                f"{version_str} is available.\nDo you want to auto-download and update now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_auto_update()
        except Exception as e:
            print(f"Auto-update prompt failed: {e}")

    def _confirm_and_start_auto_update(self):
        try:
            version_str = self.latest_version or "the latest version"
            reply = QMessageBox.question(
                self.main_window,
                "Auto Update",
                f"Do you want to auto-download and update to {version_str}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_auto_update()
        except Exception as e:
            print(f"Auto-update confirm failed: {e}")

    def _start_auto_update(self):
        try:
            from ui.update_progress_dialog import UpdateProgressDialog
        except Exception:
            thread = threading.Thread(target=self._run_auto_update, daemon=True)
            thread.start()
            return

        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        repo_root = base_dir
        script_path = os.path.join(base_dir, "resources", "scripts", "auto_update.ps1")

        dlg = UpdateProgressDialog(self.main_window)
        if os.path.isfile(script_path):
            exe_name = os.path.basename(sys.executable) if getattr(sys, 'frozen', False) else 'REasy.exe'
            dlg.start_with_powershell_script(script_path, cwd=repo_root, target_dir=repo_root, exe_name=exe_name)
        else:
            dlg._log.append("Auto-update script not found. Opening releases page instead.")
            self.open_latest_release_page()
        dlg.exec()

    def _run_auto_update(self):
        try:
            if getattr(sys, 'frozen', False):
                repo_root = os.path.dirname(sys.executable)
            else:
                repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            script_path = os.path.join(repo_root, "resources", "scripts", "auto_update.ps1")

            if not os.path.isfile(script_path):
                def notify_missing():
                    QMessageBox.warning(
                        self.main_window,
                        "Auto Update",
                        "Auto-update script not found. Opening releases page instead.")
                    self.open_latest_release_page()
                QTimer.singleShot(0, notify_missing)
                return

            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path]
            result = subprocess.run(
                cmd,
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def notify_done():
                if result.returncode == 0:
                    QMessageBox.information(
                        self.main_window,
                        "Auto Update",
                        "Update completed successfully. Please restart REasy to apply changes.")
                else:
                    msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                    QMessageBox.critical(
                        self.main_window,
                        "Auto Update Failed",
                        f"The update failed with code {result.returncode}.\n\nDetails:\n{msg}")
            QTimer.singleShot(0, notify_done)
        except Exception as e:
            def notify_error():
                QMessageBox.critical(
                    self.main_window,
                    "Auto Update Error",
                    f"An unexpected error occurred during update.\n\n{e}")
            QTimer.singleShot(0, notify_error)
