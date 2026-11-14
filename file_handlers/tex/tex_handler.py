import struct
from collections.abc import Callable
from typing import Optional

lazy from utils.resource_file_utils import (
    ResourceResolutionContext,
    resource_context_for_handler,
    resource_path_at_version,
    resource_version_from_path,
    streaming_companion_path,
)

from . import authoring
lazy from .kinds import TextureKind, kind_catalog
lazy from .tex_file import TexFile, TEX_MAGIC
lazy from .dds import CUBEMAP_FACES, DDS_MISC_TEXTURECUBE, build_dds_dx10
from .texture_handler import TextureViewerHandler


def _is_streamed(path: str) -> bool:
    return "streaming" in (part.casefold() for part in path.replace("\\", "/").split("/"))


def _container(data: bytes, declared: int) -> TexFile:
    container = TexFile()
    if not container.read(data, file_version=declared):
        raise authoring.AuthoringError("it is not a .tex container")
    return container


class TexHandler(TextureViewerHandler):

    def __init__(self):
        super().__init__()
        self.tex: Optional[TexFile] = None
        self._companion: Optional[tuple[str, bytes]] = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic == TEX_MAGIC

    def read(self, data: bytes):
        self.raw_data = data
        tex = TexFile()
        filepath = getattr(self, "filepath", "") or ""
        file_version = resource_version_from_path(filepath, "tex") or 0

        ok = tex.read(data, file_version=file_version)

        if not ok:
            raise ValueError("Failed to parse TEX file")
        self.tex = tex
        self._companion = None
        self.modified = False

    def supports_editing(self) -> bool:
        return True

    def rebuild(self) -> bytes:
        if not self.tex:
            return bytes(self.raw_data)
        return self.tex.serialize()

    def convert_to(
        self,
        version: int,
        *,
        type_name: Optional[str] = None,
        type_index: Optional[int] = None,
    ) -> tuple[str, ...]:
        if self.tex is None:
            raise authoring.AuthoringError("no .tex is open")
        authored = authoring.convert(
            self.tex, version=version, type_name=type_name, type_index=type_index
        )
        companion, notes = self._convert_companion(version, type_name, type_index)
        self._adopt(authored)
        self._companion = companion
        return (*authored.notes, *notes)

    def import_image(
        self,
        data: bytes,
        *,
        version: Optional[int] = None,
        type_name: Optional[str] = None,
        type_index: Optional[int] = None,
        run: Callable = lambda work: work(),
    ) -> tuple[str, ...]:
        return self._replace(lambda chain, tail, target: authoring.import_image(
            data, chain, version=target, tail=tail, type_name=type_name, type_index=type_index), version, run)

    def import_lanes(
        self,
        compose: Callable,
        *,
        version: Optional[int] = None,
        type_name: Optional[str] = None,
        type_index: Optional[int] = None,
        run: Callable = lambda work: work(),
    ) -> tuple[str, ...]:
        def author(chain, tail, target):
            try:
                image = compose(chain)
            except (OSError, ValueError) as error:
                raise authoring.AuthoringError(str(error)) from None
            return authoring.replace_image(image, chain, version=target, tail=tail, type_name=type_name,
                                           type_index=type_index)
        return self._replace(author, version, run)

    def _replace(self, author: Callable, version: Optional[int], run: Callable) -> tuple[str, ...]:
        if self.tex is None:
            raise authoring.AuthoringError("no .tex is open")
        resource_path, partner_path, source, declared = self._partner()
        streamed = _is_streamed(resource_path)
        copy = "resident copy" if streamed else "streamed copy"
        try:
            partner = _container(source, declared) if source else None
        except ValueError as error:
            raise authoring.AuthoringError(f"its {copy} cannot be read: {error}") from None
        swapped = partner is not None and not streamed
        chain, tail = (partner, self.tex) if swapped else (self.tex, partner)
        target = int(self.tex.header.version if version is None else version)
        full, resident = run(lambda: author(chain, tail, target))
        main, companion = (resident, full) if swapped else (full, resident)
        self._adopt(main)
        self._companion = None if companion is None else (partner_path, companion.data)
        pair = (
            [f"its {copy} is rebuilt from the image and written beside this file"] if companion
            else [f"its {copy} was not found, so only this file is written"] if streamed else []
        )
        return tuple(dict.fromkeys((*main.notes, *(companion.notes if companion else ()), *pair)))

    def texture_kind(self) -> tuple[Optional["TextureKind"], tuple]:
        context = resource_context_for_handler(self)
        catalog = kind_catalog(self.tex.header.version, context.game if context else "")
        if catalog is None:
            return None, ()
        path = self.filepath or ""
        return catalog.kind(path), catalog.lanes(path, self.tex.header.tex_flags.type_name)

    def full_chain(self) -> "TexFile":
        resource_path, _path, source, declared = self._partner()
        return self.tex if not source or _is_streamed(resource_path) else _container(source, declared)

    def _partner(self) -> tuple[str, Optional[str], bytes, int]:
        context = resource_context_for_handler(self)
        resource_path = self._resource_path(context) if context is not None else (self.filepath or "")
        if self._companion is not None:
            return resource_path, *self._companion, 0
        path = streaming_companion_path(resource_path)
        found = context.resolve(path, allow_selection_dialog=False) if path and context else None
        if found is None:
            return resource_path, None, b"", 0
        return resource_path, *found, resource_version_from_path(found[0], "tex") or 0

    def _convert_companion(
        self, version: int, type_name: Optional[str], type_index: Optional[int]
    ) -> tuple[Optional[tuple[str, bytes]], list[str]]:
        resource_path, source_path, source, declared = self._partner()
        if not source:
            return None, []
        copy = "resident copy" if _is_streamed(resource_path) else "streamed copy"
        try:
            authored = authoring.convert(
                _container(source, declared), version=version, type_name=type_name, type_index=type_index
            )
        except ValueError as error:
            name = str(source_path).replace("\\", "/").rsplit("/", 1)[-1]
            return None, [f"its {copy} ({name}) is left as it is: {error}"]
        return (source_path, authored.data), [f"its {copy} is converted with it and written beside this file"]

    def _resource_path(self, context: ResourceResolutionContext) -> str:
        path = (self.filepath or "").replace("\\", "/")
        for root in (context.project_dir, context.unpacked_dir):
            root = str(root or "").replace("\\", "/").rstrip("/")
            if root and path.casefold().startswith(root.casefold() + "/"):
                return path[len(root) + 1 :]
        return path

    def _adopt(self, authored: "authoring.Authored") -> None:
        if self.filepath:
            self.filepath = resource_path_at_version(self.filepath, "tex", authored.version)
        self.read(authored.data)
        self.modified = True

    def related_output_targets(self, primary_path: str) -> dict[str, bytes]:
        target = streaming_companion_path(str(primary_path))
        if self._companion is None or target is None:
            return {}
        return {target: self._companion[1]}

    def pending_related_outputs(self) -> dict[str, bytes]:
        return self.related_output_targets(self.filepath or "")

    def mark_related_outputs_saved(self) -> None:
        self._companion = None

    def discard_related_outputs(self) -> None:
        self._companion = None

    def build_dds_bytes(self) -> bytes:
        return tex_to_dds(self.tex) if self.tex else b""


def tex_to_dds(tex: TexFile) -> bytes:
    header = tex.header
    if header.is_volume and header.image_count > 1:
        raise ValueError(
            f"a {header.image_count}-image volume has no DDS equivalent: a 3D resource "
            "cannot also be an array"
        )

    cubemap = header.is_cubemap
    payload = [
        tex.read_mip_level(level, image)
        for image in range(header.image_count)
        for level in range(header.mip_count)
    ]

    dds_header = build_dds_dx10(
        width=header.width,
        height=header.height,
        mip_count=header.mip_count,
        dxgi_format=header.format,
        array_size=header.image_count // CUBEMAP_FACES if cubemap else header.image_count,
        depth=header.depth,
        misc_flags=DDS_MISC_TEXTURECUBE if cubemap else 0,
    )
    return dds_header + b"".join(payload)
