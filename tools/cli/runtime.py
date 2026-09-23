"""Shared parsing, diagnostics, verification staging, and atomic candidate publication."""
import argparse
from contextlib import redirect_stdout
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

MHR_REGISTRY = Path(__file__).resolve().parents[2] / 'resources/data/dumps/rszmhrise.json'
_application = None


def qt_application():
    global _application
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    _application = QApplication.instance() or QApplication([])
    return _application


@dataclass
class EditResult:
    data: bytes
    details: dict = field(default_factory=dict)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def mutation_parser(commands, name, help_text):
    parser = commands.add_parser(name, help=help_text, description=help_text)
    parser.add_argument('source', type=Path)
    parser.add_argument('-o', '--output', required=True, type=Path, help='candidate file; must differ from every input')
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    parser.add_argument('--dry-run', action='store_true', help='build and verify without publishing')
    parser.set_defaults(kind='write')
    return parser


def read_parser(commands, name, help_text):
    parser = commands.add_parser(name, help=help_text, description=help_text)
    parser.add_argument('source', type=Path)
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    parser.set_defaults(kind='read')
    return parser


def add_batch(commands, *, prefix=(), depth=1):
    parser = mutation_parser(commands, 'batch', 'Apply a JSON command list and publish one verified candidate')
    parser.add_argument('--plan', required=True, type=Path, help='JSON array of editing-command argument arrays')
    parser.set_defaults(kind='batch', command='batch', batch_prefix=prefix, batch_depth=depth)


def run_operation(args):
    if not __debug__:
        raise ValueError('Resource verification requires Python without -O')
    with redirect_stdout(sys.stderr):
        try:
            result = args.run(args)
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
    if isinstance(result, bytes):
        return EditResult(result)
    if not isinstance(result, EditResult):
        raise TypeError('Editing operation must return verified bytes or EditResult')
    return result


def run_batch(args, parser):
    plan = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    if not isinstance(plan, list) or not plan:
        raise ValueError('Batch plan must be a nonempty JSON array of command argument arrays')
    reports = []
    with tempfile.TemporaryDirectory(prefix='reasy-cli-') as folder:
        current = args.source
        for index, step in enumerate(plan):
            if (not isinstance(step, list) or len(step) < args.batch_depth or
                    not all(isinstance(value, str) for value in step)):
                raise ValueError(f'Batch step {index} must be a command argument array')
            candidate = Path(folder) / str(index) / args.source.name
            argv = [*args.batch_prefix, *step[:args.batch_depth], str(current), '-o', str(candidate),
                    *step[args.batch_depth:]]
            with redirect_stdout(sys.stderr):
                try:
                    parsed = parser.parse_args(argv)
                except SystemExit as exc:
                    raise ValueError(f'Batch step {index} must specify an edit, not help') from exc
            if parsed.kind != 'write' or parsed.source != current or parsed.output != candidate or parsed.dry_run:
                raise ValueError('Batch steps must be edits and omit source, output, and --dry-run')
            if getattr(parsed, 'extra_inputs', None):
                check_output(args.output, parsed.extra_inputs(parsed))
            print(f'[{index + 1}/{len(plan)}] {" ".join(step[:args.batch_depth])}', file=sys.stderr)
            result = run_operation(parsed)
            candidate.parent.mkdir()
            candidate.write_bytes(result.data)
            current = candidate
            reports.append({'command': step[:args.batch_depth], 'details': result.details})
        return EditResult(result.data, {'operations': reports}), len(plan)


def publish(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != data:
            raise OSError('Staged output differs from the verified bytes')
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def check_output(destination, inputs):
    destination = Path(destination).resolve()
    for path in inputs:
        if path is None:
            continue
        path = Path(path).resolve()
        if path == destination or (destination.exists() and path.samefile(destination)):
            raise ValueError('Output must not replace the source or another input')


def main(build_parser, argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = '--json' in argv
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.kind == 'read':
            with redirect_stdout(sys.stderr):
                result = args.inspect(args)
            valid = result.get('valid', True) if getattr(args, 'check_valid', False) else True
            result.update(status='ok' if valid else 'error', command=args.command)
            if json_mode or not getattr(args, 'render', None):
                print(json.dumps(result, ensure_ascii=False, indent=None if json_mode else 2))
            else:
                args.render(result)
            return 0 if valid else 2
        if args.kind == 'bundle':
            if not __debug__:
                raise ValueError('Resource verification requires Python without -O')
            with redirect_stdout(sys.stderr):
                result = args.run(args)
            result.update(status='ok', command=args.command)
            print(json.dumps(result, ensure_ascii=False, indent=None if json_mode else 2))
            return 0
        source, destination = args.source.resolve(), args.output.resolve()
        inputs = [source]
        if getattr(args, 'extra_inputs', None):
            inputs.extend(Path(p).resolve() for p in args.extra_inputs(args) if p is not None)
        if args.kind == 'batch':
            inputs.append(args.plan)
        check_output(destination, inputs)
        original = source.read_bytes()
        result, steps = run_batch(args, parser) if args.kind == 'batch' else (run_operation(args), 1)
        report = {'status': 'ok', 'command': args.command, 'source': str(source), 'output': str(destination),
                  'dry_run': args.dry_run, 'steps': steps, 'source_size': len(original), 'output_size': len(result.data),
                  'source_sha256': hashlib.sha256(original).hexdigest(),
                  'output_sha256': hashlib.sha256(result.data).hexdigest(), 'changed': original != result.data,
                  'details': result.details}
        if not args.dry_run:
            publish(destination, result.data)
        if json_mode:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(f'{"Verified (dry run)" if args.dry_run else "Wrote"}: {destination} '
                  f'({len(original)} -> {len(result.data)} bytes)')
        return 0
    except (ValueError, OSError, AssertionError, IndexError, KeyError, OverflowError, struct.error) as exc:
        if json_mode:
            print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False))
        else:
            print(f'error: {exc}', file=sys.stderr)
        return 2
