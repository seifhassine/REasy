"""Resolve native player Actions through an explicit MOTBANK resource."""
from dataclasses import dataclass, replace
from pathlib import Path

from file_handlers.motbank.motbank_file import MotbankFile
from utils.resource_file_utils import ResourceResolutionContext, resource_path_with_version


@dataclass(frozen=True)
class ActionMotion:
    action_index: int
    action_id: int
    ex_id: int
    bank_id: int
    motion_id: int
    resource: str
    enabled: bool


def action_motion(document, id_hash, ex_id, action_index=-1):
    instance = document.references.action(id_hash, ex_id)
    if instance is None or instance.class_name != 'snow.PlayerPlayMotion2':
        return None
    fields = {field.name: field.value for field in instance.fields}
    return ActionMotion(action_index, id_hash, ex_id, fields['v3_BankID'], fields['v4_MotionID'],
                        fields['v2_Motion'], fields['v0_Enabled'])


def node_motions(document, node_index):
    return tuple(motion for index, reference in enumerate(document.bhvt.nodes[node_index].actions)
                 if (motion := action_motion(document, reference.id_hash, reference.ex_id, index)) is not None)


def linked_context(handler):
    context = handler.resource_context or ResourceResolutionContext()
    # A directly opened file still owns the natives tree it came from.
    path = Path(handler.filepath)
    for parent in path.parents:
        if parent.name.casefold() == 'natives':
            return replace(context, project_dir=str(parent.parent), game='MHRise')
    return context


def default_bank_path(filepath):
    from file_handlers.motion.preview.mhr_assets import WEAPON_PRESETS
    family = Path(filepath).name.split('.')[0].casefold()
    if family in WEAPON_PRESETS:
        return f'player/mot/plw_{family}_bank.motbank.3'
    return ''


class MotionLinkResolver:
    def __init__(self, context, bank_path=''):
        self.context = context
        self.bank_path = bank_path
        self._bank = None

    def load_resource(self, path):
        if Path(path).is_absolute():
            return path, Path(path).read_bytes()
        result = self.context.resolve(path, allow_selection_dialog=False)
        if result is None:
            raise ValueError(f'Missing resource: {path}')
        return result

    def motion_path(self, action):
        if not action.enabled:
            raise ValueError('Selected playback Action is disabled.')
        if action.resource:
            if '.motlist' not in action.resource.casefold():
                raise ValueError(f'Unsupported direct motion resource: {action.resource}')
            return resource_path_with_version(action.resource, 'motlist', 528)
        if not self.bank_path:
            raise ValueError('Select the player MOTBANK for this FSM.')
        if self._bank is None:
            bank = MotbankFile()
            bank.read(self.load_resource(self.bank_path)[1])
            self._bank = bank
        paths = tuple(dict.fromkeys(item.path for item in self._bank.items
                                   if item.bank_id == action.bank_id and item.path))
        if len(paths) != 1:
            raise ValueError(f'BankID {action.bank_id}: {len(paths)} MOTLIST matches in {self.bank_path}')
        return resource_path_with_version(paths[0], 'motlist', 528)


def motion_entry_index(entries, motion_id):
    matches = [i for i, entry in enumerate(entries) if entry.motion_id == motion_id]
    if len(matches) != 1:
        raise ValueError(f'MotionID {motion_id}: {len(matches)} playable matches in MOTLIST')
    return matches[0]
