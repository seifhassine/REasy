from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
lazy from file_handlers.mdf.material_layout import MaterialDescriptorLayout

lazy from file_handlers.sdf.models import SdfDocument, SdfProgram
lazy from .profiles import MmtrGameProfile


@dataclass(frozen=True, slots=True)
class MmtrSemanticNote:
    field: str
    message: str
    status: str = "unknown"


@dataclass(frozen=True, slots=True)
class MmtrNameSemantics:
    family: str
    shader_type_id: int | None
    shader_type: str
    input_type_id: int | None
    input_type: str
    geometry: str
    alpha_test: bool = False
    two_sided: bool = False  # Shading permutation; culling belongs to rasterizer_state.
    instanced: bool = False  # Renderer permutation, not a required SV_InstanceID input.
    clip_distance: bool = False
    direct: bool = False
    secondary_world_source: bool = False
    with_normals: bool = False
    render_output: str = "default"
    features: tuple[str, ...] = ()
    notes: tuple[MmtrSemanticNote, ...] = ()


@dataclass(frozen=True, slots=True)
class MmtrProgramContext:
    program: "SdfProgram"
    program_count: int
    depth_writes: bool | None


@dataclass(frozen=True, slots=True)
class MmtrProgramSemantics:
    target_index: int
    program_index: int
    name: str
    descriptor: MmtrNameSemantics
    vertex_inputs: tuple[str, ...] = ()
    skinning_influence_count: int | None = None
    notes: tuple[MmtrSemanticNote, ...] = ()
    shader_role: str = "pixel"
    # Set only while a build generates the shader, as are the fields below.
    purpose: str = ""
    surface_data: tuple = ()
    # ``vertex_value_refusals`` holds (source, reason) pairs.
    vertex_values: tuple = ()
    vertex_value_refusals: tuple = ()


@dataclass(frozen=True, slots=True)
class MmtrMaterialParameter:
    name: str
    byte_offset: int
    byte_size: int
    type_names: tuple[str, ...]
    program_count: int


@dataclass(frozen=True, slots=True)
class MmtrResourceContract:
    resource_kind: str
    name: str
    type_names: tuple[str, ...]


BINDLESS_PARAMETER_STORAGES = frozenset({"bindless-material-constant-buffer", "mixed-material-constant-buffer"})


@dataclass(frozen=True, slots=True)
class MmtrMaterialInterface:
    user_buffer_sizes: tuple[int, ...] = ()
    parameters: tuple[MmtrMaterialParameter, ...] = ()
    resources: tuple[MmtrResourceContract, ...] = ()
    parameter_storage: str = "none"
    descriptor_layouts: tuple[MaterialDescriptorLayout, ...] = ()
    shader_lod: int | None = None


class LazyProgramSemantics(Sequence):
    def __init__(self, keys: tuple[tuple[int, int], ...],
                 semantics: Callable[[int, int], MmtrProgramSemantics]):
        self._keys = keys
        self._known = frozenset(keys)
        self._semantics = semantics
        self._made: dict[tuple[int, int], MmtrProgramSemantics] = {}
        self._lock = threading.RLock()

    def program(self, target_index: int, program_index: int) -> MmtrProgramSemantics:
        key = (target_index, program_index)
        if key not in self._known:
            raise KeyError(key)
        found = self._made.get(key)
        if found is None:
            with self._lock:
                found = self._made.get(key)
                if found is None:
                    found = self._made[key] = self._semantics(*key)
        return found

    def __len__(self) -> int:
        return len(self._keys)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self.program(*key) for key in self._keys[index])
        return self.program(*self._keys[index])


@dataclass(slots=True)
class MmtrDocument:
    profile: "MmtrGameProfile"
    sdf: "SdfDocument"
    family: str
    programs: tuple[MmtrProgramSemantics, ...] | LazyProgramSemantics
    material_interface: MmtrMaterialInterface = field(
        default_factory=MmtrMaterialInterface
    )
    source_label: str = ""
    notes: tuple[MmtrSemanticNote, ...] = ()
    # Sizes of the constant buffers named exactly as the profile's user material buffers.
    material_buffer_sizes: tuple[int, ...] = field(kw_only=True)
    material_family: object | None = field(default=None, repr=False)
    _program_lookup: dict[tuple[int, int], MmtrProgramSemantics] = field(
        default_factory=dict,
        repr=False,
    )
    _shader_contract_cache: dict[tuple[str, int, int, str], object] = field(
        default_factory=dict,
        repr=False,
    )
    _native_shader_contract_cache: dict[str, object] = field(
        default_factory=dict,
        repr=False,
    )
    _material_contract_cache: dict[tuple[int, int, str, bool], object] = field(
        default_factory=dict,
        repr=False,
    )
    _material_buffer_contract_cache: dict[tuple, tuple] = field(
        default_factory=dict, repr=False,
    )
    _material_program_routes_cache: dict[str, tuple[tuple[int, int, str], ...]] = field(
        default_factory=dict, repr=False,
    )
    _material_texture_contracts_cache: dict[str, tuple[MmtrResourceContract, ...]] = field(default_factory=dict, repr=False)
    _native_input_usage_cache: dict[str, object] = field(default_factory=dict, repr=False)
    _material_input_usage_cache: dict[MaterialDescriptorLayout | None, object] = field(default_factory=dict, repr=False)
    _pass_purpose_cache: dict[tuple[int, int], str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.programs, LazyProgramSemantics):
            self._program_lookup = {
                (program.target_index, program.program_index): program
                for program in self.programs
            }

    @property
    def skinning_contracts(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    program.skinning_influence_count
                    for program in self.programs
                    if program.skinning_influence_count is not None
                }
            )
        )

    def program(self, target_index: int, program_index: int) -> MmtrProgramSemantics:
        if isinstance(self.programs, LazyProgramSemantics):
            return self.programs.program(target_index, program_index)
        return self._program_lookup[(target_index, program_index)]

    def write(self) -> bytes:
        return self.sdf.write_canonical()


__all__ = [name for name in globals() if name.startswith("Mmtr")]
