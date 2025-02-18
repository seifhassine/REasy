import os
import tkinter as tk
from tkinter import ttk

from file_handlers import rcol_file
from file_handlers.base_handler import FileHandler
from file_handlers.scn_file import ScnFile, guid_le_to_str, parse_instance_fields
from settings import *

class ScnHandler(FileHandler):
    def __init__(self):
        super().__init__()
        self.scn = ScnFile()
        self.refresh_tree_callback = None
        self.app = None
        self.type_registry = None
        self.folder_icon = None
        self.gameobject_icon = None
        self.testing = False

    def _ensure_icons_loaded(self):
        """Only load icons if not in testing mode and icons aren't already loaded"""
        if self.testing:
            return
        if self.folder_icon is None:
            self.folder_icon = tk.PhotoImage(file="resources/icons/folder.png")
        if self.gameobject_icon is None:
            self.gameobject_icon = tk.PhotoImage(file="resources/icons/gameobject.png")

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        return data[0:4].startswith(b"SCN")

    def read(self, data: bytes):
        json_path = self.app.settings.get("rcol_json_path", "")
        if not json_path or not os.path.exists(json_path):
            raise ValueError("Missing valid JSON file for SCN processing.")
        from utils.type_registry import TypeRegistry
        self.type_registry = TypeRegistry(json_path)
        self.scn.type_registry = self.type_registry
        self.scn.read(data)

    def rebuild(self) -> bytes:
        """
        Finalize SCN file rebuild sequence.
        """
        return self.scn.build()

    def add_variables(self, target, prefix: str, count: int):
        pass

    def get_context_menu(self, tree: tk.Widget, row_id, meta: dict) -> tk.Menu:
        return None

    def supports_editing(self) -> bool:
        return False

    def handle_edit(self, meta: dict, new_val, old_val, row_id):
        return self.rebuild(self)

    def update_strings(self):
        pass

    def _get_gameobject_name(self, instance_index: int, default_name: str) -> str:
        """Helper function to get consistent GameObject names from instance data"""
        if 0 <= instance_index < len(self.scn.parsed_instances):
            parsed_fields = self.scn.parsed_instances[instance_index]
            name = "Game Object"
            if (parsed_fields):
                name = parsed_fields[0]['value']
            return f"{name} (ID: {instance_index})"
        return default_name

    def _get_folder_name(self, folder_id: int, default_name: str) -> str:
        """Helper function to get consistent folder names from instance data"""
        name = default_name
        if folder_id < len(self.scn.object_table):
            instance_index = self.scn.object_table[folder_id]
            if instance_index and instance_index < len(self.scn.parsed_instances):
                parsed_fields = self.scn.parsed_instances[instance_index]
                if parsed_fields and parsed_fields[0].get("value"):
                    name = f"{parsed_fields[0]['value']} [{instance_index}]"
        return " " + name

    def populate_treeview(self, tree: ttk.Treeview, parent_id, metadata_map: dict):
        
        self.testing = not isinstance(tree, ttk.Treeview)
        self._ensure_icons_loaded()
        
        top_id = tree.insert(parent_id, "end", text="SCN_File", values=("",))
        # --- Header ---
        hdr = self.scn.header
        hdr_node = tree.insert(top_id, "end", text="Header", values=("",))
        tree.insert(
            hdr_node,
            "end",
            text="Signature",
            values=(hdr.signature.decode("ascii", errors="replace"),),
        )
        tree.insert(hdr_node, "end", text="Info Count", values=(hdr.info_count,))
        tree.insert(hdr_node, "end", text="Resource Count", values=(hdr.resource_count,))
        tree.insert(hdr_node, "end", text="Folder Count", values=(hdr.folder_count,))
        tree.insert(hdr_node, "end", text="Prefab Count", values=(hdr.prefab_count,))
        tree.insert(hdr_node, "end", text="UserData Count", values=(hdr.userdata_count,))
        tree.insert(hdr_node, "end", text="Folder Tbl", values=(f"0x{hdr.folder_tbl:X}",))
        tree.insert(
            hdr_node,
            "end",
            text="Resource Info Tbl",
            values=(f"0x{hdr.resource_info_tbl:X}",),
        )
        tree.insert(
            hdr_node,
            "end",
            text="Prefab Info Tbl",
            values=(f"0x{hdr.prefab_info_tbl:X}",),
        )
        tree.insert(
            hdr_node,
            "end",
            text="UserData Info Tbl",
            values=(f"0x{hdr.userdata_info_tbl:X}",),
        )
        tree.insert(
            hdr_node, "end", text="Data Offset", values=(f"0x{hdr.data_offset:X}",)
        )

        # --- GameObjects ---
        go_node = tree.insert(
            top_id,
            "end",
            text="GameObjects",
            values=(f"{len(self.scn.gameobjects)} items",),
        )
        for i, go in enumerate(self.scn.gameobjects):
            instance_index = self.scn.object_table[go.id] if go.id < len(self.scn.object_table) else None
            instance_name = self._get_gameobject_name(instance_index, f"GameObject[{i}]")
            
            go_item = tree.insert(
                go_node,
                "end",
                text=instance_name,
                values=(guid_le_to_str(go.guid),),
                image=self.gameobject_icon
            )
            tree.insert(go_item, "end", text="ID", values=(go.id,))
            tree.insert(go_item, "end", text="Parent ID", values=(go.parent_id,))
            tree.insert(
                go_item, "end", text="Component Count", values=(go.component_count,)
            )
            tree.insert(go_item, "end", text="Prefab ID", values=(go.prefab_id,))

        # --- Folder Infos ---
        folder_node = tree.insert(top_id, "end", text="Folder Infos", values=(f"{len(self.scn.folder_infos)} items",))
        for i, folder in enumerate(self.scn.folder_infos):
            folder_name = self._get_folder_name(folder.id, f"FolderInfo[{i}]")
            tree.insert(folder_node, "end", text=folder_name, 
                       values=(f"ID: {self.scn.object_table[folder.id]}, Parent: {folder.parent_id}",),
                       image=self.folder_icon)

        # --- Resource Infos ---
        res_node = tree.insert(top_id, "end", text="Resource Infos", values=(f"{len(self.scn.resource_infos)} items",))
        for i, res in enumerate(self.scn.resource_infos):
            resource_str = ""
            if res.string_offset != 0:
                resource_str = self.scn.get_resource_string(res)
            tree.insert(res_node, "end", text=f"ResourceInfo[{i}]", values=(resource_str,))

        # --- Prefab Infos ---
        prefab_node = tree.insert(top_id, "end", text="Prefab Infos", values=(f"{len(self.scn.prefab_infos)} items",))
        for i, prefab in enumerate(self.scn.prefab_infos):
            prefab_str = ""
            if prefab.string_offset != 0:
                prefab_str = self.scn.get_prefab_string(prefab)
            tree.insert(prefab_node, "end", text=f"PrefabInfo[{i}]", values=(prefab_str,))

        # --- UserData Infos ---
        userdata_node = tree.insert(top_id, "end", text="UserData Infos", values=(f"{len(self.scn.userdata_infos)} items",))
        for i, ui in enumerate(self.scn.userdata_infos):
            userdata_str = ""
            if ui.string_offset != 0:
                userdata_str = self.scn.get_userdata_string(ui)
            tree.insert(userdata_node, "end", text=f"UserDataInfo[{i}]", 
                        values=(f"{userdata_str}",))


        # --- RSZHeader ---
        rsz = self.scn.rsz_header
        rsz_node = tree.insert(top_id, "end", text="RSZHeader", values=("48 bytes",))
        tree.insert(rsz_node, "end", text="Magic", values=(f"0x{rsz.magic:X}",))
        tree.insert(rsz_node, "end", text="Version", values=(rsz.version,))
        tree.insert(rsz_node, "end", text="Object Count", values=(rsz.object_count,))
        tree.insert(rsz_node, "end", text="Instance Count", values=(rsz.instance_count,))
        tree.insert(rsz_node, "end", text="UserData Count", values=(rsz.userdata_count,))
        tree.insert(
            rsz_node, "end", text="Instance Offset", values=(f"0x{rsz.instance_offset:X}",)
        )
        tree.insert(
            rsz_node, "end", text="Data Offset", values=(f"0x{rsz.data_offset:X}",)
        )
        tree.insert(
            rsz_node, "end", text="UserData Offset", values=(f"0x{rsz.userdata_offset:X}",)
        )

        # --- Object Table ---
        ot_node = tree.insert(
            top_id,
            "end",
            text="Object Table",
            values=(f"{len(self.scn.object_table)} items",),
        )
        for i, entry in enumerate(self.scn.object_table):
            tree.insert(ot_node, "end", text=f"Entry {i}", values=(entry,))

        # --- Instance Infos ---
        ii_node = tree.insert(
            top_id,
            "end",
            text="Instance Infos",
            values=(f"{len(self.scn.instance_infos)} items",),
        )
        for i, inst in enumerate(self.scn.instance_infos):
            if i == 0:
                inst_node = tree.insert(ii_node, "end", text="NULL Entry", values=("",))
            else:
                friendly = f"Instance[{i}]"
                if self.type_registry:
                    info = self.type_registry.get_type_info(int(inst.type_id))
                    if info and "name" in info:
                        friendly = info["name"]
                inst_node = tree.insert(ii_node, "end", text=friendly, values=("",))
                tree.insert(inst_node, "end", text="Type", values=(f"0x{inst.type_id:08X}",))
                tree.insert(inst_node, "end", text="CRC", values=(f"0x{inst.crc:08X}",))

        # --- RSZUserDataInfos ---
        rsz_ud_node = tree.insert(top_id, "end", text="RSZUserData Infos", values=(f"{len(self.scn.rsz_userdata_infos)} items",))
        for i, rui in enumerate(self.scn.rsz_userdata_infos):
            rsz_ud_str = ""
            if rui.string_offset != 0:
                rsz_ud_str = self.scn.get_rsz_userdata_string(rui)
            tree.insert(rsz_ud_node, "end", text=f"RSZUserDataInfo[{i}]", 
                        values=(f"{rsz_ud_str}",))

        # --- Data Block – Parsed into Expandable Instances ---
        data_node = tree.insert(top_id, "end", text="Data Block", values=("Instances",))
        
        used_child_indexes = set()  # will hold all instance indexes referenced as children

        # *** Compute locked instances for first 2 priorities ***
        # Lock instances that are already used as GameObjects or Folder Infos
        locked_instances = set()
        for go in self.scn.gameobjects:
            if go.id < len(self.scn.object_table):
                locked_instances.add(self.scn.object_table[go.id])
        for folder in self.scn.folder_infos:
            if folder.id < len(self.scn.object_table):
                locked_instances.add(self.scn.object_table[folder.id])
        
        # Resolve nested references (3rd priority) but skip any reference that is locked.
        for idx in range(1, len(self.scn.parsed_instances)):
            resolve_field_references(
                self.scn.parsed_instances[idx],
                self.scn.parsed_instances,
                used_child_indexes,
                locked_instances=locked_instances
            )

        # Determine which instances are definitely used later as components
        component_instances = set()
        for go in self.scn.gameobjects:
            start_idx = go.id + 1
            end_idx = go.id + go.component_count
            for obj_idx in range(start_idx, end_idx + 1):
                if 0 <= obj_idx < len(self.scn.object_table):
                    inst_idx = self.scn.object_table[obj_idx]
                    component_instances.add(inst_idx)
    
        # Remove component instances from used_child_indexes so they appear as components
        used_child_indexes -= component_instances

        # Build a sorted list of top-level instance indexes (those not used as nested children)
        # Skip index 0 since it's the NULL entry
        top_level_order = [idx for idx in range(1, len(self.scn.instance_infos)) if idx not in used_child_indexes]
        for go in self.scn.gameobjects:
            go_obj_table_index = go.id
            if 0 <= go_obj_table_index < len(self.scn.object_table):
                go_inst_idx = self.scn.object_table[go_obj_table_index]
                if go_inst_idx not in top_level_order:
                    top_level_order.append(go_inst_idx)
        top_level_order.sort()
        
        new_order_pos = {}
        for pos, inst_idx in enumerate(top_level_order):
            new_order_pos[inst_idx] = pos

        instance_to_node = {}
        for inst_idx in top_level_order:
            has_rsz_userdata = any(rui.instance_id == inst_idx for rui in self.scn.rsz_userdata_infos)
            if not has_rsz_userdata:
                type_info = self.type_registry.get_type_info(self.scn.instance_infos[inst_idx].type_id) if self.type_registry else {}
                instance_label = type_info.get("name", f"Instance[{inst_idx}]")
                is_gameobject = any(go.id < len(self.scn.object_table) and 
                                      self.scn.object_table[go.id] == inst_idx 
                                      for go in self.scn.gameobjects)
                is_folder = any(folder.id < len(self.scn.object_table) and 
                              self.scn.object_table[folder.id] == inst_idx 
                              for folder in self.scn.folder_infos)
                if is_gameobject:
                    instance_label = self._get_gameobject_name(inst_idx, instance_label)
                    node = tree.insert(data_node, "end", text=instance_label, values=("",), image=self.gameobject_icon)
                    instance_to_node[inst_idx] = node
                    parsed_fields = self.scn.parsed_instances[inst_idx]
                    insert_parsed_fields_into_tree(tree, node, parsed_fields)
                    continue
                elif is_folder:
                    instance_label = self._get_folder_name(
                        next(folder.id for folder in self.scn.folder_infos 
                            if folder.id < len(self.scn.object_table) and 
                            self.scn.object_table[folder.id] == inst_idx),
                        instance_label
                    )
                    node = tree.insert(data_node, "end", text=instance_label, values=("",), image=self.folder_icon)
                    instance_to_node[inst_idx] = node
                    parsed_fields = self.scn.parsed_instances[inst_idx]
                    insert_parsed_fields_into_tree(tree, node, parsed_fields)
                    continue
                else:
                    instance_label = f"{instance_label} (ID: {inst_idx})"
                node = tree.insert(data_node, "end", text=instance_label, values=("",))
                instance_to_node[inst_idx] = node
                parsed_fields = self.scn.parsed_instances[inst_idx]
                insert_parsed_fields_into_tree(tree, node, parsed_fields)

        # --- Now, organize Components based on GameObjectInfos (1st Priority) ---
        for go in self.scn.gameobjects:
            go_obj_table_index = go.id
            if go_obj_table_index < 0 or go_obj_table_index >= len(self.scn.object_table):
                continue
            go_inst_idx = self.scn.object_table[go_obj_table_index]
            if go_inst_idx not in instance_to_node or go_inst_idx not in new_order_pos:
                continue
            go_node = instance_to_node[go_inst_idx]
            comp_count = go.component_count
            if comp_count <= 0:
                continue
            comp_container = tree.insert(go_node, "end", text="Components", values=("",))
            
            start_pos = new_order_pos[go_inst_idx] + 1
            remaining_comps = comp_count
            component_candidates = []
            
            for inst_idx in top_level_order[start_pos:]:
                if remaining_comps <= 0:
                    break
                if inst_idx in used_child_indexes:
                    continue
                if inst_idx >= len(self.scn.instance_infos):
                    continue
                has_rsz_userdata = any(rui.instance_id == inst_idx for rui in self.scn.rsz_userdata_infos)
                if has_rsz_userdata:
                    continue
                component_candidates.append(inst_idx)
                remaining_comps -= 1

            for comp_idx in component_candidates[:comp_count]:
                if comp_idx not in instance_to_node:
                    continue
                used_child_indexes.add(comp_idx)
                comp_node = instance_to_node[comp_idx]
                comp_label = tree.item(comp_node, "text")
                new_comp_node = tree.insert(comp_container, "end", text=comp_label, values=tree.item(comp_node, "values"))
                for child in tree.get_children(comp_node):
                    tree.move(child, new_comp_node, "end")
                tree.detach(comp_node)

        # --- Organize by ParentID (2nd Priority) ---
        for go in self.scn.gameobjects:
            if go.parent_id != -1:
                child_inst_idx = self.scn.object_table[go.id] if go.id < len(self.scn.object_table) else None
                parent_inst_idx = self.scn.object_table[go.parent_id] if go.parent_id < len(self.scn.object_table) else None
                if child_inst_idx in instance_to_node and parent_inst_idx in instance_to_node:
                    child_node = instance_to_node[child_inst_idx]
                    parent_node = instance_to_node[parent_inst_idx]
                    tree.move(child_node, parent_node, "end")

        for folder in self.scn.folder_infos:
            if folder.parent_id != -1:
                child_inst_idx = self.scn.object_table[folder.id] if folder.id < len(self.scn.object_table) else None
                parent_inst_idx = self.scn.object_table[folder.parent_id] if folder.parent_id < len(self.scn.object_table) else None
                if child_inst_idx in instance_to_node and parent_inst_idx in instance_to_node:
                    child_node = instance_to_node[child_inst_idx]
                    parent_node = instance_to_node[parent_inst_idx]
                    tree.move(child_node, parent_node, "end")

