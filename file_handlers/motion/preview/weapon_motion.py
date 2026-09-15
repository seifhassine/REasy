"""Prefab-authored weapon motions sampled in the weapon's own rig space."""
from dataclasses import dataclass

from ..evaluation.mhr import MHR_EVALUATION_PROFILE
from ..mot.model import Motion
from .controller import MotionPreviewController


@dataclass(frozen=True, slots=True)
class WeaponMotion:
    bank_id: int
    list_path: str
    slot_index: int
    motion_id: int
    motion: Motion

    @property
    def special(self):
        return '_special_' in self.list_path.casefold()

    @property
    def label(self):
        return f'{self.bank_id}:{self.motion_id} · {self.motion.name}'


def select_weapon_motion(choices, motion_id, selection=-1):
    if selection >= 0:
        return choices[selection]
    if selection == -2:
        return None
    matches = [choice for choice in choices if choice.motion_id == motion_id and not choice.special]
    return matches[0] if len(matches) == 1 else None


class WeaponMotionPlayer:
    def __init__(self, choice, rig):
        self.controller = MotionPreviewController(MHR_EVALUATION_PROFILE)
        if not self.controller.load(choice.motion, rig):
            raise ValueError(f'{choice.label}: {self.controller.error_message}')

    def sample(self, frame, source_fps):
        controller = self.controller
        weapon_frame = frame * controller.frames_per_second / source_fps
        if controller.motion.looping and controller.end_frame > 0:
            weapon_frame %= controller.end_frame
        controller.set_frame(weapon_frame)
        return controller.sample()
