"""Native MOT 495 construction from neutral tracks, without retargeting."""
import math
import struct

from utils.hash_util import murmur3_hash_utf16le
from .binary import pad_to_alignment
from .errors import MotionWriteError
from .mhr_tracks import encode_track
from .mot.model import TrackFamily


def finite(values,label):
    if not all(math.isfinite(v) for v in values):raise MotionWriteError(f'{label} must be finite')


def build_motion(motion, *, sequence_templates=(), translation_w=(), joint_unknown=()):
    """Write an independent rig; sequence templates are (document, SequenceRecord).

    Coordinates/rest transforms must already satisfy the imported mesh contract.
    Translation W and reserved joint words are explicit construction inputs.
    Loop intros require a separately authored intro motion; MOT loop time only
    supports whole-motion looping in the validated Rise layout.
    """
    if motion.skeleton is None:raise MotionWriteError('A canonical skeleton is required')
    joints=motion.skeleton.joints
    if not 0<len(joints)<=65535 or len(translation_w)!=len(joints) or len(joint_unknown)!=len(joints):
        raise MotionWriteError('Every canonical joint requires translation W and reserved word')
    if motion.looping and motion.loop_start_frame:raise MotionWriteError('Rise looping intro must be a separate MOT')
    finite((motion.end_frame,motion.raw_start_frame,motion.raw_end_frame),'MOT frames')
    if motion.end_frame<0 or not 0<motion.frames_per_second<=0xffffffff:raise MotionWriteError('Invalid MOT duration/fps')
    if not motion.name or '\0' in motion.name:raise MotionWriteError('Invalid MOT name')
    names=[j.name for j in joints];hashes=[murmur3_hash_utf16le(n) for n in names]
    if len(set(names))!=len(names) or len(set(hashes))!=len(hashes):raise MotionWriteError('Canonical joints require unique names/hashes')
    indexes={id(j):i for i,j in enumerate(joints)}
    for j in joints:
        finite((*j.translation,*j.rotation),'Joint rest transform')
        if j.parent is not None and id(j.parent) not in indexes:raise MotionWriteError('Dangling canonical joint parent')
        visited=set();current=j
        while current:
            if id(current) in visited:raise MotionWriteError('Cyclic canonical skeleton')
            visited.add(id(current));current=current.parent
    if len(motion.animation_nodes)>65535:raise MotionWriteError('Too many animation nodes')
    seen=set()
    for n in motion.animation_nodes:
        if id(n.joint) not in indexes or id(n.joint) in seen:raise MotionWriteError('Invalid/duplicate animation joint binding')
        seen.add(id(n.joint))
        if not 0<=n.weight<=1:raise MotionWriteError('Invalid animation weight')
    out=bytearray(128)
    nt=len(out);out.extend(bytes(len(motion.animation_nodes)*12))
    for i,n in enumerate(motion.animation_nodes):
        pad_to_alignment(out,4);th=len(out)
        channels=[(bit,getattr(n,name),family) for bit,name,family in
                  ((1,'translation',TrackFamily.VECTOR3),(2,'rotation',TrackFamily.QUATERNION),(4,'scale',TrackFamily.VECTOR3))
                  if getattr(n,name) is not None]
        if not channels:raise MotionWriteError('Animation node has no channels')
        out.extend(bytes(20*len(channels)))
        flags=sum(bit for bit,_,_ in channels)|(round(n.weight*255)<<8)
        idx=indexes[id(n.joint)]
        struct.pack_into('<HHII',out,nt+i*12,idx,flags,hashes[idx],th)
        for k,(_,track,family) in enumerate(channels):
            if track.family!=family:raise MotionWriteError('Track family does not match channel')
            encoded,frames,values=encode_track(track)
            pad_to_alignment(out,4);fp=len(out);out.extend(frames)
            pad_to_alignment(out,4);vp=len(out);out.extend(values)
            struct.pack_into('<5I',out,th+k*20,encoded,len(track.frames),fp,vp,0)
    pad_to_alignment(out,16)
    jmap=0
    if motion.character_path:
        jmap=len(out);out.extend(motion.character_path.encode('utf-16le')+b'\0\0');pad_to_alignment(out,16)
    st=0
    if sequence_templates:
        if len(sequence_templates)>255:raise MotionWriteError('Too many MOT sequences')
        st=len(out);out.extend(bytes(len(sequence_templates)*8));pad_to_alignment(out,16)
        from .mhr_structure import _clone
        for i,(document,record) in enumerate(sequence_templates):
            struct.pack_into('<Q',out,st+i*8,len(out));out.extend(_clone(document,record,len(out),0))
    ap=0
    if motion.append is not None:
        from .mot.writer import MotV65Writer
        ap=len(out);MotV65Writer._write_append(None,out,motion);pad_to_alignment(out,16)
    sync=0
    if motion.sync_points:
        if len(motion.sync_points)>255:raise MotionWriteError('Too many sync grids')
        sync=len(out);out.extend(bytes(len(motion.sync_points)*8))
        for i,g in enumerate(motion.sync_points):
            if not 0<g.block_count<=255 or not 0<g.point_count<=255 or len(g.frames)!=g.block_count*g.point_count+1:
                raise MotionWriteError('Invalid sync grid dimensions')
            finite(g.frames,'Sync frames')
            struct.pack_into('<Q',out,sync+i*8,len(out))
            out.extend(struct.pack('<4B',g.block_count,g.point_count,g.start,g.end));out.extend(struct.pack(f'<{len(g.frames)}f',*g.frames))
    speed=0
    if motion.frame_curves:
        if len(motion.frame_curves)>65535:raise MotionWriteError('Too many speed curves')
        speed=len(out);out.extend(bytes(len(motion.frame_curves)*8))
        for i,curve in enumerate(motion.frame_curves):
            struct.pack_into('<Q',out,speed+i*8,len(out));out.extend(struct.pack('<I',len(curve.points)))
            for frame,rate in curve.points:
                finite((frame,rate),'Speed curve');out.extend(struct.pack('<2f',frame,rate))
            pad_to_alignment(out,8)
    name=len(out);out.extend(motion.name.encode('utf-16le')+b'\0\0');pad_to_alignment(out,16)
    skeleton=len(out);out.extend(bytes(16));jt=len(out);out.extend(bytes(len(joints)*80))
    struct.pack_into('<2Q',out,skeleton,jt,len(joints))
    children={id(j):[k for k in joints if k.parent is j] for j in joints}
    for i,j in enumerate(joints):
        sibling=children[id(j.parent)] if j.parent else [k for k in joints if k.parent is None]
        si=sibling.index(j);next_j=sibling[si+1] if si+1<len(sibling) else None
        first=children[id(j)][0] if children[id(j)] else None
        def ptr(item):return jt+indexes[id(item)]*80 if item else 0
        r=jt+i*80;np=len(out);out.extend(j.name.encode('utf-16le')+b'\0\0')
        struct.pack_into('<4Q8f2IQ',out,r,np,ptr(j.parent),ptr(first),ptr(next_j),*j.translation,translation_w[i],*j.rotation,i,hashes[i],joint_unknown[i])
    pad_to_alignment(out,16)
    struct.pack_into('<I4sII',out,0,495,b'mot ',0,len(out))
    struct.pack_into('<10Q',out,16,skeleton,nt,0,0,st,jmap,sync,speed,ap,name)
    struct.pack_into('<4fHHBBHIHH',out,96,motion.end_frame,0.0 if motion.looping else -1.0,
                     motion.raw_start_frame,motion.raw_end_frame,len(joints),len(motion.animation_nodes),len(sequence_templates),
                     len(motion.sync_points),len(motion.frame_curves),motion.frames_per_second,0,0)
    return bytes(out)
