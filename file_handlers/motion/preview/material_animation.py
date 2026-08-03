from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from file_handlers.mesh.material_session import scoped_material_key

from ..evaluation import DeformationTarget
from ..runtime import MotionMaterialController


@dataclass(frozen=True, slots=True)
class MaterialAnimationBinding:
    material_scope: str
    controller: MotionMaterialController


@dataclass(frozen=True, slots=True)
class MaterialAnimationResolution:
    bindings: tuple[MaterialAnimationBinding, ...]
    diagnostics: tuple[str, ...]


class SceneMaterialAnimationResolver:
    """Resolve semantic material controllers onto loaded scene meshes."""

    def __init__(
        self,
        load_controllers: Callable[
            [object],
            tuple[MotionMaterialController, ...],
        ],
    ) -> None:
        self._load_controllers = load_controllers
        self._cache: dict[object, tuple[MotionMaterialController, ...]] = {}

    def clear(self) -> None:
        self._cache.clear()

    def resolve(
        self,
        documents: Mapping[str, object],
        mesh_bindings: Iterable[object],
        material_scope_for: Callable[[object], str],
        *,
        root_rsz: object | None = None,
        root_controllers: tuple[MotionMaterialController, ...] = (),
    ) -> MaterialAnimationResolution:
        mesh_bindings = tuple(mesh_bindings)
        bindings = {}
        diagnostics = []
        for document_id, document in documents.items():
            controllers = self._controllers_for(
                document,
                root_rsz,
                root_controllers,
            )
            for controller in controllers:
                matches = tuple(
                    binding
                    for binding in mesh_bindings
                    if binding.asset.renderable.source_object_id.document_id
                    == document_id
                    and binding.asset.renderable.source_object_id.local_object_id
                    == controller.provider_object_id
                )
                if not matches:
                    diagnostics.append(
                        f"{document.source_path}: material controller "
                        f"#{controller.provider_instance_id} has no loaded "
                        "mesh on its GameObject."
                    )
                    continue
                for binding in matches:
                    parameters = self._resolved_parameters(
                        binding,
                        controller,
                    )
                    if not parameters:
                        continue
                    scope = material_scope_for(binding.asset.renderable)
                    bindings[scope, controller.provider_instance_id] = (
                        MaterialAnimationBinding(
                            scope,
                            replace(controller, parameters=parameters),
                        )
                    )
        return MaterialAnimationResolution(
            tuple(bindings.values()),
            tuple(dict.fromkeys(diagnostics)),
        )

    def _controllers_for(
        self,
        document,
        root_rsz: object | None,
        root_controllers: tuple[MotionMaterialController, ...],
    ) -> tuple[MotionMaterialController, ...]:
        cached = self._cache.get(document.rsz_file)
        if cached is not None:
            return cached
        try:
            cached = (
                root_controllers
                if document.rsz_file is root_rsz
                else tuple(self._load_controllers(document.rsz_file))
            )
        except (ValueError, RuntimeError) as exc:
            raise ValueError(
                f"{document.source_path}: material animation cannot be "
                f"resolved: {exc}"
            ) from exc
        self._cache[document.rsz_file] = cached
        return cached

    @staticmethod
    def _resolved_parameters(
        binding,
        controller: MotionMaterialController,
    ):
        material_names = set(
            getattr(binding.target.mesh, "material_names", ()) or ()
        )
        required = {
            parameter.material_name for parameter in controller.parameters
        }
        missing = required - material_names
        if missing:
            raise ValueError(
                f"{binding.target.label}: material animation targets "
                f"missing material(s) {', '.join(sorted(missing))}"
            )
        return controller.parameters


def material_deformation_targets(
    bindings: Iterable[MaterialAnimationBinding],
    property_name_hash: Callable[[str], int] | None,
) -> tuple[DeformationTarget, ...]:
    """Expose each material input to the existing scalar MOT evaluator."""

    names = tuple(dict.fromkeys(
        name
        for binding in bindings
        for name in binding.controller.source_names
    ))
    if not names:
        return ()
    if property_name_hash is None:
        raise ValueError("material animation requires a property-name hash")
    targets = tuple(
        DeformationTarget(None, name, property_name_hash(name))
        for name in names
    )
    by_hash: dict[int, str] = {}
    for target in targets:
        assert target.property_hash is not None
        previous = by_hash.setdefault(target.property_hash, target.name)
        if previous != target.name:
            raise ValueError(
                f"material inputs {previous!r} and {target.name!r} "
                f"share property hash 0x{target.property_hash:08X}"
            )
    return targets


def evaluate_material_parameters(
    bindings: Iterable[MaterialAnimationBinding],
    deformation_weights: Iterable[tuple[str, float]],
) -> dict[str, dict[str, float]]:
    """Evaluate scoped material values from the composed scalar motion state."""

    sources = dict(deformation_weights)
    result: dict[str, dict[str, float]] = {}
    for binding in bindings:
        controller = binding.controller
        for parameter in controller.parameters:
            material_key = scoped_material_key(
                binding.material_scope,
                parameter.material_name,
            )
            values = result.setdefault(material_key, {})
            value = parameter.evaluate(sources.get(parameter.source_name, 0.0))
            previous = values.setdefault(parameter.parameter_name, value)
            if previous != value:
                raise ValueError(
                    f"material parameter {material_key}."
                    f"{parameter.parameter_name} has conflicting animation values"
                )
    return result
