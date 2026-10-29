"""CLI adapter for a native Rise resource batch plan."""
import argparse
import json
from pathlib import Path

from services.mhr.motion_batch import build_plan


def configure(commands):
    parser = commands.add_parser('build', help='Build and verify a native MOTLIST/MTRE/CLIP/MOTBANK plan')
    parser.add_argument('source', type=Path, help='native batch JSON plan')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    parser.set_defaults(kind='bundle', run=run)


def run(args):
    plan = json.loads(args.source.read_text(encoding='utf-8-sig'))
    return build_plan(plan, plan_path=args.source, report_path=args.report, dry_run=args.dry_run)
