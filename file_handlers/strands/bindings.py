from dataclasses import dataclass
import struct

import numpy as np


@dataclass
class StrandBindings:
    version: int
    mesh_path: str
    levels: tuple[tuple[np.ndarray, np.ndarray], ...]
    quantized: bool = False
    tables: tuple[bytes, ...] = ()

    @classmethod
    def read(cls, data):
        if len(data) < 12:
            raise ValueError('Truncated SBD header')
        magic, version, size = struct.unpack_from('<4sII', data)
        if magic != b'SDBD':
            raise ValueError('Invalid SBD magic')
        if version == 0:
            # RE4's .sbd.7; the scene names the target mesh.
            version, mesh_path, quantized = 7, '', False
            sizes, offset = (size,), 12
        elif version in (250925253, 250925255, 251121870):
            if len(data) < 16:
                raise ValueError('Truncated SBD header')
            lods, quantized, size = struct.unpack_from('<HHI', data, 8)
            if not 1 <= lods <= 8 or not 1 < size <= len(data) - 16 - 4 * lods:
                raise ValueError('Invalid SBD section sizes')
            uri = data[16:16 + size]
            if uri[-1:] != b'\0' or b'\0' in uri[:-1]:
                raise ValueError('Invalid SBD mesh path')
            try:
                mesh_path = uri[:-1].decode('utf-8')
            except UnicodeDecodeError as error:
                raise ValueError('Invalid SBD mesh path') from error
            sizes = struct.unpack_from(f'<{lods}I', data, 16 + size)
            offset = 16 + size + 4 * lods
        else:
            raise ValueError(f'Unsupported SBD version: {version}')
        stride = 16 if quantized else 20
        if offset + sum(sizes) != len(data) or any(n % stride for n in sizes):
            raise ValueError('Invalid SBD binding sizes')
        levels, tables = [], []
        for n in sizes:
            tables.append(bytes(data[offset:offset + n]))
            words = np.frombuffer(data, '<u4', n // 4, offset).reshape(-1, stride // 4)
            if np.any(words[:, :3] % 12):
                raise ValueError('SBD position offset is not aligned to a vertex')
            weights = (np.column_stack((words[:, 3] & 65535, words[:, 3] >> 16)).astype(np.float32) / 65535
                       if quantized else words[:, 3:5].view('<f4'))
            if not np.isfinite(weights).all():
                raise ValueError('Non-finite SBD barycentric coordinates')
            levels.append((words[:, :3] // 12, weights))
            offset += n
        return cls(version, mesh_path, tuple(levels), bool(quantized), tuple(tables))

    def to_bytes(self) -> bytes:
        stride = 16 if self.quantized else 20
        if any(len(table) % stride for table in self.tables):
            raise ValueError('Invalid SBD binding sizes')
        if self.version == 7:
            if len(self.tables) != 1:
                raise ValueError('An RE4 SBD holds one binding table')
            return struct.pack('<4sII', b'SDBD', 0, len(self.tables[0])) + self.tables[0]
        uri = self.mesh_path.encode('utf-8') + b'\0'
        head = struct.pack('<4sIHHI', b'SDBD', self.version, len(self.tables), int(self.quantized), len(uri))
        return head + uri + struct.pack(f'<{len(self.tables)}I', *map(len, self.tables)) + b''.join(self.tables)

    @staticmethod
    def encode(triangles, weights, quantized: bool) -> bytes:
        triangles = np.asarray(triangles, np.int64).reshape(-1, 3)
        weights = np.asarray(weights, np.float64).reshape(-1, 2)
        if quantized:
            q = np.clip(np.rint(weights * 65535), 0, 65535).astype(np.uint32)
            words = np.column_stack((triangles * 12, q[:, 0] | (q[:, 1] << 16))).astype('<u4')
            return words.tobytes()
        records = np.zeros(len(triangles), [('offsets', '<u4', 3), ('weights', '<f4', 2)])
        records['offsets'], records['weights'] = triangles * 12, weights
        return records.tobytes()

    def with_tables(self, tables) -> 'StrandBindings':
        return StrandBindings.read(StrandBindings(self.version, self.mesh_path, (), self.quantized,
                                                  tuple(map(bytes, tables))).to_bytes())

    def rebound(self, surfaces, roots, strands) -> 'StrandBindings':
        strands = np.asarray(strands, np.int64)
        if not len(strands):
            return self
        stride = 16 if self.quantized else 20
        tables = []
        for table, surface in zip(self.tables, surfaces, strict=True):
            triangles, weights, _distance = surface.nearest(roots)
            records = np.frombuffer(table, np.uint8).reshape(-1, stride).copy()
            records[strands] = np.frombuffer(self.encode(triangles, weights, self.quantized), np.uint8).reshape(
                -1, stride)
            tables.append(records.tobytes())
        return self.with_tables(tables)

