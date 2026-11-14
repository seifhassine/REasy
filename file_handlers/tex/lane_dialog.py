from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from ui.background_jobs import run_job
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role
lazy from utils.resource_file_utils import streaming_companion_path

from . import authoring
lazy from .images import read_image_file
lazy from .kinds import cook_version, kind_catalogs
from .lanes import CHANNELS, LaneSource, compose, lane_tokens

KEEP, NEUTRAL, IMAGE, CONSTANT = range(4)
IMAGE_FILTER = "Images (*.png *.tga *.dds)"
IMAGE_SUFFIXES = (".png", ".tga", ".dds")


def lane_label(lane) -> str:
    return f"{' '.join(lane.channels.upper())} · {lane.meaning.replace('_', ' ').capitalize()}"


def exported_lane(path: str, token: str) -> bool:
    return Path(path).stem.casefold().endswith("." + token.casefold())


def write_texture(catalog, kind, output: str, *, packed: str = "", sources=None) -> tuple[list[str], tuple[str, ...]]:
    version = cook_version(catalog.game)
    output = str(Path(output).with_name(f"{Path(output).name.split('.', 1)[0]}.tex.{version}"))
    companion = streaming_companion_path(output)
    common = dict(version=version, stored_chunks=catalog.stored_chunks, streaming=companion is not None)
    if packed:
        main, full = authoring.create_from_file(Path(packed).read_bytes(), kind, **common)
    else:
        image = compose(kind.lanes, sources, None, kind.neutral, read_image_file)
        main, full = authoring.create_texture(image, kind, **common)
    written = {output: main} if full is None else {output: main, companion: full}
    for path, authored in written.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(authored.data)
    return list(written), tuple(dict.fromkeys(note for authored in written.values() for note in authored.notes))


def new_texture(app, parent=None, *, image: str = "") -> None:
    profile = mmtr_profile_for_game(app.current_game)
    dialog = NewTextureDialog(kind_catalogs(), game=profile.game if profile else "", image=image, parent=parent or app)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        paths, notes = dialog.written
        written = QCoreApplication.translate("NewTextureDialog", "Wrote {files}.").format(
            files=", ".join(Path(path).name for path in paths))
        app.status_bar.showMessage(" ".join([written, *notes]), 10000)
        app.add_tab(filename=paths[0])
    dialog.deleteLater()


