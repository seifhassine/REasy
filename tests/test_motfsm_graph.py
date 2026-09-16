"""Native graph identity, editing integration and viewport interaction contracts."""
import copy
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVT, BHVTNode, ChildNode, State, Transition, AllState
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.motfsm.graph_model import MotfsmGraph
from file_handlers.motfsm.graph_view import MotfsmGraphView


CORPUS = Path(__file__).parent/'TESTFILE/natives/STM/player/Fsm'


def graph_document():
    root = BHVTNode(id_hash=0, name='root', parent=0xFFFFFFFF)
    first = BHVTNode(id_hash=10, name='same', parent=0)
    second = BHVTNode(id_hash=20, ex_id=2, name='same', parent=0)
    leaf = BHVTNode(id_hash=30, name='leaf', parent=10)
    root.children = [ChildNode(10, 0, -1), ChildNode(20, 2, -1)]
    root.transitions = [Transition(mStartState=10)]
    first.children = [ChildNode(30, 0, -1)]
    first.states = [State(mTransitions=20, TransitionConditions=3, mStatesEx=999)]
    second.states = [State(mTransitions=10)]
    second.all_states = [AllState(mAllState=30, mAllTransition=0x40000001)]
    leaf.states = [State(mTransitions=30)]
    doc = MotfsmFile()
    doc.bhvt = BHVT(0, {}, [root, first, second, leaf])
    return doc


class GraphModelTests(unittest.TestCase):
    def test_root_identity_and_same_name_nodes_remain_distinct(self):
        doc = graph_document()
        graph = MotfsmGraph(doc)
        self.assertEqual(graph.parents[1], 0)
        self.assertEqual(doc.references.parent_index(doc.bhvt.nodes[1]), 0)
        self.assertNotEqual(graph.identity(1), graph.identity(2))
        self.assertEqual(graph.path(3), 'root.same.leaf')
        edge = next(edge for edge in graph.outgoing[1] if edge.kind == 'state')
        self.assertEqual(edge.target, 2)
        self.assertEqual(edge.condition, 3)

    def test_relationship_kinds_cycles_and_subgraph_scope(self):
        doc = graph_document()
        before = copy.deepcopy(doc.bhvt)
        graph = MotfsmGraph(doc)
        self.assertEqual({edge.kind for edge in graph.edges}, {'child', 'state', 'start', 'all'})
        visible, edges = graph.subgraph(0, 'children')
        self.assertEqual(visible, [0, 1, 2])
        self.assertTrue(any(edge.source == 2 and edge.target == 1 for edge in edges))
        visible, edges = graph.subgraph(1, 'neighbors')
        self.assertEqual(visible, [0, 1, 2])
        self.assertTrue(all(edge.kind != 'child' for edge in edges))
        self.assertEqual(set(graph.columns(1, visible, edges, 'neighbors')), set(visible))
        self.assertEqual(doc.bhvt, before)

    def test_unresolved_and_ambiguous_references_stay_visible(self):
        doc = graph_document()
        doc.bhvt.nodes[1].states.append(State(mTransitions=0xDEAD))
        doc.bhvt.nodes.append(BHVTNode(id_hash=20, ex_id=3, name='different', parent=0))
        graph = MotfsmGraph(doc)
        broken = [edge for edge in graph.outgoing[1] if edge.error]
        self.assertEqual(len(broken), 2)
        self.assertTrue(all(edge.target is None for edge in broken))
        self.assertIn('Ambiguous', broken[0].error)
        self.assertIn('Unknown', broken[1].error)
        self.assertIn(broken[1], graph.subgraph(1, 'neighbors')[1])


class GraphInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_drag_reroutes_edges_without_changing_the_document(self):
        errors = []
        previous_hook = sys.excepthook
        sys.excepthook = lambda kind, error, trace: errors.append(error)
        doc = graph_document()
        before = copy.deepcopy(doc.bhvt)
        graph = MotfsmGraph(doc)
        view = MotfsmGraphView()
        moved, selected = [], []
        view.node_moved.connect(lambda *args: moved.append(args))
        view.node_selected.connect(selected.append)
        view.resize(1000, 700)
        view.set_graph(graph, 0, 'children', {})
        view.show()
        view.fit_graph()
        self.app.processEvents()
        try:
            item = view.node_items[1]
            edge = next(edge for edge in view.edge_items if edge.edge.target == 1)
            original_path = edge.path()
            start = view.mapFromScene(item.pos()+QPointF(80, 35))
            QTest.mousePress(view.viewport(), Qt.LeftButton, pos=start)
            QTest.mouseMove(view.viewport(), start+QPoint(70, 45), delay=20)
            QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=start+QPoint(70, 45))
            self.assertIn(1, selected)
            self.assertTrue(moved)
            self.assertNotEqual(edge.path(), original_path)
            scale = view.transform().m11()
            wheel = QWheelEvent(QPointF(200, 200), QPointF(200, 200), QPoint(), QPoint(0, 120),
                                Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
            self.app.sendEvent(view.viewport(), wheel)
            self.assertGreater(view.transform().m11(), scale)
            view.grab()
            edge_selected = []
            view.edge_selected.connect(edge_selected.append)
            view.scene().clearSelection()
            edge.setSelected(True)
            self.assertEqual(edge_selected, [edge.edge])
            self.assertEqual(doc.bhvt, before)
            self.assertFalse(errors, errors)
        finally:
            view.close()
            sys.excepthook = previous_hook


@unittest.skipUnless(CORPUS.is_dir(), 'local FSM corpus is absent')
class NativeGraphTests(unittest.TestCase):
    def test_corpus_graphs_resolve_without_modifying_native_resources(self):
        paths = sorted(CORPUS.rglob('*.motfsm2.*'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(file=path.name):
                source = path.read_bytes()
                doc = MotfsmFile()
                doc.read(source)
                graph = MotfsmGraph(doc)
                self.assertFalse([issues for issues in graph.issues.values() if issues])
                self.assertEqual(doc.rebuild(), source)
                root = graph.root()
                self.assertIsNotNone(root)
                visible, edges = graph.subgraph(root, 'children')
                self.assertIn(root, visible)
                self.assertTrue(all(edge.source in visible for edge in edges))

    def test_graph_inspector_edit_save_and_layout_are_independent(self):
        app = QApplication.instance() or QApplication([])
        path = max(CORPUS.rglob('*.motfsm2.*'), key=lambda path: path.stat().st_size)
        source = path.read_bytes()
        handler = MotfsmHandler()
        handler.read(source)
        editor = handler.create_viewer()
        try:
            index = next(i for i, node in enumerate(handler.motfsm.bhvt.nodes) if node.children and node.states)
            editor.navigate(index)
            editor.mode.setCurrentIndex(1)
            self.assertEqual(editor.mode.currentData(), 'neighbors')
            editor._back()
            self.assertEqual(editor.focus, index)
            self.assertEqual(editor.mode.currentData(), 'children')
            identity = editor.model.identity(index)
            item = editor.view.node_items[index]
            item.setPos(item.pos()+QPointF(90, 55))
            position = editor.layouts[editor._scope_key()][identity]
            self.assertFalse(handler.modified)
            self.assertEqual(handler.rebuild(), source)
            editor.search.setText(handler.motfsm.bhvt.nodes[index].name)
            self.assertGreater(editor.proxy.rowCount(), 0)
            index = editor.model.children(index)[0]
            editor.view.scene().clearSelection()
            editor.view.node_items[index].setSelected(True)
            node = handler.motfsm.bhvt.nodes[index]
            editor.search.setText(f'0x{node.id_hash:08X}')
            self.assertGreater(editor.proxy.rowCount(), 0)
            pending = [editor.inspector.tree.topLevelItem(0)]
            row = None
            while pending:
                item = pending.pop()
                if item.binding is not None and item.binding.owner is node and item.binding.attribute == 'priority':
                    row = item
                    break
                pending.extend(item.child(i) for i in range(item.childCount()))
            self.assertIsNotNone(row)
            original = node.priority
            row.setText(1, str(original+1))
            QTest.qWait(120)
            self.assertTrue(handler.modified)
            self.assertEqual(editor.model.nodes[index].priority, original+1)
            self.assertTrue(editor.view.node_items[index].isSelected())
            self.assertEqual(editor.layouts[editor._scope_key()][identity], position)
            reopened = MotfsmFile()
            reopened.read(handler.rebuild())
            self.assertEqual(reopened.bhvt.nodes[index].priority, original+1)
            handler.mark_saved()
            self.assertFalse(editor.modified)
            self.assertEqual(path.read_bytes(), source)
        finally:
            editor.close()
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
