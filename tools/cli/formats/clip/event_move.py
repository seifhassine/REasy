"""Move one CLIP event (a VFXRangeTrack, or a sound trigger) to a different frame, in place.

Frames live in fixed-size fields (property start/end floats + key frame floats), so retiming one
track never changes the file size: the tool patches exactly those floats and asserts that
nothing else in the whole motlist changed (byte compare + decoded clip graph compare).

"""
import argparse
import struct
from pathlib import Path


from file_handlers.motion.binary import ReadContext
from file_handlers.motion.errors import MotionWriteError
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--effect', help='EffectId string of the VFXRangeTrack')
    target.add_argument('--trigger', help='Wwise trigger ID, decimal or 0x-prefixed')
    target.add_argument('--track-index', type=int, help='VFXRangeTrack position (0-based)')
    timing = parser.add_mutually_exclusive_group(required=True)
    timing.add_argument('--to', type=float, help='absolute frame for the track (first key)')
    timing.add_argument('--delta', type=float, help='frames added to the track')


def run(args):
    if args.trigger is not None:
        from tools.fsm.common import integer
        args.trigger = integer(args.trigger)

    SOURCE = args.source.resolve()
    source = SOURCE.read_bytes()
    document = RISE.parse(source, label=str(SOURCE))
    ids = [slot.motion_id for slot in document.slots]
    index = ids.index(args.motion)
    base = struct.unpack_from('<Q', source, struct.unpack_from('<Q', source, 16)[0] + index * 8)[0]
    motion = document.slots[index].payload.value


    def parse_clip(wrapper_rel):
        _name, clip_rel, tracks_rel = struct.unpack_from('<3Q', source, base + wrapper_rel)
        parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
            ReadContext.from_bytes(source), base + clip_rel, base + tracks_rel, pointer_base=base)
        return clip_rel, tracks_rel, parsed


    def walk(node, out):
        out.append(node)
        for child in node.children:
            walk(child, out)


    # ---------------------------------------------------------------- locate the track
    table_rel = struct.unpack_from('<Q', source, base + 48)[0]
    target, seen = None, 0
    for position, sequence in enumerate(motion.sequences):
        wrapper_rel = struct.unpack_from('<Q', source, base + table_rel + position * 8)[0]
        clip_rel, tracks_rel, parsed = parse_clip(wrapper_rel)
        nodes = []
        walk(parsed.clip.root, nodes)
        for node in nodes[1:]:
            if not node.name.endswith('VFXRangeTrack'):
                continue
            props = {prop.name: prop for prop in node.properties}
            effect_id = props['EffectId'].keys[0].value if props.get('EffectId') and props['EffectId'].keys else None
            matched = (args.effect is not None and effect_id == args.effect) or                   (args.track_index is not None and seen == args.track_index)
            if matched:
                target = dict(sequence=position, node=node, props=props, parsed=parsed, effect_id=effect_id)
                break
            seen += 1
        if target is None and args.trigger is not None:
            for node in nodes[1:]:
                props = {prop.name: prop for prop in node.properties}
                if not any('Sound' in node.name or 'Voice' in node.name for _ in [0]):
                    continue
                for prop_name in ('_TriggerId', '_StopTriggerId'):
                    prop = props.get(prop_name)
                    if prop and any(key.value == args.trigger for key in prop.keys):
                        target = dict(sequence=position, node=node, props=props, parsed=parsed, effect_id=None)
                        break
                if target is not None:
                    break
        if target is not None:
            break
    if target is None:
        raise SystemExit(f'no VFXRangeTrack matched (--effect {args.effect}, --track-index {args.track_index})')

    if args.trigger is not None:
        effect_prop = next(prop for prop in (target['props'].get('_TriggerId'), target['props'].get('_StopTriggerId'))
                           if prop and any(key.value == args.trigger for key in prop.keys))
        target_keys = [key for key in effect_prop.keys if key.value == args.trigger]
        assert target_keys, 'no key carries the requested trigger id'
    else:
        effect_prop = target['props']['EffectId']
        target_keys = list(effect_prop.keys)
    frames = [key.frame for key in target_keys]
    assert frames, 'the EffectId property has no keys'
    delta = args.delta if args.delta is not None else args.to - frames[0]
    label = f"EffectId={target['effect_id']!r}" if args.trigger is None else f"trigger 0x{args.trigger:08X} on {effect_prop.name}"
    print(f"  motion {args.motion}: {target['node'].name.split('.')[-1]} {label} keys at {frames} -> delta {delta:+g}")

    # every float that belongs to this track: the property ranges and each of its keys (incl. last key)
    patch = []
    for record in target['parsed'].properties:
        if record.prop is not effect_prop:
            continue
        if len(record.prop.keys) == 1:
            patch.append((record.offset, 4, record.prop.start_frame + delta, 'start_frame'))
            if record.prop.end_frame >= 0:
                patch.append((record.offset + 4, 4, record.prop.end_frame + delta, 'end_frame'))
        else:
            moved = [key.frame + (delta if key in target_keys else 0.0) for key in record.prop.keys]
            patch.append((record.offset, 4, min(moved), 'start_frame'))
            if record.prop.end_frame >= 0:
                patch.append((record.offset + 4, 4, max(moved), 'end_frame'))
    model_keys = list(target_keys) + ([effect_prop.last_key] if effect_prop.last_key else [])
    for record in list(target['parsed'].keys) + list(target['parsed'].last_keys):
        if any(record.key is key for key in model_keys):
            patch.append((record.offset, 4, record.key.frame + delta, 'key.frame'))
    assert patch, 'nothing to patch'

    output = bytearray(source)
    for offset, size, value, label in patch:
        assert 0 <= offset <= len(output) - size
        struct.pack_into('<f', output, offset, value)
    output = bytes(output)
    assert len(output) == len(source), 'retiming must not resize the file'

    # ---------------------------------------------------------------- verification
    verified = RISE.parse(output, label='verified')
    assert [slot.motion_id for slot in verified.slots] == ids, 'motion list changed'
    moved = verified.slots[index].payload.value
    check_nodes = []
    walk(getattr(moved.sequences[target['sequence']].clip, 'root'), check_nodes)
    target_index = next(record.index for record in target['parsed'].nodes if record.node is target['node'])
    moved_node = check_nodes[target_index]
    prop_name = effect_prop.name
    moved_prop = {p.name: p for p in moved_node.properties}[prop_name]
    moved_frames = [round(key.frame, 3) for key in moved_prop.keys
                    if args.trigger is None or key.value == args.trigger]
    assert moved_frames == [round(frame + delta, 3) for frame in frames], (moved_frames, frames)
    # the same motion's other sequences and every other payload must be untouched
    for position, (before, after) in enumerate(zip(motion.sequences, moved.sequences)):
        if position != target['sequence']:
            before_nodes, after_nodes = [], []
            walk(getattr(before.clip, 'root'), before_nodes)
            walk(getattr(after.clip, 'root'), after_nodes)
            assert [n.name for n in before_nodes] == [n.name for n in after_nodes]
    for other, (before, after) in enumerate(zip(document.slots, verified.slots)):
        if other == index:
            continue
        assert before.payload.value.name == after.payload.value.name
    touched = {offset for offset, _size, _value, _label in patch}
    differing = {i for i in range(len(source)) if source[i] != output[i]}
    assert differing <= {offset + byte for offset in touched for byte in range(4)}, \
        f'unexpected bytes changed outside the patched floats: {sorted(differing)[:8]}'
    print(f'  只改了 {len(touched)} 个 float（{len(patch)} 个字段），其它字节完全一致；重解析后的键位={moved_frames}')


    return bytes(output)
