"""Sound modding is gated on an active project plus a configured PAK directory.

Replacing or editing audio writes into the game folder, so every modding entry
point must refuse before it touches the user's file.
"""

from __future__ import annotations

import os
import sys
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from file_handlers.sound import sound_viewer
from file_handlers.sound.sound_viewer import SoundViewer

ENTRY_POINTS = (
    "_on_replace",
    "_on_bulk_replace",
    "_on_add_audio_source",
    "_on_edit_wem_metadata",
)


class TestModdingGate(unittest.TestCase):
    def setUp(self):
        warning = patch.object(sound_viewer.QMessageBox, "warning")
        self.warning = warning.start()
        self.addCleanup(warning.stop)
        directory = patch.object(
            sound_viewer.QFileDialog, "getExistingDirectory", return_value=""
        )
        self.directory = directory.start()
        self.addCleanup(directory.stop)

    def _viewer(self, project_dir="", pak_dir=""):
        viewer = SimpleNamespace(tr=str)
        viewer.handler = SimpleNamespace(
            resource_context=SimpleNamespace(project_dir=project_dir),
            app=SimpleNamespace(proj_dock=SimpleNamespace(pak_dir=pak_dir)),
        )
        # The real gate, bound to the stub the entry points are called with.
        viewer._ensure_modding_allowed = partial(
            SoundViewer._ensure_modding_allowed, viewer
        )
        viewer._require_track = MagicMock(return_value=None)
        viewer._parse_result = MagicMock()
        viewer._bank_edits_supported = MagicMock(return_value=False)
        return viewer

    def _probe(self, viewer, name):
        """The call an entry point makes once the gate has let it through."""
        if name in ("_on_replace", "_on_edit_wem_metadata"):
            return viewer._require_track
        if name == "_on_bulk_replace":
            return self.directory
        return viewer._bank_edits_supported

    def test_every_entry_point_is_blocked_without_a_project(self):
        for name in ENTRY_POINTS:
            with self.subTest(entry=name):
                viewer = self._viewer()
                probe = self._probe(viewer, name)
                self.warning.reset_mock()
                self.directory.reset_mock()
                getattr(SoundViewer, name)(viewer)
                probe.assert_not_called()
                self.warning.assert_called_once()

    def test_every_entry_point_passes_with_project_and_pak_dir(self):
        for name in ENTRY_POINTS:
            with self.subTest(entry=name):
                viewer = self._viewer(project_dir="C:/project", pak_dir="C:/game")
                probe = self._probe(viewer, name)
                self.warning.reset_mock()
                self.directory.reset_mock()
                getattr(SoundViewer, name)(viewer)
                probe.assert_called_once()
                self.warning.assert_not_called()

    def test_gate_requires_both_a_project_and_a_pak_dir(self):
        cases = (
            ("", "", False),
            ("C:/project", "", False),
            ("", "C:/game", False),
            ("C:/project", "C:/game", True),
        )
        for project_dir, pak_dir, expected in cases:
            with self.subTest(project_dir=project_dir, pak_dir=pak_dir):
                viewer = self._viewer(project_dir=project_dir, pak_dir=pak_dir)
                self.assertEqual(
                    SoundViewer._ensure_modding_allowed(viewer, "Replace"), expected
                )

    def test_blocked_warning_names_the_action_and_the_fix(self):
        viewer = self._viewer()
        SoundViewer._ensure_modding_allowed(viewer, "Bulk Replace")
        _parent, title, body = self.warning.call_args.args
        self.assertIn("Bulk Replace", title)
        self.assertIn("project", body)
        self.assertIn("PAK", body)

    def test_gate_tolerates_a_handler_without_project_wiring(self):
        for handler in (SimpleNamespace(app=None), SimpleNamespace()):
            with self.subTest(handler=repr(handler)):
                viewer = SimpleNamespace(tr=str, handler=handler)
                self.assertFalse(
                    SoundViewer._ensure_modding_allowed(viewer, "Replace")
                )


if __name__ == "__main__":
    unittest.main()
