"""Reproduce the em100 native writer fixture from an extracted, hashed corpus.

Usage: python tests/fixtures/mhr_motion_writer.py --source SOURCE_ROOT --manifest SOURCE_MANIFEST --output NEW_DIRECTORY
SOURCE_ROOT contains natives/stm/enemy/em100/00; inputs are never modified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

REASY=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REASY))
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as C
from file_handlers.motion.mhr_batch import RiseEditSession,copy_event,audit_shared_payloads,audit_shared_sequences
from file_handlers.motion.mhr_structure import Owner
from file_handlers.motion.mhr_validation import document_value,assert_other_slots
from file_handlers.motbank.motbank_file import MotbankFile
from file_handlers.uvar.uvar_file import UVarFile
from tools.mhr_motion_writer import validate_references,execute_plan

BANKS={0:'00',2:'02',3:'03',4:'04',5:'05',22:'22',50:'tree'}
def sha(data):return hashlib.sha256(data).hexdigest()
def save(path,data):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
def resource(bank):return f'enemy/offline_writer_probe/probe_{bank:02d}.motlist'

def run(source, manifest, output_directory):
    source_manifest=json.loads(manifest.read_text(encoding='utf8'))
    for row in source_manifest['resources']:
        assert sha((source/row['path']).read_bytes())==row['sha256'], row['path']
    output_directory.mkdir(parents=True, exist_ok=False)
    corpus=source/'natives/stm/enemy/em100/00/mot'
    docs={b:C.parse((corpus/f'em100_00_{suffix}.motlist.528').read_bytes()) for b,suffix in BANKS.items()}
    baseline=[]
    for b,doc in docs.items():
        raw=C.write(doc);again=C.parse(raw)
        assert raw==doc.source and C.write(again)==raw
        assert repr(document_value(doc))==repr(document_value(again))
        dest=output_directory/f'baseline/em100_00_{BANKS[b]}.motlist.528';save(dest,raw)
        baseline.append(dict(bank_id=b,slots=len(doc.slots),unique_motions=len(doc.motion_spans),trees=len(doc.tree_spans),
            byte_exact=True,stable=True,sha256=sha(raw),path=str(dest),shared_payloads=audit_shared_payloads(doc),
            shared_sequences=audit_shared_sequences(doc)))
        print(f'Baseline bank {b}: {len(doc.slots)} slots byte exact',flush=True)
    bank=MotbankFile();bank.read((corpus/'em100_00_bank.motbank.3').read_bytes())
    uvar_path=corpus.parent/'user_variables/em100_00_mot.uvar.3'
    uvar=UVarFile(uvar_path.read_bytes());validate_references(bank,docs,uvar)
    tree=docs[50].slots[0].payload.value
    ids={id(n):1001+i for i,n in enumerate(tree.nodes)}
    tree_spec=dict(name='REasyOfflineTreeProbe',root=ids[id(tree.root)],
        nodes=[dict(template_node=i,authored_id=ids[id(n)],name=n.name,
            parameters={f'{p.name_hash:08x}':p.value for p in n.parameters}) for i,n in enumerate(tree.nodes)],
        links=[dict(input=ids[id(l.input_node)],input_pin=l.input_pin,output=ids[id(l.output_node)],output_pin=l.output_pin,
            type=int(l.link_type),output_guid=l.output_guid.hex() if l.output_guid else None) for l in tree.links],
        motion_references=[dict(node=ids[id(r.node)],bank_id=r.bank_id,motion_id=r.motion_id) for r in tree.motion_references])
    leaf_index=next(i for i,n in enumerate(tree.nodes) if any(r.node is n for r in tree.motion_references))
    tree_spec['nodes'].append(dict(template_node=leaf_index,authored_id=2001,name='OfflineAdditionalLeaf',parameters={}))
    tree_spec['links'].append(dict(input=tree_spec['root'],input_pin=7,output=2001,output_pin=0,type=1))
    tree_spec['motion_references'].append(dict(node=2001,bank_id=0,motion_id=60000))
    skeleton=dict(coordinates='rise_local',asset='OfflineFixture',joints=[
        dict(name='Root',parent=None,translation=[0,0,0],rotation=[0,0,0,1],translation_w=0,reserved_word=0),
        dict(name='ProbeJoint',parent=0,translation=[0,1,0],rotation=[0,0,0,1],translation_w=0,reserved_word=0)])
    contract=dict(assets=[dict(asset='OfflineFixture',joints=[dict(mesh_joint_index=0,target_name='Root',target_parent=None),
        dict(mesh_joint_index=1,target_name='ProbeJoint',target_parent='Root')])])
    (output_directory/'offline_fixture_contract.json').write_text(json.dumps(contract,indent=2),encoding='utf8')
    plan=dict(schema_version=1,documents={},skeleton=skeleton,skeleton_contract=str(output_directory/'offline_fixture_contract.json'),operations=[],
        bank=dict(template=str(corpus/'em100_00_bank.motbank.3'),output=str(output_directory/'cli_candidates/natives/stm/enemy/offline_writer_probe/probe_bank.motbank.3'),
            uvar_template=str(uvar_path),items=[]))
    for b in (*BANKS,51):
        plan['documents'][f'bank{b}']=dict(template=str(corpus/f'em100_00_{BANKS.get(b,"tree")}.motlist.528'),
            output=str(output_directory/f'cli_candidates/natives/stm/{resource(b)}.528'),bank_id=b,resource_path=resource(b))
        original=next((i for i in bank.items if i.bank_id==b),None)
        plan['bank']['items'].append(dict(bank_id=b,path=resource(b),bank_type=original.bank_type if original else 0,
            bank_type_mask_bits=original.bank_type_mask_bits if original else 0))
    plan['operations'].append(dict(document='bank0',operation='motion',motion_id=60000,template_id=docs[0].slots[0].motion_id,
        motion=dict(name='REasyOfflineProbe',fps=60,end_frame=20,looping=True,raw_start_frame=0,raw_end_frame=20,
            tracks=[dict(joint='Root',translation=dict(frames=[0,20],values=[[0,0,0],[1.25,2,3]])),
                    dict(joint='ProbeJoint',rotation=dict(frames=[0],values=[[0,0,0,1]]))])))
    plan['operations'].append(dict(document='bank51',operation='tree',motion_id=60001,template_id=docs[50].slots[0].motion_id,tree=tree_spec))
    event_specs=[]
    for b,sid,scope,category,node_name,properties,shift,total in (
        (0,1,'motion',1,'snow.wwise.SoundTriggerTracks',{'_TriggerId':{'frames':[33.5],'values':[12345],'start':33.5,'end':33.5}},2,33.5),
        (2,26,'override',0,'snow.enemy.EnemyHitAttackTrack',{'_hitDataId':{'values':[7,7]}},-2,44)):
        owner=Owner(docs[b],sid,scope);record=owner.one(category=category)
        sequence=owner.records.index(record)
        idx=next(r.index for r in record.parsed.nodes if r.node.name==node_name)
        event_specs.append((b,sid,scope,category,idx,properties,shift,total))
        plan['operations'].append(dict(document=f'bank{b}',operation='event',clock='target_motion_frames',motion_id=sid,
            scope=scope,category=category,authored_id=0x11223344,properties=properties,shift=shift,clip_total_frame=total,
            source=dict(document=f'bank{b}',motion_id=sid,scope=scope,sequence=sequence,node_index=idx)))
    (output_directory/'batch_plan.json').write_text(json.dumps(plan,indent=2),encoding='utf8')
    output,cli_report=execute_plan(plan)
    reopened={}
    for name,row in plan['documents'].items():
        doc=C.parse(output[row['output']]);reopened[row['bank_id']]=doc
        if row['bank_id'] in docs:
            changed={0:{1,60000},2:{26}}.get(row['bank_id'],set())
            assert_other_slots(docs[row['bank_id']],doc,changed)
    rebuilt_bank=MotbankFile();rebuilt_bank.read(output[plan['bank']['output']])
    validate_references(rebuilt_bank,reopened,uvar,{row['bank_id']:row['resource_path'] for row in plan['documents'].values()})
    for path,data in output.items():save(Path(path),data)
    (output_directory/'cli_report.json').write_text(json.dumps(cli_report,indent=2),encoding='utf8')
    undos=[]
    for b,sid,scope,category,idx,properties,shift,total in event_specs:
        session=RiseEditSession(docs[b]);candidate=session.apply(copy_event,sid,donor=docs[b],source_id=sid,node_index=idx,
            authored_id=0x11223344,category=category,scope=scope,source_category=category,source_scope=scope,
            properties=properties,shift=shift,clip_total_frame=total)
        save(output_directory/f'undo_examples/bank{b}_edited.motlist.528',C.write(candidate))
        undone=C.write(session.undo());assert undone==docs[b].source
        save(output_directory/f'undo_examples/bank{b}_undone.motlist.528',undone)
        undos.append(dict(bank_id=b,motion_id=sid,sha256=sha(undone),exact=True))
    for row in source_manifest['resources']:assert sha((source/row['path']).read_bytes())==row['sha256']
    guid_rows=[]
    def collect(file):
        for v in file.variables:guid_rows.append(dict(group=file.header.name,name=v.name,uuid=str(v.guid),native_bytes=v.guid.bytes.hex()))
        for embedded in file.embedded_uvars:collect(embedded)
    collect(uvar)
    report=dict(generated_unix=time.time(),archives=source_manifest['archives'],precedence=source_manifest['precedence'],
        source_manifest=str(manifest),source_files_unchanged=True,
        corpus=dict(banks=7,slots=sum(len(d.slots) for d in docs.values()),unique_mots=sum(len(d.motion_spans) for d in docs.values()),
                    trees=sum(len(d.tree_spans) for d in docs.values()),leaf_references=sum(len(t.motion_references) for d in docs.values() for _,_,t in d.tree_spans)),
        baseline=baseline,bank_and_tree_reference_closure=True,uvar_variables=guid_rows,exact_undo_examples=undos,
        batch=dict(plan='batch_plan.json',report='cli_report.json',resources=len(output),stable=True,other_slots_unchanged=True,
                   bank_and_tree_reference_closure=True,added_bank_id=51,added_motion_id=60000,added_tree_id=60001,
                   fixture_bindings_game_ready=False),
        remaining_semantic_blockers=[
            'Canonical rest transforms, axis/unit conversion and source-to-target retargeting are caller responsibilities.',
            'Source clip/action clocks must be converted into target motion frames before event writes.',
            'Audio trigger/WEL/HIRC closure and explicit loop start/stop semantics have not been established.',
            'EnemyHitAttackTrack serializes hit-event IDs and timing, not collision geometry; hit-ID binding, enum equivalence and continuation/loop behavior need consumer evidence. A specific RCOL association is not established.',
            'Sync grid phase markers, frame/scalar curves, MotionAppend hashes and blend algorithms have unresolved consumer semantics.',
            'Nonempty MOT +0x20/+0x28 sections and unobserved MTRE classes/resources/tags are explicitly rejected.',
            'JMAP is an unchanged dependency; no in-game runtime or Glavenus content validation is claimed.'])
    (output_directory/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print('Validated baseline, 9 CLI candidates, isolated events, reference closure and exact undo',flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    args=parser.parse_args()
    run(args.source.resolve(), args.manifest.resolve(), args.output.resolve())


if __name__=='__main__':main()
