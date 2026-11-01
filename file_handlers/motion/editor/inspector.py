from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

lazy from utils.hash_util import murmur3_hash_utf16le

from ui.field_form import Field, FieldPanel

from .. import edits
from ..model import (
    JointChannels,
    Motion,
    MotionList,
    MotionListEntry,
    MotionTree,
    PropertyTrack,
    Sequence,
    Track,
    TreeLink,
    TreeNode,
    TreeParam,
)
from ..pose import TRANSFORMS
from ..rules import display_names, encoding, list_profile, mot_profile, tree_param_kinds, tree_rules
from .state import EditorState

_VALUE_KINDS = {"i32": "int", "u32": "int", "f32": "float", "string16": "text"}


class Inspector(FieldPanel):
    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self._sync)
        state.selection_changed.connect(self._refresh.start)
        state.document.changed.connect(self._refresh.start)
        state.document.clip_changed.connect(self._refresh.start)
        self._sync()

    def _sync(self) -> None:
        item = self.state.current
        title = {MotionList: self.tr("Motion List"), MotionListEntry: self.tr("Entry"), Motion: self.tr("Motion"),
                 MotionTree: self.tr("MotTree"), JointChannels: self.tr("Joint"), PropertyTrack: self.tr("Property"),
                 Sequence: self.tr("Sequence"), TreeNode: self.tr("Node"), TreeParam: self.tr("Parameter"),
                 TreeLink: self.tr("Link")}.get(type(item), "")
        self.show_fields(id(item), title, self._fields(item))

    def _set(self, target, text: str) -> Callable[[str], Callable[[object], None]]:
        return lambda name: lambda value: edits.set_fields(self.state.document, target, text, **{name: value})

    def _fields(self, item) -> list[Field]:
        providers = {MotionList: self._list_fields, MotionListEntry: self._entry_fields, Motion: self._motion_fields,
                     MotionTree: self._tree_fields, JointChannels: self._channel_fields,
                     PropertyTrack: self._property_fields, Sequence: self._sequence_fields,
                     TreeNode: self._node_fields, TreeParam: self._param_fields, TreeLink: self._link_fields}
        provider = providers.get(type(item))
        return [] if provider is None else provider(item)

    def _list_fields(self, motion_list: MotionList) -> list[Field]:
        setter = self._set(motion_list, self.tr("Edit Motion List"))
        stored = list_profile(motion_list.version).header.names
        fields = [Field(self.tr("Version"), "readonly", lambda: motion_list.version),
                  Field(self.tr("Name"), "text", lambda: motion_list.name, setter("name")),
                  Field(self.tr("Base list"), "text", lambda: motion_list.base_list or "",
                        lambda text: setter("base_list")(text or None),
                        tooltip=self.tr("The list this one adds to; empty for none."))]
        if "joint_mask_mode" in stored:
            fields.append(Field(self.tr("Joint mask mode"), "enum", lambda: motion_list.joint_mask_mode,
                                setter("joint_mask_mode"), display_names("JointMaskMode")))
        if "motion_packs" in stored:
            fields.append(Field(self.tr("Motion packs"), "readonly", lambda: len(motion_list.motion_packs)))
        return fields

    def _entry_fields(self, entry: MotionListEntry) -> list[Field]:
        setter = self._set(entry, self.tr("Edit Entry"))
        version = self.state.document.root.version
        stored = list_profile(version).records["row"].names
        fields = [Field(self.tr("Motion ID"), "int", lambda: entry.motion_id, setter("motion_id")),
                  Field(self.tr("Data type"), "enum", lambda: entry.type, setter("type"), display_names("DataType")),
                  Field(self.tr("Flags"), "hex", lambda: entry.flags, setter("flags"),
                        tooltip=self.tr("Bits: {0}").format(", ".join(
                            f"{bit:#x} {name}" for bit, name in display_names("ElementFlags").items()))),
                  Field(self.tr("Tag hash"), "hex", lambda: entry.tag_hash, setter("tag_hash")),
                  Field(self.tr("Physics"), "int", lambda: entry.physics, setter("physics")),
                  Field(self.tr("Joint mask"), "int", lambda: entry.joint_mask, setter("joint_mask"))]
        if "additional_sequences" in stored:
            fields += [Field(self.tr("Additional sequences"), "int", lambda: entry.additional_sequences,
                             setter("additional_sequences")),
                       Field(self.tr("Source bank"), "int", lambda: entry.source_bank, setter("source_bank")),
                       Field(self.tr("Source key"), "int", lambda: entry.source_key, setter("source_key"))]
        if isinstance(entry.item, Motion):
            fields += [Field(self.tr("Motion"), "section", lambda: None), *self._motion_fields(entry.item)]
        elif isinstance(entry.item, MotionTree):
            fields += [Field(self.tr("MotTree"), "section", lambda: None), *self._tree_fields(entry.item)]
        elif entry.item is not None:
            fields.append(Field(self.tr("Path"), "text", lambda: entry.item.path,
                                self._set(entry.item, self.tr("Edit Reference"))("path")))
        return fields

    def _motion_fields(self, motion: Motion) -> list[Field]:
        setter = self._set(motion, self.tr("Edit Motion"))
        profile = mot_profile(motion.version)
        fields = [Field(self.tr("Version"), "readonly", lambda: f"{motion.version} ({', '.join(profile.games)})"),
                  Field(self.tr("Name"), "text", lambda: motion.name, setter("name")),
                  Field(self.tr("End frame"), "float", lambda: motion.end_frame, setter("end_frame")),
                  Field(self.tr("Loop frame"), "float", lambda: motion.loop_frame, setter("loop_frame")),
                  Field(self.tr("Raw start frame"), "float", lambda: motion.raw_start_frame, setter("raw_start_frame")),
                  Field(self.tr("Raw end frame"), "float", lambda: motion.raw_end_frame, setter("raw_end_frame")),
                  Field(self.tr("Frame rate"), "int", lambda: motion.fps, setter("fps")),
                  Field(self.tr("Attributes"), "hex", lambda: motion.attributes, setter("attributes"),
                        tooltip=self.tr("Bits: {0}").format(", ".join(
                            f"{bit:#x} {name}" for bit, name in display_names("AttrFlags").items())))]
        if motion.jmap_path is not None:
            fields.append(Field(self.tr("Joint map"), "text", lambda: motion.jmap_path, setter("jmap_path")))
        extra = []
        if motion.gpu is not None:
            extra.append(self.tr("GPU motion, {0} joints").format(len(motion.gpu.joint_hashes)))
        if motion.joint_poses is not None:
            extra.append(self.tr("joint poses, {0} joints").format(len(motion.joint_poses)))
        fields.append(Field(self.tr("Holds"), "readonly", lambda: ", ".join([
            self.tr("{0} joints").format(len(motion.channels)),
            self.tr("{0} properties").format(len(motion.properties)),
            self.tr("{0} sequences").format(len(motion.sequences)), *extra])))
        return fields

    def _track_summary(self, owner: JointChannels | PropertyTrack, track: Track) -> str:
        enc = encoding(mot_profile(self.state.document.motion_of(owner).version).keys, track.kind, track.encoding)
        stored = enc.decode if not enc.bits else f"{enc.decode} {enc.bits}-bit"
        return self.tr("{0} keys, encoding 0x{1:02X} ({2})").format(track.key_count, track.encoding, stored)

    def _channel_fields(self, channel: JointChannels) -> list[Field]:
        setter = self._set(channel, self.tr("Edit Joint"))
        fields = [Field(self.tr("Joint"), "text", lambda: self.state.document.joint_name(channel),
                        lambda name: setter("name_hash")(murmur3_hash_utf16le(name)),
                        tooltip=self.tr("The joint the channel drives, by the hash of its name.")),
                  Field(self.tr("Name hash"), "hex", lambda: channel.name_hash, setter("name_hash")),
                  Field(self.tr("Joint ID"), "int", lambda: channel.joint_id, setter("joint_id")),
                  Field(self.tr("Weight"), "float", lambda: channel.weight, setter("weight"))]
        labels = {"translation": self.tr("Translation"), "rotation": self.tr("Rotation"), "scale": self.tr("Scale")}
        fields += [Field(labels[name], "readonly", lambda name=name: self._track_summary(channel, getattr(channel, name)))
                   for name in TRANSFORMS if getattr(channel, name) is not None]
        return fields

    def _property_fields(self, prop: PropertyTrack) -> list[Field]:
        return [Field(self.tr("Name hash"), "hex", lambda: prop.name_hash,
                      self._set(prop, self.tr("Edit Property"))("name_hash")),
                Field(self.tr("Keys"), "readonly", lambda: self._track_summary(prop, prop.track))]

    def _sequence_fields(self, sequence: Sequence) -> list[Field]:
        return [Field(self.tr("Category"), "enum", lambda: sequence.category,
                      self._set(sequence, self.tr("Edit Sequence"))("category"), display_names("SequenceCategory")),
                Field(self.tr("Clip"), "readonly", lambda: self.tr("{0} tracks, {1:g} frames; edited below").format(
                    len(sequence.clip.tracks), sequence.clip.total_frame))]

    def _tree_fields(self, tree: MotionTree) -> list[Field]:
        setter = self._set(tree, self.tr("Edit MotTree"))
        fields = [Field(self.tr("Version"), "readonly", lambda: tree.version),
                  Field(self.tr("Name"), "text", lambda: tree.name, setter("name")),
                  Field(self.tr("Root"), "readonly", lambda: "-" if tree.root is None else
                        tree.root.name or tree.root.class_name),
                  Field(self.tr("Contents"), "readonly", lambda: self.tr("{0} nodes, {1} links").format(
                      len(tree.nodes), len(tree.links)))]
        if "unknown_50" in tree_rules(tree.version).header.names:
            fields.append(Field(self.tr("Value at 0x50"), "int", lambda: tree.unknown_50, setter("unknown_50"),
                                tooltip=self.tr("No accessor of the engine reads it; kept as stored.")))
        return fields

    def _node_fields(self, node: TreeNode) -> list[Field]:
        setter = self._set(node, self.tr("Edit Node"))
        fields = [Field(self.tr("Class"), "readonly", lambda: node.class_name),
                  Field(self.tr("Name"), "text", lambda: node.name, setter("name")),
                  Field(self.tr("Name hash"), "hex", lambda: node.name_hash, setter("name_hash")),
                  Field(self.tr("Node type"), "enum", lambda: node.node_type, setter("node_type"),
                        display_names("TreeNodeType")),
                  Field(self.tr("Flags"), "hex", lambda: node.flags, setter("flags"))]
        found = edits.container(self.state.document, node)
        if found is not None and "id" in tree_rules(found[0].version).records["node"].names:
            fields.insert(3, Field(self.tr("ID"), "int", lambda: node.id, setter("id")))
        return fields

    def _param_fields(self, param: TreeParam) -> list[Field]:
        setter = self._set(param, self.tr("Edit Parameter"))
        kind = tree_param_kinds()[param.type]
        fields = [Field(self.tr("Type"), "readonly", lambda: display_names("TreeParamType").get(param.type))]
        if param.name is not None:
            fields.append(Field(self.tr("Name"), "text", lambda: param.name, setter("name")))
        if kind == "blob":
            return fields + [Field(self.tr("Data"), "readonly", lambda: self.tr("{0} bytes").format(len(param.value)))]
        fields.append(Field(self.tr("Name hash"), "hex", lambda: param.key, setter("key")))
        if kind == "u8":
            return fields + [Field(self.tr("Value"), "bool", lambda: bool(param.value),
                                   lambda checked: setter("value")(int(checked)))]
        return fields + [Field(self.tr("Value"), _VALUE_KINDS[kind], lambda: param.value, setter("value"))]

    def _link_fields(self, link: TreeLink) -> list[Field]:
        setter = self._set(link, self.tr("Edit Link"))
        fields = [Field(self.tr("From"), "readonly", lambda: link.parent.name or link.parent.class_name),
                  Field(self.tr("Pin"), "int", lambda: link.pin, setter("pin")),
                  Field(self.tr("To"), "readonly", lambda: link.child.name or link.child.class_name),
                  Field(self.tr("Child pin"), "int", lambda: link.child_pin, setter("child_pin")),
                  Field(self.tr("Type"), "enum", lambda: link.type, setter("type"), display_names("TreeLinkType"))]
        if link.guid is not None:
            fields.append(Field(self.tr("GUID"), "guid", lambda: link.guid, setter("guid")))
        return fields
