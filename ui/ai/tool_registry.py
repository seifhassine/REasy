from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from PySide6.QtCore import QCoreApplication


class AssistantToolError(ValueError):
    """A user-facing validation or execution error from an assistant tool."""


def translate_tool_text(source: str, **values: Any) -> str:
    translated = QCoreApplication.translate("ReasyAssistantTools", source)
    return translated.format(**values) if values else translated


@dataclass(frozen=True)
class ToolSchemaContext:
    available_capabilities: frozenset[str]
    mutation_tool_names: tuple[str, ...]


@dataclass(frozen=True)
class AiToolDefinition:

    name: str
    description: str
    properties: (
        dict[str, Any]
        | Callable[[ToolSchemaContext], dict[str, Any]]
        | None
    )
    required: tuple[str, ...]
    activity: tuple[str, str]
    capability: str | tuple[str, ...] | None = None
    handler: str | None = None
    incremental: bool = False
    mutation: bool = False
    ui_edit: bool = False
    persistent: bool = False
    result_card: bool = False
    unsaved_result: bool = False

    @property
    def handler_name(self) -> str:
        return self.handler or f"_{self.name}"

    @property
    def incremental_handler_name(self) -> str | None:
        return f"_{self.name}_steps" if self.incremental else None

    @property
    def confirmation_handler_name(self) -> str | None:
        return f"_summarize_{self.name}" if self.persistent else None

    def is_enabled(self, capabilities: Iterable[str]) -> bool:
        if self.capability is None:
            return True
        enabled = frozenset(capabilities)
        if isinstance(self.capability, str):
            return self.capability in enabled
        return any(name in enabled for name in self.capability)

    def schema(self, context: ToolSchemaContext) -> dict[str, Any]:
        source = (
            self.properties(context)
            if callable(self.properties)
            else self.properties
        )
        properties = copy.deepcopy(source or {})
        if self.mutation:
            properties["tab"] = {
                "type": "string",
                "description": (
                    "Optional exact destination tab ID. Omit only to edit "
                    "the active compatible editor."
                ),
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


def tool(
    name: str,
    description: str,
    properties: (
        dict[str, Any]
        | Callable[[ToolSchemaContext], dict[str, Any]]
        | None
    ) = None,
    required: Iterable[str] = (),
    *,
    activity: tuple[str, str],
    capability: str | tuple[str, ...] | None = None,
    handler: str | None = None,
    incremental: bool = False,
    mutation: bool = False,
    ui_edit: bool = False,
    persistent: bool = False,
    result_card: bool = False,
    unsaved_result: bool = False,
) -> AiToolDefinition:
    return AiToolDefinition(
        name=name,
        description=description,
        properties=properties,
        required=tuple(required),
        activity=activity,
        capability=capability,
        handler=handler,
        incremental=incremental,
        mutation=mutation,
        ui_edit=ui_edit or mutation,
        persistent=persistent,
        result_card=result_card or persistent,
        unsaved_result=unsaved_result or mutation,
    )
