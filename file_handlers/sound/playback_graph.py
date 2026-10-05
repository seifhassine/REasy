"""Lossless playback IR and schema-driven Wwise v140 offline construction.

The IR archives every scalar, flag (including opaque bits), and original payload.
The writer emits the destination schema, never relabels a legacy payload. Public
bus/state/switch/RTPC IDs require explicit bindings. Nonzero opaque flags require
an explicit field resolution, retained in the build report.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import copy
import hashlib
import struct

from .bnk_parser import (parse_soundbank, rewrite_soundbank, parse_wem_metadata,
                        _split_chunk_records, _pack_chunk_records, HircObject,
                        _read_hirc_objects,
                        _decode_hirc_objects, clone_hirc_payload, set_bank_sources,
                        set_hirc_children, set_random_sequence, set_switch_container,
                        set_music_segment, set_music_track_clips)
from .wwise_schema import property_names
from .wwise_v132 import (_Reader, _PARSERS, _CHUNK_PARSERS, _pack_var,
                        parse_structured_object, parse_structured_chunk, set_wwise_fields)

SUPPORTED_TYPES = frozenset({1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14})


class UnboundPlaybackGraph(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__(f"Playback graph has {len(report['unbound'])} unresolved bindings/fields")


def export_playback_graph(data: bytes, *, object_ids=None, include_dependencies=False) -> dict:
    """Export complete structures; selection must include its required objects.

    Partial/unknown layouts are retained, with errors, for inspection. They cannot
    be built until a supported destination structure is supplied explicitly.
    """
    chunks, _ = _split_chunk_records(data)
    header = next(chunk.payload for chunk in chunks if chunk.chunk_id == b'BKHD')
    bank_version, bank_id = struct.unpack_from('<II', header)
    objects = _read_hirc_objects(next(chunk.payload for chunk in chunks if chunk.chunk_id == b'HIRC'), bank_version=bank_version)
    if not objects.complete:
        raise ValueError('Truncated source HIRC')
    selected = None if object_ids is None else set(map(int, object_ids))
    by_id = {obj.object_id: obj for obj in objects.objects}
    layouts = {}
    if selected is not None and include_dependencies:
        pending = list(selected)
        while pending:
            key = pending.pop()
            if key not in by_id:
                raise ValueError(f'Requested object ID is absent: {key}')
            obj = by_id[key]
            layout = parse_structured_object(obj.type_id, obj.payload, bank_version)
            layouts[key] = layout
            for field in layout.fields:
                if field.id_kind in ('hirc', 'event') and field.reference_role != 'parent' and field.value in by_id and field.value not in selected:
                    selected.add(field.value)
                    pending.append(field.value)
    nodes = []
    chunk_pos = 0
    hirc_offset = None
    while chunk_pos + 8 <= len(data):
        chunk_size = struct.unpack_from('<I', data, chunk_pos + 4)[0]
        if data[chunk_pos:chunk_pos + 4] == b'HIRC':
            hirc_offset = chunk_pos + 8
            break
        chunk_pos += 8 + chunk_size
    for obj in objects.objects:
        if selected is not None and obj.object_id not in selected:
            continue
        layout = layouts.get(obj.object_id) or parse_structured_object(obj.type_id, obj.payload, bank_version)
        nodes.append({
            'id': obj.object_id, 'type': obj.type_id, 'name': obj.type_name,
            'complete': layout.complete, 'error': layout.error,
            'payload_hex': obj.payload.hex(),
            'payload_file_offset': hirc_offset + obj.payload_offset,
            'fields': [asdict(field) for field in layout.fields],
        })
    if selected is not None and selected != {node['id'] for node in nodes}:
        raise ValueError('Requested object IDs are absent from the source bank')
    return {'schema': 'reasy.playback/1', 'bank_version': bank_version,
            'bank_id': bank_id, 'sha256': hashlib.sha256(data).hexdigest(),
            'nodes': nodes}


def select_playback_graph(graph: dict, root_ids) -> dict:
    """Keep requested events and their playback/inheritance dependencies.

    Ancestor Actor-Mixers retain inherited parameters; their unplayed siblings
    are removed from the child list with an explicit audit and source field copy.
    Random/Switch/Layer/music containers always retain their complete branches.
    """
    by_id = {node['id']: node for node in graph['nodes']}
    roots = set(map(int, root_ids))
    if roots - set(by_id):
        raise ValueError(f'Missing playback roots: {sorted(roots - set(by_id))}')
    modes = {}
    pending = [(key, True) for key in roots]
    while pending:
        key, expand = pending.pop()
        if key in modes and (modes[key] or not expand):
            continue
        modes[key] = expand
        node = by_id[key]
        for field in node['fields']:
            target, role = field['value'], field['reference_role']
            if field['id_kind'] not in ('hirc', 'event') or target not in by_id:
                continue
            if not expand and node['type'] == 7 and role == 'child':
                continue
            pending.append((target, not (role == 'parent' and by_id[target]['type'] == 7)))
    selected = copy.deepcopy(graph)
    selected['nodes'] = [copy.deepcopy(by_id[key]) for key in by_id if key in modes]
    selected['selection'] = {'roots': sorted(roots), 'ancestor_child_pruning': []}
    for node in selected['nodes']:
        if node['type'] != 7 or modes[node['id']]:
            continue
        children = [field for field in node['fields'] if field['reference_role'] == 'child']
        retained = [field for field in children if field['value'] in modes]
        removed = [field['value'] for field in children if field['value'] not in modes]
        if not removed:
            continue
        node['source_fields'] = copy.deepcopy(node['fields'])
        node['fields'] = [field for field in node['fields'] if field['reference_role'] != 'child']
        next(field for field in node['fields'] if field['path'] == 'Children/Child count')['value'] = len(retained)
        for index, field in enumerate(retained):
            field['path'] = f'Children/Child {index + 1}'
            field['label'] = f'Child {index + 1}'
            node['fields'].append(field)
        selected['selection']['ancestor_child_pruning'].append({'object': node['id'], 'removed': removed})
    return selected


class _Emitter(_Reader):
    """Use the destination parser's traversal to serialize a field dictionary."""
    def __init__(self, values):
        super().__init__(b'', 140)
        self.values = values
        self.used = set()

    def value(self, label):
        path = self._path(label)
        if path not in self.values:
            raise ValueError(f'Missing native v140 field: {path}')
        self.used.add(path)
        return self.values[path]

    def scalar(self, label, storage, **kwargs):
        if label == 'Branch or audio object' and self._path(label) not in self.values:
            prefix = self._path(label).rsplit('/', 1)[0]
            if prefix + '/Audio object' in self.values:
                self.values[self._path(label)] = self.value('Audio object')
            else:
                self.values[self._path(label)] = int(self.value('First child')) | (int(self.value('Child count')) << 16)
        self.data += struct.pack('<' + self._FORMATS[storage], self.value(label))
        return super().scalar(label, storage, **kwargs)

    def var(self, label, **kwargs):
        self.data += _pack_var(int(self.value(label)))
        return super().var(label, **kwargs)

    def flags(self, label, specs, *, storage='u8'):
        raw = 0
        used = 0
        for spec in specs:
            name, shift = spec[:2]
            width = spec[2] if len(spec) > 2 else 1
            value = int(self.value(name))
            if not 0 <= value < 1 << width:
                raise ValueError(f'Flag out of range: {self._path(name)}')
            mask = ((1 << width) - 1) << shift
            used |= mask
            raw |= value << shift
        reserved_path = self._path(f'{label} reserved bits')
        if reserved_path in self.values:
            self.used.add(reserved_path)
            reserved = ((1 << (struct.calcsize(self._FORMATS[storage]) * 8)) - 1) & ~used
            if reserved:
                shift = (reserved & -reserved).bit_length() - 1
                value = int(self.values[reserved_path]) << shift
                if value & ~reserved:
                    raise ValueError(f'Reserved flag out of range: {reserved_path}')
                raw |= value
        self.data += struct.pack('<' + self._FORMATS[storage], raw)
        return super().flags(label, specs, storage=storage)

    def blob(self, label, size, **kwargs):
        value = self.value(label)
        value = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
        if len(value) != size:
            raise ValueError(f'Blob size differs: {self._path(label)}')
        self.data += value
        return super().blob(label, size, **kwargs)

    def zstring(self, label):
        value = str(self.value(label)).encode('utf-8') + b'\0'
        self.data += value
        # Bypass emitter.blob: this string was already emitted.
        raw = _Reader.blob(self, label, len(value), visible=True)
        return raw.rstrip(b'\0').decode('utf-8')


