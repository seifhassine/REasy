"""MOTFSM views of the shared RSZ reader. No independent field-layout parser."""
from dataclasses import dataclass, field as member
import struct

from file_handlers.rsz.rsz_file import RszFile, TypeRegistryValidationError
from file_handlers.rsz.utils.rsz_field_utils import VALUE_COMPONENTS
from file_handlers.rsz.rsz_data_types import ObjectData, UserDataData, StructData, ArrayData
from .fields import SCALAR_FORMATS


BLOCK_NAMES = (
    "actions", "selectors", "selector_callers", "conditions", "transition_events",
    "expression_tree_conditions", "static_actions", "static_selector_callers",
    "static_conditions", "static_transition_events", "static_expression_tree_conditions",
)


class _ObservedRszFile(RszFile):
    def __init__(self):
        super().__init__()
        self.field_spans = {}
        self.instance_spans = {}
        self.field_observer = self._record_field

    def _record_field(self, instance_id, definition, value, start, end):
        if not 0 <= start <= end <= len(self.data):
            raise ValueError(f"RSZ field exceeds block: instance {instance_id}, {definition['name']}")
        self.field_spans[id(value)] = (start, end)

    def parse_instance_fields(self, offset, fields_def, current_instance_index=None):
        end = super().parse_instance_fields(offset, fields_def, current_instance_index)
        self.instance_spans[current_instance_index] = (offset, end)
        return end


@dataclass
class RSZField:
    name: str
    type_name: str
    data: object
    offset: int
    size: int
    is_array: bool
    binding: object = None
    block: object = None
    definition: dict = member(default_factory=dict)
    path: tuple = ()
    _children: list | None = None
    instance_index: int | None = None

    @property
    def children(self):
        if self._children is None:
            self._children = self.block.field_children(self) if self.block else []
        for child in self._children:
            child.instance_index = self.instance_index
        return self._children

    @property
    def reference(self):
        return isinstance(self.data, (ObjectData, UserDataData))

    @property
    def native_type(self):
        return self.definition.get('original_type') or self.type_name

    @property
    def enum_values(self):
        if self.is_array or self.reference or self.binding is None or self.binding.type_name not in (
                's8', 'u8', 's16', 'u16', 's32', 'u32', 's64', 'u64'):
            return []
        from utils.enum_manager import registry_enums
        return registry_enums(self.block.document.type_registry.json_path).get(self.native_type, [])

    def resolve(self):
        if not self.reference or self.data.value == 0:
            return None
        return self.block.get_instance(self.data.value)

    @property
    def value(self):
        if self.is_array:
            return f"array[{len(self.data.values)}]"
        if isinstance(self.data, dict):
            return self.type_name
        if hasattr(self.data, "value"):
            value = self.data.value
            return value.rstrip("\0") if isinstance(value, str) else value
        if hasattr(self.data, "guid_str"):
            return self.data.guid_str
        components = VALUE_COMPONENTS.get(type(self.data).__name__)
        if components:
            return tuple(getattr(self.data, name) for name in components)
        if hasattr(self.data, "raw_bytes"):
            return self.data.raw_bytes.hex()
        if hasattr(self.data, 'values'):
            return list(self.data.values)
        compound = {'AABBData': ('min', 'max'), 'CapsuleData': ('start', 'end', 'radius'),
                    'AreaData': ('p0', 'p1', 'p2', 'p3', 'height', 'bottom'),
                    'AreaDataOld': ('p0', 'p1', 'p2', 'p3', 'height', 'bottom')}
        names = compound.get(type(self.data).__name__)
        if names:
            result = {}
            for name in names:
                value = getattr(self.data, name)
                components = VALUE_COMPONENTS.get(type(value).__name__)
                result[name] = [getattr(value, c) for c in components] if components else value
            return result
        return f"<{self.type_name}:{self.size}b>"


@dataclass
class RSZInstance:
    index: int
    type_id: int
    class_name: str
    start_offset: int | None
    end_offset: int | None
    fields: list
    is_userdata: bool = False
    block: object = None

    @property
    def size(self):
        return self.end_offset - self.start_offset if self.start_offset is not None else 0


