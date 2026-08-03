from __future__ import annotations

from dataclasses import dataclass

from file_handlers.mesh.mesh_handler import MeshHandler
from utils.resource_file_utils import ResourceResolutionContext

from ..evaluation.mesh_adapter import rig_from_re_engine_mesh
from ..evaluation.model import Rig


@dataclass(frozen=True, slots=True)
class RigPreviewTarget:
    """A target rig and its optional renderable source asset."""

    label: str
    rig: Rig
    mesh: object | None = None
    handler: MeshHandler | None = None


def motion_target_from_mesh_handler(
    label: str,
    handler: MeshHandler,
) -> RigPreviewTarget:
    if handler.mesh is None:
        raise ValueError("target mesh did not produce a parsed model")
    return RigPreviewTarget(
        label=label,
        rig=rig_from_re_engine_mesh(handler.mesh),
        mesh=handler.mesh,
        handler=handler,
    )


def load_re_engine_mesh_target(
    filepath: str,
    data: bytes,
    *,
    app=None,
    resource_context: ResourceResolutionContext | None = None,
) -> RigPreviewTarget:
    if not filepath:
        raise ValueError("target mesh needs a path with a numeric version suffix")
    if not MeshHandler.can_handle(data):
        raise ValueError("target is not an RE Engine mesh")
    try:
        handler = MeshHandler.from_bytes(
            filepath,
            data,
            app=app,
            resource_context=resource_context,
        )
    except Exception as exc:
        raise ValueError(f"could not parse target mesh: {exc}") from exc
    return motion_target_from_mesh_handler(filepath, handler)
