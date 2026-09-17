"""Header-click sorting invariants for the sound viewer media table."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from file_handlers.sound import sound_viewer


class _Track:
    def __init__(self, index, source_id):
        self.index, self.source_id = index, source_id
        self.available, self.payload_complete = True, True
        self.event_ids, self.plugin_id, self.media_kind = (), 0, 0
        self.absolute_offset = False


class TestSoundViewerSorting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _viewer(self, tracks):
        viewer = sound_viewer.SoundViewer.__new__(sound_viewer.SoundViewer)
        viewer._parsed_tracks = tracks
        viewer._parse_result = None
        viewer._active_event_id = None
        viewer._sound_metadata = MagicMock()
        viewer._sound_metadata.label.return_value = ""
        viewer._sound_metadata.source_event_labels.return_value = ()
        viewer._sound_metadata.event_names.return_value = ()
        viewer._sound_metadata.prefetch_event_banks.return_value = ()
        viewer._sound_metadata.prefetch_media_banks.return_value = ()
        viewer._is_media_bank = MagicMock(return_value=False)
        viewer._is_split_prefetch = MagicMock(return_value=False)
        viewer._track_status = MagicMock(return_value=("Bank", ""))
        viewer._update_source_card = lambda: None
        viewer._populate_event_graph = lambda event: None
        viewer.source_search = MagicMock()
        viewer.source_search.text.return_value = ""
        viewer.handler = MagicMock()
        viewer.handler.filepath = ""
        viewer.handler.raw_data = b""
        with patch.object(QMediaPlayer, "__init__", lambda self, *a, **k: None):
            viewer._build_table()
        viewer.source_context_label = MagicMock()
        viewer._populate(tracks)
        return viewer

    def _column(self, viewer, index):
        return [viewer.table.item(row, index).text() for row in range(viewer.table.rowCount())]

    def test_header_click_sorts_by_source_id(self):
        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        viewer._on_header_clicked(1)
        self.assertEqual(self._column(viewer, 1), ["77", "300", "910"])

    def test_second_click_flips_direction(self):
        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        viewer._on_header_clicked(1)
        viewer._on_header_clicked(1)
        self.assertEqual(self._column(viewer, 1), ["910", "300", "77"])

    def test_new_column_resets_to_ascending(self):
        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        viewer._on_header_clicked(1)
        viewer._on_header_clicked(1)  # descending source id
        viewer._on_header_clicked(0)  # switch column: ascending again
        self.assertEqual(self._column(viewer, 0), ["0", "1", "2"])
        self.assertEqual([track.source_id for track in viewer._visible_tracks], [910, 300, 77])

    def test_unknown_durations_sort_last_without_crashing(self):
        # "Unknown" durations are None: sorting used to raise TypeError.
        from types import SimpleNamespace

        metadata = SimpleNamespace(duration_seconds=None, codec="", channels=0, sample_rate=0, details="")
        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        with (
            patch.object(sound_viewer, "embedded_wem_view", lambda data, track: b"wem"),
            patch.object(sound_viewer, "parse_wem_metadata", lambda data, plugin, kind: metadata),
        ):
            viewer._on_header_clicked(3)
        self.assertEqual(self._column(viewer, 1), ["910", "300", "77"])  # equal keys keep order

    def test_known_duration_sorts_before_unknown(self):
        from types import SimpleNamespace

        # Durations key off the track itself, so a working sort must reorder
        # these rows: source 300 is the only one with a known duration.
        durations = {"910": None, "300": 2.5, "77": None}

        def fake_metadata(data, _plugin, _kind):
            return SimpleNamespace(
                duration_seconds=durations[data.decode()], codec="",
                channels=0, sample_rate=0, details="",
            )

        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        with (
            patch.object(sound_viewer, "embedded_wem_view", lambda data, track: str(track.source_id).encode()),
            patch.object(sound_viewer, "parse_wem_metadata", fake_metadata),
        ):
            viewer._on_header_clicked(3)
        self.assertEqual(self._column(viewer, 1), ["300", "910", "77"])  # known first, unknowns last
        self.assertEqual(self._column(viewer, 3)[0], "0:02.50")

    def test_split_prefetch_shows_full_streamed_duration(self):
        from types import SimpleNamespace

        full = SimpleNamespace(duration_seconds=3.25, codec="Vorbis", channels=2, sample_rate=48000, details="")
        viewer = self._viewer([_Track(0, 910)])
        viewer._is_split_prefetch = lambda track: True
        viewer._complete_media = lambda track: b"full-wem"
        with patch.object(sound_viewer, "parse_wem_metadata", lambda data, plugin, kind: full):
            viewer._populate(viewer._parsed_tracks)
        self.assertEqual(self._column(viewer, 3), ["0:03.25"])

    def test_split_prefetch_falls_back_to_label_when_unresolvable(self):
        viewer = self._viewer([_Track(0, 910)])
        viewer._is_split_prefetch = lambda track: True

        def explode(_track):
            raise RuntimeError("no package")

        viewer._complete_media = explode
        viewer._populate(viewer._parsed_tracks)
        self.assertEqual(self._column(viewer, 3), ["Full media in PCK"])

    def test_active_sort_column_title_is_bold(self):
        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        header_items = [viewer.table.horizontalHeaderItem(column) for column in range(viewer.table.columnCount())]
        self.assertTrue(header_items[0].font().bold())
        self.assertFalse(header_items[1].font().bold())
        viewer._on_header_clicked(1)
        self.assertFalse(header_items[0].font().bold())
        self.assertTrue(header_items[1].font().bold())

    def test_selection_survives_re_sort(self):
        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        viewer._on_header_clicked(1)
        viewer.table.selectRow(1)  # source 300 in ascending order
        viewer._on_header_clicked(1)  # flip to descending
        self.assertEqual(viewer._selected()[1].source_id, 300)

    def test_sorted_visible_tracks_follow_display_order(self):
        viewer = self._viewer([_Track(0, 910), _Track(1, 300), _Track(2, 77)])
        viewer._on_header_clicked(1)
        self.assertEqual([track.source_id for track in viewer._visible_tracks], [77, 300, 910])


if __name__ == "__main__":
    unittest.main()
