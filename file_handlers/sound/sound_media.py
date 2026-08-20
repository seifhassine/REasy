"""Verified BNK/PCK media relationships and atomic replacement transactions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .bnk_parser import (
    BnkEditModel,
    BnkParseResult,
    BnkTrack,
    export_non_streaming_pck,
    extract_embedded_wem,
    music_clip_source_ids,
    music_duration_upserts,
    parse_soundbank,
    parse_wem_metadata,
    rewrite_soundbank,
)
from .sound_resources import (
    local_sound_directories,
    read_sound_resource,
    resource_key,
)


class SoundMediaResolutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SoundAsset:
    path: str
    data: bytes
    role: str


@dataclass(frozen=True, slots=True)
class PckPair:
    media: SoundAsset
    index: SoundAsset | None = None


@dataclass(frozen=True, slots=True)
class BankMedia:
    asset: SoundAsset
    stream_type: int
    prefetch_size: int | None = None
    parsed: BnkParseResult | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SoundReplacementPlan:
    source_id: int
    current_path: str
    original_wem: bytes
    banks: tuple[BankMedia, ...]
    packages: tuple[PckPair, ...]

    def build_outputs(
        self, wem_data: bytes, *, report_changes: bool = False
    ) -> dict[str, bytes] | tuple[dict[str, bytes], tuple[str, ...]]:
        return build_sound_replacement_outputs(
            (self,), {self.source_id: wem_data}, report_changes=report_changes
        )

    def output_roles(self) -> tuple[tuple[str, str], ...]:
        values = [(bank.asset.path, bank.asset.role) for bank in self.banks]
        for package in self.packages:
            if package.index is not None:
                values.append((package.index.path, package.index.role))
            values.append((package.media.path, package.media.role))
        return tuple(dict.fromkeys(values))


def _track(result: BnkParseResult, source_id: int) -> BnkTrack | None:
    return next((item for item in result.tracks if item.source_id == source_id), None)


def _parse_asset(handler, asset: SoundAsset) -> BnkParseResult:
    parser = getattr(handler, "parse_sound_data", None)
    return parser(asset.path, asset.data) if callable(parser) else parse_soundbank(asset.data)


def _parsed_banks(handler, banks) -> tuple[BankMedia, ...]:
    return tuple(
        bank if bank.parsed is not None else replace(
            bank, parsed=_parse_asset(handler, bank.asset)
        )
        for bank in banks
    )


def _read_asset(
    handler,
    path: str,
    role: str,
    *,
    cache: bool = True,
) -> SoundAsset | None:
    resolved = read_sound_resource(handler, path, cache=cache)
    return SoundAsset(resolved[0], resolved[1], role) if resolved else None


def validate_streaming_pck_match(index_data: bytes, streaming_data: bytes) -> None:
    """Require an exact AKPK index match and actual media in the candidate PCK."""

    if index_data[:4] != b"AKPK" or streaming_data[:4] != b"AKPK":
        raise SoundMediaResolutionError("Both files must be Wwise PCK containers.")
    index = export_non_streaming_pck(index_data)
    if index != index_data:
        raise SoundMediaResolutionError(
            "The opened PCK already contains media and is not an index-only PCK."
        )
    if export_non_streaming_pck(streaming_data) != index:
        raise SoundMediaResolutionError(
            "The selected streaming PCK does not have the same AKPK media index."
        )
    full = parse_soundbank(streaming_data)
    if len(streaming_data) <= len(index) or not any(
        track.available and track.payload_complete for track in full.tracks
    ):
        raise SoundMediaResolutionError(
            "The selected PCK matches the index but contains no complete audio media."
        )


def resolve_indexed_streaming_pck(profile, handler) -> SoundAsset | None:
    """Find a full PCK for the opened index using runtime byte verification."""

    index_data = bytes(handler.raw_data)
    if index_data[:4] != b"AKPK" or export_non_streaming_pck(index_data) != index_data:
        return None
    hint = profile.streaming_package_hint(
        getattr(handler, "filepath", "")
    )
    if not hint:
        return None

    hinted = _read_asset(handler, hint, "full streaming PCK", cache=False)
    mismatch = None
    if hinted is not None:
        try:
            validate_streaming_pck_match(index_data, hinted.data)
            return hinted
        except SoundMediaResolutionError as exc:
            mismatch = exc

    # User-created full PCKs may use a different basename. Local scans compare
    # only the small header first, so even very large packages remain cheap.
    for directory in local_sound_directories(handler, hint):
        if not directory.is_dir():
            continue
        for candidate in (
            path for path in directory.iterdir()
            if path.is_file() and any(
                marker in path.name.casefold() for marker in (".pck", ".spck")
            )
        ):
            try:
                if candidate.stat().st_size <= len(index_data):
                    continue
                with candidate.open("rb") as stream:
                    if stream.read(len(index_data)) != index_data:
                        continue
                data = candidate.read_bytes()
                validate_streaming_pck_match(index_data, data)
                return SoundAsset(str(candidate), data, "full streaming PCK")
            except OSError:
                continue
    if mismatch is not None:
        raise mismatch
    return None


def _resolve_package(handler, record: dict, source_id: int) -> PckPair | None:
    index_path = record.get("index", "")
    media_path = record.get("streaming", "")
    index = _read_asset(handler, index_path, "PCK media index") if index_path else None
    media = _read_asset(handler, media_path, "streaming PCK media") if media_path else None
    if index is not None and media is not None and index.path == media.path:
        index = None
    if media is None and index is not None:
        index_result = _parse_asset(handler, index)
        index_track = _track(index_result, source_id)
        if index_track and index_track.available and index_track.payload_complete:
            media, index = SoundAsset(index.path, index.data, "PCK media"), None
    if media is None:
        return None
    media_result = _parse_asset(handler, media)
    media_track = _track(media_result, source_id)
    if media_track is None or not media_track.available or not media_track.payload_complete:
        return None
    if index is not None:
        index_track = _track(_parse_asset(handler, index), source_id)
        if index_track is None:
            raise SoundMediaResolutionError(
                f"PCK index {index.path} does not contain Source ID {source_id}."
            )
        expected = export_non_streaming_pck(media.data)
        if index.data != expected:
            raise SoundMediaResolutionError(
                f"PCK index {index.path} does not match streaming package {media.path}."
            )
    return PckPair(media, index)


def _package_records(metadata, profile, path: str, source_id: int) -> tuple[dict, ...]:
    records = metadata.media_packages(source_id, path)
    paths = profile.related_paths(path)
    fallback = ({
        "index": paths.index_pck,
        "streaming": paths.streaming_pck,
    },) if paths else ()
    return records or fallback


def _resolve_embedded_banks(handler, metadata, current: str, source_id: int):
    """Resolve a split in-bank payload and verify the exact Source ID at runtime."""

    matches = []
    for path in metadata.embedded_media_banks(source_id, current):
        asset = _read_asset(handler, path, "embedded SBNK media")
        if asset is None:
            continue
        track = _track(_parse_asset(handler, asset), source_id)
        if track is not None and track.available and track.payload_complete:
            matches.append((asset, track))
    matches = list({asset.path: (asset, track) for asset, track in matches}.values())
    payloads = [extract_embedded_wem(asset.data, track) for asset, track in matches]
    if len(set(payloads)) > 1:
        raise SoundMediaResolutionError(
            f"Source ID {source_id} has different payloads in {len(matches)} "
            "sibling banks; REasy will not guess which copy is intended."
        )
    return tuple(matches)


def _resolve_prefetch_banks(handler, metadata, current: str, source_id: int):
    """Resolve split prefetch BNKs; the full-PCK prefix is verified later."""

    matches = []
    for path in metadata.prefetch_media_banks(source_id, current):
        asset = _read_asset(handler, path, "split SBNK prefetch")
        if asset is None:
            continue
        track = _track(_parse_asset(handler, asset), source_id)
        if track is not None and track.available:
            matches.append(BankMedia(asset, 1, track.length))
    return tuple({item.asset.path: item for item in matches}.values())


def _resolve_one_package(handler, records, source_id: int) -> PckPair:
    unique = {
        (record.get("index", ""), record.get("streaming", "")): record
        for record in records
    }
    if len(unique) != 1:
        raise SoundMediaResolutionError(
            f"Source ID {source_id} maps to {len(unique)} PCK packages; REasy will not guess."
        )
    package = _resolve_package(handler, next(iter(unique.values())), source_id)
    if package is None:
        raise SoundMediaResolutionError(
            f"No complete, matching PCK payload was found for Source ID {source_id}."
        )
    return package


def resolve_sound_replacement(
    profile,
    handler,
    result: BnkParseResult,
    track: BnkTrack,
) -> SoundReplacementPlan:
    """Resolve every asset that must change for one profiled Source ID."""

    materialize = getattr(handler, "materialize_sound_edits", None)
    if callable(materialize):
        materialize()
    source_id = int(track.source_id)
    current = resource_key(getattr(handler, "filepath", ""))
    metadata = profile.metadata(getattr(handler, "filepath", ""))
    banks: list[BankMedia] = []
    packages: list[PckPair] = []

    if result.container_type.casefold() == "bnk":
        split_events = []
        if track.stream_type is None and track.available:
            for path in metadata.prefetch_event_banks(source_id, current):
                asset = _read_asset(handler, path, "event SBNK")
                if asset is None:
                    continue
                event_track = _track(_parse_asset(handler, asset), source_id)
                if event_track is not None and event_track.stream_type == 1:
                    split_events.append((asset, event_track))
        if split_events:
            records = tuple(
                record
                for asset, _event_track in split_events
                for record in metadata.media_packages(source_id, asset.path)
            ) or _package_records(metadata, profile, current, source_id)
            packages.append(_resolve_one_package(handler, records, source_id))
            banks.append(BankMedia(
                SoundAsset(current, bytes(handler.raw_data), "split SBNK prefetch"),
                1, track.length,
            ))
            banks.extend(BankMedia(asset, 1) for asset, _track in split_events)
        elif track.stream_type == 0 or (track.stream_type is None and track.available):
            linked = (
                _resolve_embedded_banks(handler, metadata, current, source_id)
                if not track.available else None
            )
            if linked:
                banks.extend(BankMedia(asset, 0) for asset, _track in linked)
                if source_id in music_clip_source_ids(result):
                    banks.append(BankMedia(
                        SoundAsset(
                            current,
                            bytes(handler.raw_data),
                            "BNK music timeline",
                        ),
                        2,
                    ))
                original = extract_embedded_wem(linked[0][0].data, linked[0][1])
                return SoundReplacementPlan(
                    source_id, current, original, _parsed_banks(handler, banks), ()
                )
            asset = SoundAsset(current, bytes(handler.raw_data), "embedded BNK media")
            banks.append(BankMedia(asset, 0))
            original = extract_embedded_wem(asset.data, track) if track.available else b""
            return SoundReplacementPlan(
                source_id, current, original, _parsed_banks(handler, banks), ()
            )
        else:
            records = _package_records(metadata, profile, current, source_id)
            packages.append(_resolve_one_package(handler, records, source_id))
            if track.stream_type == 1:
                banks.append(BankMedia(SoundAsset(
                    current, bytes(handler.raw_data), "BNK prefetch declaration"
                ), 1))
                banks.extend(_resolve_prefetch_banks(
                    handler, metadata, current, source_id
                ))
            elif track.stream_type == 2 and source_id in music_clip_source_ids(result):
                banks.append(BankMedia(SoundAsset(
                    current, bytes(handler.raw_data), "BNK music timeline"
                ), 2))
    else:
        paths = profile.related_paths(current)
        record = {
            "index": paths.index_pck,
            "streaming": paths.streaming_pck,
        } if paths else {}
        package = _resolve_package(handler, record, source_id)
        if package is None:
            raise SoundMediaResolutionError(
                f"No complete PCK payload was found for Source ID {source_id}."
            )
        packages.append(package)
        bank_paths = metadata.banks_for_package(current, source_id)
        if not bank_paths and paths:
            bank_paths = (paths.bank,)
        for bank_path in bank_paths:
            bank = _read_asset(handler, bank_path, "BNK prefetch")
            if bank is None:
                continue
            bank_result = _parse_asset(handler, bank)
            stream_types = {
                source.stream_type
                for obj in bank_result.objects
                for source in obj.sources
                if source.source_id == source_id
            }
            if not stream_types:
                continue
            if 1 in stream_types:
                banks.append(BankMedia(bank, 1))
                banks.extend(_resolve_prefetch_banks(
                    handler, metadata, bank_path, source_id
                ))
            elif 2 in stream_types and source_id in music_clip_source_ids(bank_result):
                banks.append(BankMedia(
                    replace(bank, role="BNK music timeline"), 2
                ))

    media_track = _track(_parse_asset(handler, packages[0].media), source_id)
    original = extract_embedded_wem(packages[0].media.data, media_track)
    if not original:
        raise SoundMediaResolutionError(
            f"Complete WEM data for Source ID {source_id} could not be read."
        )
    for bank in banks:
        if bank.stream_type != 1:
            continue
        bank_track = _track(_parse_asset(handler, bank.asset), source_id)
        fragment = (
            extract_embedded_wem(bank.asset.data, bank_track)
            if bank_track is not None and bank_track.available else b""
        )
        if fragment and not original.startswith(fragment):
            raise SoundMediaResolutionError(
                f"BNK prefetch in {bank.asset.path} does not match Source ID {source_id} "
                f"in {packages[0].media.path}."
            )
    return SoundReplacementPlan(
        source_id,
        current,
        original,
        _parsed_banks(
            handler, {item.asset.path: item for item in banks}.values()
        ),
        tuple({item.media.path: item for item in packages}.values()),
    )


def _replacement_duration(old_wem: bytes, new_wem: bytes) -> float | None:
    """Return the replacement's duration in ms, or None when it is unchanged/unknown."""

    if not old_wem:
        return None
    old = parse_wem_metadata(old_wem).duration_seconds
    new = parse_wem_metadata(new_wem).duration_seconds
    if old is None or new is None:
        return None
    old_ms, new_ms = old * 1000.0, new * 1000.0
    if not math.isfinite(old_ms) or not math.isfinite(new_ms) or abs(new_ms - old_ms) < 1.0:
        return None
    return new_ms


