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
from .rsz_name_helper import RszViewerNameHelper
from .rsz_object_operations import RszObjectOperations


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
        if hasattr(self, 'app') and hasattr(self.app, 'settings'):
            self.show_advanced = self.app.settings.get("show_rsz_advanced", True)
        viewer.show_advanced = self.show_advanced
        colors = get_color_scheme(self.dark_mode)
        viewer.tree.setStyleSheet(get_tree_stylesheet(colors))
        viewer.name_helper = RszViewerNameHelper(viewer.scn, viewer.type_registry)
        viewer.array_operations = RszArrayOperations(viewer)
        viewer.object_operations = RszObjectOperations(viewer)
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
        self.name_helper = None
        self.object_operations = None

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
        self.name_helper = RszViewerNameHelper(self.scn, type_registry)
        self.object_operations = RszObjectOperations(self)
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
                    self._create_resources_info(),
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
            instance_name = self.name_helper.get_gameobject_name(instance_index, f"GameObject[{i}]")
            instance_name = self.name_helper.get_gameobject_name(instance_index, f"GameObject[{i}]")
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
                folder_name = self.name_helper.get_folder_name(folder.id, f"FolderInfo[{i}]")
                folder_name = self.name_helper.get_folder_name(folder.id, f"FolderInfo[{i}]")
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
            v0_name = self.name_helper.get_instance_v0_name(go_instance_id)
            go_name = v0_name if v0_name else self.name_helper.get_instance_name(go_instance_id)
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
                    component_name = self.name_helper.get_instance_v0_name(
                        comp_instance_id
                    ) or self.name_helper.get_instance_name(comp_instance_id)
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
            folder_name = self.name_helper.get_instance_v0_name(folder_instance_id) or self.name_helper.get_instance_name(
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

    def _create_field_dict(self, field_name, data_obj):
        """Creates dictionary node with added type info - refactored to reduce redundancy"""
        if isinstance(data_obj, ArrayData):
            children = []
            original_type = f"{data_obj.orig_type}" if data_obj.orig_type else ""
            for i, element in enumerate(data_obj.values):
                if isinstance(element, ObjectData):
                    ref_id = element.value
                    if ref_id in self.scn._rsz_userdata_set:
                        display_value = self.name_helper.get_userdata_display_value(ref_id)
                        obj_node = DataTreeBuilder.create_data_node(
                            str(i) + f": {display_value}", ""
                        )
                        children.append(obj_node)
                    else:
                        type_name = self.name_helper.get_type_name_for_instance(ref_id)
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
                display_value = self.name_helper.get_userdata_display_value(ref_id)
                return DataTreeBuilder.create_data_node(
                    f"{field_name}: {display_value}", "", None, None, []
                )
            type_name = self.name_helper.get_type_name_for_instance(ref_id)
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

    def _create_rsz_header_info(self):
        return DataTreeBuilder.create_data_node("RSZHeader", "")

    def _create_resources_info(self):
        """Create Resources info section for resource string references"""
        node = DataTreeBuilder.create_data_node(
            "Resources", f"{len(self.scn.resource_infos)} items"
        )
        
        for i, res in enumerate(self.scn.resource_infos):
            res_string = ""
            if hasattr(self.scn, 'get_resource_string'):
                try:
                    res_string = self.scn.get_resource_string(res) or ""
                except:
                    res_string = "[Error reading string]"
            
            res_node = DataTreeBuilder.create_data_node(f"{res_string}", "")
            res_node["type"] = "resource"
            res_node["resource_index"] = i
            
            node["children"].append(res_node)
        
        return node

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
        """Create a new component on the specified GameObject"""
        return self.object_operations.create_component_for_gameobject(
            gameobject_instance_id, component_type
        )

    def delete_component_from_gameobject(self, component_instance_id, owner_go_id=None):
        """Delete a component from its GameObject"""
        return self.object_operations.delete_component_from_gameobject(
            component_instance_id, owner_go_id
        )

    def create_gameobject(self, name, parent_id):
        """Create a new GameObject with the given name and parent"""
        return self.object_operations.create_gameobject(name, parent_id)

    def delete_gameobject(self, gameobject_id):
        """Delete a GameObject with the given ID"""
        return self.object_operations.delete_gameobject(gameobject_id)

    def delete_folder(self, folder_id):
        """Delete a folder with the given ID"""
        return self.object_operations.delete_folder(folder_id)

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
        """Clean up UserData related to an instance being removed"""
        rsz_indices_to_remove = []
        rsz_userdata_to_remove = []
        
        # First, identify which RSZ UserData entries need to be removed
        for i, rui in enumerate(self.scn.rsz_userdata_infos):
            if rui.instance_id == instance_id:
                rsz_indices_to_remove.append(i)
                rsz_userdata_to_remove.append(rui)
        
        # Remove from mapping tables
        if instance_id in self.scn._rsz_userdata_dict:
            del self.scn._rsz_userdata_dict[instance_id]
        if instance_id in self.scn._rsz_userdata_set:
            self.scn._rsz_userdata_set.remove(instance_id)
        
        # Remove string mappings for RSZ UserData being removed
        for rui in rsz_userdata_to_remove:
            if rui in self.scn._rsz_userdata_str_map:
                del self.scn._rsz_userdata_str_map[rui]
        
        # Only remove the userdata_infos that correspond to the RSZ UserData being removed
        # They should be in the same order, so we can use the same indices
        for idx in sorted(rsz_indices_to_remove, reverse=True):
            # Remove the RSZ UserData
            del self.scn.rsz_userdata_infos[idx]
            
            if idx < len(self.scn.userdata_infos):
                ui = self.scn.userdata_infos[idx]
                if ui in self.scn._userdata_str_map:
                    del self.scn._userdata_str_map[ui]
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

    def manage_resource(self, resource_index, new_path):
        """Update an existing resource path"""
        if resource_index < 0 or resource_index >= len(self.scn.resource_infos):
            return False
            
        resource = self.scn.resource_infos[resource_index]
        
        if not hasattr(self.scn, '_resource_str_map'):
            return False
            
        self.scn._resource_str_map[resource] = new_path
        self.mark_modified()
        
        return True
    
    def add_resource(self, path):
        """Add a new resource path"""
        if not path or not hasattr(self.scn, '_resource_str_map'):
            return -1
        
        from file_handlers.rsz.rsz_file import ScnResourceInfo
        
        new_res = ScnResourceInfo()
        new_res.string_offset = 0
        resource_index = len(self.scn.resource_infos)
        self.scn.resource_infos.append(new_res)
        self.scn._resource_str_map[new_res] = path
        self.mark_modified()
        
        return resource_index
    
    def delete_resource(self, resource_index):
        """Delete a resource path"""
        if resource_index < 0 or resource_index >= len(self.scn.resource_infos):
            return False
            
        resource = self.scn.resource_infos[resource_index]
        if hasattr(self.scn, '_resource_str_map') and resource in self.scn._resource_str_map:
            del self.scn._resource_str_map[resource]
        self.scn.resource_infos.pop(resource_index)
        self.mark_modified()
        
        return True
