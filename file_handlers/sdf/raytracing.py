import struct

lazy from . import dxc
lazy from .dxbc import parse_dxbc
lazy from .models import SdfRayTracingLibrary

DXR_LIBRARY_TAG = 0x00051000
DXR_LIBRARY_HEADER_SIZE = 0x140
DXR_EXPORT_FIELDS = ("raygen", "closesthit", "miss", "anyhit")
DXR_LEGACY_EXPORT_FIELDS = DXR_EXPORT_FIELDS + (
    "miss_shadow", "anyhit_shadow", "miss_vfx", "anyhit_vfx", "anyhit_vfx_shadow",
)
# RE8 predates the VFX exports and packs its DXIL straight after the shadow pair.
DXR_SHADOW_EXPORT_FIELDS = DXR_LEGACY_EXPORT_FIELDS[:6]
DXR_EXPORT_STAGES = {
    "raygen": "raygeneration",
    "closesthit": "closesthit",
    "miss": "miss",
    "anyhit": "anyhit",
    "miss_shadow": "miss",
    "anyhit_shadow": "anyhit",
    "miss_vfx": "miss",
    "anyhit_vfx": "anyhit",
    "anyhit_vfx_shadow": "anyhit",
}


# DXR payload access qualifier bit order: four bits per stage.
PAYLOAD_ACCESS_STAGES = ('caller', 'closesthit', 'miss', 'anyhit')


def raytracing_export_fields(export_layout: str) -> tuple[str, ...]:
    if export_layout == "standard":
        return DXR_EXPORT_FIELDS
    if export_layout == "legacy-nine":
        return DXR_LEGACY_EXPORT_FIELDS
    if export_layout == "legacy-six":
        return DXR_SHADOW_EXPORT_FIELDS
    raise ValueError(f"unknown DXR export layout {export_layout!r}")


def raytracing_header_size(export_layout: str) -> int:
    if export_layout == "legacy-six":
        return (len(DXR_SHADOW_EXPORT_FIELDS) + 1) * 32
    raytracing_export_fields(export_layout)
    return DXR_LIBRARY_HEADER_SIZE


def parse_raytracing_library(
    payload: bytes, *, export_layout: str = "standard",
) -> SdfRayTracingLibrary:
    fields = raytracing_export_fields(export_layout)
    header_size = raytracing_header_size(export_layout)
    if len(payload) < header_size + 32:
        raise ValueError("DXR library header/container is truncated")
    if struct.unpack_from("<I", payload)[0] != DXR_LIBRARY_TAG or any(payload[4:32]):
        raise ValueError("DXR library tag/reserved header is invalid")
    # RE2/RE3 RT has five extra shadow/VFX exports; DD2 reserves those bytes.
    tail = (len(fields) + 1) * 32
    if any(payload[tail:header_size]):
        raise ValueError("DXR library unused export storage must remain zero in this build")
    names = {}
    for index, field in enumerate(fields, 1):
        slot = payload[index * 32 : (index + 1) * 32]
        name, separator, padding = slot.partition(b"\0")
        if not separator or any(padding):
            raise ValueError(f"DXR {field} export is not a zero-padded char[32]")
        names[field] = name.decode("ascii")
    library = SdfRayTracingLibrary(
        **names, bytecode=payload[header_size:], export_layout=export_layout,
    )
    validate_raytracing_library(library)
    return library


def validate_raytracing_library(
    library: SdfRayTracingLibrary, *, reflect: bool = False,
    export_layout: str | None = None,
):
    fields = raytracing_export_fields(library.export_layout)
    if export_layout is not None:
        raytracing_export_fields(export_layout)
        if library.export_layout != export_layout:
            raise ValueError(
                f"DXR export layout {library.export_layout!r} does not match build layout {export_layout!r}"
            )
    for field in DXR_LEGACY_EXPORT_FIELDS:
        if field not in fields and getattr(library, field):
            raise ValueError(f"DXR {field} export is not supported by layout {library.export_layout!r}")
    # Shipped placeholder lanes (Rise/RE8 signeddistancefield) name no exports at all.
    placeholder = not any(getattr(library, field) for field in fields)
    if not library.raygen and not placeholder:
        raise ValueError("DXR library needs a ray-generation export")
    for field in fields:
        encoded = getattr(library, field).encode("ascii")
        if len(encoded) >= 32 or b"\0" in encoded:
            raise ValueError(f"DXR {field} export must fit a NUL-terminated char[32]")
    shader = parse_dxbc(library.bytecode, label="DXR library")
    if (shader.stage != "library" and not placeholder) or shader.total_size != len(library.bytecode):
        raise ValueError("DXR envelope must contain exactly one DXIL library")
    if reflect:
        shader = dxc.enrich_dxil_shader(shader, library.bytecode)
        exports = {function.name: function.stage for function in shader.reflection.library_functions}
        for field in fields:
            name = getattr(library, field)
            stage = DXR_EXPORT_STAGES[field]
            if name and exports.get(name) != stage:
                raise ValueError(f"DXR {field} refers to missing/wrong-stage export {name!r}")
    return shader


def serialize_raytracing_library(
    library: SdfRayTracingLibrary, *, export_layout: str | None = None,
) -> bytes:
    validate_raytracing_library(library, export_layout=export_layout)
    result = bytearray(raytracing_header_size(library.export_layout))
    struct.pack_into("<I", result, 0, DXR_LIBRARY_TAG)
    for index, field in enumerate(raytracing_export_fields(library.export_layout), 1):
        name = getattr(library, field).encode("ascii")
        result[index * 32 : index * 32 + len(name)] = name
    return result.take_bytes() + library.bytecode
