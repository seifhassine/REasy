"""MHR UCLS 43 path containers with both native index tables preserved."""
import struct
from dataclasses import dataclass, field


@dataclass
class UCurveListFile:
    version: int = 43
    paths: list[str] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)
    lookup: list[int] = field(default_factory=list)

    def read(self, data):
        if len(data) < 48: raise ValueError('Truncated UCLS header')
        magic, self.version, count, reserved, first, last, table, zero = struct.unpack_from('<4I4Q', data)
        if magic != 0x736c6375 or self.version != 43 or reserved or zero: raise ValueError('Invalid MHR UCLS 43 header')
        if first+count*4 > len(data) or last+count*8 != len(data) or table+count*8 > len(data):
            raise ValueError('UCLS table exceeds file boundary')
        self.ids = list(struct.unpack_from('<'+'I'*count, data, first))
        self.lookup = list(struct.unpack_from('<'+'Q'*count, data, last))
        self.paths = []
        for index in range(count):
            offset = struct.unpack_from('<Q', data, table+index*8)[0]
            end = offset
            while end+2 <= len(data) and data[end:end+2] != b'\0\0': end += 2
            if end+2 > len(data): raise ValueError('Unterminated UCLS resource path')
            self.paths.append(data[offset:end].decode('utf-16le'))
        return self

    def write(self):
        if len(self.ids) != len(self.paths) or len(self.lookup) != len(self.paths):
            raise ValueError('UCLS index tables and paths must have equal counts')
        first = 48; table = first+len(self.paths)*4; table += -table%16
        output = bytearray(table+8*len(self.paths))
        for index, path in enumerate(self.paths):
            if '\0' in path: raise ValueError('NUL in UCLS resource path')
            struct.pack_into('<Q', output, table+index*8, len(output))
            output.extend(path.encode('utf-16le')+b'\0\0')
        output.extend(bytes(-len(output)%16)); last = len(output)
        output.extend(struct.pack('<'+'Q'*len(self.lookup), *self.lookup))
        struct.pack_into('<4I4Q', output, 0, 0x736c6375, self.version, len(self.paths), 0, first, last, table, 0)
        struct.pack_into('<'+'I'*len(self.ids), output, first, *self.ids)
        raw = bytes(output)
        if UCurveListFile().read(raw) != self: raise ValueError('UCLS semantic roundtrip mismatch')
        return raw
