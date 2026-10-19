from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import struct

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from file_handlers.sdf.sdf_file import (
    BLEND_INVERSE_SOURCE_ALPHA, BLEND_NAMES, BLEND_OPERATION_ADD, BLEND_OPERATION_NAMES, BLEND_SOURCE_ALPHA,
    COMPARE_ALWAYS, COMPARE_NEVER, COMPARISON_FUNCTION_NAMES, CULL_BACK, CULL_MODE_NAMES, CULL_NONE,
    DEPTH_WRITE_MASK_NAMES, FILL_MODE_NAMES, STENCIL_OPERATION_NAMES, write_channels,
)

from file_handlers.sdf.models import STATE_KINDS
from .pass_purpose import pass_purpose

_FIELDS = {
    "rasterizer": ("fill_mode_id", "cull_mode_id", "front_counter_clockwise", "depth_clip_enable", "scissor_enable",
                   "multisample_enable", "antialiased_line_enable", "conservative_rasterization", "depth_bias",
                   "depth_bias_clamp", "slope_scaled_depth_bias"),
    "blend": ("alpha_to_coverage_enable", "independent_blend_enable"),
    "depth": ("depth_enable", "depth_write_mask_id", "depth_function_id", "depth_bounds_test_enable",
              "stencil_enable", "stencil_read_mask", "stencil_write_mask"),
}
_TARGET_FIELDS = ("blend_enable", "source_blend_id", "destination_blend_id", "blend_operation_id",
                  "source_alpha_blend_id", "destination_alpha_blend_id", "alpha_blend_operation_id", "write_mask")
_FACE_FIELDS = ("stencil_fail_operation_id", "stencil_depth_fail_operation_id", "stencil_pass_operation_id",
                "stencil_function_id")
_FACES = ("front_face", "back_face")
_NAMES = {
    "fill_mode_id": ("fill_mode", FILL_MODE_NAMES), "cull_mode_id": ("cull_mode", CULL_MODE_NAMES),
    "source_blend_id": ("source_blend", BLEND_NAMES), "destination_blend_id": ("destination_blend", BLEND_NAMES),
    "blend_operation_id": ("blend_operation", BLEND_OPERATION_NAMES),
    "source_alpha_blend_id": ("source_alpha_blend", BLEND_NAMES),
    "destination_alpha_blend_id": ("destination_alpha_blend", BLEND_NAMES),
    "alpha_blend_operation_id": ("alpha_blend_operation", BLEND_OPERATION_NAMES),
    "depth_write_mask_id": ("depth_write_mask", DEPTH_WRITE_MASK_NAMES),
    "depth_function_id": ("depth_function", COMPARISON_FUNCTION_NAMES),
    "stencil_fail_operation_id": ("stencil_fail_operation", STENCIL_OPERATION_NAMES),
    "stencil_depth_fail_operation_id": ("stencil_depth_fail_operation", STENCIL_OPERATION_NAMES),
    "stencil_pass_operation_id": ("stencil_pass_operation", STENCIL_OPERATION_NAMES),
    "stencil_function_id": ("stencil_function", COMPARISON_FUNCTION_NAMES),
}


class PipelineSettingError(ValueError):
    pass


translate = QCoreApplication.translate


def pass_key(target_index: int, program_index: int, lod: int | None = None) -> str:
    key = f"{int(target_index)}:{int(program_index)}"
    return key if lod is None else f"{int(lod)}/{key}"


def member_states(states: Mapping, lod: int | None) -> dict:
    if lod is None:
        return dict(states)
    prefix = f"{int(lod)}/"
    return {key[len(prefix):]: changes for key, changes in states.items() if key.startswith(prefix)}


def with_member_states(states: Mapping, lods, changes: Mapping) -> dict:
    if lods == (None,):
        return dict(changes)
    prefixes = tuple(f"{int(lod)}/" for lod in lods)
    result = {key: value for key, value in states.items() if not key.startswith(prefixes)}
    for prefix in prefixes:
        result.update({prefix + key: value for key, value in changes.items()})
    return result


