from __future__ import annotations

from copy import deepcopy
import struct
from dataclasses import dataclass
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton

lazy from file_handlers.mdf.mdf_file import MdfFile
lazy from file_handlers.mdf.mdf_handler import MdfHandler
from file_handlers.sdf.sdf_viewer import SdfViewer
lazy from services.backup_store import create_backup
lazy from ui.background_jobs import run_job
lazy from ui.file_tab import open_tabs_for_paths, tab_output_path
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role
lazy from utils.resource_file_utils import (
    normalize_resource_path,
    resource_context_for_handler,
    resource_path_with_version,
    resource_version_from_path,
)

lazy from .material_context import bind_material_instance
lazy from .material_editor import MmtrMaterialEditor
lazy from .material_family import family_member_filename
from .material_project import apply_material_build, material_authoring_coverage
lazy from .material_variant import (
    create_material_variant_artifacts,
    material_project_sidecar_path,
    mmtr_resource_path_from_file,
    write_material_variant,
)
lazy from .material_variant_dialog import MaterialVariantDialog
lazy from .material_variant_sources import MdfVariantSourceChoice, discover_mdf_consumers


@dataclass(frozen=True)
class ObjectScope:
    material: object
    mdf_handler: object | None = None
    material_index: int = 0
    family_loader: object | None = None


_PENDING_SCOPES: dict[str, ObjectScope] = {}


def open_master_for_object(app, path: str, data: bytes, scope: ObjectScope, *, pak_project_dir: str = ""):
    key = os.path.normcase(str(path))
    _PENDING_SCOPES[key] = scope
    try:
        if not pak_project_dir:
            return app.add_tab(path, data)
        tab = app.add_tab(path, data, pak_source_path=path, pak_project_dir=pak_project_dir)
        if tab:
            app.attach_pak_source_tab(tab, path, pak_project_dir)
        return tab
    finally:
        _PENDING_SCOPES.pop(key, None)