def build_v140_object(type_id: int, object_id: int, fields: dict) -> bytes:
    """Construct Sound/Action/Event/containers/music from exact native fields.

    Copy fields from a native object or author all fields. Count fields drive
    variable arrays, allowing new children, weighted playlists, switch branches,
    layers, clips, cues, transition rules and music decision trees.
    """
    if type_id not in SUPPORTED_TYPES:
        raise ValueError(f'Unsupported playback object type {type_id}')
    if not 0 < object_id <= 0xffffffff:
        raise ValueError('Object ID must be a nonzero uint32')
    values = dict(fields, **{'Object ID': object_id})
    emitter = _Emitter(values)
    emitter.scalar('Object ID', 'u32', editable=False, visible=False)
    _PARSERS[type_id](emitter)
    unused = set(values) - emitter.used
    # Zero reserved bits have no physical field when all bits are accounted for.
    unused = {path for path in unused if not (path.endswith('reserved bits') and values[path] == 0)}
    if unused:
        raise ValueError(f'Unused native fields: {sorted(unused)}')
    layout = parse_structured_object(type_id, emitter.data, 140)
    if not layout.complete:
        raise ValueError(layout.error)
    return emitter.data


def build_v140_chunk(chunk_id: str, fields: dict) -> bytes:
    """Emit a native settings chunk, including STMG game-parameter defaults.

    Supply every native field from a parsed chunk; update count/array fields for
    additions. Pass the payload to rewrite_soundbank(bank_chunk_payloads=...).
    """
    if chunk_id not in ('STMG', 'BKHD'):
        raise ValueError('Settings construction supports STMG and BKHD')
    values = dict(fields)
    emitter = _Emitter(values)
    _CHUNK_PARSERS[chunk_id][1](emitter)
    unused = set(values) - emitter.used
    if unused:
        raise ValueError(f'Unused native settings fields: {sorted(unused)}')
    parsed = parse_structured_chunk(chunk_id, emitter.data, 140)
    if not parsed.structure.complete:
        raise ValueError(parsed.structure.error)
    return emitter.data


