lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mesh.conversion_skin import coalesce_weights
from file_handlers.mesh.mesh_file import SKIN_WEIGHT_STREAMS


SKIN_WEIGHT_ATTRIBUTE_NAMES = frozenset(SKIN_WEIGHT_STREAMS)
HEAT_COLORS = ((0.06, 0.08, 0.22), (0.0, 0.45, 0.95), (0.0, 0.85, 0.65),
               (1.0, 0.85, 0.05), (0.95, 0.12, 0.04))
INVALID_COLOR = (1.0, 0.0, 1.0)
ZERO_COLOR = (0.16, 0.16, 0.16)


def merge_attribute(records, name, width, dtype):
    chunks, missing = [], []
    for record in records:
        values = getattr(record.payload, name, None)
        if values is None or len(values) == 0:
            missing.append(record.buffer_index)
            continue
        data = np.asarray(values, dtype=dtype).reshape(-1)
        end = (record.source_vertex_start + record.vertex_count) * width
        if data.size < end:
            raise ValueError(f"Malformed {name} in mesh buffer {record.buffer_index}")
        chunks.append(data[record.source_vertex_start * width:end].reshape(-1, width))
    if not chunks:
        return None
    if missing:
        raise ValueError(f"Missing {name} in mesh buffers {missing}")
    return np.concatenate(chunks)


def heat_colors(values, maximum=1.0):
    values = np.clip(np.nan_to_num(values / maximum), 0.0, 1.0)
    stops = np.linspace(0.0, 1.0, len(HEAT_COLORS))
    return np.column_stack([np.interp(values, stops, channel)
                            for channel in np.asarray(HEAT_COLORS).T]).astype(np.float32)


def category_colors(indices):
    indices = np.asarray(indices, dtype=np.uint32)
    return 0.15 + 0.85 * np.column_stack((
        (indices * 97 + 17) % 251, (indices * 57 + 83) % 253, (indices * 23 + 191) % 255,
    )).astype(np.float32) / 254.0


def coalesce_weight_lanes(indices, weights):
    """Sorting by index first makes a tie for the strongest lane go to the lowest index."""
    order = np.argsort(indices, axis=1, kind="stable")
    indices = np.take_along_axis(indices, order, axis=1)
    return indices, coalesce_weights(indices, np.take_along_axis(weights, order, axis=1))


