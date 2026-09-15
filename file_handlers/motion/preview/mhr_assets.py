"""Default Rise preview assets, resolved from the owning project or installation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import re
import sys

import numpy as np
from PySide6.QtCore import QThread, Signal

from file_handlers.mesh.mesh_handler import MeshHandler
from file_handlers.pak.reader import CachedPakReader
from file_handlers.rsz.rsz_file import RszFile
from file_handlers.motbank.motbank_file import MotbankFile
from utils.app_paths import resource_path
from utils.resource_file_utils import ResourceResolutionContext
from utils.type_registry import TypeRegistry
from utils.hash_util import murmur3_hash_utf16le
from ..evaluation.mesh_adapter import rig_from_re_engine_mesh
from .target import PreviewMeshPart, RigPreviewTarget
from .weapon_motion import WeaponMotion
from ..mhr_codec import MHR_MOTION_FORMAT_CODEC


# Native prefab identities; attachment transforms come from PlayerWeaponCtrl.
WEAPON_PRESETS = {
    'greatsword': (('g_swd001', 'left'),),
    'longsword': (('ls_swd003', 'left'), ('ls_saya003', 'right')),
    'shortsword': (('ss_swd001', 'left'), ('ss_sld001', 'right')),
    'dualblades': (('db_l001', 'left'), ('db_r001', 'right')),
    'hammer': (('ham001', 'left'),),
    'horn': (('hrn001', 'left'),),
    'lance': (('l_lan001', 'left'), ('l_sld001', 'right')),
    'gunlance': (('gl_lan001', 'left'), ('gl_sld001', 'right')),
    'slashaxe': (('s_axe001', 'left'),),
    'chargeaxe': (('ca_swd001', 'left'), ('ca_sld001', 'right')),
    'insectglaive': (('i_gla002', 'left'),),
    'lightbowgun': (('l_bg003', 'left'),),
    'heavybowgun': (('h_bg001', 'left'),),
    'bow': (('b_bow001', 'left'), ('b_ydt001', 'body')),
}


def weapon_family(name: str) -> str | None:
    match = re.search(r'(?:plw|wp[gln]|wpl)_([a-z]+)(?:_|\.)', name, re.IGNORECASE)
    family = match.group(1).lower() if match else None
    return family if family in WEAPON_PRESETS else None


def find_rise_installation() -> Path | None:
    if sys.platform != 'win32':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            steam = Path(winreg.QueryValueEx(key, 'SteamPath')[0])
    except FileNotFoundError:
        return None
    libraries = [steam]
    vdf = steam/'steamapps'/'libraryfolders.vdf'
    if vdf.is_file():
        libraries.extend(Path(p.replace('\\\\', '\\')) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding='utf-8')))
    for library in libraries:
        manifest = library/'steamapps'/'appmanifest_1446780.acf'
        if manifest.is_file():
            match = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding='utf-8'))
            if match:
                root = library/'steamapps'/'common'/match.group(1)
                if root.is_dir():
                    return root
    return None


def preview_context(handler, directory='') -> ResourceResolutionContext:
    context = handler.resource_context
    if not directory and context is not None and context.game.lower() in ('mhrise', 'mhr'):
        return context
    configured = (handler.app.settings.get('mhr_preview_game_directory', '') if handler.app is not None else '')
    root = Path(directory or configured) if directory or configured else find_rise_installation()
    if root is None:
        raise ValueError('Select the Monster Hunter Rise game directory to load preview assets.')
    paks = sorted(str(p) for p in root.glob('*.pak'))
    if not paks and not (root/'natives').is_dir():
        raise ValueError(f'No Rise PAKs or natives directory found in {root}')
    reader = None
    if paks:
        paths = resource_path('resources/data/lists/MHR_STM.list').read_text(encoding='utf-8').splitlines()
        reader = CachedPakReader.from_paks(paks, game='MHRise').prepare(paths)
    return ResourceResolutionContext(unpacked_dir=str(root), pak_cached_reader=reader, game='MHRise')


def attachment_transform(position, rotation) -> np.ndarray:
    x, y, z = (float(v) for v in rotation)
    cx, cy, cz, sx, sy, sz = math.cos(x), math.cos(y), math.cos(z), math.sin(x), math.sin(y), math.sin(z)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    matrix = np.eye(4, dtype=np.float32)
    matrix[:3, :3] = rz @ ry @ rx
    matrix[:3, 3] = position
    return matrix


@dataclass(frozen=True, slots=True)
class WeaponDefinition:
    mesh_path: str
    mdf_path: str
    attachments: tuple
    motion_bank_path: str


class MhrPreviewAssets:
    def __init__(self, context):
        self.context = context
        self._meshes = {}
        self._mesh_bone_hashes = {}
        self._weapons = {}
        self._weapon_definitions = {}
        self._motion_banks = {}
        self._registry = None

    def resource(self, path):
        result = self.context.resolve(path, allow_selection_dialog=False)
        if result is None:
            raise ValueError(f'Missing preview resource: {path}')
        return result

    def _read_mesh(self, path):
        resolved, data = self.resource(path)
        handler = MeshHandler()
        handler.filepath = resolved
        handler.resource_context = self.context
        handler.read(data)
        return handler

    def mesh(self, path):
        if path not in self._meshes:
            handler = self._read_mesh(path)
            self._meshes[path] = handler, rig_from_re_engine_mesh(handler.mesh)
        return self._meshes[path]

    def weapon_definition(self, family, name):
        identity = family, name
        if identity in self._weapon_definitions:
            return self._weapon_definitions[identity]
        _, data = self.resource(f'player/prefab/weapon/{family}/{name}.pfb.17')
        if self._registry is None:
            self._registry = TypeRegistry(str(resource_path('resources/data/dumps/rszmhrise.json')))
        prefab = RszFile()
        prefab.type_registry = self._registry
        prefab.game_version = 'MHRise'
        prefab.read(data)
        mesh_fields = []
        controls = []
        motion_fields = []
        for index, fields in prefab.parsed_elements.items():
            if not index:
                continue
            info = self._registry.get_type_info(prefab.instance_infos[index].type_id)
            type_name = info['name']
            if type_name == 'via.render.Mesh':
                mesh_fields.append(fields)
            if type_name == 'via.motion.Motion':
                motion_fields.append(fields)
            if type_name.startswith('snow.player.PlayerWeaponCtrl') and '_bodyData' in fields:
                controls.append(fields)
        if len(mesh_fields) != 1 or len(controls) != 1:
            raise ValueError(f'{name}: ambiguous native weapon mesh or attachment controller')
        fields = mesh_fields[0]
        # MHRise's via.render.Mesh schema names these resource slots v2/v3.
        mesh_path = fields['v2'].value.rstrip('\0')+'.2109148288'
        mdf_path = fields['v3'].value.rstrip('\0')+'.23'
        control = controls[0]
        xyz = lambda value: (value.x, value.y, value.z)
        options = []
        for option, field in (('left', '_leftHandData'), ('right', '_rightHandData'), ('body', '_bodyData')):
            record = prefab.parsed_elements[control[field].value]
            transform = attachment_transform(xyz(record['_Position']), xyz(record['_Rotation']))
            if option == 'body':
                transform[:3, :3] *= control['_bodyConstScale'].value
            options.append((option, record['_Joint'].value.rstrip('\0'), transform))
        if len(motion_fields) > 1:
            raise ValueError(f'{name}: ambiguous native weapon motion component')
        bank_path = motion_fields[0]['v9'].value.rstrip('\0') if motion_fields else ''
        result = WeaponDefinition(mesh_path, mdf_path, tuple(options), bank_path)
        self._weapon_definitions[identity] = result
        return result

    def weapon_motions(self, bank_path):
        if not bank_path:
            return ()
        if bank_path not in self._motion_banks:
            bank = MotbankFile()
            bank.read(self.resource(bank_path+'.3')[1])
            choices = []
            for item in bank.items:
                model = MHR_MOTION_FORMAT_CODEC.parse(self.resource(item.path+'.528')[1], label=item.path)
                for index, slot in enumerate(model.slots):
                    if slot.payload is not None:
                        choices.append(WeaponMotion(item.bank_id, item.path, index, slot.motion_id, slot.payload.value))
            self._motion_banks[bank_path] = tuple(choices)
        return self._motion_banks[bank_path]

    def weapon(self, family, name, hand):
        identity = family, name, hand
        if identity in self._weapons:
            return self._weapons[identity]
        definition = self.weapon_definition(family, name)
        joint, matrix = next((joint, matrix) for option, joint, matrix in definition.attachments if option == hand)
        handler, rig = self.mesh(definition.mesh_path)
        part = PreviewMeshPart(f'weapon:{name}', handler.mesh, rig, handler, definition.mdf_path, joint, matrix,
                               '' if hand == 'body' else 'main' if hand == 'left' else 'sub', definition.attachments,
                               self.weapon_motions(definition.motion_bank_path))
        self._weapons[identity] = part
        return part

    def load(self, family=None, *, weapon_only=False, motion_list_name='', required_bones=()):
        if weapon_only:
            if family not in WEAPON_PRESETS:
                raise ValueError('Select a weapon for this weapon-motion list.')
            kind = re.search(r'_(Main|Sub)_(\d+)_', motion_list_name, re.IGNORECASE)
            if kind is None:
                raise ValueError('Weapon-motion list does not identify its native parts sequence type.')
            part_index = 1 if kind[1].lower() == 'sub' else 0
            if part_index >= len(WEAPON_PRESETS[family]):
                raise ValueError(f'{family} has no default secondary weapon model.')
            name, hand = WEAPON_PRESETS[family][part_index]
            prefix = re.sub(r'\d+$', '', name)
            paths = resource_path('resources/data/lists/MHR_STM.list').read_text(encoding='utf-8').splitlines()
            pattern = re.compile(rf'natives/stm/player/prefab/weapon/{family}/({prefix}\d+)\.pfb\.17$')
            for path in paths:
                match = pattern.fullmatch(path)
                if match is None: continue
                name = match[1]
                definition = self.weapon_definition(family, name)
                known_hashes = self._mesh_bone_hashes.get(definition.mesh_path)
                if known_hashes is not None and not set(required_bones).issubset(known_hashes):
                    continue
                cached = self._meshes.get(definition.mesh_path)
                handler = cached[0] if cached is not None else self._read_mesh(definition.mesh_path)
                mesh = handler.mesh
                hashes = {murmur3_hash_utf16le(mesh.names[index]) for index in mesh.bone_indices[:mesh.joint_count]}
                self._mesh_bone_hashes[definition.mesh_path] = hashes
                if not set(required_bones).issubset(hashes): continue
                if cached is None:
                    self._meshes[definition.mesh_path] = handler, rig_from_re_engine_mesh(mesh)
                part = self.weapon(family, name, hand)
                part = replace(part, parent_joint='', local_transform=None, weapon_role='', attachment_options=(), weapon_motions=())
                return RigPreviewTarget('MHR · '+name, part.rig, parts=(part,))
            raise ValueError(f'No {family} model contains this motion list\'s animated bones.')
        _, rig = self.mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
        parts = []
        for name in ('body', 'arm', 'leg', 'wst', 'helm'):
            base = f'player/mod/m/pl001/m_{name}001'
            handler, part_rig = self.mesh(base+'.mesh.2109148288')
            parts.append(PreviewMeshPart('armor:'+name, handler.mesh, part_rig, handler, base+'.mdf2.23'))
        base = 'player/mod/face/pl_face000'
        handler, face_rig = self.mesh(base+'.mesh.2109148288')
        parts.append(PreviewMeshPart('face', handler.mesh, face_rig, handler, base+'.mdf2.23'))
        for name, hand in WEAPON_PRESETS.get(family, ()):
            parts.append(self.weapon(family, name, hand))
        return RigPreviewTarget('MHR · Hunter PL001'+(' · '+family if family else ''), rig, parts=tuple(parts))


class MhrAssetLoader(QThread):
    loaded = Signal(object, object)
    failed = Signal(str)

    def __init__(self, handler, family, *, assets=None, directory='', weapon_only=False, parent=None):
        super().__init__(parent)
        self.handler, self.family, self.assets, self.directory = handler, family, assets, directory
        self.weapon_only = weapon_only
        self.motion_list_name = handler.model.name
        self.required_bones = frozenset(node.joint.binding_hash
            for slot in handler.model.slots if slot.payload is not None
            for node in slot.payload.value.animation_nodes) if weapon_only else ()

    def run(self):
        try:
            assets = self.assets or MhrPreviewAssets(preview_context(self.handler, self.directory))
            target = assets.load(self.family, weapon_only=self.weapon_only,
                                 motion_list_name=self.motion_list_name, required_bones=self.required_bones)
            for handler, rig in assets._meshes.values():
                if handler.thread() == QThread.currentThread():
                    handler.moveToThread(self.parent().thread())
            self.loaded.emit(assets, target)
        except (ValueError, OSError, KeyError) as exc:
            self.failed.emit(str(exc))
