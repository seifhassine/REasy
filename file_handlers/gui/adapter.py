"""See docs/internals/gui-format.md."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

lazy from file_handlers.clip.rules import PropertyType
lazy from utils.resource_file_utils import ResourceDataLoader

lazy from .dependencies import GuiDependencyCatalog
lazy from .profiles import GuiRuntimeProfile
lazy from .scene import GuiResource, GuiScene, GuiSceneNode, GuiWorkspace


@dataclass(frozen=True, slots=True)
class GuiRuntimeEditorConfig:
    """Runtime controls exposed by the generic editor for one exact format."""

    safe_area_tooltip: str
    input_devices: tuple[str, ...]
    input_tooltip: str
    interaction_tooltip: str
    interaction_description: str
    scenario_tooltip: str


@dataclass(frozen=True, slots=True)
class GuiPreviewScenario:
    """One coherent set of external state used to evaluate a GUI preview."""

    key: str
    label: str
    description: str
    coverage: str
    state: Any
    preferred: bool = False
    issues: tuple[str, ...] = ()
    custom: bool = False
    base_key: str | None = None


@dataclass(frozen=True, slots=True)
class GuiPreviewOption:
    """One semantic value offered by a preview-state control."""

    value: Any
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class GuiPreviewControl:
    """Format-described control rendered by the generic preview-state editor."""

    key: Any
    group: str
    label: str
    description: str
    value: Any
    value_type: "PropertyType | None" = None
    options: tuple[GuiPreviewOption, ...] = ()
    can_inherit: bool = False
    inherited: bool = False
    minimum: float | None = None
    maximum: float | None = None
    decimals: int | None = None


class GuiRuntimeAdapter(Protocol):
    """All semantics that vary with a game's GUI runtime."""

    editor: GuiRuntimeEditorConfig

    def create_scene(
        self,
        workspace: "GuiWorkspace",
        resource: "GuiResource",
        root: "GuiSceneNode",
    ) -> "GuiScene": ...

    def create_canvas(
        self,
        dependencies: "GuiDependencyCatalog",
        game: str,
    ) -> Any: ...

    def preview_scenarios(
        self,
        gui_path: str,
        loader: "ResourceDataLoader | None",
        profile: "GuiRuntimeProfile",
        scene: "GuiScene",
    ) -> tuple[GuiPreviewScenario, ...]: ...

    def apply_preview_scenario(
        self,
        canvas: Any,
        scenario: GuiPreviewScenario | None,
    ) -> None: ...

    def preview_controls(
        self,
        scenarios: tuple[GuiPreviewScenario, ...],
        scenario: GuiPreviewScenario,
        scene: "GuiScene",
        selected_path: str | None,
    ) -> tuple[GuiPreviewControl, ...]: ...

    def set_preview_control(
        self,
        scenarios: tuple[GuiPreviewScenario, ...],
        scenario: GuiPreviewScenario,
        scene: "GuiScene",
        key: Any,
        value: Any,
        *,
        inherit: bool = False,
    ) -> GuiPreviewScenario: ...

    def rebase_custom_preview(
        self,
        scenarios: tuple[GuiPreviewScenario, ...],
        scenario: GuiPreviewScenario,
        scene: "GuiScene",
    ) -> GuiPreviewScenario: ...

    def export_preview_preset(
        self,
        gui_path: str,
        gui_version: int,
        scenario: GuiPreviewScenario,
    ) -> dict[str, Any]: ...

    def import_preview_preset(
        self,
        gui_path: str,
        gui_version: int,
        payload: dict[str, Any],
        scenarios: tuple[GuiPreviewScenario, ...],
        scene: "GuiScene",
    ) -> GuiPreviewScenario: ...
