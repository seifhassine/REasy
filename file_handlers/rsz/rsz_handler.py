"""
RSZ file handler and viewer implementation.

This file contains:
- RszHandler: Main handler for RSZ file loading and management
- RszViewer: Qt widget for displaying and editing RSZ file contents
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QMessageBox
import re

from utils.hex_util import guid_le_to_str
from ..base_handler import BaseFileHandler
from file_handlers.pyside.value_widgets import *
from file_handlers.rsz.rsz_data_types import *
from .rsz_file import ScnFile, ScnInstanceInfo
from utils.type_registry import TypeRegistry
from ui.styles import get_color_scheme, get_tree_stylesheet
from ..pyside.tree_model import ScnTreeBuilder, DataTreeBuilder
from ..pyside.tree_widgets import AdvancedTreeView
from utils.id_manager import IdManager
from .rsz_array_operations import RszArrayOperations


class RszHandler(BaseFileHandler):
    """Handler for SCN/PFB/USR files"""
    @staticmethod
    def needs_json_path() -> bool:
        return True

    def __init__(self):
        super().__init__()
        self.scn_file = None
        self.show_advanced = True
        self._viewer = None

    def can_handle(data: bytes) -> bool:
        """Check if data appears to be an SCN, USR, or PFB file"""
        if len(data) < 4:
            return False
        scn_sig = b"SCN\x00"
        usr_sig = b"USR\x00"
        pfb_sig = b"PFB\x00"
        return data[:4] in [scn_sig, usr_sig, pfb_sig]

    def read(self, data: bytes):
        """Parse the file data"""
        self.init_type_registry()
        self.scn_file = ScnFile()
        self.scn_file.type_registry = self.type_registry
        self.scn_file.read(data)
        IdManager.instance().reset()

    def create_viewer(self):
        """Create a new viewer instance"""
        viewer = RszViewer()
        viewer.scn = self.scn_file
        viewer.handler = self
        viewer.type_registry = self.type_registry
        viewer.dark_mode = self.dark_mode
        viewer.show_advanced = self.show_advanced
        colors = get_color_scheme(self.dark_mode)
        viewer.tree.setStyleSheet(get_tree_stylesheet(colors))
        viewer.populate_tree()
        viewer.destroyed.connect(viewer.cleanup)
        viewer.modified_changed.connect(self.modified_changed.emit)
        self._viewer = viewer
        return viewer

    def rebuild(self) -> bytes:
        """Rebuild SCN file data with better error handling"""
        if not self.scn_file:
            raise ValueError("No SCN file loaded")
        return self.scn_file.build()


class RszViewer(QWidget):
    INSTANCE_ID_ROLE = Qt.UserRole + 1
    ROW_HEIGHT = 24
    modified_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modified = False
        self.scn = ScnFile()
        self.handler = None
        self.type_registry = None
        self.dark_mode = False
        self.show_advanced = False
        self._cleanup_pending = False
        self._created_widgets = []
        self._created_labels = []
        self.tree = AdvancedTreeView(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tree)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree.installEventFilter(self)
        self.array_operations = None

    def mark_modified(self):
        """Mark the viewer as modified and emit signal"""
        if not self._modified:
            self._modified = True
            self.modified_changed.emit(True)
            if self.handler:
                self.handler.modified = True

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    def cleanup(self):
        """Safer cleanup that prevents access to deleted widgets"""
        if self._cleanup_pending:
            return
        self._cleanup_pending = True
        try:
            try:
                self.modified_changed.disconnect()
            except:
                pass
            if self.tree:
                self.tree.setModel(None)
            self._created_widgets.clear()
            self._created_labels.clear()
        except Exception:
            pass
        self._cleanup_pending = False

    def closeEvent(self, event):
        """Handle cleanup before closing"""
        self.cleanup()
        super().closeEvent(event)

    def load_scn(self, data: bytes, type_registry: TypeRegistry):
        """Load SCN data and populate tree"""
        self.type_registry = type_registry
        self.scn.type_registry = type_registry
        self.scn.debug = False
        self.scn.read(data)
        self.array_operations = RszArrayOperations(self)
        self.populate_tree()

    def supports_editing(self) -> bool:
        return True

    def handle_edit(self, meta: dict, new_val, old_val, row_id):
        return self.rebuild()

    def update_strings(self):
        pass

    def populate_tree(self):
        self.tree.setUpdatesEnabled(False)
        root_data = self._build_tree_data()
        self.tree.setModelData(root_data)
        self.tree.setUpdatesEnabled(True)
        self.embed_forms()

    def _build_tree_data(self):
        root_dict = DataTreeBuilder.create_data_node("SCN_File", "")
        file_type = "USR" if self.scn.is_usr else "PFB" if self.scn.is_pfb else "SCN"
        root_dict["data"][0] = f"{file_type}_File"
        if self.show_advanced:
            advanced_node = ScnTreeBuilder.create_advanced_node()
            root_dict["children"].append(advanced_node)
            advanced_node["children"].extend(
                [
                    self._create_header_info(),
                    self._create_gameobjects_info(),
                ]
            )
            if not self.scn.is_pfb and not self.scn.is_usr:
                advanced_node["children"].append(self._create_folders_info())
            if self.scn.is_pfb:
                advanced_node["children"].append(self._create_gameobject_ref_infos())
            advanced_node["children"].extend(
                [
                    self._create_rsz_header_info(),
                    self._create_object_table_info(),
                    self._create_instance_infos(),
                    self._create_userdata_infos(),
                ]
            )
        data_node = DataTreeBuilder.create_data_node(
            ScnTreeBuilder.NODES["DATA_BLOCK"],
        )
        root_dict["children"].append(data_node)
        self._add_data_block(data_node)
        return root_dict

    def _create_header_info(self):
        """Create Header info section for self.scn.header"""
        children = []
        if self.scn.is_pfb:
            header_fields = [
                ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip("\x00")),
                ("Info Count", lambda h: str(h.info_count)),
                ("Resource Count", lambda h: str(h.resource_count)),
                ("GameObjectRefInfo Count", lambda h: str(h.gameobject_ref_info_count)),
                ("UserData Count", lambda h: str(h.userdata_count)),
                ("Reserved", lambda h: str(h.reserved)),
                ("GameObjectRefInfo Tbl", lambda h: f"0x{h.gameobject_ref_info_tbl:X}"),
                ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                ("UserData Info Tbl", lambda h: f"0x{h.userdata_info_tbl:X}"),
                ("Data Offset", lambda h: f"0x{h.data_offset:X}"),
            ]
        elif self.scn.is_usr:
            header_fields = [
                ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip("\x00")),
                ("Resource Count", lambda h: str(h.resource_count)),
                ("UserData Count", lambda h: str(h.userdata_count)),
                ("Info Count", lambda h: str(h.info_count)),
                ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                ("UserData Info Tbl", lambda h: f"0x{h.userdata_info_tbl:X}"),
                ("Data Offset", lambda h: f"0x{h.data_offset:X}"),
                ("Reserved", lambda h: str(h.reserved)),
            ]
        else:
            header_fields = [
                ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip("\x00")),
                ("Info Count", lambda h: str(h.info_count)),
                ("Resource Count", lambda h: str(h.resource_count)),
                ("Folder Count", lambda h: str(h.folder_count)),
                ("Prefab Count", lambda h: str(h.prefab_count)),
                ("UserData Count", lambda h: str(h.userdata_count)),
                ("Folder Tbl", lambda h: f"0x{h.folder_tbl:X}"),
                ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                ("Prefab Info Tbl", lambda h: f"0x{h.prefab_info_tbl:X}"),
                ("UserData Info Tbl", lambda h: f"0x{h.userdata_info_tbl:X}"),
                ("Data Offset", lambda h: f"0x{h.data_offset:X}"),
            ]
        for title, getter in header_fields:
            children.append(
                DataTreeBuilder.create_data_node(
                    title
                    + ": "
                    + getter(
                        self.scn.header,
                    )
                )
            )
        return DataTreeBuilder.create_data_node("Header", "", children=children)

    def _create_gameobject_ref_infos(self):
        """Create GameObjectRefInfo section for PFB files"""
        node = DataTreeBuilder.create_data_node(
            "GameObjectRefInfos", f"{len(self.scn.gameobject_ref_infos)} items"
        )
        for i, gori in enumerate(self.scn.gameobject_ref_infos):
            ref_node = DataTreeBuilder.create_data_node(f"GameObjectRefInfo[{i}]", "")
            ref_node["children"] = [
                DataTreeBuilder.create_data_node(f"Object ID: {gori.object_id}", ""),
                DataTreeBuilder.create_data_node(f"Property ID: {gori.property_id}", ""),
                DataTreeBuilder.create_data_node(f"Array Index: {gori.array_index}", ""),
                DataTreeBuilder.create_data_node(f"Target ID: {gori.target_id}", ""),
            ]
            node["children"].append(ref_node)
        return node

    def _create_gameobjects_info(self):
        """Create GameObjects info section"""
        node = ScnTreeBuilder.create_gameobjects_node(len(self.scn.gameobjects))
        for i, go in enumerate(self.scn.gameobjects):
            instance_index = None
            if go.id < len(self.scn.object_table):
                instance_index = self.scn.object_table[go.id]
            instance_name = self._get_gameobject_name(instance_index, f"GameObject[{i}]")
            if self.scn.is_pfb:
                children = [
                    DataTreeBuilder.create_data_node("ID: " + str(go.id), ""),
                    DataTreeBuilder.create_data_node("Parent ID: " + str(go.parent_id), ""),
                    DataTreeBuilder.create_data_node(
                        "Component Count: " + str(go.component_count), ""
                    ),
                ]
            else:
                children = [
                    DataTreeBuilder.create_data_node("GUID: " + guid_le_to_str(go.guid), ""),
                    DataTreeBuilder.create_data_node("ID: " + str(go.id), ""),
                    DataTreeBuilder.create_data_node("Parent ID: " + str(go.parent_id), ""),
                    DataTreeBuilder.create_data_node(
                        "Component Count: " + str(go.component_count), ""
                    ),
                    DataTreeBuilder.create_data_node("Prefab ID: " + str(go.prefab_id), ""),
                ]
            go_item = DataTreeBuilder.create_data_node(instance_name, "", children=children)
            node["children"].append(go_item)
        return node

    def _create_folders_info(self):
        node = DataTreeBuilder.create_data_node(
            "Folder Infos", f"{len(self.scn.folder_infos)} items"
        )
        for i, folder in enumerate(self.scn.folder_infos):
            if folder.id < len(self.scn.object_table):
                folder_name = self._get_folder_name(folder.id, f"FolderInfo[{i}]")
                folder_node = DataTreeBuilder.create_data_node(folder_name, "")
                folder_node["children"] = [
                    DataTreeBuilder.create_data_node(f"ID: {folder.id}", ""),
                    DataTreeBuilder.create_data_node(
                        f"Instance ID: {self.scn.object_table[folder.id]}", ""
                    ),
                    DataTreeBuilder.create_data_node(f"Parent ID: {folder.parent_id}", ""),
                ]
                node["children"].append(folder_node)
        return node

    def _create_object_table_info(self):
        node = DataTreeBuilder.create_data_node(
            "Object Table", f"{len(self.scn.object_table)} items"
        )
        for i, entry in enumerate(self.scn.object_table):
            node["children"].append(DataTreeBuilder.create_data_node(f"Entry {i}: {entry}", ""))
        return node

    def _create_instance_infos(self):
        node = DataTreeBuilder.create_data_node(
            "Instance Infos", f"{len(self.scn.instance_infos)} items"
        )
        for i, inst in enumerate(self.scn.instance_infos):
            if i == 0:
                node["children"].append(DataTreeBuilder.create_data_node("NULL Entry", ""))
            else:
                friendly = f"Instance[{i}]"
                if self.type_registry:
                    info = self.type_registry.get_type_info(int(inst.type_id))
                    if info and "name" in info:
                        friendly = info["name"]
                children = [
                    DataTreeBuilder.create_data_node(f"Type: 0x{inst.type_id:08X}", ""),
                    DataTreeBuilder.create_data_node(f"CRC: 0x{inst.crc:08X}", ""),
                ]
                inst_node = DataTreeBuilder.create_data_node(friendly, "", children=children)
                node["children"].append(inst_node)
        return node

    def _create_userdata_infos(self):
        node = DataTreeBuilder.create_data_node(
            "RSZUserData Infos", f"{len(self.scn.rsz_userdata_infos)} items"
        )
        for i, rui in enumerate(self.scn.rsz_userdata_infos):
            str_val = self.scn.get_rsz_userdata_string(rui) if rui.string_offset != 0 else ""
            node["children"].append(
                DataTreeBuilder.create_data_node(f"RSZUserDataInfo[{i}] : {str_val}", "")
            )
        return node

    def _add_data_block(self, parent_dict):
        if self.handler.scn_file.is_usr:
            if len(self.scn.object_table) > 0:
                root_instance_id = self.scn.object_table[0]
                if root_instance_id in self.scn.parsed_elements:
                    inst_info = self.scn.instance_infos[root_instance_id]
                    type_info = self.type_registry.get_type_info(inst_info.type_id)
                    type_name = (
                        type_info["name"] if type_info and "name" in type_info else "UserData"
                    )
                    root_dict = {
                        "data": [f"{type_name} (ID: {root_instance_id})", ""],
                        "children": [],
                    }
                    fields = self.scn.parsed_elements[root_instance_id]
                    for field_name, field_data in fields.items():
                        root_dict["children"].append(
                            self._create_field_dict(field_name, field_data)
                        )
                    parent_dict["children"].append(root_dict)
            return
        processed = set()
        nodes = {}
        gameobjects_folder = {"data": ["GameObjects", ""], "children": []}
        folders_folder = {"data": ["Folders", ""], "children": []}
        parent_dict["children"].append(gameobjects_folder)
        parent_dict["children"].append(folders_folder)
        for go in self.scn.gameobjects:
            if go.id >= len(self.scn.object_table):
                continue
            go_instance_id = self.scn.object_table[go.id]
            if go_instance_id in processed:
                continue
            reasy_id = IdManager.instance().register_instance(go_instance_id)
            v0_name = self._get_instance_v0_name(go_instance_id)
            go_name = v0_name if v0_name else self.get_instance_name(go_instance_id)
            go_dict = {
                "data": [f"{go_name} (ID: {go_instance_id})", ""],
                "type": "gameobject",
                "instance_id": go_instance_id,
                "reasy_id": reasy_id,
                "children": [],
            }
            nodes[go.id] = (go_dict, go.parent_id)
            settings_node = {"data": ["Settings", ""], "children": []}
            go_dict["children"].append(settings_node)
            if go_instance_id in self.scn.parsed_elements:
                fields = self.scn.parsed_elements[go_instance_id]
                for field_name, field_data in fields.items():
                    settings_node["children"].append(
                        self._create_field_dict(field_name, field_data)
                    )
            processed.add(go_instance_id)
            if go.component_count > 0:
                comp_node = {"data": ["Components", ""], "children": []}
                go_dict["children"].append(comp_node)
                for i in range(1, go.component_count + 1):
                    component_go_id = go.id + i
                    if component_go_id >= len(self.scn.object_table):
                        break
                    comp_instance_id = self.scn.object_table[component_go_id]
                    if comp_instance_id in processed:
                        continue
                    reasy_id = IdManager.instance().register_instance(comp_instance_id)
                    component_name = self._get_instance_v0_name(
                        comp_instance_id
                    ) or self.get_instance_name(comp_instance_id)
                    comp_dict = {
                        "data": [f"{component_name} (ID: {comp_instance_id})", ""],
                        "instance_id": comp_instance_id,
                        "reasy_id": reasy_id,
                        "children": [],
                    }
                    comp_node["children"].append(comp_dict)
                    if comp_instance_id in self.scn.parsed_elements:
                        fields = self.scn.parsed_elements[comp_instance_id]
                        for f_name, f_data in fields.items():
                            comp_dict["children"].append(self._create_field_dict(f_name, f_data))
                    processed.add(comp_instance_id)
            gameobjects_folder["children"].append(go_dict)
        for folder in self.scn.folder_infos:
            if folder.id >= len(self.scn.object_table):
                continue
            folder_instance_id = self.scn.object_table[folder.id]
            if folder_instance_id in processed:
                continue
            reasy_id = IdManager.instance().register_instance(folder_instance_id)
            folder_name = self._get_instance_v0_name(folder_instance_id) or self.get_instance_name(
                folder_instance_id
            )
            folder_dict = {
                "data": [f"{folder_name} (ID: {folder_instance_id})", ""],
                "type": "folder",
                "instance_id": folder_instance_id,
                "reasy_id": reasy_id,
                "children": [],
            }
            nodes[folder.id] = (folder_dict, folder.parent_id)
            settings_node = {"data": ["Settings", ""], "children": []}
            folder_dict["children"].append(settings_node)
            if folder_instance_id in self.scn.parsed_elements:
                fields = self.scn.parsed_elements[folder_instance_id]
                for field_name, field_data in fields.items():
                    settings_node["children"].append(
                        self._create_field_dict(field_name, field_data)
                    )
            processed.add(folder_instance_id)
            folders_folder["children"].append(folder_dict)
        for id_, (node_dict, parent_id) in nodes.items():
            if parent_id in nodes:
                parent_node_dict, _ = nodes[parent_id]
                children_node = None
                for ch in parent_node_dict["children"]:
                    if ch["data"][0] == "Children":
                        children_node = ch
                        break
                if not children_node:
                    children_node = {"data": ["Children", ""], "children": []}
                    parent_node_dict["children"].append(children_node)
                def remove_from_folder(folder_data, target):
                    if target in folder_data["children"]:
                        folder_data["children"].remove(target)
                remove_from_folder(gameobjects_folder, node_dict)
                remove_from_folder(folders_folder, node_dict)
                children_node["children"].append(node_dict)

    def _get_userdata_display_value(self, ref_id):
        """Get display value for UserData reference"""
        for rui in self.scn.rsz_userdata_infos:
            if rui.instance_id == ref_id:
                return self.scn.get_rsz_userdata_string(rui)
        return ""

    def _create_field_dict(self, field_name, data_obj):
        """Creates dictionary node with added type info - refactored to reduce redundancy"""
        if isinstance(data_obj, ArrayData):
            children = []
            original_type = f"{data_obj.orig_type}" if data_obj.orig_type else ""
            for i, element in enumerate(data_obj.values):
                if isinstance(element, ObjectData):
                    ref_id = element.value
                    if ref_id in self.scn._rsz_userdata_set:
                        display_value = self._get_userdata_display_value(ref_id)
                        obj_node = DataTreeBuilder.create_data_node(
                            str(i) + f": {display_value}", ""
                        )
                        children.append(obj_node)
                    else:
                        type_name = self._get_type_name_for_instance(ref_id)
                        obj_node = DataTreeBuilder.create_data_node(str(i) + f": ({type_name})", "")
                        if ref_id in self.scn.parsed_elements:
                            for fn, fd in self.scn.parsed_elements[ref_id].items():
                                obj_node["children"].append(self._create_field_dict(fn, fd))
                        children.append(obj_node)
                else:
                    element_type = element.__class__.__name__
                    children.append(
                        DataTreeBuilder.create_data_node(str(i) + ": ", "", element_type, element)
                    )
            return DataTreeBuilder.create_data_node(
                f"{field_name}: {original_type}", "", "array", data_obj, children
            )
        elif isinstance(data_obj, ObjectData):
            ref_id = data_obj.value
            if ref_id in self.scn._rsz_userdata_set:
                display_value = self._get_userdata_display_value(ref_id)
                return DataTreeBuilder.create_data_node(
                    f"{field_name}: {display_value}", "", None, None, []
                )
            type_name = self._get_type_name_for_instance(ref_id)
            children = []
            if ref_id in self.scn.parsed_elements:
                for fn, fd in self.scn.parsed_elements[ref_id].items():
                    children.append(self._create_field_dict(fn, fd))
            return DataTreeBuilder.create_data_node(
                f"{field_name}: ({type_name})", "", None, None, children
            )
        elif isinstance(data_obj, UserDataData):
            return DataTreeBuilder.create_data_node(
                f"{field_name}:", data_obj.value, data_obj.__class__.__name__, data_obj
            )
        else:
            return DataTreeBuilder.create_data_node(
                f"{field_name}:", "", data_obj.__class__.__name__, data_obj
            )

    def _get_type_name_for_instance(self, instance_id):
        """Get type name for an instance ID with optimized lookup"""
        if instance_id >= len(self.scn.instance_infos):
            return "Invalid ID"
        if (
            getattr(self, "_last_added_object", None)
            and self._last_added_object.value == instance_id
            and self._last_added_object.orig_type
        ):
            return self._last_added_object.orig_type
        inst_info = self.scn.instance_infos[instance_id]
        type_info = self.type_registry.get_type_info(inst_info.type_id)
        return (
            type_info.get("name", f"Type 0x{inst_info.type_id:08X}")
            if type_info
            else f"Type 0x{inst_info.type_id:08X}"
        )

    def get_instance_name(self, instance_id):
        """Fallback name from type info or instance id."""
        if instance_id >= len(self.scn.instance_infos):
            return f"Instance[{instance_id}]"
        inst_info = self.scn.instance_infos[instance_id]
        type_info = self.scn.type_registry.get_type_info(inst_info.type_id)
        if type_info and "name" in type_info:
            return type_info["name"]
        return f"Instance[{instance_id}]"

    def _get_gameobject_name(self, instance_index, default_name):
        return default_name

    def _get_folder_name(self, folder_id, default_name):
        return default_name

    def _create_rsz_header_info(self):
        return DataTreeBuilder.create_data_node("RSZHeader", "")

    def _get_instance_v0_name(self, instance_id):
        """Get name from v0 field if available."""
        if instance_id in self.scn.parsed_elements:
            fields = self.scn.parsed_elements[instance_id]
            if "v0" in fields and isinstance(fields["v0"], StringData):
                return fields["v0"].value.rstrip("\x00")
        return None

    def embed_forms(self):
        def on_modified():
            self.mark_modified()
        self.tree.embed_forms(parent_modified_callback=on_modified)

    def rebuild(self) -> bytes:
        if not self.handler:
            raise AttributeError("No handler assigned to viewer")
        try:
            return self.handler.scn_file.build()
        except Exception as e:
            raise RuntimeError(f"Failed to rebuild SCN file: {str(e)}")

    def create_array_element(self, element_type, array_data, direct_update=False, array_item=None):
        if not self.array_operations:
            self.array_operations = RszArrayOperations(self)
        return self.array_operations.create_array_element(
            element_type, array_data, direct_update, array_item
        )

    def delete_array_element(self, array_data, element_index):
        if not self.array_operations:
            self.array_operations = RszArrayOperations(self)
        return self.array_operations.delete_array_element(array_data, element_index)

    def create_component_for_gameobject(self, gameobject_instance_id, component_type):
        if gameobject_instance_id <= 0 or gameobject_instance_id >= len(self.scn.instance_infos):
            raise ValueError(f"Invalid GameObject instance ID: {gameobject_instance_id}")
        target_go = None
        for i, go in enumerate(self.scn.gameobjects):
            if (
                go.id < len(self.scn.object_table)
                and self.scn.object_table[go.id] == gameobject_instance_id
            ):
                target_go = go
                break
        if not target_go:
            raise ValueError(f"GameObject with instance ID {gameobject_instance_id} not found")
        type_info, type_id = self.type_registry.find_type_by_name(component_type)
        if not type_info:
            raise ValueError(f"Component type '{component_type}' not found in registry")
        object_table_insertion_index = target_go.id + target_go.component_count + 1
        new_instance = self._initialize_new_instance(type_id, type_info)
        if not new_instance:
            raise ValueError(f"Failed to create instance of type {component_type}")
        instance_insertion_index = self._calculate_component_insertion_index(target_go)
        temp_parsed_elements = {}
        nested_objects = []
        self._analyze_instance_fields_for_nested_objects(
            temp_parsed_elements, type_info, nested_objects, instance_insertion_index
        )
        valid_nested_objects = []
        for nested_type_info, nested_type_id in nested_objects:
            nested_instance = self._initialize_new_instance(nested_type_id, nested_type_info)
            if not nested_instance or nested_instance.type_id == 0:
                continue
            self._insert_instance_and_update_references(instance_insertion_index, nested_instance)
            nested_object_fields = {}
            self._initialize_fields_from_type_info(nested_object_fields, nested_type_info)
            self.scn.parsed_elements[instance_insertion_index] = nested_object_fields
            valid_nested_objects.append((nested_type_info, nested_type_id))
            instance_insertion_index += 1
            IdManager.instance().register_instance(instance_insertion_index)
        self._insert_instance_and_update_references(instance_insertion_index, new_instance)
        IdManager.instance().register_instance(instance_insertion_index)
        component_fields = {}
        self._initialize_fields_from_type_info(component_fields, type_info)
        self._update_object_references(
            component_fields,
            temp_parsed_elements,
            instance_insertion_index,
            valid_nested_objects,
        )
        if "parent" in component_fields:
            parent_obj = component_fields["parent"]
            if hasattr(parent_obj, "value"):
                parent_obj.value = gameobject_instance_id
        self.scn.parsed_elements[instance_insertion_index] = component_fields
        target_go.component_count += 1
        self._insert_into_object_table(object_table_insertion_index, instance_insertion_index)
        self.mark_modified()
        return True

    def _insert_into_object_table(self, object_table_index, instance_id):
        if object_table_index >= len(self.scn.object_table):
            self.scn.object_table.extend(
                [0] * (object_table_index - len(self.scn.object_table) + 1)
            )
            self.scn.object_table[object_table_index] = instance_id
        else:
            self.scn.object_table.insert(object_table_index, instance_id)
        for go in self.scn.gameobjects:
            if go.id >= object_table_index:
                go.id += 1
            if go.parent_id >= object_table_index:
                go.parent_id += 1
        for folder in self.scn.folder_infos:
            if folder.id >= object_table_index:
                folder.id += 1
            if folder.parent_id >= object_table_index:
                folder.parent_id += 1
        if self.scn.is_pfb:
            for ref_info in self.scn.gameobject_ref_infos:
                if hasattr(ref_info, "object_id") and ref_info.object_id >= object_table_index:
                    ref_info.object_id += 1
                if hasattr(ref_info, "target_id") and ref_info.target_id >= object_table_index:
                    ref_info.target_id += 1

    def _calculate_component_insertion_index(self, gameobject):
        """Calculate the best insertion index for a new component"""
        insertion_index = len(self.scn.instance_infos)
        if gameobject.component_count > 0:
            last_component_go_id = gameobject.id + gameobject.component_count
            if last_component_go_id < len(self.scn.object_table):
                last_component_instance_id = self.scn.object_table[last_component_go_id]
                if last_component_instance_id > 0:
                    insertion_index = last_component_instance_id + 1
        if insertion_index == len(self.scn.instance_infos):
            go_instance_id = self.scn.object_table[gameobject.id]
            if go_instance_id > 0:
                insertion_index = go_instance_id + 1
        return insertion_index

    def delete_component_from_gameobject(self, component_instance_id, owner_go_id=None):
        if component_instance_id <= 0 or component_instance_id >= len(self.scn.instance_infos):
            raise ValueError(f"Invalid component instance ID: {component_instance_id}")
        
        object_table_index = -1
        owner_go = None
        
        if owner_go_id is not None:
            owner_go = next((go for go in self.scn.gameobjects if go.id == owner_go_id), None)
            if owner_go:
                for comp_index in range(1, owner_go.component_count + 1):
                    comp_object_id = owner_go.id + comp_index
                    if (comp_object_id < len(self.scn.object_table) and 
                        self.scn.object_table[comp_object_id] == component_instance_id):
                        object_table_index = comp_object_id
                        break
        
        if object_table_index < 0:
            for i, instance_id in enumerate(self.scn.object_table):
                if instance_id == component_instance_id:
                    object_table_index = i
                    break
                    
            if object_table_index < 0:
                raise ValueError(f"Component {component_instance_id} not found in object table")
            
            if not owner_go:
                for go in self.scn.gameobjects:
                    if go.id < object_table_index and object_table_index <= go.id + go.component_count:
                        owner_go = go
                        break
        
        if not owner_go:
            raise ValueError(f"Could not find GameObject owning component {component_instance_id}")
        
        original_component_count = owner_go.component_count
        
        instance_fields = self.scn.parsed_elements.get(component_instance_id, {})
        nested_objects = self._find_nested_objects(instance_fields, component_instance_id)
        nested_objects.add(component_instance_id)
        
        to_delete_instances = sorted(nested_objects, reverse=True)
        
        owner_go.component_count -= 1
        
        self.scn.object_table.pop(object_table_index)
        
        for go in self.scn.gameobjects:
            if go.id > object_table_index:
                go.id -= 1
            if go.parent_id > object_table_index:
                go.parent_id -= 1
                
        for folder in self.scn.folder_infos:
            if folder.id > object_table_index:
                folder.id -= 1
            if folder.parent_id > object_table_index:
                folder.parent_id -= 1
                
        if self.scn.is_pfb:
            for ref_info in self.scn.gameobject_ref_infos:
                if hasattr(ref_info, 'object_id') and ref_info.object_id > object_table_index:
                    ref_info.object_id -= 1
                if hasattr(ref_info, 'target_id') and ref_info.target_id > object_table_index:
                    ref_info.target_id -= 1
        
        for instance_id in to_delete_instances:
            self._remove_instance_references(instance_id)
        
        all_deleted = set(to_delete_instances)
        id_mapping = self._update_instance_references_after_deletion(component_instance_id, all_deleted)
        deleted_instance_ids = set(to_delete_instances)
        IdManager.instance().update_all_mappings(id_mapping, deleted_instance_ids)
        
        expected_component_indices = original_component_count - 1
        actual_component_indices = 0
        for i in range(1, expected_component_indices + 1):
            comp_object_id = owner_go.id + i
            if comp_object_id < len(self.scn.object_table) and self.scn.object_table[comp_object_id] > 0:
                actual_component_indices += 1
                
        if actual_component_indices != expected_component_indices:
            print(f"Warning: Component count mismatch after deletion - expected {expected_component_indices}, got {actual_component_indices}")
        
        self.mark_modified()
        
        return True

    def create_gameobject(self, name, parent_id):
        if not self.scn:
            return False
        insertion_index = self._calculate_gameobject_insertion_index()
        type_info, type_id = self.type_registry.find_type_by_name("via.GameObject")
        if not type_info or type_id == 0:
            QMessageBox.warning(self, "Error", "Cannot find GameObject type in registry")
            return False
        new_instance = self._initialize_new_instance(type_id, type_info)
        if not new_instance:
            QMessageBox.warning(self, "Error", "Failed to create GameObject instance")
            return False
        self._insert_instance_and_update_references(insertion_index, new_instance)
        IdManager.instance().register_instance(insertion_index)
        gameobject_fields = {}
        self._initialize_fields_from_type_info(gameobject_fields, type_info)
        if "v0" in gameobject_fields and hasattr(gameobject_fields["v0"], "set_value"):
            gameobject_fields["v0"].set_value(name)
        self.scn.parsed_elements[insertion_index] = gameobject_fields
        object_table_index = len(self.scn.object_table)
        self.scn.object_table.append(insertion_index)
        new_gameobject = self._create_gameobject_entry(
            object_table_index, parent_id, insertion_index
        )
        self._update_gameobject_hierarchy(new_gameobject)
        self.scn.gameobjects.append(new_gameobject)
        self.mark_modified()
        return True

    def _initialize_fields_from_type_info(self, fields_dict, type_info):
        """Initialize fields based on type info"""
        for field_def in type_info.get("fields", []):
            field_name = field_def.get("name", "")
            if not field_name:
                continue
            field_type = field_def.get("type", "unknown").lower()
            field_size = field_def.get("size", 4)
            field_native = field_def.get("native", False)
            field_array = field_def.get("array", False)
            field_align = field_def.get("align", 4)
            field_orig_type = field_def.get("original_type", "")
            field_class = get_type_class(
                field_type, field_size, field_native, field_array, field_align
            )
            field_obj = self._create_default_field(field_class, field_orig_type, field_array)
            if field_obj:
                fields_dict[field_name] = field_obj

    def _calculate_gameobject_insertion_index(self):
        """Calculate the best insertion index for a new GameObject instance"""
        return len(self.scn.instance_infos)

    def _create_gameobject_entry(self, object_id, parent_id, instance_id):
        from .rsz_file import ScnGameObject
        new_go = ScnGameObject()
        new_go.id = object_id
        new_go.parent_id = parent_id
        new_go.component_count = 0
        if not self.scn.is_pfb and not self.scn.is_usr:
            import uuid
            guid_bytes = uuid.uuid4().bytes_le
            new_go.guid = guid_bytes
            new_go.prefab_id = 0
        return new_go

    def _update_gameobject_hierarchy(self, gameobject):
        """Update instance hierarchy with parent-child relationship for GameObjects"""
        instance_id = self.scn.object_table[gameobject.id]
        self.scn.instance_hierarchy[instance_id] = {"children": [], "parent": None}
        if gameobject.parent_id >= 0:
            if gameobject.parent_id < len(self.scn.object_table):
                parent_instance_id = self.scn.object_table[gameobject.parent_id]
                if parent_instance_id > 0:
                    self.scn.instance_hierarchy[instance_id]["parent"] = parent_instance_id
                    if parent_instance_id in self.scn.instance_hierarchy:
                        if "children" not in self.scn.instance_hierarchy[parent_instance_id]:
                            self.scn.instance_hierarchy[parent_instance_id]["children"] = []
                        self.scn.instance_hierarchy[parent_instance_id]["children"].append(
                            instance_id
                        )

    def delete_gameobject(self, gameobject_id):
        if gameobject_id < 0 or gameobject_id >= len(self.scn.object_table):
            QMessageBox.warning(self, "Error", f"Invalid GameObject ID: {gameobject_id}")
            return False
            
        target_go = None
        
        for go in self.scn.gameobjects:
            if go.id == gameobject_id:
                target_go = go
                break
        
        if target_go is None:
            print(f"Warning: GameObject with exact ID {gameobject_id} not found, attempting recovery...")
            
            instance_id = self.scn.object_table[gameobject_id] if gameobject_id < len(self.scn.object_table) else 0
            if instance_id > 0:
                for go in self.scn.gameobjects:
                    if go.id < len(self.scn.object_table) and self.scn.object_table[go.id] == instance_id:
                        target_go = go
                        gameobject_id = go.id
                        print(f"  Found GameObject at adjusted ID {gameobject_id}")
                        break
        
        if target_go is None:
            QMessageBox.warning(self, "Error", f"GameObject with ID {gameobject_id} not found")
            return False
            
        success = self._delete_gameobject_directly(target_go)
        
        if success:
            self.mark_modified()
            
        return success

    def _collect_gameobject_hierarchy_by_reference(self, root_go):
        go_objects = {go.id: go for go in self.scn.gameobjects}
        
        child_map = {}
        for go in self.scn.gameobjects:
            if go.parent_id >= 0 and go.parent_id in go_objects:
                parent = go_objects[go.parent_id]
                if parent not in child_map:
                    child_map[parent] = []
                child_map[parent].append(go)
        
        gameobjects = []
        
        def collect_recursive(go):
            gameobjects.append(go)
            if go in child_map:
                for child in child_map[go]:
                    collect_recursive(child)
        
        collect_recursive(root_go)
        return gameobjects

    def _delete_all_components_of_gameobject(self, gameobject):
        if gameobject.component_count <= 0:
            return
            
        print(f"GameObject {gameobject.id} has {gameobject.component_count} components")
        
        if gameobject.id >= len(self.scn.object_table):
            print(f"Warning: GameObject ID {gameobject.id} is out of bounds, cannot delete components")
            gameobject.component_count = 0
            return
        
        initial_component_count = gameobject.component_count
        deleted_count = 0
        
        max_iterations = initial_component_count * 2
        iteration = 0
        
        while gameobject.component_count > 0 and iteration < max_iterations:
            iteration += 1
            
            component_object_id = gameobject.id + 1
            
            if component_object_id >= len(self.scn.object_table):
                print(f"  Warning: Component object ID {component_object_id} is out of bounds")
                gameobject.component_count = 0
                break
                
            component_instance_id = self.scn.object_table[component_object_id]
            
            if component_instance_id <= 1:
                print(f"  Skipping invalid component with instance_id={component_instance_id}")
                gameobject.component_count -= 1
                continue
                
            try:
                self.delete_component_from_gameobject(component_instance_id, gameobject.id)
                deleted_count += 1
            except Exception as e:
                print(f"  Error deleting component {component_instance_id}: {str(e)}")
        
        if deleted_count != initial_component_count:
            print(f"  Warning: Expected to delete {initial_component_count} components, but deleted {deleted_count}")
            gameobject.component_count = 0

    def delete_folder(self, folder_id):
        if folder_id < 0 or folder_id >= len(self.scn.object_table):
            QMessageBox.warning(self, "Error", f"Invalid Folder ID: {folder_id}")
            return False

        target_folder = None
        for folder in self.scn.folder_infos:
            if folder.id == folder_id:
                target_folder = folder
                break

        if target_folder is None:
            print(f"Warning: Folder with exact ID {folder_id} not found, attempting recovery...")
            
            instance_id = self.scn.object_table[folder_id] if folder_id < len(self.scn.object_table) else 0
            if instance_id > 0:
                for folder in self.scn.folder_infos:
                    if folder.id < len(self.scn.object_table) and self.scn.object_table[folder.id] == instance_id:
                        target_folder = folder
                        folder_id = folder.id
                        print(f"  Found Folder at adjusted ID {folder_id}")
                        break

        if target_folder is None:
            QMessageBox.warning(self, "Error", f"Folder with ID {folder_id} not found")
            return False

        folder_instance_id = self.scn.object_table[folder_id] if folder_id < len(self.scn.object_table) else 0
        print(f"Deleting folder ID={folder_id} (instance_id={folder_instance_id})")

        folder_objects = {f.id: f for f in self.scn.folder_infos}
        
        folder_child_map = {}
        for folder in self.scn.folder_infos:
            if folder.parent_id in folder_objects:
                parent_folder = folder_objects[folder.parent_id]
                if parent_folder not in folder_child_map:
                    folder_child_map[parent_folder] = []
                folder_child_map[parent_folder].append(folder)

        folders_to_delete = []
        
        def collect_folders_recursive(folder):
            folders_to_delete.append(folder)
            if folder in folder_child_map:
                for child in folder_child_map[folder]:
                    collect_folders_recursive(child)
        
        collect_folders_recursive(target_folder)
        
        print(f"Found {len(folders_to_delete)} folders to delete")
        
        folder_ids = {f.id for f in folders_to_delete}
        
        gameobjects_to_delete = []
        for go in self.scn.gameobjects:
            if go.parent_id in folder_ids:
                gameobjects_to_delete.append(go)
        
        print(f"Found {len(gameobjects_to_delete)} GameObjects to delete")
        
        deletion_errors = 0
        
        for go in reversed(gameobjects_to_delete):
            try:
                success = self._delete_gameobject_directly(go)
                if not success:
                    print(f"  Warning: Failed to delete GameObject with ID {go.id}")
                    deletion_errors += 1
            except Exception as e:
                print(f"  Error deleting GameObject with ID {go.id}: {str(e)}")
                deletion_errors += 1

        for folder in reversed(folders_to_delete):
            try:
                if folder is target_folder:
                    continue
                    
                if not self.scn.is_pfb and not self.scn.is_usr and hasattr(folder, 'prefab_id') and folder.prefab_id > 0:
                    prefab_deleted = self._delete_prefab_for_object(folder.prefab_id)
                    if prefab_deleted:
                        print(f"  Deleted prefab {folder.prefab_id} associated with folder {folder.id}")
                
                print(f"  Deleting subfolder with ID {folder.id}")
                folder_instance_id = self.scn.object_table[folder.id] if folder.id < len(self.scn.object_table) else 0
                
                if folder_instance_id > 0:
                    instance_fields = self.scn.parsed_elements.get(folder_instance_id, {})
                    nested_objects = self._find_nested_objects(instance_fields, folder_instance_id)
                    nested_objects.add(folder_instance_id)
                    
                    for inst_id in sorted(nested_objects, reverse=True):
                        self._remove_instance_references(inst_id)
                    
                    id_mapping = self._update_instance_references_after_deletion(folder_instance_id, nested_objects)
                    
                    if id_mapping:
                        IdManager.instance().update_all_mappings(id_mapping, nested_objects)
                
                if folder.id < len(self.scn.object_table):
                    self._remove_from_object_table(folder.id)
                
                if folder in self.scn.folder_infos:
                    self.scn.folder_infos.remove(folder)
            except Exception as e:
                print(f"  Error deleting subfolder with ID {folder.id}: {str(e)}")
                deletion_errors += 1

        try:
            print(f"  Deleting target folder with ID {target_folder.id}")
            folder_instance_id = self.scn.object_table[target_folder.id] if target_folder.id < len(self.scn.object_table) else 0
            
            if folder_instance_id > 0:
                instance_fields = self.scn.parsed_elements.get(folder_instance_id, {})
                nested_objects = self._find_nested_objects(instance_fields, folder_instance_id)
                nested_objects.add(folder_instance_id)
                
                for inst_id in sorted(nested_objects, reverse=True):
                    self._remove_instance_references(inst_id)
                
                id_mapping = self._update_instance_references_after_deletion(folder_instance_id, nested_objects)
                
                if id_mapping:
                    IdManager.instance().update_all_mappings(id_mapping, nested_objects)
            
            if target_folder.id < len(self.scn.object_table):
                self._remove_from_object_table(target_folder.id)
            
            if target_folder in self.scn.folder_infos:
                self.scn.folder_infos.remove(target_folder)
        except Exception as e:
            print(f"  Error deleting target folder: {str(e)}")
            deletion_errors += 1
        
        if deletion_errors > 0:
            print(f"Warning: Encountered {deletion_errors} errors during folder deletion")
        
        self.mark_modified()
        return True

    def _create_default_field(self, data_class, original_type, is_array=False):
        try:
            if is_array:
                return ArrayData([], data_class, original_type)
            if data_class == ObjectData:
                return ObjectData(0, original_type)
            if data_class == RawBytesData:
                raise ValueError("Unsupported field type: RawBytesData")
            return data_class()
        except Exception as e:
            print(f"Error creating field: {str(e)}")
            return None

    def _insert_instance_and_update_references(self, index, instance):
        self.scn.instance_infos.insert(index, instance)
        
        for i in range(len(self.scn.object_table)):
            if self.scn.object_table[i] >= index:
                self.scn.object_table[i] += 1
                
        updated_elements = {}
        for instance_id, fields in self.scn.parsed_elements.items():
            updated_fields = {}
            new_id = instance_id + 1 if instance_id >= index else instance_id
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData):
                    if field_data.value >= index:
                        field_data.value += 1
                elif isinstance(field_data, ResourceData) and hasattr(field_data, "value"):
                    if field_data.value >= index:
                        field_data.value += 1
                elif isinstance(field_data, UserDataData) and hasattr(field_data, "index"):
                    if field_data.index >= index:
                        field_data.index += 1
                elif isinstance(field_data, ArrayData):
                    for elem in field_data.values:
                        if isinstance(elem, ObjectData) and elem.value >= index:
                            elem.value += 1
                        elif (
                            isinstance(elem, ResourceData)
                            and hasattr(elem, "value")
                            and elem.value >= index
                        ):
                            elem.value += 1
                        elif (
                            isinstance(elem, UserDataData)
                            and hasattr(elem, "index")
                            and elem.index >= index
                        ):
                            elem.index += 1
                updated_fields[field_name] = field_data
            updated_elements[new_id] = updated_fields
        self.scn.parsed_elements = updated_elements
        
        updated_hierarchy = {}
        for instance_id, data in self.scn.instance_hierarchy.items():
            new_id = instance_id + 1 if instance_id >= index else instance_id
            children = data["children"].copy()
            updated_children = []
            for child in children:
                updated_children.append(child + 1 if child >= index else child)
            parent = data["parent"]
            if parent is not None and parent >= index:
                parent += 1
            updated_hierarchy[new_id] = {"children": updated_children, "parent": parent}
        self.scn.instance_hierarchy = updated_hierarchy
        
        self._shift_userdata_references(index)
        
        id_mapping = {}
        for i in range(index, len(self.scn.instance_infos)):
            id_mapping[i] = i + 1
        IdManager.instance().update_all_mappings(id_mapping)

    def _shift_userdata_references(self, index):
        updated_userdata_set = set()
        for id_value in self.scn._rsz_userdata_set:
            updated_userdata_set.add(id_value + 1 if id_value >= index else id_value)
        self.scn._rsz_userdata_set = updated_userdata_set
        
        updated_userdata_dict = {}
        for id_value, rui in self.scn._rsz_userdata_dict.items():
            new_id = id_value + 1 if id_value >= index else id_value
            updated_userdata_dict[new_id] = rui
        self.scn._rsz_userdata_dict = updated_userdata_dict
        
        for rui in self.scn.rsz_userdata_infos:
            if rui.instance_id >= index:
                rui.instance_id += 1

    def _remove_instance_references(self, instance_id):
        if instance_id < len(self.scn.instance_infos):
            del self.scn.instance_infos[instance_id]
        if instance_id in self.scn.parsed_elements:
            del self.scn.parsed_elements[instance_id]
        if instance_id in self.scn.instance_hierarchy:
            parent_id = self.scn.instance_hierarchy[instance_id].get("parent")
            if parent_id is not None and parent_id in self.scn.instance_hierarchy:
                if instance_id in self.scn.instance_hierarchy[parent_id]["children"]:
                    self.scn.instance_hierarchy[parent_id]["children"].remove(instance_id)
            del self.scn.instance_hierarchy[instance_id]
        
        self._cleanup_userdata_for_instance(instance_id)

        for i in range(len(self.scn.object_table)):
            if self.scn.object_table[i] == instance_id:
                self.scn.object_table[i] = 0
                
    def _cleanup_userdata_for_instance(self, instance_id):
        rsz_indices_to_remove = []
        rsz_userdata_to_remove = []
        userdata_indices_to_remove = []
        userdata_to_remove = []
        
        for i, rui in enumerate(self.scn.rsz_userdata_infos):
            if rui.instance_id == instance_id:
                rsz_indices_to_remove.append(i)
                rsz_userdata_to_remove.append(rui)
                
                for j, ui in enumerate(self.scn.userdata_infos):
                    if rui.hash == ui.hash:
                        if j not in userdata_indices_to_remove:
                            userdata_indices_to_remove.append(j)
                            userdata_to_remove.append(ui)
        
        if instance_id in self.scn._rsz_userdata_dict:
            del self.scn._rsz_userdata_dict[instance_id]
        if instance_id in self.scn._rsz_userdata_set:
            self.scn._rsz_userdata_set.remove(instance_id)
        
        for rui in rsz_userdata_to_remove:
            if rui in self.scn._rsz_userdata_str_map:
                del self.scn._rsz_userdata_str_map[rui]
                
        for ui in userdata_to_remove:
            if ui in self.scn._userdata_str_map:
                del self.scn._userdata_str_map[ui]
        
        for idx in sorted(rsz_indices_to_remove, reverse=True):
            del self.scn.rsz_userdata_infos[idx]
            
        for idx in sorted(userdata_indices_to_remove, reverse=True):
            del self.scn.userdata_infos[idx]

    def _update_instance_references_after_deletion(self, deleted_id, deleted_nested_ids=None):
        if deleted_nested_ids is None:
            deleted_nested_ids = set()
        deleted_nested_ids.add(deleted_id)
        deleted_ids_sorted = sorted(deleted_nested_ids)
        
        id_mapping = {}
        for old_id in range(len(self.scn.instance_infos) + len(deleted_ids_sorted)):
            if old_id not in deleted_nested_ids:
                new_id = old_id
                for deleted_id in deleted_ids_sorted:
                    if old_id > deleted_id:
                        new_id -= 1
                if old_id != new_id:
                    id_mapping[old_id] = new_id

        new_parsed_elements = {}
        for instance_id, fields in self.scn.parsed_elements.items():
            if instance_id in deleted_nested_ids:
                continue
                
            new_id = id_mapping.get(instance_id, instance_id)
            updated_fields = self._update_fields_after_deletion(fields, deleted_nested_ids, id_mapping)
            new_parsed_elements[new_id] = updated_fields
            
        self.scn.parsed_elements = new_parsed_elements
        
        new_hierarchy = {}
        for instance_id, data in self.scn.instance_hierarchy.items():
            if instance_id in deleted_nested_ids:
                continue
            new_id = id_mapping.get(instance_id, instance_id)
            new_children = [id_mapping.get(child_id, child_id) for child_id in data["children"] if child_id not in deleted_nested_ids]
            parent_id = data["parent"]
            if parent_id in deleted_nested_ids:
                parent_id = None
            else:
                parent_id = id_mapping.get(parent_id, parent_id)
            new_hierarchy[new_id] = {"children": new_children, "parent": parent_id}
        self.scn.instance_hierarchy = new_hierarchy
        
        self._update_userdata_references(deleted_nested_ids, id_mapping)
        
        for i in range(len(self.scn.object_table)):
            obj_id = self.scn.object_table[i]
            if obj_id in deleted_nested_ids:
                self.scn.object_table[i] = 0
            else:
                self.scn.object_table[i] = id_mapping.get(obj_id, obj_id)
        
        for go in self.scn.gameobjects:
            if go.id < len(self.scn.object_table):
                instance_id = self.scn.object_table[go.id]
                if instance_id > 0:
                    IdManager.instance().register_instance(instance_id)
        
        return id_mapping
    
    def _update_userdata_references(self, deleted_ids, id_mapping):
        new_userdata_dict = {}
        for instance_id, rui in self.scn._rsz_userdata_dict.items():
            if instance_id not in deleted_ids:
                new_id = id_mapping.get(instance_id, instance_id)
                new_userdata_dict[new_id] = rui
        self.scn._rsz_userdata_dict = new_userdata_dict
        
        new_userdata_set = {id_mapping.get(instance_id, instance_id) 
                           for instance_id in self.scn._rsz_userdata_set 
                           if instance_id not in deleted_ids}
        self.scn._rsz_userdata_set = new_userdata_set
        
        i = 0
        while i < len(self.scn.rsz_userdata_infos):
            rui = self.scn.rsz_userdata_infos[i]
            if rui.instance_id in deleted_ids:
                if rui in self.scn._rsz_userdata_str_map:
                    del self.scn._rsz_userdata_str_map[rui]
                self.scn.rsz_userdata_infos.pop(i)
                continue
            elif rui.instance_id in id_mapping:
                rui.instance_id = id_mapping[rui.instance_id]
            i += 1
        
        for ui in list(self.scn._userdata_str_map.keys()):
            if ui not in self.scn.userdata_infos:
                del self.scn._userdata_str_map[ui]

    def _update_fields_after_deletion(self, fields, deleted_ids, id_mapping):
        updated_fields = {}
        for field_name, field_data in fields.items():
            if isinstance(field_data, ObjectData):
                ref_id = field_data.value
                if ref_id > 0:
                    if ref_id in deleted_ids:
                        field_data.value = 0
                    else:
                        field_data.value = id_mapping.get(ref_id, ref_id)
            elif isinstance(field_data, ResourceData) and hasattr(field_data, "value"):
                ref_id = field_data.value
                if ref_id > 0:
                    if ref_id in deleted_ids:
                        field_data.value = 0
                    else:
                        field_data.value = id_mapping.get(ref_id, ref_id)
            elif isinstance(field_data, UserDataData) and hasattr(field_data, "index"):
                ref_id = field_data.index
                if ref_id > 0:
                    if ref_id in deleted_ids:
                        field_data.index = 0
                    else:
                        field_data.index = id_mapping.get(ref_id, ref_id)
            elif isinstance(field_data, ArrayData):
                for elem in field_data.values:
                    if isinstance(elem, ObjectData):
                        ref_id = elem.value
                        if ref_id > 0:
                            if ref_id in deleted_ids:
                                elem.value = 0
                            else:
                                elem.value = id_mapping.get(ref_id, ref_id)
                    elif isinstance(elem, ResourceData) and hasattr(elem, "value"):
                        ref_id = elem.value
                        if ref_id > 0:
                            if ref_id in deleted_ids:
                                elem.value = 0
                            else:
                                elem.value = id_mapping.get(ref_id, ref_id)
                    elif isinstance(elem, UserDataData) and hasattr(elem, "index"):
                        ref_id = elem.index
                        if ref_id > 0:
                            if ref_id in deleted_ids:
                                elem.index = 0
                            else:
                                elem.index = id_mapping.get(ref_id, ref_id)
            updated_fields[field_name] = field_data
        return updated_fields

    def _find_nested_objects(self, fields, base_instance_id):
        nested_objects = set()
        
        base_object_id = -1
        for i, instance_id in enumerate(self.scn.object_table):
            if instance_id == base_instance_id:
                base_object_id = i
                break
                
        if base_object_id <= 0:
            return nested_objects
            
        prev_instance_id = 0
        for i in range(base_object_id - 1, -1, -1):
            if self.scn.object_table[i] > 0:
                prev_instance_id = self.scn.object_table[i]
                break
        
        for instance_id in range(prev_instance_id + 1, base_instance_id):
            if instance_id > 0 and instance_id < len(self.scn.instance_infos) and self.scn.instance_infos[instance_id].type_id != 0:
                if instance_id not in self.scn.object_table:
                    nested_objects.add(instance_id)
                
        return nested_objects

    def _remove_from_object_table(self, object_table_index):
        if object_table_index < 0 or object_table_index >= len(self.scn.object_table):
            print(f"Warning: Invalid object table index {object_table_index}")
            return
            
        removed_instance_id = self.scn.object_table[object_table_index]
            
        self.scn.object_table.pop(object_table_index)
        
        for go in self.scn.gameobjects:
            if go.id > object_table_index:
                go.id -= 1
            if go.parent_id > object_table_index:
                go.parent_id -= 1
                
        for folder in self.scn.folder_infos:
            if folder.id > object_table_index:
                folder.id -= 1
            if folder.parent_id > object_table_index:
                folder.parent_id -= 1
                
        if self.scn.is_pfb:
            for ref_info in self.scn.gameobject_ref_infos:
                if hasattr(ref_info, 'object_id') and ref_info.object_id > object_table_index:
                    ref_info.object_id -= 1
                if hasattr(ref_info, 'target_id') and ref_info.target_id > object_table_index:
                    ref_info.target_id -= 1

    def _initialize_new_instance(self, type_id, type_info):
        if type_id == 0 or not type_info:
            return None
            
        new_instance = ScnInstanceInfo()
        
        new_instance.type_id = type_id
        
        if isinstance(type_info.get("crc", 0), str):
            crc_str = type_info.get("crc", "0")
            if crc_str.startswith('0x') or any(c in crc_str.lower() for c in 'abcdef'):
                crc = int(crc_str, 16)
            else:
                crc = int(crc_str, 10)
        else:
            crc = int(type_info.get("crc", 0))
        
        new_instance.crc = crc
        if(crc == 0):
            raise ValueError("CRC is 0")
        return new_instance

    def _initialize_fields_from_type_info(self, fields_dict, type_info):
        """Initialize fields based on type info"""
        for field_def in type_info.get("fields", []):
            field_name = field_def.get("name", "")
            if not field_name:
                continue
                
            field_type = field_def.get("type", "unknown").lower()
            field_size = field_def.get("size", 4)
            field_native = field_def.get("native", False)
            field_array = field_def.get("array", False)
            field_align = field_def.get("align", 4)
            field_orig_type = field_def.get("original_type", "")
            
            field_class = get_type_class(field_type, field_size, field_native, field_array, field_align)
            field_obj = self._create_default_field(field_class, field_orig_type, field_array)
            
            if field_obj:
                fields_dict[field_name] = field_obj
                
    def _analyze_instance_fields_for_nested_objects(self, temp_elements, type_info, nested_objects, parent_id, visited_types=None):
        if visited_types is None:
            visited_types = set()
        
        if not type_info or "fields" not in type_info:
            return {}
            
        type_name = type_info.get("name", "")
        if not type_name:
            return {}
            
        visited_types.add(type_name)
        
        fields_dict = {}
        for field_def in type_info.get("fields", []):
            field_name = field_def.get("name", "")
            if not field_name:
                continue
                
            field_type = field_def.get("type", "unknown").lower()
            field_size = field_def.get("size", 4)
            field_native = field_def.get("native", False)
            field_array = field_def.get("array", False)
            field_align = field_def.get("align", 4)
            field_orig_type = field_def.get("original_type", "")
            
            field_class = get_type_class(field_type, field_size, field_native, field_array, field_align)
            field_obj = self._create_default_field(field_class, field_orig_type, field_array)
            
            if field_obj:
                temp_elements[field_name] = field_obj
                fields_dict[field_name] = field_obj
                
                if not field_array and isinstance(field_obj, ObjectData) and field_orig_type:
                    if not field_orig_type or field_orig_type in visited_types:
                        continue
                        
                    nested_type_info, nested_type_id = self.type_registry.find_type_by_name(field_orig_type)
                    
                    if not nested_type_info or not nested_type_id or nested_type_id == 0:
                        continue
                        
                    nested_objects.append((nested_type_info, nested_type_id))
                    
                    nested_temp = {}
                    self._analyze_instance_fields_for_nested_objects(
                        nested_temp,
                        nested_type_info,
                        nested_objects,
                        parent_id,
                        visited_types.copy()
                    )
        
        return fields_dict
    
    def _update_object_references(self, target_fields, temp_fields, main_instance_index, nested_objects):
        offset_start = main_instance_index - len(nested_objects)
        
        if offset_start < 0:
            return
        
        type_to_index = {
            nested_type_info.get("name", ""): i
            for i, (nested_type_info, _) in enumerate(nested_objects)
            if nested_type_info.get("name", "")
        }
        
        for field_name, field_data in temp_fields.items():
            if field_name not in target_fields:
                continue
                
            if isinstance(field_data, ObjectData) and field_data.value == 0:
                type_name = field_data.orig_type
                if type_name in type_to_index:
                    target_fields[field_name].value = offset_start + type_to_index[type_name]
                    
            elif isinstance(field_data, ArrayData) and field_data.element_class == ObjectData:
                array_data = target_fields[field_name]
                array_data.values = []
                
                for element in field_data.values:
                    if isinstance(element, ObjectData):
                        new_obj = ObjectData(element.value, element.orig_type)
                        
                        if element.value == 0 and element.orig_type:
                            type_name = element.orig_type
                            if type_name in type_to_index:
                                new_obj.value = offset_start + type_to_index[type_name]
                        array_data.values.append(new_obj)
                    else:
                        array_data.values.append(element)

    def _delete_prefab_for_object(self, prefab_id):
        if prefab_id == -1:
            print("  No prefab to delete (prefab_id is -1)")
            return False
            
        if prefab_id < 0 or not hasattr(self.scn, 'prefab_infos') or self.scn.is_pfb or self.scn.is_usr:
            print(f"  Cannot delete prefab: invalid conditions (prefab_id={prefab_id})")
            return False
        
        if prefab_id >= len(self.scn.prefab_infos):
            print(f"  Warning: Invalid prefab index {prefab_id}")
            return False
            
        prefab_to_delete = self.scn.prefab_infos[prefab_id]
        
        path_str = ""
        if hasattr(self.scn, 'get_prefab_string'):
            path_str = self.scn.get_prefab_string(prefab_to_delete)
        
        print(f"  Removing prefab {prefab_id} with path: {path_str}")
        
        if hasattr(self.scn, '_prefab_str_map'):
            if prefab_to_delete in self.scn._prefab_str_map:
                del self.scn._prefab_str_map[prefab_to_delete]
                print(f"  Removed string map entry for prefab {prefab_id}")
            else:
                print(f"  Warning: Prefab {prefab_id} not found in string map")
            
            for i, prefab in enumerate(self.scn.prefab_infos):
                if (i != prefab_id and 
                    prefab.string_offset == prefab_to_delete.string_offset and
                    prefab.string_offset != 0 and 
                    prefab in self.scn._prefab_str_map):
                    print(f"  Cleaning up duplicate prefab string reference at index {i}")
                    del self.scn._prefab_str_map[prefab]
        
        self.scn.prefab_infos.pop(prefab_id)
        print(f"  Prefab {prefab_id} removed from prefab_infos array")
        
        updated_count = 0
        for go in self.scn.gameobjects:
            if hasattr(go, 'prefab_id'):
                if go.prefab_id == prefab_id:
                    go.prefab_id = -1
                    updated_count += 1
                elif go.prefab_id > prefab_id:
                    go.prefab_id -= 1
                    updated_count += 1
        
        for folder in self.scn.folder_infos:
            if hasattr(folder, 'prefab_id'):
                if folder.prefab_id == prefab_id:
                    folder.prefab_id = -1
                    updated_count += 1
                elif folder.prefab_id > prefab_id:
                    folder.prefab_id -= 1
                    updated_count += 1
        
        print(f"  Updated {updated_count} objects referencing prefabs")
        
        if not self.scn.is_pfb and not self.scn.is_usr and hasattr(self.scn.header, 'prefab_count'):
            self.scn.header.prefab_count = len(self.scn.prefab_infos)
            print(f"  Updated header prefab count to {self.scn.header.prefab_count}")
        
        return True

    def _delete_gameobject_directly(self, gameobject):
        try:
            if gameobject not in self.scn.gameobjects:
                return False
                
            gameobject_id = gameobject.id
            
            if gameobject_id >= len(self.scn.object_table):
                return False
            
            if not self.scn.is_pfb and not self.scn.is_usr and hasattr(gameobject, 'prefab_id'):
                if gameobject.prefab_id >= 0:
                    _ = self._delete_prefab_for_object(gameobject.prefab_id)
            
            gameobject_refs_to_delete = self._collect_gameobject_hierarchy_by_reference(gameobject)
            
            if not gameobject_refs_to_delete:
                return False
                
            for go in reversed(gameobject_refs_to_delete):
                if go != gameobject and not self.scn.is_pfb and not self.scn.is_usr and hasattr(go, 'prefab_id'):
                    if go.prefab_id >= 0:
                        _ = self._delete_prefab_for_object(go.prefab_id)
                
                self._delete_all_components_of_gameobject(go)
                
                if go.id < len(self.scn.object_table):
                    go_instance_id = self.scn.object_table[go.id]
                    
                    if go_instance_id > 0:
                        print(f"  Deleting GameObject instance {go_instance_id} (object_id: {go.id})")
                        
                        instance_fields = self.scn.parsed_elements.get(go_instance_id, {})
                        nested_objects = self._find_nested_objects(instance_fields, go_instance_id)
                        nested_objects.add(go_instance_id)
                        
                        for instance_id in sorted(nested_objects, reverse=True):
                            self._remove_instance_references(instance_id)
                        
                        id_mapping = self._update_instance_references_after_deletion(go_instance_id, nested_objects)
                        
                        if id_mapping:
                            IdManager.instance().update_all_mappings(id_mapping, nested_objects)
                
                if go.id < len(self.scn.object_table):
                    self._remove_from_object_table(go.id)
                
                if go in self.scn.gameobjects:
                    self.scn.gameobjects.remove(go)
            
            return True
            
        except Exception as e:
            print(f"Error deleting GameObject: {str(e)}")
            import traceback
            traceback.print_exc()
            return False

    def _delete_object_prefab(self, object_ref):
        if self.scn.is_pfb or self.scn.is_usr or not hasattr(object_ref, 'prefab_id'):
            return False
            
        prefab_id = object_ref.prefab_id
        
        if prefab_id == -1:
            return False
            
        if prefab_id >= 0:
            prefab_deleted = self._delete_prefab_for_object(prefab_id)
            if prefab_deleted:
                print(f"  Deleted prefab {prefab_id} associated with object {object_ref.id}")
                return True
            else:
                print(f"  Failed to delete prefab {prefab_id} for object {object_ref.id}")
        else:
            print(f"  Object {object_ref.id} has unusual prefab_id: {prefab_id}")
            
        return False

