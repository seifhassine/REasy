"""Exact, session-scoped sound previews for MSG entries."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from file_handlers.sound.bnk_parser import (
    BnkTrack,
    extract_embedded_wem,
    parse_soundbank,
)
from file_handlers.sound.sound_metadata import MessageSoundReference, SoundMetadata
from file_handlers.sound.sound_resources import read_sound_resource, resource_key
from file_handlers.sound.sound_waveform import (
    analyze_wave_activity,
    write_wave_segment,
)
from file_handlers.sound.runtime_sound_index import (
    request_runtime_sound_index,
    snapshot_pak_reader,
)
from file_handlers.sound.wwise_media import WwiseMediaKind
from utils.resource_file_utils import resource_context_for_handler


@dataclass(frozen=True, slots=True)
class SoundPreviewCandidate:
    """One distinct payload and every container that supplied that payload."""

    source_id: int
    paths: tuple[str, ...]
    payload_hash: str
    start_ms: int = 0
    end_ms: int = 0

    @property
    def path(self) -> str:
        return self.paths[0]

    @property
    def is_segment(self) -> bool:
        return self.end_ms > self.start_ms >= 0


@dataclass(frozen=True, slots=True)
class SoundScanResult:
    catalog: dict[str, tuple[SoundPreviewCandidate, ...]]
    references: dict[str, tuple[int, ...]]
    inspected_file_count: int
    unavailable_source_count: int


@dataclass(slots=True)
class _CachedResource:
    data: bytes
    tracks: dict[int, tuple[BnkTrack, ...]]


class _ResourceOwner:
    """Non-QObject snapshot safe for resource reads on the preview worker."""

    def __init__(self, handler, profile):
        self.filepath = str(getattr(handler, "filepath", "") or "")
        self.raw_data = b""
        context = resource_context_for_handler(handler)
        reader = getattr(context, "pak_cached_reader", None) if context else None
        self.runtime_handle = (
            request_runtime_sound_index(reader, profile) if reader else None
        )
        if reader is not None:
            context = context.with_pak_reader(snapshot_pak_reader(reader))
        self.resource_context = context


def configured_vgmstream(handler) -> str | None:
    app = getattr(handler, "app", None)
    settings = getattr(app, "settings", {}) if app is not None else {}
    configured = str(settings.get("vgmstream_cli_path", "")).strip()
    return (
        shutil.which(configured)
        or shutil.which("vgmstream-cli")
        or shutil.which("vgmstream-cli.exe")
    )


class MsgSoundPreviewSession(QObject):
    """Resolve, cache, decode, and play MSG audio without touching Qt off-thread."""

    scan_finished = Signal(object)
    scan_failed = Signal(str)
    preparing = Signal(object)
    playback_started = Signal(object)
    playback_stopped = Signal()
    playback_failed = Signal(str)
    waveform_ready = Signal(object)
    position_changed = Signal(float)

    _worker_done = Signal(object)

    def __init__(self, handler, profile, parent: QObject | None = None):
        super().__init__(parent)
        self._profile = profile
        self._metadata: SoundMetadata | None = None
        self._owner = _ResourceOwner(handler, profile)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="msg-sound-preview"
        )
        self._lock = threading.Lock()
        self._closed = False
        self._scan_token = 0
        self._play_token = 0
        self._resources: dict[str, _CachedResource] = {}
        self._decoded: dict[
            tuple[int, str], tuple[SoundPreviewCandidate, str]
        ] = {}
        self._prepared: dict[
            tuple[int, str, int, int], tuple[SoundPreviewCandidate, str, object]
        ] = {}
        self._temp_dir = tempfile.mkdtemp(prefix="reasy_msg_sound_")

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.7)
        self._player.setAudioOutput(self._audio)
        self._active_message_id = ""

        queued = Qt.ConnectionType.QueuedConnection
        self._worker_done.connect(self._deliver_worker_result, queued)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_player_error)
        self._player.positionChanged.connect(self._emit_position)

    @property
    def active_message_id(self) -> str:
        return self._active_message_id

    def scan(self, messages) -> None:
        message_sounds: dict[str, list[int]] = {}
        for message_id, sound_id in messages:
            key = str(message_id or "").strip().strip("{}").casefold()
            if not key:
                continue
            values = message_sounds.setdefault(key, [])
            try:
                sound_id = int(sound_id) & 0xFFFFFFFF
            except (TypeError, ValueError):
                sound_id = 0
            if sound_id and sound_id not in values:
                values.append(sound_id)
        records = tuple(
            (message_id, tuple(sound_ids))
            for message_id, sound_ids in message_sounds.items()
        )
        with self._lock:
            if self._closed:
                return
            self._scan_token += 1
            token = self._scan_token
        self._submit("scan", token, self._scan_worker, token, records)

    def cancel_scan(self) -> None:
        with self._lock:
            if not self._closed:
                self._scan_token += 1

    def play(
        self,
        message_id: str,
        candidate: SoundPreviewCandidate,
        executable: str,
        waveform_width: int,
    ) -> None:
        self.stop()
        with self._lock:
            if self._closed:
                return
            self._play_token += 1
            token = self._play_token
        self._active_message_id = str(message_id)
        self.preparing.emit(candidate)
        self._submit(
            "preview",
            token,
            self._prepare_worker,
            token,
            candidate,
            executable,
            max(300, min(int(waveform_width), 1200)),
        )

    def stop(self) -> None:
        was_active = bool(self._active_message_id)
        with self._lock:
            if self._closed:
                return
            self._play_token += 1
        self._active_message_id = ""
        self._player.stop()
        self._player.setSource(QUrl())
        if was_active:
            self.playback_stopped.emit()

    def seek(self, per_mille: int) -> None:
        duration = self._player.duration()
        if duration > 0:
            self._player.setPosition(
                round(duration * max(0, min(per_mille, 1000)) / 1000)
            )

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._scan_token += 1
            self._play_token += 1
        self._active_message_id = ""
        self._player.stop()
        self._player.setSource(QUrl())
        try:
            self._executor.submit(self._cleanup_worker)
        except RuntimeError:
            self._cleanup_worker()
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _submit(self, kind: str, token: int, function, *args) -> None:
        future = self._executor.submit(function, *args)
        future.add_done_callback(
            lambda completed: self._finish_work(kind, token, completed)
        )

    # Worker-only cache -------------------------------------------------

    def _scan_worker(
        self,
        token: int,
        messages: tuple[tuple[str, tuple[int, ...]], ...],
    ) -> SoundScanResult:
        if not self._scan_is_current(token):
            raise ValueError("Sound scan was cancelled.")
        metadata = self._metadata
        if metadata is None:
            metadata = self._profile.metadata(self._owner.filepath)
            metadata.attach_runtime_handle(self._owner.runtime_handle)
            self._metadata = metadata
        metadata.prepare_operational_index(wait=True)
        if not self._scan_is_current(token):
            raise ValueError("Sound scan was cancelled.")

        trigger_ids = tuple(
            sound_id
            for _message_id, sound_ids in messages
            for sound_id in sound_ids
        )
        reference_lookup = getattr(
            metadata, "sound_references_for_triggers", None
        )
        if reference_lookup:
            trigger_references = reference_lookup(trigger_ids)
        else:
            trigger_references = {
                trigger_id: tuple(
                    MessageSoundReference(source_id)
                    for source_id in source_ids
                )
                for trigger_id, source_ids in metadata.sources_for_triggers(
                    trigger_ids
                ).items()
            }
        resolved: dict[str, tuple[MessageSoundReference, ...]] = {}
        references = {}
        for message_id, sound_ids in messages:
            # UUID/media and timeline links identify the message itself and
            # therefore outrank a Wwise event graph, which may intentionally
            # contain switch/random variants. SoundID is an exact fallback,
            # never a Source-ID guess and never an additive broadening step.
            message_references = metadata.message_sound_references(message_id)
            if not message_references:
                message_references = tuple(dict.fromkeys(
                    reference
                    for sound_id in sound_ids
                    for reference in trigger_references.get(sound_id, ())
                ))
            if message_references:
                resolved[message_id] = message_references
                references[message_id] = tuple(dict.fromkeys(
                    reference.source_id for reference in message_references
                ))
        unique_references = tuple(dict.fromkeys(
            reference
            for values in resolved.values()
            for reference in values
        ))
        reference_paths = metadata.preview_media_paths_for_references(
            unique_references
        )

        # Invert the lookup so each potentially large container is loaded once
        # and released before the next one. Only a container selected for
        # playback is retained in the session cache.
        paths: dict[str, tuple[str, dict[int, list[MessageSoundReference]]]] = {}
        for reference in unique_references:
            if not self._scan_is_current(token):
                raise ValueError("Sound scan was cancelled.")
            for path in reference_paths.get(reference, ()):
                key = resource_key(path)
                if key not in paths:
                    paths[key] = (path, {})
                paths[key][1].setdefault(reference.source_id, []).append(reference)

        payload_paths: dict[
            MessageSoundReference, dict[str, list[str]]
        ] = {
            reference: {} for reference in unique_references
        }
        for key, (path, sources) in paths.items():
            if not self._scan_is_current(token):
                raise ValueError("Sound scan was cancelled.")
            cached = self._resources.get(key) or self._read_resource(path)
            if cached is None:
                continue
            for source_id, source_references in sources.items():
                digests = tuple(dict.fromkeys(
                    self._payload_hash(wem)
                    for wem in self._wems(cached, source_id)
                ))
                for reference in source_references:
                    for digest in digests:
                        locations = payload_paths[reference].setdefault(digest, [])
                        if key not in locations:
                            locations.append(key)

        candidates_by_reference = {
            reference: tuple(
                SoundPreviewCandidate(
                    reference.source_id,
                    tuple(candidate_paths),
                    digest,
                    reference.start_ms,
                    reference.end_ms,
                )
                for digest, candidate_paths in payload_paths[reference].items()
            )
            for reference in unique_references
        }

        catalog = {}
        for message_id, message_references in resolved.items():
            distinct = {}
            for reference in message_references:
                for candidate in candidates_by_reference.get(reference, ()):
                    identity = (
                        candidate.payload_hash,
                        candidate.start_ms,
                        candidate.end_ms,
                    )
                    previous = distinct.setdefault(identity, candidate)
                    if previous.source_id == candidate.source_id:
                        distinct[identity] = replace(
                            previous,
                            paths=tuple(dict.fromkeys((
                                *previous.paths, *candidate.paths,
                            ))),
                        )
            catalog[message_id] = tuple(distinct.values())
        available_sources = {
            reference.source_id
            for reference, candidates in candidates_by_reference.items()
            if candidates
        }
        unavailable = sum(
            source_id not in available_sources
            for source_id in {
                reference.source_id for reference in unique_references
            }
        )
        return SoundScanResult(
            catalog,
            references,
            len(paths),
            unavailable,
        )

    def _read_resource(self, path: str) -> _CachedResource | None:
        try:
            resolved = read_sound_resource(self._owner, path, cache=False)
        except (OSError, ValueError):
            return None
        if resolved is None:
            return None
        _, data = resolved
        try:
            result = parse_soundbank(data)
        except (OSError, ValueError, struct.error):
            return None
        tracks: dict[int, list[BnkTrack]] = {}
        for track in result.tracks:
            if (
                track.available
                and track.payload_complete
                and track.media_kind == WwiseMediaKind.AUDIO
            ):
                tracks.setdefault(track.source_id, []).append(track)
        return _CachedResource(
            data,
            {source_id: tuple(values) for source_id, values in tracks.items()},
        )

    @staticmethod
    def _wems(cached: _CachedResource, source_id: int) -> tuple[bytes, ...]:
        payloads = []
        for track in cached.tracks.get(int(source_id), ()):
            try:
                payload = extract_embedded_wem(cached.data, track)
            except (OSError, ValueError, struct.error):
                continue
            if payload:
                payloads.append(payload)
        return tuple(payloads)

    @staticmethod
    def _payload_hash(payload: bytes) -> str:
        return hashlib.blake2b(payload, digest_size=16).hexdigest()

    def _prepare_worker(
        self,
        token: int,
        candidate: SoundPreviewCandidate,
        executable: str,
        waveform_width: int,
    ) -> tuple[SoundPreviewCandidate, str, object]:
        if not self._play_is_current(token):
            raise ValueError("Sound preview was cancelled.")
        prepared_key = (
            candidate.source_id,
            candidate.payload_hash,
            candidate.start_ms,
            candidate.end_ms,
        )
        prepared = self._prepared.get(prepared_key)
        if prepared is not None:
            return prepared

        decoded_key = candidate.source_id, candidate.payload_hash
        cached = self._decoded.get(decoded_key)
        if cached is None:
            wem = b""
            selected = candidate
            for path in candidate.paths:
                key = resource_key(path)
                resource = self._resources.get(key) or self._read_resource(path)
                payloads = self._wems(resource, candidate.source_id) if resource else ()
                payload = next((
                    value for value in payloads
                    if self._payload_hash(value) == candidate.payload_hash
                ), b"")
                if payload:
                    self._resources[key] = resource
                    wem = payload
                    if path != candidate.path:
                        selected = SoundPreviewCandidate(
                            candidate.source_id,
                            (path, *(value for value in candidate.paths if value != path)),
                            candidate.payload_hash,
                            candidate.start_ms,
                            candidate.end_ms,
                        )
                    break
            if not wem:
                raise ValueError(
                    f"Source {candidate.source_id} is no longer available in its referenced media."
                )
            wav_path = self._decode_wem(token, selected, wem, executable)
            self._decoded[decoded_key] = selected, wav_path
        else:
            decoded, wav_path = cached
            selected = SoundPreviewCandidate(
                decoded.source_id,
                decoded.paths,
                decoded.payload_hash,
                candidate.start_ms,
                candidate.end_ms,
            )
        if not self._play_is_current(token):
            raise ValueError("Sound preview was cancelled.")

        playback_path = wav_path
        if selected.is_segment:
            stem = (
                f"{selected.source_id}_{selected.payload_hash}_"
                f"{selected.start_ms}_{selected.end_ms}"
            )
            playback_path = str(Path(self._temp_dir, f"{stem}.wav"))
            if not Path(playback_path).is_file():
                write_wave_segment(
                    wav_path,
                    playback_path,
                    selected.start_ms,
                    selected.end_ms,
                )
        waveform = analyze_wave_activity(playback_path, waveform_width)
        if not self._play_is_current(token):
            raise ValueError("Sound preview was cancelled.")
        result = selected, playback_path, waveform
        self._prepared[prepared_key] = result
        return result

    def _decode_wem(
        self,
        token: int,
        candidate: SoundPreviewCandidate,
        wem: bytes,
        executable: str,
    ) -> str:
        stem = f"{candidate.source_id}_{candidate.payload_hash}"
        wem_path = Path(self._temp_dir, f"{stem}.wem")
        wav_path = Path(self._temp_dir, f"{stem}.wav")
        wav_path.unlink(missing_ok=True)
        wem_path.write_bytes(wem)
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                [executable, "-i", "-o", str(wav_path), str(wem_path)],
                **kwargs,
            )
        except OSError as exc:
            raise ValueError(f"Could not start VGMStream: {exc}") from exc

        try:
            output = error = ""
            while True:
                try:
                    output, error = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if not self._play_is_current(token):
                        process.terminate()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise ValueError("Sound preview was cancelled.")
        finally:
            try:
                wem_path.unlink(missing_ok=True)
            except OSError:
                pass
        if process.returncode or not wav_path.is_file() or not wav_path.stat().st_size:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
            detail = (
                error or output or "VGMStream could not decode this WEM."
            ).strip()
            raise ValueError(detail)
        return str(wav_path)

    def _cleanup_worker(self) -> None:
        self._resources.clear()
        self._decoded.clear()
        self._prepared.clear()
        for attempt in range(20):
            try:
                shutil.rmtree(self._temp_dir)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt < 19:
                    time.sleep(0.1)

    # Worker-to-GUI delivery -------------------------------------------

    def _finish_work(self, kind: str, token: int, future: Future) -> None:
        try:
            result = future.result()
        except Exception as exc:
            result, error = None, str(exc)
        else:
            error = ""
        self._emit_if_open(self._worker_done, (kind, token, result, error))

    def _emit_if_open(self, signal, value) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                signal.emit(value)
            except RuntimeError:
                pass

    def _play_is_current(self, token: int) -> bool:
        with self._lock:
            return not self._closed and token == self._play_token

    def _scan_is_current(self, token: int) -> bool:
        with self._lock:
            return not self._closed and token == self._scan_token

    @Slot(object)
    def _deliver_worker_result(self, payload) -> None:
        kind, token, result, error = payload
        with self._lock:
            current = not self._closed and token == (
                self._scan_token if kind == "scan" else self._play_token
            )
        if not current:
            return
        if kind == "scan":
            if error:
                self.scan_failed.emit(error)
            else:
                self.scan_finished.emit(result)
            return
        if error:
            self._active_message_id = ""
            self.playback_failed.emit(error)
            return
        candidate, wav_path, waveform = result
        self._player.setSource(QUrl.fromLocalFile(wav_path))
        self._player.play()
        self.waveform_ready.emit(waveform)
        self.playback_started.emit(candidate)

    @Slot(QMediaPlayer.MediaStatus)
    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop()

    @Slot(QMediaPlayer.Error, str)
    def _on_player_error(self, _error, message: str) -> None:
        if not self._active_message_id or self._player.source().isEmpty():
            return
        self._active_message_id = ""
        self._player.stop()
        self._player.setSource(QUrl())
        self.playback_failed.emit(message or "Qt could not play the decoded audio.")

    @Slot(int)
    def _emit_position(self, position: int) -> None:
        duration = self._player.duration()
        if duration > 0 and self._active_message_id:
            self.position_changed.emit(position / duration)


__all__ = [
    "MsgSoundPreviewSession",
    "SoundPreviewCandidate",
    "SoundScanResult",
    "configured_vgmstream",
]