def register_v140_rtpc_defaults(template: bytes, defaults: dict[int, float]) -> bytes:
    """Append private RTPC definitions to a native Init STMG, preserving others.

    New entries use no ramping and no built-in driver. Existing IDs are rejected;
    game/object-specific dynamic values remain the caller's explicit binding.
    """
    result = parse_soundbank(template)
    if result.bank_version != 140:
        raise ValueError('Native v140 Init template required')
    chunk = next(item for item in result.bank_chunks if item.chunk_id == 'STMG')
    if not chunk.structure.complete:
        raise ValueError(chunk.structure.error)
    fields = {field.path: field.value for field in chunk.structure.fields}
    existing = {int(field.value) for field in chunk.structure.fields
                if field.path.startswith('Game parameters/') and field.id_kind == 'game_parameter'}
    count = int(fields['Game parameter count'])
    for key, value in defaults.items():
        key = int(key)
        if not 0 < key <= 0xffffffff or key in existing:
            raise ValueError(f'Invalid or already registered RTPC ID: {key}')
        existing.add(key)
        count += 1
        prefix = f'Game parameters/{count}'
        fields.update({prefix + '/Game parameter': key, prefix + '/Default value': float(value),
                       prefix + '/Ramping': 0, prefix + '/Ramp up': 0.0,
                       prefix + '/Ramp down': 0.0, prefix + '/Built-in parameter': 0})
    fields['Game parameter count'] = count
    return rewrite_soundbank(template, {}, bank_chunk_payloads={'STMG': build_v140_chunk('STMG', fields)})


def native_template_fields(obj) -> dict:
    if obj.bank_version != 140 or not obj.structure or not obj.structure.complete:
        raise ValueError('A complete native v140 object is required')
    return {field.path: field.value for field in obj.structure.fields}


