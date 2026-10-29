"""Clone native request userdata without creating a GUI viewer."""
from file_handlers.rcol.rcol_handler import RcolHandler
from file_handlers.rcol.editing import clone_request_set, resolve_requests
from tools.cli.runtime import MHR_REGISTRY


def configure(parser):
    parser.add_argument('--name', required=True)
    parser.add_argument('--field0', type=int, help='new hit reference (default: max field0 + 1)')
    template = parser.add_mutually_exclusive_group(required=True)
    template.add_argument('--template', help='unique template request name')
    template.add_argument('--template-id', type=int, help='template request ID')


def run(args):
    handler = RcolHandler()
    handler.filepath = str(args.source)
    handler.init_type_registry(str(MHR_REGISTRY))
    handler.read(args.source.read_bytes())
    before = resolve_requests(handler.rcol)
    matches = [i for i, request in enumerate(handler.rcol.request_sets)
               if (request.info.id == args.template_id if args.template_id is not None
                   else request.info.name == args.template)]
    if len(matches) != 1:
        raise ValueError(f'Request template resolves to {len(matches)} entries')
    index = clone_request_set(handler.rcol, matches[0], args.name, field0=args.field0)
    expected = resolve_requests(handler.rcol)
    for identity, row in before.items():
        if expected[identity] != row:
            raise ValueError(f'Cloning changed existing request {identity}')
    output = handler.rebuild()
    reopened = RcolHandler()
    reopened.filepath = handler.filepath
    reopened.init_type_registry(str(MHR_REGISTRY))
    reopened.read(output)
    if resolve_requests(reopened.rcol) != expected or reopened.rebuild() != output:
        raise ValueError('Cloned request graph did not roundtrip')
    request = reopened.rcol.request_sets[index]
    from tools.cli.runtime import EditResult
    return EditResult(output, {'index': index, 'id': request.info.id, 'field0': request.info.field0})
