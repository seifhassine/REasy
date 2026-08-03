from __future__ import annotations

import math
import struct

from ..errors import MotionValidationError
from ..profiles import MotionFormatProfile
from .model import (
    MotTree,
    TreeLinkType,
    TreeNodeType,
    TreeParameterType,
)


class MotTreeV4Validator:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(mot_tree=4)
        self.profile = profile

    def validate(self, tree: MotTree) -> None:
        self._utf16(tree.name, "MotTree name")
        if len(tree.nodes) > 0xFFFF or len(tree.links) > 0xFFFF or len(tree.motion_id_remaps) > 0xFFFF:
            self._fail("MotTree count exceeds u16")
        node_ids = {id(node) for node in tree.nodes}
        if len(node_ids) != len(tree.nodes):
            self._fail("MotTree node table repeats an object")
        authored_ids = [node.authored_id for node in tree.nodes]
        if any(
            type(value) is not int
            or value in (0, 0xFFFFFFFF)
            or not 0 <= value <= 0xFFFFFFFF
            for value in authored_ids
        ):
            self._fail("MotTree authored node IDs must be nonzero and nonsentinel")
        if len(authored_ids) != len(set(authored_ids)):
            self._fail("MotTree authored node IDs must be unique")
        if tree.nodes:
            if tree.root is None or id(tree.root) not in node_ids:
                self._fail("nonempty MotTree requires an in-table root node")
        elif tree.root is not None:
            self._fail("empty MotTree cannot have a root node")
        for node in tree.nodes:
            self._ascii(node.class_name, "MotTree class name")
            if node.name is not None:
                self._utf16(node.name, "MotTree node name")
            if node.node_type not in (
                TreeNodeType.GAME_OBJECT,
                TreeNodeType.COMPONENT,
            ):
                self._fail("DMC5 MotTree node type must be GameObject or Component")
            if len(node.tags) > 0xFF or len(node.parameters) > 0xFF:
                self._fail("MotTree node tag/parameter count exceeds u8")
            for tag in node.tags:
                self._utf16(tag.name, "MotTree tag")
            for parameter in node.parameters:
                self._ascii(parameter.name, "MotTree parameter name")
                if parameter.parameter_type not in TreeParameterType:
                    self._fail("unsupported MotTree parameter type")
                if parameter.parameter_type == TreeParameterType.BOOL and type(parameter.value) is not bool:
                    self._fail("Bool MotTree parameter must be bool")
                if parameter.parameter_type == TreeParameterType.U32 and (
                    type(parameter.value) is not int or not 0 <= parameter.value <= 0xFFFFFFFF
                ):
                    self._fail("U32 MotTree parameter is invalid")
                if parameter.parameter_type == TreeParameterType.F32:
                    if type(parameter.value) not in (int, float):
                        self._fail("F32 MotTree parameter must be numeric")
                    try:
                        finite = math.isfinite(parameter.value)
                        struct.pack("<f", parameter.value)
                    except (OverflowError, struct.error, TypeError, ValueError):
                        self._fail("F32 MotTree parameter is not representable as binary32")
                    if not finite:
                        self._fail("F32 MotTree parameter must be finite")
                if parameter.parameter_type == TreeParameterType.STR16:
                    if not isinstance(parameter.value, str):
                        self._fail("Str16 MotTree parameter must be str")
                    self._utf16(parameter.value, "MotTree parameter value")
        for link in tree.links:
            if id(link.input_node) not in node_ids or id(link.output_node) not in node_ids:
                self._fail("MotTree link references a node outside the table")
            if link.link_type not in (
                TreeLinkType.MOTION,
                TreeLinkType.PARAMETER,
            ):
                self._fail("DMC5 MotTree link type must be Motion or Param")
            if any(
                type(pin) is not int or not 0 <= pin <= 0xFFFFFFFF
                for pin in (link.input_pin, link.output_pin)
            ):
                self._fail("MotTree link pins must fit u32")
            if link.output_guid is not None and (
                type(link.output_guid) is not bytes or len(link.output_guid) != 16
            ):
                self._fail("MotTree link GUID must be 16 bytes")
        for remap in tree.motion_id_remaps:
            if any(
                type(value) is not int or not -0x8000 <= value <= 0x7FFF
                for value in (remap.target, remap.source)
            ):
                self._fail("v4 motion-ID remap values must fit i16")

    @staticmethod
    def _ascii(value: str, what: str) -> None:
        if "\0" in value:
            raise MotionValidationError(f"{what} contains NUL")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise MotionValidationError(f"{what} is not ASCII") from exc

    @staticmethod
    def _utf16(value: str, what: str) -> None:
        if "\0" in value:
            raise MotionValidationError(f"{what} contains NUL")
        value.encode("utf-16le")

    @staticmethod
    def _fail(message: str) -> None:
        raise MotionValidationError(message)
