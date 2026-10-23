import threading
import os
import sys
lazy import json
lazy import urllib.request
from PySide6.QtCore import QCoreApplication, QObject, QT_TRANSLATE_NOOP, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QMessageBox
from PySide6.QtGui import QDesktopServices
lazy from ui.qt_timers import call_later
lazy from ui.update_progress_dialog import UpdateProgressDialog

AUTO_UPDATE_TITLE = QT_TRANSLATE_NOOP(
    "UpdateNotificationManager", "Auto Update"
)


def is_windows() -> bool:
    return os.name == "nt"


class UpdateNotificationManager(QObject):
    check_finished = Signal()

    def __init__(self, main_window, current_version):
        super().__init__(main_window)
        self.current_version = current_version
        self.latest_version = None
        self.latest_release_url = None
        self._update_menu = None
        self._auto_update_prompted = False
        self.check_finished.connect(self.update_update_menu)

        self._update_check_timer = QTimer(self)
        self._update_check_timer.timeout.connect(self._start_update_check)
        self._update_check_timer.start(10 * 60 * 1000)
        call_later(self, 1000, self._start_update_check)

    @property
    def main_window(self):
        return self.parent()

    def _start_update_check(self):
        threading.Thread(target=self.check_for_updates, daemon=True).start()

    def check_for_updates(self):
        try:
            with urllib.request.urlopen("https://api.github.com/repos/seifhassine/REasy/releases/latest", timeout=5) as resp:
                data = json.load(resp)
            self.latest_version = data.get("tag_name", "")
            self.latest_release_url = data.get("html_url", "https://github.com/seifhassine/REasy/releases")
            self.check_finished.emit()
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

    def update_update_menu(self):
        button = self.main_window.update_button
        if self._update_menu:
            button.setMenu(None)
            button.hide()
            self._update_menu.deleteLater()
            self._update_menu = None

        if self.is_new_version_available():
            if self._update_check_timer is not None:
                self._update_check_timer.stop()
                self._update_check_timer = None

            update_menu = QMenu(button)

            update_action = QAction(
                QCoreApplication.translate("UpdateNotificationManager", "View on Github"),
                self.main_window,
            )
            update_action.triggered.connect(self.open_latest_release_page)
            update_menu.addAction(update_action)

            if is_windows() and getattr(sys, 'frozen', False):
                auto_update_action = QAction(
                    QCoreApplication.translate("UpdateNotificationManager", "Auto-Download and Update"),
                    self.main_window,
                )
                auto_update_action.triggered.connect(self._confirm_and_start_auto_update)
                update_menu.addAction(auto_update_action)

            button.setText(QCoreApplication.translate("UpdateNotificationManager", "Update Available"))
            button.setToolTip(QCoreApplication.translate(
                "UpdateNotificationManager", "REasy {version} is available"
            ).format(version=self.latest_version))
            button.setMenu(update_menu)
            button.show()
            self._update_menu = update_menu

            if not self._auto_update_prompted and is_windows() and getattr(sys, 'frozen', False):
                self._auto_update_prompted = True
                QTimer.singleShot(500, self._prompt_auto_update_dialog)

        # QMenuBar sizes its corner widget only when the corner is set, shown or hidden.
        self.main_window.menuBar().setCornerWidget(button.parentWidget(), Qt.TopRightCorner)

    def _prompt_auto_update_dialog(self):
        try:
            if not is_windows() or not getattr(sys, 'frozen', False):
                return
            version_str = self.latest_version or QCoreApplication.translate(
                "UpdateNotificationManager", "a new version"
            )
            reply = QMessageBox.question(
                self.main_window,
                QCoreApplication.translate("UpdateNotificationManager", AUTO_UPDATE_TITLE),
                QCoreApplication.translate(
                    "UpdateNotificationManager",
                    "{version} is available.\nDo you want to auto-download and update now?",
                ).format(version=version_str),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_auto_update()
        except Exception as e:
            print(f"Auto-update prompt failed: {e}")

    def _confirm_and_start_auto_update(self):
        try:
            version_str = self.latest_version or QCoreApplication.translate(
                "UpdateNotificationManager", "the latest version"
            )
            reply = QMessageBox.question(
                self.main_window,
                QCoreApplication.translate("UpdateNotificationManager", AUTO_UPDATE_TITLE),
                QCoreApplication.translate(
                    "UpdateNotificationManager",
                    "Do you want to auto-download and update to {version}?",
                ).format(version=version_str),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_auto_update()
        except Exception as e:
            print(f"Auto-update confirm failed: {e}")

    def _start_auto_update(self):
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
            dlg._log.append(QCoreApplication.translate(
                "UpdateNotificationManager",
                "Auto-update script not found. Opening releases page instead.",
            ))
            self.open_latest_release_page()
        dlg.exec()
