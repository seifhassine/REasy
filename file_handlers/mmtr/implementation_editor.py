from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
    QStackedWidget, QCompleter,
)
from PySide6.QtCore import Qt

from file_handlers.mesh.preview_controls import find_combo_data
from file_handlers.sdf.shader_graph import ShaderGraph, ShaderGraphError, new_shader_graph, SHADER_GRAPH_STAGES
from file_handlers.sdf.shader_graph_editor import SdfShaderGraphEditor
from .material_editor import (
    extract_selected_function, import_material_function, material_function_root, save_material_function,
)
from .material_function import (
    expand_material_function_node, material_function_node_properties,
)
from .material_implementation import (
    MaterialProgramImplementation, group_implementation_routes, implementation_contract, implementation_execution,
    implementation_routes,
)
from .surface_meaning import shader_bytes
from .material_project import material_program_contract, material_program_routes
from .pass_purpose import PURPOSES, pass_purpose, purpose_title
from .profiles.dxbc_recovery import recover_dxbc_ir
from .profiles.re_engine_dxil_recovery import recover_re_engine_dxil_ir
from .vanilla_function import _recover_vanilla_graph_snapshot


class MaterialImplementationDialog(QDialog):
    def __init__(self, project, document, parent=None, *, source_name="", project_dir=""):
        super().__init__(parent)
        self.resize(1500, 900)
        self.project = deepcopy(project)
        self.document = document
        self._source_name = Path(source_name).name if source_name else ""
        self._project_dir = project_dir
        self._routes = material_program_routes(document, project)
        self._active = None
        self._contract = None
        self._draft = None
        root = QVBoxLayout(self)
        note = QLabel(self.tr(
            "Overrides replace whole shader stages. Identical programs with matching interfaces are grouped; "
            "edits apply to every variant in the group. Each graphics feature set has its own passes."
        ))
        note.setWordWrap(True)
        root.addWidget(note)
        actions = QHBoxLayout()
        self.purposes = QComboBox()
        self.purposes.setToolTip(self.tr("Show the passes drawing for one purpose."))
        actions.addWidget(self.purposes)
        self.routes = QComboBox()
        self.routes.setMinimumContentsLength(45)
        self.routes.setEditable(True)
        self.routes.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.routes.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.routes.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        owners = implementation_routes(project)
        self._groups = group_implementation_routes(document, self._routes, owners)
        named = " · ".join(
            part
            for part in (
                self._source_name,
                self.project.name or "",
                document.profile.display_name,
            )
            if part
        )
        self.setWindowTitle(
            self.tr("Native Shader Overrides — Advanced — {named} — {paths} path(s) in {groups} group(s)").format(
                named=named,
                paths=len(self._routes),
                groups=len(self._groups),
            )
        )
        profile = document.profile.shader_graph_profile
        self._purposes = {route: pass_purpose(document, profile, route[0], route[1]) for route in self._groups}
        self.purposes.addItem(self.tr("All purposes"), "")
        for purpose in PURPOSES:
            count = sum(item == purpose for item in self._purposes.values())
            if count:
                self.purposes.addItem(self.tr("{purpose} · {count}").format(purpose=purpose_title(purpose), count=count),
                                      purpose)
        self._fill_routes()
        listed = [self.routes.itemData(index) for index in range(self.routes.count())]
        preferred = next((r for r in listed if r in owners), None) or next((r for r in listed if r[2] == "pixel"), None)
        if preferred is not None:
            self.routes.setCurrentIndex(find_combo_data(self.routes, preferred))
        self.purposes.currentIndexChanged.connect(lambda _index: self._fill_routes())
        actions.addWidget(self.routes, 1)
        root.addLayout(actions)
        actions = QHBoxLayout()
        self._buttons = {}
        for label, callback in ((self.tr("Recover Editable Body"), self.recover), (self.tr("New Body"), self.new_body),
                                (self.tr("Import Function…"), self.import_function),
                                (self.tr("Extract Function…"), self.extract_function),
                                (self.tr("Expand Function"), self.expand_function),
                                (self.tr("Use Imported Body"), self.restore)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            self._buttons[callback.__name__] = button
            actions.addWidget(button)
        self.hlsl = QCheckBox(self.tr("Write HLSL for this pass"))
        self.hlsl.setToolTip(self.tr(
            "Replace the HLSL this pass's graph generates with your own, starting from Generate from graph. The build "
            "checks it against the pass's interface like a graph."))
        self.hlsl.toggled.connect(self._show_hlsl)
        actions.addWidget(self.hlsl)
        root.addLayout(actions)
        self.editor = SdfShaderGraphEditor(self)
        self.editor.graph_changed.connect(self._changed)
        self.editor.source_editor.textChanged.connect(self._changed)
        self.pages = QStackedWidget()
        self.empty = QLabel()
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pages.addWidget(self.empty)
        self.pages.addWidget(self.editor)
        root.addWidget(self.pages, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.detach = QCheckBox(self.tr("Remove the imported master (requires authored bodies for every route)"))
        self.detach.setChecked(project.domain == "implementation")
        root.addWidget(self.detach)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self.routes.currentIndexChanged.connect(self._activate)
        self._activate()

    def _fill_routes(self) -> None:
        chosen, current = self.purposes.currentData(), self.routes.currentData()
        self.routes.blockSignals(True)
        self.routes.clear()
        for route, variants in sorted(self._groups.items(), key=lambda item: PURPOSES.index(self._purposes[item[0]])):
            if chosen and self._purposes[route] != chosen:
                continue
            target, index, role = route
            program = self.document.sdf.targets[target].programs[index]
            feature_sets = len({item[0] for item in variants})
            span = "" if feature_sets < 2 else self.tr(" · {count} feature sets").format(count=feature_sets)
            self.routes.addItem(
                self.tr("{purpose} · {target} · {program} · {role} · {count} variant(s)").format(
                    purpose=purpose_title(self._purposes[route]), target=self._target_label(target),
                    program=program.name, role=role, count=len(variants)) + span,
                route,
            )
        self.routes.setCurrentIndex(find_combo_data(self.routes, current, 0))
        self.routes.blockSignals(False)
        if self.routes.currentData() != current and hasattr(self, "editor"):
            self._activate()

    def _target_label(self, target_index: int) -> str:
        target = self.document.sdf.targets[target_index]
        return target.display_name or target.key or self.tr("Target {index}").format(index=target_index)

    def _store_draft(self):
        if self._draft is None:
            return
        kept = self._without_active_routes()
        target, index, role = self._active
        label = f"{self.document.sdf.targets[target].programs[index].name} · {role}"
        source = self.editor.source_text() if self.hlsl.isChecked() and not self.editor.source_is_generated() else ""
        kept.append(MaterialProgramImplementation(label, self._groups[self._active],
            self._contract, deepcopy(self.editor.graph), source))
        self.project.implementations = kept
        self._draft = None

    def _without_active_routes(self):
        kept = []
        for item in self.project.implementations:
            routes = tuple(route for route in item.routes if route not in self._groups[self._active])
            if routes:
                kept.append(replace(item, routes=routes))
        return kept

    def _activate(self, *_args):
        self._store_draft()
        self._active = self.routes.currentData()
        self._contract = None
        self.editor.setEnabled(False)
        self.editor.scene.set_graph(None, None)
        self.pages.setCurrentWidget(self.empty)
        self.empty.setText(self.tr("No graph loaded. Recover the shader or create a new body to edit it."))
        for button in self._buttons.values():
            button.setEnabled(False)
        if self._active is None:
            return
        try:
            self._contract = material_program_contract(self.document, *self._active, allow_unreflected_material_fields=True)
            target, index, role = self._active
            shader = self.document.sdf.targets[target].programs[index].shader(role)
            if shader.binary_format in {"dxbc-container", "spirv-module"}:
                self._contract = implementation_contract(self._contract, shader_bytes(shader))
            existing = implementation_routes(self.project).get(self._active)
            self.hlsl.blockSignals(True)
            self.hlsl.setChecked(existing is not None and bool(existing.source))
            self.hlsl.blockSignals(False)
            if existing is None:
                self.status.setText(self.tr("Using the original shader."))
            else:
                self._contract = existing.renderer_contract(self._contract)
                self.editor.set_graph(deepcopy(existing.graph), self._contract,
                                      early_depth_stencil=self._early_depth(shader))
                self.pages.setCurrentWidget(self.editor)
                self.status.setText(self.tr("This graph replaces the original shader."))
                self.editor.set_source_text(existing.source, generated=not existing.source)
                if existing.source:
                    self.status.setText(self.tr("This pass's HLSL replaces the original shader."))
            self.editor.set_source_page_visible(self.hlsl.isChecked())
            self._draft = None
            for name in ("recover", "new_body"):
                self._buttons[name].setEnabled(self._contract.stage in SHADER_GRAPH_STAGES and shader.binary_format in {"dxbc-container", "spirv-module"})
            self._body_actions(existing is not None)
        except ValueError as exc:
            self.status.setText(str(exc))
            self.empty.setText(str(exc))

    def _body_actions(self, enabled):
        for name in ("import_function", "extract_function", "expand_function"):
            self._buttons[name].setEnabled(enabled)
        self._buttons["restore"].setEnabled(enabled and self.project.domain != "implementation")
        self.hlsl.setEnabled(enabled)

    @staticmethod
    def _early_depth(shader) -> bool:
        return implementation_execution(shader_bytes(shader)).early_depth_stencil

    def _show_hlsl(self, shown: bool) -> None:
        self.editor.set_source_page_visible(shown)
        if shown and not self.editor.source_text():
            try:
                self.editor.set_source_text(self.editor.generated_source_text(), generated=True)
            except ShaderGraphError as exc:
                self.status.setText(str(exc))
        self._changed()

    def _changed(self):
        if self._contract is not None:
            self._draft = True

    def _set_body(self, graph):
        target, index, role = self._active
        self.editor.set_graph(graph, self._contract, early_depth_stencil=self._early_depth(
            self.document.sdf.targets[target].programs[index].shader(role)))
        self.editor.set_source_text("", generated=True)
        self.hlsl.setChecked(False)
        self.pages.setCurrentWidget(self.editor)
        self._body_actions(True)
        self._draft = True
        self.status.setText(self.tr("Graph ready. Saving recompiles it and checks the shader interface."))

    def recover(self):
        if self._contract is None:
            return
        with ExitStack() as recovered:
            try:
                target, index, role = self._active
                shader = self.document.sdf.targets[target].programs[index].shader(role)
                if shader.binary_format not in {"dxbc-container", "spirv-module"}:
                    raise ShaderGraphError("Editable body recovery is not implemented for this native backend.")
                if shader.binary_format == 'spirv-module':
                    from file_handlers.sdf.spirv_ir import SpirvSemanticIr
                    ir = SpirvSemanticIr.from_bytes(shader.spirv.module)
                else:
                    ir = (recover_re_engine_dxil_ir if self._contract.model_major >= 6 else recover_dxbc_ir)(bytes(shader.payload))
                graph = recovered.enter_context(_recover_vanilla_graph_snapshot(
                    ir, self._contract, name=self.routes.currentText(),
                ))
            except ValueError as exc:
                self.status.setText(self.tr("Recovery is incomplete; no body was changed: {error}").format(error=exc))
                return
            self._set_body(graph)

    def new_body(self):
        if self._contract is None:
            return
        if self._contract.stage not in SHADER_GRAPH_STAGES:
            self.status.setText(self.tr("This stage needs an execution/side-effect graph editor; its imported body is unchanged."))
            return
        if QMessageBox.question(self, self.tr("New implementation"), self.tr(
            "Replace this route's entire behavior with a new graph? You must define every required renderer output."
        )) == QMessageBox.StandardButton.Yes:
            self._set_body(new_shader_graph(self._contract))

    def restore(self):
        if self._active is None or self.project.domain == "implementation":
            return
        self.project.implementations = self._without_active_routes()
        self._draft = None
        self.detach.setChecked(False)
        self._activate()

    def import_function(self):
        if not self.editor.isEnabled():
            return
        try:
            asset = import_material_function(self, self.project.source_profile)
            if asset is None:
                return
            node = self.editor.graph.add_node("material_function", properties=material_function_node_properties(asset))
            self.editor.scene.rebuild(select_node=node.node_id)
            self.editor._graph_changed()
        except (OSError, ShaderGraphError) as exc:
            self.status.setText(str(exc))

    def expand_function(self):
        selected = [item.node.node_id for item in self.editor.scene.selectedItems() if hasattr(item, "node")]
        if len(selected) != 1 or not self.editor.isEnabled():
            return
        try:
            graph = deepcopy(self.editor.graph)
            expand_material_function_node(graph, selected[0])
            self._set_body(graph)
        except ShaderGraphError as exc:
            self.status.setText(str(exc))

    def extract_function(self):
        if not self.editor.isEnabled():
            return
        selected = {item.node.node_id for item in self.editor.scene.selectedItems() if hasattr(item, "node")}
        try:
            extracted = extract_selected_function(self, self.editor.graph, selected,
                                                  source_profile=self.project.source_profile, contract=self._contract)
            if extracted is None:
                return
            graph, asset, _node = extracted
            if save_material_function(self, asset, material_function_root(self._project_dir)):
                self._set_body(graph)
        except (OSError, ShaderGraphError) as exc:
            self.status.setText(str(exc))

    def accept(self):
        try:
            self._store_draft()
            by_route = implementation_routes(self.project)
            for item in self.project.implementations:
                errors = [d.message for d in item.diagnostics(source_profile=self.project.source_profile) if d.severity == "error"]
                if errors:
                    raise ShaderGraphError("\n".join(errors))
            if self.detach.isChecked():
                if set(by_route) != set(self._routes):
                    raise ShaderGraphError("The imported master cannot be removed until every required renderer route has an authored body.")
                self.project.graph = ShaderGraph("material", "")
                self.project.domain = "implementation"
                self.project.renderer_template_routes = [(*route, self.document.sdf.targets[route[0]].programs[route[1]].name) for route in self._routes]
                self.project.source_material_model = "authored.renderer-implementations"
                self.project._prepare_semantics = None
            elif self.project.domain == "implementation":
                raise ShaderGraphError("This project has no imported master to restore. Reopen the original MMTR to use its body.")
        except ShaderGraphError as exc:
            self.status.setText(str(exc))
            return
        super().accept()
