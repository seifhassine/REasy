"""Command adapters for native MHR move bundles."""
from services.mhr import move


def check(args):
    return move.check(args.source)


def retime(args):
    return move.retime(args.source, args.frames, args.output, dry_run=args.dry_run)