def setting_members(document) -> tuple[tuple[tuple, object], ...]:
    from .material_family import distinct_members
    family = getattr(document, "material_family", None)
    return (((None,), document),) if family is None else distinct_members(family)


def parse_pass_key(key: str) -> tuple[int, int]:
    target, _, program = str(key).partition(":")
    return int(target), int(program)


def _named(item, values: Mapping):
    changes = {}
    for field, value in values.items():
        changes[field] = value
        if field in _NAMES:
            name, table = _NAMES[field]
            changes[name] = table.get(int(value), f"value-{int(value)}")
        elif field == "write_mask":
            changes["write_channels"] = write_channels(int(value))
    return replace(item, **changes)


def state_values(program, kind: str) -> dict:
    """Keys as ``SdfBinaryEditor.set_*_state`` takes them."""
    state = getattr(program, STATE_KINDS[kind][0])
    if state is None:
        return {}
    values = {field: getattr(state, field) for field in _FIELDS[kind]}
    if kind == "blend":
        values["render_targets"] = [{field: getattr(target, field) for field in _TARGET_FIELDS}
                                    for target in state.render_targets]
    elif kind == "depth":
        values.update({face: {field: getattr(getattr(state, face), field) for field in _FACE_FIELDS}
                       for face in _FACES})
    return values


def changed_fields(original: Mapping, values: Mapping) -> dict:
    result = {}
    for field, value in values.items():
        if field == "render_targets":
            targets = [changed_fields(before, after) for before, after in zip(original[field], value)]
            while targets and not targets[-1]:
                targets.pop()
            if targets:
                result[field] = targets
        elif isinstance(value, Mapping):
            face = changed_fields(original[field], value)
            if face:
                result[field] = face
        elif not _same(value, original[field]):
            result[field] = value
    return result


def _same(value, original) -> bool:
    """The state stores floats as 32-bit."""
    if isinstance(original, float):
        return struct.pack("<f", float(value)) == struct.pack("<f", original)
    return value == original


def merged_values(original: Mapping, change: Mapping) -> dict:
    result = dict(original)
    for field, value in change.items():
        if field == "render_targets":
            result[field] = [merged_values(before, value[index] if index < len(value) else {})
                             for index, before in enumerate(original[field])]
        elif isinstance(value, Mapping):
            result[field] = merged_values(original[field], value)
        else:
            result[field] = value
    return result


def effective_program(program, changes: Mapping):
    states = {}
    for kind, change in changes.items():
        attribute = STATE_KINDS[kind][0]
        state = getattr(program, attribute)
        flat = {field: value for field, value in change.items() if field not in {"render_targets", *_FACES}}
        if kind == "blend" and "render_targets" in change:
            targets = change["render_targets"]
            flat["render_targets"] = tuple(_named(target, targets[index]) if index < len(targets) else target
                                           for index, target in enumerate(state.render_targets))
        for face in _FACES:
            if face in change:
                flat[face] = _named(getattr(state, face), change[face])
        states[attribute] = _named(state, flat)
    return replace(program, **states)


