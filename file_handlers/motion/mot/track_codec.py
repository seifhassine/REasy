from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..binary import ReadContext
from ..errors import MotionParseError, MotionWriteError
from .model import KeyTrack, TrackFamily


SOLVER_BY_FAMILY = {
    TrackFamily.FLOAT: 0x0A2,
    TrackFamily.VECTOR3: 0x0F2,
    TrackFamily.QUATERNION: 0x112,
}
FAMILY_BY_SOLVER = {value: key for key, value in SOLVER_BY_FAMILY.items()}

OBSERVED_ENCODINGS = {
    TrackFamily.FLOAT: frozenset({0x00}),
    TrackFamily.VECTOR3: frozenset(
        {0x00, 0x21, 0x22, 0x23, 0x24, 0x31, 0x32, 0x33, 0x34}
    ),
    TrackFamily.QUATERNION: frozenset(
        {0x21, 0x22, 0x23, 0x30, 0x31, 0x32, 0x33, 0x70, 0xB0}
    ),
}


def bytes_per_key(family: TrackFamily, compression: int) -> int:
    if compression not in OBSERVED_ENCODINGS.get(family, ()):
        raise MotionParseError(
            f"unsupported v65 {family.name} compression 0x{compression:02X}"
        )
    if family == TrackFamily.FLOAT and compression == 0:
        return 4
    if family == TrackFamily.VECTOR3:
        if compression == 0:
            return 12
        if 0x21 <= compression <= 0x24:
            return 2
        if 0x31 <= compression <= 0x34:
            return 4
    if family == TrackFamily.QUATERNION:
        if 0x21 <= compression <= 0x23:
            return 2
        if 0x30 <= compression <= 0x33:
            return 4
        if compression == 0x70:
            return 8
        if compression == 0xB0:
            return 12
    raise AssertionError("observed v65 encoding has no key width")


def needs_parameters(family: TrackFamily, compression: int) -> bool:
    if family == TrackFamily.FLOAT:
        return compression != 0
    if family == TrackFamily.VECTOR3:
        return compression != 0 and compression != 0x34
    if family == TrackFamily.QUATERNION:
        return compression not in (0, 0x31, 0x32, 0x33, 0xB0)
    return False


def parameter_count(family: TrackFamily, compression: int) -> int:
    if compression not in OBSERVED_ENCODINGS.get(family, ()):
        raise MotionParseError(
            f"unsupported v65 {family.name} compression 0x{compression:02X}"
        )
    if not needs_parameters(family, compression):
        return 0
    if family == TrackFamily.FLOAT:
        return 2
    if family == TrackFamily.VECTOR3:
        if 0x21 <= compression <= 0x23:
            return 4
        if compression == 0x24:
            return 2
        if 0x31 <= compression <= 0x33:
            return 3
    if family == TrackFamily.QUATERNION:
        if 0x21 <= compression <= 0x23:
            return 2
        if compression in (0x30, 0x70):
            return 8
    raise MotionParseError(
        f"unsupported v65 parameter layout for {family.name}/0x{compression:02X}"
    )


