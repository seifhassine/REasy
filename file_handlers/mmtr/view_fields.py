"""See docs/internals/light-passes.md, View fields."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

lazy import numpy as np

from file_handlers.build_catalog import catalog_reader
lazy from file_handlers.engine_math import crt
lazy from file_handlers.rsz.game_registry import game_type_registry
lazy from file_handlers.rsz.rsz_file import RszFile

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/view_fields"
INPUTS = ("range", "contrast", "linear_begin", "linear_length", "toe", "conversion", "exposure_scale", "knee_start",
          "knee", "output_id", "exposure_enable", "ev", "auto_exposure")
RENDERER_INPUTS = ("display_lower_limit", "display_upper_limit")
TERMS = (*INPUTS, "max_nit", "linear_start", "display_max_nit_sub_contrast_factor", "contrast_factor",
         "mul_linear_start_contrast_factor", "inv_linear_begin", "mad_linear_start_contrast_factor", "hdr_ip", "k2",
         "k4", "visible_mask", "exposure_adjustment", "use_auto_exposure", "pass_enabled", "display_range")


class ViewFieldCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TextureSize:
    texture: str
    size: str
    inverse_size: str


@dataclass(frozen=True, slots=True)
class ViewFieldCatalog:
    game: str
    resource_version: int
    scene: str
    components: Mapping[str, Mapping[str, str]]
    renderer: Mapping[str, float]
    effects: Mapping[str, int]
    buffers: Mapping[str, Mapping[str, str]]
    unset: Mapping[str, tuple[str, ...]]
    texture_sizes: Mapping[str, TextureSize]
    # The engine's: the brightness the curve maps to on an SDR screen, log2(e), ln(2), nits to the curve's unit, the
    # knee's top and the bit every view's visibility mask sets.
    max_nit: float
    log2e: float
    ln2: float
    nit_scale: float
    knee_most: float
    visible_bit: int
    # The base an EV raises (exposure = base^-EV) and the AutoExposure value turning automatic exposure on.
    exposure_base: float
    automatic_exposure: int


def catalog_from_dict(data: Mapping) -> ViewFieldCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise ViewFieldCatalogError(f"view field catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    catalog = ViewFieldCatalog(
        str(data["game"]), int(data["resource_version"]), str(data["scene"]),
        {str(kind): {str(name): str(field) for name, field in fields.items()}
         for kind, fields in data["components"].items()},
        {str(term): float(value) for term, value in data["renderer"].items()},
        {str(name): int(bit) for name, bit in data["effects"].items()},
        {str(buffer): {str(field): str(term) for field, term in fields.items()}
         for buffer, fields in data["buffers"].items()},
        {str(buffer): tuple(str(field) for field in fields) for buffer, fields in data["unset"].items()},
        {str(buffer): TextureSize(str(sized["texture"]), str(sized["size"]), str(sized["inverse_size"]))
         for buffer, sized in data["texture_sizes"].items()},
        float(data["max_nit"]), float(data["log2e"]), float(data["ln2"]), float(data["nit_scale"]),
        float(data["knee_most"]), int(data["visible_bit"]), float(data["exposure_base"]),
        int(data["automatic_exposure"]))
    inputs = [name for fields in catalog.components.values() for name in fields]
    terms = {*TERMS, *catalog.renderer}
    bits = list(catalog.effects.values())
    if sorted(inputs) != sorted((*INPUTS, *catalog.effects)) or set(catalog.renderer) & set(TERMS) or set(
            catalog.effects) & terms or not set(RENDERER_INPUTS) <= set(catalog.renderer) or any(
            bit <= 0 or bit & (bit - 1) for bit in bits) or len(set(bits)) != len(bits) or any(
            term not in terms for fields in catalog.buffers.values() for term in fields.values()) or any(
            buffer not in catalog.buffers or set(fields) & set(catalog.buffers[buffer])
            for buffer, fields in catalog.unset.items()) or not 0 <= catalog.visible_bit < 32 or any(
            {sized.size, sized.inverse_size} & {*catalog.buffers.get(buffer, ()), *catalog.unset.get(buffer, ())}
            for buffer, sized in catalog.texture_sizes.items()):
        raise ViewFieldCatalogError(f"the view field catalog's components must give {INPUTS} and its effects once "
                                    f"each, its renderer {RENDERER_INPUTS}, its effects a bit each, its buffers name "
                                    f"its terms and the renderer's values, and its unset and texture-sized fields are "
                                    f"apart from those it gives")
    return catalog


view_field_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, ViewFieldCatalogError, "view field")


def camera_settings(data: bytes, catalog: ViewFieldCatalog, game: str) -> dict[str, object] | None:
    registry = game_type_registry(game)
    if registry is None:
        return None
    rsz = RszFile()
    rsz.filepath, rsz.game_version, rsz.type_registry = catalog.scene, game, registry
    rsz.read(data)
    settings = {}
    for kind, names in catalog.components.items():
        _info, type_id = registry.find_type_by_name(kind)
        found = [index for index, instance in enumerate(rsz.instance_infos)
                 if type_id is not None and instance.type_id == type_id]
        fields = rsz.parsed_elements.get(found[0], {}) if len(found) == 1 else {}
        if any(field not in fields for field in names.values()):
            return None
        settings.update({name: getattr(fields[field], "value", fields[field]) for name, field in names.items()})
    return settings


def _terms(settings: Mapping[str, object], catalog: ViewFieldCatalog) -> dict[str, np.generic]:
    """In the renderer's order of float operations."""
    f = np.float32
    one = f(1.0)
    a, m, length, p = f(settings["contrast"]), f(settings["linear_begin"]), f(settings["linear_length"]), f(
        catalog.max_nit)
    run = ((p - m) * length) / a
    start = m + run
    rest = p - (a * run + m)
    factor = ((-(a * p)) / rest) * (one / p) * f(catalog.log2e)
    scale, knee_start, knee, most = f(settings["exposure_scale"]), f(settings["knee_start"]), f(settings["knee"]), f(
        catalog.knee_most)
    # minss: an unordered knee gives the top.
    knee = f(0.0) if f(0.0) > knee else knee if knee < most else most
    hdr_ip = (knee_start / scale) * f(catalog.nit_scale)
    k2 = (one - knee) * scale * hdr_ip * f(catalog.ln2)
    output = np.uint32(int(settings["output_id"]) & 0xFFFFFFFF)
    ev = f(settings["ev"])
    effects = 0
    for name, bit in catalog.effects.items():
        effects |= bit if settings[name] else 0
    lower, upper = f(catalog.renderer["display_lower_limit"]), f(catalog.renderer["display_upper_limit"])
    return {"range": f(settings["range"]), "contrast": a, "linear_begin": m, "linear_length": length,
            "toe": f(settings["toe"]), "conversion": f(float(bool(settings["conversion"]))), "exposure_scale": scale,
            "knee_start": knee_start, "knee": knee, "output_id": output, "max_nit": p, "linear_start": start,
            "display_max_nit_sub_contrast_factor": rest, "contrast_factor": factor,
            "mul_linear_start_contrast_factor": (-start) * factor, "inv_linear_begin": one / m,
            "mad_linear_start_contrast_factor": m - m * a, "hdr_ip": hdr_ip, "k2": k2,
            "k4": hdr_ip * scale - f(crt("logf")(float(one - knee))) * k2,
            "visible_mask": output | np.uint32(1 << catalog.visible_bit),
            "exposure_enable": np.uint32(bool(settings["exposure_enable"])), "ev": ev,
            "auto_exposure": np.uint32(int(settings["auto_exposure"]) & 0xFFFFFFFF),
            "exposure_adjustment": f(crt("powf")(float(catalog.exposure_base), float(-ev)))
            if settings["exposure_enable"] else one,
            "use_auto_exposure": np.uint32(int(settings["auto_exposure"]) == catalog.automatic_exposure),
            "pass_enabled": np.uint32(effects), "display_range": upper - lower,
            **{term: f(value) for term, value in catalog.renderer.items()}}


def view_field_words(settings: Mapping[str, object], catalog: ViewFieldCatalog) -> dict[str, dict[str, tuple[int]]]:
    # Settings dividing by zero give what the engine's do.
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        terms = _terms(settings, catalog)
    return {buffer: {field: (int(terms[term].view(np.uint32)),) for field, term in fields.items()}
            for buffer, fields in catalog.buffers.items()}


def texture_size_words(width: int, sized: TextureSize) -> dict[str, tuple[int]]:
    size = np.float32(width)
    return {sized.size: (int(size.view(np.uint32)),),
            sized.inverse_size: (int((np.float32(1.0) / size).view(np.uint32)),)}


def effects_on(settings: Mapping[str, object], catalog: ViewFieldCatalog) -> list[str]:
    return [name for name in catalog.effects if settings[name]]


__all__ = ["RENDERER_INPUTS", "TextureSize", "ViewFieldCatalog", "ViewFieldCatalogError", "camera_settings",
           "catalog_from_dict", "effects_on", "texture_size_words", "view_field_catalog_for", "view_field_words"]
