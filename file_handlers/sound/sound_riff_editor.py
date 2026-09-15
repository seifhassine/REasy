"""Loop, cue, and marker metadata editor for RIFF/WEM media."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from tools.riff_metadata import RiffLoop, RiffMarker, RiffMetadata

from .sound_hirc_editor import RowsEditor, _number

class RiffMetadataDialog(QDialog):
    def __init__(self, metadata, parent=None, profile=None):
        super().__init__(parent)
        self._source, self._metadata = metadata, None
        self.setWindowTitle(self.tr("Edit WEM Loop and Markers"))
        self.resize(760, 570)
        layout = QVBoxLayout(self)
        rate = metadata.sample_rate or 0
        count = metadata.sample_count
        duration = count * 1000 / rate if rate and count is not None else None
        info = self.tr("{rate} Hz · {samples} samples").format(
            rate=rate or self.tr("unknown rate"), samples=count if count is not None else self.tr("unknown")
        )
        if duration is not None:
            info += self.tr(" · {duration:.3f} ms").format(duration=duration)
        authoring = (
            self.tr("Wwise {version}").format(version=profile.required_version_text)
            if profile else self.tr("the compatible Wwise authoring version")
        )
        label = QLabel(
            info + "\n" + self.tr(
                "Loop end is inclusive. Changing metadata decodes and lossy "
                "re-encodes the existing WEM through {authoring}."
            ).format(authoring=authoring)
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(QLabel(self.tr("Sample loops")))
        self.loops = RowsEditor(
            [self.tr("Start sample"), self.tr("End sample (inclusive)"), self.tr("Play count (0 = infinite)"), self.tr("Type"), self.tr("Cue ID")],
            ((item.start_sample, item.end_sample, item.play_count, item.loop_type, item.cue_id) for item in metadata.loops),
            ("0", str(max(0, (count or 1) - 1)), "0", "0", "0"),
        )
        layout.addWidget(self.loops, 1)
        layout.addWidget(QLabel(self.tr("Cue markers")))
        self.markers = RowsEditor(
            [self.tr("Cue ID"), self.tr("Sample offset"), self.tr("Label")],
            ((item.cue_id, item.sample_offset, item.label) for item in metadata.markers),
            ("0", "0", ""),
        )
        layout.addWidget(self.markers, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def metadata(self):
        count = self._source.sample_count
        loops = tuple(
            RiffLoop(
                _number(row[0], "Loop start"), _number(row[1], "Loop end"),
                _number(row[2], "Play count"), _number(row[3], "Loop type"),
                _number(row[4], "Loop cue ID"),
            )
            for row in self.loops.values()
        )
        markers = tuple(
            RiffMarker(
                _number(row[0], "Cue ID"), _number(row[1], "Marker offset"), row[2]
            )
            for row in self.markers.values()
        )
        if count is not None and any(item.end_sample >= count for item in loops):
            raise ValueError(self.tr("A loop ends beyond the final sample ({count}).").format(count=count - 1))
        if count is not None and any(item.sample_offset > count for item in markers):
            raise ValueError(self.tr("A marker is beyond the audio duration."))
        if len({item.cue_id for item in markers}) != len(markers):
            raise ValueError(self.tr("Cue marker IDs must be unique."))
        return RiffMetadata(self._source.sample_rate, count, loops, markers)

    def accept(self):
        try:
            self._metadata = self.metadata()
        except ValueError as exc:
            QMessageBox.warning(self, self.tr("Invalid Loop or Marker"), str(exc))
            return
        super().accept()

    def edited_metadata(self):
        return self._metadata
