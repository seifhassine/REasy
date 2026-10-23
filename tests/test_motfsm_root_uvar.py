"""Root UVAR replacement in a real native FSM, including simultaneous growth."""
import os
from pathlib import Path
import unittest
import uuid

from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.validation import variable_snapshot
from file_handlers.uvar.uvar_types import TypeKind

SOURCE = Path(os.environ['REASY_MHR_FSM_TEST_FILE']) if 'REASY_MHR_FSM_TEST_FILE' in os.environ else None


@unittest.skipUnless(SOURCE is not None and SOURCE.is_file(), 'Set REASY_MHR_FSM_TEST_FILE to a native MHR FSM')
class NativeRootVariableTests(unittest.TestCase):
    def exercise(self, grow_graph, named=True):
        original = SOURCE.read_bytes()
        document = MotfsmFile()
        document.read(original)
        before = variable_snapshot(document)
        variables = document.read_root_variables()
        if named:
            variables.header.name = 'native_root_variable_test'
        integer = variables.add_variable('state_counter_test', TypeKind.Int32, -4)
        integer.guid = uuid.UUID('72618342-c45b-4d17-97af-a2be9835120d')
        meter = variables.add_variable('state_meter_test', TypeKind.Single, 50.0)
        meter.guid = uuid.UUID('9bfc1436-8337-4b21-b479-a68694d32ac0')
        document.set_root_variables(variables)
        if grow_graph:
            node = next(node for node in document.bhvt.nodes if node.actions)
            node.actions.extend(node.actions[:1] * 3)
            document.graph_editor().rename_node(document.bhvt.nodes.index(node), node.name + '_root_variable_test')
        raw = document.build_validated()
        reopened = MotfsmFile()
        reopened.read(raw)
        after = variable_snapshot(reopened)
        if not named:
            self.assertEqual(after[0][:3], before[0][:3])
        self.assertEqual(after[1:], before[1:])
        self.assertEqual(after[0][3][:-2], before[0][3])
        self.assertEqual([(value[0], value[1], value[5]) for value in after[0][3][-2:]],
                         [(integer.guid, 'state_counter_test', -4), (meter.guid, 'state_meter_test', 50.0)])
        self.assertEqual(reopened.build_validated(), raw)
        self.assertEqual(SOURCE.read_bytes(), original)

    def test_native_root_variable_growth_preserves_banks_and_resources(self):
        self.exercise(False)

    def test_root_variable_growth_with_node_and_string_relocation(self):
        self.exercise(True)

    def test_unnamed_native_root_keeps_namespace_identity(self):
        self.exercise(False, named=False)


if __name__ == '__main__':
    unittest.main()
