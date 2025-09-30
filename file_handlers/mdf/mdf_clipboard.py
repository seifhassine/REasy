import json
import os
from typing import Any, Dict, List, Optional, Tuple

from file_handlers.mdf.mdf_file import (
    MatData,
    MatHeader,
    ParamHeader,
    TexHeader,
    GpbfHeader,
)
from file_handlers.rsz.utils.rsz_clipboard_utils import RszClipboardUtils


class MdfClipboard:
    CLIPBOARD_TYPE = "mdf"
    FILE_NAME = "materials-clipboard.json"
    FORMAT_VERSION = 1

    @classmethod
    def _get_clipboard_file(cls) -> str:
        directory = RszClipboardUtils.get_type_clipboard_directory(cls.CLIPBOARD_TYPE)
        return os.path.join(directory, cls.FILE_NAME)

    @classmethod
    def copy_materials(
        cls,
        materials: List[MatData],
        file_version: int,
        source_file_name: Optional[str] = None,
    ) -> str:
        payload = {
            "format": "mdf_materials",
            "version": cls.FORMAT_VERSION,
            "source_file_version": int(file_version),
            "source_file_name": source_file_name or "",
            "materials": [cls._serialize_material(mat, file_version) for mat in materials],
        }
        clipboard_file = cls._get_clipboard_file()
        with open(clipboard_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        return clipboard_file

    @classmethod
    def load_materials(cls, target_version: int) -> Tuple[List[MatData], Dict[str, Any]]:
        clipboard_file = cls._get_clipboard_file()
        if not os.path.exists(clipboard_file):
            return [], {}
        try:
            with open(clipboard_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return [], {}

        if not isinstance(data, dict):
            return [], {}
        if data.get("format") != "mdf_materials":
            return [], {}

        mats: List[MatData] = []
        for entry in data.get("materials", []):
            if isinstance(entry, dict):
                mats.append(cls._deserialize_material(entry, target_version))
        metadata = {
            "source_file_version": int(data.get("source_file_version", 0) or 0),
            "source_file_name": str(data.get("source_file_name", "") or ""),
            "format_version": int(data.get("version", 0) or 0),
        }
        return mats, metadata

    @classmethod
    def _serialize_material(cls, mat: MatData, file_version: int) -> Dict[str, Any]:
        header = mat.header
        header_data: Dict[str, Any] = {
            "mat_name": header.mat_name,
            "mmtr_path": header.mmtr_path,
            "shader_type": int(header.shader_type),
            "alpha_flags": int(header.alpha_flags),
        }
        if file_version == 6:
            header_data["ukn_re7"] = int(header.ukn_re7)
        if file_version >= 31:
            header_data["ukn"] = int(header.ukn)
            header_data["ukn1"] = int(header.ukn1)
        if header.texID_count > 0:
            header_data["texID_count"] = int(header.texID_count)

        textures = [
            {
                "tex_type": tex.tex_type,
                "tex_path": tex.tex_path,
            }
            for tex in mat.textures
        ]

        parameters = [
            {
                "name": param.name,
                "component_count": int(param.component_count),
                "component_ukn": int(param.component_ukn),
                "gap_size": int(param.gap_size),
                "parameter": [float(v) for v in param.parameter],
            }
            for param in mat.parameters
        ]

        gpu_buffers = [
            {
                "name": name_hdr.name,
                "data": data_hdr.name,
            }
            for name_hdr, data_hdr in mat.gpu_buffers
        ]

        tex_id_arrays: List[Dict[str, Any]] = []
        if file_version >= 31 and header.texID_count > 0:
            for counts, elems in mat.tex_id_arrays:
                tex_id_arrays.append(
                    {
                        "counts": [int(v) for v in counts],
                        "elements": [int(v) for v in elems],
                    }
                )

        return {
            "header": header_data,
            "textures": textures,
            "parameters": parameters,
            "gpu_buffers": gpu_buffers,
            "tex_id_arrays": tex_id_arrays,
        }

    @classmethod
    def _deserialize_material(cls, data: Dict[str, Any], target_version: int) -> MatData:
        header_info = data.get("header", {})
        header = MatHeader()
        header.mat_name = str(header_info.get("mat_name", ""))
        header.mmtr_path = str(header_info.get("mmtr_path", ""))
        header.shader_type = int(header_info.get("shader_type", 0))
        header.alpha_flags = int(header_info.get("alpha_flags", 0))
        if target_version == 6:
            header.ukn_re7 = int(header_info.get("ukn_re7", 0))
        if target_version >= 31:
            header.ukn = int(header_info.get("ukn", 0))
            header.ukn1 = int(header_info.get("ukn1", 0))

        textures: List[TexHeader] = []
        for tex_info in data.get("textures", []):
            if not isinstance(tex_info, dict):
                continue
            tex = TexHeader()
            tex.tex_type = str(tex_info.get("tex_type", ""))
            tex.tex_path = str(tex_info.get("tex_path", ""))
            textures.append(tex)

        parameters: List[ParamHeader] = []
        for par_info in data.get("parameters", []):
            if not isinstance(par_info, dict):
                continue
            param = ParamHeader()
            param.name = str(par_info.get("name", ""))
            param.component_count = int(par_info.get("component_count", 0))
            param.component_ukn = int(par_info.get("component_ukn", 0))
            param.gap_size = int(par_info.get("gap_size", 0))
            raw_values = par_info.get("parameter", [0.0, 0.0, 0.0, 0.0])
            values = list(raw_values) if isinstance(raw_values, (list, tuple)) else [raw_values]
            while len(values) < 4:
                values.append(0.0)
            param.parameter = tuple(float(v) for v in values[:4])
            parameters.append(param)

        gpu_buffers = []
        if target_version >= 19:
            for gpbf_info in data.get("gpu_buffers", []):
                if not isinstance(gpbf_info, dict):
                    continue
                name_hdr = GpbfHeader()
                data_hdr = GpbfHeader()
                name_hdr.name = str(gpbf_info.get("name", ""))
                data_hdr.name = str(gpbf_info.get("data", ""))
                gpu_buffers.append((name_hdr, data_hdr))

        tex_id_arrays: List[tuple[List[int], List[int]]] = []
        if target_version >= 31:
            for arr_info in data.get("tex_id_arrays", []):
                if not isinstance(arr_info, dict):
                    continue
                counts = [int(v) for v in arr_info.get("counts", [])]
                elems = [int(v) for v in arr_info.get("elements", [])]
                tex_id_arrays.append((counts, elems))
            header.texID_count = max(len(tex_id_arrays), int(header_info.get("texID_count", 0)))
            while len(tex_id_arrays) < header.texID_count:
                tex_id_arrays.append(([], []))
        else:
            header.texID_count = 0

        mat = MatData()
        mat.header = header
        mat.textures = textures
        mat.parameters = parameters
        mat.gpu_buffers = gpu_buffers if target_version >= 19 else []
        mat.tex_id_arrays = tex_id_arrays if target_version >= 31 else []
        return mat

