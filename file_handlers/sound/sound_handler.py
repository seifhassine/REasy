from __future__ import annotations

import weakref
from pathlib import Path

from file_handlers.base_handler import FileHandler
from .bnk_parser import rewrite_soundbank
from .sound_resources import (
    matching_sound_companion_path as matching_sound_companion_path,
    resource_key,
)

SOUND_MAGICS = frozenset({b"BKHD", b"AKPK", b"SBNK", b"SPCK"})


class SoundHandler(FileHandler):
    def __init__(self):
        super().__init__()
        self.raw_data: bytes = b""
        self.filename: str = ""
        self.extension: str = ""
        self._replacements: dict[int, bytes] = {}
        self._event_actions: dict[int, tuple[int, ...]] = {}
        self._action_targets: dict[int, int] = {}
        self._deleted_event_ids: set[int] = set()
        self._hirc_upserts: dict[tuple[int, int], tuple[int, bytes]] = {}
        self._deleted_hirc_ids: set[int] = set()
        self._renamed_hirc_ids: dict[int, int] = {}
        self._reference_targets: dict[tuple[int, int], int] = {}
        self._bank_chunk_payloads: dict[bytes, bytes] = {}
        self._related_outputs: dict[str, bytes] = {}
        self._related_inputs: dict[str, bytes] = {}
        self._viewer_ref = None

    def _clear_edits(self):
        self._replacements.clear()
        self._event_actions.clear()
        self._action_targets.clear()
        self._deleted_event_ids.clear()
        self._hirc_upserts.clear()
        self._deleted_hirc_ids.clear()
        self._renamed_hirc_ids.clear()
        self._reference_targets.clear()
        self._bank_chunk_payloads.clear()

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[:4] in SOUND_MAGICS

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        self.raw_data = data
        source_path = self.filepath or self.filename
        self.filename = source_path
        self.extension = Path(source_path).suffix.lower()
        self._clear_edits()
        self._related_outputs.clear()
        self._related_inputs.clear()
        self.modified = False
        self._reset_viewer_modified()

    def rebuild(self) -> bytes:
        self.raw_data = rewrite_soundbank(
            self.raw_data,
            self._replacements,
            event_actions=self._event_actions,
            action_targets=self._action_targets,
            deleted_event_ids=self._deleted_event_ids,
            hirc_upserts=self._hirc_upserts,
            deleted_hirc_ids=self._deleted_hirc_ids,
            renamed_hirc_ids=self._renamed_hirc_ids,
            reference_targets=self._reference_targets,
            bank_chunk_payloads=self._bank_chunk_payloads,
        )
        self._clear_edits()
        self.modified = False
        self._reset_viewer_modified()
        return self.raw_data

    def _reset_viewer_modified(self):
        viewer = self._viewer_ref() if self._viewer_ref else None
        if viewer is not None:
            viewer.modified = False

    def replace_track_data(self, source_id: int, wem_data: bytes):
        self._replacements[int(source_id)] = bytes(wem_data)
        self.modified = True

    def apply_replacement_outputs(self, outputs: dict[str, bytes]):
        """Stage all files produced by one verified cross-container replacement."""

        current = resource_key(self.filepath or self.filename)
        normalized = {resource_key(path): bytes(data) for path, data in outputs.items()}
        if current in normalized:
            self.raw_data = normalized.pop(current)
        self._related_outputs.update(normalized)
        if not outputs:
            raise ValueError("Sound replacement produced no output files")
        self.modified = True

    def pending_related_outputs(self) -> dict[str, bytes]:
        return dict(self._related_outputs)

    def cached_related_input(self, path: str) -> bytes | None:
        return self._related_inputs.get(resource_key(path))

    def cache_related_input(self, path: str, data: bytes):
        self._related_inputs[resource_key(path)] = bytes(data)

    def related_output_targets(self) -> dict[str, bytes]:
        if not self._related_outputs:
            return {}
        context = self.resource_context
        project_dir = str(getattr(context, "project_dir", "") or "")
        if project_dir:
            root = Path(project_dir).resolve()
        else:
            current = str(self.filepath or self.filename).replace("\\", "/")
            marker = current.casefold().find("natives/")
            if marker < 0 or not Path(current).is_absolute():
                raise ValueError(
                    "Related BNK/PCK outputs require an active REasy project or a file inside a natives directory."
                )
            root = Path(current[:marker]).resolve()
        targets = {}
        for path, data in self._related_outputs.items():
            target = (root / Path(path.replace("/", "\\"))).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Related sound output escapes the project: {path}")
            targets[str(target)] = data
        return targets

    def mark_related_outputs_saved(self):
        self._related_outputs.clear()
        self._related_inputs.clear()

    def discard_related_outputs(self):
        self._related_outputs.clear()

    def set_event_actions(self, event_id: int, action_ids):
        event_id = int(event_id) & 0xFFFFFFFF
        self._event_actions[event_id] = tuple(
            int(action_id) & 0xFFFFFFFF for action_id in action_ids
        )
        self._deleted_event_ids.discard(event_id)
        self.modified = True

    def delete_event(self, event_id: int):
        event_id = int(event_id) & 0xFFFFFFFF
        self._event_actions.pop(event_id, None)
        self._deleted_event_ids.add(event_id)
        self.modified = True

    def set_action_target(self, action_id: int, target_id: int):
        self._action_targets[int(action_id) & 0xFFFFFFFF] = int(target_id) & 0xFFFFFFFF
        self.modified = True

    def upsert_hirc_object(self, type_id: int, object_id: int, payload: bytes):
        object_id = int(object_id) & 0xFFFFFFFF
        type_id = int(type_id) & 0xFF
        self._hirc_upserts[(object_id, type_id)] = (type_id, bytes(payload))
        self._deleted_hirc_ids.discard(object_id)
        self.modified = True

    def upsert_hirc_objects(self, objects):
        for type_id, object_id, payload in objects:
            self.upsert_hirc_object(type_id, object_id, payload)

    def delete_hirc_objects(self, object_ids):
        for value in object_ids:
            object_id = int(value) & 0xFFFFFFFF
            for key in tuple(self._hirc_upserts):
                if key[0] == object_id:
                    self._hirc_upserts.pop(key)
            self._event_actions.pop(object_id, None)
            self._action_targets.pop(object_id, None)
            self._deleted_hirc_ids.add(object_id)
        self.modified = True

    def rename_hirc_object(self, old_id: int, new_id: int):
        self._renamed_hirc_ids[int(old_id) & 0xFFFFFFFF] = int(new_id) & 0xFFFFFFFF
        self.modified = True

    def set_hirc_reference(self, object_id: int, offset: int, target_id: int):
        key = (int(object_id) & 0xFFFFFFFF, int(offset))
        self._reference_targets[key] = int(target_id) & 0xFFFFFFFF
        self.modified = True

    def set_bank_chunk_payload(self, chunk_id: bytes | str, payload: bytes):
        key = chunk_id.encode("ascii") if isinstance(chunk_id, str) else bytes(chunk_id)
        if len(key) != 4:
            raise ValueError("A Wwise bank chunk ID must contain four ASCII bytes")
        self._bank_chunk_payloads[key] = bytes(payload)
        self.modified = True

    def create_viewer(self):
        try:
            from .sound_viewer import SoundViewer
        except Exception:
            return None
        viewer = SoundViewer(self)
        self._viewer_ref = weakref.ref(viewer)
        viewer.modified_changed.connect(self.modified_changed.emit)
        viewer.modified_changed.connect(
            lambda value: setattr(self, "modified", bool(value))
        )
        return viewer