class LaneSources(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(4)
        self.lanes, self.tokens, self.rows = (), (), []

    def set_lanes(self, lanes, *, keep: bool, neutral: bool) -> None:
        for row in self.rows:
            for widget in row.values():
                widget.setParent(None)
                widget.deleteLater()
        self.lanes, self.tokens, self.rows = tuple(lanes), lane_tokens(lanes), []
        for index, lane in enumerate(self.lanes):
            self.rows.append(self._row(index, lane, keep, neutral))
        self.changed.emit()

    def _row(self, index: int, lane, keep: bool, neutral: bool) -> dict[str, QWidget]:
        value = lane.codec == "value"
        row = {"label": QLabel(lane_label(lane)), "source": QComboBox(), "path": QLineEdit(),
               "browse": QPushButton(), "channel": QComboBox(), "flag": QCheckBox(self.tr("Invert") if value else
                                                                                    self.tr("DirectX")),
               "constant": QSpinBox()}
        choices = [(self.tr("Keep"), KEEP)] * keep + [(self.tr("Neutral"), NEUTRAL)] * neutral + [
            (self.tr("Image"), IMAGE)] + [(self.tr("Constant"), CONSTANT)] * value
        for text, data in choices:
            row["source"].addItem(text, data)
        row["path"].setPlaceholderText(self.tr("Image file"))
        row["browse"].setIcon(line_icon("folder"))
        row["browse"].setToolTip(self.tr("Choose an image"))
        set_style_role(row["browse"], compact=True)
        for text, data in ((self.tr("R"), "r"), (self.tr("G"), "g"), (self.tr("B"), "b"), (self.tr("A"), "a"),
                           (self.tr("Luminance"), "luminance")):
            row["channel"].addItem(text, data)
        row["channel"].setCurrentIndex(CHANNELS.index(lane.channels[0]))
        row["channel"].setVisible(value)
        row["flag"].setToolTip(self.tr("Use 1 - value") if value else self.tr("The normal map's green points down"))
        row["flag"].setVisible(value or lane.meaning == "normal")
        row["constant"].setRange(0, 255)
        for column, name in enumerate(("label", "source", "path", "browse", "channel", "flag", "constant")):
            self.grid.addWidget(row[name], index, column)
        row["source"].currentIndexChanged.connect(lambda _index, at=index: self._shown(at))
        row["path"].textChanged.connect(self.changed)
        row["browse"].clicked.connect(lambda _checked=False, at=index: self._browse(at))
        self._shown(index, row)
        return row

    def _shown(self, index: int, row=None) -> None:
        row = row or self.rows[index]
        source = row["source"].currentData()
        for name in ("path", "browse"):
            row[name].setVisible(source == IMAGE)
        row["channel"].setVisible(source == IMAGE and self.lanes[index].codec == "value")
        row["flag"].setEnabled(source == IMAGE)
        row["constant"].setVisible(source == CONSTANT)
        self.changed.emit()

    def _browse(self, index: int) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Choose an image"), self.rows[index]["path"].text(),
                                              IMAGE_FILTER)
        if path:
            self.set_image(index, path)

    def set_image(self, index: int, path: str) -> None:
        row = self.rows[index]
        row["source"].setCurrentIndex(row["source"].findData(IMAGE))
        row["path"].setText(path)
        if exported_lane(path, self.tokens[index]):
            row["channel"].setCurrentIndex(0)

    def fill_from_folder(self, folder: str, stem: str) -> int:
        found = 0
        for index, token in enumerate(self.tokens):
            path = next((Path(folder, f"{stem}.{token}{suffix}") for suffix in IMAGE_SUFFIXES
                         if Path(folder, f"{stem}.{token}{suffix}").is_file()), None)
            if path is not None:
                self.set_image(index, str(path))
                found += 1
        return found

    def valid(self) -> bool:
        return all(row["source"].currentData() != IMAGE or row["path"].text().strip() for row in self.rows)

    def images(self) -> list[str]:
        return [row["path"].text().strip() for row in self.rows if row["source"].currentData() == IMAGE]

    def sources(self) -> dict[str, LaneSource | None]:
        result = {}
        for lane, token, row in zip(self.lanes, self.tokens, self.rows):
            source = row["source"].currentData()
            if source == NEUTRAL:
                result[token] = None
            elif source == CONSTANT:
                result[token] = LaneSource(constant=row["constant"].value() / 255)
            elif source == IMAGE:
                flag = row["flag"].isChecked()
                result[token] = LaneSource(image=row["path"].text().strip(), channel=(
                    row["channel"].currentData() if lane.codec == "value" else ""),
                    invert=flag and lane.codec == "value", directx=flag and lane.meaning == "normal")
        return result


