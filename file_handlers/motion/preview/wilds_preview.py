"""MOT 932 source-rig playback and an explicit Rise hunter retarget target."""
from dataclasses import replace
import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton

from .widget import MotListPreviewWidget
from .mhr_assets import MhrAssetLoader
from .assembly_renderer import MotionAssemblyRenderer
from ..evaluation.wilds_retarget import WILDS_TO_RISE


# Native Wilds weapon classes; this differs from Rise's weapon ordering.
WILDS_WEAPONS = ('greatsword', 'shortsword', 'dualblades', 'longsword', 'hammer',
                 'horn', 'lance', 'gunlance', 'slashaxe', 'chargeaxe',
                 'insectglaive', 'bow', 'heavybowgun', 'lightbowgun')


class WildsPreview(MotListPreviewWidget):
    def __init__(self, handler, *, viewport_factory=None):
        self._asset_loader = None
        self._assets = None
        super().__init__(handler, **({'viewport_factory': viewport_factory} if viewport_factory is not None else {}))
        self._scene_renderer = MotionAssemblyRenderer(self.viewport)
        self.motion_changed.connect(lambda motion: self._scene_renderer.set_motion(motion))
        self._scene_renderer.set_motion(self.current_motion)
        self.retarget_button = QPushButton(self.tr('Retarget to Rise hunter'), self)
        self.retarget_button.clicked.connect(self.load_rise_target)
        self.rig_pane.add_widget(self.retarget_button)
        QTimer.singleShot(0, self.load_rise_target)

    def load_rise_target(self):
        if self._cleaned or self._asset_loader is not None:
            return
        name = self.handler.model.name
        match = re.match(r'wp(\d{2})(?:_|$)', name, re.IGNORECASE)
        if match is None or int(match[1]) >= len(WILDS_WEAPONS):
            self._show_error(self.tr('Select a Wilds hunter weapon MOTLIST for Rise retargeting.'))
            return
        self.retarget_button.setEnabled(False)
        loader = MhrAssetLoader(self.handler, WILDS_WEAPONS[int(match[1])], assets=self._assets, parent=self)
        self._asset_loader = loader
        loader.loaded.connect(self._rise_loaded)
        loader.failed.connect(self._rise_failed)
        loader.finished.connect(lambda: self._release_loader(loader))
        loader.start()

    def _rise_loaded(self, assets, target):
        if not self._cleaned:
            self._assets = assets
            self.set_target(replace(target, label='Wilds → '+target.label), retarget=WILDS_TO_RISE)

    def _rise_failed(self, message):
        if not self._cleaned:
            self._show_error(message)

    def _release_loader(self, loader):
        if self._asset_loader is loader:
            self._asset_loader = None
        self.retarget_button.setEnabled(True)
        loader.deleteLater()

    def _status_text(self, snapshot):
        return super()._status_text(snapshot) + self.tr(' · Preview only · CLIP 85 unavailable')

    def cleanup(self):
        if self._asset_loader is not None and self._asset_loader.isRunning():
            self._asset_loader.wait()
        super().cleanup()
