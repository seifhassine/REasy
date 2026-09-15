from __future__ import annotations

import math
import struct
from collections import Counter

from ..errors import MotionValidationError
from ..profiles import MotionFormatProfile
from .model import (
    CONTAINER_PROPERTY_TYPES,
    Bezier3DCurve,
    ClipInterpolation,
    ClipNode,
    ClipProperty,
    ClipPropertyType,
    CompactMotClip,
    HermiteCurve,
)


class CompactMotClipV27Validator:
    _SUPPORTED_KEYED_TYPES = frozenset(
        {
            ClipPropertyType.BOOL,
            ClipPropertyType.S8,
            ClipPropertyType.U8,
            ClipPropertyType.S16,
            ClipPropertyType.U16,
            ClipPropertyType.S32,
            ClipPropertyType.U32,
            ClipPropertyType.S64,
            ClipPropertyType.U64,
            ClipPropertyType.F32,
            ClipPropertyType.F64,
            ClipPropertyType.STR8,
            ClipPropertyType.STR16,
            ClipPropertyType.ENUM,
            ClipPropertyType.ASSET,
            ClipPropertyType.GUID,
            ClipPropertyType.GAME_OBJECT_REF,
            ClipPropertyType.USER_DATA_ASSET,
            ClipPropertyType.RESOURCE_PATH,
            ClipPropertyType.ACTION,
            ClipPropertyType.PATH_POINT3D,
            ClipPropertyType.UNKNOWN,
        }
    )

    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot_clip=27)
        self.profile = profile

    def validate(
        self,
        clip: CompactMotClip,
        *,
        ascii_value_interpolations: frozenset[ClipInterpolation] = frozenset(),
    ) -> None:
        self._f32(clip.total_frame, "total_frame")
        nodes = [clip.root, *clip.root.children]
        self._count(len(nodes), 0xFFFFFFFF, "node", "u32")
        if any(node.children for node in clip.root.children):
            self._fail("v27 compact MotClip supports one flat root-child level")
        if len({id(node) for node in nodes}) != len(nodes):
            self._fail("a compact MotClip node has more than one owner")
        for node in nodes:
            self._f32(node.start_frame, "node start_frame")
            self._f32(node.end_frame, "node end_frame")
            if any(
                type(value) is not bytes or len(value) != 16
                for value in (node.root_guid, node.extra_guid)
            ):
                self._fail("node GUID fields must be 16 bytes")
            self._ascii(node.name, "node name")

        props = self.flatten_properties(nodes)
        self._count(len(props), 0xFFFFFFFF, "property", "u32")
        if len({id(prop) for prop in props}) != len(props):
            self._fail("a compact MotClip property has more than one owner")
        all_keys = []
        all_speeds = []
        curves = []
        main_key_count = 0
        for prop in props:
            self._ascii(prop.name, "property name")
            self._i32(prop.array_index, "property array_index")
            self._f32(prop.start_frame, "property start_frame")
            self._f32(prop.end_frame, "property end_frame")
            self._count(len(prop.speed_points), 0xFFFFFFFF, "speed point", "u32")
            if not isinstance(prop.property_type, ClipPropertyType):
                self._fail("property_type must be a modeled ClipPropertyType")
            is_container = prop.property_type in CONTAINER_PROPERTY_TYPES
            if is_container:
                if prop.keys or prop.last_key is not None or prop.speed_points:
                    self._fail("container properties cannot own key records")
            else:
                if prop.children:
                    self._fail("non-container properties cannot own children")
                if (prop.keys or prop.last_key is not None) and prop.property_type not in self._SUPPORTED_KEYED_TYPES:
                    self._fail(f"keyed property type {prop.property_type.name} is unsupported by v27")
                if prop.speed_points and prop.property_type != ClipPropertyType.PATH_POINT3D:
                    self._fail("legacy speed points are only valid on PathPoint3D")
            main_key_count += len(prop.keys)
            for key in [*prop.keys, *([prop.last_key] if prop.last_key else [])]:
                if key is None:
                    continue
                self._f32(key.frame, "key frame")
                self._f32(key.rate, "key rate")
                if key.interpolation not in ClipInterpolation:
                    self._fail("unknown key interpolation")
                if (
                    key.interpolation in (
                        ClipInterpolation.HERMITE,
                        ClipInterpolation.BEZIER,
                    )
                    and not isinstance(key.curve, HermiteCurve)
                ):
                    self._fail("Hermite/Bezier key requires a HermiteCurve")
                if (
                    key.interpolation == ClipInterpolation.BEZIER_3D
                    and not isinstance(key.curve, Bezier3DCurve)
                ):
                    self._fail("Bezier key requires a Bezier3DCurve")
                if key.interpolation not in (
                    ClipInterpolation.HERMITE,
                    ClipInterpolation.BEZIER,
                    ClipInterpolation.BEZIER_3D,
                ) and key.curve is not None:
                    self._fail("non-curve key cannot reference a curve")
                if key.interpolation in ascii_value_interpolations:
                    self._ascii(key.value, "owner-specific ASCII key value")
                else:
                    self._validate_value(prop.property_type, key.value)
                all_keys.append(key)
                if key.curve is not None:
                    curves.append(key.curve)
            for point in prop.speed_points:
                self._f32(point.frame, "speed-point frame")
                self._f32(point.rate, "speed-point rate")
                if point.interpolation not in ClipInterpolation:
                    self._fail("unknown speed-point interpolation")
                if (
                    point.interpolation in (
                        ClipInterpolation.HERMITE,
                        ClipInterpolation.BEZIER,
                    )
                    and not isinstance(point.curve, HermiteCurve)
                ):
                    self._fail(
                        "Hermite/Bezier speed point requires a HermiteCurve"
                    )
                if (
                    point.interpolation == ClipInterpolation.BEZIER_3D
                    and not isinstance(point.curve, Bezier3DCurve)
                ):
                    self._fail("Bezier speed point requires a Bezier3DCurve")
                if point.interpolation not in (
                    ClipInterpolation.HERMITE,
                    ClipInterpolation.BEZIER,
                    ClipInterpolation.BEZIER_3D,
                ) and point.curve is not None:
                    self._fail("non-curve speed point cannot reference a curve")
                all_speeds.append(point)
                if point.curve is not None:
                    curves.append(point.curve)
        self._count(main_key_count, 0xFFFFFFFF, "key", "u32")
        if any(count != 1 for count in Counter(map(id, all_keys)).values()):
            self._fail("a compact MotClip key has more than one owner")
        if any(count != 1 for count in Counter(map(id, all_speeds)).values()):
            self._fail("a compact MotClip speed point has more than one owner")
        if any(count != 1 for count in Counter(map(id, curves)).values()):
            self._fail("v27 curve records cannot be aliased")
        for curve in curves:
            width = 4 if isinstance(curve, HermiteCurve) else 8
            if len(curve.values) != width:
                self._fail(f"curve must contain {width} binary32 values")
            for value in curve.values:
                self._f32(value, "curve value")

        node_ids = {id(node): index for index, node in enumerate(nodes)}
        self._count(len(clip.extra_ranges), 0xFFFFFFFF, "extra range", "u32")
        previous = -1
        for extra in clip.extra_ranges:
            owner_index = node_ids.get(id(extra.owner), -1)
            if owner_index <= 0:
                self._fail("extra range owner must be a non-root node")
            track_index = owner_index - 1
            if track_index > 0x7FFF:
                self._fail("extra range owner index exceeds i16")
            if track_index < previous:
                self._fail("extra ranges must be sorted by owner")
            previous = track_index
            self._count(len(extra.intervals), 0x7FFF, "extra-range interval", "i16")
            for interval in extra.intervals:
                if interval.begin_frame is not None:
                    self._f32(interval.begin_frame, "extra-range begin frame")
                if type(interval.frame_span) is not int or not 0 <= interval.frame_span <= 0xFFFFFFFF:
                    self._fail("extra-range frame span exceeds u32")

    @classmethod
    def flatten_properties(cls, nodes: list[ClipNode]) -> list[ClipProperty]:
        result: list[ClipProperty] = []
        seen: set[int] = set()
        active: set[int] = set()

        def visit_group(group: list[ClipProperty]) -> None:
            for prop in group:
                if id(prop) in active:
                    cls._fail("property graph contains a cycle")
                if id(prop) in seen:
                    cls._fail("property graph contains a shared owner")
                seen.add(id(prop))
                result.append(prop)
            for prop in group:
                active.add(id(prop))
                visit_group(prop.children)
                active.remove(id(prop))

        for node in nodes:
            visit_group(node.properties)
        return result

    @classmethod
    def _validate_value(cls, property_type: ClipPropertyType, value) -> None:
        if property_type == ClipPropertyType.BOOL and type(value) is not bool:
            cls._fail("Bool key value must be bool")
        elif property_type in {
            ClipPropertyType.S8,
            ClipPropertyType.S16,
            ClipPropertyType.S32,
            ClipPropertyType.S64,
        }:
            bits = {
                ClipPropertyType.S8: 8,
                ClipPropertyType.S16: 16,
                ClipPropertyType.S32: 32,
                ClipPropertyType.S64: 64,
            }[property_type]
            minimum, maximum = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
            if type(value) is not int or not minimum <= value <= maximum:
                cls._fail(f"{property_type.name} key value exceeds i{bits}")
        elif property_type in {
            ClipPropertyType.U8,
            ClipPropertyType.U16,
            ClipPropertyType.U32,
            ClipPropertyType.U64,
        }:
            bits = {
                ClipPropertyType.U8: 8,
                ClipPropertyType.U16: 16,
                ClipPropertyType.U32: 32,
                ClipPropertyType.U64: 64,
            }[property_type]
            if type(value) is not int or not 0 <= value < 1 << bits:
                cls._fail(f"{property_type.name} key value exceeds u{bits}")
        elif property_type in (ClipPropertyType.F32, ClipPropertyType.F64):
            if type(value) not in (int, float):
                cls._fail(f"{property_type.name} key value must be numeric")
            try:
                struct.pack("<d", value)
            except (OverflowError, struct.error):
                cls._fail(
                    f"{property_type.name} key value is not representable as binary64"
                )
        elif property_type in {
            ClipPropertyType.STR8,
            ClipPropertyType.STR16,
            ClipPropertyType.ENUM,
            ClipPropertyType.ASSET,
            ClipPropertyType.GUID,
            ClipPropertyType.GAME_OBJECT_REF,
            ClipPropertyType.USER_DATA_ASSET,
            ClipPropertyType.RESOURCE_PATH,
        }:
            if not isinstance(value, str):
                cls._fail("string key value must be str")
            if property_type in (ClipPropertyType.STR8, ClipPropertyType.ENUM):
                cls._ascii(value, f"{property_type.name} value")
        elif property_type == ClipPropertyType.ACTION and value is not None:
            cls._fail("Action key value must be None")
        elif property_type == ClipPropertyType.PATH_POINT3D:
            if not isinstance(value, tuple) or len(value) != 3:
                cls._fail("PathPoint3D key value must be a three-float tuple")
            for component in value:
                cls._f32(component, "PathPoint3D component")
        elif property_type == ClipPropertyType.UNKNOWN and value is not None:
            cls._fail("Unknown key value must be None")

    @classmethod
    def _f32(cls, value, what: str) -> None:
        if type(value) not in (int, float):
            cls._fail(f"{what} must be finite and binary32-representable")
        try:
            finite = math.isfinite(value)
            struct.pack("<f", value)
        except (OverflowError, struct.error, TypeError, ValueError):
            cls._fail(f"{what} must be finite and binary32-representable")
        if not finite:
            cls._fail(f"{what} must be finite and binary32-representable")

    @classmethod
    def _i32(cls, value, what: str) -> None:
        if type(value) is not int or not -0x80000000 <= value <= 0x7FFFFFFF:
            cls._fail(f"{what} exceeds i32")

    @classmethod
    def _count(cls, value: int, maximum: int, what: str, storage: str) -> None:
        if value > maximum:
            cls._fail(f"{what} count exceeds {storage}")

    @staticmethod
    def _ascii(value: str, what: str) -> None:
        if not isinstance(value, str):
            raise MotionValidationError(f"{what} must be str")
        if "\0" in value:
            raise MotionValidationError(f"{what} contains NUL")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise MotionValidationError(f"{what} is not ASCII") from exc

    @staticmethod
    def _fail(message: str) -> None:
        raise MotionValidationError(message)
