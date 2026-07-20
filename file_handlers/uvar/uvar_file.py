from typing import List, Optional, TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from .variable import Variable

from .base_model import BaseModel, FileHandler
from .header import HeaderStruct, HashData
from .variable import Variable
from .uvar_types import TypeKind
from .uvar_expression import UvarExpression
from .config import MAX_VARIABLES
from utils.hash_util import murmur3_hash

class UVarFile(BaseModel):
    def __init__(self, handler: FileHandler | bytes | bytearray | None = None, is_embedded: bool = False):
        super().__init__()
        self.header = HeaderStruct()
        self.variables: List[Variable] = []
        self.embedded_uvars: List['UVarFile'] = []
        self.hash_data = HashData()
        self.is_embedded = is_embedded
        
        if handler is not None:
            if isinstance(handler, FileHandler):
                self.do_read(handler)
            else:
                self.read(handler)

    def _read_header(self, handler: FileHandler):
        self.header = HeaderStruct()
        if not self.header.read(handler):
            raise ValueError("Failed to read UVAR header")

        if self.header.strings_offset > 0:
            with handler.seek_temp(self.header.strings_offset):
                self.header.name = handler.read_wstring()

    def _read_variable_headers(
        self,
        handler: FileHandler,
    ) -> List[Variable]:
        variables = []
        for index in range(self.header.variable_count):
            if handler.tell + 48 > len(handler.data):
                raise ValueError(
                    f"Not enough data to read variable {index} "
                    f"at position {handler.tell}"
                )
            variable = Variable()
            if not variable.read_header(handler):
                raise ValueError(
                    f"Failed to read variable header {index} "
                    f"at position {handler.tell}"
                )
            variables.append(variable)
        return variables

    @staticmethod
    def _read_variable_values(
        handler: FileHandler,
        variables: List[Variable],
    ):
        data_ptr = handler.position
        for variable in variables:
            if variable.type == TypeKind.Trigger:
                variable.reset_value()
                continue
            handler.seek_relative(data_ptr)
            start_rel = handler.position
            variable.read_value(handler)
            consumed = max(0, handler.position - start_rel)
            data_ptr += consumed
            data_ptr += (4 - (data_ptr % 4)) % 4

    @staticmethod
    def _read_variable_expressions(
        handler: FileHandler,
        variables: List[Variable],
    ):
        for variable in variables:
            if variable.expression_offset <= 0:
                continue
            with handler.seek_temp(variable.expression_offset):
                variable.expression = UvarExpression()
                if not variable.expression.read(handler):
                    raise ValueError(
                        "Failed to read expression for variable "
                        f"{variable.name}"
                    )

    def _read_variables(self, handler: FileHandler):
        self.variables.clear()
        if self.header.variable_count <= 0:
            return
        if self.header.variable_count > MAX_VARIABLES:
            raise ValueError(
                f"Variable count {self.header.variable_count} "
                f"exceeds maximum {MAX_VARIABLES}"
            )
        if self.header.data_offset <= 0:
            return
        if self.header.data_offset >= len(handler.data):
            raise ValueError(
                f"data_offset {self.header.data_offset} is beyond file size "
                f"{len(handler.data)}"
            )

        handler.seek_relative(self.header.data_offset)
        variables = self._read_variable_headers(handler)
        self._read_variable_values(handler, variables)
        self.variables = variables
        self._read_variable_expressions(handler, self.variables)

    def _read_embedded_uvars(self, handler: FileHandler):
        self.embedded_uvars.clear()
        if (
            self.header.embed_count <= 0
            or self.header.embeds_info_offset <= 0
        ):
            return

        handler.seek_relative(self.header.embeds_info_offset)
        for index in range(self.header.embed_count):
            embed_offset = handler.read('<Q')
            if embed_offset == 0 or embed_offset >= len(handler.data):
                raise ValueError(
                    f"Invalid embedded UVAR offset: {embed_offset}"
                )

            with handler.seek_temp(embed_offset):
                embed_handler = FileHandler(handler.data, embed_offset)
                embed_file = UVarFile()
                embed_file.is_embedded = True
                if not embed_file.do_read(embed_handler):
                    raise ValueError(
                        f"Failed to read embedded UVAR {index}"
                    )
                self.embedded_uvars.append(embed_file)

    def _read_hash_data(self, handler: FileHandler):
        if (
            self.header.variable_count <= 0
            or self.header.hash_info_offset <= 0
        ):
            return

        handler.seek_relative(self.header.hash_info_offset)
        self.hash_data = HashData()
        self.hash_data.count = self.header.variable_count
        if not self.hash_data.read(handler):
            raise ValueError("Failed to read hash data")
            
    def do_read(self, handler: FileHandler) -> bool:
        self._read_header(handler)
        self._read_variables(handler)
        self._read_embedded_uvars(handler)
        self._read_hash_data(handler)
        return True

    @staticmethod
    def _write_shared_variable_value(
        handler: FileHandler,
        variable: Variable,
        group_start_by_base: dict[int, int],
    ):
        original_base = variable.value_offset
        if original_base not in group_start_by_base:
            group_start_by_base[original_base] = handler.tell
        if variable.type == TypeKind.Trigger:
            handler.write_at(
                variable.start_offset + 24,
                '<Q',
                variable.value_offset,
            )
            return
        variable.write_value(
            handler,
            value_offset_override=group_start_by_base[original_base],
        )

    @staticmethod
    def _write_single_variable_value(
        handler: FileHandler,
        variable: Variable,
        current_data_end_offset: int,
    ):
        if variable.type != TypeKind.Trigger:
            variable.write_value(handler)
            return
        if (
            variable._original_value_offset is not None
            and variable._original_value_offset == 0
        ):
            handler.write_at(variable.start_offset + 24, '<Q', 0)
            variable.value_offset = 0
        elif variable.value_offset == 0:
            handler.write_at(
                variable.start_offset + 24,
                '<Q',
                current_data_end_offset,
            )
            variable.value_offset = current_data_end_offset
        else:
            handler.write_at(
                variable.start_offset + 24,
                '<Q',
                variable.value_offset,
            )

    def _write_variable_values(
        self,
        handler: FileHandler,
        base_counts: dict[int, int],
    ):
        group_start_by_base: dict[int, int] = {}
        current_data_end_offset = handler.tell
        for variable in self.variables:
            original_base = variable.value_offset
            if (
                original_base > 0
                and base_counts.get(original_base, 0) > 1
            ):
                self._write_shared_variable_value(
                    handler,
                    variable,
                    group_start_by_base,
                )
            else:
                self._write_single_variable_value(
                    handler,
                    variable,
                    current_data_end_offset,
                )

            if variable.type != TypeKind.Trigger:
                current_data_end_offset = handler.tell

    def _write_variables(self, handler: FileHandler):
        if not self.variables:
            return

        handler.write_at(self.header.start_offset + 16, '<Q', handler.tell)
        self.header.data_offset = handler.tell
        for variable in self.variables:
            variable.write(handler)

        base_counts: dict[int, int] = {}
        for variable in self.variables:
            if variable.value_offset > 0:
                base_counts[variable.value_offset] = (
                    base_counts.get(variable.value_offset, 0) + 1
                )

        self._write_variable_values(handler, base_counts)
        handler.align_write(16)
        for variable in self.variables:
            variable.write_expression(handler)

    def _write_strings(self, handler: FileHandler):
        handler.write_at(self.header.start_offset + 8, '<Q', handler.tell)
        self.header.strings_offset = handler.tell
        handler.write_wstring(self.header.name or "")

        for variable in self.variables:
            handler.write_at(variable.start_offset + 16, '<Q', handler.tell)
            handler.write_wstring(variable.name or "")

    def _write_embedded_uvars(self, handler: FileHandler):
        if not self.embedded_uvars:
            return

        handler.align_write(16)
        handler.write_at(self.header.start_offset + 24, '<Q', handler.tell)
        self.header.embeds_info_offset = handler.tell
        embed_offsets_start = handler.tell
        handler.skip(8 * len(self.embedded_uvars))

        for index, embed in enumerate(self.embedded_uvars):
            handler.align_write(16)
            embed_start_pos = handler.tell
            handler.write_at(
                embed_offsets_start + index * 8,
                '<Q',
                embed_start_pos,
            )
            embed_handler = FileHandler(bytearray())
            embed.do_write(embed_handler)
            handler.write_bytes(bytearray(embed_handler.get_bytes()))

    def _write_hash_data(self, handler: FileHandler):
        if self.hash_data is None:
            return

        handler.align_write(16)
        handler.write_at(self.header.start_offset + 32, '<Q', handler.tell)
        self.header.hash_info_offset = handler.tell
        if not self.hash_data.write(handler):
            raise ValueError("Failed to write hash data")
            
    def do_write(self, handler: FileHandler) -> bool:
        self.update_strings()
        
        if self.hash_data is None:
            self.hash_data = HashData()
        self.hash_data.rebuild(self.variables)
        
        self.header.variable_count = len(self.variables)
        self.header.embed_count = len(self.embedded_uvars)
        
        self.header.start_offset = handler.tell
        if not self.header.write(handler):
            raise ValueError("Failed to write header")
            
        handler.align_write(16)

        self._write_variables(handler)
        self._write_strings(handler)
        self._write_embedded_uvars(handler)
        self._write_hash_data(handler)
        return True
            
    def read(self, data: bytes | bytearray) -> bool:
        handler = FileHandler(data)
        return self.do_read(handler)
        
    def write(self) -> bytes:
        handler = FileHandler(bytearray())
        if self.do_write(handler):
            return handler.get_bytes()
        return b''
        
    def update_strings(self):
        for var in self.variables:
            var.name_hash = murmur3_hash((var.name or "").encode('utf-16le'))
            
        for embed in self.embedded_uvars:
            embed.update_strings()
            
    def add_variable(self, name: str, var_type: int | TypeKind, value: Any = None) -> Variable:
        from .uvar_types import TypeKind
        
        var = Variable()
        var.guid = uuid.uuid4()
        var.name = name
        if isinstance(var_type, int):
            var.type = TypeKind(var_type)
        else:
            var.type = var_type
        var.flags = 0
        var.name_hash = murmur3_hash(name.encode('utf-16le')) 
        
        if value is not None:
            var.value = value
        else:
            var.reset_value()
        
        var.name_offset = 0
        var.value_offset = 0
        var.expression_offset = 0
        
        self.variables.append(var)
        return var
        
    def remove_variable(self, index: int) -> bool:
        if 0 <= index < len(self.variables):
            del self.variables[index]
            return True
        return False
        
    def find_variable_by_name(self, name: str) -> Optional[Variable]:
        for var in self.variables:
            if var.name == name:
                return var
        return None
        
    def find_variable_by_guid(self, guid: uuid.UUID) -> Optional[Variable]:
        for var in self.variables:
            if var.guid == guid:
                return var
        return None
        
    def __repr__(self) -> str:
        return f"UVarFile(name='{self.header.name}', vars={len(self.variables)}, embeds={len(self.embedded_uvars)})"
