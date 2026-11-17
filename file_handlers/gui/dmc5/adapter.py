"""See docs/internals/gui-editor.md, Stage."""

from __future__ import annotations

lazy from ..dependencies import GuiDependencyCatalog
lazy from ..scene import GuiResource, GuiSceneNode, GuiWorkspace
lazy from .interaction import Dmc5GuiInteraction
lazy from .scene import Dmc5GuiScene
lazy from .stage import Dmc5GuiStage


class Dmc5GuiAdapter:
    def create_scene(self, workspace: GuiWorkspace, resource: GuiResource, root: GuiSceneNode) -> Dmc5GuiScene:
        return Dmc5GuiScene(workspace, resource, root)

    def create_stage(self, dependencies: GuiDependencyCatalog, game: str) -> Dmc5GuiStage:
        return Dmc5GuiStage(dependencies, game)

    def interaction(self, stage: Dmc5GuiStage) -> Dmc5GuiInteraction:
        return Dmc5GuiInteraction(stage)

    def text_animations(self, profile, load) -> None:
        return None


DMC5_GUI_ADAPTER = Dmc5GuiAdapter()
