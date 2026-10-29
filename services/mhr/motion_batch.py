"""Batch Rise MOTLIST/MOT/MTRE/CLIP/MOTBANK writer from explicit neutral input."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_storage import MhrMotion,MhrJoint,MhrSyncPointGrid,MhrFrameCurve
from file_handlers.motion.mot.model import Skeleton,AnimationNode,KeyTrack,TrackFamily
from file_handlers.motion.mhr_generate import build_motion
from file_handlers.motion.mhr_tree import build_tree,set_leaf,RiseMotionReference
from file_handlers.motion.mhr_structure import Owner,copy_sequences,delete_sequences,edit_clip
from file_handlers.motion.mhr_batch import (slot_index,install_payload,delete_slot,bind_slot,copy_event,audit_shared_payloads,audit_shared_sequences)
from file_handlers.motion.errors import MotionWriteError
from file_handlers.motion.mot_tree.model import TreeLink,TreeLinkType
from file_handlers.motbank.motbank_file import MotbankFile,MotlistItem
from file_handlers.uvar.uvar_file import UVarFile


def neutral_motion(spec,skeleton_spec,contract=None):
    if skeleton_spec.get('coordinates')!='rise_local':raise MotionWriteError('Skeleton/track coordinates must explicitly be rise_local')
    rows=skeleton_spec['joints']
    if contract:
        assets=[a for a in contract['assets'] if a['asset']==skeleton_spec['asset']]
        if len(assets)!=1:raise MotionWriteError('Select one canonical skeleton contract asset')
        expected=sorted(assets[0]['joints'],key=lambda j:j['mesh_joint_index'])
        actual=[(j['name'],None if j['parent'] is None else rows[j['parent']]['name']) for j in rows]
        if actual!=[(j['target_name'],j['target_parent']) for j in expected]:
            raise MotionWriteError('Skeleton order/name/parent differs from imported mesh contract')
    joints=[MhrJoint(r['name'],translation=tuple(r['translation']),rotation=tuple(r['rotation']),binding_hash=0) for r in rows]
    for j,row in zip(joints,rows):
        if len(j.translation)!=3 or len(j.rotation)!=4:raise MotionWriteError('Invalid rest transform dimensions')
        if row['parent'] is not None:
            if type(row['parent']) is not int or not 0<=row['parent']<len(joints):raise MotionWriteError('Invalid joint parent index')
            j.parent=joints[row['parent']];j.parent.children.append(j)
    motion=MhrMotion(spec['name'],end_frame=spec['end_frame'],looping=spec['looping'],
                     raw_start_frame=spec['raw_start_frame'],raw_end_frame=spec['raw_end_frame'],
                     skeleton=Skeleton(joints),frames_per_second=spec['fps'])
    motion.loop_start_frame=spec.get('loop_start_frame',0)
    motion.character_path=spec.get('jmap_path')
    by_name={j.name:j for j in joints}
    for row in spec['tracks']:
        node=AnimationNode(by_name[row['joint']],row.get('weight',1))
        for attr,family in (('translation',TrackFamily.VECTOR3),('rotation',TrackFamily.QUATERNION),('scale',TrackFamily.VECTOR3)):
            if attr in row:
                channel=row[attr];setattr(node,attr,KeyTrack(family,channel['frames'],[tuple(v) for v in channel['values']]))
        motion.animation_nodes.append(node)
    motion.sync_points=[MhrSyncPointGrid(g['block_count'],g['point_count'],g['frames'],g['start'],g['end']) for g in spec.get('sync_points',[])]
    motion.frame_curves=[MhrFrameCurve(c['points']) for c in spec.get('frame_curves',[])]
    return motion


def tree_from_template(template,spec):
    tree=deepcopy(template);tree.name=spec['name'];tree.nodes=[];tree.links=[];tree.motion_references=[]
    for row in spec['nodes']:
        node=deepcopy(template.nodes[row['template_node']]);node.authored_id=row['authored_id'];node.name=row['name']
        for key,value in row.get('parameters',{}).items():
            matches=[p for p in node.parameters if p.name==key or f'{p.name_hash:08x}'==key.lower()]
            if len(matches)!=1:raise MotionWriteError('Tree parameter identity is unresolved')
            matches[0].value=value
        tree.nodes.append(node)
    ids={n.authored_id:n for n in tree.nodes}
    tree.root=ids[spec['root']]
    for row in spec['links']:
        tree.links.append(TreeLink(ids[row['input']],row['input_pin'],ids[row['output']],row['output_pin'],
                                  TreeLinkType(row['type']),bytes.fromhex(row['output_guid']) if row.get('output_guid') else None))
    for row in spec['motion_references']:
        tree.motion_references.append(RiseMotionReference(ids[row['node']],row['bank_id'],row['motion_id']))
        set_leaf(tree,ids[row['node']],row['bank_id'],row['motion_id'])
    return tree


def validate_references(bank,documents,uvar=None,paths=None):
    if len({i.bank_id for i in bank.items})!=len(bank.items):raise MotionWriteError('Ambiguous MOTBANK identities')
    if set(documents)!={i.bank_id for i in bank.items}:raise MotionWriteError('Every MOTBANK entry requires one supplied document')
    if paths is not None:
        for item in bank.items:
            if item.path.replace('\\','/').lower()!=paths[item.bank_id].replace('\\','/').lower():
                raise MotionWriteError('MOTBANK path disagrees with document resource_path')
    slots={b:{s.motion_id for s in m.slots} for b,m in documents.items()}
    variables={}
    def collect(file):
        for variable in file.variables:
            if variable.guid.bytes in variables:raise MotionWriteError('Ambiguous bank UVAR GUID')
            variables[variable.guid.bytes]=(file.header.name,variable.name)
        for embedded in file.embedded_uvars:collect(embedded)
    if uvar is not None:collect(uvar)
    for model in documents.values():
        for _,_,tree in model.tree_spans:
            for ref in tree.motion_references:
                if ref.bank_id not in slots or ref.motion_id not in slots[ref.bank_id]:
                    raise MotionWriteError(f'Unresolved MTRE leaf BankID={ref.bank_id} MotionID={ref.motion_id}')
            for link in tree.links:
                if link.link_type==TreeLinkType.PARAMETER and link.output_guid not in variables:
                    raise MotionWriteError('Unresolved MTRE parameter GUID; supply the bank UVAR dependency')
                if link.link_type==TreeLinkType.PARAMETER:
                    label=link.output_node.parameters[0].value.replace('\\','/').rsplit('/',1)[-1].removesuffix('.uvar')
                    if variables[link.output_guid][0]!=label:raise MotionWriteError('MTRE variable group disagrees with bank UVAR group')


def execute_plan(plan):
    if plan['schema_version']!=1:raise MotionWriteError('Unsupported batch input schema')
    docs={name:CODEC.parse(Path(row['template']).read_bytes(),label=name) for name,row in plan['documents'].items()}
    contract=json.loads(Path(plan['skeleton_contract']).read_text(encoding='utf8')) if plan.get('skeleton_contract') else None
    for op in plan['operations']:
        key=op['document'];document=docs[key];kind=op['operation']
        if kind=='motion':
            motion=neutral_motion(op['motion'],plan['skeleton'],contract)
            if op.get('append_template'):
                source=op['append_template'];donor=docs[source['document']]
                motion.append=deepcopy(donor.slots[slot_index(donor,source['motion_id'])].payload.value.append)
                if motion.append is None:raise MotionWriteError('MotionAppend template is absent')
                if 'class_authored_ids' in source:
                    if len(source['class_authored_ids'])!=len(motion.append.classes):raise MotionWriteError('Supply every MotionAppend class identity')
                    for cls,identity in zip(motion.append.classes,source['class_authored_ids']):cls.authored_id=identity
            templates=[]
            for seq in op.get('sequence_templates',[]):
                donor=docs[seq['document']];owner=Owner(donor,seq['motion_id'],seq['scope'])
                templates.append((donor,owner.one(sequence=seq['sequence'])))
            rows=plan['skeleton']['joints']
            payload=build_motion(motion,sequence_templates=templates,translation_w=[j['translation_w'] for j in rows],joint_unknown=[j['reserved_word'] for j in rows])
            document=install_payload(document,payload,op['motion_id'],template_id=op['template_id'],replace=op.get('replace',False))
        elif kind=='tree':
            template=document.slots[slot_index(document,op['template_id'])].payload.value
            tree=tree_from_template(template,op['tree'])
            document=install_payload(document,build_tree(tree),op['motion_id'],template_id=op['template_id'],replace=op.get('replace',False))
        elif kind=='event':
            if op['clock']!='target_motion_frames':raise MotionWriteError('Event frames must explicitly use the target motion clock; convert source clip frames before writing')
            source=op['source'];kwargs={k:v for k,v in op.items() if k not in ('document','operation','clock','source','slot_options')}
            document=copy_event(document,donor=docs[source['document']],source_id=source['motion_id'],
                node_index=source['node_index'],source_sequence=source['sequence'],source_scope=source['scope'],**kwargs)
        elif kind=='copy_sequences':
            source=op['source']
            document=copy_sequences(document,source['motion_id'],op['motion_id'],donor=docs[source['document']],
                positions=source['positions'],source_scope=source['scope'],target_scope=op['scope'],
                shift=op.get('shift',0),replace=op.get('replace',False))
        elif kind=='delete_sequences':
            document=delete_sequences(document,op['motion_id'],positions=op['positions'],scope=op['scope'])
        elif kind=='clip_edit':
            kwargs={k:v for k,v in op.items() if k not in ('document','operation','edit','slot_options')}
            document=edit_clip(document,operation=op['edit'],**kwargs)
        elif kind=='delete_slot':document=delete_slot(document,op['motion_id'])
        elif kind=='bind_slot':document=bind_slot(document,op['motion_id'],op['target_id'])
        else:raise MotionWriteError(f'Unsupported batch operation {kind}')
        if op.get('slot_options'):
            slot=document.slots[slot_index(document,op['motion_id'])]
            for attr,value in op['slot_options'].items():
                if attr not in ('flags','tag_hash'):raise MotionWriteError('Unsupported slot option')
                setattr(slot,attr,value)
        docs[key]=document
    bank=None
    if 'bank' in plan:
        spec=plan['bank'];raw=Path(spec['template']).read_bytes()
        if struct.unpack_from('<I',raw)[0]!=3 or any(raw[8:16]):raise MotionWriteError('Unsupported MOTBANK version/header flags')
        bank=MotbankFile();bank.read(raw)
        for row in spec.get('items',[]):
            found=[i for i in bank.items if i.bank_id==row['bank_id']]
            if len(found)>1:raise MotionWriteError('Ambiguous bank ID')
            if found:item=found[0]
            else:item=MotlistItem(bank_id=row['bank_id']);bank.items.append(item)
            item.path=row['path'];item.bank_type=row['bank_type'];item.bank_type_mask_bits=row['bank_type_mask_bits']
        bank.items[:]=[i for i in bank.items if i.bank_id not in spec.get('delete_ids',[])]
        uvar=UVarFile(Path(spec['uvar_template']).read_bytes()) if spec.get('uvar_template') else None
        if len({r['bank_id'] for r in plan['documents'].values()})!=len(docs):raise MotionWriteError('Ambiguous document BankIDs')
        validate_references(bank,{row['bank_id']:docs[name] for name,row in plan['documents'].items()},uvar,
                            {row['bank_id']:row['resource_path'] for row in plan['documents'].values()})
    # Validate all documents before writing any output.
    output={}
    targets=[Path(r['output']).resolve() for r in plan['documents'].values()]
    if bank:targets.append(Path(plan['bank']['output']).resolve())
    if len(set(targets))!=len(targets):raise MotionWriteError('Batch output paths must be distinct')
    for name,document in docs.items():
        data=CODEC.write(document);reopened=CODEC.parse(data)
        if CODEC.write(reopened)!=data:raise MotionWriteError('Batch output is not stable')
        output[plan['documents'][name]['output']]=data
    if bank:
        data=bank.write();reopened=MotbankFile();reopened.read(data)
        if reopened.write()!=data:raise MotionWriteError('MOTBANK output is not stable')
        output[plan['bank']['output']]=data
    return output,dict(resources=[dict(path=p,bytes=len(d),sha256=hashlib.sha256(d).hexdigest()) for p,d in output.items()],
                       shared_payloads={name:audit_shared_payloads(d) for name,d in docs.items()},
                       shared_sequences={name:audit_shared_sequences(d) for name,d in docs.items()},
                       semantics='Native structure and explicit target-frame values; runtime consumers and external audio/hit bindings are not inferred')


def build_plan(plan, *, plan_path=None, report_path=None, dry_run=False):
    """Build and verify every resource before publishing the requested batch."""
    from services.file_io import check_output, publish_files
    output, report = execute_plan(plan)
    inputs = [row['template'] for row in plan['documents'].values()]
    inputs += [plan_path, plan.get('skeleton_contract')]
    if plan.get('bank'):
        inputs += [plan['bank']['template'], plan['bank'].get('uvar_template')]
    for path in output:
        check_output(path, inputs)
    if report_path is not None:
        check_output(report_path, [*inputs, *output])
        output[report_path] = (json.dumps(report, indent=2) + '\n').encode('utf-8')
    if not dry_run:
        publish_files(output, inputs=inputs)
    return {**report, 'dry_run': dry_run}
