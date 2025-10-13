import tempfile
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from pathlib import Path

from file_handlers.rsz.rsz_file import RszFile, RszGameObject, RszFolderInfo
from file_handlers.rsz.rsz_handler import RszHandler
from file_handlers.rsz.utils.rsz_name_helper import RszViewerNameHelper
from utils.hex_util import guid_le_to_str

@dataclass
class GameObjectDiff:
    guid: str
    name: str
    status: str
    details: Optional[str] = None

@dataclass
class FolderDiff:
    path: str
    status: str
    details: Optional[str] = None

@dataclass
class DiffResult:
    gameobject_diffs: List[GameObjectDiff]
    folder_diffs: List[FolderDiff]
    summary: Dict[str, int]

class RszDiffer:
    def __init__(
        self,
        json_path: Optional[str] = None,
        file1_json_path: Optional[str] = None,
        file2_json_path: Optional[str] = None,
    ):
        self.handler1 = RszHandler()
        self.handler2 = RszHandler()
        self.export_dir = tempfile.gettempdir()
        self.json_path = json_path
        self.file1_json_path = file1_json_path
        self.file2_json_path = file2_json_path

        class MockApp:
            def __init__(self, json_path: Optional[str] = None):
                self.settings = {}
                if json_path:
                    self.settings["rcol_json_path"] = json_path

        handler1_json = file1_json_path or json_path
        handler2_json = file2_json_path or json_path

        self.handler1.app = MockApp(handler1_json)
        self.handler2.app = MockApp(handler2_json)

    def set_game_version(self, version: str):
        self.handler1.game_version = version
        self.handler2.game_version = version

    def export_files(self, file1_data: bytes, file2_data: bytes) -> Tuple[Path, Path]:
        export_path1 = Path(self.export_dir) / "reasy_comparee_1"
        export_path2 = Path(self.export_dir) / "reasy_comparee_2"

        with open(export_path1, 'wb') as f:
            f.write(file1_data)

        with open(export_path2, 'wb') as f:
            f.write(file2_data)

        return export_path1, export_path2

    def load_rsz_files(self, file1_data: bytes, file2_data: bytes, file1_path: str = None, file2_path: str = None) -> Tuple[RszFile, RszFile]:
        if file1_path:
            self.handler1.filepath = file1_path
        else:
            self.handler1.filepath = "reasy_comparee_1"

        if file2_path:
            self.handler2.filepath = file2_path
        else:
            self.handler2.filepath = "reasy_comparee_2"

        self.handler1.read(file1_data)

        self.handler2.read(file2_data)

        self.name_helper1 = RszViewerNameHelper(self.handler1.rsz_file, self.handler1.type_registry)
        self.name_helper2 = RszViewerNameHelper(self.handler2.rsz_file, self.handler2.type_registry)

        return self.handler1.rsz_file, self.handler2.rsz_file

    def get_gameobjects_map(self, rsz_file: RszFile) -> Dict[str, Tuple[RszGameObject, str]]:
        gameobjects = {}

        if hasattr(rsz_file, 'gameobjects'):
            for go in rsz_file.gameobjects:
                if hasattr(go, 'guid'):
                    guid_str = guid_le_to_str(go.guid)
                    if guid_str == "00000000-0000-0000-0000-000000000000":
                        continue
                    key = guid_str
                else:
                    key = f"pfb-{go.id}"

                name = self._get_gameobject_name(rsz_file, go)
                gameobjects[key] = (go, name)

        return gameobjects

    def _get_gameobject_name(self, rsz_file: RszFile, gameobject: RszGameObject) -> str:
        if hasattr(self, 'name_helper1') and rsz_file == self.handler1.rsz_file:
            name_helper = self.name_helper1
        elif hasattr(self, 'name_helper2') and rsz_file == self.handler2.rsz_file:
            name_helper = self.name_helper2
        else:
            type_registry = self.handler1.type_registry if rsz_file == self.handler1.rsz_file else self.handler2.type_registry
            name_helper = RszViewerNameHelper(rsz_file, type_registry)
        
        if hasattr(gameobject, 'guid'):
            guid_str = guid_le_to_str(gameobject.guid)
            default_name = f"GameObject_{guid_str[:8]}"
        elif getattr(rsz_file, 'is_pfb', False):
            default_name = f"PrefabObject_{gameobject.id}"
        elif getattr(rsz_file, 'is_usr', False):
            default_name = "UserRoot"
        else:
            default_name = f"GameObject_{gameobject.id}"
        
        if gameobject.id < len(rsz_file.object_table):
            instance_id = rsz_file.object_table[gameobject.id]
            return name_helper.get_gameobject_name(instance_id, default_name)
        return default_name

    def get_all_instances(self, rsz_file: RszFile) -> Dict[int, Any]:
        if hasattr(rsz_file, 'parsed_elements'):
            return rsz_file.parsed_elements
        return {}

    def get_gameobject_instances(self, rsz_file: RszFile, gameobject: RszGameObject) -> List[Tuple[int, Any]]:
        instances = []
        if not hasattr(rsz_file, 'parsed_elements') or not hasattr(rsz_file, 'instance_infos'):
            return instances

        if hasattr(rsz_file, 'object_table') and rsz_file.object_table:
            if gameobject.id < len(rsz_file.object_table):

                for i in range(1, gameobject.component_count + 1):
                    comp_object_id = gameobject.id + i
                    if comp_object_id < len(rsz_file.object_table):
                        comp_instance_id = rsz_file.object_table[comp_object_id]
                        if comp_instance_id > 0 and comp_instance_id < len(rsz_file.instance_infos):
                            inst_info = rsz_file.instance_infos[comp_instance_id]
                            parsed_data = rsz_file.parsed_elements.get(comp_instance_id, {})
                            if parsed_data:
                                instances.append((comp_instance_id, parsed_data, inst_info))

        if not instances:
            for idx, inst_info in enumerate(rsz_file.instance_infos):
                if idx == 0:
                    continue
                parsed_data = rsz_file.parsed_elements.get(idx, {})
                if parsed_data and 'm_GameObject' in parsed_data:
                    go_ref = parsed_data['m_GameObject']
                    if hasattr(go_ref, 'value') and go_ref.value == gameobject.id:
                        instances.append((idx, parsed_data, inst_info))

        return instances

    def compare_gameobject_instances(self, go1: RszGameObject, go2: RszGameObject) -> List[str]:
        changes = []

        if not self.handler1.rsz_file or not self.handler2.rsz_file:
            return changes

        instances1 = self.get_gameobject_instances(self.handler1.rsz_file, go1)
        instances2 = self.get_gameobject_instances(self.handler2.rsz_file, go2)

        if len(instances1) != len(instances2):
            changes.append(f"Component count: {len(instances1)} → {len(instances2)}")

            for i, (idx, data, info) in enumerate(instances1[:5]):
                type_name = self.get_type_name(self.handler1.rsz_file, info)
                if i >= len(instances2):
                    changes.append(f"Removed component[{i}]: {type_name}")

            for i, (idx, data, info) in enumerate(instances2[:5]):
                type_name = self.get_type_name(self.handler2.rsz_file, info)
                if i >= len(instances1):
                    changes.append(f"Added component[{i}]: {type_name}")
        else:
            for comp_idx, (inst1, inst2) in enumerate(zip(instances1, instances2)):
                idx1, data1, info1 = inst1
                idx2, data2, info2 = inst2
                type_name1 = self.get_type_name(self.handler1.rsz_file, info1)
                type_name2 = self.get_type_name(self.handler2.rsz_file, info2)

                if type_name1 != type_name2:
                    changes.append(f"Component[{comp_idx}] ({type_name1} → {type_name2})")
                else:
                    field_changes = self.compare_parsed_data(data1, data2, f"{type_name1}", 0, max_changes=5)
                    if field_changes:
                        if len(field_changes) == 1:
                            changes.append(f"Component[{comp_idx}] ({type_name1}): {field_changes[0]}")
                        else:
                            changes.append(f"Component[{comp_idx}] ({type_name1}) has {len(field_changes)} changes")
                            for fc in field_changes[:3]:
                                changes.append(f"  - {fc}")

                    embedded_changes = self.check_embedded_rsz(idx1, idx2)
                    if embedded_changes:
                        changes.append(f"Component[{comp_idx}] ({type_name1}) has embedded RSZ changes:")
                        for ec in embedded_changes[:3]:
                            changes.append(f"  - {ec}")

        return changes

    def traverse_and_compare_embedded_data(self, data1, data2, path: str, rsz1: RszFile, rsz2: RszFile, in_embedded: bool = True) -> List[str]:

        changes = []

        if hasattr(data1, '__dict__') and hasattr(data2, '__dict__'):
            attrs1 = set(dir(data1))
            attrs2 = set(dir(data2))

            common_attrs = attrs1 & attrs2
            for attr in common_attrs:
                if attr.startswith('_'):
                    continue

                val1 = getattr(data1, attr, None)
                val2 = getattr(data2, attr, None)

                if val1 != val2:
                    if isinstance(val1, (int, float, str, bool)) and isinstance(val2, (int, float, str, bool)):
                        changes.append(f"{path}.{attr}: {val1} → {val2}")
                    elif isinstance(val1, list) and isinstance(val2, list):
                        if len(val1) != len(val2):
                            changes.append(f"{path}.{attr} count: {len(val1)} → {len(val2)}")
                        else:
                            for i, (item1, item2) in enumerate(zip(val1, val2)):
                                item_changes = self.traverse_and_compare_embedded_data(
                                    item1, item2, f"{path}.{attr}[{i}]", rsz1, rsz2, in_embedded
                                )
                                changes.extend(item_changes[:3])
                    elif hasattr(val1, '__dict__') and hasattr(val2, '__dict__'):
                        nested_changes = self.traverse_and_compare_embedded_data(
                            val1, val2, f"{path}.{attr}", rsz1, rsz2, in_embedded
                        )
                        changes.extend(nested_changes[:5])

        elif isinstance(data1, dict) and isinstance(data2, dict):
            all_keys = set(data1.keys()) | set(data2.keys())

            for key in sorted(all_keys):
                if key in data1 and key not in data2:
                    changes.append(f"{path}.{key}: exists only in file 1")
                elif key not in data1 and key in data2:
                    changes.append(f"{path}.{key}: exists only in file 2")
                elif key in data1 and key in data2:
                    val1 = data1[key]
                    val2 = data2[key]

                    if val1 != val2:
                        field_changes = self.compare_field_values(val1, val2, f"{path}.{key}", 0, in_embedded=in_embedded)
                        changes.extend(field_changes[:3])

        return changes

    def check_embedded_rsz(self, instance_id1: int, instance_id2: int, depth: int = 0) -> List[str]:

        changes = []

        if depth > 3:
            return changes

        rsz1 = self.handler1.rsz_file
        rsz2 = self.handler2.rsz_file

        if not hasattr(rsz1, 'rsz_userdata_infos') or not hasattr(rsz2, 'rsz_userdata_infos'):
            return changes

        userdata1 = [ud for ud in rsz1.rsz_userdata_infos if ud.instance_id == instance_id1]
        userdata2 = [ud for ud in rsz2.rsz_userdata_infos if ud.instance_id == instance_id2]

        if len(userdata1) != len(userdata2):
            changes.append(f"Embedded RSZ entries: {len(userdata1)} → {len(userdata2)}")
            return changes

        for i, (ud1, ud2) in enumerate(zip(userdata1, userdata2)):
            embedded_changes = self.compare_embedded_instance(ud1, ud2, rsz1, rsz2, i, depth)
            changes.extend(embedded_changes)

        return changes

    def compare_embedded_instance(self, ud1, ud2, rsz1, rsz2, index: int, depth: int) -> List[str]:

        changes = []
        prefix = "  " * depth + f"Embedded[{index}]"

        if hasattr(ud1, 'hash') and hasattr(ud2, 'hash'):
            if ud1.hash != ud2.hash:
                changes.append(f"{prefix} hash: {ud1.hash} → {ud2.hash}")

        str1 = rsz1._rsz_userdata_str_map.get(ud1, "") if hasattr(rsz1, '_rsz_userdata_str_map') else ""
        str2 = rsz2._rsz_userdata_str_map.get(ud2, "") if hasattr(rsz2, '_rsz_userdata_str_map') else ""

        if str1 != str2:
            changes.append(f"{prefix} string: '{str1[:50]}' → '{str2[:50]}'")

        if hasattr(ud1, 'embedded_rsz_header') and hasattr(ud2, 'embedded_rsz_header'):
            header1 = ud1.embedded_rsz_header
            header2 = ud2.embedded_rsz_header

            if hasattr(header1, 'instance_count') and hasattr(header2, 'instance_count'):
                if header1.instance_count != header2.instance_count:
                    changes.append(f"{prefix} embedded instances: {header1.instance_count} → {header2.instance_count}")

            if hasattr(ud1, 'embedded_instances') and hasattr(ud2, 'embedded_instances'):
                inst_changes = self.compare_embedded_instances(ud1.embedded_instances, ud2.embedded_instances,
                                                               prefix, depth + 1, in_embedded=True)
                changes.extend(inst_changes)

        if hasattr(ud1, 'nested_userdata') and hasattr(ud2, 'nested_userdata'):
            for j, (nested1, nested2) in enumerate(zip(ud1.nested_userdata, ud2.nested_userdata)):
                nested_changes = self.compare_embedded_instance(nested1, nested2, rsz1, rsz2, j, depth + 1)
                changes.extend(nested_changes)

        return changes

    def compare_embedded_instances(self, instances1, instances2, prefix: str, depth: int, in_embedded: bool = True) -> List[str]:

        changes = []

        if len(instances1) != len(instances2):
            changes.append(f"{prefix} instance count: {len(instances1)} → {len(instances2)}")
            return changes[:5]

        for i, (inst1, inst2) in enumerate(list(zip(instances1, instances2))[:10]):
            inst_prefix = f"{prefix}.Instance[{i}]"

            if hasattr(inst1, 'type_id') and hasattr(inst2, 'type_id'):
                if inst1.type_id != inst2.type_id:
                    changes.append(f"{inst_prefix} type_id: {inst1.type_id} → {inst2.type_id}")

            if hasattr(inst1, 'data') and hasattr(inst2, 'data'):
                if isinstance(inst1.data, dict) and isinstance(inst2.data, dict):
                    field_changes = self.compare_parsed_data(inst1.data, inst2.data, inst_prefix, depth, max_changes=3, in_embedded=in_embedded)
                    changes.extend(field_changes)

        return changes[:10]

    def compare_all_embedded_rsz(self, rsz1: RszFile, rsz2: RszFile) -> List[str]:

        changes = []

        has_embedded1 = hasattr(rsz1, 'has_embedded_rsz') and rsz1.has_embedded_rsz
        has_embedded2 = hasattr(rsz2, 'has_embedded_rsz') and rsz2.has_embedded_rsz

        if has_embedded1 != has_embedded2:
            if has_embedded1:
                changes.append("File 1 has embedded RSZ data, File 2 does not")
            else:
                changes.append("File 2 has embedded RSZ data, File 1 does not")
            return changes

        if not has_embedded1:
            return changes

        if hasattr(rsz1, 'rsz_userdata_infos') and hasattr(rsz2, 'rsz_userdata_infos'):
            userdata1 = rsz1.rsz_userdata_infos
            userdata2 = rsz2.rsz_userdata_infos

            if len(userdata1) != len(userdata2):
                changes.append(f"Embedded RSZ count: {len(userdata1)} → {len(userdata2)}")

            min_len = min(len(userdata1), len(userdata2))
            for i in range(min_len):
                ud1 = userdata1[i]
                ud2 = userdata2[i]

                ud_changes = self.compare_single_userdata_deep(ud1, ud2, rsz1, rsz2, i)
                changes.extend(ud_changes)

                if len(changes) > 200:
                    changes.append("... too many embedded changes to list all")
                    break

        embedded_instance_ids = set()

        for inst_id, data in rsz1.parsed_elements.items():
            if self.instance_has_embedded_data(data):
                embedded_instance_ids.add(inst_id)

        for inst_id, data in rsz2.parsed_elements.items():
            if self.instance_has_embedded_data(data):
                embedded_instance_ids.add(inst_id)

        return changes[:200]

    def compare_single_userdata_deep(self, ud1, ud2, rsz1: RszFile, rsz2: RszFile, index: int) -> List[str]:

        changes = []

        inst_id1 = getattr(ud1, 'instance_id', -1)
        inst_id2 = getattr(ud2, 'instance_id', -1)

        is_nested = isinstance(index, str) and '.nested[' in str(index)

        if is_nested:
            if inst_id1 != inst_id2:
                changes.append(f"Embedded[{index}] instance_id: {inst_id1} → {inst_id2}")
                return changes
        else:
            if inst_id1 >= 0 and inst_id2 >= 0:
                expected_id2 = inst_id1 + getattr(self, 'instance_offset', 0)
                if inst_id2 != expected_id2:
                    changes.append(f"Embedded[{index}] instance_id: {inst_id1} → {inst_id2} (real change)")
                    return changes
            elif inst_id1 != inst_id2:
                changes.append(f"Embedded[{index}] instance_id: {inst_id1} → {inst_id2}")
                return changes

        if hasattr(ud1, 'embedded_instances') and hasattr(ud2, 'embedded_instances'):
            emb_inst1 = ud1.embedded_instances
            emb_inst2 = ud2.embedded_instances

            if emb_inst1 and emb_inst2:

                ids1 = sorted(emb_inst1.keys())
                ids2 = sorted(emb_inst2.keys())

                if len(ids1) != len(ids2):
                    changes.append(f"Embedded[{index}] instance count: {len(ids1)} → {len(ids2)}")

                for i in range(min(len(ids1), len(ids2))):
                    id1 = ids1[i]
                    id2 = ids2[i]
                    data1 = emb_inst1[id1]
                    data2 = emb_inst2[id2]

                    if isinstance(data1, dict) and isinstance(data2, dict):
                        field_changes = self.compare_parsed_data(data1, data2, f"Embedded[{index}].inst[{i}]", 0, max_changes=5, in_embedded=True)
                        if field_changes:
                            changes.extend(field_changes[:3])

                if len(ids1) > len(ids2):
                    for i in range(len(ids2), len(ids1)):
                        changes.append(f"Embedded[{index}].inst[{i}] removed in file 2")
                elif len(ids2) > len(ids1):
                    for i in range(len(ids1), len(ids2)):
                        changes.append(f"Embedded[{index}].inst[{i}] added in file 2")

        if hasattr(ud1, 'embedded_userdata_infos') and hasattr(ud2, 'embedded_userdata_infos'):
            nested1 = ud1.embedded_userdata_infos
            nested2 = ud2.embedded_userdata_infos

            if nested1 and nested2:
                min_len = min(len(nested1), len(nested2))
                if len(nested1) != len(nested2):
                    changes.append(f"Embedded[{index}] nested count: {len(nested1)} → {len(nested2)}")

                for j in range(min_len):
                    nested_changes = self.compare_single_userdata_deep(
                        nested1[j], nested2[j], rsz1, rsz2, f"{index}.nested[{j}]"
                    )
                    changes.extend(nested_changes)

        return changes

    def compare_embedded_instances_data(self, emb1, emb2, rsz1: RszFile, rsz2: RszFile, prefix: str) -> List[str]:

        changes = []

        if isinstance(emb1, dict) and isinstance(emb2, dict):
            all_keys = set(emb1.keys()) | set(emb2.keys())

            for key in sorted(all_keys):
                if key not in emb1:
                    changes.append(f"{prefix}.inst[{key}] added in file 2")
                elif key not in emb2:
                    changes.append(f"{prefix}.inst[{key}] removed in file 2")
                else:
                    inst1 = emb1[key]
                    inst2 = emb2[key]
                    inst_changes = self.compare_single_embedded_instance(inst1, inst2, rsz1, rsz2, f"{prefix}.inst[{key}]")
                    changes.extend(inst_changes)

        elif isinstance(emb1, list) and isinstance(emb2, list):
            if len(emb1) != len(emb2):
                changes.append(f"{prefix} instance count: {len(emb1)} → {len(emb2)}")

            for i, (inst1, inst2) in enumerate(zip(emb1, emb2)):
                inst_changes = self.compare_single_embedded_instance(inst1, inst2, rsz1, rsz2, f"{prefix}.inst[{i}]")
                changes.extend(inst_changes)

        return changes

    def compare_single_embedded_instance(self, inst1, inst2, rsz1: RszFile, rsz2: RszFile, prefix: str) -> List[str]:

        changes = []

        type1 = getattr(inst1, 'type_id', None)
        type2 = getattr(inst2, 'type_id', None)

        if type1 != type2:
            changes.append(f"{prefix} type: {type1} → {type2}")
            return changes

        data1 = None
        data2 = None

        if hasattr(inst1, 'data'):
            data1 = inst1.data
        if hasattr(inst2, 'data'):
            data2 = inst2.data

        if data1 is None and hasattr(inst1, 'instance_id'):
            inst_id = inst1.instance_id
            if inst_id and inst_id >= 0:
                data1 = rsz1.parsed_elements.get(inst_id)

        if data2 is None and hasattr(inst2, 'instance_id'):
            inst_id = inst2.instance_id
            if inst_id and inst_id >= 0:
                data2 = rsz2.parsed_elements.get(inst_id)

        if data1 is None and hasattr(inst1, 'fields'):
            data1 = inst1.fields
        if data2 is None and hasattr(inst2, 'fields'):
            data2 = inst2.fields

        if data1 is None and hasattr(inst1, '__dict__'):
            data1 = {k: v for k, v in inst1.__dict__.items()
                    if not k.startswith('_') and k not in ['type_id', 'instance_id']}
        if data2 is None and hasattr(inst2, '__dict__'):
            data2 = {k: v for k, v in inst2.__dict__.items()
                    if not k.startswith('_') and k not in ['type_id', 'instance_id']}

        if data1 and data2:
            if isinstance(data1, dict) and isinstance(data2, dict):
                field_changes = self.compare_parsed_data(data1, data2, prefix, 0, max_changes=10, in_embedded=True)
                if field_changes:
                    type_name = f"Type_{type1}" if type1 else "Unknown"
                    if rsz1.type_registry and type1:
                        type_info = rsz1.type_registry.get_type_info(type1)
                        if type_info:
                            type_name = type_info.get('name', type_name)

                    changes.append(f"{prefix} ({type_name}):")
                    for fc in field_changes[:5]:
                        changes.append(f"  • {fc}")
            else:
                if data1 != data2:
                    changes.append(f"{prefix} data changed")
        elif data1 and not data2:
            changes.append(f"{prefix} data removed in file 2")
        elif not data1 and data2:
            changes.append(f"{prefix} data added in file 2")

        return changes

    def instance_has_embedded_data(self, data: dict) -> bool:

        if not isinstance(data, dict):
            return False

        embedded_keywords = ['embedded', 'userdata', 'rsz', 'binary', 'serialized']

        for field_name in data.keys():
            field_lower = field_name.lower()
            if any(keyword in field_lower for keyword in embedded_keywords):
                return True

            field_val = data[field_name]
            if hasattr(field_val, '__class__'):
                class_name = field_val.__class__.__name__.lower()
                if any(keyword in class_name for keyword in embedded_keywords):
                    return True

        return False

    def compare_embedded_instance_fields(self, data1: dict, data2: dict, inst_id: int, rsz1: RszFile, rsz2: RszFile) -> List[str]:

        changes = []
        inst_name = self.get_instance_name(rsz1, inst_id)

        for field_name in data1.keys() | data2.keys():
            if 'embedded' in field_name.lower() or 'userdata' in field_name.lower():
                val1 = data1.get(field_name)
                val2 = data2.get(field_name)

                if val1 != val2:
                    if val1 is None:
                        changes.append(f"[{inst_name}] {field_name} added in file 2")
                    elif val2 is None:
                        changes.append(f"[{inst_name}] {field_name} removed in file 2")
                    else:
                        field_changes = self.compare_field_values(val1, val2, field_name, 0, in_embedded=True)
                        if field_changes:
                            changes.append(f"[{inst_name}] {field_name}:")
                            for fc in field_changes[:3]:
                                changes.append(f"  • {fc}")

        return changes

    def get_nested_embedded_instance_changes(self, ud1, ud2, rsz1: RszFile, rsz2: RszFile) -> Dict:

        nested_changes = {}
        index_str = "nested"

        if hasattr(ud1, 'embedded_instances') and hasattr(ud2, 'embedded_instances'):
            emb1 = ud1.embedded_instances
            emb2 = ud2.embedded_instances

            if emb1 and emb2:
                instances_to_check = []

                if isinstance(emb1, dict) and isinstance(emb2, dict):
                    for key in set(emb1.keys()) & set(emb2.keys()):
                        instances_to_check.append((emb1[key], emb2[key]))
                elif isinstance(emb1, list) and isinstance(emb2, list):
                    for inst1, inst2 in zip(emb1, emb2):
                        instances_to_check.append((inst1, inst2))

                for inst1, inst2 in instances_to_check:
                    inst_id1 = getattr(inst1, 'instance_id', None)
                    inst_id2 = getattr(inst2, 'instance_id', None)

                    if inst_id1 and inst_id2 and inst_id1 == inst_id2:
                        data1 = rsz1.parsed_elements.get(inst_id1, {})
                        data2 = rsz2.parsed_elements.get(inst_id2, {})

                        if data1 and data2:
                            field_changes = self.compare_parsed_data(data1, data2, f"Embedded[{index_str}]", 0, max_changes=10, in_embedded=True)
                            if field_changes:
                                inst_name = self.get_instance_name(rsz1, inst_id1)
                                nested_changes[inst_id1] = {
                                    'name': inst_name,
                                    'changes': field_changes
                                }

                    if hasattr(inst1, 'data') and hasattr(inst2, 'data'):
                        if isinstance(inst1.data, dict) and isinstance(inst2.data, dict):
                            field_changes = self.compare_parsed_data(inst1.data, inst2.data, f"Embedded[{index_str}]", 0, max_changes=10, in_embedded=True)
                            if field_changes:
                                type_id = getattr(inst1, 'type_id', 'Unknown')
                                nested_changes[f"embedded_{type_id}"] = {
                                    'name': f"Embedded Type {type_id}",
                                    'changes': field_changes
                                }

        if hasattr(ud1, 'nested_userdata') and hasattr(ud2, 'nested_userdata'):
            nested1 = ud1.nested_userdata
            nested2 = ud2.nested_userdata

            if nested1 and nested2:
                if isinstance(nested1, list) and isinstance(nested2, list):
                    for n1, n2 in zip(nested1, nested2):
                        sub_changes = self.get_nested_embedded_instance_changes(n1, n2, rsz1, rsz2)
                        nested_changes.update(sub_changes)
                elif not isinstance(nested1, list) and not isinstance(nested2, list):
                    sub_changes = self.get_nested_embedded_instance_changes(nested1, nested2, rsz1, rsz2)
                    nested_changes.update(sub_changes)

        if hasattr(ud1, 'referenced_instance_id') and hasattr(ud2, 'referenced_instance_id'):
            ref_id1 = ud1.referenced_instance_id
            ref_id2 = ud2.referenced_instance_id

            if ref_id1 and ref_id2 and ref_id1 == ref_id2 and ref_id1 > 0:
                ref_data1 = rsz1.parsed_elements.get(ref_id1, {})
                ref_data2 = rsz2.parsed_elements.get(ref_id2, {})

                if ref_data1 and ref_data2:
                    field_changes = self.compare_parsed_data(ref_data1, ref_data2, f"Embedded[{index_str}]", 0, max_changes=10, in_embedded=True)
                    if field_changes:
                        inst_name = self.get_instance_name(rsz1, ref_id1)
                        nested_changes[ref_id1] = {
                            'name': inst_name,
                            'changes': field_changes
                        }

        return nested_changes

    def compare_userdata_embedded_instances(self, ud1, ud2, rsz1: RszFile, rsz2: RszFile, ud_index) -> List[str]:

        changes = []

        index_str = str(ud_index)

        if hasattr(ud1, 'instance_id') and hasattr(ud2, 'instance_id'):
            if ud1.instance_id >= 0 and ud2.instance_id >= 0:
                expected_id = ud1.instance_id + getattr(self, 'instance_offset', 0)
                if ud2.instance_id != expected_id:
                    changes.append(f"Embedded[{index_str}] instance_id: {ud1.instance_id} → {ud2.instance_id} (real change)")
            elif ud1.instance_id != ud2.instance_id:
                changes.append(f"Embedded[{index_str}] instance_id: {ud1.instance_id} → {ud2.instance_id}")

        if hasattr(ud1, '__dict__') and hasattr(ud2, '__dict__'):
            attrs1 = ud1.__dict__
            attrs2 = ud2.__dict__

            all_keys = set(attrs1.keys()) | set(attrs2.keys())
            for key in all_keys:
                if key.startswith('_'):
                    continue

                val1 = attrs1.get(key)
                val2 = attrs2.get(key)

                if key in ['instance_id', 'embedded_rsz_header', 'embedded_instances',
                          'referenced_instance_id', 'nested_userdata']:
                    continue

                if val1 != val2:
                    if isinstance(val1, (int, float, str, bool)) and isinstance(val2, (int, float, str, bool)):
                        changes.append(f"Embedded[{index_str}].{key}: {val1} → {val2}")
                    elif val1 is None or val2 is None:
                        if val1 is None:
                            changes.append(f"Embedded[{index_str}].{key}: None → {type(val2).__name__}")
                        else:
                            changes.append(f"Embedded[{index_str}].{key}: {type(val1).__name__} → None")
                    else:
                        attr_changes = self.traverse_and_compare_embedded_data(val1, val2, f"Embedded[{index_str}].{key}", rsz1, rsz2, in_embedded=True)
                        changes.extend(attr_changes[:3])

        if hasattr(ud1, 'embedded_rsz_header') and hasattr(ud2, 'embedded_rsz_header'):
            header1 = ud1.embedded_rsz_header
            header2 = ud2.embedded_rsz_header

            if header1 and header2:
                inst_count1 = getattr(header1, 'instance_count', 0)
                inst_count2 = getattr(header2, 'instance_count', 0)

                if inst_count1 != inst_count2:
                    changes.append(f"Embedded[{index_str}] instance count: {inst_count1} → {inst_count2}")

                if hasattr(ud1, 'embedded_instances') and hasattr(ud2, 'embedded_instances'):
                    emb_instances1 = ud1.embedded_instances
                    emb_instances2 = ud2.embedded_instances

                    if emb_instances1 and emb_instances2:
                        instances_to_compare = []

                        if isinstance(emb_instances1, dict) and isinstance(emb_instances2, dict):
                            all_keys = set(emb_instances1.keys()) | set(emb_instances2.keys())
                            for j in sorted(all_keys):
                                if j not in emb_instances1:
                                    changes.append(f"Embedded[{index_str}].inst[{j}] exists only in file 2")
                                elif j not in emb_instances2:
                                    changes.append(f"Embedded[{index_str}].inst[{j}] exists only in file 1")
                                else:
                                    instances_to_compare.append((j, emb_instances1[j], emb_instances2[j]))
                        elif isinstance(emb_instances1, list) and isinstance(emb_instances2, list):
                            min_inst = min(len(emb_instances1), len(emb_instances2))
                            for j in range(min_inst):
                                instances_to_compare.append((j, emb_instances1[j], emb_instances2[j]))
                            if len(emb_instances1) != len(emb_instances2):
                                changes.append(f"Embedded[{index_str}] instance count: {len(emb_instances1)} → {len(emb_instances2)}")
                        else:
                            changes.append(f"Embedded[{index_str}] instances structure mismatch")

                        for j, inst1, inst2 in instances_to_compare:
                            type_id1 = getattr(inst1, 'type_id', None)
                            type_id2 = getattr(inst2, 'type_id', None)

                            if type_id1 != type_id2:
                                changes.append(f"Embedded[{index_str}].inst[{j}] type: {type_id1} → {type_id2}")
                                continue

                            data1 = getattr(inst1, 'data', None)
                            data2 = getattr(inst2, 'data', None)

                            if data1 is None:
                                if hasattr(inst1, 'instance_id'):
                                    data1 = rsz1.parsed_elements.get(inst1.instance_id, None)
                                if data1 is None and hasattr(inst1, 'fields'):
                                    data1 = inst1.fields
                                if data1 is None and hasattr(inst1, '__dict__'):
                                    data1 = inst1.__dict__

                            if data2 is None:
                                if hasattr(inst2, 'instance_id'):
                                    data2 = rsz2.parsed_elements.get(inst2.instance_id, None)
                                if data2 is None and hasattr(inst2, 'fields'):
                                    data2 = inst2.fields
                                if data2 is None and hasattr(inst2, '__dict__'):
                                    data2 = inst2.__dict__

                            if data1 and data2:
                                if isinstance(data1, dict) and isinstance(data2, dict):
                                    field_changes = self.compare_parsed_data(data1, data2, f"Embedded[{index_str}].inst[{j}]", 0, max_changes=5, in_embedded=True)
                                    changes.extend(field_changes)
                                else:
                                    field_changes = self.traverse_and_compare_embedded_data(data1, data2, f"Embedded[{index_str}].inst[{j}]", rsz1, rsz2, in_embedded=True)
                                    changes.extend(field_changes)

        if hasattr(ud1, 'referenced_instance_id') and hasattr(ud2, 'referenced_instance_id'):
            ref_id1 = ud1.referenced_instance_id
            ref_id2 = ud2.referenced_instance_id

            if ref_id1 > 0 and hasattr(self, 'instance_offset'):
                expected_ref_id2 = ref_id1 + self.instance_offset
                if ref_id2 != expected_ref_id2:
                    changes.append(f"Embedded[{index_str}] referenced_instance_id changed: {ref_id1} → {ref_id2}")
                    return changes

            if ref_id1 and ref_id2 and ref_id1 > 0 and ref_id2 > 0:
                ref_data1 = rsz1.parsed_elements.get(ref_id1, {})
                ref_data2 = rsz2.parsed_elements.get(ref_id2, {})

                if ref_data1 and ref_data2:
                    field_changes = self.compare_parsed_data(ref_data1, ref_data2, f"Embedded[{index_str}].ref[{ref_id1}]", 0, max_changes=10, in_embedded=True)
                    changes.extend(field_changes)
                elif ref_data1 and not ref_data2:
                    changes.append(f"Embedded[{index_str}] referenced instance {ref_id1} exists only in file 1")
                elif not ref_data1 and ref_data2:
                    changes.append(f"Embedded[{index_str}] referenced instance {ref_id2} exists only in file 2")

        if hasattr(ud1, 'nested_userdata') and hasattr(ud2, 'nested_userdata'):
            nested1 = ud1.nested_userdata
            nested2 = ud2.nested_userdata

            if nested1 and nested2:
                if isinstance(nested1, list) and isinstance(nested2, list):
                    for k, (n1, n2) in enumerate(zip(nested1, nested2)):
                        nested_changes = self.compare_userdata_embedded_instances(n1, n2, rsz1, rsz2, f"{ud_index}.nested[{k}]")
                        changes.extend(nested_changes)
                else:
                    nested_changes = self.compare_userdata_embedded_instances(nested1, nested2, rsz1, rsz2, f"{ud_index}.nested")
                    changes.extend(nested_changes)

        return changes

    def compare_embedded_instance_data(self, userdata_list1, userdata_list2, rsz1, rsz2, parent_inst_id: int) -> List[str]:

        changes = []

        for i, (ud1, ud2) in enumerate(zip(userdata_list1, userdata_list2)):

            if hasattr(ud1, 'hash') and hasattr(ud2, 'hash'):
                if ud1.hash != ud2.hash:
                    changes.append(f"embedded[{i}] hash changed")

            str1 = rsz1._rsz_userdata_str_map.get(ud1, "") if hasattr(rsz1, '_rsz_userdata_str_map') else ""
            str2 = rsz2._rsz_userdata_str_map.get(ud2, "") if hasattr(rsz2, '_rsz_userdata_str_map') else ""

            if str1 != str2:
                if len(str1) > 30 or len(str2) > 30:
                    changes.append(f"embedded[{i}] data changed")
                else:
                    changes.append(f"embedded[{i}]: '{str1}' → '{str2}'")

            if hasattr(ud1, 'referenced_instance_id') and hasattr(ud2, 'referenced_instance_id'):
                ref_id1 = ud1.referenced_instance_id
                ref_id2 = ud2.referenced_instance_id

                if ref_id1 != ref_id2:
                    changes.append(f"embedded[{i}] references different instance: {ref_id1} → {ref_id2}")
                elif ref_id1 >= 0:
                    data1 = rsz1.parsed_elements.get(ref_id1, {})
                    data2 = rsz2.parsed_elements.get(ref_id2, {})

                    if data1 and data2:
                        field_changes = self.compare_parsed_data(data1, data2, f"embedded[{i}]", 0, max_changes=2)
                        changes.extend(field_changes)

        return changes

    def compare_all_instances(self, rsz1: RszFile, rsz2: RszFile) -> List[Dict]:
        instance_diffs = []

        if not (hasattr(rsz1, 'parsed_elements') and hasattr(rsz2, 'parsed_elements')):
            return instance_diffs

        go_instance_ids = set()
        user_root_ids = set()

        if rsz1.is_usr and rsz2.is_usr:
            if getattr(rsz1, 'object_table', None):
                root_id = next((obj_id for obj_id in rsz1.object_table if obj_id > 0), None)
                if root_id is not None:
                    user_root_ids.add(root_id)
            if getattr(rsz2, 'object_table', None):
                root_id = next((obj_id for obj_id in rsz2.object_table if obj_id > 0), None)
                if root_id is not None:
                    user_root_ids.add(root_id)

        for rsz in [rsz1, rsz2]:
            if hasattr(rsz, 'object_table'):
                for obj_id in rsz.object_table:
                    if obj_id > 0 and obj_id not in user_root_ids:
                        go_instance_ids.add(obj_id)

        if not hasattr(self, 'general_offset'):
            self.general_offset = getattr(self, 'instance_offset', 0)

        checked_ids = set()

        for inst_id1 in sorted(set(rsz1.parsed_elements.keys())):
            if inst_id1 in go_instance_ids or inst_id1 == 0:
                continue

            inst_id2 = inst_id1 + self.general_offset

            data1 = rsz1.parsed_elements.get(inst_id1, {})
            data2 = rsz2.parsed_elements.get(inst_id2, {})

            checked_ids.add(inst_id1)
            checked_ids.add(inst_id2)

            if data1 and not data2:
                name = self.get_instance_name(rsz1, inst_id1)
                instance_diffs.append({
                    'id': inst_id1,
                    'name': name,
                    'details': f"Instance removed (was at ID {inst_id1})"
                })
            elif data1 and data2:
                field_changes = self.compare_parsed_data(data1, data2, f"Instance[{inst_id1}]", 0, max_changes=5)
                if field_changes:
                    name = self.get_instance_name(rsz1, inst_id1)
                    details = "\n".join(f"• {fc}" for fc in field_changes[:3])
                    instance_diffs.append({
                        'id': inst_id1,
                        'name': name,
                        'details': details
                    })

        for inst_id2 in sorted(set(rsz2.parsed_elements.keys())):
            if inst_id2 in go_instance_ids or inst_id2 == 0 or inst_id2 in checked_ids:
                continue

            inst_id1 = inst_id2 - self.general_offset
            if inst_id1 not in rsz1.parsed_elements:
                name = self.get_instance_name(rsz2, inst_id2)
                instance_diffs.append({
                    'id': inst_id2,
                    'name': name,
                    'details': f"Instance added (at ID {inst_id2})"
                })

            if len(instance_diffs) >= 50:
                break

        return instance_diffs

    def get_instance_name(self, rsz_file: RszFile, instance_id: int) -> str:

        if instance_id < 0 or instance_id >= len(rsz_file.instance_infos):
            return "Unknown"

        inst_info = rsz_file.instance_infos[instance_id]
        type_name = self.get_type_name(rsz_file, inst_info)

        parsed_data = rsz_file.parsed_elements.get(instance_id, {})
        if parsed_data:
            for field_name in ['m_Name', 'name', 'Name']:
                if field_name in parsed_data:
                    field_val = parsed_data[field_name]
                    if hasattr(field_val, 'value'):
                        return f"{field_val.value} ({type_name})"

        return type_name

    def get_type_name(self, rsz_file: RszFile, inst_info) -> str:
        if hasattr(rsz_file, 'type_registry') and rsz_file.type_registry:
            type_info = rsz_file.type_registry.get_type_info(inst_info.type_id)
            if type_info:
                return type_info.get('name', 'Unknown')
        return f"TypeID_{inst_info.type_id}"

    def compare_parsed_data(self, data1: dict, data2: dict, path: str, depth: int, max_changes: int = 10, in_embedded: bool = False) -> List[str]:
        changes = []
        if depth > 5:
            return changes

        all_fields = set(data1.keys()) | set(data2.keys())
        sorted_fields = sorted(all_fields)

        for idx, field in enumerate(sorted_fields):
            if field in ['m_GameObject', 'm_Transform', 'm_Parent', 'm_Children']:
                continue

            field_path = f"{path}.{field}"

            if field not in data1:
                val2_str = self.get_field_value_string(data2[field])
                changes.append(f"{field_path}: [missing] → {val2_str}")
            elif field not in data2:
                val1_str = self.get_field_value_string(data1[field])
                changes.append(f"{field_path}: {val1_str} → [missing]")
            else:
                field_changes = self.compare_field_values(data1[field], data2[field], field_path, depth + 1, in_embedded)
                changes.extend(field_changes)

            if len(changes) >= max_changes:
                remaining = len(sorted_fields) - idx - 1
                if remaining > 0:
                    changes.append(f"... and {remaining} more field(s)")
                break

        return changes[:max_changes]

    def compare_instances(self, inst1, inst2, path: str, depth: int = 0, max_depth: int = 5) -> List[str]:
        changes = []

        if depth > max_depth:
            return changes

        type_name1 = inst1.type_info.name if hasattr(inst1, 'type_info') else 'Unknown'
        type_name2 = inst2.type_info.name if hasattr(inst2, 'type_info') else 'Unknown'

        if type_name1 != type_name2:
            changes.append(f"{path} type: {type_name1} → {type_name2}")
            return changes[:10]

        if hasattr(inst1, 'data') and hasattr(inst2, 'data'):
            field_changes = self.compare_data_fields(inst1.data, inst2.data, path, depth)
            changes.extend(field_changes)

        return changes[:20]

    def compare_data_fields(self, data1, data2, path: str, depth: int, in_embedded: bool = False) -> List[str]:
        changes = []

        if hasattr(data1, 'fields') and hasattr(data2, 'fields'):
            fields1 = data1.fields
            fields2 = data2.fields

            all_fields = set(fields1.keys()) | set(fields2.keys())

            for field in sorted(all_fields):
                if field in ['m_GameObject', 'm_Transform', 'm_Parent', 'm_Children']:
                    continue

                field_path = f"{path}.{field}"
                val1 = fields1.get(field)
                val2 = fields2.get(field)

                if field not in fields1:
                    changes.append(f"{field_path}: [missing] → {self.get_field_value_string(val2)}")
                elif field not in fields2:
                    changes.append(f"{field_path}: {self.get_field_value_string(val1)} → [missing]")
                else:
                    field_changes = self.compare_field_values(val1, val2, field_path, depth + 1, in_embedded)
                    changes.extend(field_changes)

        return changes

    def compare_field_values(self, val1, val2, path: str, depth: int, in_embedded: bool = False) -> List[str]:
        changes = []

        if val1 == val2:
            return changes

        if type(val1).__name__ != type(val2).__name__:
            changes.append(f"{path}: type changed from {type(val1).__name__} to {type(val2).__name__}")
            return changes

        from file_handlers.rsz.rsz_data_types import (
            ArrayData, StructData, ObjectData, ResourceData, UserDataData,
            BoolData, StringData, Float2Data, Float3Data, Float4Data,
            Vec2Data, Vec3Data, Vec3ColorData, Vec4Data, QuaternionData, Mat4Data,
            Int2Data, Int3Data, Int4Data, Uint2Data, Uint3Data,
            S8Data, U8Data, S16Data, U16Data, S32Data, U32Data, S64Data, U64Data,
            F32Data, F64Data, GuidData, GameObjectRefData, ColorData,
            PositionData, RangeData, RangeIData, OBBData, AABBData, CapsuleData,
            AreaData, ConeData, LineSegmentData, PointData, SizeData, SphereData,
            CylinderData, RectData, RuntimeTypeData, RawBytesData
        )

        if isinstance(val1, (BoolData, S8Data, U8Data, S16Data, U16Data, S32Data, U32Data,
                            S64Data, U64Data, F32Data, F64Data)):
            if val1.value != val2.value:
                changes.append(f"{path}: {self.format_value(val1.value)} → {self.format_value(val2.value)}")

        elif isinstance(val1, StringData) and val1.value != val2.value:
            changes.append(f'{path}: "{val1.value}" → "{val2.value}"')

        elif isinstance(val1, ObjectData):
            if not in_embedded and hasattr(self, 'instance_offset') and val1.value >= 0:
                if val2.value == val1.value + self.instance_offset:
                    return changes
            if val1.value != val2.value:
                changes.append(f"{path}: Object[{val1.value}] → Object[{val2.value}]")

        elif isinstance(val1, GameObjectRefData) and val1.guid_str != val2.guid_str:
            changes.append(f"{path}: GameObject[{val1.guid_str}] → GameObject[{val2.guid_str}]")

        elif isinstance(val1, ResourceData) and val1.value != val2.value:
            changes.append(f'{path}: Resource "{val1.value}" → "{val2.value}"')

        elif isinstance(val1, UserDataData):
            if not in_embedded and hasattr(self, 'instance_offset') and val1.value >= 0 and val1.string == val2.string:
                if val2.value == val1.value + self.instance_offset:
                    return changes
            if val1.value != val2.value or val1.string != val2.string:
                changes.append(f'{path}: UserData[{val1.value}]:"{val1.string}" → [{val2.value}]:"{val2.string}"')

        elif isinstance(val1, GuidData) and val1.guid_str != val2.guid_str:
            changes.append(f"{path}: GUID {val1.guid_str} → {val2.guid_str}")

        elif isinstance(val1, (Float2Data, Vec2Data, Int2Data, Uint2Data)):
            if val1.x != val2.x or val1.y != val2.y:
                changes.append(f"{path}: ({val1.x}, {val1.y}) → ({val2.x}, {val2.y})")

        elif isinstance(val1, (Float3Data, Vec3Data, PositionData, Int3Data, Uint3Data, PointData)):
            if val1.x != val2.x or val1.y != val2.y or val1.z != val2.z:
                changes.append(f"{path}: ({val1.x}, {val1.y}, {val1.z}) → ({val2.x}, {val2.y}, {val2.z})")

        elif isinstance(val1, (Float4Data, Vec4Data, QuaternionData, Int4Data)):
            if val1.x != val2.x or val1.y != val2.y or val1.z != val2.z or val1.w != val2.w:
                changes.append(f"{path}: ({val1.x}, {val1.y}, {val1.z}, {val1.w}) → ({val2.x}, {val2.y}, {val2.z}, {val2.w})")
                
        elif isinstance(val1, ColorData):
            if val1.r != val2.r or val1.g != val2.g or val1.b != val2.b or val1.a != val2.a:
                changes.append(f"{path}: ({val1.r}, {val1.g}, {val1.b}, {val1.a}) → ({val2.r}, {val2.g}, {val2.b}, {val2.a})")

        elif isinstance(val1, Mat4Data):
            for i in range(16):
                if val1.values[i] != val2.values[i]:
                    changes.append(f"{path}: Matrix changed at index {i}: {val1.values[i]} → {val2.values[i]}")
                    break

        elif isinstance(val1, (RangeData, RangeIData)):
            if val1.min != val2.min or val1.max != val2.max:
                changes.append(f"{path}: Range[{val1.min}, {val1.max}] → [{val2.min}, {val2.max}]")

        elif isinstance(val1, AABBData):
            if (val1.min.x != val2.min.x or val1.min.y != val2.min.y or val1.min.z != val2.min.z or
                val1.max.x != val2.max.x or val1.max.y != val2.max.y or val1.max.z != val2.max.z):
                changes.append(f"{path}: AABB[({val1.min.x}, {val1.min.y}, {val1.min.z})-({val1.max.x}, {val1.max.y}, {val1.max.z})] → [({val2.min.x}, {val2.min.y}, {val2.min.z})-({val2.max.x}, {val2.max.y}, {val2.max.z})]")

        elif isinstance(val1, OBBData):
            if any(val1.values[i] != val2.values[i] for i in range(20)):
                changes.append(f"{path}: OBB data changed")

        elif isinstance(val1, CapsuleData):
            if (val1.start.x != val2.start.x or val1.start.y != val2.start.y or val1.start.z != val2.start.z or
                val1.end.x != val2.end.x or val1.end.y != val2.end.y or val1.end.z != val2.end.z or
                val1.radius != val2.radius):
                changes.append(f"{path}: Capsule changed")
                
        elif isinstance(val1, SphereData):
            if (val1.center.x != val2.center.x or val1.center.y != val2.center.y or val1.center.z != val2.center.z or
                val1.radius != val2.radius):
                changes.append(f"{path}: Sphere[({val1.center.x}, {val1.center.y}, {val1.center.z}), r={val1.radius}] → [({val2.center.x}, {val2.center.y}, {val2.center.z}), r={val2.radius}]")
                
        elif isinstance(val1, CylinderData):
            if (val1.center.x != val2.center.x or val1.center.y != val2.center.y or val1.center.z != val2.center.z or
                val1.radius != val2.radius or val1.height != val2.height):
                changes.append(f"{path}: Cylinder changed")
                
        elif isinstance(val1, ConeData):
            if (val1.position.x != val2.position.x or val1.position.y != val2.position.y or val1.position.z != val2.position.z or
                val1.direction.x != val2.direction.x or val1.direction.y != val2.direction.y or val1.direction.z != val2.direction.z or
                val1.angle != val2.angle or val1.distance != val2.distance):
                changes.append(f"{path}: Cone changed")
                
        elif isinstance(val1, LineSegmentData):
            if (val1.start.x != val2.start.x or val1.start.y != val2.start.y or val1.start.z != val2.start.z or
                val1.end.x != val2.end.x or val1.end.y != val2.end.y or val1.end.z != val2.end.z):
                changes.append(f"{path}: LineSegment changed")
                
        elif isinstance(val1, AreaData):
            if (val1.p0.x != val2.p0.x or val1.p0.y != val2.p0.y or
                val1.p1.x != val2.p1.x or val1.p1.y != val2.p1.y or
                val1.p2.x != val2.p2.x or val1.p2.y != val2.p2.y or
                val1.p3.x != val2.p3.x or val1.p3.y != val2.p3.y or
                val1.height != val2.height or val1.bottom != val2.bottom):
                changes.append(f"{path}: Area changed")
                
        elif isinstance(val1, RectData):
            if (val1.min_x != val2.min_x or val1.min_y != val2.min_y or 
                val1.max_x != val2.max_x or val1.max_y != val2.max_y):
                changes.append(f"{path}: Rect[({val1.min_x}, {val1.min_y}), ({val1.max_x}, {val1.max_y})] → [({val2.min_x}, {val2.min_y}), ({val2.max_x}, {val2.max_y})]")
                
        elif isinstance(val1, SizeData):
            if val1.width != val2.width or val1.height != val2.height:
                changes.append(f"{path}: Size[{val1.width}x{val1.height}] → [{val2.width}x{val2.height}]")

        elif isinstance(val1, Vec3ColorData):
            if val1.x != val2.x or val1.y != val2.y or val1.z != val2.z:
                changes.append(f"{path}: Color({val1.x}, {val1.y}, {val1.z}) → ({val2.x}, {val2.y}, {val2.z})")

        elif isinstance(val1, RuntimeTypeData) and val1.value != val2.value:
            changes.append(f"{path}: RuntimeType[{val1.value}] → [{val2.value}]")

        elif isinstance(val1, RawBytesData) and val1.raw_bytes != val2.raw_bytes:
            changes.append(f"{path}: RawBytes[{len(val1.raw_bytes)} bytes] → [{len(val2.raw_bytes)} bytes]")

        elif isinstance(val1, ArrayData):
            if len(val1.values) != len(val2.values):
                changes.append(f"{path}: array size {len(val1.values)} → {len(val2.values)}")
            else:
                for i, (elem1, elem2) in enumerate(list(zip(val1.values, val2.values))[:10]):
                    changes.extend(self.compare_field_values(elem1, elem2, f"{path}[{i}]", depth + 1, in_embedded))

        elif isinstance(val1, StructData):
            if len(val1.values) != len(val2.values):
                changes.append(f"{path}: struct count {len(val1.values)} → {len(val2.values)}")
            else:
                for i, (struct1, struct2) in enumerate(list(zip(val1.values, val2.values))[:5]):
                    for key in set(struct1.keys()) | set(struct2.keys()):
                        if key in struct1 and key in struct2:
                            changes.extend(self.compare_field_values(struct1[key], struct2[key], f"{path}[{i}].{key}", depth + 1, in_embedded))

        elif isinstance(val1, dict) and isinstance(val2, dict):
            changes.extend(self.compare_parsed_data(val1, val2, path, depth, in_embedded=in_embedded))

        else:
            val1_str = self.get_field_value_string(val1)
            val2_str = self.get_field_value_string(val2)
            if val1_str != val2_str:
                changes.append(f"{path}: {val1_str} → {val2_str}")

        return changes

    def compare_arrays(self, arr1, arr2, path: str, depth: int, in_embedded: bool = False) -> List[str]:
        changes = []

        len1 = len(arr1) if hasattr(arr1, '__len__') else 0
        len2 = len(arr2) if hasattr(arr2, '__len__') else 0

        if len1 != len2:
            changes.append(f"{path}: array size {len1} → {len2}")

        min_len = min(len1, len2, 5)
        for i in range(min_len):
            item1 = arr1[i] if i < len1 else None
            item2 = arr2[i] if i < len2 else None
            item_changes = self.compare_field_values(item1, item2, f"{path}[{i}]", depth + 1, in_embedded)
            changes.extend(item_changes)

        return changes

    def format_value(self, value) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, float):
            if abs(value) < 1e-12:
                return "0"
            elif abs(value) < 0.0001 or abs(value) > 100000:
                return f"{value:.6e}"
            else:
                formatted = f"{value:.6f}".rstrip('0').rstrip('.')
                return formatted if formatted else "0"
        if isinstance(value, str):
            return f'"{value[:50]}..."' if len(value) > 50 else f'"{value}"' if value else '""'
        if isinstance(value, int):
            return str(value)
        return str(value)[:100]

    def get_field_value_string(self, value) -> str:
        if value is None:
            return "null"

        from file_handlers.rsz.rsz_data_types import (
            ArrayData, StructData, ObjectData, ResourceData, UserDataData,
            BoolData, StringData, Float2Data, Float3Data, Float4Data,
            Vec2Data, Vec3Data, Vec3ColorData, Vec4Data, QuaternionData,
            Int2Data, Int3Data, Int4Data, Uint2Data, Uint3Data,
            S8Data, U8Data, S16Data, U16Data, S32Data, U32Data, S64Data, U64Data,
            F32Data, F64Data, GuidData, GameObjectRefData, ColorData,
            PositionData, RangeData, RangeIData, AABBData, OBBData, CapsuleData,
            SphereData, CylinderData, ConeData, LineSegmentData, AreaData,
            RectData, PointData, SizeData, RuntimeTypeData, RawBytesData, Mat4Data
        )

        if isinstance(value, (BoolData, S8Data, U8Data, S16Data, U16Data, S32Data, U32Data,
                              S64Data, U64Data, F32Data, F64Data)):
            return self.format_value(value.value)
        elif isinstance(value, StringData):
            return f'"{value.value}"'
        elif isinstance(value, ObjectData):
            return f"Object[{value.value}]"
        elif isinstance(value, GameObjectRefData):
            return f"GameObject[{value.guid_str}]"
        elif isinstance(value, ResourceData):
            return f'Resource: "{value.value}"'
        elif isinstance(value, UserDataData):
            return f'UserData[{value.value}]: "{value.string}"'
        elif isinstance(value, GuidData):
            return f"GUID: {value.guid_str}"
        elif isinstance(value, (Float2Data, Vec2Data, Int2Data, Uint2Data)):
            return f"({value.x}, {value.y})"
        elif isinstance(value, (Float3Data, Vec3Data, PositionData, Int3Data, Uint3Data)):
            return f"({value.x}, {value.y}, {value.z})"
        elif isinstance(value, (Float4Data, Vec4Data, QuaternionData, Int4Data)):
            return f"({value.x}, {value.y}, {value.z}, {value.w})"
        elif isinstance(value, ColorData):
            return f"Color({value.r}, {value.g}, {value.b}, {value.a})"
        elif isinstance(value, (RangeData, RangeIData)):
            return f"Range[{value.min}, {value.max}]"
        elif isinstance(value, AABBData):
            return f"AABB[({value.min.x:.2f}, {value.min.y:.2f}, {value.min.z:.2f})-({value.max.x:.2f}, {value.max.y:.2f}, {value.max.z:.2f})]"
        elif isinstance(value, OBBData):
            return f"OBB[{', '.join(f'{v:.2f}' for v in value.values[:6])}...]"
        elif isinstance(value, CapsuleData):
            return f"Capsule[({value.start.x:.2f}, {value.start.y:.2f}, {value.start.z:.2f})-({value.end.x:.2f}, {value.end.y:.2f}, {value.end.z:.2f}), r={value.radius:.2f}]"
        elif isinstance(value, SphereData):
            return f"Sphere[({value.center.x:.2f}, {value.center.y:.2f}, {value.center.z:.2f}), r={value.radius:.2f}]"
        elif isinstance(value, CylinderData):
            return f"Cylinder[({value.center.x:.2f}, {value.center.y:.2f}, {value.center.z:.2f}), r={value.radius:.2f}, h={value.height:.2f}]"
        elif isinstance(value, ConeData):
            return f"Cone[pos:({value.position.x:.2f}, {value.position.y:.2f}, {value.position.z:.2f}), angle={value.angle:.2f}, dist={value.distance:.2f}]"
        elif isinstance(value, LineSegmentData):
            return f"LineSegment[({value.start.x:.2f}, {value.start.y:.2f}, {value.start.z:.2f})-({value.end.x:.2f}, {value.end.y:.2f}, {value.end.z:.2f})]"
        elif isinstance(value, AreaData):
            return f"Area[p0:({value.p0.x:.2f}, {value.p0.y:.2f}), p1:({value.p1.x:.2f}, {value.p1.y:.2f}), h={value.height:.2f}]"
        elif isinstance(value, RectData):
            return f"Rect[({value.min_x:.2f}, {value.min_y:.2f})-({value.max_x:.2f}, {value.max_y:.2f})]"
        elif isinstance(value, PointData):
            return f"Point({value.x:.2f}, {value.y:.2f}, {value.z:.2f})"
        elif isinstance(value, SizeData):
            return f"Size[{value.width:.2f}x{value.height:.2f}]"
        elif isinstance(value, Vec3ColorData):
            return f"Color({value.x:.3f}, {value.y:.3f}, {value.z:.3f})"
        elif isinstance(value, RuntimeTypeData):
            return f"RuntimeType[{value.value}]"
        elif isinstance(value, RawBytesData):
            return f"RawBytes[{len(value.raw_bytes)} bytes]"
        elif isinstance(value, Mat4Data):
            return "Matrix4x4[16 values]"
        elif isinstance(value, ArrayData):
            return f"Array[{len(value.values)}]"
        elif isinstance(value, StructData):
            return f"Struct[{len(value.values)}]"
        elif isinstance(value, (int, float, bool, str)):
            return self.format_value(value)
        elif hasattr(value, '__class__'):
            return f"<{value.__class__.__name__}>"
        return str(type(value).__name__)

    def get_folders_map(self, rsz_file: RszFile) -> Dict[str, RszFolderInfo]:
        folders = {}

        if hasattr(rsz_file, 'folders'):
            for folder in rsz_file.folders:
                folder_path = self._get_folder_path(rsz_file, folder)
                folders[folder_path] = folder

        return folders

    def _get_folder_path(self, rsz_file: RszFile, folder: RszFolderInfo) -> str:
        if hasattr(folder, 'path'):
            return folder.path

        path_parts = []
        current = folder

        while current:
            if hasattr(current, 'name'):
                path_parts.insert(0, current.name)
            else:
                path_parts.insert(0, f"Folder_{current.id}")

            if hasattr(current, 'parent_id') and current.parent_id >= 0:
                parent = None
                for f in rsz_file.folders:
                    if f.id == current.parent_id:
                        parent = f
                        break
                current = parent
            else:
                break

        return "/" + "/".join(path_parts) if path_parts else "/"

    def compare_gameobjects(self, go_map1: Dict[str, Tuple[RszGameObject, str]],
                          go_map2: Dict[str, Tuple[RszGameObject, str]]) -> List[GameObjectDiff]:
        diffs = []

        guids1 = set(go_map1.keys())
        guids2 = set(go_map2.keys())

        only_in_file1 = guids1 - guids2
        only_in_file2 = guids2 - guids1
        in_both = guids1 & guids2

        for guid in sorted(only_in_file1):
            go, name = go_map1[guid]
            diffs.append(GameObjectDiff(
                guid=guid,
                name=name if not name.startswith("GameObject_") else f"Unnamed {name}",
                status="removed",
                details="GameObject exists only in first file"
            ))

        for guid in sorted(only_in_file2):
            go, name = go_map2[guid]
            diffs.append(GameObjectDiff(
                guid=guid,
                name=name if not name.startswith("GameObject_") else f"Unnamed {name}",
                status="added",
                details="GameObject exists only in second file"
            ))

        for guid in sorted(in_both):
            go1, name1 = go_map1[guid]
            go2, name2 = go_map2[guid]

            if hasattr(go1, 'guid') and hasattr(go2, 'guid'):
                guid1_str = guid_le_to_str(go1.guid)
                guid2_str = guid_le_to_str(go2.guid)
                assert guid == guid1_str == guid2_str, f"GUID mismatch: key={guid}, go1={guid1_str}, go2={guid2_str}"

            self.instance_offset = 0

            if hasattr(self.handler1.rsz_file, 'object_table') and hasattr(self.handler2.rsz_file, 'object_table'):
                if go1.id < len(self.handler1.rsz_file.object_table) and go2.id < len(self.handler2.rsz_file.object_table):
                    go1_inst_id = self.handler1.rsz_file.object_table[go1.id]
                    go2_inst_id = self.handler2.rsz_file.object_table[go2.id]

                    self.instance_offset = go2_inst_id - go1_inst_id

            changes = []

            if name1 != name2:
                changes.append(f"• Name: {name1} → {name2}")

            if go1.parent_id >= 0 and go2.parent_id >= 0:
                go_id_offset = go2.id - go1.id
                expected_parent = go1.parent_id + go_id_offset
                if go2.parent_id != expected_parent:
                    changes.append(f"• Parent ID: {go1.parent_id} → {go2.parent_id} (real change)")
            elif go1.parent_id != go2.parent_id:
                changes.append(f"• Parent ID: {go1.parent_id} → {go2.parent_id}")

            if go1.component_count != go2.component_count:
                changes.append(f"• Component count: {go1.component_count} → {go2.component_count}")

            instance_changes = self.compare_gameobject_instances(go1, go2)
            if instance_changes:
                for change in instance_changes:
                    changes.append(f"• {change}")

            if changes:
                details = "\n".join(changes) if len(changes) > 1 else changes[0].replace("• ", "")
                display_name = name1 if name1 == name2 else f"{name1} → {name2}"
                if display_name.startswith("GameObject_"):
                    display_name = f"Unnamed {display_name}"
                diffs.append(GameObjectDiff(
                    guid=guid,
                    name=display_name,
                    status="modified",
                    details=details
                ))

        return diffs

    def compare_folders(self, folder_map1: Dict[str, RszFolderInfo],
                       folder_map2: Dict[str, RszFolderInfo]) -> List[FolderDiff]:
        diffs = []

        paths1 = set(folder_map1.keys())
        paths2 = set(folder_map2.keys())

        only_in_file1 = paths1 - paths2
        only_in_file2 = paths2 - paths1
        in_both = paths1 & paths2

        for path in only_in_file1:
            diffs.append(FolderDiff(
                path=path,
                status="removed",
                details="Folder exists only in first file"
            ))

        for path in only_in_file2:
            diffs.append(FolderDiff(
                path=path,
                status="added",
                details="Folder exists only in second file"
            ))

        for path in in_both:
            folder1 = folder_map1[path]
            folder2 = folder_map2[path]

            if hasattr(folder1, 'gameobject_count') and hasattr(folder2, 'gameobject_count'):
                if folder1.gameobject_count != folder2.gameobject_count:
                    diffs.append(FolderDiff(
                        path=path,
                        status="modified",
                        details=f"GameObject count changed from {folder1.gameobject_count} to {folder2.gameobject_count}"
                    ))

        return diffs

    def compare(self, file1_data: bytes, file2_data: bytes, file1_path: str = None, file2_path: str = None) -> DiffResult:
        export_path1, export_path2 = self.export_files(file1_data, file2_data)

        rsz1, rsz2 = self.load_rsz_files(file1_data, file2_data, file1_path, file2_path)

        type1 = 'USR' if rsz1.is_usr else 'PFB' if rsz1.is_pfb else 'SCN'
        type2 = 'USR' if rsz2.is_usr else 'PFB' if rsz2.is_pfb else 'SCN'

        if type1 != type2:
            raise ValueError(f"Mismatched RSZ file types: {type1} vs {type2}")

        if hasattr(self, 'general_offset'):
            del self.general_offset
        self.instance_offset = 0

        if rsz1.is_usr and rsz2.is_usr:
            root1 = next((obj_id for obj_id in rsz1.object_table if obj_id > 0), 0) if hasattr(rsz1, 'object_table') else 0
            root2 = next((obj_id for obj_id in rsz2.object_table if obj_id > 0), 0) if hasattr(rsz2, 'object_table') else 0
            self.instance_offset = root2 - root1

        go_map1 = self.get_gameobjects_map(rsz1)
        go_map2 = self.get_gameobjects_map(rsz2)

        folder_map1 = self.get_folders_map(rsz1)
        folder_map2 = self.get_folders_map(rsz2)

        gameobject_diffs = self.compare_gameobjects(go_map1, go_map2)
        folder_diffs = self.compare_folders(folder_map1, folder_map2)

        instance_diffs = self.compare_all_instances(rsz1, rsz2)
        if instance_diffs:
            for diff in instance_diffs[:20]:
                gameobject_diffs.append(GameObjectDiff(
                    guid=f"instance-{diff['id']}",
                    name=f"[Instance {diff['id']}] {diff['name']}",
                    status="modified",
                    details=diff['details']
                ))

        embedded_diffs = self.compare_all_embedded_rsz(rsz1, rsz2)
        if embedded_diffs:
            batch_size = 5
            for i in range(0, len(embedded_diffs), batch_size):
                batch = embedded_diffs[i:i+batch_size]
                details = "\n".join(f"• {diff}" for diff in batch)
                gameobject_diffs.append(GameObjectDiff(
                    guid=f"embedded-rsz-{i//batch_size}",
                    name=f"[Embedded RSZ Data {i//batch_size + 1}]",
                    status="modified",
                    details=details
                ))

        total_instances1 = len(rsz1.instance_infos) if hasattr(rsz1, 'instance_infos') else 0
        total_instances2 = len(rsz2.instance_infos) if hasattr(rsz2, 'instance_infos') else 0

        has_embedded1 = hasattr(rsz1, 'has_embedded_rsz') and rsz1.has_embedded_rsz
        has_embedded2 = hasattr(rsz2, 'has_embedded_rsz') and rsz2.has_embedded_rsz
        embedded_count1 = len(rsz1.rsz_userdata_infos) if hasattr(rsz1, 'rsz_userdata_infos') else 0
        embedded_count2 = len(rsz2.rsz_userdata_infos) if hasattr(rsz2, 'rsz_userdata_infos') else 0

        summary = {
            'gameobjects_added': sum(1 for d in gameobject_diffs if d.status == 'added'),
            'gameobjects_removed': sum(1 for d in gameobject_diffs if d.status == 'removed'),
            'gameobjects_modified': sum(1 for d in gameobject_diffs if d.status == 'modified'),
            'folders_added': sum(1 for d in folder_diffs if d.status == 'added'),
            'folders_removed': sum(1 for d in folder_diffs if d.status == 'removed'),
            'folders_modified': sum(1 for d in folder_diffs if d.status == 'modified'),
            'total_instances1': total_instances1,
            'total_instances2': total_instances2,
            'has_embedded1': has_embedded1,
            'has_embedded2': has_embedded2,
            'embedded_count1': embedded_count1,
            'embedded_count2': embedded_count2,
            'export_path1': str(export_path1),
            'export_path2': str(export_path2)
        }

        return DiffResult(
            gameobject_diffs=gameobject_diffs,
            folder_diffs=folder_diffs,
            summary=summary
        )