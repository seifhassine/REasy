"""MOTFSM views of the shared RSZ reader. No independent field-layout parser."""
from dataclasses import dataclass

from file_handlers.rsz.rsz_file import RszFile
from file_handlers.rsz.utils.rsz_field_utils import VALUE_COMPONENTS
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

    @property
    def value(self):
        if self.is_array:
            return f"array[{len(self.data.values)}]"
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

    @property
    def file(self):
        if self._file is None:
            if self.offset % 16:
                raise ValueError(f"Unaligned MHRise RSZ block: {self.name}")
            parsed = _ObservedRszFile()
            parsed.type_registry = self.document.type_registry
            parsed.game_version = "MHRise"
            parsed.read_headless(
                self.document.source[self.offset:self.end], validate_type_registry=True,
            )
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
        if index == 0:
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
        if index and not userdata:
            definition = self.document.type_registry.get_type_info(type_id)
            for fd in definition.get("fields", []):
                value = parsed.parsed_elements[index][fd["name"]]
                first, last = parsed.field_spans[id(value)]
                scalar = fd["type"].lower()
                binding = None
                if not fd["array"] and scalar in SCALAR_FORMATS:
                    binding = self.document.bindings.bind(
                        value, "value", data_base + first, scalar, name=fd["name"],
                    )
                    if binding.size != last - first:
                        raise ValueError(f"Unexpected scalar span in {self.name}.{fd['name']}")
                fields.append(RSZField(fd["name"], fd["type"], value, data_base + first,
                                       last - first, fd["array"], binding))
        instance = RSZInstance(index, type_id, self.get_class_name(index),
                               start, end, fields, userdata)
        self._instances[index] = instance
        return instance

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
