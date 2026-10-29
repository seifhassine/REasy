"""Standalone CLI and compatibility imports for native Rise batch construction."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.mhr.motion_batch import neutral_motion, tree_from_template, validate_references, execute_plan, build_plan


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    report = build_plan(plan, plan_path=args.plan, report_path=args.report, dry_run=args.dry_run)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
