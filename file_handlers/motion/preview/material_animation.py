from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

lazy from file_handlers.mesh.material_session import scoped_material_key
from file_handlers.rsz.scn_scene_graph import ScnObjectId

from ..evaluation import DeformationTarget
from ..runtime import MotionMaterialController


@dataclass(frozen=True, slots=True)
class MaterialAnimationBinding:
    material_scope: str
    controller: MotionMaterialController
    object_id: ScnObjectId
    channels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaterialAnimationResolution:
    bindings: tuple[MaterialAnimationBinding, ...]
    diagnostics: tuple[str, ...]


class SceneMaterialAnimationResolver:
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
                    mesh = binding.target.mesh
                    names = set(getattr(mesh, "material_names", ()) or ())
                    parameters = tuple(item for item in controller.parameters if item.material_name in names)
                    missing = sorted({item.material_name for item in controller.parameters} - names)
                    if missing:
                        diagnostics.append(
                            f"{binding.target.label}: material controller #{controller.provider_instance_id} "
                            f"names material(s) the mesh lacks: {', '.join(missing)}."
                        )
                    if not parameters:
                        continue
                    scope = material_scope_for(binding.asset.renderable)
                    bindings[scope, controller.provider_instance_id] = MaterialAnimationBinding(
                        scope,
                        replace(controller, parameters=parameters),
                        binding.asset.renderable.source_object_id,
                        _channel_names(mesh) if controller.held_by_channels else (),
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


def _channel_names(mesh) -> tuple[str, ...]:
    """Ordered as the mesh component's channel name hashes."""
    data = getattr(mesh, "blend_shape_data", None)
    return tuple(channel.name for channel in getattr(data, "channels", ()) or ())


def material_deformation_targets(
    bindings: Iterable[MaterialAnimationBinding],
    property_name_hash: Callable[[str], int] | None,
) -> tuple[DeformationTarget, ...]:
    names = tuple(dict.fromkeys(
        name
        for binding in bindings
        for name in (*binding.controller.source_names, *binding.channels)
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


class MaterialAnimation:
    def __init__(self, bindings: Iterable[MaterialAnimationBinding] = ()) -> None:
        self.bindings = tuple(bindings)
        self._held: dict[int, tuple[tuple[float, ...], dict[tuple[str, str], float]]] = {}

    def restart(self) -> None:
        self._held.clear()

    def evaluate(
        self,
        deformation_weights: Iterable[tuple[str, float]],
        animated: ScnObjectId,
    ) -> dict[str, dict[str, float]]:
        sources = dict(deformation_weights)
        result: dict[str, dict[str, float]] = {}
        for index, binding in enumerate(self.bindings):
            if binding.object_id != animated:
                continue
            controller = binding.controller
            if controller.held_by_channels:
                channels = tuple(sources.get(name, 0.0) for name in binding.channels)
                held = self._held.get(index, ((0.0,) * len(channels), {}))
                if channels != held[0]:
                    held = self._held[index] = channels, controller.values(sources)
                values = held[1]
            else:
                values = controller.values(sources)
            for (material, parameter), value in values.items():
                material_key = scoped_material_key(binding.material_scope, material)
                previous = result.setdefault(material_key, {}).setdefault(parameter, value)
                if previous != value:
                    raise ValueError(
                        f"material parameter {material_key}.{parameter} has conflicting animation values"
                    )
        return result
