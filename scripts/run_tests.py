"""Run the test modules in parallel processes and compare against a saved result.

    python scripts/run_tests.py                 # every tests/test_*.py module
    python scripts/run_tests.py --fast          # skip modules that need local game data
    python scripts/run_tests.py motfsm efx      # modules whose name contains a pattern
    python scripts/run_tests.py --save .cache/tests.json
    python scripts/run_tests.py --compare .cache/tests.json

Each module runs in its own interpreter, so Qt state and module-level caches
cannot leak between modules. A result is identified by unittest's FAIL/ERROR
headers, including subTest parameters, which makes runs directly comparable.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
# A module that mentions one of these reads the local corpus (tests/TESTFILE),
# an installed game, or an externally supplied sample.
CORPUS_MARKERS = ('TESTFILE', 'find_rise_installation', 'REASY_')
HEADER = re.compile(r'^(FAIL|ERROR): (.+)$', re.MULTILINE)
SUMMARY = re.compile(r'^Ran (\d+) tests? in ', re.MULTILINE)
SKIPPED = re.compile(r'skipped=(\d+)')


def needs_corpus(path: Path) -> bool:
    text = path.read_text(encoding='utf-8', errors='replace')
    return any(marker in text for marker in CORPUS_MARKERS)


def select(patterns, fast):
    modules = sorted(TESTS.glob('test_*.py'))
    if patterns:
        modules = [m for m in modules if any(p in m.stem for p in patterns)]
    if fast:
        modules = [m for m in modules if not needs_corpus(m)]
    return modules


def run_module(path: Path, timeout: int):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8',
               PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get('PYTHONPATH')])))
    command = [sys.executable, '-m', 'unittest', 'discover', '-s', str(TESTS), '-t', str(TESTS), '-p', path.name]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=timeout,
                                   encoding='utf-8', errors='replace')
        output, code = completed.stderr + completed.stdout, completed.returncode
    except subprocess.TimeoutExpired as error:
        # Partial output is str on Windows but bytes on POSIX, whatever encoding= says.
        captured = ''.join(part.decode('utf-8', 'replace') if isinstance(part, bytes) else part
                           for part in (error.stderr, error.output) if part)
        output = f'ERROR: {path.stem} (timeout after {timeout}s)\n{captured}'
        code = -1
    ran = SUMMARY.search(output)
    skipped = SKIPPED.search(output)
    problems = [f'{kind}: {name.strip()}' for kind, name in HEADER.findall(output)]
    if code != 0 and not problems:
        problems = [f'ERROR: {path.stem} (exit {code}, no unittest summary)']
    return path.stem, {
        'ran': int(ran.group(1)) if ran else 0,
        'skipped': int(skipped.group(1)) if skipped else 0,
        'problems': problems,
        'seconds': round(time.perf_counter() - started, 1),
        'output': output if problems else '',
    }


def compare(results, baseline_path: Path):
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    shared = results.keys() & baseline.keys()
    # Problems are keyed by module, so an identical header elsewhere cannot hide one.
    before = {(m, p) for m in shared for p in baseline[m]['problems']}
    after = {(m, p) for m in shared for p in results[m]['problems']}
    new, fixed = sorted(after - before), sorted(before - after)
    print(f'\nCompared {len(shared)} modules with {baseline_path}')
    for title, items in (('New problems', new), ('No longer failing', fixed)):
        print(f'{title}: {len(items)}')
        for module, problem in items:
            print(f'  {module}: {problem}')
    for name in sorted(results.keys() - baseline.keys()):
        print(f'  (not in baseline) {name}')
    return not new


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('patterns', nargs='*', help='substrings of test module names')
    parser.add_argument('--fast', action='store_true', help='skip modules that read local game data')
    parser.add_argument('-j', '--jobs', type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument('--timeout', type=int, default=3600, help='seconds per module')
    parser.add_argument('--save', type=Path, help='write the results as JSON')
    parser.add_argument('--compare', type=Path, help='fail only on problems absent from this saved result')
    parser.add_argument('--show-output', action='store_true', help='print the output of failing modules')
    args = parser.parse_args(argv)

    modules = select(args.patterns, args.fast)
    if not modules:
        parser.error('no test modules selected')
    print(f'{len(modules)} modules, {args.jobs} parallel jobs', flush=True)
    started, results = time.perf_counter(), {}
    with ThreadPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(run_module, module, args.timeout) for module in modules]
        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result
            state = 'ok' if not result['problems'] else f"{len(result['problems'])} problems"
            print(f"{result['seconds']:7.1f}s  {name:<40} ran={result['ran']:<4} skipped={result['skipped']:<3} {state}",
                  flush=True)
    results = dict(sorted(results.items()))
    problems = [p for r in results.values() for p in r['problems']]
    total = sum(r['ran'] for r in results.values())
    print(f'\nRan {total} tests in {time.perf_counter() - started:.0f}s; {len(problems)} problems')
    if args.show_output:
        for name, result in results.items():
            if result['problems']:
                print(f'\n===== {name}\n{result["output"]}')
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        stored = {name: {k: v for k, v in r.items() if k != 'output'} for name, r in results.items()}
        args.save.write_text(json.dumps(stored, indent=1, ensure_ascii=False), encoding='utf-8')
    if args.compare:
        return 0 if compare(results, args.compare) else 1
    return 0 if not problems else 1


if __name__ == '__main__':
    raise SystemExit(main())
