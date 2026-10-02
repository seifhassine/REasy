"""Selector references use object slots, even when the instance table is reordered."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.selectors import clone_selector, resolve_selector, validate_selectors
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.cli import build_parser
from test_motfsm_corpus import CORPUS, unique_files
from test_motfsm_integration import make_fixture


class SelectorValidationTests(unittest.TestCase):
    def test_invalid_edit_is_rejected_before_mutation(self):
        doc = MotfsmFile()
        doc.read(make_fixture())
        node = doc.bhvt.nodes[0]
        binding = doc.bindings.get(node, 'selector_id')
        with self.assertRaisesRegex(ValueError, 'Invalid selectors object'):
            doc.edit_field(binding, 0)
        self.assertEqual(node.selector_id, -1)
        self.assertEqual(doc.rebuild(), doc.source)

    def test_save_rejects_invalid_selector_with_node_context(self):
        doc = MotfsmFile()
        doc.read(make_fixture())
        doc.bhvt.nodes[0].selector_id = 0
        with self.assertRaisesRegex(ValueError, 'Node 0 Fixture.*Invalid selectors object'):
            doc.rebuild()

    def test_absent_selector_does_not_create_an_object(self):
        doc = MotfsmFile()
        doc.read(make_fixture())
        for raw in (-1, 0xFFFFFFFF):
            self.assertEqual(clone_selector(doc, raw), -1)
        self.assertEqual(doc.rebuild(), doc.source)

    def test_editor_rejects_invalid_selector_before_adding_undo_command(self):
        from PySide6.QtWidgets import QApplication
        from file_handlers.motfsm.motfsm_handler import MotfsmHandler
        app = QApplication.instance() or QApplication([])
        handler = MotfsmHandler()
        handler.read(make_fixture())
        node = handler.motfsm.bhvt.nodes[0]
        binding = handler.motfsm.bindings.get(node, 'selector_id')
        with self.assertRaisesRegex(ValueError, 'Invalid selectors object'):
            handler.edit_field(binding, '0')
        self.assertEqual(handler.editor_document.undo_stack.count(), 0)
        self.assertEqual(node.selector_id, -1)
        self.assertFalse(handler.modified)


@unittest.skipUnless(CORPUS.is_dir(), 'MHRise corpus not installed')
class NativeSelectorCopyTests(unittest.TestCase):
    def read_source(self):
        doc = MotfsmFile()
        doc.read(next(unique_files()).read_bytes())
        return doc

    def test_copy_resolves_object_table_and_preserves_independent_fields_after_reopen(self):
        doc = self.read_source()
        block = doc.rsz_blocks.get_block('selectors')
        native = block.file
        # Object slot 0 deliberately points far away from instance 0/1.
        native.object_table[0], native.object_table[-1] = native.object_table[-1], native.object_table[0]
        template = resolve_selector(doc, 0)
        field = next(f for f in template.fields if f.binding and f.binding.type_name == 'bool')
        field.data.value = not field.value
        expected = [(f.name, f.value) for f in template.fields]
        old_objects = list(block.object_table)
        new_raw = clone_selector(doc, 0)
        self.assertEqual(new_raw, len(old_objects))
        cloned = resolve_selector(doc, new_raw)
        self.assertNotEqual(cloned.index, new_raw)
        self.assertNotEqual(cloned.index, template.index)
        self.assertEqual([(f.name, f.value) for f in cloned.fields], expected)
        self.assertIsNot(native.parsed_elements[cloned.index][field.name], field.data)
        self.assertEqual(block.object_table[:-1], old_objects)
        doc.bhvt.nodes[0].selector_id = new_raw
        start = doc.bhvt.offsets['nodes']
        tail = min(o for o in doc.bhvt.offsets.values() if o >= doc.bhvt.node_data_end)
        nodes = serialize_nodes(doc)
        nodes += bytes((tail - start - len(nodes)) % 16)
        data = native.build_validated()
        output = bytes(splice_document(doc, [(start, tail, nodes),
            (block.offset, block.end, data + bytes(-len(data) % 16))]))
        reopened = MotfsmFile()
        reopened.read(output)
        validate_selectors(reopened)
        self.assertEqual([(f.name, f.value) for f in resolve_selector(reopened, new_raw).fields], expected)
        self.assertEqual(reopened.rebuild(), output)
        with self.assertRaisesRegex(ValueError, 'Invalid selectors object'):
            resolve_selector(reopened, len(reopened.rsz_blocks.get_block('selectors').object_table))

    def test_node_recipes_copy_present_selector_by_default(self):
        doc = self.read_source()
        template_index = next(i for i, n in enumerate(doc.bhvt.nodes)
                              if sum(doc.references.action(a.id_hash, a.ex_id).class_name ==
                                     'snow.PlayerPlayMotion2' for a in n.actions) == 1)
        node = doc.bhvt.nodes[template_index]
        parent = doc.references.parent_index(node)
        raw = next(n.selector_id for n in doc.bhvt.nodes if n.selector_id >= 0)
        doc.edit_field(doc.bindings.get(node, 'selector_id'), raw)
        expected = [(f.name, f.value) for f in resolve_selector(doc, raw).fields]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.motfsm2.43'
            source.write_bytes(doc.rebuild())
            for command in ('node-clone', 'attack-clone'):
                with self.subTest(command=command), redirect_stdout(io.StringIO()):
                    args = [command, str(source), '-o', str(Path(folder) / 'output.motfsm2.43'),
                            '--parent', f'index:{parent}', '--name', 'selector_copy_test', '--motion', '620']
                    args += (['--copy-node', f'index:{template_index}', '--set-effect', '0']
                             if command == 'node-clone' else
                             ['--template', f'index:{template_index}', '--keep-hit-index', '0'])
                    parsed = build_parser().parse_args(args)
                    output = parsed.run(parsed)
                    reopened = MotfsmFile()
                    reopened.read(output)
                    cloned_raw = reopened.bhvt.nodes[-1].selector_id
                    self.assertNotEqual(cloned_raw, raw)
                    self.assertEqual(cloned_raw, len(doc.rsz_blocks.get_block('selectors').object_table))
                    self.assertEqual([(f.name, f.value) for f in resolve_selector(reopened, cloned_raw).fields], expected)
                    self.assertEqual(reopened.bhvt.nodes[template_index].selector_id, raw)
                    self.assertEqual(reopened.rebuild(), output)


if __name__ == '__main__':
    unittest.main()
