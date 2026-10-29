"""Headless World v120 playback export and native v140 batch import.

python -m tools.wwise_graph_importer prepare --import-ir ... --source-dir ...
    --template-dir ... --output-dir ... --namespace em356_00
Fill jobs.json external_ids and field_resolutions from the reported unbound items.
python -m tools.wwise_graph_importer build --jobs ... --output-dir ...
All writes go to the explicit output directory; source banks/manifests are read-only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from services.file_io import publish_bytes, publish_files, check_output

from file_handlers.sound.bnk_parser import parse_soundbank, wwise_id_from_name
from file_handlers.sound.playback_graph import (
    UnboundPlaybackGraph, export_playback_graph, select_playback_graph,
    compile_v140_graph, build_v140_bank,
    build_v140_object,
)


def _write_json(path, value):
    publish_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def prepare_import_ir_jobs(import_ir: Path, source_dir: Path, template_dir: Path,
                           output_dir: Path, namespace: str, *, bank_templates=None) -> list[dict]:
    """Adapt existing source_binding/target_media_* rows without flattening them.

    Produces selected lossless playback graphs, private object/event identities,
    media file bindings and unresolved semantic requirements. Repeated source
    media must map consistently. Existing target_event_id values take precedence.
    """
    manifest = json.loads(import_ir.read_text(encoding='utf-8'))
    banks = {}
    for binding in manifest['audio_bindings']:
        source = binding['source_binding']
        candidates = source['media_candidates']
        ids, files = binding['target_media_ids'], binding['target_media_files']
        if len(candidates) != len(ids) or len(ids) != len(files):
            raise ValueError('Media candidates/IDs/files must be aligned')
        for bank_name in source['event_banks']:
            bank = banks.setdefault(Path(bank_name).stem, {'roots': {}, 'media': {}})
            event_id, target_event = int(source['event_id']), binding.get('target_event_id')
            existing = bank['roots'].get(event_id)
            if existing is not None and target_event is not None and existing != target_event:
                raise ValueError(f'Conflicting Event binding: {bank_name}:{event_id}')
            bank['roots'][event_id] = existing if target_event is None else target_event
        for candidate, target_id, file in zip(candidates, ids, files):
            bank = banks.setdefault(candidate['bank'], {'roots': {}, 'media': {}})
            source_id = str(candidate['media_id'])
            record = {'id': int(target_id), 'file': file, 'storage': 0, 'plugin_id': 0x40001}
            existing = bank['media'].get(source_id)
            if existing is not None and existing != record:
                raise ValueError(f'Conflicting media binding {candidate["bank"]}:{source_id}')
            bank['media'][source_id] = record
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for name, binding in banks.items():
        if not binding['roots']:
            continue
        source_path = source_dir / (name + '.nbnk')
        graph = select_playback_graph(export_playback_graph(source_path.read_bytes()), binding['roots'])
        graph_path = output_dir / (name + '.playback.json')
        _write_json(graph_path, graph)
        kind = name.rsplit('_', 1)[-1]
        template_name = (bank_templates or {}).get(name, namespace + '_' + kind)
        object_ids = {str(node['id']): int(binding['roots'].get(node['id']) or
            wwise_id_from_name(f'{namespace}_{kind}_world_{node["id"]}')) for node in graph['nodes']}
        required_media = {str(field['value']) for node in graph['nodes'] for field in node['fields']
                          if field['id_kind'] == 'source' and field['value']}
        media = {key: value for key, value in binding['media'].items() if key in required_media}
        missing = required_media - set(media)
        job = {'name': name, 'graph': str(graph_path),
               'template': str(template_dir / (template_name + '.bnk.2.x64')),
               'media_template': str(template_dir / (template_name + '_media.bnk.2.x64')),
               'object_ids': object_ids, 'media': media,
               'missing_media': sorted(map(int, missing)),
               'external_ids': {}, 'field_resolutions': {},
               'events': {str(key): object_ids[str(key)] for key in binding['roots']}}
        jobs.append(job)
    _write_json(output_dir / 'jobs.json', {'schema': 'reasy.playback-batch/1', 'jobs': jobs})
    return jobs


def import_batch(jobs: list[dict], output_dir: Path, *, dry_run=False, inputs=()) -> dict:
    """Compile all jobs, report unresolved items, and write only complete batches."""
    if not isinstance(jobs, list) or not jobs:
        raise ValueError('Playback batch requires a nonempty jobs list')
    output_dir = Path(output_dir)
    report = {'schema': 'reasy.playback-build/1', 'status': 'unbound', 'jobs': []}
    pending, outputs = [], {}
    protected = set(Path(path).resolve() for path in inputs)
    protected.update(Path(job[key]).resolve() for job in jobs for key in ('graph', 'template', 'media_template') if job.get(key))
    protected.update(Path(record['file']).resolve() for job in jobs for record in job['media'].values())
    for job in jobs:
        graph = json.loads(Path(job['graph']).read_text(encoding='utf-8'))
        media = {int(key): dict(record, data=Path(record['file']).read_bytes()) for key, record in job['media'].items()}
        try:
            upserts, detail = compile_v140_graph(graph, object_ids=job['object_ids'], media=media,
                external_ids=job.get('external_ids', {}), field_resolutions=job.get('field_resolutions', {}),
                state_property_schema=job.get('state_property_schema', {}))
        except UnboundPlaybackGraph as exc:
            detail = exc.report
            upserts = None
        detail['name'] = job['name']
        detail['events'] = job.get('events', {})
        detail['selection'] = graph.get('selection')
        detail['external_ids'] = job.get('external_ids', {})
        detail['state_property_schema'] = job.get('state_property_schema', {})
        detail['binding_policy'] = job.get('binding_policy', '')
        report['jobs'].append(detail)
        if upserts is not None:
            for node in job.get('native_objects', []):
                if int(node['id']) in upserts:
                    raise ValueError('New native object collides with an imported object')
                upserts[int(node['id'])] = (int(node['type']), build_v140_object(int(node['type']), int(node['id']), node['fields']))
            template_path = Path(job['template'])
            split_media = bool(job.get('media_template'))
            bank = build_v140_bank(template_path.read_bytes(), upserts, media, embed_media=not split_media, bank_id=job.get('bank_id'))
            pending.append((job.get('bank_output', template_path.name), bank, detail))
            if split_media:
                media_path = Path(job['media_template'])
                pending.append((job.get('media_output', media_path.name), build_v140_bank(media_path.read_bytes(), {}, media,
                                                                                         bank_id=job.get('media_bank_id')), detail))
    if not any(detail['unbound'] for detail in report['jobs']):
        names = [name for name, _, _ in pending]
        if len(names) != len(set(names)):
            raise ValueError('Batch output filenames collide')
        for name in names:
            if Path(name).name != name or name == 'build-report.json':
                raise ValueError('Bank output must be a filename and must not overwrite an input')
            check_output(output_dir / name, protected)
        for name, data, detail in pending:
            path = output_dir / name
            outputs[path] = data
            result = parse_soundbank(data)
            detail.setdefault('outputs', []).append({'path': str(path), 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'bank_id': result.bank_id,
                'objects': len(result.objects), 'media': len(result.didx_entries)})
        report['status'] = 'built'
    report['dry_run'] = dry_run
    report_path = output_dir / 'build-report.json'
    check_output(report_path, protected)
    outputs[report_path] = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8')
    if not dry_run:
        publish_files(outputs, inputs=protected)
    return report


def prepare_music_import_ir_job(import_ir: Path, source_bank: Path, template: Path,
                                 output_dir: Path, namespace: str) -> dict:
    """Import layered music and its State dependencies; render references stay separate."""
    manifest = json.loads(import_ir.read_text(encoding='utf-8'))
    roots = {node['source_object']['object_id'] for node in manifest['music_nodes']}
    graph = export_playback_graph(source_bank.read_bytes(), object_ids=roots, include_dependencies=True)
    ids = {node['id'] for node in graph['nodes']}
    graph['selection'] = {'music_objects': [node['source_object']['object_id'] for node in manifest['music_nodes']],
                          'dependency_objects': sorted(ids - {node['source_object']['object_id'] for node in manifest['music_nodes']})}
    output_dir.mkdir(parents=True, exist_ok=True)
    graph_path = output_dir / 'music.playback.json'
    _write_json(graph_path, graph)
    object_ids = {str(node['source_object']['object_id']): node['planned_target_id'] for node in manifest['music_nodes']}
    object_ids.update({str(key): wwise_id_from_name(f'{namespace}_state_{key}') for key in ids if str(key) not in object_ids})
    media = {}
    references = []
    for record in manifest['media']:
        if record['representation'] != 'layered_track':
            references.append(record['target_media_id'])
            continue
        media[str(record['source']['media_id'])] = {'id': record['target_media_id'], 'file': record['target_wem'],
                                                    'storage': 0, 'plugin_id': 0x40001}
    job = {'name': namespace, 'graph': str(graph_path), 'template': str(template),
           'object_ids': object_ids, 'media': media, 'external_ids': {}, 'field_resolutions': {},
           'state_property_schema': {}, 'native_objects': [],
           'music_root': object_ids[str(manifest['source_music_root'])],
           'bank_id': wwise_id_from_name(namespace), 'bank_output': namespace + '.bnk.2.x64',
           'render_reference_media_not_used': references,
           'source_battle_state_paths': manifest['source_battle_state_paths']}
    _write_json(output_dir / 'jobs.json', {'schema': 'reasy.playback-batch/1', 'jobs': [job]})
    return job
