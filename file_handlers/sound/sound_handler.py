from __future__ import annotations

import weakref
from pathlib import Path

from file_handlers.base_handler import FileHandler
from .bnk_parser import (
    BnkEditModel,
    BnkParseResult,
    parse_soundbank,
    rewrite_soundbank,
)
from .sound_resources import resource_key

SOUND_MAGICS = frozenset({b"BKHD", b"AKPK", b"SBNK", b"SPCK"})


class SoundHandler(FileHandler):
    def __init__(self):
        super().__init__()
        self.raw_data: bytes = b""
        self.filename: str = ""
        self.extension: str = ""
        self._replacements: dict[int, bytes] = {}
        self._pending_graph_edits: list[tuple[str, tuple]] = []
        self._parse_result: BnkParseResult | None = None
        self._edit_model: BnkEditModel | None = None
        self._related_outputs: dict[str, bytes] = {}
        self._related_inputs: dict[str, bytes] = {}
        self._related_parses: dict[str, tuple[bytes, BnkParseResult]] = {}
        self._viewer_ref = None

    def _clear_edits(self):
        self._replacements.clear()
        self._pending_graph_edits.clear()

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
        self._parse_result = None
        self._edit_model = None
        self._related_outputs.clear()
        self._related_inputs.clear()
        self._related_parses.clear()
        self.modified = False
        self._reset_viewer_modified()

    def rebuild(self) -> bytes:
        self._materialize_edits()
        self.modified = False
        self._reset_viewer_modified()
        return self.raw_data

    def parse_result(self) -> BnkParseResult:
        """Return the current logical sound model, parsing raw bytes only once."""

        if self._edit_model is not None:
            return self._edit_model.result
        if self._parse_result is None:
            self._parse_result = parse_soundbank(self.raw_data)
            try:
                self._edit_model = BnkEditModel(self._parse_result)
            except ValueError:
                pass
        return self._parse_result

    def _apply_pending_graph_edits(self) -> BnkParseResult:
        if not self._pending_graph_edits:
            return self.parse_result()
        self.parse_result()
        if self._edit_model is None:
            raise ValueError("Wwise graph/settings edits require a structured BNK")
        working = self._edit_model.clone()
        for method, arguments in self._pending_graph_edits:
            getattr(working, method)(*arguments)
        self._edit_model = working
        self._parse_result = working.result
        self._pending_graph_edits.clear()
        return working.result

    def preview_result(self) -> BnkParseResult:
        """Apply queued edits to the logical model without rebuilding the container."""

        result = self._apply_pending_graph_edits()
        if self._replacements:
            self._materialize_edits()
            result = self.parse_result()
        return result

    def _materialize_edits(self):
        model = self._edit_model
        if (
            not self._pending_graph_edits
            and not self._replacements
            and not (model and (model.hirc_dirty or model.chunk_payloads))
        ):
            return self.raw_data
        self._apply_pending_graph_edits()
        model = self._edit_model
        hirc_payload = model.hirc_payload() if model and model.hirc_dirty else None
        chunk_payloads = model.chunk_payloads if model else {}
        if not self._replacements and hirc_payload is None and not chunk_payloads:
            return self.raw_data
        had_media_edits = bool(self._replacements)
        self.raw_data = rewrite_soundbank(
            self.raw_data,
            self._replacements,
            bank_chunk_payloads=chunk_payloads,
            hirc_payload=hirc_payload,
            hirc_source_renames=model.source_renames if hirc_payload is not None else None,
            parsed_result=model.result if model is not None else self._parse_result,
        )
        self._replacements.clear()
        if had_media_edits:
            self._parse_result = None
            self._edit_model = None
        elif model is not None:
            model.mark_serialized()
        return self.raw_data

    def materialize_sound_edits(self) -> bytes:
        """Materialize pending graph changes before a cross-container media edit."""

        return self._materialize_edits()

    def parse_sound_data(self, path: str, data: bytes) -> BnkParseResult:
        """Parse current/related sound bytes once per immutable blob."""

        key = resource_key(path)
        current = resource_key(self.filepath or self.filename)
        if key == current and data is self.raw_data:
            return self.parse_result()
        cached = self._related_parses.get(key)
        if cached is not None and cached[0] is data:
            return cached[1]
        result = parse_soundbank(data)
        self._related_parses[key] = (data, result)
        return result

    def _queue_graph_edit(self, method: str, *arguments):
        self._pending_graph_edits.append((method, arguments))
        self.modified = True

    def _reset_viewer_modified(self):
        viewer = self._viewer_ref() if self._viewer_ref else None
        if viewer is not None:
            viewer.modified = False

    def replace_track_data(self, source_id: int, wem_data: bytes):
        self._replacements[int(source_id)] = bytes(wem_data)
        self.modified = True

    def apply_replacement_outputs(self, outputs: dict[str, bytes]):
        """Stage all files produced by one verified cross-container replacement."""

        if self._pending_graph_edits or (
            self._edit_model
            and (self._edit_model.hirc_dirty or self._edit_model.chunk_payloads)
        ):
            raise ValueError(
                "Materialize pending BNK graph edits before applying media outputs"
            )
        current = resource_key(self.filepath or self.filename)
        normalized = {resource_key(path): bytes(data) for path, data in outputs.items()}
        if current in normalized:
            self.raw_data = normalized.pop(current)
            self._parse_result = None
            self._edit_model = None
            self._related_parses.pop(current, None)
        for path in normalized:
            self._related_parses.pop(path, None)
        self._related_outputs.update(normalized)
        if not outputs:
            raise ValueError("Sound replacement produced no output files")
        self.modified = True

    def pending_related_outputs(self) -> dict[str, bytes]:
        return dict(self._related_outputs)

    def cached_related_input(self, path: str) -> bytes | None:
        return self._related_inputs.get(resource_key(path))

    def cache_related_input(self, path: str, data: bytes):
        key = resource_key(path)
        value = bytes(data)
        if self._related_inputs.get(key) is not value:
            self._related_parses.pop(key, None)
        self._related_inputs[key] = value

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
        self._related_parses.clear()

    def discard_related_outputs(self):
        for path in self._related_outputs:
            self._related_parses.pop(path, None)
        self._related_outputs.clear()

    def set_event_actions(self, event_id: int, action_ids):
        event_id = int(event_id) & 0xFFFFFFFF
        self._queue_graph_edit(
            "set_event_actions",
            event_id,
            tuple(int(action_id) & 0xFFFFFFFF for action_id in action_ids),
        )

    def delete_event(self, event_id: int):
        self.delete_hirc_objects((event_id,))

    def upsert_hirc_object(self, type_id: int, object_id: int, payload: bytes):
        self.upsert_hirc_objects(((type_id, object_id, payload),))

    def upsert_hirc_objects(self, objects):
        entries = tuple(
            (int(type_id) & 0xFF, int(object_id) & 0xFFFFFFFF, bytes(payload))
            for type_id, object_id, payload in objects
        )
        if entries:
            self._queue_graph_edit("upsert_objects", entries)

    def delete_hirc_objects(self, object_ids):
        values = tuple(int(value) & 0xFFFFFFFF for value in object_ids)
        if values:
            self._queue_graph_edit("delete_objects", values)

    def rename_hirc_object(self, old_id: int, new_id: int):
        self._queue_graph_edit(
            "rename_object",
            int(old_id) & 0xFFFFFFFF,
            int(new_id) & 0xFFFFFFFF,
        )

    def set_hirc_reference(self, object_id: int, offset: int, target_id: int):
        self._queue_graph_edit(
            "set_reference",
            int(object_id) & 0xFFFFFFFF,
            int(offset),
            int(target_id) & 0xFFFFFFFF,
        )

    def set_bank_chunk_payload(self, chunk_id: bytes | str, payload: bytes):
        key = chunk_id.encode("ascii") if isinstance(chunk_id, str) else bytes(chunk_id)
        if len(key) != 4:
            raise ValueError("A Wwise bank chunk ID must contain four ASCII bytes")
        self._queue_graph_edit("set_chunk_payload", key, bytes(payload))

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