def validate_pipeline_states(document, states: Mapping) -> None:
    family = getattr(document, "material_family", None)
    if family is not None:
        lods = range(len(family.members))
        stray = sorted(key for key in states if not any(key.startswith(f"{lod}/") for lod in lods))
        if stray:
            raise PipelineSettingError(f"Render settings name pass {stray[0]!r} without a shader LOD of this family.")
        for lod, member in enumerate(family.members):
            try:
                validate_pipeline_states(member, member_states(states, lod))
            except PipelineSettingError as exc:
                raise PipelineSettingError(f"Shader LOD {lod}: {exc}") from exc
        return
    for key, changes in states.items():
        try:
            target_index, program_index = parse_pass_key(key)
            program = document.sdf.targets[target_index].programs[program_index]
        except (ValueError, IndexError) as exc:
            raise PipelineSettingError(
                f"Render settings name pass {key!r}, which this material does not have.") from exc
        for kind, change in changes.items():
            if kind not in STATE_KINDS:
                raise PipelineSettingError(f"Render settings of pass {key} name unknown state {kind!r}.")
            original = state_values(program, kind)
            if not original:
                raise PipelineSettingError(f"Pass {program.name} has no {kind} state to change.")
            try:
                merged_values(original, change)
                unknown = set(change) - set(original)
                targets = change.get("render_targets", ())
                if len(targets) > len(original.get("render_targets", ())):
                    raise KeyError("render_targets")
                unknown |= {field for target in targets for field in target if field not in _TARGET_FIELDS}
                unknown |= {field for face in _FACES for field in change.get(face, {}) if field not in _FACE_FIELDS}
            except (KeyError, TypeError, AttributeError) as exc:
                raise PipelineSettingError(f"Render settings of {program.name} are malformed: {exc}") from exc
            if unknown:
                raise PipelineSettingError(f"Render settings of {program.name} name unknown fields "
                                           f"{', '.join(sorted(map(str, unknown)))}.")


def apply_pipeline_states(editor, states: Mapping) -> None:
    for key in sorted(states, key=parse_pass_key):
        target_index, program_index = parse_pass_key(key)
        for kind, change in sorted(states[key].items()):
            if change:
                getattr(editor, STATE_KINDS[kind][1])(target_index, program_index, change, isolate=True)


@dataclass(frozen=True)
class PlainSetting:
    key: str
    title: str
    description: str
    kind: str
    purposes: frozenset[str] | None
    holds: Callable[[Mapping], bool]
    on: Mapping | None
    off: Mapping | None

    @property
    def display_title(self) -> str:
        return translate("PipelineSettings", self.title)

    @property
    def display_description(self) -> str:
        return translate("PipelineSettings", self.description)


def _transparent(values: Mapping) -> bool:
    target = values["render_targets"][0] if values["render_targets"] else None
    return bool(target and target["blend_enable"] and target["source_blend_id"] == BLEND_SOURCE_ALPHA
                and target["destination_blend_id"] == BLEND_INVERSE_SOURCE_ALPHA)


PLAIN_SETTINGS = (
    PlainSetting(
        "double_sided", QT_TRANSLATE_NOOP("PipelineSettings", "Double-sided"),
        QT_TRANSLATE_NOOP("PipelineSettings",
                          "Draw the back of each triangle too, so thin surfaces show from both sides."),
        "rasterizer", None, lambda values: values["cull_mode_id"] == CULL_NONE,
        {"cull_mode_id": CULL_NONE}, {"cull_mode_id": CULL_BACK}),
    PlainSetting(
        "transparent", QT_TRANSLATE_NOOP("PipelineSettings", "Transparent"),
        QT_TRANSLATE_NOOP("PipelineSettings", "Blend the first colour output over what is behind it by its alpha. "
                                              "Only forward passes can blend; G-buffer passes cannot."),
        "blend", frozenset({"forward", "colour"}), _transparent,
        {"render_targets": [{"blend_enable": True, "source_blend_id": BLEND_SOURCE_ALPHA,
                             "destination_blend_id": BLEND_INVERSE_SOURCE_ALPHA, "blend_operation_id": BLEND_OPERATION_ADD}]},
        {"render_targets": [{"blend_enable": False}]}),
    PlainSetting(
        "through_walls", QT_TRANSLATE_NOOP("PipelineSettings", "Visible through walls"),
        QT_TRANSLATE_NOOP("PipelineSettings", "Draw the visible colour even where something else is in front."),
        "depth", frozenset({"gbuffer", "forward"}),
        lambda values: not values["depth_enable"] or values["depth_function_id"] == COMPARE_ALWAYS,
        {"depth_function_id": COMPARE_ALWAYS}, None),
    PlainSetting(
        "casts_shadows", QT_TRANSLATE_NOOP("PipelineSettings", "Casts shadows"),
        QT_TRANSLATE_NOOP("PipelineSettings",
                          "Draw into shadow maps. Turned off, the shadow passes keep running but write nothing."),
        "depth", frozenset({"shadow"}),
        lambda values: not (values["depth_enable"] and values["depth_function_id"] == COMPARE_NEVER),
        None, {"depth_enable": True, "depth_function_id": COMPARE_NEVER}),
)


