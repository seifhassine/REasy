"""Exact DMC5 GUIR 270020 format/runtime adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..adapter import GuiPreviewControl, GuiPreviewScenario, GuiRuntimeEditorConfig
from ..errors import GuiFormatError

if TYPE_CHECKING:
    from file_handlers.clip.enums import PropertyType
    from utils.resource_file_utils import ResourceDataLoader

    from ..dependencies import GuiDependencyCatalog
    from ..model import GuiDocument
    from ..profiles import GuiFormatProfile
    from ..scene import GuiResource, GuiScene, GuiSceneNode, GuiWorkspace


DMC5_GUI_VERSION = 270020
DMC5_DEFAULT_SAFE_AREA_RATIO = 0.9
DMC5_DEFAULT_LANGUAGE = 1


class Dmc5GuiAdapter:
    version = DMC5_GUI_VERSION
    name = f"DMC5 GUIR {DMC5_GUI_VERSION}"
    editor = GuiRuntimeEditorConfig(
        default_safe_area_ratio=DMC5_DEFAULT_SAFE_AREA_RATIO,
        safe_area_tooltip=(
            "DMC5 GUISystem.SafeAreaRatio runtime value (native default 0.9)"
        ),
        default_language=DMC5_DEFAULT_LANGUAGE,
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

    def require_profile(self, profile: "GuiFormatProfile", source: str) -> None:
        if profile.adapter is not self or profile.version != self.version:
            raise GuiFormatError(
                f"{source}: {self.name} cannot use profile {profile.name!r}"
            )

    def parse(
        self,
        data: bytes,
        source: str,
        profile: "GuiFormatProfile",
    ) -> "GuiDocument":
        from .codec import Dmc5GuiCodec

        return Dmc5GuiCodec(data, source, profile).parse()

    def serialize(
        self,
        document: "GuiDocument",
        profile: "GuiFormatProfile",
    ) -> bytes:
        from .serializer import Dmc5GuiSerializer

        return Dmc5GuiSerializer(profile).build(document)

    def create_scene(
        self,
        workspace: "GuiWorkspace",
        resource: "GuiResource",
        root: "GuiSceneNode",
    ) -> "GuiScene":
        self.require_profile(workspace.profile, resource.source)
        from .scene import Dmc5GuiScene

        return Dmc5GuiScene(workspace, resource, root)

    def create_canvas(
        self,
        dependencies: "GuiDependencyCatalog | None" = None,
    ) -> Any:
        from .preview_widget import Dmc5GuiCanvas

        return Dmc5GuiCanvas(dependencies)

    def preview_scenarios(
        self,
        gui_path: str,
        loader: "ResourceDataLoader | None",
        profile: "GuiFormatProfile",
        scene: "GuiScene",
    ) -> tuple[GuiPreviewScenario, ...]:
        self.require_profile(profile, gui_path)
        from .controllers import (
            build_dmc5_preview_scenarios,
            discover_dmc5_controller_context,
        )

        context = discover_dmc5_controller_context(gui_path, loader, profile)
        return build_dmc5_preview_scenarios(context, scene, gui_path, profile)

    def apply_preview_scenario(
        self,
        canvas: Any,
        scenario: GuiPreviewScenario | None,
    ) -> None:
        from .controllers import Dmc5PreviewState

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
        from .custom_preview import dmc5_preview_controls

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
        from .custom_preview import set_dmc5_preview_control

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
        from .custom_preview import rebase_dmc5_custom_preview

        return rebase_dmc5_custom_preview(scenarios, scenario, scene)

    def export_preview_preset(
        self,
        gui_path: str,
        scenario: GuiPreviewScenario,
    ) -> dict[str, Any]:
        from .custom_preview import export_dmc5_preview_preset

        return export_dmc5_preview_preset(gui_path, self.version, scenario)

    def import_preview_preset(
        self,
        gui_path: str,
        payload: dict[str, Any],
        scenarios: tuple[GuiPreviewScenario, ...],
        scene: "GuiScene",
    ) -> GuiPreviewScenario:
        from .custom_preview import import_dmc5_preview_preset

        return import_dmc5_preview_preset(
            gui_path,
            self.version,
            payload,
            scenarios,
            scene,
        )

    def validate_property_value(
        self,
        kind: "PropertyType",
        value: Any,
        name: str,
    ) -> None:
        from .property_codec import encode_dmc5_gui_value

        encode_dmc5_gui_value(kind, value, name)


DMC5_GUI_ADAPTER = Dmc5GuiAdapter()