class RSZBlock:
    def __init__(self, document, name, offset, end):
        self.document = document
        self.name = name
        self.offset = offset
        self.end = end
        self._file = None
        self._instances = {}
        if end - offset < 8 or document.source[offset:offset + 4] != b'RSZ\0':
            raise ValueError(f"Invalid RSZ signature in {name} at 0x{offset:X}")
        version = struct.unpack_from('<I', document.source, offset + 4)[0]
        if end - offset < (32 if version < 4 else 48):
            raise ValueError(f"Truncated RSZ header in {name} at 0x{offset:X}")

    @property
    def file(self):
        if self._file is None:
            parsed = _ObservedRszFile()
            parsed.type_registry = self.document.type_registry
            parsed.game_version = ""
            try:
                parsed.read_headless(
                    self.document.source[self.offset:self.end], validate_type_registry=True,
                    absolute_offset=self.offset,
                )
            except TypeRegistryValidationError as exc:
                details = '; '.join(exc.issues[:3])
                raise ValueError(f"{self.name}: {len(exc.issues)} RSZ type/CRC mismatches in "
                                 f"{parsed.type_registry.json_path}: {details}") from exc
            except (struct.error, IndexError) as exc:
                raise ValueError(f"Cannot parse {self.name} RSZ at 0x{self.offset:X}: {exc}") from exc
            if parsed._current_offset != parsed.rsz_header.data_offset:
                raise ValueError(f"RSZ data offset mismatch in {self.name}")
            if any(not 0 <= i < len(parsed.instance_infos) for i in parsed.object_table):
                raise ValueError(f"Invalid object table in {self.name}")
            self._file = parsed
        return self._file

    @property
    def instance_count(self):
        return len(self.file.instance_infos)

    @property
    def object_table(self):
        return self.file.object_table

    def get_class_name(self, index):
        if not 0 <= index < self.instance_count:
            raise IndexError(f"Invalid {self.name} instance: {index}")
        if index == 0 or self.file.instance_infos[index].type_id == 0:
            return "NULL"
        return self.document.type_registry.get_type_info(self.file.instance_infos[index].type_id)["name"]

    def get_instance(self, index):
        if not 0 <= index < self.instance_count:
            raise IndexError(f"Invalid {self.name} instance: {index}")
        if index in self._instances:
            return self._instances[index]
        parsed = self.file
        type_id = parsed.instance_infos[index].type_id
        userdata = index in parsed._rsz_userdata_set
        data_base = self.offset + parsed._current_offset
        span = parsed.instance_spans.get(index)
        start, end = (data_base + span[0], data_base + span[1]) if span else (None, None)
        fields = []
        if index and type_id and not userdata:
            definition = self.document.type_registry.get_type_info(type_id)
            for fd in definition.get("fields", []):
                value = parsed.parsed_elements[index][fd["name"]]
                item = self.make_field(fd, value, (fd['name'],))
                item.instance_index = index
                fields.append(item)
        instance = RSZInstance(index, type_id, self.get_class_name(index),
                               start, end, fields, userdata, self)
        self._instances[index] = instance
        return instance

    def make_field(self, definition, value, path):
        span = self.file.field_spans.get(id(value))
        offset, size = (self.offset+self.file._current_offset+span[0], span[1]-span[0]) if span else (0, 0)
        scalar = 'u32' if isinstance(value, (ObjectData, UserDataData)) else definition['type'].lower()
        binding = None
        if not definition['array'] and scalar in SCALAR_FORMATS and span:
            binding = self.document.bindings.bind(value, 'value', offset, scalar, name='.'.join(map(str, path)))
            if binding.size != size:
                raise ValueError(f'Unexpected scalar span in {self.name}.{path}')
        return RSZField(str(path[-1]), definition['type'], value,
                        offset, size, definition['array'] or isinstance(value, ArrayData), binding, self, definition, path)

    def field_children(self, field):
        if isinstance(field.data, StructData):
            definition, _ = self.document.type_registry.find_type_by_name(field.data.orig_type)
            if definition is None:
                raise ValueError(f'Unknown struct type {field.data.orig_type}')
            result = []
            for index, values in enumerate(field.data.values):
                children = [self.make_field(fd, values[fd['name']], (*field.path, index, fd['name']))
                            for fd in definition['fields']]
                result.append(RSZField(f'[{index}]', field.data.orig_type, values, field.offset, 0,
                                       False, block=self, path=(*field.path, index), _children=children))
            return result
        if field.is_array:
            definition = dict(field.definition, array=False)
            return [self.make_field(definition, value, (*field.path, index))
                    for index, value in enumerate(field.data.values)]
        return []

    def get_object(self, index):
        if not 0 <= index < len(self.object_table):
            raise IndexError(f"Invalid {self.name} object: {index}")
        return self.get_instance(self.object_table[index])


class RSZBlocks:
    def __init__(self, document, offsets, ends):
        by_offset = {}
        self.blocks = {}
        for name, offset in zip(BLOCK_NAMES, offsets):
            if offset not in by_offset:
                by_offset[offset] = RSZBlock(document, name, offset, ends[offset])
            self.blocks[name] = by_offset[offset]

    def get_block(self, name):
        return self.blocks[name]
