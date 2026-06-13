from __future__ import annotations

import copy
import uuid

from .enums import PROPERTY_TYPES_WITH_CHILDREN, PropertyType, property_type_or_unknown
from .parser import EXTRA_KEY_REF_ATTRS, ParsedClip
from .structures import (
    ActionKey,
    BoolKey,
    ClipInfo,
    Key,
    Node,
    NoHermiteKey,
    Property,
    SpeedPoint,
    Track,
    UserDataAssetInfo,
)


class ClipGraphOperations:
    """Mutation helpers for ParsedClip graph editing.

    The writer rebuilds indexes and binary offsets from object relationships, so
    UI operations should update object lists/references and leave table fields
    for ClipWriter to recalculate.
    """

    def __init__(self, parsed: ParsedClip):
        self.parsed = parsed

    @staticmethod
    def create_track() -> Track:
        return Track(enable=1, type_ascii="Timeline", type_unicode="Timeline", group_name="New Track")

    @staticmethod
    def create_clip_info(total_frame: float = 0.0) -> ClipInfo:
        return ClipInfo(frame_in=0.0, frame_out=total_frame, source_in=0.0, source_out=total_frame, unicode_name="New Clip")

    @staticmethod
    def create_node(total_frame: float = 0.0) -> Node:
        guid = uuid.uuid4().bytes_le
        return Node(
            begin_frame=0.0,
            end_frame=total_frame,
            root_node_guid=guid,
            ex_id=uuid.uuid4().bytes_le,
            name="New Node",
            node_tag="",
        )

    @staticmethod
    def create_property(total_frame: float = 0.0, property_type: int = int(PropertyType.F32)) -> Property:
        prop = Property(begin_frame=0.0, end_frame=total_frame, name="New Property", property_type=property_type)
        if PropertyType(property_type) not in PROPERTY_TYPES_WITH_CHILDREN:
            prop.keys.append(ClipGraphOperations.create_key())
        return prop

    @staticmethod
    def create_key() -> Key:
        return Key(frame=0.0, rate=1.0, interpolation_type=0x1)

    @staticmethod
    def create_bool_key() -> BoolKey:
        return BoolKey(frame=0.0, bool_value=0, interpolation_type_to_next=0x1)

    @staticmethod
    def create_action_key() -> ActionKey:
        return ActionKey(frame=0.0, interpolation_type=0x3)

    @staticmethod
    def create_no_hermite_key() -> NoHermiteKey:
        return NoHermiteKey(frame=0.0, interpolation_type_to_next=0x1)

    @staticmethod
    def create_speed_point() -> SpeedPoint:
        return SpeedPoint(frame=0.0, rate=1.0, interpolation_type=0x1)

    def add_track(self, track: Track | None = None) -> Track:
        track = track or self.create_track()
        self.parsed.tracks.append(track)
        return track

    def remove_track(self, track: Track):
        private_clips = [
            clip_info for clip_info in track.clip_infos
            if not any(other is not track and clip_info in other.clip_infos for other in self.parsed.tracks)
        ]
        self._remove_attr(self.parsed, "tracks", track)
        for clip_info in private_clips:
            self._remove_attr(self.parsed, "clip_infos", clip_info)

    def add_track_clip(self, track: Track, clip_info: ClipInfo):
        self._append_unique_and_undelete(track.clip_infos, clip_info)

    def add_clip_info(self, clip_info: ClipInfo | None = None) -> ClipInfo:
        clip_info = clip_info or self.create_clip_info(self.parsed.header.total_frame)
        self._append_unique_and_undelete(self.parsed.clip_infos, clip_info)
        return clip_info

    def remove_clip_info(self, clip_info: ClipInfo):
        self._remove_attr(self.parsed, "clip_infos", clip_info)
        for track in self.parsed.tracks:
            self._remove_attr(track, "clip_infos", clip_info)

    def add_root_node(self, node: Node | None = None) -> Node:
        node = node or self.create_node(self.parsed.header.total_frame)
        self._append_unique_and_undelete(self.parsed.root_nodes, node, "_deleted_node_ids")
        return node

    def add_track_child_node(self, track: Track, node: Node):
        self._append_unique_and_undelete(track.child_nodes, node, "_deleted_node_ids")

    def add_clip_root_node(self, clip_info: ClipInfo, node: Node):
        self._append_unique_and_undelete(clip_info.root_nodes, node, "_deleted_node_ids")

    def add_node_child(self, parent_node: Node, child_node: Node):
        self._append_unique_and_undelete(parent_node.child_nodes, child_node, "_deleted_node_ids")

    def delete_node_everywhere(self, node: Node):
        self._mark_deleted("_deleted_node_ids", node)
        self._remove_attr(self.parsed, "root_nodes", node)
        self._remove_attr(self.parsed, "nodes_reorder_nodes", node)
        for track in self.parsed.tracks:
            self._remove_attr(track, "child_nodes", node)
        for clip_info in self.parsed.clip_infos:
            self._remove_attr(clip_info, "root_nodes", node)
        for parent in self.parsed.nodes:
            self._remove_attr(parent, "child_nodes", node)

    def add_node_property(self, node: Node, prop: Property):
        self._append_unique_and_undelete(node.properties, prop, "_deleted_property_ids")

    def add_property_child(self, parent_prop: Property, child_prop: Property):
        self._append_unique_and_undelete(parent_prop.child_properties, child_prop, "_deleted_property_ids")

    def delete_property_everywhere(self, prop: Property):
        self._mark_deleted("_deleted_property_ids", prop)
        for node in self.parsed.nodes:
            self._remove_attr(node, "properties", prop)
        for parent in self.parsed.properties:
            self._remove_attr(parent, "child_properties", prop)
            if parent.clip_property_ref is prop:
                parent.clip_property_ref = None

    @staticmethod
    def add_property_key(prop: Property, key_obj: object):
        prop.keys.append(key_obj)

    @staticmethod
    def remove_property_key(prop: Property, key_obj: object):
        prop.keys = [k for k in prop.keys if k is not key_obj]
        for _, attr in EXTRA_KEY_REF_ATTRS:
            if getattr(prop, attr) is key_obj:
                setattr(prop, attr, None)

    def add_user_data_asset(self, type_ascii: str = "", path_unicode: str = "") -> UserDataAssetInfo:
        asset = UserDataAssetInfo(type_ascii=type_ascii, path_unicode=path_unicode)
        self.parsed.user_data_assets.append(asset)
        return asset

    def delete_user_data_asset(self, asset: UserDataAssetInfo):
        self._remove_attr(self.parsed, "user_data_assets", asset)
        for prop in self.parsed.properties:
            for key_obj in self.iter_property_payload_keys(prop):
                if getattr(key_obj, "user_data_asset_ref", None) is asset:
                    key_obj.user_data_asset_ref = None
                    key_obj.user_data_asset_index = -1

    @staticmethod
    def reorder(items: list, item, delta: int) -> bool:
        try:
            index = next(i for i, current in enumerate(items) if current is item)
        except StopIteration:
            return False
        new_index = index + delta
        if not 0 <= new_index < len(items):
            return False
        items[index], items[new_index] = items[new_index], items[index]
        return True

    @classmethod
    def duplicate_node(cls, node: Node, randomize_guids: bool = True) -> Node:
        new_node = copy.deepcopy(node)
        if randomize_guids:
            cls._randomize_node_guids(new_node)
        return new_node

    duplicate_property = staticmethod(copy.deepcopy)
    duplicate_clip_info = staticmethod(copy.deepcopy)
    duplicate_track = staticmethod(copy.deepcopy)
    duplicate_key = staticmethod(copy.deepcopy)

    @staticmethod
    def is_property_container(prop: Property) -> bool:
        ptype = property_type_or_unknown(prop.property_type)
        return ptype in PROPERTY_TYPES_WITH_CHILDREN

    @staticmethod
    def iter_property_payload_keys(prop: Property):
        yield from prop.keys
        for _, attr in EXTRA_KEY_REF_ATTRS:
            extra = getattr(prop, attr)
            if isinstance(extra, (Key, NoHermiteKey)):
                yield extra

    @classmethod
    def _randomize_node_guids(cls, node: Node):
        node.root_node_guid = uuid.uuid4().bytes_le
        node.ex_id = uuid.uuid4().bytes_le
        for child in node.child_nodes:
            cls._randomize_node_guids(child)

    def _append_unique_and_undelete(self, items, item, deleted_attr=None):
        if not any(current is item for current in items):
            items.append(item)
        if deleted_attr and hasattr(self.parsed, deleted_attr):
            getattr(self.parsed, deleted_attr).discard(id(item))

    def _mark_deleted(self, attr: str, item):
        if not hasattr(self.parsed, attr):
            setattr(self.parsed, attr, set())
        getattr(self.parsed, attr).add(id(item))

    def _remove_attr(self, owner, attr: str, item):
        setattr(owner, attr, self._remove_identity(getattr(owner, attr), item))

    @staticmethod
    def _remove_identity(items, item):
        return [it for it in items if it is not item]
