"""Convert known XX hold states while preserving unrelated native CLIP data."""
import struct
from .errors import MotionWriteError
from .mot_clip.model import ClipInterpolation, ClipPropertyType
from .mot_clip.edit_v43 import ClipGraph
from .mhr_structure import Owner, Layout, Splice, materialize
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC


def is_weapon_hold(node):
    return node.name.rsplit('.', 1)[-1] == 'WeaponHold'


def rewrite_hold_clips(document, motion_id, keys, end_frame):
    """Keep sequence/override structure and all non-WeaponHold key timing intact."""
    document = materialize(document)
    for scope in ('motion', 'override'):
        count = len(Owner(document, motion_id, scope).records)
        for index in range(count):
            owner = Owner(document, motion_id, scope)
            record = owner.records[index]
            if not any(is_weapon_hold(r.node) for r in record.parsed.nodes):
                continue
            # Detach a shared MOT before editing its CLIP.
            from .mhr_structure import private_motion
            document = private_motion(document, motion_id, scope)
            owner = Owner(document, motion_id, scope)
            record = owner.records[index]
            graph = ClipGraph(document.source, record.parsed)
            for node in graph.nodes:
                if not is_weapon_hold(node):
                    continue
                properties = {p.name: p for p in node.properties}
                if not keys.keys() <= properties.keys():
                    raise MotionWriteError('Target WeaponHold is missing hand properties')
                for name, values in keys.items():
                    prop = properties[name]
                    if prop.property_type != ClipPropertyType.S32 or not prop.keys or prop.children:
                        raise MotionWriteError('Unsupported target WeaponHold property layout')
                    template = prop.keys[0]
                    prop.keys = []
                    for frame, value in values:
                        key = graph._clone(graph, template)
                        key.frame, key.value = frame, value
                        key.rate, key.offset_frame, key.curve = 0., False, None
                        key.interpolation = ClipInterpolation.DISCRETE
                        raw_key = bytearray(graph._templates[id(key)])
                        struct.pack_into('<ffI', raw_key, 0, frame, 0., int(ClipInterpolation.DISCRETE))
                        struct.pack_into('<qQ', raw_key, 16, value, 0)
                        graph._templates[id(key)] = bytes(raw_key)
                        prop.keys.append(key)
                    prop.start_frame, prop.end_frame = 0., end_frame
                    prop.last_key, prop.speed_points = None, []
            graph.clip.total_frame = max(graph.clip.total_frame, end_frame)
            header = bytearray(graph._header)
            struct.pack_into('<f', header, 8, graph.clip.total_frame)
            graph._header = bytes(header)
            start, stop = record.parsed.clip_offset, record.parsed.physical_end
            data = graph.build(origin_offset=start, pointer_base=owner.base)
            layout = Layout(document, [Splice(start, stop, bytearray(data))], motion_base=owner.base)
            document = CODEC.parse(layout.build(), label='converted LMT WeaponHold')
    return document


def verify_weapon_hold(source_motion, imported_motion, hold_document):
    from .mhr_import import lmt_hold_keys
    expected = lmt_hold_keys(source_motion, hold_document)
    if expected is None:
        return
    verify_hold_keys(imported_motion, expected)


def verify_hold_keys(imported_motion, expected):
    found = 0
    def visit(node):
        nonlocal found
        if is_weapon_hold(node):
            found += 1
            properties = {p.name: p for p in node.properties}
            for name, keys in expected.items():
                if name not in properties:
                    raise MotionWriteError(f'Imported WeaponHold is missing {name}')
                prop = properties[name]
                if [(k.frame, k.value) for k in prop.keys] != keys or any(
                        k.interpolation != ClipInterpolation.DISCRETE for k in prop.keys):
                    raise MotionWriteError(f'Imported {name} differs from the LMT attachment timeline')
        for child in node.children:
            visit(child)
    for sequence in imported_motion.sequences:
        visit(sequence.clip.root)
    if not found:
        raise MotionWriteError('Imported LMT motion has no WeaponHold')
