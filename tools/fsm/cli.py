"""FSM command registration shared by both REasy CLI entrypoints."""
import argparse
import importlib
from pathlib import Path

from tools.cli.runtime import Parser, mutation_parser, add_batch, main as run_cli
from .common import assignment, integer

STRUCTURAL = {
    'action-add': ('action_add', 'Append a cloned Action'),
    'action-import': ('action_import', 'Import an Action instance from another FSM file'),
    'action-remove': ('action_remove', 'Remove an Action reference from a node'),
    'node-clone': ('node_clone', 'Clone a leaf node with motion/effect overrides'),
    'node-action-child': ('node_action_child', 'Move one Action onto a new child node'),
    'attack-clone': ('attack_clone', 'Clone an attack with independent transition maps/data'),
    'node-chainsaw': ('node_chainsaw', 'Add a Charge Axe chainsaw branch'),
    'state-add': ('state_add', 'Append a derivation with its own transition map'),
    'state-condition': ('state_condition', 'Replace a state condition with a clone'),
    'state-own-map': ('state_own_map', 'Give a state an independent transition map ID'),
    'state-add-event': ('state_add_event', 'Append a cloned transition event'),
    'event-copy': ('event_copy', 'Clone a shared transition event into a private copy for one state'),
    'state-order': ('state_order', 'Reorder state priority using a complete position permutation'),
    'state-transition': ('state_transition', 'Set TransitionData fields (EndType/exitFrame) of one state'),
}
SCALAR = {
    'action-edit': ('action_edit', 'Set existing Action scalar fields'),
    'condition-edit': ('condition_edit', 'Set existing state Condition scalar fields'),
    'event-edit': ('event_edit', 'Edit existing state events, including incoming transitions'),
    'state-target': ('state_target', 'Retarget an existing state'),
    'hit-request': ('point_action', 'Set PlayerHitAction2._hitIndex from RCOL field0'),
    'effect': ('point_action', 'Set a SetEffect element, container, or frame'),
}


def register_commands(commands, *, prefix=()):
    from .query import inspect as inspect_query, render
    for name, help_text in [('query', 'Find nodes'), ('dump', 'Inspect complete nodes and their references')]:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument('source', type=Path)
        sub.add_argument('--node', action='append', required=name == 'dump', help='exact node selector (repeatable)')
        sub.add_argument('--name', help='node name substring')
        sub.add_argument('--motion', type=integer)
        sub.add_argument('--class', dest='klass', help='action class substring')
        sub.add_argument('--derives-to', help='outgoing target name substring')
        sub.add_argument('--actions', action='store_true')
        sub.add_argument('--states', action='store_true')
        sub.add_argument('--limit', type=integer)
        sub.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        sub.set_defaults(kind='read', inspect=inspect_query, render=render)
    for name, (module_name, help_text) in STRUCTURAL.items():
        sub = mutation_parser(commands, name, help_text)
        module = importlib.import_module(f'.operations.{module_name}', __package__)
        module.configure(sub)
        sub.set_defaults(run=module.run)
        for action in sub._actions:
            if action.dest == 'set':
                action.type = assignment
            elif action.type is int:
                action.type = integer
        if name == 'action-import':
            sub.set_defaults(extra_inputs=lambda args: (args.from_fsm,))
    for name, (function, help_text) in SCALAR.items():
        sub = mutation_parser(commands, name, help_text)
        sub.add_argument('--node', required=True, help='name, path, index:N, or 0xHASH[:EX]')
        from . import editing
        sub.set_defaults(run=getattr(editing, function))
        if name == 'action-edit':
            sub.add_argument('--class', dest='klass', required=True)
            sub.add_argument('--exact', action='store_true')
        if name in ('action-edit', 'effect', 'hit-request'):
            sub.add_argument('--position', type=integer, help='Action slot on the node')
        if name in ('action-edit', 'condition-edit', 'event-edit'):
            sub.add_argument('--set', action='append', required=True, type=assignment, metavar='FIELD=VALUE')
        if name == 'event-edit':
            sub.add_argument('--class', dest='klass', required=True)
            selection = sub.add_mutually_exclusive_group(required=True)
            selection.add_argument('--states', help='state positions on --node')
            selection.add_argument('--incoming', action='store_true', help='all states targeting --node')
            sub.add_argument('--allow-shared', action='store_true')
        if name == 'condition-edit':
            sub.add_argument('--states', required=True, help='state positions: 0-9 or 0,2,5')
            sub.add_argument('--allow-shared', action='store_true')
        if name == 'state-target':
            sub.add_argument('--state', type=integer, required=True)
            sub.add_argument('--target', required=True)
        if name == 'hit-request':
            group = sub.add_mutually_exclusive_group(required=True)
            group.add_argument('--field0', '--request', dest='request', type=integer,
                               help='native RCOL field0 hit reference')
            group.add_argument('--id', dest='request_id', type=integer,
                               help='resolve an authoring ID through --rcol')
            sub.add_argument('--rcol', type=Path, help='validate/resolve the referenced request')
            sub.set_defaults(extra_inputs=lambda args: (args.rcol,) if args.rcol else ())
            sub.add_argument('--expect', type=integer)
        if name == 'effect':
            sub.add_argument('--element', type=integer)
            sub.add_argument('--frame', type=float)
            sub.add_argument('--container', type=integer)
            sub.add_argument('--expect-element', type=integer)
    add_batch(commands, prefix=prefix)


def build_parser():
    parser = Parser(prog='python -m tools.fsm', description='REasy FSM inspection and verified editing.')
    parser.add_argument('--json', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)
    register_commands(commands)
    return parser


def main(argv=None):
    return run_cli(build_parser, argv)
