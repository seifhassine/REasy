from .widget import MotListPreviewWidget
from ..evaluation.mhw_mesh import MhwToMesh


class MhwLmtPreview(MotListPreviewWidget):
    def _build_ui(self, viewport_factory):
        super()._build_ui(viewport_factory)
        self.workspace.title_label.setText(f'LMT Preview  ·  {self.handler.model.name}')
        self.source_rig_button.setText(self.tr('Use MOD3 Skeleton'))

    def set_target(self, target, *, retarget=None):
        super().set_target(target, retarget=MhwToMesh() if retarget is None else retarget)

    def _load_current_motion(self, *, reset_camera):
        motion = self.current_motion
        if motion is not None and motion.unbound_tracks:
            self.controller.clear()
            ids = ', '.join(str(i) for i in sorted({t.bone_id for t in motion.unbound_tracks}))
            self._clear_scene(self.tr('LMT tracks parsed; MOD3 has no binding for IDs: {ids}.').format(ids=ids))
            return
        super()._load_current_motion(reset_camera=reset_camera)