def build_v140_from_template(template, object_id, *, fields=None, sources=None,
                              children=None, random_sequence=None, switch_container=None,
                              music_segment=None, music_track_clips=None, clip_automations=None):
    """Clone a verified native node and reuse the established semantic setters.

    For new variable music transition/decision/playlist structures, use
    build_v140_object with a complete destination field dictionary instead.
    Composite arguments are tuples matching the existing setter arguments:
    random_sequence=(settings, children, playlist),
    switch_container=(settings, children, mappings, params),
    music_segment=(duration_ms, markers), music_track_clips=(clips, subtracks).
    """
    native_template_fields(template)
    obj = HircObject(1, template.type_id, template.type_name, object_id,
                     clone_hirc_payload(template, object_id), 0, bank_version=140)
    _decode_hirc_objects([obj], 140)
    operations = []
    if fields is not None:
        operations.append(lambda item: set_wwise_fields(item.payload, item.structure, fields))
    for value, setter in ((sources, set_bank_sources), (children, set_hirc_children)):
        if value is not None:
            operations.append(lambda item, setter=setter, value=value: setter(item, value))
    for value, setter in ((random_sequence, set_random_sequence), (switch_container, set_switch_container),
                          (music_segment, set_music_segment)):
        if value is not None:
            operations.append(lambda item, setter=setter, value=value: setter(item, *value))
    if music_track_clips is not None:
        operations.append(lambda item: set_music_track_clips(item, *music_track_clips, automations=clip_automations))
    for operation in operations:
        obj.payload = operation(obj)
        _decode_hirc_objects([obj], 140)
        if not obj.structure.complete:
            raise ValueError(obj.structure.error)
    return obj.payload


def _legacy_values(node, version):
    values = {field['path']: field['value'] for field in node['fields']}
    if version == 140:
        return values, []
    if version != 120:
        raise ValueError('Playback conversion supports v120 -> v140 only')
    resolutions = []
    for path in list(values):
        # Resolve properties by their versioned semantic names.
        if '/Property ' in path and path.endswith(' type') or '/Randomizer ' in path and path.endswith(' type'):
            old_id = int(values[path])
            name = property_names('object', 120).get(old_id)
            new_id = next((key for key, value in property_names('object', 140).items() if value == name), None)
            if new_id is None:
                raise ValueError(f'No native property equivalent for {path}: {old_id}')
            values[path] = new_id
        if path.endswith('/Target property') or '/State values/Value ' in '/' + path and path.endswith(' property'):
            if values[path] in (0x10, 0x11):
                values[path] = 0x21 - int(values[path])
    resolutions.extend({'path': field['path'], 'value': field['value'], 'kind': 'opaque_payload'}
                       for field in node['fields'] if field['storage'] == 'bytes' and field['size'])
    if any(path.startswith('Node/') for path in values):
        values['Node/Metadata/Override parent metadata'] = 0
        values['Node/Metadata/Metadata count'] = 0
        values['Node/Auxiliary sends/Reflections bus'] = 0
        values['Node/State properties/Property count'] = 0
        prefix = 'Node/Positioning/'
        legacy_type = values.pop(prefix + 'Legacy position type', None)
        # Legacy opaque routing bits are kept in the IR and must be resolved.
        for label in ('Legacy 2D next flag', 'Legacy 2D flag', 'Routing reserved bits'):
            value = values.pop(prefix + label, 0)
            if value:
                resolutions.append({'path': prefix + label, 'value': value, 'kind': 'opaque_field'})
        values[prefix + 'Speaker panner'] = 0
        values[prefix + '3D position source'] = 0 if legacy_type in (None, 1) else 1
        if legacy_type is not None:
            # v120 bits 0-1 discriminate position source, not spatialization.
            # Spatialization needs a native routing choice, recorded explicitly.
            resolutions.append({'path': prefix + 'Legacy position type', 'value': legacy_type, 'kind': 'positioning'})
            values[prefix + 'Spatialization'] = 1
            values[prefix + 'Enable attenuation'] = int(bool(values.get(prefix + 'Attenuation', 0)))
            loop = values.pop(prefix + 'Loop 3D automation path')
            values[prefix + 'Do not loop 3D automation path'] = int(not loop)
            values[prefix + 'Enable diffraction'] = 0
            values[prefix + '3D flags reserved bits'] = 0
        attenuation = values.pop(prefix + 'Attenuation', 0)
        if attenuation:
            count_path = 'Node/Properties/Property count'
            count = int(values[count_path])
            values[count_path] = count + 1
            values[f'Node/Properties/Property {count + 1} type'] = 0x46
            values['Node/Properties/Attenuation ShortID value'] = attenuation
    if node['type'] == 3 and int(values['Action type']) & 0xff00 == 0x0100:
        values['Apply to state transitions'] = 0
        values['Apply to dynamic sequences'] = 0
        values['Scope reserved bits'] = 0
    if node['type'] == 14:
        values['Height spread enabled'] = 0
    for path in list(values):
        if path.startswith('Cues/') and path.endswith('/Name bytes'):
            del values[path]
    if node['type'] == 11:
        for index in range(int(values['Clips/Clip count'])):
            values[f'Clips/{index + 1}/Event'] = 0
    for path in list(values):
        if path.endswith('/Jump-to playlist item'):
            values[path.rsplit('/', 1)[0] + '/Jump-to selection'] = 1 if values[path] else 0
    return values, resolutions


