import struct
import tkinter as tk
from tkinter import ttk

from file_handlers.base_handler import FileHandler
from file_handlers.rcol_file import RcolFile, guid_le_to_str, parse_instance_fields
from utils.type_registry import TypeRegistry
from settings import *


class RcolHandler(FileHandler):
    def __init__(self):
        super().__init__()
        self.rcol = RcolFile()
        self.refresh_tree_callback = None
        self.app = None
        self.type_registry = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        sig = struct.unpack_from("<I", data, 0)[0]
        return sig == 0x4C4F4352

    def read(self, data: bytes):
        if not ensure_json_path(self):
            raise ValueError("Missing valid JSON file for RCOL processing.")
        json_path = self.app.settings["rcol_json_path"]
        self.type_registry = TypeRegistry(json_path)
        self.rcol.type_registry = self.type_registry
        self.rcol.read(data)

    def rebuild(self) -> bytes:
        return b""

    def add_variables(self, target, prefix: str, count: int):
        pass

    def get_context_menu(self, tree: tk.Widget, row_id, meta: dict) -> tk.Menu:
        return None

    def supports_editing(self) -> bool:
        return False

    def handle_edit(self, meta: dict, new_val, old_val, row_id):
        pass

    def update_strings(self):
        pass

    def _populate_rsz_user_data_components(
        self, tree: ttk.Treeview, parent_id, ud_full
    ):
        # RSZUserData Components section: Header, ObjectTable, InstanceInfos, and Data groups (renamed to "Data")
        comp_node = tree.insert(
            parent_id, "end", text="RSZUserData Components", values=("",)
        )
        hdr = ud_full.header
        hdr_node = tree.insert(comp_node, "end", text="RSZHeader", values=("",))
        tree.insert(hdr_node, "end", text="magic", values=(f"0x{hdr.magic:X}",))
        tree.insert(hdr_node, "end", text="version", values=(hdr.version,))
        tree.insert(hdr_node, "end", text="objectCount", values=(hdr.objectCount,))
        tree.insert(hdr_node, "end", text="instanceCount", values=(hdr.instanceCount,))
        tree.insert(hdr_node, "end", text="userDataCount", values=(hdr.userDataCount,))
        tree.insert(hdr_node, "end", text="reserved", values=(f"0x{hdr.reserved:X}",))
        tree.insert(
            hdr_node,
            "end",
            text="instanceOffset",
            values=(f"0x{hdr.instanceOffset:X}",),
        )
        tree.insert(
            hdr_node, "end", text="dataOffset", values=(f"0x{hdr.dataOffset:X}",)
        )
        tree.insert(
            hdr_node,
            "end",
            text="userDataOffset",
            values=(f"0x{hdr.userDataOffset:X}",),
        )

        # ObjectTable Section
        ot_node = tree.insert(
            comp_node,
            "end",
            text="ObjectTable",
            values=(f"{len(ud_full.object_table)} items",),
        )
        for i, obj in enumerate(ud_full.object_table):
            if 0 <= obj < len(ud_full.instance_infos):
                inst = ud_full.instance_infos[obj]
                if obj == 0:
                    friendly = "NULL Entry"
                else:
                    info = self.rcol.type_registry.get_type_info(int(inst.type_id))
                    friendly = (
                        info.get("name", f"Instance[{obj}]")
                        if info is not None
                        else f"Instance[{obj}]"
                    )
            else:
                friendly = "Out of range"
            tree.insert(ot_node, "end", text=friendly, values=(f"{obj}",))

        # InstanceInfos Section
        inst_node = tree.insert(
            comp_node,
            "end",
            text="InstanceInfos",
            values=(f"{len(ud_full.instance_infos)} items",),
        )
        for i, inst in enumerate(ud_full.instance_infos):
            if i == 0:
                lbl = "NULL Entry"
            else:
                info = self.rcol.type_registry.get_type_info(int(inst.type_id))
                lbl = (
                    info.get("name", f"Instance[{i}]")
                    if info is not None
                    else f"Instance[{i}]"
                )
            arr_node = tree.insert(inst_node, "end", text=lbl, values=("",))
            tree.insert(arr_node, "end", text="[0] => type_id", values=(inst.type_id,))
            tree.insert(
                arr_node, "end", text="[1] => crc", values=(f"0x{inst.crc:08X}",)
            )

        # Data Section (renamed to "Data")
        if ud_full.data_group_bytes:
            data_node = tree.insert(
                comp_node,
                "end",
                text="Data",
                values=(f"{len(ud_full.data_group_bytes)} groups",),
            )
            for (
                main,
                children,
                child_sizes,
                raw,
                parsed_fields,
            ) in ud_full.data_group_bytes:
                if main < len(ud_full.instance_infos):
                    inst = ud_full.instance_infos[main]
                    info = self.rcol.type_registry.get_type_info(int(inst.type_id))
                    friendly = (
                        info.get("name", f"Instance[{main}]")
                        if info is not None
                        else f"Instance[{main}]"
                    )
                else:
                    friendly = f"Instance[{main}]"
                # Get the first child's first field value (v0) as the group's value.
                if parsed_fields and parsed_fields[0][1]:
                    first_child_v0 = parsed_fields[0][1][0].get("value", "N/A")
                else:
                    first_child_v0 = "N/A"
                group_node = tree.insert(
                    data_node, "end", text=f"{friendly}", values=(f"{first_child_v0}",)
                )
                for child_index, fields in parsed_fields:
                    if child_index < len(ud_full.instance_infos):
                        inst_child = ud_full.instance_infos[child_index]
                        info_child = self.rcol.type_registry.get_type_info(
                            int(inst_child.type_id)
                        )
                        child_name = (
                            info_child.get("name", f"Instance[{child_index}]")
                            if info_child is not None
                            else f"Instance[{child_index}]"
                        )
                    else:
                        child_name = f"Instance[{child_index}]"
                    idx_in_group = children.index(child_index)
                    child_node = tree.insert(
                        group_node,
                        "end",
                        text=child_name,
                        values=(f"Parsed: {child_sizes[idx_in_group]} bytes",),
                    )
                    self._add_field_nodes(tree, child_node, fields)

    def _add_field_nodes(self, tree: ttk.Treeview, parent, fields):
        for fld in fields:
            fld_node = tree.insert(
                parent, "end", text=fld["name"], values=(fld["value"],)
            )
            if fld.get("subfields"):
                self._add_field_nodes(tree, fld_node, fld["subfields"])

    def populate_treeview(self, tree: ttk.Treeview, parent_id, metadata_map: dict):
        top_id = tree.insert(parent_id, "end", text="RCOL_File", values=("",))
        hdr_id = tree.insert(top_id, "end", text="Header", values=("",))
        tree.insert(hdr_id, "end", text="signature", values=(self.rcol.signature,))
        tree.insert(hdr_id, "end", text="numGroups", values=(self.rcol.numGroups,))
        tree.insert(hdr_id, "end", text="numShapes", values=(self.rcol.numShapes,))
        tree.insert(hdr_id, "end", text="numUserData", values=(self.rcol.numUserData,))
        tree.insert(
            hdr_id, "end", text="numRequestSets", values=(self.rcol.numRequestSets,)
        )
        tree.insert(
            hdr_id, "end", text="maxRequestSetId", values=(self.rcol.maxRequestSetId,)
        )
        tree.insert(
            hdr_id, "end", text="numIgnoreTags", values=(self.rcol.numIgnoreTags,)
        )
        tree.insert(
            hdr_id,
            "end",
            text="numAutoGenerateJoints",
            values=(self.rcol.numAutoGenerateJoints,),
        )
        tree.insert(
            hdr_id, "end", text="userDataSize", values=(self.rcol.userDataSize,)
        )
        tree.insert(hdr_id, "end", text="status", values=(self.rcol.status,))
        tree.insert(hdr_id, "end", text="ukn", values=(self.rcol.ukn,))
        tree.insert(
            hdr_id,
            "end",
            text="groupsPtrTbl",
            values=(f"0x{self.rcol.groupsPtrTbl:X}",),
        )
        tree.insert(
            hdr_id,
            "end",
            text="userDataStreamPtr",
            values=(f"0x{self.rcol.userDataStreamPtr:X}",),
        )
        tree.insert(
            hdr_id,
            "end",
            text="requestSetTbl",
            values=(f"0x{self.rcol.requestSetTbl:X}",),
        )
        tree.insert(
            hdr_id,
            "end",
            text="ignoreTagTbl",
            values=(f"0x{self.rcol.ignoreTagTbl:X}",),
        )
        tree.insert(
            hdr_id,
            "end",
            text="autoGenerateJointDescTbl",
            values=(f"0x{self.rcol.autoGenerateJointDescTbl:X}",),
        )

        # File-level Groups Section: display each group's name as its value.
        if self.rcol.groups:
            grp_node = tree.insert(
                top_id, "end", text=f"Groups ({self.rcol.numGroups})", values=("",)
            )
            for i, grp in enumerate(self.rcol.groups):
                # Use the group's name as the displayed value.
                g_id = tree.insert(
                    grp_node, "end", text=f"Group[{i}]", values=(grp.name,)
                )
                tree.insert(
                    g_id, "end", text="GUID", values=(guid_le_to_str(grp.group_guid),)
                )
                tree.insert(g_id, "end", text="name", values=(grp.name,))
                tree.insert(g_id, "end", text="name_hash", values=(grp.name_hash,))
                tree.insert(
                    g_id, "end", text="user_data_index", values=(grp.user_data_index,)
                )
                tree.insert(
                    g_id, "end", text="shapes_tbl", values=(f"0x{grp.shapes_tbl:X}",)
                )
                tree.insert(g_id, "end", text="num_shapes", values=(grp.num_shapes,))
                tree.insert(
                    g_id, "end", text="num_mask_guids", values=(grp.num_mask_guids,)
                )
                tree.insert(g_id, "end", text="layer_index", values=(grp.layer_index,))
                tree.insert(
                    g_id, "end", text="mask_bits", values=(f"0x{grp.mask_bits:X}",)
                )
                tree.insert(
                    g_id,
                    "end",
                    text="mask_guids_offset",
                    values=(f"0x{grp.mask_guids_offset:X}",),
                )
                tree.insert(
                    g_id,
                    "end",
                    text="layer_guid",
                    values=(guid_le_to_str(grp.layer_guid),),
                )
                if grp.num_mask_guids > 0 and grp.mask_guids:
                    mg_node = tree.insert(g_id, "end", text="MaskGuids", values=("",))
                    for j, mg in enumerate(grp.mask_guids):
                        tree.insert(
                            mg_node,
                            "end",
                            text=f"MaskGuid[{j}]",
                            values=(guid_le_to_str(mg),),
                        )
                shp_node = tree.insert(
                    g_id, "end", text=f"Shapes ({grp.num_shapes})", values=("",)
                )
                for s_idx, shape in enumerate(grp.shapes):
                    sid = tree.insert(
                        shp_node, "end", text=f"Shape[{s_idx}]", values=("",)
                    )
                    tree.insert(
                        sid, "end", text="GUID", values=(guid_le_to_str(shape.guid),)
                    )
                    tree.insert(sid, "end", text="name", values=(shape.name,))
                    tree.insert(sid, "end", text="name_hash", values=(shape.name_hash,))
                    tree.insert(
                        sid,
                        "end",
                        text="user_data_index",
                        values=(shape.user_data_index,),
                    )
                    tree.insert(
                        sid, "end", text="layer_index", values=(shape.layer_index,)
                    )
                    tree.insert(sid, "end", text="Attribute", values=(shape.attribute,))
                    tree.insert(
                        sid, "end", text="SkipIdBits", values=(shape.skip_id_bits,)
                    )
                    tree.insert(
                        sid,
                        "end",
                        text="IgnoreTagBits",
                        values=(shape.ignore_tag_bits,),
                    )
                    tree.insert(
                        sid,
                        "end",
                        text="primary_joint_name",
                        values=(shape.primary_joint_name,),
                    )
                    tree.insert(
                        sid,
                        "end",
                        text="secondary_joint_name",
                        values=(shape.secondary_joint_name,),
                    )
                    tree.insert(
                        sid,
                        "end",
                        text="PrimaryJointNameHash",
                        values=(shape.primary_joint_name_hash,),
                    )
                    tree.insert(
                        sid,
                        "end",
                        text="SecondaryJointNameHash",
                        values=(shape.secondary_joint_name_hash,),
                    )
                    tree.insert(
                        sid, "end", text="ShapeType", values=(shape.shape_type,)
                    )
                    params_node = tree.insert(
                        sid, "end", text="Parameters", values=("",)
                    )
                    # Need to double check the names of these parameters.
                    parameter_titles = [
                        "Position",
                        "Rotation",
                        "Scale",
                        "Shear",
                        "Extra",
                    ]
                    for group_num in range(5):
                        gp = tree.insert(
                            params_node,
                            "end",
                            text=parameter_titles[group_num],
                            values=("",),
                        )
                        # Use uppercase labels for coordinates.
                        labels = ["X", "Y", "Z", "W"]
                        for j in range(4):
                            idx = group_num * 4 + j
                            val = shape.parameters[idx]
                            tree.insert(
                                gp, "end", text=f"{labels[j]}", values=(f"{val:.2f}",)
                            )

        # RSZUserData Components Section
        if self.rcol.user_data_full:
            self._populate_rsz_user_data_components(
                tree, top_id, self.rcol.user_data_full
            )

        # RequestSets Section with detailed Referenced Group and Referenced Object
        if self.rcol.request_sets:
            rs_node = tree.insert(
                top_id,
                "end",
                text=f"RequestSets ({len(self.rcol.request_sets)})",
                values=("",),
            )
            for i, rs in enumerate(self.rcol.request_sets):
                rid = tree.insert(rs_node, "end", text=f"RequestSet[{i}]", values=("",))
                tree.insert(rid, "end", text="req_id", values=(rs.req_id,))
                tree.insert(rid, "end", text="group_index", values=(rs.group_index,))
                tree.insert(
                    rid, "end", text="shape_offset", values=(f"0x{rs.shape_offset:X}",)
                )
                tree.insert(rid, "end", text="status", values=(rs.status,))
                tree.insert(rid, "end", text="uknA", values=(rs.uknA,))
                tree.insert(rid, "end", text="uknB", values=(rs.uknB,))
                # Display the actual name fields instead of offsets.
                tree.insert(rid, "end", text="Name", values=(rs.name,))
                tree.insert(rid, "end", text="KeyName", values=(rs.keyname,))
                tree.insert(rid, "end", text="keyhash", values=(f"0x{rs.keyhash:X}",))
                tree.insert(rid, "end", text="keyhash2", values=(f"0x{rs.keyhash2:X}",))

                # Add Referenced Group as a child node (detailed like Groups array)
                if 0 <= rs.group_index < len(self.rcol.groups):
                    grp = self.rcol.groups[rs.group_index]
                    grp_node = tree.insert(
                        rid, "end", text="Referenced Group", values=("",)
                    )
                    tree.insert(
                        grp_node,
                        "end",
                        text="GUID",
                        values=(guid_le_to_str(grp.group_guid),),
                    )
                    tree.insert(grp_node, "end", text="name", values=(grp.name,))
                    tree.insert(
                        grp_node, "end", text="num_shapes", values=(grp.num_shapes,)
                    )
                else:
                    tree.insert(rid, "end", text="Referenced Group", values=("N/A",))

                # Add Referenced Object as an expandable array.
                # The referenced object is taken from the ObjectTable at index equal to the request's req_id.
                if self.rcol.user_data_full and self.rcol.user_data_full.object_table:
                    if rs.req_id < len(self.rcol.user_data_full.object_table):
                        obj_index = self.rcol.user_data_full.object_table[rs.req_id]
                        if (
                            0
                            <= obj_index
                            < len(self.rcol.user_data_full.instance_infos)
                        ):
                            inst = self.rcol.user_data_full.instance_infos[obj_index]
                            info = self.rcol.type_registry.get_type_info(
                                int(inst.type_id)
                            )
                            friendly = (
                                info.get("name", f"Instance[{obj_index}]")
                                if info is not None
                                else f"Instance[{obj_index}]"
                            )
                        else:
                            friendly = f"Instance[{obj_index}]"
                        ref_obj_node = tree.insert(
                            rid,
                            "end",
                            text="Referenced Object",
                            values=(f"ObjectTable[{rs.req_id}] -> {friendly}",),
                        )
                        # Expand referenced object details:
                        tree.insert(
                            ref_obj_node, "end", text="type_id", values=(inst.type_id,)
                        )
                        tree.insert(
                            ref_obj_node,
                            "end",
                            text="crc",
                            values=(f"0x{inst.crc:08X}",),
                        )
                    else:
                        tree.insert(
                            rid,
                            "end",
                            text="Referenced Object",
                            values=("Index out of range",),
                        )
                else:
                    tree.insert(rid, "end", text="Referenced Object", values=("N/A",))
