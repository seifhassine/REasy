"""Verified multi-file flipbook authoring command."""
import json
from pathlib import Path

from file_handlers.efx.sequence_assets import build_sequence_bundle, load_rgba, slice_atlas


def configure(commands):
    parser = commands.add_parser('sequence', help='Build a Rise EFX/UVS/TEX flipbook bundle')
    parser.add_argument('source', type=Path)
    parser.add_argument('--entry', required=True, help='emitter path, e.g. entries/1')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--frames', nargs='+', type=Path, help='PNG files in playback order')
    group.add_argument('--atlas', type=Path)
    parser.add_argument('--rectangles', type=json.loads, help='atlas frame rectangles [[x,y,width,height], ...] in playback order')
    parser.add_argument('--resource', required=True, help='virtual base path without extension, e.g. vfx/custom/fire')
    parser.add_argument('-o', '--output', type=Path, required=True, help='new bundle directory')
    parser.add_argument('--fps', type=float, default=60.)
    parser.add_argument('--padding', type=int, default=4)
    parser.add_argument('--columns', type=int)
    parser.add_argument('--compression', choices=('bc7', 'rgba8'), default='bc7')
    parser.add_argument('--linear', action='store_true', help='input RGB values are linear instead of sRGB')
    parser.add_argument('--no-mipmaps', action='store_true')
    parser.add_argument('--dependency-root', type=Path, help='copy referenced resources from an unpacked natives tree')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.set_defaults(kind='bundle', run=run)
    linked = commands.add_parser('uvs-edit', help='Edit UVS sequences/frames/textures and remap linked EFX selections')
    linked.add_argument('source', type=Path)
    linked.add_argument('--uvs', type=Path, required=True)
    linked.add_argument('--resource', required=True, help='virtual UVS path referenced by EFX')
    linked.add_argument('--effect-resource', required=True, help='output EFX virtual base without extension')
    linked.add_argument('--plan', type=Path, required=True)
    linked.add_argument('--dependency-root', type=Path)
    linked.add_argument('-o', '--output', type=Path, required=True)
    linked.add_argument('--dry-run', action='store_true')
    linked.add_argument('--json', action='store_true')
    linked.set_defaults(kind='bundle', run=run_linked)


def run_linked(args):
    from .efx import load_editable
    from file_handlers.efx.sequence_project import SequenceProject
    from file_handlers.uvs.uvs_file import UvsFile
    if args.output.exists():
        raise ValueError('Bundle destination already exists; select a new directory')
    uvs = UvsFile(); uvs.read(args.uvs.read_bytes(), version=7)
    project = SequenceProject(load_editable(args.source), uvs, args.resource)
    operations = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    if not isinstance(operations, list) or not operations:
        raise ValueError('UVS plan must contain a nonempty array of operations')
    for operation in operations:
        project.apply(operation)
    bundle = project.bundle(args.effect_resource)
    if args.dependency_root:
        from utils.resource_file_utils import ResourceResolutionContext
        context = ResourceResolutionContext(unpacked_dir=str(args.dependency_root), game='MHRise')
        bundle.include_dependencies(lambda path: context.resolve(path, allow_selection_dialog=False))
    if not args.dry_run:
        bundle.publish(args.output)
    return {'output': str(args.output.resolve()), 'dry_run': args.dry_run, 'details': bundle.manifest}


def run(args):
    from .efx import load_editable
    if args.output.exists():
        raise ValueError('Bundle destination already exists; select a new directory')
    if args.atlas:
        if not args.rectangles:
            raise ValueError('--atlas requires --rectangles')
        frames = slice_atlas(args.atlas, args.rectangles)
    else:
        if args.rectangles:
            raise ValueError('--rectangles requires --atlas')
        frames = [load_rgba(path) for path in args.frames]
    bundle = build_sequence_bundle(load_editable(args.source), args.entry, frames, args.resource,
                                   fps=args.fps, padding=args.padding, columns=args.columns,
                                   compression=args.compression, srgb=not args.linear, mipmaps=not args.no_mipmaps)
    if args.dependency_root:
        from utils.resource_file_utils import ResourceResolutionContext
        context = ResourceResolutionContext(unpacked_dir=str(args.dependency_root), game='MHRise')
        bundle.include_dependencies(lambda path: context.resolve(path, allow_selection_dialog=False))
    if not args.dry_run:
        bundle.publish(args.output)
    return {'output': str(args.output.resolve()), 'dry_run': args.dry_run, 'details': bundle.manifest}