def compile_v140_graph(graph: dict, *, object_ids: dict, media: dict,
                       external_ids: dict, field_resolutions: dict | None = None,
                       state_property_schema: dict | None = None):
    """Return (HIRC upserts, report), or raise UnboundPlaybackGraph(report).

    object_ids: source HIRC ID -> unique private v140 ID.
    media: source media ID -> {id, data: bytes, storage: 0|1|2, plugin_id}.
    external_ids: kind -> {source ID: target ID}; no identity assumption.
    field_resolutions: 'source_object_id:source_field_path' ->
        {reason: str, target_fields: {native_path: value}}.
    state_property_schema: native state property ID ->
        {accumulation: int, in_db: int}, taken from native declarations.
    """
    if graph.get('schema') != 'reasy.playback/1':
        raise ValueError('Unsupported playback IR schema')
    object_ids = {int(key): int(value) for key, value in object_ids.items()}
    media = {int(key): value for key, value in media.items()}
    media_ids = [int(record['id']) for record in media.values()]
    if len(set(media_ids)) != len(media_ids):
        raise ValueError('Target media IDs must be unique')
    for record in media.values():
        data = record['data']
        if not 0 < int(record['id']) <= 0xffffffff or record['storage'] not in (0, 1, 2):
            raise ValueError('Invalid target media identity/storage')
        if not isinstance(data, bytes) or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
            raise ValueError('Mapped media must be encoded RIFF WEM bytes')
        codec = parse_wem_metadata(data).codec
        expected = {'Wwise PCM': 0x10001, 'Wwise ADPCM': 0x20001,
                    'Wwise Vorbis': 0x40001, 'WEM Opus': 0x140001}.get(codec)
        if expected is None or int(record['plugin_id']) != expected:
            raise ValueError(f'Media codec/plugin mismatch: {codec}')
        if record['storage'] == 1 and not 0 < record.get('prefetch_bytes', 0) <= len(data):
            raise ValueError('Prefetch sources require explicit prefetch_bytes')
    external_ids = {kind: {int(key): int(value) for key, value in rows.items()} for kind, rows in external_ids.items()}
    field_resolutions = field_resolutions or {}
    state_property_schema = {int(key): value for key, value in (state_property_schema or {}).items()}
    report = {'unbound': [], 'resolutions': [], 'objects': [], 'source_sha256': graph['sha256']}
    ids = {node['id'] for node in graph['nodes']}
    by_id = {node['id']: node for node in graph['nodes']}
    if len(ids) != len(graph['nodes']):
        raise ValueError('Duplicate source object IDs')
    if len(set(object_ids.values())) != len(object_ids):
        raise ValueError('Private target object IDs must be unique')
    upserts = {}
    for node in graph['nodes']:
        source_id = node['id']
        if not node['complete'] or node['type'] not in SUPPORTED_TYPES:
            report['unbound'].append({'object': source_id, 'kind': 'layout', 'error': node['error']})
            continue
        if source_id not in object_ids:
            report['unbound'].append({'object': source_id, 'kind': 'object_id'})
            continue
        values, requirements = _legacy_values(node, graph['bank_version'])
        if graph['bank_version'] == 120 and values.get('Node/State groups/Group count'):
            props = set()
            for field in node['fields']:
                if field['reference_role'] == 'state object' and field['value'] in by_id:
                    for state_field in by_id[field['value']]['fields']:
                        if state_field['path'].startswith('State values/Value ') and state_field['path'].endswith(' property'):
                            prop_id = int(state_field['value'])
                            props.add(0x21 - prop_id if prop_id in (0x10, 0x11) else prop_id)
            values['Node/State properties/Property count'] = len(props)
            for index, prop_id in enumerate(sorted(props), 1):
                definition = state_property_schema.get(prop_id)
                if definition is None:
                    report['unbound'].append({'object': source_id, 'kind': 'state_property', 'id': prop_id})
                    definition = {'accumulation': 0, 'in_db': 0}
                prefix = f'Node/State properties/{index}'
                values[prefix + '/Property'] = prop_id
                values[prefix + '/Accumulation'] = definition['accumulation']
                values[prefix + '/Stored in decibels'] = definition['in_db']
        for requirement in requirements:
            key = f"{source_id}:{requirement['path']}"
            resolution = field_resolutions.get(key)
            if not resolution or not resolution.get('reason') or not resolution.get('target_fields'):
                report['unbound'].append(dict(requirement, object=source_id))
            else:
                values.update(resolution['target_fields'])
                report['resolutions'].append(dict(requirement, object=source_id, resolution=resolution))
        # Produce the native structure first, then resolve its typed references.
        values['Object ID'] = object_ids[source_id]
        payload = build_v140_object(node['type'], object_ids[source_id], values)
        layout = parse_structured_object(node['type'], payload, 140)
        patched = bytearray(payload)
        for field in layout.fields:
            value = int(field.value) if field.id_kind else 0
            if (not field.id_kind or value == 0 or field.path == 'Object ID' or
                value == 0xffffffff and field.reference_role in ('transition source', 'transition destination')):
                continue
            kind = field.id_kind
            if kind in ('hirc', 'event'):
                if value in ids:
                    target = object_ids.get(value)
                else:
                    kind = 'bus' if 'bus' in field.reference_role else kind
                    target = external_ids.get(kind, {}).get(value)
            elif kind == 'source':
                record = media.get(value)
                target = int(record['id']) if record else None
            elif kind in ('plugin', 'cue', 'layer', 'playlist_item', 'curve'):
                # Codec/plugin and bank-local curve/cue identities are not game syncs.
                continue
            else:
                target = external_ids.get(kind, {}).get(value)
            if target is None:
                report['unbound'].append({'object': source_id, 'path': field.path, 'kind': kind, 'id': value})
            else:
                struct.pack_into('<I', patched, field.offset, target)
        # Reuse source serialization for the newly encoded data/storage.
        if node['type'] in (2, 11):
            obj = HircObject(1, node['type'], node['name'], object_ids[source_id], bytes(patched), 0, bank_version=140)
            _decode_hirc_objects([obj], 140)
            records_by_target = {int(record['id']): record for record in media.values()}
            sources = []
            for source in obj.sources:
                record = records_by_target.get(source.source_id)
                sources.append(replace(source, plugin_id=int(record['plugin_id']), stream_type=record['storage'],
                    in_memory_size=len(record['data']) if record['storage'] == 0 else record.get('prefetch_bytes', 0),
                    source_bits=(source.source_bits & ~2) | (2 if record['storage'] == 1 else 0)) if record else source)
            patched = set_bank_sources(obj, sources)
        upserts[object_ids[source_id]] = (node['type'], bytes(patched))
        report['objects'].append({'source_id': source_id, 'target_id': object_ids[source_id], 'type': node['type']})
    if report['unbound']:
        raise UnboundPlaybackGraph(report)
    return upserts, report


