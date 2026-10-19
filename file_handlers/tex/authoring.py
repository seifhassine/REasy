from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .dds import CUBEMAP_FACES
from .dxgi import dxgi_name, get_bits_per_pixel, is_block_compressed, is_known_format
from .flags import EV_MASK, FIELD_18_23_MASK, TYPE_MASK, TexFlags, type_choices
from .profiles import (
    LAYOUT_LEGACY,
    LAYOUT_STORED,
    TexCookProfile,
    TexFormatError,
    tex_profile,
    texture_format_ids,
    texture_type_name,
)
from .tex_file import (
    MipHeader,
    StoredMipHeader,
    TexFile,
    TexHeader,
    header_count_limits,
    natural_mip_layout,
    write_tex,
)
from .texture_decoder import dds_info, dds_subresources

MAX_AXIS = 0xFFFF

_NAME_HINTS = (
    ("nrmr", "NormalRoughness"),
    ("nrm", "Normal"),
    ("normal", "Normal"),
    ("rough", "Roughness"),
    ("rgh", "Roughness"),
    ("metal", "Metallic"),
    ("mtl", "Metallic"),
    ("_met", "Metallic"),
    ("albd", "BaseAlpha"),
    ("alb", "Base"),
    ("basecolor", "Base"),
    ("_col", "Base"),
    ("msk", "Mask"),
    ("mask", "Mask"),
    ("blackwhite", "BlackWhite"),
    ("_light", "Light"),
    ("_hdr", "HDR"),
)

_BLOCK_CHANNELS = {0x46: 3, 0x47: 3, 0x50: 4, 0x51: 4, 0x53: 4, 0x54: 4}


class AuthoringError(TexFormatError):
    pass


class TypeNeedsChoice(AuthoringError):
    def __init__(self, message: str, *, options: tuple[str, ...]):
        super().__init__(message)
        self.options = options


@dataclass(frozen=True, slots=True)
class TextureSource:
    width: int
    height: int
    depth: int
    image_count: int
    mip_count: int
    dxgi_format: int
    cubemap: bool
    row_pitches: tuple[int, ...]
    # At the natural row pitch, each holding every depth slice of its mip contiguously.
    subresources: tuple[bytes, ...]
    # The same cook ships containers with and without the per-mip chunk table, so it is preserved.
    stored_chunks: bool = False

    def dimensions(self, level: int) -> tuple[int, int]:
        return max(1, self.width >> level), max(1, self.height >> level)

    def slices(self, level: int) -> int:
        return max(1, self.depth >> level)

    def payload(self, level: int, image: int = 0) -> bytes:
        return self.subresources[image * self.mip_count + level]



@dataclass(frozen=True, slots=True)
class Authored:
    data: bytes
    version: int
    notes: tuple[str, ...] = ()


def source_from_dds(data: bytes) -> TextureSource:
    try:
        info = dds_info(data)
    except ValueError as exc:
        raise AuthoringError(f"unreadable DDS: {exc}") from None
    if not is_known_format(info.dxgi_format):
        raise AuthoringError(
            f"the DDS stores format {info.dxgi_format}, whose size this project has not reversed"
        )

    payloads: list[bytes] = []
    pitches: list[int] = []
    end = info.data_offset
    for index, (offset, span) in enumerate(dds_subresources(info)):
        level = index % info.mip_count
        width, height = max(1, info.width >> level), max(1, info.height >> level)
        payloads.append(data[offset : offset + span])
        pitches.append(natural_mip_layout(info.dxgi_format, width, height)[1])
        end = offset + span
    if end != len(data):
        raise AuthoringError(
            f"the DDS payload is {len(data) - info.data_offset} bytes, but its "
            f"{info.mip_count}-mip {info.image_count}-image shape needs "
            f"{end - info.data_offset}: a row pitch padded past the natural one is "
            "not supported"
        )
    return TextureSource(
        width=info.width,
        height=info.height,
        depth=1 if info.cubemap else info.depth,
        image_count=info.image_count,
        mip_count=info.mip_count,
        dxgi_format=info.dxgi_format,
        cubemap=info.cubemap,
        row_pitches=tuple(pitches),
        subresources=tuple(payloads),
        stored_chunks=True,
    )


