"""The red mod-PAK warning in the Project Browser and the PAK Browser.

Both browsers share ``any_pak_modded``: a PAK carrying a mod payload or entries
whose path hash a mod manager zeroed means files opened here may resolve to
modified copies, which breaks modding workflows.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from file_handlers.pak.utils import any_pak_modded
from tests.test_pak_mod_detection import _write_pak
from ui.pak_browser_dialog import PakBrowserDialog
from ui.project_manager import manager as manager_module
from ui.project_manager.manager import ProjectManager

BANNER_TEXT = (
    "⚠ Modded PAKs detected in the game folder (mod payloads or invalidated "
    "entries). Please disable your mods and rescan."
)


def _pak(directory, name, hashes):
    path = os.path.join(directory, name)
    _write_pak(path, hashes)
    return path


class TestAnyPakModded(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name

    def test_clean_or_empty_sets_are_not_modded(self):
        self.assertFalse(any_pak_modded([]))
        self.assertFalse(any_pak_modded([_pak(self.directory, "clean.pak", [0x1, 0x2])]))

    def test_zeroed_path_hash_is_modded(self):
        self.assertTrue(any_pak_modded([_pak(self.directory, "invalidated.pak", [0x1, 0x0])]))

    def test_unreadable_paths_are_skipped(self):
        missing = os.path.join(self.directory, "absent.pak")
        self.assertFalse(any_pak_modded([missing]))
        self.assertTrue(
            any_pak_modded([missing, _pak(self.directory, "invalidated.pak", [0x0])])
        )


class _BannerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.modded_dir = os.path.join(self._tmp.name, "modded")
        self.clean_dir = os.path.join(self._tmp.name, "clean")
        os.makedirs(self.modded_dir)
        os.makedirs(self.clean_dir)
        _pak(self.modded_dir, "re_chunk_000.pak", [0x1, 0x0, 0x2])
        _pak(self.clean_dir, "re_chunk_000.pak", [0x1, 0x2, 0x3])


class TestProjectBrowserBanner(_BannerCase):
    def _browser(self, pak_dir, active_tab="pak"):
        return SimpleNamespace(
            pak_dir=pak_dir,
            _active_tab=active_tab,
            _mod_paks_found=False,
            mod_warning=MagicMock(),
            tr=str,
        )

    def test_modded_folder_warns_on_the_pak_tab(self):
        browser = self._browser(self.modded_dir)
        ProjectManager._refresh_mod_warning(browser)
        self.assertTrue(browser._mod_paks_found)
        browser.mod_warning.setText.assert_called_once_with(BANNER_TEXT)
        browser.mod_warning.setVisible.assert_called_once_with(True)

    def test_clean_folder_warns_nothing(self):
        browser = self._browser(self.clean_dir)
        ProjectManager._refresh_mod_warning(browser)
        self.assertFalse(browser._mod_paks_found)
        browser.mod_warning.setText.assert_not_called()
        browser.mod_warning.setVisible.assert_called_once_with(False)

    def test_flag_survives_leaving_the_tab_but_the_banner_hides(self):
        browser = self._browser(self.modded_dir, active_tab="proj")
        ProjectManager._refresh_mod_warning(browser)
        self.assertTrue(browser._mod_paks_found)  # the next PAK-tab visit re-shows it
        browser.mod_warning.setVisible.assert_called_once_with(False)

    def test_no_pak_dir_never_scans(self):
        browser = self._browser(None)
        scans = MagicMock()
        with patch.object(manager_module, "scan_all_pak_files", scans):
            ProjectManager._refresh_mod_warning(browser)
        scans.assert_not_called()
        self.assertFalse(browser._mod_paks_found)
        browser.mod_warning.setVisible.assert_called_once_with(False)

    def test_scan_failure_is_not_fatal(self):
        browser = self._browser(self.modded_dir)
        with patch.object(manager_module, "scan_all_pak_files", side_effect=OSError("gone")):
            ProjectManager._refresh_mod_warning(browser)
        self.assertFalse(browser._mod_paks_found)
        browser.mod_warning.setVisible.assert_called_once_with(False)


class TestPakBrowserBanner(_BannerCase):
    def _dialog(self, paks):
        return SimpleNamespace(
            mod_warning=MagicMock(),
            _selected_paks=MagicMock(return_value=paks),
            _valid_paths=set(),
            _all_manifest_paths=[],
            _cache_outdated=False,
            _cached_reader=None,
            show_unknown_cb=MagicMock(),
            show_only_valid_cb=MagicMock(),
            _apply_filter=MagicMock(),
            _ensure_cache=MagicMock(),
            _recompute_display=MagicMock(),
        )

    def test_modded_selection_warns_and_still_builds_the_index(self):
        dialog = self._dialog([os.path.join(self.modded_dir, "re_chunk_000.pak")])
        PakBrowserDialog._refresh_index(dialog)
        dialog.mod_warning.setVisible.assert_called_once_with(True)
        dialog._ensure_cache.assert_called_once_with(full=True)
        dialog._recompute_display.assert_called_once()

    def test_clean_selection_shows_no_banner(self):
        dialog = self._dialog([os.path.join(self.clean_dir, "re_chunk_000.pak")])
        PakBrowserDialog._refresh_index(dialog)
        dialog.mod_warning.setVisible.assert_called_once_with(False)

    def test_no_selection_clears_the_banner(self):
        dialog = self._dialog([])
        PakBrowserDialog._refresh_index(dialog)
        dialog.mod_warning.setVisible.assert_called_once_with(False)
        dialog._apply_filter.assert_called_once()
        dialog._ensure_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