def build_v140_bank(template: bytes, upserts: dict, media: dict, *, embed_media=True, bank_id=None) -> bytes:
    """Replace a native v140 template's graph and optionally embed mapped WEMs."""
    result = parse_soundbank(template)
    if result.bank_version != 140:
        raise ValueError('Native v140 bank template required')
    # Content replacement starts from empty media, preserving native settings.
    chunks, trailing = _split_chunk_records(template)
    template = _pack_chunk_records([chunk for chunk in chunks if chunk.chunk_id not in (b'DIDX', b'DATA')], trailing)
    payloads = {int(record['id']): bytes(record['data']) for record in media.values()} if embed_media else {}
    settings = {}
    if bank_id is not None:
        if not 0 < int(bank_id) <= 0xffffffff:
            raise ValueError('Private bank ID must be a nonzero uint32')
        header = bytearray(next(chunk.payload for chunk in chunks if chunk.chunk_id == b'BKHD'))
        struct.pack_into('<I', header, 4, int(bank_id))
        settings[b'BKHD'] = bytes(header)
    declared_sources = {int(field.value) for kind, payload in upserts.values()
                        for field in parse_structured_object(kind, payload, 140).fields
                        if field.id_kind == 'source'}
    built = rewrite_soundbank(template, payloads,
        added_source_ids=set(payloads) - declared_sources,
        deleted_hirc_ids={obj.object_id for obj in result.objects} - set(upserts),
        hirc_upserts=upserts, bank_chunk_payloads=settings)
    checked = parse_soundbank(built)
    if checked.warnings or any(not obj.structure or not obj.structure.complete for obj in checked.objects):
        raise ValueError(f'Built bank is not completely parsed: {checked.warnings}')
    return built
