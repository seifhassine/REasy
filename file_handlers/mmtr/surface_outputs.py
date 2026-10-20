from __future__ import annotations

lazy from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
lazy import multiprocessing
import os
import re

from file_handlers.bounded_cache import BoundedCache
lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.sdf.spirv_ir import SpirvSemanticIr
from utils.garbage_collection import configure_garbage_collector

lazy from .surface_flow import ChannelSplit, SurfaceChannel, recovered_surface_flow, surface_channels
lazy from .surface_meaning import GBUFFER_PURPOSE, pixel_shader_digest, shader_bytes
lazy from .surface_reach import channel_reach, parameter_sources
lazy from .profiles.re_engine_dxil_recovery import recover_re_engine_dxil_ir
lazy from .surface_data import projected_position


SURFACE_OUTPUTS_VERSION = 6


@dataclass(frozen=True, slots=True)
class SurfaceTap:
    name: str
    split: ChannelSplit
    form: str
    value_type: str


@dataclass(frozen=True, slots=True)
class SurfaceEdit:
    """``form``: ``value`` (before the channel's packing) or ``stored``."""
    split: ChannelSplit
    form: str
    expression: str
    value_type: str
    taps: tuple[SurfaceTap, ...] = ()
    # The expression reads ``REasyMaterialAddress``.
    material: bool = False


_CACHE_LIMIT = 4096
# Fewer programs than this are analysed in the calling process: starting workers would cost more.
_PARALLEL_MINIMUM = 32
_CHANNEL_CACHE = BoundedCache()
_SEMANTIC_LABELS = {"sv_target": "Target {index}", "sv_depth": "Depth", "sv_depthgreaterequal": "Depth",
                    "sv_depthlessequal": "Depth", "sv_coverage": "Coverage", "sv_stencilref": "Stencil Reference",
                    "sv_position": "Position", "sv_clipdistance": "Clip Distance {index}",
                    "sv_culldistance": "Cull Distance {index}"}


def _recover_ir(ir_kind: str, data: bytes):
    if ir_kind == "spirv":
        return SpirvSemanticIr.from_bytes(data)
    if ir_kind == "dxil":
        return recover_re_engine_dxil_ir(data)
    from .profiles.dxbc_recovery import recover_dxbc_ir
    return recover_dxbc_ir(data)


def _analyse(ir_kind: str, data: bytes, contract, buffer_name: str, prefix: int):
    ir = _recover_ir(ir_kind, data)
    flow = recovered_surface_flow(ir_kind, ir, contract)
    splits = recovered_channels(ir_kind, ir, contract, flow)
    sources = {value: f"parameter@{offset}"
               for value, offset in parameter_sources(ir_kind, ir, contract, flow, buffer_name, prefix).items()}
    sources.update((store.value, f"texture:{split.channel.semantic}") for split in splits
                   if split.channel.scope == "read" for store in split.stores)
    return splits, channel_reach(flow, splits, sources)


def _analyse_job(arguments):
    try:
        return _analyse(*arguments)
    except ShaderGraphError:
        return None


def _surface_key(ir_kind: str, contract, digest: str, buffer_name: str, prefix: int) -> tuple:
    return f"{ir_kind}:{digest}", contract.fingerprint, buffer_name, prefix


def program_surface(ir_kind: str, data: bytes, contract, digest: str, buffer_name: str = "", prefix: int = 0):
    """Reach keys are ``parameter@<offset>`` and ``texture:<name>``."""
    key = _surface_key(ir_kind, contract, digest, buffer_name, prefix)
    found = _CHANNEL_CACHE.get(key)
    if found is None:
        found = _analyse(ir_kind, data, contract, buffer_name, prefix)
        _CHANNEL_CACHE.put(key, found, 0, _CACHE_LIMIT, 0)
    return found


