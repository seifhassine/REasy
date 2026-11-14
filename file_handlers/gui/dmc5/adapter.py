"""See docs/internals/gui-editor.md, Stage."""

from __future__ import annotations

lazy from ..dependencies import GuiDependencyCatalog
lazy from ..scene import GuiResource, GuiSceneNode, GuiWorkspace
lazy from .scene import Dmc5GuiScene
lazy from .stage import Dmc5GuiStage


class Dmc5GuiAdapter:
    def create_scene(self, workspace: GuiWorkspace, resource: GuiResource, root: GuiSceneNode) -> Dmc5GuiScene:
        return Dmc5GuiScene(workspace, resource, root)

    def create_stage(self, dependencies: GuiDependencyCatalog, game: str) -> Dmc5GuiStage:
        return Dmc5GuiStage(dependencies, game)


DMC5_GUI_ADAPTER = Dmc5GuiAdapter()
