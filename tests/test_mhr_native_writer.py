"""Real Rise relocation/graph/ownership regressions; explicit extracted corpus."""
from copy import deepcopy
import os
from pathlib import Path
import struct
import unittest

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as C
from file_handlers.motion.mhr_batch import (RiseEditSession,install_payload,delete_slot,bind_slot,copy_event,audit_shared_payloads)
from file_handlers.motion.mhr_tree import build_tree,set_leaf,RiseMotionReference
from file_handlers.motion.mhr_generate import build_motion
from file_handlers.motion.mhr_structure import Owner,edit_clip,copy_sequences,delete_sequences
from file_handlers.motion.mhr_validation import document_value,assert_other_slots,motion_value
from file_handlers.motion.mot_tree.model import TreeLink,TreeLinkType
from file_handlers.motion.errors import MotionParseError,MotionWriteError
from tools.mhr_motion_writer import validate_references,neutral_motion
from file_handlers.motbank.motbank_file import MotbankFile,MotlistItem
from file_handlers.uvar.uvar_file import UVarFile

CORPUS=Path(os.environ['REASY_MHR_NATIVE_CORPUS']) if os.environ.get('REASY_MHR_NATIVE_CORPUS') else None


@unittest.skipUnless(CORPUS and CORPUS.is_dir(),'Set REASY_MHR_NATIVE_CORPUS to extracted native em100 mot directory')
class NativeWriterTests(unittest.TestCase):
    def load(self,bank):
        return C.parse((CORPUS/f'em100_00_{bank}.motlist.528').read_bytes())

    def stable(self,model):
        raw=C.write(model);again=C.parse(raw)
        self.assertEqual(repr(document_value(model)),repr(document_value(again)))
        self.assertEqual(C.write(again),raw)
        return again

    def test_all_seven_banks_full_roundtrip_and_reference_closure(self):
        banks={b:self.load(f'{b:02d}' if b!=50 else 'tree') for b in (0,2,3,4,5,22,50)}
        self.assertEqual(sum(len(m.slots) for m in banks.values()),524)
        for b,m in banks.items():
            with self.subTest(bank=b):self.assertEqual(C.write(self.stable(m)),m.source)
        bank=MotbankFile();bank.read((CORPUS/'em100_00_bank.motbank.3').read_bytes())
        uvar=UVarFile((CORPUS.parent/'user_variables/em100_00_mot.uvar.3').read_bytes())
        validate_references(bank,banks,uvar)
        with self.assertRaisesRegex(MotionWriteError,'parameter GUID'):validate_references(bank,banks)
        self.assertEqual(len(banks[50].tree_spans),80)

    def test_auxiliary_tracks_strings_and_append_relocate_together(self):
        source=self.load('02');model=self.load('02');motion=model.slots[0].payload.value
        changed={s.motion_id for s in model.slots if s.payload is model.slots[0].payload}
        motion.name+='__nontrivial_long_name';motion.character_path+='__independent_path'
        motion.sync_points[0].frames[1]+=0.5;motion.frame_curves[0].points[1][0]+=0.5
        motion.append.classes[0].authored_id+=10
        motion.append.classes[0].arrays[0].name_hash^=0x100
        binding=next(t for t in model.tracks if t.base==model.motion_spans[0][0])
        values=list(binding.track.values[0]);values[0]+=0.125;binding.track.values[0]=tuple(values)
        result=self.stable(model);assert_other_slots(source,result,changed)
        self.assertEqual(result.slots[0].payload.value.sync_points[0].frames,motion.sync_points[0].frames)

    def test_tree_leaf_branch_add_delete_and_exact_undo(self):
        model=self.load('tree');session=RiseEditSession(model);original=model.source;sid=model.slots[0].motion_id
        def mutate(doc):
            t=doc.slots[0].payload.value;set_leaf(t,t.motion_references[0].node,2,86)
            t.name+='__long_tree_name'
            leaf=deepcopy(t.nodes[0]);leaf.authored_id=0x11223344;leaf.name='IndependentLeaf';t.nodes.append(leaf)
            t.motion_references.append(RiseMotionReference(leaf,2,87));set_leaf(t,leaf,2,87)
            t.links.append(TreeLink(t.root,7,leaf,0,TreeLinkType.MOTION))
            t.links[0].input_pin=4
            return doc
        result=session.apply(mutate);self.stable(result);assert_other_slots(model,result,{sid})
        self.assertEqual(len(result.slots[0].payload.value.nodes),len(model.slots[0].payload.value.nodes)+1)
        self.assertEqual(C.write(session.undo()),original)
        def remove(doc):
            t=doc.slots[0].payload.value;leaf=t.motion_references[0].node
            t.nodes.remove(leaf);t.motion_references[:]=[r for r in t.motion_references if r.node is not leaf]
            t.links[:]=[l for l in t.links if l.input_node is not leaf and l.output_node is not leaf]
            return doc
        self.stable(session.apply(remove));self.assertEqual(C.write(session.undo()),original)

    def test_slot_add_delete_replace_and_aliases(self):
        for bank in ('00','02','03','04','05','22','tree'):
            with self.subTest(bank=bank):
                model=self.load(bank);slot=model.slots[0];session=RiseEditSession(model)
                if bank=='tree':payload=build_tree(deepcopy(slot.payload.value))
                else:
                    motion=slot.payload.value;owner=Owner(model,slot.motion_id,'motion')
                    payload=build_motion(motion,sequence_templates=[(model,r) for r in owner.records],
                        translation_w=[0]*len(motion.skeleton.joints),joint_unknown=[0]*len(motion.skeleton.joints))
                result=session.apply(install_payload,payload,1000,template_id=slot.motion_id)
                self.assertEqual(len(result.slots),len(model.slots)+1);assert_other_slots(model,result,{1000})
                self.stable(result);self.stable(session.apply(delete_slot,1000))
                session.undo();self.assertEqual(C.write(session.undo()),model.source)
                result=session.apply(install_payload,payload,slot.motion_id,template_id=slot.motion_id,replace=True)
                assert_other_slots(model,result,{slot.motion_id});self.stable(result)
                self.assertEqual(C.write(session.undo()),model.source)
                target=model.slots[-1].motion_id;result=session.apply(bind_slot,slot.motion_id,target)
                assert_other_slots(model,result,{slot.motion_id});self.stable(result)
                self.assertTrue(any(slot.motion_id in g and target in g for g in audit_shared_payloads(result)))
                self.assertEqual(C.write(session.undo()),model.source)

    def test_sound_and_collision_event_identity_timing_and_shared_clip_isolation(self):
        for bank,sid,scope,category,name,edits in (
            ('00',1,'motion',1,'snow.wwise.SoundTriggerTracks',{'_TriggerId':{'frames':[33.5],'values':[12345],'start':33.5,'end':33.5}}),
            ('02',26,'override',0,'snow.enemy.EnemyHitAttackTrack',{'_hitDataId':{'values':[7,7]}})):
            model=self.load(bank);session=RiseEditSession(model);record=Owner(model,sid,scope).one(category=category)
            idx=next(r.index for r in record.parsed.nodes if r.node.name==name)
            result=session.apply(copy_event,sid,donor=model,source_id=sid,node_index=idx,authored_id=0x11223344,
                 category=category,scope=scope,source_category=category,source_scope=scope,
                 shift=2 if bank=='00' else -2,properties=edits,clip_total_frame=33.5 if bank=='00' else 44)
            assert_other_slots(model,result,{sid});self.stable(result)
            after=Owner(result,sid,scope).one(category=category)
            self.assertEqual(after.sequence.tracks[-1].authored_id,0x11223344)
            self.assertEqual(len(after.sequence.clip.root.children),len(record.sequence.clip.root.children)+1)
            changed=session.apply(edit_clip,sid,category=category,scope=scope,operation='node-delete',node_index=len(after.parsed.nodes)-1)
            self.assertEqual(len(Owner(changed,sid,scope).one(category=category).sequence.clip.root.children),len(record.sequence.clip.root.children))
            session.undo()
            self.assertEqual(C.write(session.undo()),model.source)

    def test_remove_native_shared_rig_anchor_preserves_remaining_motions(self):
        model=self.load('00');anchor=model.slots[0].payload
        removed={s.motion_id for s in model.slots if s.payload is anchor}
        self.assertTrue(any(shared for _,_,shared in model.motion_spans))
        session=RiseEditSession(model)
        for sid in sorted(removed):session.apply(delete_slot,sid)
        result=self.stable(session.document)
        assert_other_slots(model,result,removed)
        self.assertEqual({s.motion_id for s in result.slots},{s.motion_id for s in model.slots}-removed)
        for _ in removed:session.undo()
        self.assertEqual(C.write(session.document),model.source)

    def test_neutral_owned_rig_root_motion_and_canonical_binding_contract(self):
        skeleton=dict(coordinates='rise_local',asset='OfflineFixture',joints=[
            dict(name='Root',parent=None,translation=[0,0,0],rotation=[0,0,0,1],translation_w=0,reserved_word=0),
            dict(name='ProbeJoint',parent=0,translation=[0,1,0],rotation=[0,0,0,1],translation_w=0,reserved_word=0)])
        spec=dict(name='REasyOfflineProbe',fps=60,end_frame=20,looping=True,raw_start_frame=0,raw_end_frame=20,
            tracks=[dict(joint='Root',translation=dict(frames=[0,20],values=[[0,0,0],[1.25,2,3]])),
                    dict(joint='ProbeJoint',rotation=dict(frames=[0],values=[[0,0,0,1]]))])
        contract=dict(assets=[dict(asset='OfflineFixture',joints=[dict(mesh_joint_index=0,target_name='Root',target_parent=None),
                     dict(mesh_joint_index=1,target_name='ProbeJoint',target_parent='Root')])])
        motion=neutral_motion(spec,skeleton,contract);payload=build_motion(motion,translation_w=[0,0],joint_unknown=[0,0])
        model=self.load('00');new=install_payload(model,payload,60000,template_id=model.slots[0].motion_id)
        target=new.slots[-1].payload.value
        self.assertEqual([j.name for j in target.skeleton.joints],['Root','ProbeJoint'])
        self.assertIs(target.skeleton.joints[1].parent,target.skeleton.joints[0])
        self.assertEqual(target.animation_nodes[0].translation.frames,[0,20])
        self.assertEqual(target.animation_nodes[0].translation.values,[(0,0,0),(1.25,2,3)])
        self.assertEqual(target.frames_per_second,60);self.assertTrue(target.looping)
        assert_other_slots(model,new,{60000});self.stable(new)
        skeleton['joints'][1]['name']='WrongMeshName'
        with self.assertRaisesRegex(MotionWriteError,'mesh contract'):neutral_motion(spec,skeleton,contract)

    def test_shared_sequence_copy_delete_and_complete_filter_pages(self):
        model=self.load('00');session=RiseEditSession(model)
        result=session.apply(copy_sequences,1,3,positions=[0],replace=True)
        assert_other_slots(model,result,{3});self.stable(result)
        self.assertEqual(C.write(session.undo()),model.source)
        model=self.load('02');session=RiseEditSession(model)
        result=session.apply(delete_sequences,26,positions=[0],scope='override')
        assert_other_slots(model,result,{26});self.stable(result)
        self.assertEqual(C.write(session.undo()),model.source)
        record=Owner(model,26,'override').one(category=0);track=record.sequence.tracks[0]
        track.filter_page3=0x11223344;track.filter_page4=0x55667788;track.filter_page5=0x99aabbcc
        result=self.stable(model);after=Owner(result,26,'override').one(category=0).sequence.tracks[0]
        self.assertEqual((after.filter_page3,after.filter_page4,after.filter_page5),(0x11223344,0x55667788,0x99aabbcc))

    def test_real_bank_add_path_identity_remove_and_stability(self):
        raw=(CORPUS/'em100_00_bank.motbank.3').read_bytes();bank=MotbankFile();bank.read(raw)
        original=[(i.bank_id,i.bank_type,i.bank_type_mask_bits,i.path) for i in bank.items]
        bank.items.append(MotlistItem(bank_id=51,bank_type=0,bank_type_mask_bits=0,path='enemy/offline_probe/probe.motlist'))
        output=bank.write();again=MotbankFile();again.read(output)
        self.assertEqual(again.write(),output)
        self.assertEqual([(i.bank_id,i.bank_type,i.bank_type_mask_bits,i.path) for i in again.items[:-1]],original)
        self.assertEqual(again.items[-1].bank_id,51);self.assertEqual(again.items[-1].path,'enemy/offline_probe/probe.motlist')
        again.items.pop();restored=MotbankFile();restored.read(again.write())
        self.assertEqual([(i.bank_id,i.bank_type,i.bank_type_mask_bits,i.path) for i in restored.items],original)

    def test_invalid_layouts_and_references_are_rejected(self):
        source=self.load('02');raw=bytearray(source.source);base=source.motion_spans[0][0]
        struct.pack_into('<Q',raw,base+32,128)
        with self.assertRaisesRegex(MotionParseError,'property/custom'):C.parse(bytes(raw))
        tree=self.load('tree');graph=tree.slots[0].payload.value
        graph.motion_references[0].motion_id+=1
        with self.assertRaisesRegex(MotionWriteError,'disagree'):C.write(tree)
        tree=self.load('tree');graph=tree.slots[0].payload.value;graph.nodes.remove(graph.links[0].output_node)
        with self.assertRaisesRegex(MotionWriteError,'Dangling'):C.write(tree)
        tree=self.load('tree');graph=tree.slots[0].payload.value;graph.nodes[1].authored_id=graph.nodes[0].authored_id
        with self.assertRaisesRegex(MotionWriteError,'identity'):C.write(tree)
        source=self.load('02');source.slots[0].payload.value.frame_curves[0].points.append([10,1])
        with self.assertRaisesRegex(MotionWriteError,'Auxiliary structure'):C.write(source)
        source=self.load('02');raw=bytearray(source.source);base=source.motion_spans[0][0]
        struct.pack_into('<Q',raw,base+64,len(raw))
        with self.assertRaises(MotionParseError):C.parse(bytes(raw))
        source=self.load('00');clip=Owner(source,1,'motion').one(category=1).sequence.clip
        clip.root.children.append(clip.root)
        with self.assertRaisesRegex(MotionWriteError,'cycle'):C.write(source)
        source=self.load('00');clip=Owner(source,1,'motion').one(category=1).sequence.clip
        clip.root.children[0].properties[0].enum_closed=True
        with self.assertRaisesRegex(MotionWriteError,'CLIP graph/flag'):C.write(source)


if __name__=='__main__':unittest.main()
