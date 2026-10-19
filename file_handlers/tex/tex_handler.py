import struct
from typing import Optional

from utils.resource_file_utils import (
    ResourceResolutionContext,
    resource_context_for_handler,
    resource_path_at_version,
    resource_version_from_path,
    streaming_companion_path,
)

from . import authoring
from .tex_file import TexFile, TEX_MAGIC
from .dds import CUBEMAP_FACES, DDS_MISC_TEXTURECUBE, build_dds_dx10
from .texture_handler import TextureViewerHandler


def _is_streamed(path: str) -> bool:
    return "streaming" in (part.casefold() for part in path.replace("\\", "/").split("/"))


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

    def import_dds(
        self,
        data: bytes,
        *,
        version: Optional[int] = None,
        name: str = "",
        type_name: Optional[str] = None,
        type_index: Optional[int] = None,
    ) -> tuple[str, ...]:
        if self.tex is None:
            raise authoring.AuthoringError("no .tex is open")
        target = int(self.tex.header.version if version is None else version)
        authored = authoring.import_dds(
            data,
            version=target,
            name=name or self.filepath,
            type_name=type_name,
            type_index=type_index,
        )
        self._adopt(authored)
        return authored.notes

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

    def _convert_companion(
        self, version: int, type_name: Optional[str], type_index: Optional[int]
    ) -> tuple[Optional[tuple[str, bytes]], list[str]]:
        context = resource_context_for_handler(self)
        resource_path = self._resource_path(context) if context is not None else (self.filepath or "")
        source_path, source = self._companion or (None, b"")
        declared = 0
        if not source:
            path = streaming_companion_path(resource_path)
            found = context.resolve(path, allow_selection_dialog=False) if path and context else None
            if found is None:
                return None, []
            source_path, source = found
            declared = resource_version_from_path(source_path, "tex") or 0
        copy = "resident copy" if _is_streamed(resource_path) else "streamed copy"
        try:
            container = TexFile()
            if not container.read(source, file_version=declared):
                raise authoring.AuthoringError("it is not a .tex container")
            authored = authoring.convert(
                container, version=version, type_name=type_name, type_index=type_index
            )
        except (authoring.AuthoringError, ValueError) as error:
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
        if not self.tex:
            return b""
        tex = self.tex
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