def insert_parsed_fields_into_tree(tree, parent, fields, depth=0, max_depth=10000):
    if depth > max_depth:
        return
    for field in fields:
        if not isinstance(field, dict):
            field = {"name": str(field), "value": "", "subfields": []}
        name = field.get("name", "<unnamed>")
        value = field.get("value", "")
        node = tree.insert(parent, "end", text=name, values=(value,))
        subfields = field.get("subfields", [])
        if isinstance(subfields, list) and subfields:
            insert_parsed_fields_into_tree(tree, node, subfields, depth=depth+1, max_depth=max_depth)
            
def resolve_field_references(fields, parsed_instances, used_child_indexes, locked_instances=None, depth=0, max_depth=10000):
    """ field reference resolution that will skip references to locked instances.
       This ensures that component and parentid nesting (first 2 priorities) arent overridden."""
    if depth > max_depth:
        return

    SINGLE_REF = "Child index:"
    ARRAY_REF = "Child indexes:"
    SINGLE_LEN = len(SINGLE_REF)
    ARRAY_LEN = len(ARRAY_REF)
    value_cache = {}
    field_cache = {}
    stack = [(fields, depth)]
    while stack:
        current_fields, current_depth = stack.pop()
        field_list = current_fields if isinstance(current_fields, list) else [current_fields]
        for field in field_list:
            if not isinstance(field, dict):
                continue
                
            val = field.get("value")
            if not isinstance(val, str):
                continue
                
            # Process parentid field – skip if the referenced id is locked.
            if field.get("name", "").lower() == "parentid":
                try:
                    pid = int(val)
                    if locked_instances is not None and pid in locked_instances:
                        continue
                    if pid != -1 and pid < len(parsed_instances):
                        used_child_indexes.add(pid)
                    field["value"] = ""
                except ValueError:
                    pass
                continue

            if val.startswith(SINGLE_REF):
                try:
                    if val in value_cache:
                        idx = value_cache[val]
                    else:
                        idx = int(val[SINGLE_LEN:].strip())
                        value_cache[val] = idx
                    if locked_instances is not None and idx in locked_instances:
                        continue
                    if idx < len(parsed_instances):
                        used_child_indexes.add(idx)
                        cache_key = (field.get("name", "Unknown"), idx)
                        if cache_key not in field_cache:
                            field_cache[cache_key] = {
                                "name": f"{field.get('name', 'Unknown')}[0]",
                                "value": "",
                                "subfields": parsed_instances[idx]
                            }
                        field["subfields"] = [field_cache[cache_key]]
                        field["value"] = ""
                except ValueError:
                    continue
                    
            elif val.startswith(ARRAY_REF):
                try:
                    if val in value_cache:
                        indexes = value_cache[val]
                    else:
                        indexes_str = val[ARRAY_LEN:].strip("[] ")
                        indexes = [int(x) for x in indexes_str.split(",") if x.strip()]
                        value_cache[val] = indexes
                    containers = []
                    for i, idx in enumerate(indexes):
                        if locked_instances is not None and idx in locked_instances:
                            continue
                        if idx < len(parsed_instances):
                            used_child_indexes.add(idx)
                            cache_key = (field.get("name", "Unknown"), i, idx)
                            if cache_key not in field_cache:
                                field_cache[cache_key] = {
                                    "name": f"{field.get('name', 'Unknown')}[{i}]",
                                    "value": "",
                                    "subfields": parsed_instances[idx]
                                }
                            containers.append(field_cache[cache_key])
                    field["subfields"] = containers
                    field["value"] = ""
                except ValueError:
                    continue
            
            subfields = field.get("subfields")
            if isinstance(subfields, list) and subfields and current_depth < max_depth:
                stack.append((subfields, current_depth + 1))
