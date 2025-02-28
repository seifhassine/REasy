"""
RSZ file handler and viewer implementation.

This file contains:
- RszHandler: Main handler for RSZ file loading and management
- RszViewer: Qt widget for displaying and editing RSZ file contents
"""

from PySide6.QtCore import (Qt, Signal)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QMessageBox)
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


#########################################################

class RszHandler(BaseFileHandler):
    """Handler for SCN/PFB/USR files"""
    
    @staticmethod
    def needs_json_path() -> bool:
        return True
        
    def __init__(self):
        super().__init__()
        self.scn_file = None
        self.show_advanced = False
        self._viewer = None 

    def can_handle(data: bytes) -> bool:
        """Check if data appears to be an SCN, USR, or PFB file"""
        if len(data) < 4:
            return False
        scn_sig = b'SCN\x00'
        usr_sig = b'USR\x00'
        pfb_sig = b'PFB\x00'
        return data[:4] in [scn_sig, usr_sig, pfb_sig]
        
    def read(self, data: bytes):
        """Parse the file data"""
        self.init_type_registry()
        self.scn_file = ScnFile()
        self.scn_file.type_registry = self.type_registry
        self.scn_file.read(data)
        
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
    
    # Type mapping - defined once as class attribute rather than recreating it in methods
    TYPE_MAP = {
        "bool": BoolData,
        "s8": S8Data,
        "u8": U8Data,
        "s16": S16Data,
        "u16": U16Data,
        "s32": S32Data,
        "u32": U32Data,
        "s64": S64Data,
        "u64": U64Data,
        "f32": F32Data,
        "f64": F64Data,
        "string": StringData,
        "vec2": Vec2Data,
        "vec3": Vec3Data,
        "vec4": Vec4Data,
        "color": ColorData,
        "quaternion": QuaternionData,
        "guid": GuidData,
        "gameobjectref": GameObjectRefData,
        "range": RangeData,
        "rangei": RangeIData,
    }

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
            # Disconnect all signals first
            try:
                self.modified_changed.disconnect()
            except:
                pass
            
            # Remove model and clear tree
            if self.tree:
                self.tree.setModel(None)
            
            # Clear references but don't access widgets
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

            advanced_node["children"].extend([
                self._create_header_info(),
                self._create_gameobjects_info(),
            ])
            
            if not self.scn.is_pfb and not self.scn.is_usr:
                advanced_node["children"].append(self._create_folders_info())
            
            if self.scn.is_pfb:
                advanced_node["children"].append(self._create_gameobject_ref_infos())
                
            advanced_node["children"].extend([
                self._create_rsz_header_info(),
                self._create_object_table_info(),
                self._create_instance_infos(),
                self._create_userdata_infos()
            ])

        data_node = DataTreeBuilder.create_data_node(
            ScnTreeBuilder.NODES['DATA_BLOCK'], 
        )
        root_dict["children"].append(data_node)
        self._add_data_block(data_node)

        return root_dict

    def _create_header_info(self):
        """Create Header info section for self.scn.header"""
        children = []
        
        if self.scn.is_pfb:
            # PFB header fields
            header_fields = [
                ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip('\x00')),
                ("Info Count", lambda h: str(h.info_count)),
                ("Resource Count", lambda h: str(h.resource_count)),
                ("GameObjectRefInfo Count", lambda h: str(h.gameobject_ref_info_count)),
                ("UserData Count", lambda h: str(h.userdata_count)),
                ("Reserved", lambda h: str(h.reserved)),
                ("GameObjectRefInfo Tbl", lambda h: f"0x{h.gameobject_ref_info_tbl:X}"),
                ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                ("UserData Info Tbl", lambda h: f"0x{h.userdata_info_tbl:X}"),
                ("Data Offset", lambda h: f"0x{h.data_offset:X}")
            ]
        elif self.scn.is_usr:
            # USR header fields
            header_fields = [
                ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip('\x00')),
                ("Resource Count", lambda h: str(h.resource_count)),
                ("UserData Count", lambda h: str(h.userdata_count)),
                ("Info Count", lambda h: str(h.info_count)),
                ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                ("UserData Info Tbl", lambda h: f"0x{h.userdata_info_tbl:X}"),
                ("Data Offset", lambda h: f"0x{h.data_offset:X}"),
                ("Reserved", lambda h: str(h.reserved))
            ]
        else:
            # SCN header fields
            header_fields = [
                ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip('\x00')),
                ("Info Count", lambda h: str(h.info_count)),
                ("Resource Count", lambda h: str(h.resource_count)),
                ("Folder Count", lambda h: str(h.folder_count)),
                ("Prefab Count", lambda h: str(h.prefab_count)),
                ("UserData Count", lambda h: str(h.userdata_count)),
                ("Folder Tbl", lambda h: f"0x{h.folder_tbl:X}"),
                ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                ("Prefab Info Tbl", lambda h: f"0x{h.prefab_info_tbl:X}"),
                ("UserData Info Tbl", lambda h: f"0x{h.userdata_info_tbl:X}"),
                ("Data Offset", lambda h: f"0x{h.data_offset:X}")
            ]
            
        for title, getter in header_fields:
            children.append(
                DataTreeBuilder.create_data_node(title + ": " + getter(self.scn.header, ))
            )
        return DataTreeBuilder.create_data_node("Header", "", children=children)

    def _create_gameobject_ref_infos(self):
        """Create GameObjectRefInfo section for PFB files"""
        node = DataTreeBuilder.create_data_node("GameObjectRefInfos", f"{len(self.scn.gameobject_ref_infos)} items")
        
        for i, gori in enumerate(self.scn.gameobject_ref_infos):
            ref_node = DataTreeBuilder.create_data_node(f"GameObjectRefInfo[{i}]", "")
            ref_node["children"] = [
                DataTreeBuilder.create_data_node(f"Object ID: {gori.object_id}", ""),
                DataTreeBuilder.create_data_node(f"Property ID: {gori.property_id}", ""),
                DataTreeBuilder.create_data_node(f"Array Index: {gori.array_index}", ""),
                DataTreeBuilder.create_data_node(f"Target ID: {gori.target_id}", "")
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
            
            # Different handling for PFB vs SCN gameobjects
            if self.scn.is_pfb:
                children = [
                    DataTreeBuilder.create_data_node("ID: " + str(go.id), ""),
                    DataTreeBuilder.create_data_node("Parent ID: " + str(go.parent_id), ""),
                    DataTreeBuilder.create_data_node("Component Count: " + str(go.component_count), "")
                ]
            else:
                children = [
                    DataTreeBuilder.create_data_node("GUID: " + guid_le_to_str(go.guid), ""),
                    DataTreeBuilder.create_data_node("ID: " + str(go.id), ""),
                    DataTreeBuilder.create_data_node("Parent ID: " + str(go.parent_id), ""),
                    DataTreeBuilder.create_data_node("Component Count: " + str(go.component_count), ""),
                    DataTreeBuilder.create_data_node("Prefab ID: " + str(go.prefab_id), "")
                ]
            
            go_item = DataTreeBuilder.create_data_node(instance_name, "", children=children)
            node["children"].append(go_item)
        return node

    def _create_folders_info(self):
        node = DataTreeBuilder.create_data_node("Folder Infos", f"{len(self.scn.folder_infos)} items")
        for i, folder in enumerate(self.scn.folder_infos):
            if folder.id < len(self.scn.object_table):
                folder_name = self._get_folder_name(folder.id, f"FolderInfo[{i}]")
                folder_node = DataTreeBuilder.create_data_node(folder_name, "")
                folder_node["children"] = [
                    DataTreeBuilder.create_data_node(f"ID: {folder.id}", ""),
                    DataTreeBuilder.create_data_node(f"Instance ID: {self.scn.object_table[folder.id]}", ""),
                    DataTreeBuilder.create_data_node(f"Parent ID: {folder.parent_id}", "")
                ]
                node["children"].append(folder_node)
        return node

    def _create_object_table_info(self):
        node = DataTreeBuilder.create_data_node("Object Table", f"{len(self.scn.object_table)} items")
        for i, entry in enumerate(self.scn.object_table):
            node["children"].append(DataTreeBuilder.create_data_node(f"Entry {i}: {entry}", ""))
        return node

    def _create_instance_infos(self):
        node = DataTreeBuilder.create_data_node("Instance Infos", f"{len(self.scn.instance_infos)} items")
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
                    DataTreeBuilder.create_data_node(f"CRC: 0x{inst.crc:08X}", "")
                ]
                
                inst_node = DataTreeBuilder.create_data_node(friendly, "", children=children)
                node["children"].append(inst_node)
        return node

    def _create_userdata_infos(self):
        node = DataTreeBuilder.create_data_node(
            "RSZUserData Infos", 
            f"{len(self.scn.rsz_userdata_infos)} items"
        )
        for i, rui in enumerate(self.scn.rsz_userdata_infos):
            str_val = self.scn.get_rsz_userdata_string(rui) if rui.string_offset != 0 else ""
            node["children"].append(
                DataTreeBuilder.create_data_node(f"RSZUserDataInfo[{i}] : {str_val}", "")
            )
        return node

    def _add_data_block(self, parent_dict):
        if self.handler.scn_file.is_usr:
            # For USR files - add a single root object
            if len(self.scn.object_table) > 0:
                root_instance_id = self.scn.object_table[0]
                if root_instance_id in self.scn.parsed_elements:
                    inst_info = self.scn.instance_infos[root_instance_id]
                    type_info = self.type_registry.get_type_info(inst_info.type_id)
                    type_name = type_info["name"] if type_info and "name" in type_info else "UserData"
                    
                    root_dict = {
                        "data": [f"{type_name} (ID: {root_instance_id})", ""],
                        "children": []
                    }
                    fields = self.scn.parsed_elements[root_instance_id]
                    for field_name, field_data in fields.items():
                        root_dict["children"].append(
                            self._create_field_dict(field_name, field_data)
                        )
                    parent_dict["children"].append(root_dict)
            return

        # Original SCN file handling
        processed = set()
        nodes = {}
        gameobjects_folder = {
            "data": ["GameObjects", ""],
            "children": []
        }
        folders_folder = {
            "data": ["Folders", ""],
            "children": []
        }
        parent_dict["children"].append(gameobjects_folder)
        parent_dict["children"].append(folders_folder)

        # 1) Create GameObject nodes
        for go in self.scn.gameobjects:
            if go.id >= len(self.scn.object_table):
                continue
            go_instance_id = self.scn.object_table[go.id]
            if go_instance_id in processed:
                continue

            v0_name = self._get_instance_v0_name(go_instance_id)
            go_name = v0_name if v0_name else self.get_instance_name(go_instance_id)
            go_dict = {
                "data": [f"{go_name} (ID: {go_instance_id})", ""],
                "type": "gameobject", 
                "children": []
            }
            nodes[go.id] = (go_dict, go.parent_id)

            settings_node = {
                "data": ["Settings", ""],
                "children": []
            }
            go_dict["children"].append(settings_node)
            if go_instance_id in self.scn.parsed_elements:
                fields = self.scn.parsed_elements[go_instance_id]
                for field_name, field_data in fields.items():
                    settings_node["children"].append(
                        self._create_field_dict(field_name, field_data)
                    )

            processed.add(go_instance_id)

            # 1b) Components
            if go.component_count > 0:
                comp_node = {
                    "data": ["Components", ""],
                    "children": []
                }
                go_dict["children"].append(comp_node)
                for i in range(1, go.component_count + 1):
                    component_go_id = go.id + i
                    if component_go_id >= len(self.scn.object_table):
                        break
                    comp_instance_id = self.scn.object_table[component_go_id]
                    if comp_instance_id in processed:
                        continue

                    component_name = self._get_instance_v0_name(comp_instance_id) or self.get_instance_name(comp_instance_id)
                    comp_dict = {
                        "data": [f"{component_name} (ID: {comp_instance_id})", ""],
                        "children": []
                    }
                    comp_node["children"].append(comp_dict)

                    if comp_instance_id in self.scn.parsed_elements:
                        fields = self.scn.parsed_elements[comp_instance_id]
                        for f_name, f_data in fields.items():
                            comp_dict["children"].append(
                                self._create_field_dict(f_name, f_data)
                            )
                    processed.add(comp_instance_id)

            gameobjects_folder["children"].append(go_dict)

        # 2) Create Folder nodes
        for folder in self.scn.folder_infos:
            if folder.id >= len(self.scn.object_table):
                continue
            folder_instance_id = self.scn.object_table[folder.id]
            if folder_instance_id in processed:
                continue

            folder_name = self._get_instance_v0_name(folder_instance_id) or self.get_instance_name(folder_instance_id)
            folder_dict = {
                "data": [f"{folder_name} (ID: {folder_instance_id})", ""],
                "type": "folder", 
                "children": []
            }
            nodes[folder.id] = (folder_dict, folder.parent_id)

            settings_node = {
                "data": ["Settings", ""],
                "children": []
            }
            folder_dict["children"].append(settings_node)

            if folder_instance_id in self.scn.parsed_elements:
                fields = self.scn.parsed_elements[folder_instance_id]
                for field_name, field_data in fields.items():
                    settings_node["children"].append(
                        self._create_field_dict(field_name, field_data)
                    )
            processed.add(folder_instance_id)

            folders_folder["children"].append(folder_dict)

        # 3) Now reorganize nodes based on parent IDs
        for id_, (node_dict, parent_id) in nodes.items():
            if parent_id in nodes:
                parent_node_dict, _ = nodes[parent_id]
                children_node = None
                for ch in parent_node_dict["children"]:
                    if ch["data"][0] == "Children":
                        children_node = ch
                        break
                if not children_node:
                    children_node = {
                        "data": ["Children", ""],
                        "children": []
                    }
                    parent_node_dict["children"].append(children_node)

                def remove_from_folder(folder_data, target):
                    if target in folder_data["children"]:
                        folder_data["children"].remove(target)

                remove_from_folder(gameobjects_folder, node_dict)
                remove_from_folder(folders_folder, node_dict)

                # Attach to parent's Children
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
            original_type = f'{data_obj.orig_type}' if data_obj.orig_type else ""

            # Create child nodes
            for i, element in enumerate(data_obj.values):
                if isinstance(element, ObjectData):
                    ref_id = element.value
                    # Check if reference is to UserData
                    if (ref_id in self.scn._rsz_userdata_set):
                        display_value = self._get_userdata_display_value(ref_id)
                        obj_node = DataTreeBuilder.create_data_node(str(i) + f": {display_value}", "")
                        children.append(obj_node)
                    else:
                        # Normal object reference handling
                        type_name = self._get_type_name_for_instance(ref_id)
                        
                        obj_node = DataTreeBuilder.create_data_node(str(i) + f": ({type_name})", "")
                        if ref_id in self.scn.parsed_elements:
                            for fn, fd in self.scn.parsed_elements[ref_id].items():
                                obj_node["children"].append(self._create_field_dict(fn, fd))
                        children.append(obj_node)
                else:
                    # Pass proper type info for array elements
                    element_type = element.__class__.__name__
                    children.append(DataTreeBuilder.create_data_node(
                        str(i) + ": ",
                        "",
                        element_type, 
                        element
                    ))

            return DataTreeBuilder.create_data_node(
                f"{field_name}: {original_type}",
                "",
                "array",
                data_obj,
                children
            )

        elif isinstance(data_obj, ObjectData):
            ref_id = data_obj.value
            
            # Check if reference is to UserData first
            if ref_id in self.scn._rsz_userdata_set:
                display_value = self._get_userdata_display_value(ref_id)
                return DataTreeBuilder.create_data_node(
                    f"{field_name}: {display_value}",
                    "",
                    None,
                    None,
                    []
                )

            # Normal object reference handling
            type_name = self._get_type_name_for_instance(ref_id)
            
            children = []
            if ref_id in self.scn.parsed_elements:
                for fn, fd in self.scn.parsed_elements[ref_id].items():
                    children.append(self._create_field_dict(fn, fd))
            return DataTreeBuilder.create_data_node(
                f"{field_name}: ({type_name})",
                "",
                None,
                None,
                children
            )

        elif isinstance(data_obj, UserDataData):
            # Display UserData string directly for UserDataData type
            return DataTreeBuilder.create_data_node(
                f"{field_name}:",
                data_obj.value,
                data_obj.__class__.__name__,
                data_obj
            )
            
        else:
            return DataTreeBuilder.create_data_node(
                f"{field_name}:",
                "",
                data_obj.__class__.__name__,
                data_obj
            )

    def _get_type_name_for_instance(self, instance_id):
        """Get type name for an instance ID with optimized lookup"""
        # Invalid ID check
        if instance_id >= len(self.scn.instance_infos):
            return "Invalid ID"
        
        # Check cached type from recent addition
        if getattr(self, '_last_added_object', None) and \
           self._last_added_object.value == instance_id and \
           self._last_added_object.orig_type:
            return self._last_added_object.orig_type
        
        # Get from registry
        inst_info = self.scn.instance_infos[instance_id]
        type_info = self.type_registry.get_type_info(inst_info.type_id)
        
        # Return name from registry or fallback to hex ID
        return type_info.get("name", f"Type 0x{inst_info.type_id:08X}") if type_info else f"Type 0x{inst_info.type_id:08X}"

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
            if 'v0' in fields and isinstance(fields['v0'], StringData):
                return fields['v0'].value.rstrip('\x00')
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
        """Create a new element for an array based on type information"""

        element_class = getattr(array_data, 'element_class', None) if array_data else None
        
        if not self.type_registry or not array_data or not element_class:
            QMessageBox.warning(self, "Error", "Missing required data for array element creation")
            return None
            
        type_info, type_id = self.type_registry.find_type_by_name(element_type)
        if not type_info:
            QMessageBox.warning(self, "Error", f"Type not found in registry: {element_type}")
            return None
        
        new_element = (self._create_new_object_instance_for_array(type_id, type_info, element_type, array_data)  # If the elemnt we're creating is an object
                      if element_class == ObjectData 
                      else self._create_default_field(element_class, array_data.orig_type)) # If the element we're creating is a simple type
        
        if new_element:
            array_data.values.append(new_element)
            self.mark_modified()
            
            if direct_update and array_item:
                self._add_element_to_ui_direct(array_item, new_element, element_type)
            QMessageBox.information(self, "Element Added", f"New {element_type} element added successfully.")
            
        return new_element

    def _create_default_field(self, data_class, original_type, is_array=False):
        """Create default field value based on type"""
        try:
            if(is_array):
                return ArrayData([], data_class, original_type)
            
            if data_class == ObjectData:
                return ObjectData(0, original_type)
            if data_class == RawBytesData:
                raise ValueError("Unsupported field type: RawBytesData")
            
            return data_class()
        
        except Exception as e:
            print(f"Error creating field: {str(e)}")
            return None

    def _create_new_object_instance_for_array(self, type_id, type_info, element_type, array_data):
        """Create a new object instance for an array element"""
        parent_data = self._find_array_parent_data(array_data) 
        if not parent_data:
            return None
        parent_instance_id, parent_field_name = parent_data
        
        # Calculate insertion index for the new instance, considering parent location
        insertion_index = self._calculate_insertion_index(parent_instance_id, parent_field_name)
        
        # Create and initialize the instance
        new_instance = self._initialize_new_instance(type_id, type_info)
        if not new_instance or new_instance.type_id == 0:
            QMessageBox.warning(self, "Error", f"Failed to create valid instance with type {element_type}")
            return None
        
        # Initialize fields first to analyze potential nested objects
        temp_parsed_elements = {}
        nested_objects = []
        
        # Pre-analyze fields to identify nested objects
        self._analyze_instance_fields_for_nested_objects(
            temp_parsed_elements,
            type_info,
            nested_objects,
            insertion_index
        )
        
        # Insert nested objects first (before the main instance)
        valid_nested_objects = []
        for nested_type_info, nested_type_id in nested_objects:
            # Create the nested instance
            nested_instance = self._initialize_new_instance(nested_type_id, nested_type_info)
            
            # Skip invalid instances
            if not nested_instance or nested_instance.type_id == 0:
                continue
                
            # Insert it before target index
            self._insert_instance_and_update_references(insertion_index, nested_instance)
            
            # Initialize nested object fields
            nested_object_fields = {}
            self._initialize_fields_from_type_info(nested_object_fields, nested_type_info)
            self.scn.parsed_elements[insertion_index] = nested_object_fields
            
            # Track valid nested object for later reference updating
            valid_nested_objects.append((nested_type_info, nested_type_id))
            
            # This shifts the target index up by one
            insertion_index += 1
        
        # Now insert the main instance
        self._insert_instance_and_update_references(insertion_index, new_instance)
        
        # Initialize fields for the main instance
        main_instance_fields = {}
        self._initialize_fields_from_type_info(main_instance_fields, type_info)
        
        # Make sure all ObjectData fields point to the correct nested objects
        self._update_object_references(main_instance_fields, temp_parsed_elements, insertion_index, valid_nested_objects)
        
        # Store the parsed elements
        self.scn.parsed_elements[insertion_index] = main_instance_fields
        
        # Update hierarchy
        self._update_instance_hierarchy(insertion_index, parent_instance_id)
        
        # Create object reference and cache it
        obj_data = ObjectData(insertion_index, element_type)
        self._last_added_object = obj_data
        
        return obj_data

    def _analyze_instance_fields_for_nested_objects(self, temp_elements, type_info, nested_objects, parent_id, visited_types=None):
        if visited_types is None:
            visited_types = set()
        
        if not type_info or "fields" not in type_info:
            return {}
            
        # Preventing infinite recursion 
        type_name = type_info.get("name", "")
        if not type_name:  # Skip types without names
            return {}
            
        visited_types.add(type_name)
        
        # Initialize fields dictionary
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
                
                # Handle object reference fields that need their own object created
                if not field_array and isinstance(field_obj, ObjectData) and field_orig_type:
                    # Skip empty or already visited types
                    if not field_orig_type or field_orig_type in visited_types:
                        continue
                        
                    # Find type info for this field
                    nested_type_info, nested_type_id = self.type_registry.find_type_by_name(field_orig_type)
                    
                    # Skip if type info or ID is invalid
                    if not nested_type_info or not nested_type_id or nested_type_id == 0:
                        continue
                        
                    # Add to list of objects to create
                    nested_objects.append((nested_type_info, nested_type_id))
                    
                    # Recursively check if this nested object has its own nested objects
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
        """Update object references to point to correct nested objects"""
        # Calculate offset from temporary indexes to actual indexes
        # First nested object is at (main_instance_index - len(nested_objects))
        offset_start = main_instance_index - len(nested_objects)
        
        # Safety check
        if offset_start < 0:
            return
        
        # Update all object references
        for field_name, field_data in temp_fields.items():
            # Skip if field doesn't exist in target fields
            if field_name not in target_fields:
                continue
                
            if isinstance(field_data, ObjectData) and field_data.value == 0:
                # This is a reference to a nested object
                # Find the correct index for this nested object
                for i, (nested_type_info, _) in enumerate(nested_objects):
                    if nested_type_info.get("name", "") == field_data.orig_type:
                        # Update reference to actual instance index
                        target_fields[field_name].value = offset_start + i
                        break
            elif isinstance(field_data, ArrayData) and field_data.element_class == ObjectData:
                # Handle array of objects
                array_data = target_fields[field_name]
                array_data.values = []
                
                # Process each element in the array
                for element in field_data.values:
                    if isinstance(element, ObjectData):
                        # Create a new object reference with the adjusted index
                        new_obj = ObjectData(element.value, element.orig_type)
                        
                        # If it's a reference to a nested object (value=0), find the correct index
                        if element.value == 0 and element.orig_type:
                            for i, (nested_type_info, _) in enumerate(nested_objects):
                                if nested_type_info.get("name", "") == element.orig_type:
                                    new_obj.value = offset_start + i
                                    break
                        array_data.values.append(new_obj)
                    else:
                        # Non-object array elements
                        array_data.values.append(element)

    def _update_ui_after_element_add(self, direct_update, array_item, new_element, element_type):
        """Update UI after adding an array element"""
        success = False
        
        if direct_update and array_item:
            success = self._add_element_to_ui_direct(array_item, new_element, element_type)
            if not success:
                print("Direct UI update not possible - array node not found")
        
        QMessageBox.information(
            self, "Element Added", 
            f"New {element_type} element added successfully."
        )

    def _add_element_to_ui_direct(self, array_item, element, element_type_clean):
        """Add a new element directly to the tree using the provided array item"""
        model = getattr(self.tree, 'model', lambda: None)()
        if not model or not hasattr(array_item, 'raw'):
            return False
            
        # Get array data safely
        array_data = array_item.raw.get('obj') if isinstance(array_item.raw, dict) else None
        if not array_data or not hasattr(array_data, 'values'):
            return False
        
        # Get element index
        element_index = len(array_data.values) - 1
        
        # Create and add node
        node_data = (
            self._create_object_node_data(element.value, element_index, element) 
            if isinstance(element, ObjectData)
            else DataTreeBuilder.create_data_node(f"{element_index}: ", "", element.__class__.__name__, element)
        )
        
        model.addChild(array_item, node_data)
        
        # Expand the node
        array_index = model.getIndexFromItem(array_item)
        self.tree.expand(array_index)
        
        return True

    def _create_object_node_data(self, ref_id, index, element):
        """Helper to create a node for an object reference"""
        type_name = self._get_type_name_for_instance(ref_id)
        
        node_data = DataTreeBuilder.create_data_node(
            f"{index}: ({type_name})",
            "",
            None,
            element
        )
        
        # Add child field nodes if available
        if ref_id in self.scn.parsed_elements:
            fields = self.scn.parsed_elements[ref_id]
            for field_name, field_data in fields.items():
                node_data["children"].append(
                    self._create_field_dict(field_name, field_data)
                )
                
        return node_data

    def _calculate_insertion_index(self, parent_instance_id, parent_field_name):
        """Calculate the best insertion index for a new instance based on field positioning"""
        # Default to placing after parent
        insertion_index = parent_instance_id  # Default to right after the parent
        
        # Early exit for invalid parent
        if parent_instance_id >= len(self.scn.instance_infos):
            return insertion_index
            
        # Get parent type information
        parent_type_id = self.scn.instance_infos[parent_instance_id].type_id
        parent_type_info = self.type_registry.get_type_info(parent_type_id)
        
        # Early exit if type info missing
        if not parent_type_info or "fields" not in parent_type_info:
            return insertion_index
        
        # Get field position information
        field_indices = {field["name"]: idx for idx, field in enumerate(parent_type_info["fields"])}
        target_pos = field_indices.get(parent_field_name, -1)
        
        if target_pos < 0:
            return insertion_index
        
        # Find minimum reference value in fields after target position and all their nested objects
        min_later_ref = float('inf')
        
        # Track processed object ids to avoid infinite recursion
        processed_ids = set()
        
        # Recursively find all references in object fields
        def collect_references(instance_id, target_pos = -1):
            nonlocal min_later_ref, processed_ids
            
            # Avoid infinite recursion
            if instance_id in processed_ids:
                return
            processed_ids.add(instance_id)
            
            if instance_id not in self.scn.parsed_elements:
                return
                
            # Get instance type info
            if instance_id >= len(self.scn.instance_infos):
                return
            inst_type_id = self.scn.instance_infos[instance_id].type_id
            inst_type_info = self.type_registry.get_type_info(inst_type_id)
            if not inst_type_info or "fields" not in inst_type_info:
                return
                
            # Build field position mapping for this instance
            inst_field_indices = {field["name"]: idx for idx, field in enumerate(inst_type_info["fields"])}
            
            # Process all fields in this instance
            for field_name, field_data in self.scn.parsed_elements[instance_id].items():
                field_pos = inst_field_indices.get(field_name, -1)
                
                # For parent instance, only check fields after target position
                if instance_id == parent_instance_id and field_pos <= target_pos:
                    continue
                    
                # Check direct object reference
                if isinstance(field_data, ObjectData):
                    ref_id = field_data.value
                    if ref_id > 0:  # valid reference
                        min_later_ref = min(min_later_ref, ref_id)
                        # Recursively process this object's fields too
                        collect_references(ref_id)
                        
                # Check array elements
                elif isinstance(field_data, ArrayData):
                    for elem in field_data.values:
                        if isinstance(elem, ObjectData):
                            ref_id = elem.value
                            if ref_id > 0:  # valid reference
                                min_later_ref = min(min_later_ref, ref_id)
                                # Recursively process this object's fields too
                                collect_references(ref_id)
        
        # Start collection from parent instance
        collect_references(parent_instance_id, target_pos)
        
        # Update index if we found a valid reference
        if min_later_ref != float('inf'):
            insertion_index = min_later_ref
        
        return insertion_index

    def _find_array_parent_data(self, array_data):
        """Find parent instance and field for an array"""
        for instance_id, fields in self.scn.parsed_elements.items():
            for field_name, field_data in fields.items():
                if field_data is array_data:
                    return instance_id, field_name
        
        QMessageBox.warning(self, "Error", "Could not find array's parent instance")
        return None

    def _initialize_new_instance(self, type_id, type_info):
        if type_id == 0 or not type_info:
            return None
            
        new_instance = ScnInstanceInfo()
        
        new_instance.type_id = type_id
        
        # Need to clean this up later TODO
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
            raise("CRC is 0")
        return new_instance

    def _insert_instance_and_update_references(self, index, instance):
        """Insert instance at index and update all references to maintain consistency"""
        # Insert the instance info
        self.scn.instance_infos.insert(index, instance)
        
        # Update all references in object_table
        for i in range(len(self.scn.object_table)):
            if self.scn.object_table[i] >= index:
                self.scn.object_table[i] += 1
        
        # Update all ObjectData references in parsed_elements
        updated_elements = {}
        for instance_id, fields in self.scn.parsed_elements.items():
            updated_fields = {}
            new_id = instance_id + 1 if instance_id >= index else instance_id
            
            # Update references within fields
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData):
                    if field_data.value >= index:
                        field_data.value += 1
                elif isinstance(field_data, ArrayData):
                    for elem in field_data.values:
                        if isinstance(elem, ObjectData) and elem.value >= index:
                            elem.value += 1
                updated_fields[field_name] = field_data
            
            updated_elements[new_id] = updated_fields
        
        self.scn.parsed_elements = updated_elements
        
        # Update instance hierarchy
        updated_hierarchy = {}
        for instance_id, data in self.scn.instance_hierarchy.items():
            new_id = instance_id + 1 if instance_id >= index else instance_id
            
            # Update children references
            children = data["children"].copy()
            updated_children = []
            for child in children:
                updated_children.append(child + 1 if child >= index else child)
                
            # Update parent reference
            parent = data["parent"]
            if parent is not None and parent >= index:
                parent += 1
                
            updated_hierarchy[new_id] = {"children": updated_children, "parent": parent}
            
        self.scn.instance_hierarchy = updated_hierarchy
        
        # Update userdata references
        updated_userdata_set = set()
        for id_value in self.scn._rsz_userdata_set:
            updated_userdata_set.add(id_value + 1 if id_value >= index else id_value)
        self.scn._rsz_userdata_set = updated_userdata_set
        
        updated_userdata_dict = {}
        for id_value, rui in self.scn._rsz_userdata_dict.items():
            new_id = id_value + 1 if id_value >= index else id_value
            updated_userdata_dict[new_id] = rui
        self.scn._rsz_userdata_dict = updated_userdata_dict
        
        # Update RSZUserDataInfos
        for rui in self.scn.rsz_userdata_infos:
            if rui.instance_id >= index:
                rui.instance_id += 1
                
        # Update instance hashes
        updated_hashes = {}
        for id_value, hash_value in self.scn._instance_hashes.items():
            new_id = id_value + 1 if id_value >= index else id_value
            updated_hashes[new_id] = hash_value
        updated_hashes[index] = instance.crc 
        self.scn._instance_hashes = updated_hashes

    def _update_instance_hierarchy(self, instance_id, parent_id):
        """Update instance hierarchy with parent-child relationship"""
        self.scn.instance_hierarchy[instance_id] = {"children": [], "parent": parent_id}

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
