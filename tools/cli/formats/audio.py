"""Wwise playback-graph commands: prepare jobs from an import IR, then build banks."""
import argparse
import json
from pathlib import Path

from services.audio.wwise_graph_importer import (
    import_batch, prepare_import_ir_jobs, prepare_music_import_ir_job,
)


def configure(domains):
    group = domains.add_parser('audio', help='Wwise playback-graph authoring')
    group.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    commands = group.add_subparsers(dest='command', required=True)

    parser = commands.add_parser('prepare', help='Extract playback graphs and write jobs.json from an import IR',
                                 description='Writes the selected graphs and jobs.json into --output, then '
                                             'attempts a build. Fill external_ids/field_resolutions for the '
                                             'reported unbound items and run `audio build` on jobs.json.')
    for name in ('import-ir', 'source-dir', 'template-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('-o', '--output', required=True, type=Path, help='output directory')
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--bank-template', action='append', default=[], metavar='SOURCE=TARGET_BASE')
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    parser.set_defaults(kind='bundle', run=prepare, check_valid=True)

    parser = commands.add_parser('prepare-music', help='Extract a layered music graph and write jobs.json',
                                 description='Writes music.playback.json and jobs.json into --output, then '
                                             'attempts a build.')
    for name in ('import-ir', 'source-bank', 'template'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('-o', '--output', required=True, type=Path, help='output directory')
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    parser.set_defaults(kind='bundle', run=prepare_music, check_valid=True)

    parser = commands.add_parser('build', help='Compile and publish a playback batch')
    parser.add_argument('source', type=Path, help='reasy.playback-batch/1 jobs JSON')
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    parser.set_defaults(kind='bundle', run=run, check_valid=True)


def _result(report, output, dry_run=False):
    return {'valid': report['status'] == 'built', 'output': str(output.resolve()), 'dry_run': dry_run,
            'jobs': str((output / 'jobs.json').resolve()), 'details': report}


def prepare(args):
    templates = {}
    for item in args.bank_template:
        source, separator, target = item.partition('=')
        if not separator:
            raise ValueError('--bank-template requires SOURCE=TARGET_BASE')
        templates[source] = target
    jobs = prepare_import_ir_jobs(args.import_ir, args.source_dir, args.template_dir, args.output,
                                  args.namespace, bank_templates=templates)
    return _result(import_batch(jobs, args.output), args.output)


def prepare_music(args):
    job = prepare_music_import_ir_job(args.import_ir, args.source_bank, args.template, args.output, args.namespace)
    return _result(import_batch([job], args.output), args.output)


def run(args):
    manifest = json.loads(args.source.read_text(encoding='utf-8-sig'))
    if manifest.get('schema') != 'reasy.playback-batch/1':
        raise ValueError('Unsupported playback batch schema')
    report = import_batch(manifest['jobs'], args.output, dry_run=args.dry_run, inputs=[args.source])
    return {'valid': report['status'] == 'built', 'output': str(args.output.resolve()),
            'dry_run': args.dry_run, 'details': report}
