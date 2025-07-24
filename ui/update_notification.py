import threading
import requests
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu
from PySide6.QtGui import QDesktopServices

class UpdateNotificationManager:
    def __init__(self, main_window, current_version):
        self.main_window = main_window
        self.current_version = current_version
        self.latest_version = None
        self.latest_release_url = None
        self._update_menu = None
        self._update_check_timer = None

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

            update_action = QAction("Download Latest Release", self.main_window)
            font = update_action.font()
            font.setBold(True)
            update_action.setFont(font)
            update_menu.setStyleSheet("QMenu { color: red; font-weight: bold; }")
            update_action.triggered.connect(self.open_latest_release_page)
            update_menu.addAction(update_action)

            menubar.addMenu(update_menu)
            self._update_menu = update_menu