def source_from_tex(tex: TexFile) -> TextureSource:
    header = tex.header
    return TextureSource(
        width=header.width,
        height=header.height,
        depth=header.depth,
        image_count=header.image_count,
        mip_count=header.mip_count,
        dxgi_format=header.format,
        cubemap=header.is_cubemap,
        stored_chunks=tex.uses_stored_mips,
        row_pitches=tuple(
            tex.mips[image * header.mip_count + level].pitch
            for image in range(header.image_count)
            for level in range(header.mip_count)
        ),
        subresources=tuple(
            tex.read_mip_level(level, image)
            for image in range(header.image_count)
            for level in range(header.mip_count)
        ),
    )


def guess_type_name(source: TextureSource, name: str = "") -> Optional[str]:
    if source.cubemap:
        return "HDR"
    lowered = name.lower()
    for fragment, type_name in _NAME_HINTS:
        if fragment in lowered:
            return f"Mask{_channel_count(source.dxgi_format)}ch" if type_name == "Mask" else type_name
    return "Image" if is_block_compressed(source.dxgi_format) else None


def _padded_rows(
    payload: bytes, natural_pitch: int, pitch: int, rows: int, slices: int
) -> bytes:
    if pitch == natural_pitch:
        return payload
    natural_size = natural_pitch * rows
    out = bytearray()
    for index in range(slices):
        chunk = payload[index * natural_size : (index + 1) * natural_size]
        for row in range(rows):
            out += chunk[row * natural_pitch : (row + 1) * natural_pitch]
            out += bytes(pitch - natural_pitch)
    return bytes(out)


