"""Headless Rise operations. Every structural operation returns fresh bindings."""
import struct

from .binary import align_up
from .errors import MotionWriteError
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from .mhr_structure import Layout,Splice,materialize,Owner,private_motion


def slot_index(document,motion_id):
    found=[i for i,s in enumerate(document.slots) if s.motion_id==motion_id]
    if len(found)!=1:raise MotionWriteError(f'MotionID {motion_id} resolves to {len(found)} slots')
    return found[0]


def audit_shared_payloads(document):
    groups={}
    for slot in document.slots:
        if slot.payload is not None:groups.setdefault(id(slot.payload),[]).append(slot.motion_id)
    return list(groups.values())


def audit_shared_sequences(document):
    """Physical wrapper/CLIP/metadata sharing with every owning slot and scope."""
    raw=document.source;pointers,rows=struct.unpack_from('<QQ',raw,16);groups={}
    for i,slot in enumerate(document.slots):
        base=struct.unpack_from('<Q',raw,pointers+i*8)[0]
        scopes=[('override',0,struct.unpack_from('<Q',raw,rows+i*72)[0],raw[rows+i*72+23])]
        if base and raw[base+4:base+8]==b'mot ':
            scopes.append(('motion',base,base+struct.unpack_from('<Q',raw,base+48)[0],raw[base+116]))
        for scope,origin,table,count in scopes:
            for j in range(count):
                wrapper=origin+struct.unpack_from('<Q',raw,table+j*8)[0]
                _,clip,tracks=struct.unpack_from('<3Q',raw,wrapper)
                owner=dict(motion_id=slot.motion_id,scope=scope,sequence=j)
                for kind,offset in (('wrapper',wrapper),('clip',origin+clip),('metadata',origin+tracks)):
                    groups.setdefault((kind,offset),[]).append(owner)
    return [dict(kind=k,offset=p,owners=owners) for (k,p),owners in groups.items() if len(owners)>1]


def rewrite_slots(document, records, *, append_payload=None, payload_sources=None):
    """records are (MotionID, source slot index) pairs, preserving row metadata.

    A new payload can replace exactly one row's binding. Source payloads are
    removed only when their last slot reference disappears; anchored shared rigs
    require an explicit independent payload before deleting their anchor.
    """
    document=materialize(document);raw=document.source
    pointers,rows=struct.unpack_from('<QQ',raw,16)
    ids=[i for i,_ in records]
    if not records or len(ids)!=len(set(ids)) or any(type(i) is not int or not 0<=i<=65535 for i in ids):
        raise MotionWriteError('Slot IDs must be distinct unsigned 16-bit integers')
    records=sorted(records)
    if any(type(i) is not int or not 0<=i<len(document.slots) for _,i in records):
        raise MotionWriteError('Slot row template index is out of range')
    old_addresses=[struct.unpack_from('<Q',raw,pointers+i*8)[0] for i in range(len(document.slots))]
    payload_sources=payload_sources or {}
    addresses=[old_addresses[payload_sources.get(sid,i)] for sid,i in records]
    new_id,payload = append_payload if append_payload else (None,None)
    if payload is not None:
        if ids.count(new_id)!=1 or len(payload)%16:raise MotionWriteError('New payload requires one target slot and 16-byte alignment')
        if payload[4:8] not in (b'mot ',b'mtre'):raise MotionWriteError('Expected native MOT or MTRE payload')
        addresses[[i for i,(sid,_) in enumerate(records) if sid==new_id][0]]=None
    retained=set(addresses)-{None,0}
    spans=[(s,e) for s,e,_ in document.motion_spans]+[(s,e) for s,e,_ in document.tree_spans]
    edits=[]
    rig_copies={}
    for start,end in sorted(spans):
        if start not in retained:
            current=next((s for s in document.motion_spans if s[0]==start),None)
            if current and not current[2]:
                following=[s for s in document.motion_spans if s[0]>=end]
                for next_start,next_end,shared in following:
                    if not shared:break
                    if next_start in retained:
                        from .mhr_editing import _copy_skeleton
                        data=bytearray(raw[next_start:next_end])
                        skeleton,joints=_copy_skeleton(raw,start,len(data))
                        struct.pack_into('<IQ',data,12,len(data)+len(skeleton),len(data))
                        struct.pack_into('<H',data,112,joints)
                        data.extend(skeleton)
                        rig_copies[next_start]=Splice(next_start,next_end,data)
                        break
            edits.append(Splice(start,end,bytearray()))
    edits.extend(rig_copies.values())
    ptr_end=align_up(pointers+len(document.slots)*8,16)
    row_end=rows+len(document.slots)*72
    if any(raw[pointers+len(document.slots)*8:ptr_end]):
        raise MotionWriteError('Unsupported nonzero slot-table alignment data')
    ptr_data=bytearray(align_up(len(records)*8,16))
    row_size=len(document.slots)*72+align_up((len(records)-len(document.slots))*72,16)
    row_data=bytearray(row_size)
    edits.extend([Splice(pointers,ptr_end,ptr_data),Splice(rows,row_end,row_data)])
    if payload is not None:edits.append(Splice(rows,rows,bytearray(payload)))
    layout=Layout(document,edits);output=layout.build()
    new_ptrs=layout.position(pointers);new_rows=layout.position(rows)
    added=layout.position(rows,before=True) if payload is not None else None
    for i,((sid,source),address) in enumerate(zip(records,addresses)):
        row=bytearray(raw[rows+source*72:rows+(source+1)*72])
        over=struct.unpack_from('<Q',row)[0]
        if over:struct.pack_into('<Q',row,0,layout.position(over))
        struct.pack_into('<H',row,8,sid)
        if payload is not None and sid==new_id:
            # The row's private per-motion word cannot be inherited by a new
            # payload; overrides remain explicitly attached to the slot.
            struct.pack_into('<I',row,12,0)
            row[20]=2 if payload[4:8]==b'mtre' else 1
        elif sid in payload_sources:
            row[20]=raw[rows+payload_sources[sid]*72+20]
        struct.pack_into('<Q',output,new_ptrs+i*8,added if address is None else layout.position(address))
        output[new_rows+i*72:new_rows+(i+1)*72]=row
    struct.pack_into('<I',output,48,len(records))
    struct.pack_into('<QQ',output,16,new_ptrs,new_rows)
    result=CODEC.parse(bytes(output),label='rewritten v528 slots')
    if CODEC.write(result)!=bytes(output):raise MotionWriteError('Slot rewrite is not stable')
    return result