class NewTextureDialog(QDialog):
    def __init__(self, catalogs: dict, *, game: str = "", kind: str = "", image: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("New Texture"))
        self.catalogs = catalogs
        self.written = None
        root = QVBoxLayout(self)
        root.setSpacing(8)
        form = QGridLayout()
        form.setHorizontalSpacing(6)
        self.game = QComboBox()
        for name in sorted(catalogs):
            self.game.addItem(name, name)
        self.game.setCurrentIndex(max(0, self.game.findData(game)))
        self.kind = QComboBox()
        self.packed = QCheckBox(self.tr("One packed image, written as stored"))
        self.packed.setToolTip(self.tr("Take the image's channels as the texture's lanes; a block-compressed DDS keeps "
                                       "its encoded mips"))
        self.image = QLineEdit(image)
        self.image.setPlaceholderText(self.tr("Image file"))
        browse_image = QPushButton()
        browse_image.setIcon(line_icon("folder"))
        set_style_role(browse_image, compact=True)
        self.output = QLineEdit()
        self.output.setPlaceholderText(self.tr("Where to write the .tex"))
        browse_output = QPushButton()
        browse_output.setIcon(line_icon("folder"))
        set_style_role(browse_output, compact=True)
        rows = ((self.tr("Game"), self.game, None), (self.tr("Kind"), self.kind, None),
                (self.tr("Image"), self.image, browse_image), (self.tr("Output"), self.output, browse_output))
        for row, (text, field, button) in enumerate(rows):
            form.addWidget(QLabel(text), row, 0)
            form.addWidget(field, row, 1)
            if button is not None:
                form.addWidget(button, row, 2)
        root.addLayout(form)
        root.addWidget(self.packed)
        self.lanes = LaneSources(self)
        root.addWidget(self.lanes)
        self.status = QLabel("")
        set_style_role(self.status, tone="muted")
        root.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self.game.currentIndexChanged.connect(lambda _index: self._kinds(""))
        self.kind.currentIndexChanged.connect(lambda _index: self._lanes())
        self.packed.toggled.connect(lambda _checked: self._lanes())
        browse_image.clicked.connect(self._browse_image)
        browse_output.clicked.connect(self._browse_output)
        for field in (self.image, self.output):
            field.textChanged.connect(self._validate)
        self.lanes.changed.connect(self._validate)
        self._kinds(kind)
        self.packed.setChecked(bool(image))

    def catalog(self):
        return self.catalogs[self.game.currentData()]

    def chosen_kind(self):
        return self.catalog().kinds[self.kind.currentData()]

    def _kinds(self, preferred: str) -> None:
        self.kind.blockSignals(True)
        self.kind.clear()
        for name, kind in self.catalog().kinds.items():
            self.kind.addItem(f"{name} · {kind.type_name or self.tr('no type')} · {kind.files}", name)
        self.kind.setCurrentIndex(max(0, self.kind.findData(preferred)))
        self.kind.blockSignals(False)
        self._lanes()

    def _lanes(self) -> None:
        packed = self.packed.isChecked()
        self.lanes.set_lanes(() if packed else self.chosen_kind().lanes, keep=False,
                             neutral=self.chosen_kind().neutral is not None)
        self.lanes.setVisible(not packed)
        self._validate()

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Choose an image"), self.image.text(), IMAGE_FILTER)
        if path:
            self.image.setText(path)

    def _browse_output(self) -> None:
        suggested = self.output.text() or f"new_{self.chosen_kind().name}.tex.{cook_version(self.catalog().game)}"
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Save .tex"), suggested, "TEX files (*.tex.*)")
        if path:
            self.output.setText(path)

    def accept(self) -> None:
        catalog, kind, output = self.catalog(), self.chosen_kind(), self.output.text().strip()
        packed = self.image.text().strip() if self.packed.isChecked() else ""
        sources = None if packed else self.lanes.sources()
        try:
            self.written = run_job(self, self.tr("New Texture"), lambda _progress, _cancelled: write_texture(
                catalog, kind, output, packed=packed, sources=sources))
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            set_style_role(self.status, tone="danger")
            return
        super().accept()

    def _validate(self) -> None:
        packed = self.packed.isChecked()
        self.image.setEnabled(packed)
        ready = bool(self.output.text().strip()) and (bool(self.image.text().strip()) if packed else (
            self.lanes.valid() and bool(self.lanes.images())))
        self.status.setText("" if packed or self.lanes.images() else self.tr(
            "Give at least one lane an image: it sets the texture's size."))
        set_style_role(self.status, tone="muted")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ready)


class LanesDialog(QDialog):
    def __init__(self, lanes, *, title: str, intro: str, stem: str, keep: bool, neutral: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.stem = stem
        root = QVBoxLayout(self)
        root.setSpacing(8)
        text = QLabel(intro)
        text.setWordWrap(True)
        set_style_role(text, tone="muted")
        root.addWidget(text)
        self.lanes = LaneSources(self)
        self.lanes.set_lanes(lanes, keep=keep, neutral=neutral)
        root.addWidget(self.lanes)
        fill = QPushButton(self.tr("Fill from Folder..."))
        fill.setIcon(line_icon("folder"))
        fill.setToolTip(self.tr("Take each lane's image named as Export Lanes writes it, such as {name}").format(
            name=f"{stem}.{self.lanes.tokens[0]}.png"))
        fill.clicked.connect(self._fill)
        self.status = QLabel("")
        set_style_role(self.status, tone="muted")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(fill)
        root.addWidget(self.status)
        root.addWidget(self.buttons)
        self.lanes.changed.connect(self._validate)
        self._validate()

    def _fill(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, self.tr("Folder with the lane images"))
        if folder:
            found = self.lanes.fill_from_folder(folder, self.stem)
            self.status.setText(self.tr("{found} of {total} lanes found.").format(
                found=found, total=len(self.lanes.tokens)))

    def _validate(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.lanes.valid())
