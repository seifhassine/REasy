"""Import selected MOTLIST 992 motions into a new MOTLIST 528 file.

Run with REasy's Python environment. --motion takes SOURCE_ID[:TARGET_ID] and
can be repeated. --replace requires explicit, existing target IDs. Bone animation,
the baked IK goals (native goal convention) and the native 001_Loop WeaponHold are
exported; CLIP 85 is not. Output follows the native v528 payload layout, and both
the deform pose and the engine-side contract are verified before publication.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_handlers.motion.errors import MotionParseError, MotionWriteError
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from file_handlers.motion.wilds_codec import WILDS_MOTION_FORMAT_CODEC as WILDS
from file_handlers.motion.mhr_editing import next_motion_id
from file_handlers.motion.mhr_import import import_motion, verify_imported_motion, verify_native_contract
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context, weapon_family


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path, help='source .motlist.992')
    parser.add_argument('--target', required=True, type=Path, help='existing Rise hunter .motlist.528')
    parser.add_argument('--output', required=True, type=Path, help='new output file; existing files are never overwritten')
    parser.add_argument('--motion', required=True, action='append', type=motion_mapping, metavar='SOURCE_ID[:TARGET_ID]')
    parser.add_argument('--replace', action='store_true', help='replace each explicitly named existing target MotionID')
    parser.add_argument('--name', help='override the imported name, for a single motion only')
    parser.add_argument('--game-dir', default='', help='Rise installation or unpacked natives directory; defaults to the Steam installation')
    parser.add_argument('--hold-template', type=Path, help='528 file containing 001_Loop; defaults to the target weapon family in Rise game resources')
    args = parser.parse_args(argv)
    if args.name is not None and len(args.motion) != 1:
        parser.error('--name requires a single --motion')
    if args.replace and any(target is None for _, target in args.motion):
        parser.error('--replace requires SOURCE_ID:TARGET_ID for every --motion')
    output = args.output.resolve()
    inputs = [args.source.resolve(), args.target.resolve()]
    if args.hold_template:
        inputs.append(args.hold_template.resolve())
    if output in inputs or output.exists():
        parser.error('--output must be a new file, distinct from all inputs')
    staged = None
    try:
        source = WILDS.parse(args.source.read_bytes(), label=str(args.source))
        target = RISE.parse(args.target.read_bytes(), label=str(args.target))
        family = weapon_family(target.name)
        if not target.name.lower().startswith('plw_') or family is None:
            raise MotionWriteError('Target must be a Rise hunter weapon motion list (plw_...)')
        assets = MhrPreviewAssets(preview_context(MotListHandler(), args.game_dir))
        _, rig = assets.mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
        if args.hold_template:
            hold_path, hold_data = str(args.hold_template), args.hold_template.read_bytes()
        else:
            hold_path = f'player/mot/plw_{family}_100.motlist.528'
            hold_data = assets.resource(hold_path)[1]
        hold = RISE.parse(hold_data, label=hold_path)
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
            target = import_motion(target, motion, rig, hold, target_id,
                                   replace_existing=args.replace, name=args.name)
            imported.append((source_id, target_id, motion))
        data = RISE.write(target)
        reopened = RISE.parse(data, label='verified output')
        report = []
        for source_id, target_id, motion in imported:
            result = next(slot.payload.value for slot in reopened.slots if slot.motion_id == target_id)
            error = verify_imported_motion(motion, result, rig)
            contract = verify_native_contract(reopened, target_id, result, rig)
            report.append(dict(source_id=source_id, target_id=target_id, name=result.name,
                               end_frame=result.end_frame, fps=result.frames_per_second,
                               looping=result.looping, max_matrix_error=error,
                               max_ik_goal_error=contract))
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name+'.', suffix='.tmp', delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if staged.read_bytes() != data:
            raise OSError('Staged output differs from the verified bytes')
        # Same-directory hard-link publication is atomic and refuses overwrite.
        os.link(staged, output)
        print(json.dumps(dict(output=str(output), mode='replace' if args.replace else 'append',
                              hold_template=hold_path, imported=report,
                              events='Only native 001_Loop WeaponHold; source CLIP 85 is not converted'),
                         ensure_ascii=False, indent=2))
    except (MotionParseError, MotionWriteError, OSError, ValueError) as exc:
        parser.exit(1, f'Import failed: {exc}\n')
    finally:
        if staged is not None:
            staged.unlink()


if __name__ == '__main__':
    main()