def install_payload(document,payload,motion_id,*,template_id,replace=False):
    template=slot_index(document,template_id)
    exists=any(s.motion_id==motion_id for s in document.slots)
    if exists!=replace:raise MotionWriteError('Explicit replacement must match target slot existence')
    plan=[(s.motion_id,i) for i,s in enumerate(document.slots) if s.motion_id!=motion_id]
    plan.append((motion_id,slot_index(document,motion_id) if replace else template))
    return rewrite_slots(document,plan,append_payload=(motion_id,payload))


def delete_slot(document,motion_id):
    index=slot_index(document,motion_id)
    return rewrite_slots(document,[(s.motion_id,i) for i,s in enumerate(document.slots) if i!=index])


def bind_slot(document,motion_id,target_id):
    slot_index(document,motion_id);target=slot_index(document,target_id)
    return rewrite_slots(document,[(s.motion_id,i) for i,s in enumerate(document.slots)],payload_sources={motion_id:target})


def replace_clip(document,motion_id,graph,metadata,*,sequence=None,category=None,scope='motion'):
    """Serialize a ClipGraph and all seven metadata words per root track."""
    document=detach_sequence(document,motion_id,sequence=sequence,category=category,scope=scope)
    owner=Owner(document,motion_id,scope);record=owner.one(sequence=sequence,category=category)
    if len(metadata)!=len(graph.clip.root.children) or any(len(row)!=28 for row in metadata):
        raise MotionWriteError('One complete 28-byte metadata row is required per root child')
    start=record.parsed.clip_offset
    data=graph.build(origin_offset=start,pointer_base=owner.base)
    tracks=b''.join(metadata);tracks+=bytes(-len(tracks)%16)
    if record.tracks!=record.parsed.physical_end:raise MotionWriteError('Unsupported CLIP/metadata ordering')
    end=align_up(record.tracks+len(record.sequence.tracks)*28,16)
    layout=Layout(document,[Splice(start,end,bytearray(data+tracks))],motion_base=owner.base,
                  pointer_overrides={record.wrapper+16:start+len(data)-owner.base})
    output=layout.build();struct.pack_into('<I',output,layout.position(record.wrapper+28),len(metadata))
    result=CODEC.parse(bytes(output),label='replaced v43 event graph')
    if CODEC.write(result)!=bytes(output):raise MotionWriteError('CLIP replacement is not stable')
    return result


def detach_sequence(document,motion_id,*,sequence=None,category=None,scope='motion'):
    """Give the selected wrapper, CLIP, metadata and override table private ownership."""
    from .mhr_structure import _clone
    document=private_motion(materialize(document),motion_id,scope)
    owner=Owner(document,motion_id,scope);record=owner.one(sequence=sequence,category=category)
    selected=owner.records.index(record);raw=document.source
    insertion=owner.insertion;padding=-insertion%16
    table_bytes=align_up(len(owner.records)*8,16) if scope=='override' else 0
    wrapper=insertion+padding+table_bytes
    cloned=_clone(document,record,wrapper,owner.base)
    data=bytearray(padding+table_bytes)+cloned;data.extend(bytes(-len(data)%16))
    layout=Layout(document,[Splice(insertion,insertion,data)],motion_base=owner.base)
    output=layout.build()
    if scope=='override':
        new_table=insertion+padding
        for i,r in enumerate(owner.records):
            struct.pack_into('<Q',output,new_table+i*8,wrapper if i==selected else layout.position(r.wrapper))
        struct.pack_into('<Q',output,layout.position(owner.pointer_offset),new_table)
    else:
        struct.pack_into('<Q',output,layout.position(owner.table+selected*8),wrapper-layout.position(owner.base))
    return CODEC.parse(bytes(output),label='private sequence ownership')


