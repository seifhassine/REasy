from __future__ import annotations

import copy
import uuid
from dataclasses import fields

from .enums import PROPERTY_TYPES_WITH_CHILDREN, PropertyType, property_type_or_unknown
from .parser import ParsedClip
from .reader import ClipParserError
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


_C8_TYPES = {PropertyType.ENUM, PropertyType.STR8}
_C16_TYPES = {
    PropertyType.STR16,
    PropertyType.ASSET,
    PropertyType.RESOURCE_PATH,
    PropertyType.GAME_OBJECT_REF,
    PropertyType.GUID,
}
_KEY_VALUE_FIELDS = frozenset(
    "raw0 raw1 interpolation_offset string_value string_is_wide string_original_value "
    "oword_ref user_data_asset_index user_data_asset_ref".split()
)


class ClipGraphOperations:
    """Identity-safe mutation helpers for the parsed CLIP occurrence graph."""

    def __init__(self, parsed: ParsedClip):
        self.parsed = parsed

    @staticmethod
    def create_track() -> Track:
        return Track(enable=1, type_ascii="Timeline", type_unicode="Timeline", group_name="New Track")

    @staticmethod
    def create_clip_info(total_frame: float = 0.0) -> ClipInfo:
        return ClipInfo(frame_out=total_frame, source_out=total_frame, unicode_name="New Clip")

    @staticmethod
    def create_node(total_frame: float = 0.0) -> Node:
        return Node(
            begin_frame=0.0,
            end_frame=total_frame,
            root_node_guid=uuid.uuid4().bytes_le,
            ex_id=uuid.uuid4().bytes_le,
            name="New Node",
            node_tag="",
        )

    @classmethod
    def create_property(
        cls,
        total_frame: float = 0.0,
        property_type: int = int(PropertyType.F32),
        version: int = 0,
    ) -> Property:
        prop = Property(
            begin_frame=0.0,
            end_frame=total_frame,
            name="New Property",
            property_type=int(property_type),
        )
        if not cls.is_property_container(prop):
            prop.keys.append(cls.create_key_for_type(property_type, version))
        return prop

    def create_graph_property(
        self,
        total_frame: float = 0.0,
        property_type: int = int(PropertyType.F32),
    ) -> Property:
        prop = self.create_property(total_frame, property_type, self.parsed.header.version)
        self._register_key_references(prop)
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

    @classmethod
    def create_key_for_type(
        cls,
        property_type: int,
        version: int,
        source=None,
        old_property_type: int | None = None,
    ):
        ptype = property_type_or_unknown(int(property_type))
        old_type = property_type_or_unknown(int(old_property_type)) if old_property_type is not None else None
        frame = float(getattr(source, "frame", 0.0))

        if version >= 85 and ptype == PropertyType.BOOL:
            key = cls.create_bool_key()
            key.frame = frame
            if old_type == PropertyType.BOOL:
                key.bool_value = int(getattr(source, "bool_value", 0))
            return key
        if version >= 85 and ptype == PropertyType.ACTION:
            key = cls.create_action_key()
            key.frame = frame
            return key
        key = (
            cls.create_no_hermite_key()
            if version >= 85 and isinstance(source, NoHermiteKey)
            else cls.create_key()
        )
        if type(source) is type(key):
            for field in fields(key):
                if field.name not in _KEY_VALUE_FIELDS:
                    setattr(key, field.name, getattr(source, field.name))
        key.frame = frame

        legacy_uda_type = PropertyType.USER_DATA_ASSET if version < 62 else None
        string_types = _C8_TYPES | _C16_TYPES | ({legacy_uda_type} if legacy_uda_type is not None else set())
        reference_types = string_types | {PropertyType.PATH_POINT3D, PropertyType.USER_DATA_ASSET}
        if ptype == PropertyType.ACTION and version < 85:
            # Legacy Action values live in the normal Key payload and are the
            # constant qword 1 in both the template and every sampled file.
            key.raw0 = 1
            key.raw1 = 0
        elif old_type is not None and old_type not in reference_types and ptype not in reference_types:
            key.raw0 = int(getattr(source, "raw0", 0))
            key.raw1 = int(getattr(source, "raw1", 0))
        elif ptype in string_types:
            key.string_is_wide = int(ptype not in _C8_TYPES)
            if old_type in string_types:
                key.string_value = str(getattr(source, "string_value", ""))
                key.string_original_value = getattr(source, "string_original_value", None)
        elif ptype == PropertyType.USER_DATA_ASSET:
            old_ref = getattr(source, "user_data_asset_ref", None)
            key.user_data_asset_ref = old_ref if old_type == ptype and old_ref else UserDataAssetInfo()
        elif ptype == PropertyType.PATH_POINT3D:
            old_ref = getattr(source, "oword_ref", None)
            key.oword_ref = old_ref if old_type == ptype and old_ref else (0.0, 0.0, 0.0, 0.0)
        return key

    def retarget_property_type(self, prop: Property, property_type: int):
        if prop.property_type == int(property_type):
            return
        old_type = property_type_or_unknown(prop.property_type)
        new_type = property_type_or_unknown(int(property_type))
        old_container = old_type in PROPERTY_TYPES_WITH_CHILDREN
        new_container = new_type in PROPERTY_TYPES_WITH_CHILDREN
        removed_descendant_ids = (
            {id(item) for item in self._walk_unique(prop.child_properties, "child_properties")}
            if old_container and not new_container
            else set()
        )
        prop.property_type = int(property_type)

        if new_container:
            if not old_container:
                prop.child_properties = []
            prop.keys = []
            prop.extra_keys = []
            prop.last_key_ref = None
            prop.speed_points_ref = []
            prop.is_exist_last_key = prop.extra_key_flags = prop.aux_key_flags = 0
            prop.last_key_offset = 0
            prop.speed_point_num = 0
            prop.speed_point_offset = 0
            return

        prop.child_properties = []
        prop.children = []
        self._clear_clip_property_refs_to(removed_descendant_ids)
        if old_container:
            prop.keys = [self.create_key_for_type(new_type, self.parsed.header.version)]
        else:
            def convert(key):
                converted = self.create_key_for_type(
                    new_type, self.parsed.header.version, key, old_type
                )
                if type(key) is type(converted):
                    for field in fields(key):
                        setattr(key, field.name, getattr(converted, field.name))
                    return key
                return converted

            prop.keys = [convert(key) for key in prop.keys]
            prop.last_key_ref = convert(prop.last_key_ref) if prop.last_key_ref else None
            prop.extra_keys = [convert(key) for key in prop.extra_keys]
        if old_container or new_type != PropertyType.PATH_POINT3D:
            prop.speed_points_ref = []
            prop.speed_point_num = 0
            prop.speed_point_offset = 0
        self._register_key_references(prop)

    def add_track(self, track: Track | None = None) -> Track:
        track = track or self.create_track()
        self._append_unique(self.parsed.tracks, track)
        return track

    def remove_track(self, track: Track):
        candidate_nodes = list(track.child_nodes)
        for clip_info in list(track.clip_infos):
            self.remove_clip_info(clip_info)
        self._remove_attr(self.parsed, "tracks", track)
        for node in candidate_nodes:
            self._remove_one(self.parsed.root_nodes, node)
            if not self._node_has_owner(node):
                self.delete_node_everywhere(node)

    def add_track_clip(self, track: Track, clip_info: ClipInfo):
        if self.parsed.header.version < 40:
            raise ClipParserError("Track ClipInfo relationships require CLIP v40+")
        if self._contains_identity(track.clip_infos, clip_info):
            return
        old_track = self._track_for_clip(clip_info)
        if old_track is not None:
            self._remove_one(old_track.clip_infos, clip_info)
            for node in clip_info.root_nodes:
                self._remove_one(old_track.child_nodes, node)
        track.clip_infos.append(clip_info)
        if self.parsed.header.version >= 85:
            track.child_nodes.extend(clip_info.root_nodes)
        self._append_unique(self.parsed.clip_infos, clip_info)

    def add_clip_info(self, clip_info: ClipInfo | None = None) -> ClipInfo:
        if self.parsed.header.version < 40:
            raise ClipParserError("ClipInfo records require CLIP v40+")
        clip_info = clip_info or self.create_clip_info(self.parsed.header.total_frame)
        self._append_unique(self.parsed.clip_infos, clip_info)
        return clip_info

    def remove_clip_info(self, clip_info: ClipInfo):
        self._remove_attr(self.parsed, "clip_infos", clip_info)
        track = self._track_for_clip(clip_info)
        if track is not None:
            self._remove_one(track.clip_infos, clip_info)
            for node in clip_info.root_nodes:
                self._remove_one(track.child_nodes, node)
        for node in clip_info.root_nodes:
            if not self._node_has_owner(node):
                self.delete_node_everywhere(node)

    def add_root_node(self, node: Node | None = None) -> Node:
        if self.parsed.header.version >= 85:
            raise ClipParserError("v85+ root nodes must be attached through a ClipInfo")
        node = node or self.create_node(self.parsed.header.total_frame)
        self._detach_from_node_parents(node)
        self._append_unique(self.parsed.root_nodes, node)
        return node

    def add_track_child_node(self, track: Track, node: Node, allow_duplicate: bool = False):
        if self.parsed.header.version >= 85:
            raise ClipParserError("v85+ track roots must be attached through a ClipInfo")
        self._detach_from_node_parents(node)
        if allow_duplicate:
            track.child_nodes.append(node)
        else:
            self._append_unique(track.child_nodes, node)

    def add_clip_root_node(self, clip_info: ClipInfo, node: Node):
        if self.parsed.header.version < 85:
            raise ClipParserError("ClipInfo root-node ranges require CLIP v85+")
        self._detach_from_node_parents(node)
        if self._append_unique(clip_info.root_nodes, node):
            track = self._track_for_clip(clip_info)
            if track is not None:
                track.child_nodes.append(node)

    def add_node_child(self, parent_node: Node, child_node: Node):
        if parent_node is child_node or self._graph_contains(child_node, parent_node, "child_nodes"):
            raise ClipParserError("Attaching this node would create a cycle")
        self._detach_node_root_references(child_node)
        self._detach_from_node_parents(child_node)
        self._append_unique(parent_node.child_nodes, child_node)

    def delete_node_everywhere(self, node: Node):
        doomed = list(self._walk_unique([node], "child_nodes"))
        doomed_ids = {id(item) for item in doomed}
        all_nodes = self._all_nodes()
        self.parsed.root_nodes = [item for item in self.parsed.root_nodes if id(item) not in doomed_ids]
        for track in self.parsed.tracks:
            track.child_nodes = [item for item in track.child_nodes if id(item) not in doomed_ids]
        for clip_info in self.parsed.clip_infos:
            clip_info.root_nodes = [item for item in clip_info.root_nodes if id(item) not in doomed_ids]
        for parent in all_nodes:
            parent.child_nodes = [item for item in parent.child_nodes if id(item) not in doomed_ids]
        for prop in (prop for item in doomed for prop in item.properties):
            self._delete_property_subgraph(prop)

    def add_node_property(self, node: Node, prop: Property):
        self._detach_property(prop)
        self._append_unique(node.properties, prop)

    def add_property_child(self, parent_prop: Property, child_prop: Property):
        if not self.is_property_container(parent_prop):
            raise ClipParserError("Only container properties can own child properties")
        if parent_prop is child_prop or self._graph_contains(child_prop, parent_prop, "child_properties"):
            raise ClipParserError("Attaching this property would create a cycle")
        self._detach_property(child_prop)
        self._append_unique(parent_prop.child_properties, child_prop)

    def delete_property_everywhere(self, prop: Property):
        self._delete_property_subgraph(prop)

    def _delete_property_subgraph(self, prop: Property):
        doomed_ids = {id(item) for item in self._walk_unique([prop], "child_properties")}
        for node in self._all_nodes():
            node.properties = [item for item in node.properties if id(item) not in doomed_ids]
        for parent in self._all_properties():
            parent.child_properties = [item for item in parent.child_properties if id(item) not in doomed_ids]
        self._clear_clip_property_refs_to(doomed_ids)

    def add_property_key(self, prop: Property, key_obj: object | None = None):
        if self.is_property_container(prop):
            raise ClipParserError("Container properties cannot own key payloads")
        if key_obj is None:
            key_obj = self._create_key_for_property(prop)
        self._validate_key_for_property(prop, key_obj)
        # Key/extra/last ranges have single ownership. Passing an existing key
        # is therefore a move; callers that want a copy use duplicate_key().
        self._detach_key(key_obj, prop)
        prop.keys.append(key_obj)
        self.register_key_reference(key_obj)
        return key_obj

    def add_extra_key(
        self,
        prop: Property,
        key_obj: object | None = None,
        slot: int | None = None,
    ):
        """Add or move a key into one of the four v53+ extra-key slots."""
        if self.is_property_container(prop):
            raise ClipParserError("Container properties cannot own key payloads")
        if self.parsed.header.version < 53:
            raise ClipParserError("Properties before v53 cannot own extra keys")
        if slot is not None and not 0 <= slot < 4:
            raise ClipParserError("Extra-key slot must be in the range 0..3")
        if len(prop.extra_keys) > 4:
            raise ClipParserError("Properties support at most four extra-key flags")

        if key_obj is None:
            key_obj = self._create_key_for_property(prop)
        self._validate_key_for_property(prop, key_obj)
        location = self._key_location(key_obj, prop)
        current_slot = (
            location[2]
            if location and location[0] is prop and location[1] == "extra"
            else None
        )
        if current_slot is not None and (slot is None or slot == current_slot):
            return key_obj

        occupied = set(self.extra_key_slots(prop))
        if current_slot is not None:
            occupied.remove(current_slot)
        if slot is None:
            slot = next((candidate for candidate in range(4) if candidate not in occupied), None)
            if slot is None:
                raise ClipParserError("Properties support at most four extra keys")
        elif slot in occupied:
            raise ClipParserError(f"Extra-key slot {slot} is already occupied")
        if current_slot is None and len(prop.extra_keys) >= 4:
            raise ClipParserError("Properties support at most four extra keys")

        self._detach_key(key_obj, prop)
        slots = self.extra_key_slots(prop)
        insert_at = sum(existing < slot for existing in slots)
        prop.extra_keys.insert(insert_at, key_obj)
        prop.extra_key_flags = sum(1 << existing for existing in [*slots, slot])
        self.register_key_reference(key_obj)
        return key_obj

    def add_last_key(self, prop: Property, key_obj: object | None = None):
        """Create or move a key into the versioned LastKey slot."""
        if self.parsed.header.version >= 53:
            return self.add_extra_key(prop, key_obj, slot=0)
        if prop.last_key_ref is not None:
            raise ClipParserError("Property already owns a legacy last key")
        if key_obj is None:
            key_obj = self._create_key_for_property(prop)
        return self.set_last_key(prop, key_obj)

    def set_last_key(self, prop: Property, key_obj: object | None):
        """Replace or clear the versioned LastKey relationship."""
        if self.is_property_container(prop):
            raise ClipParserError("Container properties cannot own key payloads")
        if self.parsed.header.version >= 53:
            if len(prop.extra_keys) > 4:
                raise ClipParserError("Properties support at most four extra-key flags")
            slots = self.extra_key_slots(prop)
            current = next((key for key, slot in zip(prop.extra_keys, slots) if slot == 0), None)
            if key_obj is current:
                return key_obj
            if key_obj is not None:
                self._validate_key_for_property(prop, key_obj)
                self._key_location(key_obj, prop)
            if current is not None:
                self._remove_key_from_owner(prop, current)
            return self.add_extra_key(prop, key_obj, slot=0) if key_obj is not None else None
        if self.parsed.header.version > 43:
            raise ClipParserError("Properties after v43 cannot own legacy last-key records")
        if key_obj is prop.last_key_ref:
            return key_obj
        if key_obj is not None:
            self._validate_key_for_property(prop, key_obj)
            self._detach_key(key_obj, prop)
        prop.last_key_ref = key_obj
        prop.is_exist_last_key = int(key_obj is not None)
        prop.last_key_offset = 0
        if key_obj is not None:
            self.register_key_reference(key_obj)
        return key_obj

    def add_speed_point(self, prop: Property, point: SpeedPoint | None = None) -> SpeedPoint:
        if self.is_property_container(prop):
            raise ClipParserError("Container properties cannot own speed points")
        if (
            self.parsed.header.version < 40
            and property_type_or_unknown(prop.property_type) != PropertyType.PATH_POINT3D
        ):
            raise ClipParserError("Only PathPoint3D legacy properties can own speed points")
        if point is not None and type(point) is not SpeedPoint:
            raise ClipParserError("Speed-point ranges require SpeedPoint records")
        if point is None:
            point = self.create_speed_point()
            if prop.speed_points_ref:
                point.frame = prop.speed_points_ref[-1].frame
        for owner in self._all_properties():
            if self._contains_identity(owner.speed_points_ref, point):
                owner.speed_points_ref = [
                    item for item in owner.speed_points_ref if item is not point
                ]
                if not owner.speed_points_ref:
                    owner.speed_point_offset = 0
        prop.speed_points_ref.append(point)
        return point

    def remove_property_key(self, prop: Property, key_obj: object):
        self._remove_key_from_owner(prop, key_obj)

    def remove_speed_point(self, prop: Property, point: SpeedPoint):
        prop.speed_points_ref = [item for item in prop.speed_points_ref if item is not point]
        if not prop.speed_points_ref:
            prop.speed_point_offset = 0

    def add_user_data_asset(self, type_ascii: str = "", path_unicode: str = "") -> UserDataAssetInfo:
        if self.parsed.header.version < 62:
            raise ClipParserError("UserDataAsset tables require CLIP v62+")
        asset = UserDataAssetInfo(type_ascii=type_ascii, path_unicode=path_unicode)
        self.parsed.user_data_assets.append(asset)
        return asset

    def delete_user_data_asset(self, asset: UserDataAssetInfo):
        referenced = any(
            getattr(key, "user_data_asset_ref", None) is asset
            for prop in self._all_properties()
            for key in self.iter_property_payload_keys(prop, include_last=True)
        )
        if referenced:
            raise ClipParserError("Cannot delete a UserDataAsset while keys still reference it")
        self._remove_attr(self.parsed, "user_data_assets", asset)

    def reorder(self, items: list, item, delta: int) -> bool:
        if self.parsed.header.version >= 85 and any(
            items is track.child_nodes for track in self.parsed.tracks
        ):
            raise ClipParserError("v85+ track roots must be reordered through ClipInfo roots")
        matches = [i for i, current in enumerate(items) if current is item]
        if len(matches) != 1:
            return False
        index = matches[0]
        new_index = index + delta
        if not 0 <= new_index < len(items):
            return False
        items[index], items[new_index] = items[new_index], items[index]
        if self.parsed.header.version >= 85:
            for track in self.parsed.tracks:
                if items is track.clip_infos or any(
                    items is clip.root_nodes for clip in track.clip_infos
                ):
                    track.child_nodes[:] = [
                        node for clip in track.clip_infos for node in clip.root_nodes
                    ]
                    break
        return True

    def duplicate_node(self, node: Node, randomize_guids: bool = True) -> Node:
        new_node = copy.deepcopy(node, self._external_reference_memo([node]))
        if randomize_guids:
            self._randomize_node_guids(new_node)
        return new_node

    def duplicate_property(self, prop: Property) -> Property:
        return copy.deepcopy(prop, self._external_reference_memo([], [prop]))

    def duplicate_clip_info(self, clip_info: ClipInfo) -> ClipInfo:
        return copy.deepcopy(clip_info, self._external_reference_memo(clip_info.root_nodes))

    def duplicate_track(self, track: Track) -> Track:
        return copy.deepcopy(track, self._external_reference_memo(track.child_nodes))

    def duplicate_key(self, key):
        memo = {id(asset): asset for asset in self.parsed.user_data_assets}
        return copy.deepcopy(key, memo)

    def _create_key_for_property(self, prop: Property):
        source = next((keys[-1] for keys in (prop.keys, prop.extra_keys) if keys), None)
        source = source or prop.last_key_ref
        if (
            source is None
            and self.parsed.header.version >= 85
            and prop.aux_key_flags == 3
            and property_type_or_unknown(prop.property_type)
            not in {PropertyType.BOOL, PropertyType.ACTION}
        ):
            return self.create_no_hermite_key()
        return self.create_key_for_type(
            prop.property_type,
            self.parsed.header.version,
            source=source,
        )

    def _validate_key_for_property(self, prop: Property, key_obj: object):
        version = self.parsed.header.version
        ptype = property_type_or_unknown(prop.property_type)
        allowed = (Key,) if version < 85 else {
            PropertyType.BOOL: (BoolKey,),
            PropertyType.ACTION: (ActionKey,),
        }.get(ptype, (Key, NoHermiteKey))
        if type(key_obj) not in allowed:
            raise ClipParserError(
                f"{ptype.name} property cannot own {type(key_obj).__name__} records"
            )

        if version >= 85 and any(
            key is not key_obj and type(key) is not type(key_obj)
            for key in self.iter_property_payload_keys(prop, include_last=True)
        ):
            raise ClipParserError("A property cannot mix key-table record types")

        if not isinstance(key_obj, (Key, NoHermiteKey)):
            return
        legacy_uda = PropertyType.USER_DATA_ASSET if version < 62 else None
        asset = getattr(key_obj, "user_data_asset_ref", None)
        oword = getattr(key_obj, "oword_ref", None)
        width = getattr(key_obj, "string_is_wide", -1)
        expected_width = 0 if ptype in _C8_TYPES else 1 if ptype in _C16_TYPES or ptype == legacy_uda else -1
        compatible = (
            width == expected_width
            and (asset is not None) == (ptype == PropertyType.USER_DATA_ASSET and version >= 62)
            and (oword is not None) == (ptype == PropertyType.PATH_POINT3D)
        )
        if not compatible:
            raise ClipParserError(f"Key payload is incompatible with {ptype.name} property")

    def _key_location(self, key_obj: object, extra_prop: Property | None = None):
        locations: list[tuple[Property, str, int | None]] = []
        properties = self._all_properties()
        if extra_prop is not None and not self._contains_identity(properties, extra_prop):
            properties.append(extra_prop)
        for owner in properties:
            locations.extend((owner, "main", index) for index, key in enumerate(owner.keys) if key is key_obj)
            slots = self.extra_key_slots(owner)
            locations.extend(
                (owner, "extra", slots[index])
                for index, key in enumerate(owner.extra_keys)
                if key is key_obj
            )
            if owner.last_key_ref is key_obj:
                locations.append((owner, "last", None))
        if len(locations) > 1:
            raise ClipParserError("Key record has more than one live owner")
        return locations[0] if locations else None

    def _detach_key(self, key_obj: object, extra_prop: Property | None = None):
        location = self._key_location(key_obj, extra_prop)
        if location is not None:
            self._remove_key_from_owner(location[0], key_obj)

    @staticmethod
    def extra_key_slots(prop: Property) -> list[int]:
        flags = prop.extra_key_flags & 0xF
        slots = [slot for slot in range(4) if flags & (1 << slot)]
        return slots if len(slots) == len(prop.extra_keys) else list(range(len(prop.extra_keys)))

    @classmethod
    def _remove_key_from_owner(cls, prop: Property, key_obj: object):
        prop.keys = [key for key in prop.keys if key is not key_obj]
        slots = cls.extra_key_slots(prop)
        kept = [(key, slot) for key, slot in zip(prop.extra_keys, slots) if key is not key_obj]
        prop.extra_keys = [key for key, _ in kept]
        prop.extra_key_flags = sum(1 << slot for _, slot in kept)
        if prop.last_key_ref is key_obj:
            prop.last_key_ref = None
            prop.is_exist_last_key = 0
            prop.last_key_offset = 0

    @staticmethod
    def is_property_container(prop: Property) -> bool:
        return property_type_or_unknown(prop.property_type) in PROPERTY_TYPES_WITH_CHILDREN

    @staticmethod
    def iter_property_payload_keys(prop: Property, include_last: bool = False):
        yield from prop.keys
        if include_last and prop.last_key_ref is not None:
            yield prop.last_key_ref
        yield from prop.extra_keys

    def _register_key_references(self, prop: Property):
        for key in self.iter_property_payload_keys(prop, include_last=True):
            self.register_key_reference(key)

    def register_key_reference(self, key):
        asset = getattr(key, "user_data_asset_ref", None)
        if asset is not None:
            self._append_unique(self.parsed.user_data_assets, asset)
        oword = getattr(key, "oword_ref", None)
        if oword is not None:
            self._append_unique(self.parsed.owords, oword)

    def _node_has_owner(self, node: Node) -> bool:
        return self._contains_identity(self._node_roots(), node) or any(
            parent is not node and self._contains_identity(parent.child_nodes, node)
            for parent in self._all_nodes()
        )

    def _detach_node_root_references(self, node: Node):
        self._remove_attr(self.parsed, "root_nodes", node)
        self._remove_attr(self.parsed, "nodes_reorder_nodes", node)
        for track in self.parsed.tracks:
            self._remove_attr(track, "child_nodes", node)
        for clip in self.parsed.clip_infos:
            self._remove_attr(clip, "root_nodes", node)

    def _detach_from_node_parents(self, node: Node):
        for parent in self._all_nodes():
            self._remove_attr(parent, "child_nodes", node)

    def _detach_property(self, prop: Property):
        for node in self._all_nodes():
            self._remove_attr(node, "properties", prop)
        for parent in self._all_properties():
            self._remove_attr(parent, "child_properties", prop)

    def _clear_clip_property_refs_to(self, target_ids: set[int]):
        if not target_ids:
            return
        for owner in self._all_properties():
            if owner.clip_property_ref is not None and id(owner.clip_property_ref) in target_ids:
                owner.clip_property_ref = None
                owner.clip_property_offset = 0

    def _track_for_clip(self, clip_info: ClipInfo) -> Track | None:
        return next((track for track in self.parsed.tracks if self._contains_identity(
            track.clip_infos, clip_info
        )), None)

    def _all_nodes(self) -> list[Node]:
        return list(self._walk_unique(self._node_roots(), "child_nodes"))

    def _node_roots(self) -> list[Node]:
        if self.parsed.tracks:
            return [node for track in self.parsed.tracks for node in track.child_nodes]
        return [] if self.parsed.header.version >= 85 else self.parsed.root_nodes

    def _all_properties(self) -> list[Property]:
        roots = [prop for node in self._all_nodes() for prop in node.properties]
        return list(self._walk_unique(roots, "child_properties"))

    def _external_reference_memo(
        self,
        node_roots: list[Node],
        property_roots: list[Property] | None = None,
    ) -> dict[int, object]:
        memo: dict[int, object] = {id(asset): asset for asset in self.parsed.user_data_assets}
        owned_props = list(property_roots or [])
        for node in self._walk_unique(node_roots, "child_nodes"):
            owned_props.extend(node.properties)
        owned = list(self._walk_unique(owned_props, "child_properties"))
        owned_ids = {id(prop) for prop in owned}
        for prop in owned:
            ref = prop.clip_property_ref
            if ref is not None and id(ref) not in owned_ids:
                memo[id(ref)] = ref
        return memo

    @classmethod
    def _randomize_node_guids(cls, node: Node, seen: set[int] | None = None):
        seen = seen or set()
        if id(node) in seen:
            return
        seen.add(id(node))
        node.root_node_guid = uuid.uuid4().bytes_le
        node.ex_id = uuid.uuid4().bytes_le
        for child in node.child_nodes:
            cls._randomize_node_guids(child, seen)

    @staticmethod
    def _graph_contains(root, target, child_attr: str) -> bool:
        return any(item is target for item in ClipGraphOperations._walk_unique([root], child_attr))

    @staticmethod
    def _walk_unique(roots: list, child_attr: str):
        seen: set[int] = set()
        stack = list(reversed(roots))
        while stack:
            item = stack.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            yield item
            stack.extend(reversed(getattr(item, child_attr)))

    @staticmethod
    def _append_unique(items: list, item):
        if any(current is item for current in items):
            return False
        items.append(item)
        return True

    @staticmethod
    def _contains_identity(items: list, item) -> bool:
        return any(current is item for current in items)

    @staticmethod
    def _remove_attr(owner, attr: str, item):
        setattr(owner, attr, [current for current in getattr(owner, attr) if current is not item])

    @staticmethod
    def _remove_one(items: list, item) -> bool:
        index = next((i for i, current in enumerate(items) if current is item), -1)
        if index < 0:
            return False
        items.pop(index)
        return True
