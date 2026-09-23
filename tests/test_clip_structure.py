from dataclasses import asdict
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import struct
import tempfile
import unittest

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_clip_editing import copy_sequences, delete_sequences, edit_clip
from file_handlers.motion.mhr_structure import Owner


CORPUS = Path(__file__).parent / 'TESTFILE/natives/STM/player/mot/plw_GunLance_100.motlist.528'
DEPLOYED = Path(r'C:\Program Files (x86)\Steam\steamapps\common\MonsterHunterRise\natives\STM\player\mot\plw_GunLance_100.motlist.528')


def sequence_values(sequences):
    return [asdict(sequence) for sequence in sequences]


def animation_values(motion):
    return [(node.joint.name, tuple(None if track is None else
             (tuple(track.frames), tuple(tuple(v) if isinstance(v, (list, tuple)) else v for v in track.values))
             for track in (node.translation, node.rotation, node.scale)))
            for node in motion.animation_nodes]


def slot_for(document, motion_id):
    return next(slot for slot in document.slots if slot.motion_id == motion_id)


class ClipStructureTests(unittest.TestCase):
    def load(self, path=CORPUS):
        if not path.is_file():
            self.skipTest(f'Local fixture unavailable: {path}')
        raw = path.read_bytes()
        return raw, CODEC.parse(raw, label=str(path))

    def fixture_pair(self, document):
        donor = next(slot for slot in document.slots if
                     sum(seq.category.name == 'SOUND' for seq in slot.payload.value.sequences) == 1)
        target = next(slot for slot in document.slots[1:-1] if slot.motion_id != donor.motion_id)
        return donor, target

    def assert_preserved(self, before, after, changed_id, changed_scope='motion'):
        self.assertEqual([s.motion_id for s in before.slots], [s.motion_id for s in after.slots])
        for old, new in zip(before.slots, after.slots):
            self.assertEqual(animation_values(old.payload.value), animation_values(new.payload.value))
            if old.motion_id != changed_id or changed_scope != 'motion':
                self.assertEqual(sequence_values(old.payload.value.sequences), sequence_values(new.payload.value.sequences))
            if old.motion_id != changed_id or changed_scope != 'override':
                self.assertEqual(sequence_values(old.overrides), sequence_values(new.overrides))
        encoded = CODEC.write(after)
        self.assertEqual(CODEC.write(CODEC.parse(encoded)), encoded)

    def test_copy_into_middle_motion_preserves_animation_and_overrides(self):
        raw, document = self.load()
        donor, target = self.fixture_pair(document)
        old = sequence_values(target.payload.value.sequences)
        copied = next(seq for seq in donor.payload.value.sequences if seq.category.name == 'SOUND')
        result = copy_sequences(document, donor.motion_id, target.motion_id, categories=['SOUND'])
        values = sequence_values(slot_for(result, target.motion_id).payload.value.sequences)
        self.assertEqual(len(values), len(old) + 1)
        self.assertIn(asdict(copied), values)
        for value in old:
            self.assertIn(value, values)
        self.assert_preserved(document, result, target.motion_id)
        self.assertEqual(CODEC.write(document), raw)

    def test_delete_all_then_copy_into_empty_motion(self):
        _, document = self.load()
        donor, target = self.fixture_pair(document)
        empty = delete_sequences(document, target.motion_id,
                                 positions=list(range(len(target.payload.value.sequences))))
        self.assertEqual(slot_for(empty, target.motion_id).payload.value.sequences, [])
        self.assert_preserved(document, empty, target.motion_id)
        result = copy_sequences(empty, donor.motion_id, target.motion_id, categories=['SOUND'])
        self.assertEqual(sequence_values(slot_for(result, target.motion_id).payload.value.sequences),
                         sequence_values([seq for seq in donor.payload.value.sequences if seq.category.name == 'SOUND']))
        self.assert_preserved(empty, result, target.motion_id)

    def test_replace_removes_duplicate_target_categories(self):
        _, document = self.load()
        donor, target = self.fixture_pair(document)
        first = copy_sequences(document, donor.motion_id, target.motion_id, categories=['SOUND'])
        duplicated = copy_sequences(first, donor.motion_id, target.motion_id, categories=['SOUND'])
        self.assertGreaterEqual(sum(seq.category.name == 'SOUND' for seq in
                                    slot_for(duplicated, target.motion_id).payload.value.sequences), 2)
        result = copy_sequences(duplicated, donor.motion_id, target.motion_id,
                                categories=['SOUND'], replace=True)
        sequences = slot_for(result, target.motion_id).payload.value.sequences
        self.assertEqual(sum(seq.category.name == 'SOUND' for seq in sequences), 1)
        self.assertEqual(sequence_values([s for s in sequences if s.category.name != 'SOUND']),
                         sequence_values([s for s in target.payload.value.sequences if s.category.name != 'SOUND']))
        self.assert_preserved(duplicated, result, target.motion_id)

    def test_delete_override_preserves_motion_sequences(self):
        _, document = self.load()
        target = next(slot for slot in document.slots if slot.overrides)
        result = delete_sequences(document, target.motion_id, positions=[0], scope='override')
        self.assertEqual(sequence_values(slot_for(result, target.motion_id).overrides),
                         sequence_values(target.overrides[1:]))
        self.assert_preserved(document, result, target.motion_id, 'override')

    def graph_fixture(self, document, scope='motion'):
        for slot in document.slots:
            owner = Owner(document, slot.motion_id, scope)
            for position, record in enumerate(owner.records):
                if record.sequence.clip.root.children and len(record.sequence.tracks) == len(record.sequence.clip.root.children):
                    if any(r.prop.keys for r in record.parsed.properties):
                        return slot.motion_id, position, record
        self.fail('No native CLIP graph fixture')

    def test_node_copy_delete_maintains_all_seven_metadata_columns(self):
        _, document = self.load()
        motion_id, position, record = self.graph_fixture(document)
        node = record.parsed.clip.root.children[0]
        node_index = next(r.index for r in record.parsed.nodes if r.node is node)
        copied = edit_clip(document, motion_id, sequence=position, operation='node-copy', source_id=motion_id,
                           source_sequence=position, node_index=node_index, target_node_index=0)
        new = Owner(copied, motion_id, 'motion').records[position]
        self.assertEqual(len(new.sequence.tracks), len(record.sequence.tracks) + 1)
        self.assertEqual(copied.source[new.tracks + len(record.sequence.tracks) * 28:new.tracks + len(new.sequence.tracks) * 28],
                         document.source[record.tracks:record.tracks + 28])
        self.assertEqual(asdict(new.sequence.clip.root.children[-1]), asdict(node))
        self.assert_preserved(document, copied, motion_id)
        added = new.parsed.clip.root.children[-1]
        added_index = next(r.index for r in new.parsed.nodes if r.node is added)
        restored = edit_clip(copied, motion_id, sequence=position, operation='node-delete', node_index=added_index)
        self.assertEqual(sequence_values(slot_for(restored, motion_id).payload.value.sequences),
                         sequence_values(slot_for(document, motion_id).payload.value.sequences))

    def test_property_and_key_copy_delete(self):
        _, document = self.load()
        motion_id, position, record = self.graph_fixture(document)
        prop = next(r for r in record.parsed.properties if r.prop.keys)
        copied = edit_clip(document, motion_id, sequence=position, operation='property-copy', source_id=motion_id,
                           source_sequence=position, property_index=prop.index, target_node_index=0)
        new = Owner(copied, motion_id, 'motion').records[position]
        added_prop = new.parsed.clip.root.properties[-1]
        self.assertEqual(asdict(added_prop), asdict(prop.prop))
        added_index = next(r.index for r in new.parsed.properties if r.prop is added_prop)
        restored = edit_clip(copied, motion_id, sequence=position, operation='property-delete', property_index=added_index)
        self.assertEqual(sequence_values(slot_for(restored, motion_id).payload.value.sequences),
                         sequence_values(slot_for(document, motion_id).payload.value.sequences))
        copied = edit_clip(document, motion_id, sequence=position, operation='key-copy', source_id=motion_id,
                           source_sequence=position, property_index=prop.index, key_index=0,
                           target_property_index=prop.index, frame=prop.prop.keys[-1].frame + 1)
        new = Owner(copied, motion_id, 'motion').records[position]
        # Table ordering may normalize; resolve the original property through its ownership graph.
        candidates = [r for r in new.parsed.properties if r.prop.name == prop.prop.name and
                      len(r.prop.keys) == len(prop.prop.keys) + 1]
        self.assertEqual(len(candidates), 1)
        changed = candidates[0]
        self.assertEqual(changed.prop.keys[-1].value, prop.prop.keys[0].value)
        restored = edit_clip(copied, motion_id, sequence=position, operation='key-delete',
                             property_index=changed.index, key_index=len(changed.prop.keys) - 1)
        self.assertEqual(sequence_values(slot_for(restored, motion_id).payload.value.sequences),
                         sequence_values(slot_for(document, motion_id).payload.value.sequences))
        self.assert_preserved(document, copied, motion_id)

    def test_override_node_edit_and_cross_scope_sequence_copy(self):
        _, document = self.load()
        motion_id, position, record = self.graph_fixture(document, 'override')
        child = record.parsed.clip.root.children[0]
        index = next(r.index for r in record.parsed.nodes if r.node is child)
        copied = edit_clip(document, motion_id, scope='override', sequence=position, operation='node-copy',
                           source_id=motion_id, source_scope='override', source_sequence=position,
                           node_index=index, target_node_index=0)
        self.assert_preserved(document, copied, motion_id, 'override')
        target = next(s for s in document.slots if not s.overrides)
        result = copy_sequences(document, motion_id, target.motion_id, positions=[position],
                                source_scope='override', target_scope='override')
        self.assertEqual(sequence_values(slot_for(result, target.motion_id).overrides), [asdict(record.sequence)])
        self.assert_preserved(document, result, target.motion_id, 'override')

    def test_cross_file_copy_preserves_donor(self):
        _, document = self.load()
        donor_path = CORPUS.with_name('plw_LongSword_100.motlist.528')
        donor_raw, donor = self.load(donor_path)
        source = next(slot for slot in donor.slots if
                      sum(s.category.name == 'SOUND' for s in slot.payload.value.sequences) == 1)
        target = document.slots[-1]
        result = copy_sequences(document, source.motion_id, target.motion_id, donor=donor, categories=['SOUND'])
        self.assertEqual(asdict(slot_for(result, target.motion_id).payload.value.sequences[0]),
                         asdict(next(s for s in source.payload.value.sequences if s.category.name == 'SOUND')))
        self.assertEqual(CODEC.write(donor), donor_raw)
        self.assert_preserved(document, result, target.motion_id)

    def test_node_copy_delete_cli_batch(self):
        from tools.cli.main import main
        raw, document = self.load()
        motion_id, position, record = self.graph_fixture(document)
        node_index = next(r.index for r in record.parsed.nodes if r.node is record.parsed.clip.root.children[0])
        with tempfile.TemporaryDirectory() as folder:
            plan_path, output = Path(folder) / 'plan.json', Path(folder) / 'candidate.motlist.528'
            plan = [
                ['node-copy', '--from', str(motion_id), '--to', str(motion_id),
                 '--source-sequence', str(position), '--sequence', str(position),
                 '--node-index', str(node_index), '--target-node-index', '0'],
                ['node-delete', '--motion', str(motion_id), '--sequence', str(position),
                 '--node-index', str(len(record.parsed.clip.root.children) + 1)],
            ]
            plan_path.write_text(json.dumps(plan), encoding='utf-8')
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(['clip', 'batch', str(CORPUS), '--plan', str(plan_path), '-o', str(output), '--json'])
            self.assertEqual(code, 0, stderr.getvalue() + stdout.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())['steps'], 2)
            result = CODEC.parse(output.read_bytes())
            self.assert_preserved(document, result, -1)
            self.assertEqual(CORPUS.read_bytes(), raw)

    def test_deployed_last_motion_copy_relocates_absolute_override_tail(self):
        raw, document = self.load(DEPLOYED)
        ids = {slot.motion_id for slot in document.slots}
        if not {111, 626}.issubset(ids):
            self.skipTest('Deployed file no longer contains the regression motion pair')
        result = copy_sequences(document, 111, 626, categories=['SOUND'])
        self.assert_preserved(document, result, 626)
        self.assertEqual(sum(bool(s.overrides) for s in result.slots), sum(bool(s.overrides) for s in document.slots))
        output = CODEC.write(result)
        old_table = struct.unpack_from('<Q', raw, 24)[0]
        new_table = struct.unpack_from('<Q', output, 24)[0]
        self.assertGreater(new_table, old_table)
        index = next(i for i, slot in enumerate(document.slots) if slot.overrides)
        old_pointer = struct.unpack_from('<Q', raw, old_table + index * 72)[0]
        new_pointer = struct.unpack_from('<Q', output, new_table + index * 72)[0]
        self.assertEqual(new_pointer - old_pointer, new_table - old_table)
        self.assertEqual(DEPLOYED.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