def _channel_count(dxgi_format: int) -> int:
    if is_block_compressed(dxgi_format):
        return _BLOCK_CHANNELS.get(dxgi_format, 4)
    return max(1, min(4, get_bits_per_pixel(dxgi_format) // 8))


def _resolve_type(
    profile: TexCookProfile,
    *,
    name: Optional[str] = None,
    index: Optional[int] = None,
    explicit: bool,
    notes: list[str],
) -> int:
    names = type_choices(profile)
    if index is not None:
        if not profile.type_field_used:
            if index:
                raise AuthoringError(
                    f"{profile.display_name} never writes the texture type field, "
                    f"so it cannot store type index {index}"
                )
            return 0
        if names and index >= len(names):
            raise AuthoringError(
                f"{profile.display_name} stores type indices 0..{len(names) - 1}"
            )
        if not 0 <= index <= TYPE_MASK:
            raise AuthoringError(f"type index {index} does not fit the field's {TYPE_MASK}")
        return index

    if name is None:
        return 0
    if not profile.type_field_used:
        if explicit:
            raise AuthoringError(
                f"{profile.display_name} never writes the texture type field, "
                f"so a texture cannot carry {name!r} there"
            )
        notes.append(f"{profile.display_name} never writes the texture type; {name} is not stored")
        return 0
    if not names:
        message = (
            f"{profile.display_name} declares no via.render.TextureType names, so "
            f"{name!r} cannot be resolved for it; pass a type index instead"
        )
        raise AuthoringError(message) if explicit else TypeNeedsChoice(message, options=())
    if name not in names:
        message = (
            f"{profile.display_name} has no {name!r} texture type; it declares: "
            f"{', '.join(names)}"
        )
        raise AuthoringError(message) if explicit else TypeNeedsChoice(message, options=names)
    return names.index(name)


def _check(profile: TexCookProfile, source: TextureSource) -> None:
    problems = []
    for image in range(source.image_count):
        for level in range(source.mip_count):
            natural_pitch = natural_mip_layout(
                source.dxgi_format, *source.dimensions(level)
            )[1]
            pitch = source.row_pitches[image * source.mip_count + level]
            if pitch < natural_pitch:
                problems.append(
                    f"image {image} mip {level} declares a pitch of {pitch}, below the "
                    f"natural {natural_pitch}"
                )
    if source.width > MAX_AXIS or source.height > MAX_AXIS or source.depth > MAX_AXIS:
        problems.append(f"{source.width}x{source.height}x{source.depth} exceeds the 16-bit axes")
    max_images, max_mips = header_count_limits(profile.layout)
    if source.image_count > max_images:
        problems.append(f"{source.image_count} images exceeds the {max_images} this header stores")
    full_chain = max(source.width, source.height).bit_length()
    if source.mip_count > full_chain:
        problems.append(f"{source.mip_count} mip levels exceeds the {full_chain} of a full chain")
    if source.mip_count > max_mips:
        problems.append(f"{source.mip_count} mip levels exceeds the {max_mips} this header stores")
    if source.cubemap and (source.image_count != CUBEMAP_FACES or source.depth != 1):
        problems.append("a cubemap is six images of depth 1")
    if problems:
        raise AuthoringError(
            f"{profile.display_name} cannot store this shape: " + "; ".join(problems)
        )

    declared = texture_format_ids(profile)
    if declared and source.dxgi_format not in declared:
        raise AuthoringError(
            f"{profile.display_name} does not declare {dxgi_name(source.dxgi_format)}; it "
            f"declares {len(declared)} DXGI formats"
        )


def _raw_payload_note(profile: TexCookProfile, stored_chunks: bool) -> list[str]:
    if profile.layout != LAYOUT_STORED:
        return []
    table = "a chunk table recording them raw" if stored_chunks else "no chunk table, as the source has none"
    return [f"mip payloads are written uncompressed (no GDeflate writer here): {table}"]


def _dropped_members(profile: TexCookProfile, flags: TexFlags) -> list[str]:
    notes = []
    if flags.streaming_mips and not profile.streaming_mips:
        notes.append(
            f"{profile.display_name} does not record omitted mip levels; "
            f"{flags.streaming_mips} are not stored"
        )
    if flags.refiltered_normal and not profile.refiltered_normal:
        notes.append(f"{profile.display_name} does not record a re-filtered normal tail")
    if (flags.field_18_23 or flags.opaque_flag) and not profile.unnamed_fields:
        notes.append(f"{profile.display_name} does not store this cook's two unnamed flag members")
    return notes


def _flags_word(
    profile: TexCookProfile,
    *,
    index: int,
    resident: bool,
    ev: int,
    streaming_mips: int,
    refiltered_normal: bool,
    field_18_23: int,
    opaque_flag: bool,
) -> int:
    return TexFlags.decode(0, profile.version).encode(
        profile,
        resident=1 if resident else 0,
        ev=ev & EV_MASK,
        texture_type=index,
        streaming_mips=streaming_mips if profile.streaming_mips else 0,
        refiltered_normal=1 if refiltered_normal and profile.refiltered_normal else 0,
        field_18_23=field_18_23 & FIELD_18_23_MASK if profile.unnamed_fields else 0,
        opaque_flag=1 if opaque_flag and profile.unnamed_fields else 0,
    )


def _container(profile: TexCookProfile, source: TextureSource, flags_word: int) -> bytes:
    table_at = 32 if profile.layout == LAYOUT_LEGACY else 40
    total = source.image_count * source.mip_count

    mips: list[MipHeader] = []
    spans: list[int] = []
    payloads: list[bytes] = []
    offset = table_at + 16 * total
    for image in range(source.image_count):
        for level in range(source.mip_count):
            width, height = source.dimensions(level)
            slices = source.slices(level)
            natural_size, natural_pitch, row_step = natural_mip_layout(
                source.dxgi_format, width, height
            )
            pitch = source.row_pitches[image * source.mip_count + level]
            rows = (height + row_step - 1) // row_step
            size = pitch * rows
            payloads.append(
                _padded_rows(source.payload(level, image), natural_pitch, pitch, rows, slices)
            )
            mips.append(MipHeader(offset, pitch, size))
            spans.append(size * slices)
            offset += size * slices

    if any(len(chunk) != span for chunk, span in zip(payloads, spans)):
        raise AuthoringError(
            f"the source holds {sum(map(len, payloads))} payload bytes but its shape needs "
            f"{sum(spans)}"
        )
    stored: list[StoredMipHeader] = []
    if profile.layout == LAYOUT_STORED and source.stored_chunks:
        running = 0
        for span in spans:
            stored.append(StoredMipHeader(span, running))
            running += span
    header = TexHeader(
        version=profile.version,
        width=source.width,
        height=source.height,
        depth=source.depth,
        image_count=source.image_count,
        mip_count=source.mip_count,
        format=source.dxgi_format,
        swizzle_control=-1,
        cubemap_marker=4 if source.cubemap else 0,
        flags=flags_word,
    )
    return write_tex(profile.layout, header, mips, stored, b"".join(payloads))


def _author(
    profile: TexCookProfile,
    source: TextureSource,
    *,
    index: int,
    notes: Sequence[str],
    **members,
) -> Authored:
    _check(profile, source)
    data = _container(profile, source, _flags_word(profile, index=index, **members))
    tex = TexFile()
    if not tex.read(data):
        raise AuthoringError("the authored container is not a .tex at all")
    for image in range(source.image_count):
        for level in range(source.mip_count):
            if tex.read_mip_level(level, image) != source.payload(level, image):
                raise AuthoringError(
                    f"the authored container does not read back: image {image}, mip {level}"
                )
    return Authored(
        data=data,
        version=profile.version,
        notes=tuple(notes),
    )


def import_dds(
    data: bytes,
    *,
    version: int,
    name: str = "",
    type_name: Optional[str] = None,
    type_index: Optional[int] = None,
    resident: bool = False,
    ev: int = 0,
) -> Authored:
    profile = tex_profile(version)
    source = source_from_dds(data)
    notes: list[str] = []
    if type_name is not None or type_index is not None:
        index = _resolve_type(profile, name=type_name, index=type_index, explicit=True, notes=notes)
    else:
        names = type_choices(profile)
        guessed = guess_type_name(source, name)
        if guessed is not None and guessed in names:
            index = names.index(guessed)
        elif not names:
            index = 0
        else:
            index = names.index("Default") if "Default" in names else 0
            if guessed is not None:
                notes.append(
                    f"{profile.display_name} has no {guessed!r} type; imported as {names[index]}"
                )
    return _author(
        profile,
        source,
        index=index,
        notes=_raw_payload_note(profile, source.stored_chunks) + notes,
        resident=resident,
        ev=ev,
        streaming_mips=0,
        refiltered_normal=False,
        field_18_23=0,
        opaque_flag=False,
    )


def convert(
    tex: TexFile,
    *,
    version: int,
    type_name: Optional[str] = None,
    type_index: Optional[int] = None,
) -> Authored:
    profile = tex_profile(version)
    source_profile = tex_profile(tex.header.version)
    source = source_from_tex(tex)
    flags = tex.header.tex_flags
    notes: list[str] = []

    if type_index is not None or type_name is not None:
        index = _resolve_type(profile, name=type_name, index=type_index, explicit=True, notes=notes)
    elif version == tex.header.version:
        index = flags.texture_type
    else:
        name = texture_type_name(source_profile, flags.texture_type)
        if name is None:
            raise TypeNeedsChoice(
                f"{source_profile.display_name} names no texture type for index "
                f"{flags.texture_type}, so the target's type has to be chosen",
                options=type_choices(profile),
            )
        index = _resolve_type(profile, name=name, explicit=False, notes=notes)

    notes.extend(_dropped_members(profile, flags))
    return _author(
        profile,
        source,
        index=index,
        notes=_raw_payload_note(profile, source.stored_chunks) + notes,
        resident=flags.resident,
        ev=flags.ev,
        streaming_mips=flags.streaming_mips,
        refiltered_normal=flags.refiltered_normal,
        field_18_23=flags.field_18_23,
        opaque_flag=flags.opaque_flag,
    )