def copy_event(document,motion_id,*,donor,source_id,node_index,authored_id,
               sequence=None,category=None,scope='motion',source_sequence=None,source_category=None,
               source_scope='motion',shift=0.0,properties=None,filter_pages=None,clip_total_frame=None):
    """Copy a native consumer template; values/timing/metadata identity are explicit.

    properties maps a unique leaf name to {frames, values, start, end}. It does
    not infer hit-data IDs, audio trigger IDs, enum equivalence or loop behavior.
    """
    from .mot_clip.edit_v43 import ClipGraph
    from .mot_clip.model import CONTAINER_PROPERTY_TYPES
    document=materialize(document);donor=materialize(donor)
    target=Owner(document,motion_id,scope).one(sequence=sequence,category=category)
    source=Owner(donor,source_id,source_scope).one(sequence=source_sequence,category=source_category)
    graph=ClipGraph(document.source,target.parsed);other=ClipGraph(donor.source,source.parsed)
    if not 0<node_index<len(other.nodes) or other.nodes[node_index] not in other.clip.root.children:
        raise MotionWriteError('Event template must be a root child with metadata')
    if type(authored_id) is not int or not 0<=authored_id<=0xffffffff:
        raise MotionWriteError('Event authored ID must be an unsigned 32-bit integer')
    if authored_id!=0xffffffff and any(t.authored_id==authored_id for t in target.sequence.tracks):
        raise MotionWriteError('Event authored ID is already used in target sequence')
    graph.copy_node(other,node_index,shift=shift)
    node=graph.clip.root.children[-1]
    def leaves(prop):
        if prop.property_type not in CONTAINER_PROPERTY_TYPES:yield prop
        for child in prop.children:yield from leaves(child)
    all_props=[p for top in node.properties for p in leaves(top)]
    for name,edit in (properties or {}).items():
        matches=[p for p in all_props if p.name==name]
        if len(matches)!=1:raise MotionWriteError(f'Event property {name} resolves to {len(matches)} leaves')
        prop=matches[0]
        for key in ('frames','values'):
            if key in edit and len(edit[key])!=len(prop.keys):raise MotionWriteError('Event key replacement must match template count; use ClipGraph for key edits')
        for i,key in enumerate(prop.keys):
            if 'frames' in edit:key.frame=edit['frames'][i]
            if 'values' in edit:key.value=edit['values'][i]
        if 'start' in edit:prop.start_frame=edit['start']
        if 'end' in edit:prop.end_frame=edit['end']
    if clip_total_frame is not None:graph.clip.total_frame=clip_total_frame
    if any(k.frame>graph.clip.total_frame for p in all_props for k in p.keys if not k.offset_frame and int(k.interpolation) in (1,2,3)):
        raise MotionWriteError('New event exceeds CLIP total frame; supply an explicit clip_total_frame')
    source_track=other.clip.root.children.index(other.nodes[node_index])
    metadata=[document.source[target.tracks+i*28:target.tracks+(i+1)*28] for i in range(len(target.sequence.tracks))]
    new=bytearray(donor.source[source.tracks+source_track*28:source.tracks+(source_track+1)*28])
    struct.pack_into('<I',new,0,authored_id)
    if filter_pages is not None:
        if len(filter_pages)!=6:raise MotionWriteError('Six event filter pages are required')
        struct.pack_into('<6I',new,4,*filter_pages)
    metadata.append(bytes(new))
    return replace_clip(document,motion_id,graph,metadata,sequence=sequence,category=category,scope=scope)


class RiseEditSession:
    """Byte snapshots give exact undo, including source padding and sharing."""
    def __init__(self,document):
        self.document=materialize(document);self._history=[]

    def apply(self,edit,*args,**kwargs):
        before=CODEC.write(self.document)
        candidate=edit(self.document,*args,**kwargs)
        raw=CODEC.write(candidate);reopened=CODEC.parse(raw)
        if CODEC.write(reopened)!=raw:raise MotionWriteError('Edit did not stabilize')
        self._history.append(before);self.document=reopened
        return reopened

    def undo(self):
        if not self._history:raise MotionWriteError('No edit to undo')
        self.document=CODEC.parse(self._history.pop(),label='undone edit')
        return self.document