class MeshAttributeView:
    def __init__(self, records, name):
        self.name, self.entry, self.uv_scale = name, None, 8
        # Entry -> skeleton joint (``bone_remap_indices``), for the "dominant_joint" mode (not in ``modes``) that
        # compares meshes with different palettes.
        self.palette = ()
        self.values, self.chunks = None, []
        self.entries, self.entry_counts = [], {}
        self.is_weight = name in SKIN_WEIGHT_ATTRIBUTE_NAMES
        self.is_uv = name in {"uv0", "uv1", "uv2"}
        if self.is_weight:
            self.modes = (
                (QT_TRANSLATE_NOOP("AttributeControls", "Influence"), "influence"),
                (QT_TRANSLATE_NOOP("AttributeControls", "Dominant entry"), "dominant"),
                (QT_TRANSLATE_NOOP("AttributeControls", "Influence count"), "count"),
                (QT_TRANSLATE_NOOP("AttributeControls", "Weight total"), "total"),
            )
            self._read_weights(records)
        else:
            width = (3 if name in {"normals", "tangents"} else
                     2 if self.is_uv else 4 if name == "colors" else 1)
            self.values = merge_attribute(records, name, width, np.float32)
            if self.values is None:
                raise ValueError(f"No {name} data is available")
            if name == "colors":
                self.values /= 255.0
                self.modes = (
                    (QT_TRANSLATE_NOOP("AttributeControls", "RGB"), "rgb"),
                    (QT_TRANSLATE_NOOP("AttributeControls", "Red"), "r"),
                    (QT_TRANSLATE_NOOP("AttributeControls", "Green"), "g"),
                    (QT_TRANSLATE_NOOP("AttributeControls", "Blue"), "b"),
                    (QT_TRANSLATE_NOOP("AttributeControls", "Alpha"), "a"),
                )
            elif self.is_uv:
                self.modes = ((QT_TRANSLATE_NOOP("AttributeControls", "Checker"), "checker"), (QT_TRANSLATE_NOOP("AttributeControls", "UV coordinates"), "coordinates"))
            elif name in {"normals", "tangents"}:
                self.modes = ((QT_TRANSLATE_NOOP("AttributeControls", "Direction"), "direction"), ("X", "x"), ("Y", "y"),
                              ("Z", "z"), (QT_TRANSLATE_NOOP("AttributeControls", "Length error"), "length"))
            else:
                self.modes = ((QT_TRANSLATE_NOOP("AttributeControls", "Categories") if name in {"pivot_indices", "tangent_handedness"}
                               else QT_TRANSLATE_NOOP("AttributeControls", "Signed value"), "value"),)
        self.mode = self.modes[0][1]

    def _read_weights(self, records):
        usage, strength, counts, totals = {}, {}, [], []
        for record in records:
            buffer_index, start, count = record.buffer_index, record.source_vertex_start, record.vertex_count
            stream = getattr(record.payload, self.name, None)
            if stream is None:
                raise ValueError(f"Missing {self.name} in mesh buffer {buffer_index}")
            width = int(stream.influence_count)
            if width < 1:
                raise ValueError(f"Empty {self.name} in mesh buffer {buffer_index}")
            indices = np.asarray(stream.deform_indices, dtype=np.uint16).reshape(-1, width)
            weights = np.asarray(stream.weights, dtype=np.float32).reshape(-1, width)
            if min(len(indices), len(weights)) < start + count:
                raise ValueError(f"Malformed {self.name} in mesh buffer {buffer_index}")
            indices, weights = indices[start:start + count], weights[start:start + count]
            invalid = (~np.isfinite(weights) | (weights < 0.0)).any(axis=1)
            indices, weights = coalesce_weight_lanes(
                indices, np.where(np.isfinite(weights), np.maximum(weights, 0), 0))
            self.chunks.append((indices, weights, invalid))
            active = (weights > 0) & ~invalid[:, None]
            present, frequency = np.unique(indices[active], return_counts=True)
            sums = np.bincount(indices[active], weights=weights[active])
            for entry, frequency in zip(present, frequency):
                entry = int(entry)
                usage[entry] = usage.get(entry, 0) + int(frequency)
                strength[entry] = strength.get(entry, 0.0) + float(sums[entry])
            counts.append(np.count_nonzero(weights > 0, axis=1))
            totals.append(weights.sum(axis=1))
        if not self.chunks:
            raise ValueError(f"No {self.name} data is available")
        self.counts, self.totals = np.concatenate(counts), np.concatenate(totals)
        self.invalid = np.concatenate([chunk[2] for chunk in self.chunks])
        self.entries = sorted(usage or {int(entry): 0 for indices, _, _ in self.chunks
                                       for entry in np.unique(indices)})
        self.entry_counts = usage
        self.entry = max(self.entries, key=lambda entry: strength.get(entry, 0.0), default=None)
        self.count_max = max(1, int(self.counts.max(initial=0)))

    def _joint_lanes(self, indices, weights):
        palette = np.asarray(self.palette, dtype=np.uint32)
        entries = np.where(weights > 0, indices, 0)
        return coalesce_weight_lanes(palette[entries] if len(palette) else entries, weights)

    def entry_label(self, mesh, entry):
        palette = getattr(mesh, "bone_remap_indices", ())
        joints = getattr(mesh, "bone_indices", ())
        names = getattr(mesh, "names", ())
        if 0 <= entry < len(palette):
            joint = int(palette[entry])
            if 0 <= joint < len(joints) and 0 <= joints[joint] < len(names):
                return f"{names[joints[joint]]} · entry {entry} / joint {joint}"
            return f"Entry {entry} · joint {joint}"
        return f"Entry {entry} · no bone mapping"

    def colors(self, *, normals=None, tangents=None):
        if self.is_weight:
            invalid = self.invalid
            if self.mode == "count":
                rgb = heat_colors(self.counts, self.count_max)
            elif self.mode == "total":
                rgb = heat_colors(self.totals)
            else:
                chunks = []
                for indices, weights, _invalid in self.chunks:
                    if self.mode == "influence":
                        rgb = heat_colors(np.where(indices == self.entry, weights, 0.0).sum(axis=1))
                    else:
                        if self.mode == "dominant_joint":
                            indices, weights = self._joint_lanes(indices, weights)
                        rows = np.arange(len(weights))
                        slot = weights.argmax(axis=1)
                        maximum = weights[rows, slot]
                        rgb = category_colors(indices[rows, slot])
                        rgb *= (0.2 + 0.8 * np.clip(maximum, 0.0, 1.0))[:, None]
                        rgb[maximum == 0] = ZERO_COLOR
                    chunks.append(rgb)
                rgb = np.concatenate(chunks)
        else:
            values = self.values
            if self.name == "normals" and normals is not None:
                values = normals
            elif self.name == "tangents" and tangents is not None:
                values = tangents[:, :3]
            invalid = ~np.isfinite(values).all(axis=1)
            if invalid.any():
                values = np.where(invalid[:, None], 0.0, values)
            if self.name in {"normals", "tangents"}:
                if self.mode == "length":
                    rgb = heat_colors(np.abs(np.linalg.norm(values, axis=1) - 1.0), 0.25)
                elif self.mode == "direction":
                    rgb = np.clip(values * 0.5 + 0.5, 0.0, 1.0)
                else:
                    rgb = np.repeat(np.clip(values[:, "xyz".index(self.mode), None]
                                            * 0.5 + 0.5, 0.0, 1.0), 3, axis=1)
            elif self.is_uv:
                # The device draws a UV set as its ``UvPattern``, not from these colours.
                rgb = np.full((len(values), 3), ZERO_COLOR, dtype=np.float32)
            elif self.name == "colors":
                rgb = (values[:, :3].copy() if self.mode == "rgb" else
                       np.repeat(values[:, "rgba".index(self.mode), None], 3, axis=1))
            elif self.name == "pivot_indices":
                rgb = category_colors(values[:, 0])
            elif self.name == "tangent_handedness":
                rgb = np.full((len(values), 3), ZERO_COLOR, dtype=np.float32)
                rgb[values[:, 0] < 0] = (0.15, 0.45, 1.0)
                rgb[values[:, 0] > 0] = (1.0, 0.6, 0.1)
            else:
                rgb = np.repeat(np.clip(values * 0.5 + 0.5, 0.0, 1.0), 3, axis=1)
        rgb[invalid] = INVALID_COLOR
        return np.column_stack((rgb, np.ones(len(rgb), dtype=np.float32))).astype(np.float32, copy=False)
