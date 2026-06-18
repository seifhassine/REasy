import struct

from utils.hex_util import align as align_value


UTF16_NULL = b"\x00\x00"


def pad_to_alignment(out: bytearray, alignment: int = 16):
    while len(out) % alignment != 0:
        out += b"\x00"


def pad_to_offset(out: bytearray, offset: int):
    while len(out) < offset:
        out += b"\x00"


def encode_wstring(value: str) -> bytes:
    return value.encode("utf-16-le") + UTF16_NULL


def calculate_wstring_offsets(items, string_map, start_offset: int):
    offsets = {}
    current_offset = start_offset
    for item in items:
        value = string_map.get(item, "")
        if value:
            offsets[item] = current_offset
            current_offset += len(value.encode("utf-16-le")) + len(UTF16_NULL)
        else:
            offsets[item] = 0
    return offsets, current_offset


def write_wstring_entries(out: bytearray, *offset_maps):
    entries = []
    for offsets, string_map in offset_maps:
        for item, offset in offsets.items():
            if offset:
                entries.append((offset, encode_wstring(string_map.get(item, ""))))

    entries.sort(key=lambda x: x[0])
    if not entries:
        return

    pad_to_offset(out, entries[0][0])
    for offset, string_data in entries:
        pad_to_offset(out, offset)
        out += string_data


def write_scn_gameobjects(out: bytearray, gameobjects, prefab_before_ukn: bool):
    for go in gameobjects:
        out += go.guid
        out += struct.pack("<i", go.id)
        out += struct.pack("<i", go.parent_id)
        out += struct.pack("<H", go.component_count)
        if prefab_before_ukn:
            out += struct.pack("<h", go.prefab_id)
            out += struct.pack("<i", go.ukn)
        else:
            out += struct.pack("<h", go.ukn)
            out += struct.pack("<i", go.prefab_id)


def write_resource_info_table(out: bytearray, resource_infos, resource_offsets):
    for ri in resource_infos:
        ri.string_offset = resource_offsets[ri]
        out += struct.pack("<II", ri.string_offset, ri.reserved)


def write_prefab_info_table(out: bytearray, prefab_infos, prefab_offsets):
    for pi in prefab_infos:
        pi.string_offset = prefab_offsets[pi]
        out += struct.pack("<II", pi.string_offset, pi.parent_id)


def write_userdata_info_table(out: bytearray, userdata_infos, userdata_offsets):
    for ui in userdata_infos:
        ui.string_offset = userdata_offsets[ui]
        out += struct.pack("<IIQ", ui.hash, 0, ui.string_offset)


def write_resource_userdata_tables(
    out: bytearray,
    resource_infos,
    resource_strings,
    userdata_infos,
    userdata_strings,
):
    resource_info_tbl = align_value(len(out), 16)
    resource_info_size = len(resource_infos) * 8
    userdata_info_tbl = align_value(resource_info_tbl + resource_info_size, 16)
    userdata_info_size = len(userdata_infos) * 16
    string_start = align_value(userdata_info_tbl + userdata_info_size, 16)

    resource_offsets, current_offset = calculate_wstring_offsets(
        resource_infos, resource_strings, string_start
    )
    userdata_offsets, _ = calculate_wstring_offsets(
        userdata_infos, userdata_strings, current_offset
    )

    pad_to_alignment(out)
    resource_info_tbl = len(out)
    write_resource_info_table(out, resource_infos, resource_offsets)

    pad_to_alignment(out)
    userdata_info_tbl = len(out)
    write_userdata_info_table(out, userdata_infos, userdata_offsets)

    write_wstring_entries(
        out,
        (resource_offsets, resource_strings),
        (userdata_offsets, userdata_strings),
    )
    return resource_info_tbl, userdata_info_tbl
