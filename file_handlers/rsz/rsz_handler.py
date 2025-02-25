"""
RSZ file handler and viewer implementation.

This file contains:
- RszHandler: Main handler for RSZ file loading and management
- RszViewer: Qt widget for displaying and editing RSZ file contents

"""

from PySide6.QtCore import (Qt, Signal)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QMessageBox)

from utils.hex_util import guid_le_to_str
from ..base_handler import BaseFileHandler
from file_handlers.pyside.value_widgets import *
from file_handlers.rsz.rsz_data_types import *
from .rsz_file import ScnFile
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modified = False
        self.scn = ScnFile()
        self.handler = None 
        self.type_registry = None
        self.dark_mode = False
        self.show_advanced = False
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
            self.value_changed.disconnect()
            self.modified_changed.disconnect()
            
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


    #########################################################
    #### Field creation and reference logic
    #########################################################
    def _create_field_dict(self, field_name, data_obj):
        """Creates dictionary node with added type info"""
        if isinstance(data_obj, ArrayData):
            children = []
            original_type = f'{data_obj.orig_type}' if data_obj.orig_type else ""

            # Create child nodes
            for i, element in enumerate(data_obj.values):
                if isinstance(element, ObjectData):
                    ref_id = element.value
                    # Check if reference is to UserData first
                    if ref_id in self.scn._rsz_userdata_set:
                        for rui in self.scn.rsz_userdata_infos:
                            if rui.instance_id == ref_id:
                                # For UserData, just show the string value
                                display_value = self.scn.get_rsz_userdata_string(rui)
                                obj_node = DataTreeBuilder.create_data_node(str(i) + f": {display_value}", "")
                                children.append(obj_node)
                                break
                    else:
                        # Normal object reference handling
                        type_name = "Unknown"
                        if ref_id in self.scn.parsed_elements and ref_id < len(self.scn.instance_infos):
                            inst_info = self.scn.instance_infos[ref_id]
                            type_info = self.type_registry.get_type_info(inst_info.type_id)
                            if type_info and "name" in type_info:
                                type_name = type_info["name"]
                        
                        obj_node = DataTreeBuilder.create_data_node(str(i) + f": ({type_name})", "")
                        if ref_id in self.scn.parsed_elements:
                            for fn, fd in self.scn.parsed_elements[ref_id].items():
                                obj_node["children"].append(self._create_field_dict(fn, fd))
                        children.append(obj_node)
                else:
                    # Pass proper type info for array elements
                    element_type = element.__class__.__name__
                    if element_type == "GameObjectRef":
                        element_type = "GameObjectRefData"
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
                None,
                children
            )

        elif isinstance(data_obj, ObjectData):
            ref_id = data_obj.value
            
            # Check if reference is to UserData first
            if ref_id in self.scn._rsz_userdata_set:
                for rui in self.scn.rsz_userdata_infos:
                    if rui.instance_id == ref_id:
                        # For UserData, just show the string value without type name
                        display_value = self.scn.get_rsz_userdata_string(rui)
                        return DataTreeBuilder.create_data_node(
                            f"{field_name}: {display_value}",
                            "",
                            None,
                            None,
                            []
                        )

            # Normal object reference handling
            type_name = "Unknown"
            if ref_id < len(self.scn.instance_infos):
                inst_info = self.scn.instance_infos[ref_id]
                type_info = self.type_registry.get_type_info(inst_info.type_id)
                if type_info and "name" in type_info:
                    type_name = type_info["name"]
            
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

    def _get_instance_v0_name(self, instance_id):
        """Get name from v0 field if available."""
        if instance_id in self.scn.parsed_elements:
            fields = self.scn.parsed_elements[instance_id]
            if 'v0' in fields and isinstance(fields['v0'], StringData):
                return fields['v0'].value.rstrip('\x00')
        return None

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

    def _add_info_section(self, parent_node, title, items, str_getter):
        node = {
            "data": [title, f"{len(items)} items"],
            "children": []
        }
        parent_node["children"].append(node)
        for i, item in enumerate(items):
            str_val = str_getter(item) if item.string_offset != 0 else ""
            node["children"].append({
                "data": [f"{title.split()[0]}[{i}]", str_val]
            })

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

    def _create_info_header(self, title, items_count):
        return DataTreeBuilder.create_data_node(
            title, f"{items_count} items"
        )

    def _add_field_to_parent(self, parent_dict, field_name, field_data):
        parent_dict["children"].append(
            self._create_field_dict(field_name, field_data)
        )
    
    def _create_instance_node(self, instance_id, name_prefix):
        name = self._get_instance_v0_name(instance_id) or self.get_instance_name(instance_id)
        return DataTreeBuilder.create_data_node(
            f"{name} (ID: {instance_id})", ""
        )

    def _format_value(self, value, is_hex=False):
        if is_hex:
            return f"0x{value:X}"
        return str(value)

