"""Standalone entrypoint for the Wwise graph import service."""
import argparse
import json
from pathlib import Path
from services.audio.wwise_graph_importer import (
    prepare_import_ir_jobs, prepare_music_import_ir_job, import_batch,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    for name in ('import-ir', 'source-dir', 'template-dir', 'output-dir'):
        prepare.add_argument('--' + name, type=Path, required=True)
    prepare.add_argument('--namespace', required=True)
    prepare.add_argument('--bank-template', action='append', default=[], metavar='SOURCE=TARGET_BASE')
    build = commands.add_parser('build')
    build.add_argument('--jobs', type=Path, required=True)
    build.add_argument('--output-dir', type=Path, required=True)
    music = commands.add_parser('prepare-music')
    for name in ('import-ir', 'source-bank', 'template', 'output-dir'):
        music.add_argument('--' + name, type=Path, required=True)
    music.add_argument('--namespace', required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        bank_templates = dict(item.split('=', 1) for item in args.bank_template)
        jobs = prepare_import_ir_jobs(args.import_ir, args.source_dir, args.template_dir, args.output_dir, args.namespace,
                                     bank_templates=bank_templates)
        report = import_batch(jobs, args.output_dir)
    elif args.command == 'prepare-music':
        job = prepare_music_import_ir_job(args.import_ir, args.source_bank, args.template, args.output_dir, args.namespace)
        report = import_batch([job], args.output_dir)
    else:
        manifest = json.loads(args.jobs.read_text(encoding='utf-8'))
        if manifest.get('schema') != 'reasy.playback-batch/1':
            raise ValueError('Unsupported batch schema')
        report = import_batch(manifest['jobs'], args.output_dir, inputs=[args.jobs])
    print(json.dumps({'status': report['status'], 'jobs': len(report['jobs']),
                      'unbound': sum(len(job['unbound']) for job in report['jobs'])}))
    return 0 if report['status'] == 'built' else 2


if __name__ == '__main__':
    raise SystemExit(main())
