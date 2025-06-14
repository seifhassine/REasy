"""
RSZ file handler and viewer implementation.

This file contains:
- RszHandler: Main handler for RSZ file loading and management
- RszViewer: Qt widget for displaying and editing RSZ file contents
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout

from utils.enum_manager import EnumManager
from utils.registry_manager import RegistryManager
from utils.hex_util import guid_le_to_str
from ..base_handler import BaseFileHandler
from file_handlers.pyside.value_widgets import *
from file_handlers.rsz.rsz_data_types import *
from file_handlers.rsz.pfb_16.pfb_structure import create_pfb16_resource
from .rsz_file import RszFile, RszInstanceInfo
from utils.type_registry import TypeRegistry
from ui.styles import get_color_scheme, get_tree_stylesheet
from ..pyside.tree_model import ScnTreeBuilder, DataTreeBuilder
from ..pyside.tree_widgets import AdvancedTreeView
from utils.id_manager import IdManager, EmbeddedIdManager
from .rsz_array_operations import RszArrayOperations
from .rsz_name_helper import RszViewerNameHelper
from .rsz_object_operations import RszObjectOperations
from .rsz_array_clipboard import RszArrayClipboard
from .rsz_gameobject_clipboard import RszGameObjectClipboard
from .rsz_component_clipboard import RszComponentClipboard


class RszHandler(BaseFileHandler):
    """Handler for SCN/PFB/USR files"""
    @staticmethod
    def needs_json_path() -> bool:
        return True

    def __init__(self):
        super().__init__()
        self.rsz_file = None
        self.show_advanced = True
        self._viewer = None
        self._game_version = "RE4"
        self.filepath = ""
        self.array_clipboard = None
        self.gameobject_clipboard = None
        self.component_clipboard = None
        self.type_registry = None
        self.auto_resource_management = False

    @property
    def game_version(self):
        return self._game_version

    @game_version.setter 
    def game_version(self, value):
        """Set game version on handler and all child objects"""
        self._game_version = value
        if self.rsz_file:
            self.rsz_file.game_version = value
            
        EnumManager.instance().game_version = value

    def can_handle(data: bytes) -> bool:
        """Check if data appears to be an SCN, USR, PFB or AIWAYP file"""
        if len(data) < 4:
            return False
        scn_sig = b"SCN\x00"
        usr_sig = b"USR\x00"
        pfb_sig = b"PFB\x00"
        return data[:4] in [scn_sig, usr_sig, pfb_sig]
        #aiwayp_sig = b"AIMP"
        #return data[:4] in [scn_sig, usr_sig, pfb_sig, aiwayp_sig]

    def init_type_registry(self):
        """Initialize type registry using shared registry manager"""
        if hasattr(self, 'app') and self.app:
            json_path = self.app.settings.get("rcol_json_path")
            if json_path:
                self.type_registry = RegistryManager.instance().get_registry(json_path)
                if self.type_registry and self.type_registry.registry.get("metadata", {}).get("complete", False):
                    self.auto_resource_management = True

    def read(self, data: bytes):
        """Parse the file data"""
        self.id_manager = IdManager.instance()
        self.init_type_registry()
        self.rsz_file = RszFile()
        self.rsz_file.type_registry = self.type_registry
        self.rsz_file.game_version = self._game_version 
        self.rsz_file.filepath = self.filepath
        print(f"Reading file with game version: {self._game_version}")
        self.rsz_file.read(data)
        self.rsz_file.auto_resource_management = self.auto_resource_management
        self.array_clipboard = RszArrayClipboard()
        self.gameobject_clipboard = RszGameObjectClipboard()
        self.component_clipboard = RszComponentClipboard()
        
    def create_viewer(self):
        """Create a new viewer instance"""
        viewer = RszViewer()
        viewer.scn = self.rsz_file
        viewer.handler = self
        viewer.type_registry = self.type_registry
        viewer.dark_mode = self.dark_mode
        viewer.game_version = self.game_version
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
    
    def set_advanced_mode(self, show_advanced):
        """Set whether to show advanced options"""
        self.show_advanced = show_advanced
        if self._viewer:
            self._viewer.show_advanced = show_advanced
    
    def set_game_version(self, version):
        """Set game version and update all related objects"""
        self.game_version = version  
        if self._viewer:
            self._viewer.game_version = version

    def rebuild(self) -> bytes:
        """Rebuild SCN file data with better error handling"""
        if not self.rsz_file:
            raise ValueError("No SCN file loaded")
        return self.rsz_file.build()
        
    def get_array_clipboard(self):
        """Get the array clipboard instance"""
        if not self.array_clipboard:
            self.array_clipboard = RszArrayClipboard()
        return self.array_clipboard
    
    def get_gameobject_clipboard(self):
        """Get the GameObject clipboard instance"""
        if not self.gameobject_clipboard:
            self.gameobject_clipboard = RszGameObjectClipboard.load_from_clipboard(self)
        return self.gameobject_clipboard
        
    def copy_array_element_to_clipboard(self, widget, element, array_type):
        """Copy an array element to clipboard through the handler"""
        return self.get_array_clipboard().copy_to_clipboard(widget, element, array_type)
    
    def copy_gameobject_to_clipboard(self, widget, gameobject_id):
        """Copy a GameObject to clipboard through the handler"""
        return self.get_gameobject_clipboard().copy_gameobject_to_clipboard(widget, gameobject_id)
        
    def paste_array_element_from_clipboard(self, widget, array_operations, array_data, array_item, embedded_context=None):
        """Paste an array element from clipboard through the handler"""
        return self.get_array_clipboard().paste_from_clipboard(widget, array_operations, array_data, array_item, embedded_context)
    
    def paste_gameobject_from_clipboard(self, viewer=None, parent_id=-1, new_name=None, clipboard_data=None):
        """Paste a GameObject from clipboard through the handler"""
        actual_viewer = viewer if viewer is not None else self
        
        return self.get_gameobject_clipboard().paste_gameobject_from_clipboard(
            actual_viewer, parent_id, new_name, clipboard_data
        )
        
    def get_clipboard_data(self, widget):
        """Get clipboard data through the handler"""
        return self.get_array_clipboard().get_clipboard_data(widget)
    
    def get_gameobject_clipboard_data(self, widget):
        """Get GameObject clipboard data through the handler"""
        return self.get_gameobject_clipboard().get_clipboard_data(widget)
        
    def is_clipboard_compatible(self, target_type, source_type):
        """Check if clipboard data is compatible with target type through the handler"""
        return self.get_array_clipboard().is_compatible(target_type, source_type)

    def has_gameobject_clipboard_data(self, widget):
        """Check if GameObject clipboard data exists without loading it"""
        return RszGameObjectClipboard.has_clipboard_data(widget)
    
    def copy_component_to_clipboard(self, widget, component_instance_id):
        """Copy a component to clipboard through the handler"""
        if not self.component_clipboard:
            self.component_clipboard = RszComponentClipboard()
        return self.component_clipboard.copy_component_to_clipboard(widget, component_instance_id)
        
    def paste_component_from_clipboard(self, widget, go_instance_id, clipboard_data=None):
        """Paste a component from clipboard to a GameObject through the handler"""
        if not self.component_clipboard:
            self.component_clipboard = RszComponentClipboard()
        return self.component_clipboard.paste_component_from_clipboard(widget, go_instance_id, clipboard_data)
        
    def get_component_clipboard_data(self, widget):
        """Get component clipboard data through the handler"""
        if not self.component_clipboard:
            self.component_clipboard = RszComponentClipboard()
        return self.component_clipboard.get_clipboard_data(widget)
        
    def has_component_clipboard_data(self, widget):
        """Check if component clipboard data exists without loading it"""
        return RszComponentClipboard.has_clipboard_data(widget)

class RszViewer(QWidget):
    INSTANCE_ID_ROLE = Qt.UserRole + 1
    ROW_HEIGHT = 24
    modified_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modified = False
        self.scn = RszFile()
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
        print("Populating tree")
        self.tree.setModelData(self._build_tree_data())
        self.embed_forms()

    def _build_tree_data(self):
        root_dict = DataTreeBuilder.create_data_node("SCN_File", "")
        root_dict["type"] = "root"                                          '''else "AIWAYP" if self.scn.is_aiwayp'''
        file_type = "USR" if self.scn.is_usr else "PFB" if self.scn.is_pfb                                              else "SCN"
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
            if self.scn.filepath.lower().endswith('.pfb.16'):
                # PFB.16 has a different header structure without userdata fields
                header_fields = [
                    ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip("\x00")),
                    ("Info Count", lambda h: str(h.info_count)),
                    ("Resource Count", lambda h: str(h.resource_count)),
                    ("GameObjectRefInfo Count", lambda h: str(h.gameobject_ref_info_count)),
                    ("GameObjectRefInfo Tbl", lambda h: f"0x{h.gameobject_ref_info_tbl:X}"),
                    ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                    ("Data Offset", lambda h: f"0x{h.data_offset:X}"),
                ]
            else:
                # Regular PFB format
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
            '''elif self.scn.is_aiwayp:
                header_fields = [
                    ("Signature", lambda h: h.signature.decode("ascii", errors="replace").strip("\x00")),
                    ("Version", lambda h: str(h.version)),
                    ("Info Count", lambda h: str(h.info_count)),
                    ("Resource Count", lambda h: str(h.resource_count)),
                    ("UserData Count", lambda h: str(h.userdata_count)),
                    ("Resource Info Tbl", lambda h: f"0x{h.resource_info_tbl:X}"),
                    ("UserData Info Tbl", lambda h: f"0x{h.userdata_info_tbl:X}"),
                    ("Data Offset", lambda h: f"0x{h.data_offset:X}"),
                ]
            '''
        elif self.scn.filepath.lower().endswith('.18'):
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
                ("Data Offset", lambda h: f"0x{h.data_offset:X}"),
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

    def _create_rsz_header_info(self):
        return DataTreeBuilder.create_data_node("RSZHeader", "")

    def _create_resources_info(self):
        """Create Resources info section for resource string references"""
        node = DataTreeBuilder.create_data_node(
            "Resources", f"{len(self.scn.resource_infos)} items"
        )
        
        if hasattr(self.scn, 'is_pfb16') and self.scn.is_pfb16 and hasattr(self.scn, '_pfb16_direct_strings'):
            direct_strings = self.scn._pfb16_direct_strings
            
            for i, res in enumerate(self.scn.resource_infos):
                res_string = ""
                
                if i < len(direct_strings):
                    res_string = direct_strings[i]
                    #print(f"PFB.16 resource {i}: Using direct string: '{res_string}'")
                else:
                    res_string = self.scn.get_resource_string(res) or ""
                    #print(f"PFB.16 resource {i}: Using get_resource_string: '{res_string}'")
                    
                res_node = DataTreeBuilder.create_data_node(f"{res_string}", "")
                res_node["type"] = "resource"
                res_node["resource_index"] = i
                
                node["children"].append(res_node)
                
        else:
            for i, res in enumerate(self.scn.resource_infos):
                try:
                    res_string = self.scn.get_resource_string(res) or ""
                except Exception as e:
                    print(f"Error reading resource {i}: {e}")
                    res_string = "[Error reading]"
                    
                res_node = DataTreeBuilder.create_data_node(f"{res_string}", "")
                res_node["type"] = "resource"
                res_node["resource_index"] = i
                
                node["children"].append(res_node)
        
        return node

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
            '''if self.scn.is_aiwayp and hasattr(self.scn, 'aiwayp_root_gameobject'):
                # For AIWAYP files, use the name and hash strings parsed from the header
                go_name = getattr(self.scn.header, 'gameobject_name', "AIWAYP_Object") 
                go_hash = getattr(self.scn.header, 'gameobject_type', "Unknown_Hash")
                
                # Make sure values are non-empty strings
                if not go_name or go_name.strip() == "":
                    go_name = "AIWAYP_Object"
                if not go_hash or go_hash.strip() == "":
                    go_hash = "Unknown_Hash"
                    
                print(f"AIWAYP GameObject - name: '{go_name}', hash: '{go_hash}'")
                instance_name = f"{go_name}"
                
                # Display the unknown values 
                unknown_value1 = getattr(self.scn.header, 'unknown_value1', 0)
                unknown_value2 = getattr(self.scn.header, 'unknown_value2', 0)
                
                children = [
                    DataTreeBuilder.create_data_node("GUID: " + guid_le_to_str(go.guid), ""),
                    DataTreeBuilder.create_data_node("Unknown Value 1: " + str(unknown_value1), ""),
                    DataTreeBuilder.create_data_node("Unknown Value 2: " + str(unknown_value2), ""),
                    DataTreeBuilder.create_data_node("External GameObject (not in object table)", ""),
                    DataTreeBuilder.create_data_node("Name: " + go_name, ""),
                    DataTreeBuilder.create_data_node("Unknown Value 3 (MD5 Hash?): " + go_hash, ""),  # Changed from go_type to go_hash
                ]
            el'''
            if self.scn.is_pfb:
                # Normal PFB GameObject
                instance_index = None
                if go.id < len(self.scn.object_table):
                    instance_index = self.scn.object_table[go.id]
                instance_name = self.name_helper.get_gameobject_name(instance_index, f"GameObject[{i}]")
                children = [
                    DataTreeBuilder.create_data_node("ID: " + str(go.id), ""),
                    DataTreeBuilder.create_data_node("Parent ID: " + str(go.parent_id), ""),
                    DataTreeBuilder.create_data_node(
                        "Component Count: " + str(go.component_count), ""
                    ),
                ]
            else:
                # Normal SCN GameObject
                instance_index = None
                if go.id < len(self.scn.object_table):
                    instance_index = self.scn.object_table[go.id]
                instance_name = self.name_helper.get_gameobject_name(instance_index, f"GameObject[{i}]")
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
        if self.scn.filepath.lower().endswith('.18'):
            return node
        
        is_scn19 = self.scn.filepath.lower().endswith('.19')
        
        for i, rui in enumerate(self.scn.rsz_userdata_infos):
            if is_scn19:
                # SCN.19 format - use existing user data string or build a descriptive one
                str_val = self.scn.get_rsz_userdata_string(rui)
                if not str_val:
                    data_size = getattr(rui, 'data_size', 0)
                    type_id = getattr(rui, 'type_id', 0)
                    str_val = f"Binary UserData (Type: 0x{type_id:08X}, Size: {data_size} bytes)"
                
                node["children"].append(
                    DataTreeBuilder.create_data_node(f"RSZUserDataInfo[{i}] : {str_val}", "")
                )
            else:
                # Standard format - check for string offset attribute
                str_val = ""
                if hasattr(rui, 'string_offset') and rui.string_offset != 0:
                    str_val = self.scn.get_rsz_userdata_string(rui)
                
                node["children"].append(
                    DataTreeBuilder.create_data_node(f"RSZUserDataInfo[{i}] : {str_val}", "")
                )
        
        return node

    def _add_data_block(self, parent_dict):
        if self.handler.rsz_file.is_usr:
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
        gameobjects_folder = {"data": ["Game Objects", ""], "children": []}
        folders_folder = {"data": ["Folders", ""], "children": []}
        parent_dict["children"].append(gameobjects_folder)
        parent_dict["children"].append(folders_folder)
        
        # Special handling for AIWAYP GameObject which is not in the object table
        '''if self.scn.is_aiwayp and hasattr(self.scn, 'aiwayp_root_gameobject'):
            go = self.scn.aiwayp_root_gameobject
            if go.id == -1:  # Special ID we set to indicate external GameObject
                go_name = getattr(self.scn.header, 'gameobject_name', "AIWAYP_Object")
                go_hash = getattr(self.scn.header, 'gameobject_type', "Unknown_Hash") # Changed from gameobject_type
                
                # Make sure values are non-empty strings
                if not go_name or go_name.strip() == "":
                    go_name = "AIWAYP_Object"
                if not go_hash or go_hash.strip() == "":
                    go_hash = "Unknown_Hash"
                    
                go_dict = {
                    "data": [f"{go_name} ({go_hash})", ""], # Changed from go_type to go_hash
                    "type": "gameobject",
                    "instance_id": -1,  # Special ID
                    "reasy_id": -1,     # Special ID  
                    "children": [],
                }
                settings_node = {"data": ["Settings", ""], "children": []}
                go_dict["children"].append(settings_node)
                
                # Add the Components node
                comp_node = {"data": ["Components", ""], "children": []}
                go_dict["children"].append(comp_node)
                
                # All instances in the object table are considered components of this GameObject
                for i, instance_id in enumerate(self.scn.object_table):
                    if instance_id <= 0 or instance_id in processed:
                        continue
                    reasy_id = self.handler.id_manager.register_instance(instance_id)
                    component_name = self.name_helper.get_instance_name(instance_id)
                    comp_dict = {
                        "data": [f"{component_name} (ID: {instance_id})", ""],
                        "instance_id": instance_id,
                        "reasy_id": reasy_id,
                        "children": [],
                    }
                    comp_node["children"].append(comp_dict)
                    if instance_id in self.scn.parsed_elements:
                        fields = self.scn.parsed_elements[instance_id]
                        for f_name, f_data in fields.items():
                            comp_dict["children"].append(self._create_field_dict(f_name, f_data))
                    processed.add(instance_id)
                    
                gameobjects_folder["children"].append(go_dict)
        '''
        for go in self.scn.gameobjects:
            # Skip AIWAYP root GameObject which we already processed
            '''if self.scn.is_aiwayp and hasattr(self.scn, 'aiwayp_root_gameobject') and go is self.scn.aiwayp_root_gameobject:
                continue
             '''   
            if go.id >= len(self.scn.object_table):
                continue
            go_instance_id = self.scn.object_table[go.id]
            if go_instance_id in processed:
                continue
            reasy_id = self.handler.id_manager.register_instance(go_instance_id)
            name = self.name_helper.get_instance_first_field_name(go_instance_id)
            go_name = name if name else self.name_helper.get_instance_name(go_instance_id)
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
            if not self.scn.is_pfb:
                guid_data = GuidData(guid_le_to_str(go.guid), go.guid)
                guid_data.gameobject = go
                guid_field = self._create_field_dict("GUID", guid_data)
                settings_node["children"].insert(0, guid_field)
            
            if go_instance_id in self.scn.parsed_elements:
                fields = self.scn.parsed_elements[go_instance_id]
                for field_name, field_data in fields.items():
                    field_node = self._create_field_dict(field_name, field_data)
                    if len(settings_node["children"]) == 1:
                        field_data.is_gameobject_or_folder_name = go_dict
                    settings_node["children"].append(field_node)
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
                    reasy_id = self.handler.id_manager.register_instance(comp_instance_id)
                    component_name = self.name_helper.get_instance_name(comp_instance_id)
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
            reasy_id = self.handler.id_manager.register_instance(folder_instance_id)
            folder_name = self.name_helper.get_instance_first_field_name(folder_instance_id) or self.name_helper.get_instance_name(
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
                first_field = True
                for field_name, field_data in fields.items():
                    if first_field:
                        field_data.is_gameobject_or_folder_name = folder_dict
                        first_field = False
                
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
        
        print("added data block")

    def _create_field_dict(self, field_name, data_obj, embedded_context=None):
        """Create a dictionary representation of a field for the tree view"""
        domain_id = None
        is_embedded = embedded_context is not None
        if isinstance(data_obj, StructData):
            original_type = f"{data_obj.orig_type}" if hasattr(data_obj, 'orig_type') and data_obj.orig_type else ""
            
            struct_node = DataTreeBuilder.create_data_node(
                f"{field_name}: {original_type}", "", "struct", data_obj
            )
            
            struct_type_info = None
            field_definitions = {}
            if self.type_registry and original_type:
                struct_type_info, _ = self.type_registry.find_type_by_name(original_type)
                if struct_type_info and "fields" in struct_type_info:
                    field_definitions = {
                        field_def["name"]: field_def 
                        for field_def in struct_type_info["fields"] 
                        if "name" in field_def
                    }
            
            for i, struct_value in enumerate(data_obj.values):
                if not isinstance(struct_value, dict):
                    continue
                    
                instance_label = f"{i}: {original_type}"
                
                if "name" in struct_value and hasattr(struct_value["name"], 'value') and struct_value["name"].value:
                    instance_label = f"{i}: {struct_value['name'].value}"
                
                struct_instance_node = DataTreeBuilder.create_data_node(
                    instance_label, "", "struct_instance", None
                )
                
                for field_key, field_value in struct_value.items():
                    if field_key in field_definitions:
                        field_def = field_definitions[field_key]
                        display_name = field_def["name"]
                        display_type = field_def["type"]
                        
                        field_node = self._create_field_dict(display_name, field_value, embedded_context)
                        field_node["data"][0] = f"{display_name} ({display_type})"
                    else:
                        field_node = self._create_field_dict(field_key, field_value, embedded_context)
                        
                    struct_instance_node["children"].append(field_node)
                
                struct_node["children"].append(struct_instance_node)
            
            return struct_node
            
        elif isinstance(data_obj, ArrayData):
            children = []
            original_type = f"{data_obj.orig_type}" if data_obj.orig_type else ""
            
            if is_embedded:
                if not hasattr(data_obj, '_owning_context') or data_obj._owning_context is None:
                    data_obj._owning_context = embedded_context
                
                if not hasattr(data_obj, '_owning_instance_id') or data_obj._owning_instance_id is None:
                    if hasattr(embedded_context, 'embedded_object_table') and embedded_context.embedded_object_table:
                        data_obj._owning_instance_id = embedded_context.embedded_object_table[0]
            
            for i, element in enumerate(data_obj.values):
                if isinstance(element, (ArrayData, ObjectData, UserDataData)):
                    if not hasattr(element, '_container_array') or element._container_array is None:
                        element._container_array = data_obj
                    if not hasattr(element, '_container_index'):
                        element._container_index = i
                    if is_embedded:
                        if not hasattr(element, '_container_context') or element._container_context is None:
                            element._container_context = embedded_context
                
                if isinstance(element, ObjectData) or isinstance(element, UserDataData):
                    child_node = self._handle_reference_in_array(i, element, embedded_context, domain_id)
                    if child_node:
                        children.append(child_node)
                else:
                    # Non-object elements are handled the same for both contexts
                    element_type = element.__class__.__name__
                    children.append(
                        DataTreeBuilder.create_data_node(str(i) + ": ", "", element_type, element)
                    )
                    
            return DataTreeBuilder.create_data_node(
                f"{field_name}: {original_type}", "", "array", data_obj, children
            )
            
        elif isinstance(data_obj, ObjectData) or isinstance(data_obj, UserDataData):
            return self._handle_object_reference(field_name, data_obj, embedded_context, domain_id)
        else:
            # All other data types are handled the same way regardless of context
            return DataTreeBuilder.create_data_node(
                f"{field_name}:", "", data_obj.__class__.__name__, data_obj
            )

    def _handle_reference_in_array(self, index, element, embedded_context, domain_id):
        """Handle object or userdata reference in an array"""
        if not (isinstance(element, ObjectData) or isinstance(element, UserDataData)):
            raise ValueError("Invalid data object type for reference handling")
        ref_id = element.value
        return self._handle_reference(str(index), ref_id, embedded_context, element)
    
    def _handle_object_reference(self, field_name, data_obj, embedded_context, domain_id):
        """Handle object or userdata reference"""
        if not (isinstance(data_obj, ObjectData) or isinstance(data_obj, UserDataData)):
            raise ValueError("Invalid data object type for reference handling")
        ref_id = data_obj.value
        return self._handle_reference(field_name, ref_id, embedded_context, data_obj)
    
    def _handle_reference(self, label, ref_id, embedded_context, data_obj):
        """Unified reference handling for both array elements and direct object references"""
        if embedded_context:
            return self._handle_embedded_reference(label, ref_id, embedded_context, data_obj)
        else:
            return self._handle_standard_reference(label, ref_id)

    def _handle_embedded_reference(self, field_name, ref_id, embedded_context, data_obj):
        """Handle reference in embedded context"""
        # For embedded fields, look in embedded_instances
        if hasattr(embedded_context, 'embedded_instances') and ref_id in embedded_context.embedded_instances:
            return self._create_embedded_instance_node(field_name, ref_id, embedded_context)
            
        # Handle reference to embedded UserData
        elif hasattr(embedded_context, 'embedded_userdata_infos'):
            for embedded_rui in embedded_context.embedded_userdata_infos:
                if embedded_rui.instance_id == ref_id:
                    if hasattr(embedded_rui, 'embedded_instances') and embedded_rui.embedded_instances:
                        # Set parent reference to track modification chain
                        embedded_rui.parent_userdata_rui = embedded_context
                        return self._create_direct_embedded_usr_node(field_name, embedded_rui)
                    return DataTreeBuilder.create_data_node(
                        f"{field_name}: Embedded UserData (ID: {ref_id})", "", None, None
                    )
        
        # Generic reference representation
        type_label = "UserData" if isinstance(data_obj, UserDataData) else "Object"
        return DataTreeBuilder.create_data_node(
            f"{field_name}: ({type_label} ID: {ref_id})", "", None, None
        )

    def _create_embedded_instance_node(self, field_name, ref_id, embedded_context):
        """Create node for embedded instance reference"""
        instance_data = embedded_context.embedded_instances[ref_id]
        type_name = self._get_embedded_type_name(ref_id, embedded_context)
        
        # Special handling for nested embedded userdata
        if isinstance(instance_data, dict) and "embedded_rsz" in instance_data:
            # This is a reference to another embedded RSZ structure
            nested_rui = instance_data["embedded_rsz"]
            if nested_rui and hasattr(nested_rui, 'embedded_instances') and nested_rui.embedded_instances:
                # Make sure we preserve the outer domain ID for reference
                if not hasattr(nested_rui, 'parent_domain_id'):
                    nested_rui.parent_domain_id = getattr(embedded_context, 'instance_id', None)
                return self._create_direct_embedded_usr_node(field_name, nested_rui)
        
        obj_node = DataTreeBuilder.create_data_node(f"{field_name}: ({type_name})", "")
        
        # Add all fields from the referenced instance
        if isinstance(instance_data, dict) and "embedded_rsz" not in instance_data:
            for sub_field_name, sub_field_data in instance_data.items():
                field_node = self._create_field_dict(sub_field_name, sub_field_data, embedded_context)
                obj_node["children"].append(field_node)
        
        return obj_node
    
    def _handle_standard_reference(self, field_name, ref_id):
        """Handle reference in standard (non-embedded) context"""
        scn = self.scn
        
        # UserData reference
        if ref_id in scn._rsz_userdata_set:
            rui = scn._rsz_userdata_dict.get(ref_id)
            # Check if this is embedded RSZ data with parsed instances
            if rui and hasattr(rui, 'embedded_instances') and rui.embedded_instances:
                # Direct representation: display as root .user object
                return self._create_direct_embedded_usr_node(field_name, rui)
            else:
                display_value = self.name_helper.get_userdata_display_value(ref_id)
                return DataTreeBuilder.create_data_node(
                    f"{field_name}: {display_value}", "", None, None, []
                )
        
        # Object reference
        type_name = self.name_helper.get_type_name_for_instance(ref_id)
        children = []
        if ref_id in scn.parsed_elements:
            for fn, fd in scn.parsed_elements[ref_id].items():
                children.append(self._create_field_dict(fn, fd))
        return DataTreeBuilder.create_data_node(
            f"{field_name}: ({type_name})", "", None, None, children
        )
            
    def _create_direct_embedded_usr_node(self, field_name, rui):
        """Display embedded RSZ structure as a direct .user object with proper relationships."""
        # Get domain ID and prepare structure
        domain_id = getattr(rui, 'instance_id', 0)
        
        # Create an embedded ID manager for this structure
        if not hasattr(rui, 'id_manager'):
            rui.id_manager = EmbeddedIdManager(domain_id)
        
        # Clear modified flag so we can track new changes
        rui.modified = False
        
        # Get root instance and create the root node
        root_instance_id = self._get_root_embedded_instance_id(rui)
        type_name = self._get_embedded_type_name(root_instance_id, rui) if root_instance_id else "UserData"

        root_node = DataTreeBuilder.create_data_node(f"{field_name}: {type_name} <span style='color: #FFFF00;'>(embedded RSZ)</span>", "")
        
        # Build hierarchy if needed
        hierarchy = getattr(rui, 'embedded_instance_hierarchy', None)
        if not hierarchy and hasattr(rui, 'embedded_instances'):
            hierarchy = self._build_embedded_hierarchy(rui.embedded_instances)
        
        # Skip further processing if embedded_instances doesn't exist or is empty
        if not hasattr(rui, 'embedded_instances') or not rui.embedded_instances:
            return root_node
        
        # Create all instance nodes with their fields
        nodes = self._create_embedded_instance_nodes(rui, domain_id)
        
        # Add root instance fields if it exists
        if root_instance_id and root_instance_id != 0 and root_instance_id in rui.embedded_instances:
            self._add_root_instance_fields(root_node, root_instance_id, rui, domain_id)
        
        # Build the hierarchy relationships between nodes
        self._build_embedded_node_hierarchy(nodes, hierarchy, root_node, root_instance_id)
        
        return root_node

    def _create_embedded_instance_nodes(self, rui, domain_id):
        """Create nodes for all instances in an embedded RSZ structure"""
        nodes = {}
        if not hasattr(rui, 'embedded_instances'):
            return nodes
            
        for instance_id, instance_data in rui.embedded_instances.items():
            # Skip null instance and non-dict instances
            if instance_id == 0 or not isinstance(instance_data, dict) or "embedded_rsz" in instance_data:
                continue
                
            # Use the embedded ID manager to register this instance
            reasy_id = rui.id_manager.register_instance(instance_id)
            
            # Get type name for this instance
            inst_type_name = self._get_embedded_type_name(instance_id, rui)
            
            # Create the node
            node_dict = {
                "data": [f"{inst_type_name} (ID: {instance_id})", ""],
                "instance_id": instance_id,
                "domain_id": domain_id,
                "embedded": True,
                "reasy_id": reasy_id,
                "children": [],
            }
            
            # Add fields to the node
            for field_name, field_data in instance_data.items():
                field_node = self._create_field_dict(field_name, field_data, rui)
                node_dict["children"].append(field_node)
            
            nodes[instance_id] = node_dict
            
        return nodes
    def _get_root_embedded_instance_id(self, rui):
        """Get the root instance ID from an embedded RSZ structure"""
        if hasattr(rui, 'embedded_object_table') and rui.embedded_object_table:
            return rui.embedded_object_table[0] if len(rui.embedded_object_table) > 0 else None
        return None
    
    def _add_root_instance_fields(self, root_node, root_instance_id, rui, domain_id):
        """Add fields from the root instance to the root node"""
        root_data = rui.embedded_instances[root_instance_id]
        if isinstance(root_data, dict):
            # Register the root instance with the embedded ID manager
            reasy_root_id = rui.id_manager.register_instance(root_instance_id)
            
            # Update root node with IDs
            root_node["embedded"] = True
            root_node["domain_id"] = domain_id
            root_node["instance_id"] = root_instance_id
            root_node["reasy_id"] = reasy_root_id
            
            # Add fields
            for field_name, field_data in root_data.items():
                field_node = self._create_field_dict(field_name, field_data, rui)
                root_node["children"].append(field_node)

    def _build_embedded_node_hierarchy(self, nodes, hierarchy, root_node, root_instance_id):
        """Build parent-child relationships between nodes"""
        # Track which nodes have been attached to the tree
        attached = set()
        if root_instance_id:
            attached.add(root_instance_id)
            
        # Phase 1: Use explicit hierarchy information
        if hierarchy:
            for instance_id, node_dict in nodes.items():
                if instance_id == root_instance_id:
                    continue
                    
                parent_id = hierarchy.get(instance_id, {}).get("parent")
                if parent_id is not None and parent_id in nodes:
                    nodes[parent_id]["children"].append(node_dict)
                    attached.add(instance_id)
        
        # Phase 2: Attach any remaining nodes to root
        for instance_id, node_dict in nodes.items():
            if instance_id != root_instance_id and instance_id not in attached:
                root_node["children"].append(node_dict)
    
    def _get_embedded_type_name(self, instance_id, rui):
        """Get the type name for an instance in embedded RSZ"""
        type_name = f"Instance[{instance_id}]"
        
        if hasattr(rui, 'embedded_instance_infos') and instance_id < len(rui.embedded_instance_infos):
            inst_info = rui.embedded_instance_infos[instance_id]
            if self.type_registry:
                type_info = self.type_registry.get_type_info(inst_info.type_id)
                if type_info and "name" in type_info:
                    type_name = type_info["name"]
        
        return type_name

    def _build_embedded_hierarchy(self, embedded_instances):
        """Build hierarchy structure from object references in embedded instances"""
        # Create hierarchy structure with default values
        hierarchy = {
            instance_id: {"children": [], "parent": None} 
            for instance_id in embedded_instances 
            if isinstance(embedded_instances[instance_id], dict)
        }
        
        # Process all fields to find parent-child relationships
        for instance_id, fields in embedded_instances.items():
            if not isinstance(fields, dict):
                continue
            
            # Process all fields for potential references    
            for field_data in fields.values():
                self._process_reference_for_hierarchy(instance_id, field_data, hierarchy)
        
        # Consolidate multiple root nodes if needed
        self._consolidate_root_nodes(hierarchy)
        
        return hierarchy
    
    def _consolidate_root_nodes(self, hierarchy):
        """Find main root node and make other roots its children"""
        # Identify root candidates (nodes with no parent but with children)
        root_candidates = [
            id for id, data in hierarchy.items() 
            if data["parent"] is None and data["children"]
        ]
        
        if len(root_candidates) <= 1:
            return  # No consolidation needed
            
        # Find root with most total children
        main_root = max(root_candidates, key=lambda x: self._count_all_children(x, hierarchy))
        
        # Make other roots children of the main root
        for candidate in root_candidates:
            if candidate != main_root and hierarchy[candidate]["parent"] is None:
                hierarchy[main_root]["children"].append(candidate)
                hierarchy[candidate]["parent"] = main_root

    def _process_reference_for_hierarchy(self, instance_id, field_data, hierarchy):
        """Process a field for parent-child relationships in hierarchy"""
        # Handle direct object references
        if isinstance(field_data, ObjectData) and field_data.value in hierarchy:
            child_id = field_data.value
            if child_id != instance_id:  # Avoid self-references
                hierarchy[instance_id]["children"].append(child_id)
                hierarchy[child_id]["parent"] = instance_id
                
        # Handle array of object references
        elif isinstance(field_data, ArrayData):
            for element in field_data.values:
                if isinstance(element, ObjectData) and element.value in hierarchy:
                    child_id = element.value
                    if child_id != instance_id:  # Avoid self-references
                        hierarchy[instance_id]["children"].append(child_id)
                        hierarchy[child_id]["parent"] = instance_id
    
    def _count_all_children(self, node_id, hierarchy, visited=None):
        """Count all children (direct and indirect) of a node"""
        if visited is None:
            visited = set()
        if node_id in visited:
            return 0
            
        visited.add(node_id)
        direct_children = hierarchy[node_id]["children"]
        count = len(direct_children)
        
        for child in direct_children:
            count += self._count_all_children(child, hierarchy, visited)
            
        return count

    def embed_forms(self):
        def on_modified():
            self.mark_modified()
        self.tree.embed_forms(parent_modified_callback=on_modified)

    def rebuild(self) -> bytes:
        if not self.handler:
            raise AttributeError("No handler assigned to viewer")
        try:
            return self.handler.rsz_file.build()
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

    def _create_default_field(self, data_class, original_type, is_array=False, field_size=1):
        try:
            if is_array:
                return ArrayData([], data_class, original_type)
            if data_class == ObjectData:
                return ObjectData(0, original_type)
            if data_class == RawBytesData or data_class == MaybeObject:
                return RawBytesData(bytes(field_size), field_size, original_type)
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
                if isinstance(field_data, ObjectData) or isinstance(field_data, UserDataData):
                    if field_data.value >= index:
                        field_data.value += 1
                elif isinstance(field_data, ArrayData):
                    for elem in field_data.values:
                        if (isinstance(elem, ObjectData) or isinstance(elem, UserDataData)) and elem.value >= index:
                            elem.value += 1
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
        self.handler.id_manager.update_all_mappings(id_mapping)

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
                    self.handler.id_manager.register_instance(instance_id)
        
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
        """Update references in fields after deleting instances"""
        updated_fields = {}
        for field_name, field_data in fields.items():
            if isinstance(field_data, ObjectData) or isinstance(field_data, UserDataData):
                self._update_reference_value(field_data, "value", deleted_ids, id_mapping)
            elif isinstance(field_data, ArrayData):
                for elem in field_data.values:
                    if isinstance(elem, ObjectData) or isinstance(elem, UserDataData):
                        self._update_reference_value(elem, "value", deleted_ids, id_mapping)
            updated_fields[field_name] = field_data
        return updated_fields
        
    def _update_reference_value(self, obj, attr_name, deleted_ids, id_mapping):
        """Update a reference attribute value after deletion"""
        ref_id = getattr(obj, attr_name)
        if ref_id > 0:
            if ref_id in deleted_ids:
                setattr(obj, attr_name, 0)
            else:
                setattr(obj, attr_name, id_mapping.get(ref_id, ref_id))

    def _initialize_new_instance(self, type_id, type_info):
        """Create a new instance from type info with proper CRC"""
        if type_id == 0 or not type_info:
            return None
            
        new_instance = RszInstanceInfo()
        new_instance.type_id = type_id
        
        crc = self._parse_crc_value(type_info.get("crc", 0))
        if crc == 0:
            raise ValueError("CRC is 0")
            
        new_instance.crc = crc
        return new_instance
        
    def _parse_crc_value(self, crc_value):
        """Parse a CRC value from various formats"""
        if isinstance(crc_value, int):
            return crc_value
            
        if isinstance(crc_value, str):
            if crc_value.startswith('0x') or any(c in crc_value.lower() for c in 'abcdef'):
                return int(crc_value, 16)
            return int(crc_value, 10)
            
        return 0

    def _initialize_fields_from_type_info(self, fields_dict, type_info, rui=None, instance_id=None):
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
            field_class = get_type_class(field_type, field_size, field_native, field_array, field_align, field_orig_type, field_name)
            field_obj = self._create_default_field(field_class, field_orig_type, field_array, field_size)
            
            if field_obj:
                fields_dict[field_name] = field_obj
                if hasattr(field_obj, 'orig_type') and field_orig_type:
                    field_obj.orig_type = field_orig_type
                if isinstance(field_obj, ArrayData):
                    field_obj._owning_context = rui
                    field_obj._owning_instance_id = instance_id
                    field_obj._owning_field = field_name
                    
                    field_obj._container_context = rui
                    field_obj._container_parent_id = instance_id
                    field_obj._container_field = field_name
                    
                    if hasattr(field_obj, 'values') and isinstance(field_obj.values, list):
                        for i, elem in enumerate(field_obj.values):
                            if isinstance(elem, ArrayData):
                                elem._owning_context = rui
                                elem._owning_instance_id = instance_id
                                elem._owning_field = f"{field_name}[{i}]"
                                
                                elem._container_array = field_obj
                                elem._container_context = rui
                                elem._container_parent_id = instance_id
                                elem._container_index = i
                            
                            elif isinstance(elem, (ObjectData, UserDataData)):
                                elem._container_array = field_obj
                                elem._container_context = rui
                                elem._container_index = i

    def manage_resource(self, resource_index, new_path):
        """Update an existing resource path"""

        if(self.handler.auto_resource_management):
            raise ValueError("Auto resource management is enabled for this game, cannot manually manage resources.")
        
        if resource_index < 0 or resource_index >= len(self.scn.resource_infos):
            return False
            
        resource = self.scn.resource_infos[resource_index]
        
        if not hasattr(self.scn, '_resource_str_map'):
            return False
        
        if hasattr(self.scn, 'is_pfb16') and self.scn.is_pfb16:
            if hasattr(resource, 'string_value'):
                print(f"Setting PFB.16 resource string directly: '{new_path}'")
                resource.string_value = new_path
                
            if hasattr(self.scn, '_pfb16_direct_strings'):
                direct_strings = self.scn._pfb16_direct_strings
                if resource_index < len(direct_strings):
                    direct_strings[resource_index] = new_path
                    print(f"Updated _pfb16_direct_strings[{resource_index}] to '{new_path}'")
        
        self.scn._resource_str_map[resource] = new_path
        self.mark_modified()
        
        return True

    def add_resource(self, path):
        """Add a new resource path"""

        if(self.handler.auto_resource_management):
            raise ValueError("Auto resource management is enabled for this game, cannot manually manage resources.")
        
        if not path or not hasattr(self.scn, '_resource_str_map'):
            return -1
        
        if self.scn.filepath.lower().endswith('.pfb.16'):
            new_res = create_pfb16_resource(path)
            
            if hasattr(self.scn, '_pfb16_direct_strings'):
                self.scn._pfb16_direct_strings.append(path)
                print(f"Added '{path}' to _pfb16_direct_strings")
        else:
            from file_handlers.rsz.rsz_file import RszResourceInfo
            new_res = RszResourceInfo()
            new_res.string_offset = 0
        
        resource_index = len(self.scn.resource_infos)
        self.scn.resource_infos.append(new_res)
        self.scn._resource_str_map[new_res] = path
        self.mark_modified()
        
        return resource_index

    def delete_resource(self, resource_index):
        
        if(self.handler.auto_resource_management):
            raise ValueError("Auto resource management is enabled for this game, cannot manually manage resources.")
        
        """Delete a resource path"""
        if resource_index < 0 or resource_index >= len(self.scn.resource_infos):
            return False
            
        resource = self.scn.resource_infos[resource_index]
        if hasattr(self.scn, '_resource_str_map') and resource in self.scn._resource_str_map:
            del self.scn._resource_str_map[resource]
            
        if hasattr(self.scn, '_pfb16_direct_strings'):
            direct_strings = self.scn._pfb16_direct_strings
            if resource_index < len(direct_strings):
                direct_strings.pop(resource_index)
                print(f"Removed resource {resource_index} from _pfb16_direct_strings")
        
        self.scn.resource_infos.pop(resource_index)
        self.mark_modified()
        
        return True
