"""Single resource-oriented CLI for inspection and verified editing."""
import argparse
import importlib
from pathlib import Path

from tools.fsm.common import integer
from .runtime import Parser, mutation_parser, read_parser, add_batch, main as run_cli, MHR_REGISTRY

EDIT_COMMANDS = {
    'clip': {
        'sequence-copy': ('sequence_copy', 'Copy/retime native CLIP sequences'),
        'sequence-delete': ('sequence_delete', 'Delete selected native CLIP sequences'),
        'node-copy': ('graph_edit', 'Copy a CLIP node subtree'),
        'node-delete': ('graph_edit', 'Delete a CLIP node subtree'),
        'property-copy': ('graph_edit', 'Copy a CLIP property and its keys'),
        'property-delete': ('graph_edit', 'Delete a CLIP property and its keys'),
        'key-copy': ('graph_edit', 'Copy a CLIP property key'),
        'key-delete': ('graph_edit', 'Delete a CLIP property key'),
        'track-add': ('track_add', 'Insert a cloned BOOL track into a flat compact CLIP section'),
        'events-drop': ('events_drop', 'Silence selected scalar event keys'),
        'event-move': ('event_move', 'Move an effect or sound trigger to another frame'),
        'effect-set': ('effect_set', 'Replace a string-valued effect key without resizing'),
        'property-range': ('property_range', 'Extend a property range and its final key'),
        'bool-window': ('bool_window', 'Set the on/off window of a four-key BOOL track'),
    },
    'pfb': {
        'element-add': ('element_add', 'Deep-clone an effect element and its child objects'),
        'element-edit': ('element_edit', 'Edit rotation, joint, or unparent frame'),
    },
    'rcol': {
        'request-add': ('request_add', 'Clone request userdata while reusing the shape group'),
        'request-edit': ('request_edit', 'Set scalar request userdata fields'),
    },
}


def build_parser():
    parser = Parser(prog='python -m tools', description='REasy resource CLI for agents and scripts.')
    parser.add_argument('--json', action='store_true', help='one JSON document on stdout; diagnostics on stderr')
    domains = parser.add_subparsers(dest='domain', required=True)
    from . import move
    group = domains.add_parser('move', help='Validate or retime a MOTLIST/FSM/RCOL move bundle')
    group.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    commands = group.add_subparsers(dest='command', required=True)
    sub = read_parser(commands, 'check', 'Validate a move JSON plan and report its frame domains')
    sub.set_defaults(inspect=move.check, check_valid=True)
    sub = mutation_parser(commands, 'retime', 'Bake a new animation duration and scale matching CLIP/FSM/RCOL timing')
    sub.add_argument('--frames', type=integer, required=True, help='new animation end frame (positive integer)')
    sub.set_defaults(kind='bundle', run=move.retime)
    for domain in ('fsm', 'motion', 'clip', 'pfb', 'rcol', 'rsz', 'efx'):
        group = domains.add_parser(domain, help=f'{domain.upper()} inspection and editing')
        group.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        commands = group.add_subparsers(dest='command', required=True)
        if domain == 'fsm':
            from tools.fsm.cli import register_commands
            register_commands(commands, prefix=('fsm',))
            continue
        for command, (module_name, description) in EDIT_COMMANDS.get(domain, {}).items():
            sub = mutation_parser(commands, command, description)
            module = importlib.import_module(f'.formats.{domain}.{module_name}', __package__)
            module.configure(sub)
            sub.set_defaults(run=module.run)
            if domain in ('pfb', 'rcol'):
                sub.set_defaults(extra_inputs=lambda args: (MHR_REGISTRY,))
            for action in sub._actions:
                if action.type is int:
                    action.type = integer
        if domain in ('motion', 'clip'):
            from .formats import motion
            sub = read_parser(commands, 'query', 'List motion IDs, names, and sequence categories')
            motion.configure_query(sub)
            sub.set_defaults(inspect=motion.query)
            if domain == 'motion':
                from .formats import motion_bake
                sub = mutation_parser(commands, 'bake', 'Bake frame ranges and speeds into one animation slot')
                motion_bake.configure(sub)
                sub.set_defaults(run=motion_bake.run, extra_inputs=lambda args: (args.donor, args.hold_template))
                sub = mutation_parser(commands, 'duplicate', 'Duplicate an animation slot through the native writer')
                motion.configure_duplicate(sub)
                sub.set_defaults(run=motion.duplicate)
                from .formats import wilds_import
                sub = mutation_parser(commands, 'import-wilds', 'Import selected Wilds animations into a Rise motion list')
                wilds_import.configure(sub)
                sub.set_defaults(run=wilds_import.run,
                                 extra_inputs=lambda args: (args.donor, args.hold_template))
            else:
                for name in ('dump', 'layout'):
                    sub = read_parser(commands, name, 'Inspect CLIP trees' if name == 'dump' else 'Inspect sequence byte layout')
                    motion.configure_query(sub)
                    sub.set_defaults(inspect=motion.query)
        elif domain in ('pfb', 'rsz'):
            from .formats import rsz
            sub = read_parser(commands, 'query', 'Inspect typed instances, fields, and reference values')
            rsz.configure_query(sub)
            sub.set_defaults(inspect=rsz.query)
            if domain == 'pfb':
                sub = read_parser(commands, 'elements', 'List effect elements with IDs and array positions')
                sub.add_argument('--id', type=integer, dest='element_id')
                sub.add_argument('--registry', type=Path, default=MHR_REGISTRY)
                sub.set_defaults(inspect=rsz.elements)
            else:
                sub = mutation_parser(commands, 'field-edit', 'Set scalar fields in a PFB/SCN/USER instance')
                sub.add_argument('--registry', type=Path, default=MHR_REGISTRY)
                sub.add_argument('--instance', required=True, type=integer)
                sub.add_argument('--set', required=True, action='append', dest='assignments')
                sub.set_defaults(run=rsz.field_edit, extra_inputs=lambda args: (args.registry,))
                sub = read_parser(commands, 'scan', 'Scan PFB/SCN/USER files for typed field values')
                rsz.configure_query(sub)
                sub.add_argument('--recursive', action='store_true')
                sub.set_defaults(inspect=rsz.scan)
                sub = read_parser(commands, 'references', 'Extract PFB property-reference mappings')
                sub.add_argument('--registry', type=Path, default=MHR_REGISTRY)
                sub.set_defaults(inspect=rsz.references)
        elif domain == 'rcol':
            from .formats.rcol import query
            from .formats.rcol.selection import configure as configure_selection
            sub = read_parser(commands, 'query', 'List requests, groups, shapes, and userdata fields')
            configure_selection(sub)
            sub.add_argument('--registry', type=Path, default=MHR_REGISTRY)
            sub.set_defaults(inspect=query.run)
        else:
            from .formats import efx
            sub = read_parser(commands, 'query', 'Inspect EFX header and strings with offsets')
            sub.add_argument('--game-dir', help='resolve the input as a virtual path in game resources')
            sub.set_defaults(inspect=efx.query)
        if domain in ('motion', 'clip', 'pfb', 'rcol', 'rsz'):
            add_batch(commands, prefix=(domain,))
    add_batch(domains, depth=2)
    return parser


def main(argv=None):
    return run_cli(build_parser, argv)
