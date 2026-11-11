"""See docs/internals/gui-format.md."""

from __future__ import annotations

from typing import Any

from ..adapter import GuiPreviewControl, GuiPreviewScenario, GuiRuntimeEditorConfig

lazy from utils.resource_file_utils import ResourceDataLoader

lazy from ..dependencies import GuiDependencyCatalog
lazy from ..profiles import GuiRuntimeProfile
lazy from ..scene import GuiResource, GuiScene, GuiSceneNode, GuiWorkspace
lazy from .scene import Dmc5GuiScene
lazy from .preview_widget import Dmc5GuiCanvas
lazy from .controllers import build_dmc5_preview_scenarios, discover_dmc5_controller_context, Dmc5PreviewState
lazy from .custom_preview import (
    dmc5_preview_controls, set_dmc5_preview_control, rebase_dmc5_custom_preview, export_dmc5_preview_preset,
    import_dmc5_preview_preset,
)


class Dmc5GuiAdapter:
    editor = GuiRuntimeEditorConfig(
        safe_area_tooltip=(
            "DMC5 GUISystem.SafeAreaRatio runtime value (native default 0.9)"
        ),
        input_devices=("Keyboard", "Gamepad"),
        input_tooltip=(
            "Runtime input device; action bindings are external, while DMC5's "
            "keyboard menu aliases are native constants"
        ),
        interaction_tooltip=(
            "Interaction mode: send mouse and keyboard input through recovered "
            "DMC5 runtime behavior"
        ),
        interaction_description="Mouse and keyboard drive recovered DMC5 behavior",
        scenario_tooltip=(
            "Choose the file default or an available game-runtime state"
        ),
    )

    def create_scene(
        self,
        workspace: "GuiWorkspace",
        resource: "GuiResource",
        root: "GuiSceneNode",
    ) -> "GuiScene":
        return Dmc5GuiScene(workspace, resource, root)

    def create_canvas(
        self,
        dependencies: "GuiDependencyCatalog",
        game: str,
    ) -> Any:
        return Dmc5GuiCanvas(dependencies, game=game)

    def preview_scenarios(
        self,
        gui_path: str,
        loader: "ResourceDataLoader | None",
        profile: "GuiRuntimeProfile",
        scene: "GuiScene",
    ) -> tuple[GuiPreviewScenario, ...]:
        context = discover_dmc5_controller_context(gui_path, loader, profile, scene.resource.document.version)
        return build_dmc5_preview_scenarios(context, scene, gui_path, profile)

    def apply_preview_scenario(
        self,
        canvas: Any,
        scenario: GuiPreviewScenario | None,
    ) -> None:
        state = scenario.state if scenario is not None else None
        if not isinstance(state, Dmc5PreviewState):
            state = Dmc5PreviewState()
        canvas.set_controller_context(state.controller)
        canvas.set_runtime_properties(
            {path: dict(values) for path, values in state.properties}
        )
        canvas.set_runtime_active(dict(state.active))
        canvas.set_runtime_playback(
            {path: dict(values) for path, values in state.playback}
        )

    def preview_controls(
        self,
        scenarios: tuple[GuiPreviewScenario, ...],
        scenario: GuiPreviewScenario,
        scene: "GuiScene",
        selected_path: str | None,
    ) -> tuple[GuiPreviewControl, ...]:
        return dmc5_preview_controls(
            scenarios,
            scenario,
            scene,
            selected_path,
        )

    def set_preview_control(
        self,
        scenarios: tuple[GuiPreviewScenario, ...],
        scenario: GuiPreviewScenario,
        scene: "GuiScene",
        key: Any,
        value: Any,
        *,
        inherit: bool = False,
    ) -> GuiPreviewScenario:
        return set_dmc5_preview_control(
            scenarios,
            scenario,
            scene,
            key,
            value,
            inherit=inherit,
        )

    def rebase_custom_preview(
        self,
        scenarios: tuple[GuiPreviewScenario, ...],
        scenario: GuiPreviewScenario,
        scene: "GuiScene",
    ) -> GuiPreviewScenario:
        return rebase_dmc5_custom_preview(scenarios, scenario, scene)

    def export_preview_preset(
        self,
        gui_path: str,
        gui_version: int,
        scenario: GuiPreviewScenario,
    ) -> dict[str, Any]:
        return export_dmc5_preview_preset(gui_path, gui_version, scenario)

    def import_preview_preset(
        self,
        gui_path: str,
        gui_version: int,
        payload: dict[str, Any],
        scenarios: tuple[GuiPreviewScenario, ...],
        scene: "GuiScene",
    ) -> GuiPreviewScenario:
        return import_dmc5_preview_preset(
            gui_path,
            gui_version,
            payload,
            scenarios,
            scene,
        )


DMC5_GUI_ADAPTER = Dmc5GuiAdapter()
