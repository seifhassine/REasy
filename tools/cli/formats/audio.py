"""Structured entrypoint for verified Wwise playback-graph batches."""
import argparse
import json
from pathlib import Path

from services.audio.wwise_graph_importer import import_batch


def configure(domains):
    group = domains.add_parser('audio', help='Wwise playback-graph authoring')
    group.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    commands = group.add_subparsers(dest='command', required=True)
    parser = commands.add_parser('build', help='Compile and publish a playback batch')
    parser.add_argument('source', type=Path, help='reasy.playback-batch/1 jobs JSON')
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    parser.set_defaults(kind='bundle', run=run, check_valid=True)


def run(args):
    manifest = json.loads(args.source.read_text(encoding='utf-8-sig'))
    if manifest.get('schema') != 'reasy.playback-batch/1':
        raise ValueError('Unsupported playback batch schema')
    report = import_batch(manifest['jobs'], args.output, dry_run=args.dry_run, inputs=[args.source])
    return {'valid': report['status'] == 'built', 'output': str(args.output.resolve()),
            'dry_run': args.dry_run, 'details': report}
