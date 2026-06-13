from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ClipHeader:
    magic: int = 0
    version: int = 0
    total_frame: float = 0.0
    root_node_num: int = 0
    track_num: int = 0
    clip_info_num: int = 0
    track_child_num: int = 0
    node_count_pragmata: int = 0
    node_num: int = 0
    property_num: int = 0
    key_num: int = 0
    bool_key_num: int = 0
    action_key_num: int = 0
    no_hermite_key_num: int = 0
    legacy_guid: bytes = b"\x00" * 16

    root_node_tbl_ptr: int = 0
    track_tbl_ptr: int = 0
    clip_info_tbl_ptr: int = 0
    nodes_reorder_offset2: int = 0
    track_child_tbl_ptr: int = 0
    node_tbl_ptr: int = 0
    property_tbl_ptr: int = 0
    key_tbl_ptr: int = 0
    bool_keys_offset: int = 0
    action_keys_offset: int = 0
    no_hermite_keys_offset: int = 0
    speed_point_tbl_ptr: int = 0
    interpolation_hermite_tbl_ptr: int = 0
    interpolation_hermite3d_tbl_ptr: int = 0
    last_key_tbl_ptr: int = 0
    user_data_asset_info_ptr: int = 0
    c8_ptr: int = 0
    c16_ptr: int = 0
    oword_ptr: int = 0
    data_ptr: int = 0
    legacy_clip_info_tbl_ptr: int = 0


@dataclass(slots=True)
class Track:
    enable: int = 0
    clip_num: int = 0
    child_node_num: int = 0
    type_ascii: str = ""
    type_unicode: str = ""
    group_name: str = ""
    clip_info_offset: int = 0
    child_node_start_index: int = 0
    reserved: int = 0
    clip_infos: list["ClipInfo"] = field(default_factory=list)
    child_nodes: list["Node"] = field(default_factory=list)


@dataclass(slots=True)
class ClipInfo:
    frame_in: float = 0.0
    frame_out: float = 0.0
    source_in: float = 0.0
    source_out: float = 0.0
    root_node_count: int = 0
    unicode_name: str = ""
    root_node_offset: int = 0
    root_nodes: list["Node"] = field(default_factory=list)


@dataclass(slots=True)
class Node:
    node_num: int = 0
    property_num: int = 0
    begin_frame: float = 0.0
    end_frame: float = 0.0
    root_node_guid: bytes = b"\x00" * 16
    ex_id: bytes = b"\x00" * 16
    node_type: int = 0
    unique_id: int = 0
    extra_property_pass_mask: int = 0
    route_to_secondary_property_list: int = 0
    reserved_3bits: int = 0
    dev32_id: int = 0
    padding_v53: int = 0
    name_hash: int = 0
    unique32_id: int = 0
    unicode_name_hash: int = 0
    name: str = ""
    node_tag: str = ""
    child_offset: int = 0
    property_offset: int = 0
    child_nodes: list["Node"] = field(default_factory=list)
    properties: list["Property"] = field(default_factory=list)


@dataclass(slots=True)
class Key:
    frame: float = 0.0
    rate: float = 0.0
    interpolation_type: int = 0
    offset_frame_flag: int = 0
    reserved: int = 0
    reserved2: int = 0
    frame_span: int = 0
    raw0: int = 0
    raw1: int = 0
    interpolation_offset: int = 0
    interpolation_ref: object | None = None
    legacy_tail_raw: bytes = b"\x00" * 8
    string_value: str = ""
    string_is_wide: int = -1
    string_original_value: str | None = None
    oword_ref: tuple[float, float, float, float] | None = None
    user_data_asset_index: int = -1
    user_data_asset_ref: "UserDataAssetInfo | None" = None


@dataclass(slots=True)
class BoolKey:
    frame: float = 0.0
    bool_value: int = 0
    interpolation_type_to_next: int = 0
    offset_frame_flag: int = 0
    range_v2_frame_span: int = 1
    reserved: int = 0


@dataclass(slots=True)
class ActionKey:
    frame: float = 0.0
    interpolation_type: int = 0
    reserved: int = 0


@dataclass(slots=True)
class NoHermiteKey:
    frame: float = 0.0
    interpolation_type_to_next: int = 0
    offset_frame_flag: int = 0
    range_v2_frame_span: int = 1
    reserved: int = 0
    raw0: int = 0
    raw1: int = 0
    string_value: str = ""
    string_is_wide: int = -1
    string_original_value: str | None = None
    oword_ref: tuple[float, float, float, float] | None = None
    user_data_asset_index: int = -1
    user_data_asset_ref: "UserDataAssetInfo | None" = None


@dataclass(slots=True)
class SpeedPoint:
    frame: float = 0.0
    rate: float = 0.0
    interpolation_type: int = 0
    interpolation_offset: int = 0
    interpolation_ref: object | None = None


@dataclass(slots=True)
class UserDataAssetInfo:
    type_ascii: str = ""
    path_unicode: str = ""


@dataclass(slots=True)
class Property:
    begin_frame: float = 0.0
    end_frame: float = 0.0
    name_hash: int = 0
    unique32_id: int = 0
    unicode_name_hash: int = 0
    name: str = ""
    legacy_unicode_name: str = ""
    data_offset: int = 0
    key_or_child_offset: int = 0
    key_num_or_element_num: int = 0
    array_index: int = 0
    speed_point_num: int = 0
    property_type: int = 0

    is_enum_closed: int = 0
    set_after_end_frame: int = 0
    is_exist_last_key: int = 0
    is_set_delegate_enable: int = 0
    is_prev_diff_frame_set: int = 0
    is_next_diff_frame_set: int = 0
    is_prev_key_value_set: int = 0
    is_delayed_execution_or_array_count_set: int = 0
    has_set_property_delegate: int = 0
    extra_key_flags: int = 0
    aux_key_flags: int = 0
    legacy_flags_raw: int = 0

    last_key_offset: int = 0
    speed_point_offset: int = 0
    clip_property_offset: int = 0

    keys: list[object] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    child_properties: list["Property"] = field(default_factory=list)
    last_key_ref: "Key | None" = None
    extra_key_last_ref: object | None = None
    extra_key1_ref: object | None = None
    extra_key2_ref: object | None = None
    extra_key3_ref: object | None = None
    speed_points_ref: list["SpeedPoint"] = field(default_factory=list)
    clip_property_ref: "Property | None" = None