def decode_track_values(
    context: ReadContext,
    offset: int,
    count: int,
    family: TrackFamily,
    compression: int,
    parameters: Sequence[float],
) -> list[float | tuple[float, float, float] | tuple[float, float, float, float]]:
    """Decode exactly ``count`` v65 keys from a previously bounded object context."""

    width = bytes_per_key(family, compression)
    context.require(offset, count * width, "track values")
    expected_parameters = parameter_count(family, compression)
    if len(parameters) != expected_parameters:
        raise MotionParseError(
            f"{context.label}: {family.name}/0x{compression:02X} requires "
            f"{expected_parameters} parameters, got {len(parameters)}"
        )

    if family == TrackFamily.FLOAT:
        values = np.frombuffer(context.data, dtype="<f4", count=count, offset=offset)
        return values.tolist()

    if family == TrackFamily.VECTOR3:
        if compression == 0:
            values = np.frombuffer(
                context.data,
                dtype="<f4",
                count=count * 3,
                offset=offset,
            ).reshape(count, 3)
            return [tuple(row) for row in values.tolist()]
        if 0x21 <= compression <= 0x23:
            axis = compression - 0x21
            codes = np.frombuffer(
                context.data, dtype="<u2", count=count, offset=offset
            ).astype(np.float32)
            normalized = np.multiply(codes, np.float32(1.0 / 0xFFFF))
            component = np.add(
                np.multiply(np.float32(parameters[0]), normalized),
                np.float32(parameters[axis + 1]),
            )
            values = np.empty((count, 3), dtype=np.float32)
            values[:] = np.asarray(parameters[1:4], dtype=np.float32)
            values[:, axis] = component
            return [tuple(row) for row in values.tolist()]
        if compression == 0x24:
            codes = np.frombuffer(
                context.data, dtype="<u2", count=count, offset=offset
            ).astype(np.float32)
            normalized = np.multiply(codes, np.float32(1.0 / 0xFFFF))
            scalar = np.add(
                np.multiply(np.float32(parameters[0]), normalized),
                np.float32(parameters[1]),
            )
            values = np.repeat(scalar[:, None], 3, axis=1)
            return [tuple(row) for row in values.tolist()]
        if 0x31 <= compression <= 0x33:
            axis = compression - 0x31
            component = np.frombuffer(
                context.data, dtype="<f4", count=count, offset=offset
            )
            values = np.empty((count, 3), dtype=np.float32)
            values[:] = np.asarray(parameters, dtype=np.float32)
            values[:, axis] = component
            return [tuple(row) for row in values.tolist()]
        if compression == 0x34:
            scalar = np.frombuffer(
                context.data, dtype="<f4", count=count, offset=offset
            )
            values = np.repeat(scalar[:, None], 3, axis=1)
            return [tuple(row) for row in values.tolist()]

    if family == TrackFamily.QUATERNION:
        values = np.zeros((count, 4), dtype=np.float32)
        if 0x21 <= compression <= 0x23:
            axis = compression - 0x21
            codes = np.frombuffer(
                context.data, dtype="<u2", count=count, offset=offset
            ).astype(np.float32)
            normalized = np.multiply(codes, np.float32(1.0 / 0xFFFF))
            values[:, axis] = np.add(
                np.multiply(np.float32(parameters[0]), normalized),
                np.float32(parameters[1]),
            )
        elif compression == 0x30:
            codes = np.frombuffer(
                context.data, dtype="<u4", count=count, offset=offset
            )
            for axis in range(3):
                component_codes = np.bitwise_and(
                    np.right_shift(codes, axis * 10), 0x3FF
                ).astype(np.float32)
                normalized = np.multiply(
                    component_codes, np.float32(1.0 / 0x3FF)
                )
                values[:, axis] = np.add(
                    np.multiply(np.float32(parameters[axis]), normalized),
                    np.float32(parameters[axis + 4]),
                )
        elif 0x31 <= compression <= 0x33:
            axis = compression - 0x31
            values[:, axis] = np.frombuffer(
                context.data, dtype="<f4", count=count, offset=offset
            )
        elif compression == 0x70:
            codes = np.frombuffer(
                context.data, dtype="<u8", count=count, offset=offset
            )
            for axis in range(3):
                component_codes = np.bitwise_and(
                    np.right_shift(codes, axis * 21), 0x1FFFFF
                ).astype(np.float32)
                values[:, axis] = np.add(
                    np.divide(
                        np.multiply(np.float32(parameters[axis]), component_codes),
                        np.float32(0x1FFFFF),
                    ),
                    np.float32(parameters[axis + 4]),
                )
        elif compression == 0xB0:
            values[:, :3] = np.frombuffer(
                context.data,
                dtype="<f4",
                count=count * 3,
                offset=offset,
            ).reshape(count, 3)
        else:
            raise MotionParseError(
                f"unsupported v65 Quaternion compression 0x{compression:02X}"
            )
        remaining = np.subtract(
            np.float32(1.0), np.multiply(values[:, 0], values[:, 0])
        )
        remaining = np.subtract(
            remaining, np.multiply(values[:, 1], values[:, 1])
        )
        remaining = np.subtract(
            remaining, np.multiply(values[:, 2], values[:, 2])
        )
        values[:, 3] = np.sqrt(np.maximum(np.float32(0.0), remaining))
        return [tuple(row) for row in values.tolist()]

    raise MotionParseError(
        f"unsupported v65 {family.name} compression 0x{compression:02X}"
    )


@dataclass(frozen=True, slots=True)
class TrackEncoding:
    """Writer-local physical representation derived from one semantic track."""

    compression: int
    value_bytes: bytes
    parameters: tuple[float, ...] = ()


def _semantic_array(track: KeyTrack, width: int) -> np.ndarray:
    try:
        values = np.asarray(track.values, dtype="<f4")
    except (TypeError, ValueError, OverflowError) as exc:
        raise MotionWriteError(
            f"{track.family.name} values must be {width}-component numeric tuples"
        ) from exc
    if values.shape != (len(track.values), width):
        raise MotionWriteError(
            f"{track.family.name} values must be {width}-component tuples"
        )
    return values


def encode_track(track: KeyTrack) -> TrackEncoding:
    """Choose the canonical direct DMC5 representation for semantic keys."""

    if track.family == TrackFamily.FLOAT:
        try:
            values = np.asarray(track.values, dtype="<f4")
        except (TypeError, ValueError, OverflowError) as exc:
            raise MotionWriteError("Float track contains a nonnumeric value") from exc
        if values.shape != (len(track.values),):
            raise MotionWriteError("Float track values must be scalar")
        return TrackEncoding(0x00, values.tobytes())

    if track.family == TrackFamily.VECTOR3:
        rows = _semantic_array(track, 3)
        bits = rows.view(np.uint32)
        if bool(np.all(bits[:, 0] == bits[:, 1])) and bool(
            np.all(bits[:, 1] == bits[:, 2])
        ):
            return TrackEncoding(0x34, rows[:, 0].tobytes())
        varying = np.flatnonzero(np.any(bits[1:] != bits[0], axis=0)).tolist()
        if len(varying) == 1:
            axis = varying[0]
            parameters = tuple(float(value) for value in np.min(rows, axis=0))
            return TrackEncoding(
                0x31 + axis,
                rows[:, axis].tobytes(),
                parameters,
            )
        return TrackEncoding(0x00, rows.tobytes())

    if track.family == TrackFamily.QUATERNION:
        rows = _semantic_array(track, 4)
        xyz_bits = rows[:, :3].view(np.uint32)
        axis = next(
            (
                candidate
                for candidate in range(3)
                if bool(
                    np.all(
                        xyz_bits[
                            :,
                            [component for component in range(3) if component != candidate],
                        ]
                        == 0
                    )
                )
            ),
            None,
        )
        if axis is not None:
            return TrackEncoding(
                0x31 + axis,
                rows[:, axis].tobytes(),
            )
        return TrackEncoding(
            0xB0,
            rows[:, :3].astype("<f4", copy=True).tobytes(),
        )

    raise MotionWriteError(f"unsupported semantic track family {track.family!r}")
