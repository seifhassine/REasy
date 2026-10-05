"""Offset-independent native semantics for offline writer verification."""
from dataclasses import asdict
import struct

from .mhr_tree import RiseTree


def motion_value(motion):
    if isinstance(motion,RiseTree):return motion.signature()
    joints=motion.skeleton.joints if motion.skeleton else []
    indexes={id(j):i for i,j in enumerate(joints)}
    def track(t):return None if t is None else (int(t.family),tuple(t.frames),tuple(t.values),t.max_frame)
    return (motion.name,motion.end_frame,motion.looping,motion.raw_start_frame,motion.raw_end_frame,
            motion.frames_per_second,motion.character_path,
            tuple((j.name,indexes.get(id(j.parent)),tuple(indexes[id(c)] for c in j.children),j.translation,j.rotation,j.binding_hash) for j in joints),
            tuple((n.joint.name,n.weight,track(n.translation),track(n.rotation),track(n.scale)) for n in motion.animation_nodes),
            tuple(repr(asdict(s)) for s in motion.sequences),
            tuple((g.block_count,g.point_count,g.start,g.end,tuple(g.frames)) for g in motion.sync_points),
            tuple(tuple(tuple(p) for p in c.points) for c in motion.frame_curves),
            repr(asdict(motion.append)) if motion.append else None)


def slot_value(document,index):
    slot=document.slots[index];rows=struct.unpack_from('<Q',document.source,24)[0]
    # The override pointer is layout-derived. Every other native row byte is
    # retained, including unknown words and all slot flags.
    return (document.source[rows+index*72+8:rows+(index+1)*72],
            motion_value(slot.payload.value) if slot.payload else None,
            tuple(repr(asdict(s)) for s in slot.overrides))


def document_value(document):
    return (document.name,document.base_motion_list_path,tuple(slot_value(document,i) for i in range(len(document.slots))))


def assert_other_slots(before,after,changed_ids):
    old={s.motion_id:slot_value(before,i) for i,s in enumerate(before.slots) if s.motion_id not in changed_ids}
    new={s.motion_id:slot_value(after,i) for i,s in enumerate(after.slots) if s.motion_id not in changed_ids}
    if repr(old)!=repr(new):raise AssertionError('Unrelated slot, rig, motion, tree or event semantics changed')
