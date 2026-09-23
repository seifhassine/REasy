"""Bake a Rise motion's time axis and its CLIP events into a new frame duration."""
import math
import struct

from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from .evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
from .mhr_editing import validate_slot_order
from .errors import MotionWriteError
from .evaluation.sampling import sample_track
from .mot.model import TrackFamily


def round_frame(value):
    return math.floor(value + 0.5)


def properties(node):
    def descend(prop):
        yield prop
        for child in prop.children:
            yield from descend(child)
    for prop in node.properties:
        yield from descend(prop)
    for child in node.children:
        yield from properties(child)


def scale_clip(clip, factor):
    if clip.total_frame >= 0:
        clip.total_frame *= factor
    seen = set()
    for prop in properties(clip.root):
        keys = [*prop.keys, *([prop.last_key] if prop.last_key else [])]
        if prop.speed_points or any(key.curve or key.offset_frame for key in keys):
            raise MotionWriteError(f'{prop.name}: retiming curves, speed tracks, or relative keys is not supported')
        for attr in ('start_frame', 'end_frame'):
            if getattr(prop, attr) >= 0:
                setattr(prop, attr, getattr(prop, attr)*factor)
        for key in keys:
            if id(key) not in seen:
                if key.frame >= 0:
                    key.frame *= factor
                seen.add(id(key))
    for extra in clip.extra_ranges:
        for interval in extra.intervals:
            if interval.begin_frame is None:
                interval.frame_span = round_frame(interval.frame_span*factor)
            else:
                end = (interval.begin_frame+interval.frame_span)*factor
                interval.begin_frame *= factor
                interval.frame_span = round_frame(end-interval.begin_frame)


def restore_name_tail(document, motion_id):
    """Keep new channel buffers before the authoritative MOT name pointer.

    The source-backed writer appends buffers after existing sections. Copying the
    small name block to the end preserves compact CLIP's empty terminal sections.
    """
    source = document.source
    pointers = struct.unpack_from('<Q', source, 16)[0]
    index = next(i for i, s in enumerate(document.slots) if s.motion_id == motion_id)
    base = struct.unpack_from('<Q', source, pointers+index*8)[0]
    _, end, shared = next(span for span in document.motion_spans if span[0] == base)
    name = document.slots[index].payload.value.name.encode('utf-16le')+b'\0\0'
    name += bytes((-len(name)) % 16)
    output = bytearray(source[:end] + name + source[end:])
    markers = {start+16 for start, _, is_shared in document.motion_spans if is_shared}
    shift = lambda value: value + (len(name) if value >= end else 0)
    for pointer in document.relocations.values():
        if pointer.offset in markers or pointer.offset == base+88:
            continue
        target = end if pointer.base == base and pointer.target == end else shift(pointer.target)
        delta = target-shift(pointer.base)
        if delta % pointer.unit:
            raise MotionWriteError('Name relocation violates pointer alignment')
        struct.pack_into('<'+pointer.format, output, shift(pointer.offset), delta//pointer.unit)
    struct.pack_into('<Q', output, base+88, end-base)
    struct.pack_into('<I', output, base+12, end-base+len(name))
    if shared:
        struct.pack_into('<Q', output, base+16, end-base+len(name))
    return CODEC.parse(bytes(output))


def retime_motion(document, motion_id, frames):
    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise MotionWriteError('Target frames must be a positive integer')
    model = CODEC.parse(CODEC.write(document))
    validate_slot_order(model)
    matches = [s for s in model.slots if s.motion_id == motion_id and s.payload]
    if len(matches) != 1:
        raise MotionWriteError('MotionID must resolve to one embedded motion')
    slot = matches[0]
    aliases = [s.motion_id for s in model.slots if s.payload is slot.payload]
    if len(aliases) != 1:
        raise MotionWriteError(f'Motion shares its payload with {aliases}; duplicate it before retiming')
    motion = slot.payload.value
    if not math.isfinite(motion.end_frame) or motion.end_frame <= 0:
        raise MotionWriteError('Source motion must have a positive finite duration')
    factor = frames / motion.end_frame
    seen = set()
    for node in motion.animation_nodes:
        for track in (node.translation, node.rotation, node.scale):
            if track is None or id(track) in seen:
                continue
            seen.add(id(track))
            if len(track.frames) == 1:
                track.frames = [round_frame(track.frames[0]*factor)]
                continue
            values = [sample_track(track, frame/factor, PROFILE.sampling_policy.rotation_interpolation)
                      for frame in range(frames+1)]
            track.frames, track.values = list(range(frames+1)), values
    clips = set()
    for sequence in [*motion.sequences, *slot.overrides]:
        if sequence.clip and id(sequence.clip) not in clips:
            scale_clip(sequence.clip, factor)
            clips.add(id(sequence.clip))
    motion.end_frame = float(frames)
    if motion.raw_start_frame >= 0:
        motion.raw_start_frame *= factor
    if motion.raw_end_frame >= 0:
        motion.raw_end_frame *= factor
    output = CODEC.write(model)
    reopened = restore_name_tail(CODEC.parse(output), motion_id)
    output = reopened.source
    if CODEC.write(reopened) != output:
        raise MotionWriteError('Retimed MOTLIST did not roundtrip')
    actual = next(s.payload.value for s in reopened.slots if s.motion_id == motion_id)
    if actual.end_frame != frames:
        raise MotionWriteError('Retimed motion duration differs from the request')
    for expected_node, actual_node in zip(motion.animation_nodes, actual.animation_nodes, strict=True):
        for channel in ('translation', 'rotation', 'scale'):
            expected, saved = getattr(expected_node, channel), getattr(actual_node, channel)
            if expected is None:
                if saved is not None:
                    raise MotionWriteError('Retiming created a channel')
                continue
            if expected.frames != saved.frames:
                raise MotionWriteError('Retimed channel frames differ after serialization')
            for want, got in zip(expected.values, saved.values, strict=True):
                signs = (1, -1) if expected.family == TrackFamily.QUATERNION else (1,)
                if not any(all(math.isclose(a, sign*b, abs_tol=2e-5, rel_tol=2e-6)
                               for a, b in zip(want, got, strict=True)) for sign in signs):
                    raise MotionWriteError('Retimed channel values differ after serialization')
    return reopened, factor
