import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from file_handlers.mdf.mdf_clipboard import MdfClipboard


class MdfTemplateManager:
    TEMPLATE_FORMAT = "mdf_material_template"
    TEMPLATE_VERSION = 1

    @classmethod
    def _root_dir(cls) -> str:
        return os.path.join(os.getcwd(), "templates", "mdf")

    @classmethod
    def _metadata_path(cls) -> str:
        return os.path.join(cls._root_dir(), "template_metadata.json")

    @classmethod
    def _ensure_root(cls) -> None:
        os.makedirs(cls._root_dir(), exist_ok=True)

    @classmethod
    def load_metadata(cls) -> Dict[str, Any]:
        path = cls._metadata_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                data = None
            if isinstance(data, dict):
                data.setdefault("templates", {})
                return data
        return {"templates": {}}

    @classmethod
    def save_metadata(cls, metadata: Dict[str, Any]) -> bool:
        try:
            cls._ensure_root()
            with open(cls._metadata_path(), "w", encoding="utf-8") as fh:
                json.dump(metadata, fh, indent=2)
            return True
        except Exception:
            return False

    @classmethod
    def _sanitize(cls, name: str) -> Optional[str]:
        safe = ''.join(c if c.isalnum() or c in " -_" else '_' for c in name).strip()
        return safe if safe else None

    @classmethod
    def export_material(
        cls,
        material,
        file_version: int,
        template_name: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        source_file_name: str = "",
    ) -> Dict[str, Any]:
        if material is None:
            return {"success": False, "message": "No material selected for export."}
        safe_name = cls._sanitize(template_name or "")
        if not safe_name:
            return {"success": False, "message": "Template name is required."}

        metadata, templates = cls._load_templates()
        if safe_name in templates:
            return {"success": False, "message": "A template with that name already exists."}

        cls._ensure_root()
        template_path = os.path.join(cls._root_dir(), f"{safe_name}.json")
        if os.path.exists(template_path):
            return {"success": False, "message": "A template file with that name already exists."}

        try:
            payload = {
                "format": cls.TEMPLATE_FORMAT,
                "version": cls.TEMPLATE_VERSION,
                "source_file_version": int(file_version),
                "source_file_name": source_file_name or "",
                "material": MdfClipboard._serialize_material(material, int(file_version)),
            }
            with open(template_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except Exception as exc:
            return {"success": False, "message": f"Failed to write template: {exc}"}

        tags_list = [t.strip() for t in (tags or []) if t and t.strip()]
        timestamp = datetime.now().isoformat()
        templates[safe_name] = {
            "id": safe_name,
            "name": template_name,
            "description": description,
            "tags": tags_list,
            "path": template_path,
            "created": timestamp,
            "modified": timestamp,
            "source_version": int(file_version),
            "source_file_name": source_file_name or "",
        }
        cls.save_metadata(metadata)
        return {
            "success": True,
            "template_id": safe_name,
            "path": template_path,
        }

    @classmethod
    def get_template_list(cls) -> List[Dict[str, Any]]:
        metadata = cls.load_metadata()
        templates = []
        for template_id, info in metadata.get("templates", {}).items():
            path = info.get("path")
            if not path or not os.path.exists(path):
                continue
            entry = dict(info)
            entry["id"] = template_id
            templates.append(entry)
        templates.sort(key=lambda item: item.get("name", ""))
        return templates

    @classmethod
    def get_all_tags(cls) -> List[str]:
        tags = set()
        for info in cls.get_template_list():
            for tag in info.get("tags", []):
                tags.add(tag)
        return sorted(tags)

    @classmethod
    def import_template(cls, template_id: str, target_version: int) -> Dict[str, Any]:
        metadata, templates = cls._load_templates()
        info = templates.get(template_id)
        if not info:
            return {"success": False, "message": "Template not found."}

        payload = cls._read_template_payload(info.get("path"))
        if isinstance(payload, dict):
            if payload.get("format") != cls.TEMPLATE_FORMAT:
                payload = None
        if not payload:
            return {"success": False, "message": "Template file is missing or invalid."}

        material_payload = payload.get("material")
        if not isinstance(material_payload, dict):
            return {"success": False, "message": "Template is missing material data."}

        mat = MdfClipboard._deserialize_material(material_payload, int(target_version))
        info = dict(info)
        try:
            info["source_version"] = int(payload.get("source_file_version", info.get("source_version", 0)) or 0)
        except Exception:
            pass
        if payload.get("source_file_name"):
            info["source_file_name"] = payload.get("source_file_name")
        info["last_used"] = datetime.now().isoformat()
        templates[template_id] = info
        cls.save_metadata(metadata)
        return {
            "success": True,
            "material": mat,
            "metadata": info,
            "template_id": template_id,
        }

    @classmethod
    def update_template_metadata(
        cls,
        template_id: str,
        name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        metadata, templates = cls._load_templates()
        info = templates.get(template_id)
        if not info:
            return {"success": False, "message": "Template not found."}

        current_path = info.get("path", "")
        new_id = template_id
        if name is not None:
            safe = cls._sanitize(name)
            if not safe:
                return {"success": False, "message": "Template name is required."}
            if safe != template_id and safe in templates:
                return {"success": False, "message": "Another template already uses that name."}
            new_path = os.path.join(cls._root_dir(), f"{safe}.json")
            try:
                if current_path and os.path.exists(current_path):
                    cls._ensure_root()
                    os.replace(current_path, new_path)
            except Exception as exc:
                return {"success": False, "message": f"Failed to rename template file: {exc}"}
            info["name"] = name
            info["path"] = new_path
            new_id = safe

        if tags is not None:
            info["tags"] = [t.strip() for t in tags if t and t.strip()]

        if description is not None:
            info["description"] = description

        info["modified"] = datetime.now().isoformat()

        if new_id != template_id:
            templates[new_id] = info
            del templates[template_id]
            template_id = new_id

        cls.save_metadata(metadata)
        return {"success": True, "template_id": template_id}

    @classmethod
    def get_template_preview(cls, template_id: str) -> Dict[str, Any]:
        metadata, templates = cls._load_templates()
        info = templates.get(template_id)
        if not info:
            return {}
        payload = cls._read_template_payload(info.get("path"))
        if not isinstance(payload, dict):
            return {}
        material = payload.get("material")
        if not isinstance(material, dict):
            return {}
        header = material.get("header")
        if not isinstance(header, dict):
            header = {}
        textures = material.get("textures")
        if not isinstance(textures, list):
            textures = []
        parameters = material.get("parameters")
        if not isinstance(parameters, list):
            parameters = []
        gpu_buffers = material.get("gpu_buffers")
        if not isinstance(gpu_buffers, list):
            gpu_buffers = []
        tex_arrays = material.get("tex_id_arrays")
        if not isinstance(tex_arrays, list):
            tex_arrays = []
        preview = {
            "material_name": header.get("mat_name", ""),
            "mmtr_path": header.get("mmtr_path", ""),
            "shader_type": header.get("shader_type"),
            "texture_count": len(textures),
            "parameter_count": len(parameters),
            "gpu_buffer_count": len(gpu_buffers),
            "tex_id_count": len(tex_arrays),
            "texture_types": [
                str(entry.get("tex_type", ""))
                for entry in textures[:5]
                if isinstance(entry, dict) and entry.get("tex_type")
            ],
        }
        return preview

    @classmethod
    def delete_template(cls, template_id: str) -> bool:
        metadata, templates = cls._load_templates()
        info = templates.get(template_id)
        if not info:
            return False
        path = info.get("path")
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            return False
        del templates[template_id]
        cls.save_metadata(metadata)
        return True

    @classmethod
    def _load_templates(cls) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        metadata = cls.load_metadata()
        templates = metadata.setdefault("templates", {})
        return metadata, templates

    @classmethod
    def _read_template_payload(cls, path: Optional[str]) -> Optional[Dict[str, Any]]:
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return None
        return data if isinstance(data, dict) else None