def build_sound_replacement_outputs(
    plans, replacements: dict[int, bytes], *, report_changes: bool = False
) -> dict[str, bytes] | tuple[dict[str, bytes], tuple[str, ...]]:
    """Rebuild each shared BNK/PCK once, then derive its header-only PCK.

    Music Track clips and owning Segment durations are adjusted for replaced
    sources whose duration changed. When ``report_changes`` is true the return
    value is ``(outputs, change_lines)``.
    """

    plans = tuple(plans)
    active = tuple(plan for plan in plans if plan.source_id in replacements)
    outputs: dict[str, bytes] = {}
    changes: list[str] = []
    banks = {
        bank.asset.path: bank
        for plan in plans for bank in plan.banks
    }
    packages = {
        package.media.path: package
        for plan in plans for package in plan.packages
    }
    for path, bank in banks.items():
        media = {}
        for plan in active:
            target = next((
                item for item in plan.banks if item.asset.path == path
            ), None)
            if target is None or target.stream_type not in (0, 1):
                continue
            payload = replacements[plan.source_id]
            if target.prefetch_size is not None:
                payload = payload[:target.prefetch_size]
            media[plan.source_id] = payload
        hirc_upserts: dict[int | tuple[int, int], tuple[int, bytes]] = {}
        durations = {
            plan.source_id: new_ms
            for plan in active
            if any(item.asset.path == path for item in plan.banks)
            and (new_ms := _replacement_duration(
                plan.original_wem, replacements[plan.source_id]
            )) is not None
        }
        if durations:
            hirc_upserts, bank_changes = music_duration_upserts(
                bank.asset.data,
                durations,
                parsed=bank.parsed,
            )
            changes.extend(bank_changes)
        hirc_payload = None
        source_renames = None
        rewrite_result = bank.parsed
        if hirc_upserts:
            model = BnkEditModel(bank.parsed or parse_soundbank(bank.asset.data)).clone()
            model.upsert_objects(
                (
                    type_id,
                    key[0] if isinstance(key, tuple) else key,
                    payload,
                )
                for key, (type_id, payload) in hirc_upserts.items()
            )
            hirc_payload = model.hirc_payload()
            source_renames = model.source_renames
            rewrite_result = model.result
        rebuilt = rewrite_soundbank(
            bank.asset.data,
            media,
            hirc_payload=hirc_payload,
            hirc_source_renames=source_renames,
            parsed_result=rewrite_result,
        )
        if rebuilt != bank.asset.data:
            outputs[path] = rebuilt
    for path, package in packages.items():
        media = {
            plan.source_id: replacements[plan.source_id]
            for plan in active
            if any(item.media.path == path for item in plan.packages)
        }
        full = rewrite_soundbank(package.media.data, media)
        outputs[path] = full
        if package.index is not None:
            outputs[package.index.path] = export_non_streaming_pck(full)
    if report_changes:
        return outputs, tuple(changes)
    return outputs


__all__ = [
    "SoundMediaResolutionError",
    "SoundReplacementPlan",
    "build_sound_replacement_outputs",
    "resolve_indexed_streaming_pck",
    "resolve_sound_replacement",
    "validate_streaming_pck_match",
]