class MmtrViewer(SdfViewer):
    def __init__(self, handler, parent=None):
        super().__init__(handler, parent, editor_only=True)
        self.setObjectName("mmtrViewer")
        self.material_editor = MmtrMaterialEditor(self)
        self.material_editor.source_path = str(getattr(handler, "filepath", "") or "")
        context = resource_context_for_handler(handler)
        self.material_editor.project_dir = str(
            getattr(context, "project_dir", "") or ""
        )
        self.material_editor.dirty_changed.connect(self._material_project_dirty_changed)
        self.material_editor.variant_requested.connect(
            self._material_variant_requested
        )
        self.tabs.addTab(
            self.material_editor,
            self.tr("Material"),
        )
        self.tabs.setCurrentWidget(self.material_editor)
        self.object_scope = _PENDING_SCOPES.pop(os.path.normcase(str(getattr(handler, "filepath", "") or "")), None)
        if self.object_scope is not None:
            handler.bind_material_instance(self.object_scope.material, self.object_scope.family_loader)
            self._add_scope_banner()
        elif getattr(handler, "app", None) is not None:
            self._offer_material_instance()
        self._refresh()

    def _add_scope_banner(self) -> None:
        scope = self.object_scope
        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 2)
        name = str(scope.material.header.mat_name or self.tr("Unnamed material"))
        if scope.mdf_handler is not None:
            text = self.tr("Editing the shader of {material} in {mdf} only. Save it with Create Variant: the master "
                           "and every other object keep their shader.").format(
                material=name, mdf=os.path.basename(str(getattr(scope.mdf_handler, "filepath", "") or "")))
        else:
            text = self.tr("Editing the master itself: saving it changes every object that uses it, such as "
                           "{material}.").format(material=name)
        banner = QLabel(text)
        banner.setWordWrap(True)
        set_style_role(banner, banner="info")
        row.addWidget(banner, 1)
        if scope.mdf_handler is None:
            users = QPushButton(self.tr("Objects Using It…"))
            users.setIcon(line_icon("search"))
            users.setToolTip(self.tr("Find the MDF materials that use this master in the project, unpacked files "
                                     "and game PAKs."))
            users.clicked.connect(self._show_master_users)
            row.addWidget(users)
        self.layout().insertLayout(1, row)

    def _show_master_users(self) -> None:
        _mmtr_path, resource_path = self._variant_source_paths()
        sources = self._discover_mdf_sources(resource_path, *self._variant_asset_context())
        rows = [f"{source.label} · {len(source.matching_material_indices)}" for source in sources]
        QMessageBox.information(self, self.tr("Objects using this master"), self.tr(
            "{count} MDF files have materials that use this master (file · materials):\n\n{list}").format(
            count=len(sources), list="\n".join(rows[:60]) + ("\n…" if len(rows) > 60 else "")))

    @property
    def mmtr_document(self):
        if self.handler.mmtr is None:
            raise ValueError("no MMTR file is loaded")
        return self.handler.mmtr.require_document()

    def _refresh(self) -> None:
        document = self.mmtr_document
        self.summary.setText(
            self.tr(
                "{profile} · {family} material"
            ).format(
                profile=document.profile.display_name,
                family=document.family.replace("-", " ").replace("_", " ").title(),
            )
        )
        self.material_editor.set_document(document)

    def _material_project_dirty_changed(self, dirty: bool) -> None:
        if dirty:
            self._mark_modified(True)

    def _commit_material_build(self, build) -> bool:
        if build.family_builds:
            raise ValueError("A shader-LOD family must be saved together through Create Variant.")
        coverage = material_authoring_coverage(self.mmtr_document, self.material_editor.project)
        if not coverage.complete:
            QMessageBox.critical(
                self,
                self.tr("Material cannot be saved"),
                self.tr(
                    "This game profile does not fully support this material. The MMTR was not changed."
                ),
            )
            return False

        def operation(editor):
            apply_material_build(editor, self.mmtr_document, build)

        return self._commit_edit(operation)

    def prepare_save(self, file_path: str) -> bool:
        if self.object_scope is not None and self.object_scope.mdf_handler is not None and self.material_editor._dirty:
            QMessageBox.information(self, self.tr("Save with Create Variant"), self.tr(
                "This master is open to change one object's shader. Saving it in place would change every object "
                "that uses it, so use Create Variant instead."))
            return False
        payload = self.material_editor.material_save_payload()
        if payload is None:
            return True
        build, project_data = payload
        if not self._commit_material_build(build):
            return False
        project_dir = self._project_dir_for_output(file_path)
        sidecar_path = material_project_sidecar_path(file_path, project_dir)
        self.handler.queue_material_project(sidecar_path, project_data)
        self.material_editor.material_applied()
        return True

    def save_completed(self, file_path: str) -> None:
        absolute = os.path.abspath(file_path)
        self.material_editor.project_dir = self._project_dir_for_output(absolute)
        self.material_editor.source_path = absolute
        self.material_editor._project_path = material_project_sidecar_path(
            absolute,
            self.material_editor.project_dir,
        )
        self.material_editor.status.setText(
            self.tr("Material saved.")
        )
        set_style_role(self.material_editor.status, tone="success")

    def _project_dir_for_output(self, path: str) -> str:
        app = getattr(self.handler, "app", None)
        project = getattr(app, "proj_dock", None)
        candidates = (
            self.material_editor.project_dir,
            str(getattr(project, "project_dir", "") or ""),
        )
        target = Path(path).resolve()
        for candidate in candidates:
            if not candidate:
                continue
            root = Path(candidate).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            return str(root)
        return ""

    def _all_file_tabs(self):
        app = getattr(self.handler, "app", None)
        workspace = getattr(app, "project_workspace", None)
        sessions = getattr(workspace, "sessions", None)
        return tuple(sessions.all_tabs()) if sessions is not None else ()

    def _open_mdf_sources(self) -> tuple[tuple[MdfVariantSourceChoice, ...], tuple[tuple[str, str], ...]]:
        choices, unwritten = [], []
        seen = set()
        context = resource_context_for_handler(self.handler)
        project_dir = str(getattr(context, "project_dir", "") or "")
        unpacked_dir = str(getattr(context, "unpacked_dir", "") or "")
        for tab in self._all_file_tabs():
            owner_project = str(getattr(tab, "pak_project_dir", "") or "")
            if (
                project_dir
                and owner_project
                and os.path.normcase(os.path.abspath(owner_project))
                != os.path.normcase(os.path.abspath(project_dir))
            ):
                continue
            handler = getattr(tab, "handler", None)
            if not isinstance(handler, MdfHandler) or handler.mdf is None:
                continue
            parse_path = str(
                getattr(tab, "pak_source_path", "")
                or getattr(tab, "filename", "")
                or handler.filepath
                or ""
            )
            if not parse_path:
                continue
            absolute = tab_output_path(tab)
            relative = normalize_resource_path(
                str(getattr(tab, "pak_source_path", "") or "")
            )
            origin = "pak" if relative else "disk"
            if not relative and absolute:
                for candidate_origin, root in (
                    ("project", project_dir),
                    ("system", unpacked_dir),
                ):
                    if not root:
                        continue
                    try:
                        relative = Path(absolute).resolve().relative_to(
                            Path(root).resolve()
                        ).as_posix()
                        origin = candidate_origin
                        break
                    except ValueError:
                        pass
            if not relative:
                relative = Path(parse_path).name
            key = (origin, relative.casefold())
            if key in seen:
                continue
            seen.add(key)
            try:
                data = deepcopy(handler.mdf).write()
            except (ValueError, struct.error) as exc:
                unwritten.append((str(getattr(tab, "filename", "") or parse_path), str(exc)))
                continue
            choices.append(
                MdfVariantSourceChoice(
                    label=str(getattr(tab, "filename", "") or parse_path),
                    path=parse_path,
                    data=data,
                    origin=origin,
                    relative_path=relative,
                    owner=tab,
                )
            )
        return tuple(choices), tuple(unwritten)

    def _variant_source_paths(self) -> tuple[str, str]:
        family = self.mmtr_document.material_family
        if family is not None:
            resource = mmtr_resource_path_from_file(family.source_path)
            return self._fallback_mmtr_relative_path(family.source_path, resource), resource
        source = str(getattr(self.handler, "filepath", "") or "")
        tab = next(
            (
                item
                for item in self._all_file_tabs()
                if getattr(item, "handler", None) is self.handler
            ),
            None,
        )
        if tab is None:
            resource = mmtr_resource_path_from_file(source)
            return self._fallback_mmtr_relative_path(source, resource), resource
        resource_source = str(
            getattr(tab, "pak_source_path", "")
            or getattr(tab, "filename", "")
            or source
        )
        resource = mmtr_resource_path_from_file(resource_source)
        pak_path = normalize_resource_path(str(getattr(tab, "pak_source_path", "") or ""))
        if pak_path:
            return pak_path, resource
        context = resource_context_for_handler(self.handler)
        absolute = tab_output_path(tab) or source
        for root in (
            str(getattr(context, "project_dir", "") or ""),
            str(getattr(context, "unpacked_dir", "") or ""),
        ):
            if not root:
                continue
            try:
                return Path(absolute).resolve().relative_to(Path(root).resolve()).as_posix(), resource
            except ValueError:
                pass
        return self._fallback_mmtr_relative_path(source, resource), resource

    def _fallback_mmtr_relative_path(self, source: str, resource: str) -> str:
        context = resource_context_for_handler(self.handler)
        prefix = str(getattr(context, "path_prefix", "") or "natives/x64").strip("/\\")
        extension = "mmtrs" if resource.casefold().endswith(".mmtrs") else "mmtr"
        version = resource_version_from_path(source, extension)
        versioned = resource_path_with_version(resource, extension, version) if version is not None else resource
        return normalize_resource_path(f"{prefix}/{versioned}")

    def _variant_asset_context(self):
        context = resource_context_for_handler(self.handler)
        project_dir = str(getattr(context, "project_dir", "") or "")
        unpacked_dir = str(getattr(context, "unpacked_dir", "") or "")
        reader = getattr(context, "pak_cached_reader", None)
        app = getattr(self.handler, "app", None)
        project = getattr(app, "proj_dock", None)
        pak_paths = tuple(
            getattr(project, "_pak_all_paths", ())
            or getattr(project, "_pak_base_paths", ())
            or (reader.cached_paths(include_unknown=False) if reader is not None else ())
        )
        if (
            reader is not None
            and bool(getattr(reader, "cache_ready", False))
            and not bool(getattr(reader, "cache_complete", False))
        ):
            pak_paths = tuple(
                path for path in pak_paths if reader.contains_cached(path)
            )

        def load_pak(path: str):
            stream = reader.get_file(path) if reader is not None else None
            return stream.read() if stream is not None else None

        return project_dir, unpacked_dir, pak_paths, load_pak if reader is not None else None

    def _dirty_output_tab(self, path: str, *, allowed=None):
        tab = self._output_tab(path, excluded=allowed)
        if tab is not None and bool(
            getattr(tab, "modified", False)
            or getattr(getattr(tab, "handler", None), "modified", False)
            or getattr(
                getattr(getattr(tab, "viewer", None), "material_editor", None),
                "_dirty",
                False,
            )
        ):
            return tab
        return None

    def _output_tab(self, path: str, *, excluded=None):
        matches = open_tabs_for_paths(self._all_file_tabs(), (path,), exclude=excluded)
        return matches[0][0] if matches else None

    def _reload_or_open_output(self, path: str, data: bytes) -> None:
        tab = self._output_tab(path)
        if tab is not None:
            tab._reload_from_data(data, path)
            return
        app = getattr(self.handler, "app", None)
        if app is not None and hasattr(app, "add_tab"):
            app.add_tab(path, data)

    def _discover_mdf_sources(self, resource_path, project_dir, unpacked_dir, pak_paths, pak_loader):
        open_sources, unwritten = self._open_mdf_sources()
        found = run_job(
            self, self.tr("Finding MDF consumers"),
            lambda progress, _cancelled: discover_mdf_consumers(
                mmtr_resource_path=resource_path, project_dir=project_dir, unpacked_dir=unpacked_dir,
                pak_paths=pak_paths, pak_loader=pak_loader, open_sources=open_sources,
                progress=lambda count, _label: progress(count, 0)),
            status=self.tr("Finding MDF consumers…"),
            describe=lambda count, _total: self.tr("Finding MDF consumers… {count} checked").format(count=count),
        )
        rows = [f"{label}: {reason}" for label, reason in (*unwritten, *found.unread)]
        if rows:
            QMessageBox.warning(self, self.tr("Some MDF files were not read"), self.tr(
                "{count} MDF files could not be read, so the search left them out:\n\n{list}").format(
                count=len(rows), list="\n".join(rows[:12]) + ("\n…" if len(rows) > 12 else "")))
        return found.choices

    def _offer_material_instance(self) -> None:
        document = self.mmtr_document
        if document.material_family is not None or len(document.material_interface.descriptor_layouts) == 1:
            return
        answer = QMessageBox.question(
            QApplication.activeWindow() or self,
            self.tr("Find material instance"),
            self.tr(
                "This MMTR was opened without an MDF, so its material textures and parameters are unknown.\n\n"
                "Search open files, the project, unpacked files and game PAKs for an MDF that uses it?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        _mmtr_path, resource_path = self._variant_source_paths()
        sources = self._discover_mdf_sources(resource_path, *self._variant_asset_context())
        candidates = []
        for source in sources:
            # The search read each source as an MDF already.
            mdf = MdfFile()
            mdf.read(bytes(source.data), source.path)
            for index in source.matching_material_indices:
                material = mdf.materials[index]
                try:
                    bind_material_instance(document, material)
                except ValueError:
                    continue
                candidates.append((f"{material.header.mat_name}  ·  {source.label}", material))
        parent = QApplication.activeWindow() or self
        if not candidates:
            QMessageBox.information(
                parent,
                self.tr("No MDF found"),
                self.tr("No MDF that uses this MMTR was found. Material textures stay hidden until one is available."),
            )
            return
        labels = [label for label, _material in candidates]
        label, accepted = QInputDialog.getItem(
            parent,
            self.tr("Choose material instance"),
            self.tr("{count} MDF materials use this MMTR:").format(count=len(candidates)),
            labels,
            0,
            False,
        )
        if accepted:
            self.handler.bind_material_instance(candidates[labels.index(label)][1])

    def _material_variant_requested(self, build, project) -> None:
        source_mmtr_path, source_resource_path = self._variant_source_paths()
        project_dir, unpacked_dir, pak_paths, pak_loader = self._variant_asset_context()
        if not project_dir:
            QMessageBox.information(
                self,
                self.tr("Open a project"),
                self.tr(
                    "Create Variant writes game-ready files directly into a project. Open or create a project first."
                ),
            )
            return

        mdf_sources = self._discover_mdf_sources(
            source_resource_path, project_dir, unpacked_dir, pak_paths, pak_loader,
        )

        app = getattr(self.handler, "app", None)
        scope = self.object_scope
        preferred = None if scope is None or scope.mdf_handler is None else next(
            (source for source in mdf_sources if getattr(source.owner, "handler", None) is scope.mdf_handler), None)
        dialog = MaterialVariantDialog(
            project_dir=project_dir,
            source_mmtr_relative_path=source_mmtr_path,
            source_mmtr_resource_path=source_resource_path,
            mdf_sources=mdf_sources,
            pak_loader=pak_loader,
            preferred_source=preferred,
            preferred_materials=(scope.material_index,) if preferred is not None else (),
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        source = dialog.source_choice
        if source is None:
            return
        target_mmtr = dialog.mmtr_output_path
        target_mdf = dialog.mdf_output_path
        dirty = self._dirty_output_tab(target_mmtr)
        if dirty is None:
            dirty = self._dirty_output_tab(target_mdf, allowed=source.owner)
        if dirty is not None:
            QMessageBox.warning(
                self,
                self.tr("Output is open with unsaved changes"),
                self.tr(
                    "Save or close the modified output document before creating the variant."
                ),
            )
            return
        family = self.mmtr_document.material_family
        member_paths = tuple(str(Path(target_mmtr).with_name(family_member_filename(target_mmtr, member, lod)))
                             for lod, member in enumerate(family.members)) if family else ()
        if any(self._dirty_output_tab(path) is not None for path in member_paths):
            QMessageBox.warning(self, self.tr("Unsaved shader LOD"), self.tr("Save or close modified family outputs before creating this variant."))
            return
        existing_outputs = tuple(
            path
            for path in (
                target_mmtr,
                target_mdf,
                *member_paths,
            )
            if os.path.isfile(path)
        )
        if existing_outputs:
            answer = QMessageBox.question(
                self,
                self.tr("Replace project files?"),
                self.tr(
                    "These project files already exist and will be replaced:\n\n{paths}"
                ).format(
                    paths="\n".join(
                        normalize_resource_path(os.path.relpath(path, project_dir))
                        for path in existing_outputs
                    )
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            artifacts = create_material_variant_artifacts(
                self.mmtr_document,
                build,
                project,
                mdf_source_data=source.data,
                mdf_source_path=source.path,
                target_mmtr_path=target_mmtr,
                target_mdf_path=target_mdf,
                mmtr_resource_path=dialog.resource_path.text(),
                material_indices=dialog.material_indices(),
            )
            if app is None or app.settings.get("backup_on_save", True):
                for path in (
                    target_mmtr,
                    target_mdf,
                    material_project_sidecar_path(target_mmtr, project_dir),
                    *member_paths,
                ):
                    if os.path.isfile(path):
                        create_backup(path, Path(path).read_bytes())
            written = write_material_variant(
                artifacts,
                target_mmtr_path=target_mmtr,
                target_mdf_path=target_mdf,
                project_path=material_project_sidecar_path(
                    target_mmtr,
                    project_dir,
                ),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                self.tr("Material variant was not created"),
                str(exc),
            )
            return
        finally:
            QApplication.restoreOverrideCursor()

        if artifacts.member_files:
            for filename, payload in artifacts.member_files:
                path = str(Path(written.mmtr_path).with_name(filename))
                if self._output_tab(path) is not None or filename == artifacts.member_files[0][0]:
                    self._reload_or_open_output(path, payload)
        else:
            self._reload_or_open_output(written.mmtr_path, artifacts.mmtr_data)
        self._reload_or_open_output(written.mdf_path, artifacts.mdf_data)
        project_manager = getattr(app, "proj_dock", None)
        refresh_project = getattr(project_manager, "_refresh_proj", None)
        if callable(refresh_project):
            refresh_project()
        QMessageBox.information(
            self,
            self.tr("Material variant created"),
            self.tr(
                "Created project file {mmtr}\nAssigned {count} MDF material(s) in {mdf}\n"
                "All parameters and texture slots were synchronized and validated after save/reload."
            ).format(
                mmtr=normalize_resource_path(os.path.relpath(written.mmtr_path, project_dir)),
                count=len(artifacts.material_names),
                mdf=normalize_resource_path(os.path.relpath(written.mdf_path, project_dir)),
            ),
        )

__all__ = ["MmtrViewer"]
