from .base_model import BaseModel, FileHandler
from .uvar_types import ParamType

FORMATS = {ParamType.S32: '<i', ParamType.U32: '<I', ParamType.F32: '<f'}

class NodeParameter(BaseModel):

    def __init__(self):
        super().__init__()
        self.name_hash: int = 0
        self.type: ParamType = ParamType.S32
        self.attribute: int = 0
        self.value = 0

    def do_read(self, handler: FileHandler) -> bool:
        self.name_hash, kind, self.attribute = handler.read('<IBB2x')
        self.type = ParamType(kind)
        if self.type == ParamType.Guid:
            with handler.seek_temp(handler.read('<Q')):
                self.value = handler.read_guid()
        elif self.type in FORMATS:
            self.value = handler.read(FORMATS[self.type])
        else:
            raise NotImplementedError(f"UVAR node parameter type {self.type.name} occurs in no game file")
        handler.align(16)
        return True

    def do_write(self, handler: FileHandler) -> bool:
        handler.write('<IBB2x', self.name_hash, self.type, self.attribute)
        if self.type == ParamType.Guid:
            handler.write('<Q', handler.tell + 8)
            handler.write_guid(self.value)
        else:
            handler.write(FORMATS[self.type], self.value)
        handler.align_write(16)
        return True

    def __repr__(self) -> str:
        return f"NodeParameter(name_hash={self.name_hash:08x}, type={self.type.name}, value={self.value})"
