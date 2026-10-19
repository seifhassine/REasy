from pathlib import Path

lazy import numpy as np
lazy from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.rsz.rsz_handler import RszHandler
lazy from file_handlers.rsz.scn_scene_graph import ScnSceneGraphBuilder
lazy from file_handlers.rsz.scn_scene_loader import ScnSceneSource
lazy from file_handlers.rsz.scn_scene_preview import ScnScenePreviewWidget
lazy from utils.resource_file_utils import resource_key
lazy from .chain_catalog import strand_catalog_for
lazy from .controls import LoadedSet, StrandsControls


def holds_strand_sets(handler) -> bool:
    rsz = getattr(handler, 'rsz_file', None)
    if not getattr(rsz, 'is_pfb', False):
        return False
    game = str(getattr(handler, 'game_version', '') or '')
    catalog = strand_catalog_for(mmtr_profile_for_game(game))
    if catalog is None:
        return False
    try:
        graph = ScnSceneGraphBuilder(handler.type_registry, game_version=game).build(
            rsz, root_path=str(getattr(handler, 'filepath', '') or ''))
    except (OSError, ValueError):
        return False
    return any(component.type_name == catalog.render.component
               for document in graph.documents.values() for component in document.components.values())


def create_pfb_strands_preview(handler):
    return PfbStrandsPreview(handler) if holds_strand_sets(handler) else None


class PfbStrandsPreview(QSplitter):
    """The viewer its ``controls`` call back into."""

    def __init__(self, handler, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.handler = handler
        # ``_character`` is None too when the prefab itself holds the mesh.
        self._loaded: LoadedSet | None = None
        self._character = None
        shown = QWidget(self)
        layout = QVBoxLayout(shown)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scene = ScnScenePreviewWidget(self, sources_getter=self._sources)
        self.scene.preview.strands_source = self._strands
        layout.addWidget(self.scene, 1)
        layout.addWidget(self.scene.status_label)
        self.addWidget(shown)
        self.controls = StrandsControls(self, str(getattr(handler, 'filepath', '') or ''), editing=True)
        self.controls.set_loaded.connect(self._set_loaded)
        self.addWidget(self.controls)
        self.setStretchFactor(0, 1)
        # A child timer, so it dies with the preview.
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(0)
        self._load_timer.timeout.connect(self.scene.ensure_loaded)

    @property
    def viewport(self):
        return self.scene.preview

    def strands_changed(self) -> None:
        self.scene.strands_changed()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._load_timer.start()

    def _sources(self) -> list[ScnSceneSource]:
        own = str(getattr(self.handler, 'filepath', '') or '')
        sources = [ScnSceneSource(own, self.handler, Path(own).name)]
        if self._character is not None:
            path = self._character.filepath
            sources.append(ScnSceneSource(path, self._character, Path(path).name))
        return sources

    def _set_loaded(self, loaded: LoadedSet) -> None:
        self._loaded = loaded
        path, data = loaded.character
        own = resource_key(path) == resource_key(self.controls.prefab.path)
        shown = None if self._character is None else resource_key(self._character.filepath)
        if shown != (None if own else resource_key(path)):
            self._character = None if own else self._read_character(path, data)
            self.scene.request_refresh()
        # The strands are drawn by Game lighting alone.
        self.scene.preview.set_shading('game')

    def _read_character(self, path: str, data: bytes):
        handler = RszHandler()
        handler.app, handler.filepath = getattr(self.handler, 'app', None), path
        handler.type_registry = self.handler.type_registry
        handler.game_version = self.handler.game_version
        handler.resource_context = getattr(self.handler, 'resource_context', None)
        handler.read(data)
        return handler

    def _strands(self):
        shown = self.controls.shown()
        held = None if shown is None else self._holder()
        return None if held is None else (held[0].key, held[1].handler, shown, self.controls.edited_files())

    def strands_world(self):
        """4 x 4, for column vectors."""
        held = self._holder()
        return None if held is None else np.asarray(held[0].world_matrix, np.float64).reshape(4, 4)

    def _holder(self):
        loaded = self._loaded
        if loaded is None:
            return None
        character, meshes = self._character or self.handler, {mesh.key: mesh for mesh in self.scene.loaded_meshes()}
        for graph in self.scene.graphs:
            source = self.scene.loader.source_for_graph(graph)
            if source is None or source.handler is not character:
                continue
            for renderable in graph.renderables:
                drawn, held = renderable.source_object_id, meshes.get(renderable.key)
                if (held is not None and drawn.document_id == graph.root_document_id
                        and drawn.local_object_id == loaded.holder
                        and resource_key(renderable.mesh_path) == resource_key(loaded.binding_mesh)):
                    return renderable, held
        return None

    def cleanup(self) -> None:
        self.scene.preview.strands_source = None
        self.controls.cleanup()
        self.scene.cleanup()
        self._character = self._loaded = None


__all__ = ["PfbStrandsPreview", "create_pfb_strands_preview", "holds_strand_sets"]