def prefetch_program_surfaces(jobs) -> None:
    pending = {}
    for ir_kind, data, contract, digest, buffer_name, prefix in jobs:
        key = _surface_key(ir_kind, contract, digest, buffer_name, prefix)
        if key not in _CHANNEL_CACHE.entries:
            pending.setdefault(key, (ir_kind, data, contract, buffer_name, prefix))
    if len(pending) < _PARALLEL_MINIMUM:
        return
    workers = min(os.process_cpu_count() or 1, len(pending) // (_PARALLEL_MINIMUM // 4))
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=configure_garbage_collector) as pool:
        results = pool.map(_analyse_job, pending.values(), chunksize=max(1, len(pending) // (workers * 4)))
        for key, result in zip(pending, results):
            if result is not None:
                _CHANNEL_CACHE.put(key, result, 0, _CACHE_LIMIT, 0)


def recovered_channels(ir_kind: str, ir, contract, flow) -> tuple[ChannelSplit, ...]:
    splits = surface_channels(flow)
    if flow.stage == "vertex":
        position = projected_position(ir_kind, ir, contract, flow)
        if position is not None:
            splits = (*splits, position)
    return splits


def material_texture_names(layouts, shader_lod, prefix: int) -> dict[int, str]:
    if len(layouts) != 1:
        return {}
    return {prefix + offset: name for (kind, name), offset in layouts[0].word_offsets(shader_lod).items()
            if kind == "texture"}


def named_texture_reads(splits: tuple[ChannelSplit, ...], names: dict[int, str]) -> tuple[ChannelSplit, ...]:
    result = []
    for split in splits:
        channel = split.channel
        name = names.get(int(channel.semantic[1:])) if channel.scope == "read" and channel.semantic.startswith("@") else None
        if name is not None:
            split = replace(split, channel=replace(channel, semantic=name),
                            stores=tuple(replace(store, semantic=name) for store in split.stores))
        result.append(split)
    return tuple(result)


def _shader_bytes(document, profile, program, role: str) -> tuple:
    shader = program.shader(role)
    contract = profile.shader_contract(document, program, role)
    kind = "spirv" if shader.spirv is not None else "dxil" if int(contract.model_major) >= 6 else "dxbc-sm5"
    return kind, shader_bytes(shader), contract, pixel_shader_digest(shader)


def _native_job(document, profile, target_index: int, program_index: int, role: str) -> tuple:
    program = document.sdf.targets[int(target_index)].programs[int(program_index)]
    return (*_shader_bytes(document, profile, program, role), profile.user_buffer_name,
            profile.material_record_prefix_size)


def prefetch_native_program_surfaces(profile, routes) -> None:
    jobs = []
    for document, *route in routes:
        try:
            jobs.append(_native_job(document, profile, *route))
        except ShaderGraphError:
            continue
    prefetch_program_surfaces(jobs)


def native_program_surface(document, profile, target_index: int, program_index: int, role: str):
    job = _native_job(document, profile, target_index, program_index, role)
    splits, reach = program_surface(*job)
    if job[0] != "dxil":
        return splits, reach
    interface = document.material_interface
    names = material_texture_names(interface.descriptor_layouts, interface.shader_lod,
                                   profile.material_record_prefix_size)
    renamed = {f"texture:@{offset}": f"texture:{name}" for offset, name in names.items()}
    return named_texture_reads(splits, names), {renamed.get(source, source): channels
                                                for source, channels in reach.items()}


def native_program_channels(document, profile, target_index: int, program_index: int, role: str):
    return native_program_surface(document, profile, target_index, program_index, role)[0]


def reach_records(entries, parameters) -> list[dict[str, object]]:
    owners = {}
    for parameter in parameters:
        for offset in range(parameter.byte_offset, parameter.byte_offset + parameter.byte_size, 4):
            owners[f"parameter@{offset}"] = ("parameter", parameter.name)
    counts: dict[tuple[str, str], dict[tuple[str, str], set]] = {}
    for pass_name, channels, reach in entries:
        pins = {split.channel.key: channel_pin_name(split.channel, "value") for split in channels}
        for source, reached in reach.items():
            owner = owners.get(source) or (("texture", source[len("texture:"):]) if source.startswith("texture:")
                                           else None)
            if owner is None:
                continue
            for key, how in reached.items():
                counts.setdefault(owner, {}).setdefault((pins[key], how), set()).add(pass_name)
    return [{"kind": kind, "source": name,
             "channels": [{"pin": pin, "how": how, "passes": len(passes)} for (pin, how), passes in sorted(rows.items())]}
            for (kind, name), rows in sorted(counts.items())]


def channel_label(channel: SurfaceChannel) -> str:
    if channel.scope == "discard":
        return "Discard"
    if channel.scope == "position":
        return "Vertex position the pass projects"
    stage = "" if channel.stage == "pixel" else f"{channel.stage.title()} "
    lanes = "".join("xyzw"[lane] for lane in channel.lanes)
    if channel.scope == "read":
        texture = (f"Material texture at byte {channel.semantic[1:]}" if channel.semantic.startswith("@")
                   else channel.semantic)
        return f"{stage}{texture} as read"
    if channel.scope == "buffer":
        words = [channel.semantic_index * 4 + lane for lane in channel.lanes]
        span = (f"word {words[0]}" if len(words) == 1 else f"words {words[0]}–{words[-1]}"
                if words[-1] - words[0] == len(words) - 1 else "words " + ", ".join(map(str, words)))
        return f"{stage}{channel.semantic} {span}"
    if channel.scope == "typed":
        return f"{stage}{channel.semantic}.{lanes}"
    name = _SEMANTIC_LABELS.get(channel.semantic, channel.semantic.upper() + "{index}")
    name = name.format(index=channel.semantic_index)
    if channel.scope == "patch":
        name = "Patch " + name
    return f"{stage}{name}.{lanes}"


def channel_pin_name(channel: SurfaceChannel, form: str) -> str:
    value_type = channel.value_type if form == "value" else channel.stored_type
    stem = re.sub(r"[^a-z0-9]+", "_", f"{channel.key}_{value_type}".casefold()).strip("_")
    return f"channel_{stem}" + ("_stored" if form == "stored" else "")


def output_pin_records(entries, catalog=None) -> list[dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    purposes: dict[str, dict[str, set]] = {}
    meanings: dict[str, dict[str, dict]] = {}
    for pass_name, purpose, shader, channels in entries:
        for split in channels:
            channel = split.channel
            meaning = (catalog.target(purpose, channel.semantic, channel.semantic_index, shader)
                       if catalog is not None and channel.scope == "output" and channel.stage == "pixel" else None)
            forms = ("value", "stored") if channel.packed else ("value",)
            for form in forms:
                pin = channel_pin_name(channel, form)
                record = records.setdefault(pin, {
                    "pin": pin,
                    "label": channel_label(channel) + (" before packing" if channel.packed and form == "value"
                                                       else " stored" if form == "stored" else ""),
                    "value_type": channel.value_type if form == "value" else channel.stored_type,
                    "channel": channel.key,
                    "form": form,
                    "packed": channel.packed,
                    "constant": channel.constant,
                    "passes": [],
                })
                if meaning is not None:
                    meanings.setdefault(pin, {}).setdefault(purpose, {})[pass_name] = {
                        "slot": meaning.slot, "read_as": list(meaning.read_as)}
                if pass_name not in record["passes"]:
                    record["passes"].append(pass_name)
                purposes.setdefault(pin, {}).setdefault(purpose, set()).add(pass_name)
    for pin, record in records.items():
        record["passes"].sort()
        record["purposes"] = {purpose: len(names) for purpose, names in sorted(purposes[pin].items())}
        if pin in meanings:
            record["meanings"] = {
                purpose: (next(iter(by_pass.values())) if len({str(value) for value in by_pass.values()}) == 1
                          else {"by_pass": dict(sorted(by_pass.items()))})
                for purpose, by_pass in sorted(meanings[pin].items())}
            gbuffer = record["meanings"].get(GBUFFER_PURPOSE)
            if gbuffer is not None and "slot" in gbuffer:
                record["label"] += f" · G-buffer {gbuffer['slot']}"
    return sorted(records.values(), key=lambda item: (str(item["channel"]), str(item["form"]), str(item["pin"])))


def channel_for_pin(channels: tuple[ChannelSplit, ...], record) -> ChannelSplit | None:
    for split in channels:
        if channel_pin_name(split.channel, str(record.get("form", "value"))) == record.get("pin"):
            return split
    return None


def validate_output_pin(record) -> None:
    if (not isinstance(record, dict) or not str(record.get("pin", "")).startswith("channel_")
            or record.get("form") not in {"value", "stored"}
            or not re.fullmatch(r"(?:float|int|uint|bool)[2-4]?", str(record.get("value_type", "")))):
        raise ShaderGraphError("Recovered master-material output is invalid.")


__all__ = [
    "SURFACE_OUTPUTS_VERSION",
    "SurfaceEdit",
    "SurfaceTap",
    "channel_for_pin",
    "channel_label",
    "channel_pin_name",
    "material_texture_names",
    "named_texture_reads",
    "native_program_channels",
    "native_program_surface",
    "output_pin_records",
    "prefetch_native_program_surfaces",
    "prefetch_program_surfaces",
    "program_surface",
    "reach_records",
    "recovered_channels",
    "validate_output_pin",
]
