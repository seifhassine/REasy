"""Import selected MOTLIST 992 motions into a new MOTLIST 528 file.

Run with REasy's Python environment. --motion takes SOURCE_ID[:TARGET_ID] and
can be repeated. --replace requires explicit, existing target IDs. Bone animation,
the baked IK goals (native goal convention) and the native 001_Loop WeaponHold are
exported; CLIP 85 is not. Output follows the native v528 payload layout, and both
the deform pose and the engine-side contract are verified before publication.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


from file_handlers.motion.errors import MotionParseError, MotionWriteError
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from file_handlers.motion.wilds_codec import WILDS_MOTION_FORMAT_CODEC as WILDS
from file_handlers.motion.mhr_editing import next_motion_id
from file_handlers.motion.mhr_import import import_motion, verify_imported_motion, verify_native_contract
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context, weapon_family
from file_handlers.motion.wilds_weapons import wilds_weapon_family


def motion_mapping(value):
    try:
        values = value.split(':')
        if len(values) not in (1, 2):
            raise ValueError
        ids = [int(item, 16 if item.lower().startswith('0x') else 10) for item in values]
        if any(not 0 <= item <= 0xFFFF for item in ids):
            raise ValueError
        return ids[0], ids[1] if len(ids) == 2 else None
    except ValueError:
        raise argparse.ArgumentTypeError('Use SOURCE_ID[:TARGET_ID], with IDs from 0 to 65535') from None

from tools.cli.runtime import EditResult


def configure_donor(parser, *, required=True):
    parser.add_argument('--donor', required=required, type=Path, help='source Wilds MOTLIST 992')
    parser.add_argument('--game-dir', default='', help='Rise installation or unpacked natives directory; defaults to the Steam installation')
    parser.add_argument('--hold-template', type=Path, help='528 file containing 001_Loop; defaults to the target weapon family in Rise game resources')


def configure(parser):
    configure_donor(parser)
    parser.add_argument('--motion', required=True, action='append', type=motion_mapping, metavar='SOURCE_ID[:TARGET_ID]')
    parser.add_argument('--replace', action='store_true', help='replace each explicitly named existing target MotionID')
    parser.add_argument('--name', help='override the imported name, for a single motion only')


def load_inputs(args):
    source = WILDS.parse(args.donor.read_bytes(), label=str(args.donor))
    target = RISE.parse(args.source.read_bytes(), label=str(args.source))
    family = weapon_family(target.name)
    if not target.name.lower().startswith('plw_') or family is None:
        raise MotionWriteError('Target must be a Rise hunter weapon motion list (plw_...)')
    # The source motion's own weapon class selects the weapon carriers: a
    # shielded model drives R_Weapon_00 from R_Shield, not from the hand.
    attach = wilds_weapon_family(source.name) or family
    if attach != family:
        print(f'note: {source.name} is a {attach} list, so the target weapon carriers'
              f' follow the source motion rather than the {family} target family',
              file=sys.stderr)
    assets = MhrPreviewAssets(preview_context(MotListHandler(), args.game_dir))
    _, rig = assets.mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
    if args.hold_template:
        hold_path, hold_data = str(args.hold_template), args.hold_template.read_bytes()
    else:
        hold_path = f'player/mot/plw_{family}_100.motlist.528'
        hold_data = assets.resource(hold_path)[1]
    hold = RISE.parse(hold_data, label=hold_path)
    return source, target, rig, hold, attach, hold_path


def run(args):
    if args.name is not None and len(args.motion) != 1:
        raise ValueError('--name requires a single --motion')
    if args.replace and any(target is None for _, target in args.motion):
        raise ValueError('--replace requires SOURCE_ID:TARGET_ID for every --motion')
    source, target, rig, hold, attach, hold_path = load_inputs(args)
    imported = []
    selected_targets = set()
    for source_id, target_id in args.motion:
        slots = [slot for slot in source.slots if slot.motion_id == source_id]
        if len(slots) != 1 or slots[0].payload is None:
            raise MotionWriteError(f'Source MotionID {source_id} must resolve to one embedded animation')
        if target_id is None:
            target_id = next_motion_id(target)
        if target_id in selected_targets:
            raise MotionWriteError(f'Target MotionID {target_id} is mapped more than once')
        selected_targets.add(target_id)
        motion = slots[0].payload.value
        target = import_motion(target, motion, rig, hold, target_id, family=attach,
                               replace_existing=args.replace, name=args.name)
        imported.append((source_id, target_id, motion))
    data = RISE.write(target)
    reopened = RISE.parse(data, label='verified output')
    report = []
    for source_id, target_id, motion in imported:
        result = next(slot.payload.value for slot in reopened.slots if slot.motion_id == target_id)
        error = verify_imported_motion(motion, result, rig, family=attach)
        contract = verify_native_contract(reopened, target_id, result, rig)
        report.append(dict(source_id=source_id, target_id=target_id, attach_family=attach,
                           name=result.name,
                           end_frame=result.end_frame, fps=result.frames_per_second,
                           looping=result.looping, max_matrix_error=error,
                           max_ik_goal_error=contract))
    return EditResult(data, dict(mode='replace' if args.replace else 'append', hold_template=hold_path, imported=report,
                                 events='Only native 001_Loop WeaponHold; source CLIP 85 is not converted'))
