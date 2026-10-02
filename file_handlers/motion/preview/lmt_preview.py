"""XX source skeleton and native Rise hunter/weapon preview."""
from dataclasses import replace

from .wilds_preview import WildsPreview
from .widget import MotListPreviewWidget
from ..lmt_codec import xx_weapon_family
from ..mhr_codec import MHR_MOTION_FORMAT_CODEC
from ..mhr_import import idle_template
from ..evaluation.lmt_retarget import LmtToRise
from .lmt_attachments import lmt_weapon_attachment


class LmtPreview(WildsPreview):
    weapon_family = staticmethod(xx_weapon_family)
    attachment_resolver = staticmethod(lmt_weapon_attachment)

    def _build_ui(self, viewport_factory):
        super()._build_ui(viewport_factory)
        self.workspace.title_label.setText(f'LMT Editor  ·  {self.handler.model.name}')
        self.source_rig_button.setText(self.tr('Use MOD Skeleton'))

    def _load_current_motion(self, *, reset_camera):
        super()._load_current_motion(reset_camera=reset_camera)
        if self._using_source_rig:
            self.rig_label.setText(self.tr('XX MOD skeleton'))

    def _rise_loaded(self, assets, target):
        if self._cleaned:
            return
        try:
            path = f'player/mot/plw_{self._family}_100.motlist.528'
            document = MHR_MOTION_FORMAT_CODEC.parse(assets.resource(path)[1], label=path)
            hold = document.slots[idle_template(document)].payload.value
            self._assets = assets
            self.set_target(replace(target, label='XX → '+target.label), retarget=LmtToRise(hold))
        except (ValueError, OSError) as exc:
            self._rise_failed(str(exc))

    def _status_text(self, snapshot):
        return MotListPreviewWidget._status_text(self, snapshot)