def plain_setting_passes(document, setting: PlainSetting) -> tuple[tuple[int, int], ...]:
    profile = document.profile.shader_graph_profile
    attribute = STATE_KINDS[setting.kind][0]
    return tuple((target.index, program.program_index) for target in document.sdf.targets for program in target.programs
                 if getattr(program, attribute) is not None
                 and (setting.purposes is None
                      or pass_purpose(document, profile, target.index, program.program_index) in setting.purposes))


def plain_setting_state(document, states: Mapping, setting: PlainSetting) -> tuple[int, int]:
    total = holding = 0
    for lods, member in setting_members(document):
        changes = member_states(states, lods[0])
        passes = plain_setting_passes(member, setting)
        total += len(passes)
        holding += sum(setting.holds(merged_values(
            state_values(member.sdf.targets[item[0]].programs[item[1]], setting.kind),
            changes.get(pass_key(*item), {}).get(setting.kind, {}))) for item in passes)
    return total, holding


def _without(change: dict, fields: Mapping) -> dict:
    result = {}
    for field, value in change.items():
        if field not in fields:
            result[field] = value
        elif field == "render_targets":
            targets = [_without(dict(target), fields[field][index]) if index < len(fields[field]) else target
                       for index, target in enumerate(value)]
            while targets and not targets[-1]:
                targets.pop()
            if targets:
                result[field] = targets
        elif isinstance(value, Mapping):
            face = _without(dict(value), fields[field])
            if face:
                result[field] = face
    return result


def with_plain_setting(document, states: Mapping, setting: PlainSetting, enabled: bool) -> dict:
    result = dict(states)
    for lods, member in setting_members(document):
        result = with_member_states(result, lods, _with_plain_setting(member, member_states(result, lods[0]), setting,
                                                                      enabled))
    return result


def _with_plain_setting(document, states: Mapping, setting: PlainSetting, enabled: bool) -> dict:
    result = {key: {kind: dict(change) for kind, change in changes.items()} for key, changes in states.items()}
    values = setting.on if enabled else setting.off
    fields = _merge_change(setting.on or {}, setting.off or {})
    for target_index, program_index in plain_setting_passes(document, setting):
        key = pass_key(target_index, program_index)
        program = document.sdf.targets[target_index].programs[program_index]
        change = _without(result.get(key, {}).get(setting.kind, {}), fields)
        if setting.holds(state_values(program, setting.kind)) != enabled:
            if values is None:
                raise PipelineSettingError(translate(
                    "PipelineSettings", "{setting} cannot be changed for {name} on its own. Change the pass's fields "
                                        "instead.").format(setting=setting.display_title, name=program.name))
            change = _merge_change(change, values)
        if change:
            result.setdefault(key, {})[setting.kind] = change
        elif key in result:
            result[key].pop(setting.kind, None)
            if not result[key]:
                del result[key]
    return result


def _merge_change(change: Mapping, values: Mapping) -> dict:
    result = dict(change)
    for field, value in values.items():
        if field == "render_targets":
            current = list(result.get(field, ()))
            current.extend({} for _ in range(len(value) - len(current)))
            result[field] = [_merge_change(item, value[index]) if index < len(value) else item
                             for index, item in enumerate(current)]
        elif isinstance(value, Mapping):
            result[field] = _merge_change(result.get(field, {}), value)
        else:
            result[field] = value
    return result


__all__ = [
    "PLAIN_SETTINGS", "STATE_KINDS", "PipelineSettingError", "PlainSetting", "apply_pipeline_states",
    "changed_fields", "effective_program", "member_states", "merged_values", "parse_pass_key", "pass_key",
    "plain_setting_passes", "plain_setting_state", "setting_members", "state_values", "validate_pipeline_states",
    "with_member_states", "with_plain_setting",
]
