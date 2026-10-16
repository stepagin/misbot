"""Validate command scenarios and inspect gaps; run linked tests only with --run."""

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType
import unittest

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'ci/command_behavior.json'
AXES = (
    'success',
    'failure',
    'permissions',
    'preconditions',
    'interruption',
    'retry',
    'output',
    'state',
)
SURFACES = ('cli', 'ui', 'telegram', 'matrix_admin', 'matrix_referral')
LEVELS = ('routing', 'presentation', 'backend', 'interaction')


def route_signature(root, filename, kind):
    source = (root / filename).read_text(encoding='utf-8')
    if kind in ('telegram_slash', 'telegram_callbacks', 'telegram_buttons'):
        tree = ast.parse(source)
        if kind == 'telegram_buttons':
            return sorted(
                {
                    node.args[1].value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'button'
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                }
            )
        values = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        if kind == 'telegram_slash':
            return sorted({value for value in values if re.fullmatch(r'/[a-z_]+', value)})
        return sorted(
            {value.split('|', 1)[0] for value in values if re.match(r'^[a-z_]+\|', value)}
        )
    if kind == 'matrix_exact_text':
        return sorted(set(re.findall(r'text === "([^"\n]+)"', source)))
    raise ValueError('Unknown route signature extractor')


def source_module(root, name):
    path = root / 'installer' / (name + '.py')
    module = ModuleType(name)
    module.__file__ = str(path)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def vocabulary(root):
    commands = source_module(root, 'cli_commands')
    menus = source_module(root, 'ui_actions').MENUS
    direct = {
        item.path for _, items in menus.values() for item in items if not item.path.startswith('@')
    }
    return {item.path for item in commands.COMMANDS}, direct


def safe_path(root, value):
    if not isinstance(value, str):
        raise ValueError('Test file must be a relative path')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or path.suffix not in ('.py', '.js'):
        raise ValueError('Unsafe test path: ' + value)
    target = root / path
    if (
        not target.is_file()
        or target.is_symlink()
        or not target.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError('Test file is missing or not a regular repository file: ' + value)
    if 'tests' not in path.parts:
        raise ValueError('Evidence must reference a test: ' + value)
    return target


def test_link(root, reference):
    """Check an exact declaration without importing tests or running their setup."""
    if not isinstance(reference, dict) or set(reference) != {'file', 'test'}:
        raise ValueError('Test reference needs exactly file and test')
    path = safe_path(root, reference['file'])
    name = reference['test']
    if not isinstance(name, str) or not name:
        raise ValueError('Empty test name')
    source = path.read_text(encoding='utf-8')
    if path.suffix == '.py':
        names = [
            node.name + '.' + method.name
            for node in ast.parse(source).body
            if isinstance(node, ast.ClassDef)
            for method in node.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            and method.name.startswith('test_')
        ]
    else:
        # The repository uses top-level node:test calls with literal double-quoted names.
        # This checks that supported declaration syntax, not JavaScript control flow.
        names = [
            json.loads(value)
            for value in re.findall(r'^test\(\s*("(?:\\.|[^"\\])*")\s*,', source, re.M)
        ]
    if names.count(name) != 1:
        raise ValueError('Missing or ambiguous test: ' + reference['file'] + '::' + name)


def catalog_commands(catalog):
    return [command for group in catalog['groups'].values() for command in group['commands']]


def validate(catalog, root=ROOT):
    """Return structural/link errors; no claim that linked assertions passed."""
    errors = []
    try:
        if catalog.get('format') != 1:
            raise ValueError('Unsupported catalog format')
        known, direct_ui = vocabulary(root)
        declared = catalog_commands(catalog)
        duplicates = sorted(key for key, count in Counter(declared).items() if count != 1)
        if duplicates:
            errors.append('Duplicate command ownership: ' + ', '.join(duplicates))
        for label, values in (
            ('Missing commands', known - set(declared)),
            ('Unknown commands', set(declared) - known),
        ):
            if values:
                errors.append(label + ': ' + ', '.join(sorted(values)))
        for name, group in catalog['groups'].items():
            for key in ('role', 'runtime', 'invariants', 'risk'):
                if not group.get(key):
                    errors.append(name + ': missing ' + key)
        for signature in catalog['route_signatures']:
            if route_signature(root, signature['file'], signature['kind']) != signature['values']:
                errors.append('Recognized bot route literals changed: ' + signature['kind'])
        for surface in SURFACES[2:]:
            for field in ('role', 'runtime'):
                if not catalog['surface_requirements'][surface].get(field):
                    errors.append(surface + ': missing ' + field)
        overrides = catalog['surfaces']
        for surface in SURFACES[1:]:
            settings = overrides[surface]
            for mode, paths in settings.items():
                if mode not in ('direct', 'embedded', 'conditional') or not isinstance(paths, list):
                    errors.append(surface + ': invalid exposure list')
                elif set(paths) - known:
                    errors.append(surface + ': unknown command exposure')
            flattened = [item for paths in settings.values() for item in paths]
            if len(flattened) != len(set(flattened)):
                errors.append(surface + ': duplicate exposure')
        if set(overrides['ui']['direct']) != direct_ui:
            errors.append('UI direct command map differs from ui_actions.MENUS')
        scenario_ids = [item['id'] for item in catalog['scenarios']]
        if len(set(scenario_ids)) != len(scenario_ids):
            errors.append('Duplicate scenario ID')
        bot_only = catalog['bot_only']
        for name, item in bot_only.items():
            if not item.get('role') or not item.get('runtime') or not item.get('invariants'):
                errors.append(name + ': incomplete bot-only behavior')
            if not set(item['surfaces']) <= set(SURFACES[2:]):
                errors.append(name + ': invalid bot surface')
        for item in catalog['scenarios']:
            context = item['id'] + ': '
            targets = item.get('commands', [])
            if targets != ['*'] and (not targets or set(targets) - (known | set(bot_only))):
                errors.append(context + 'unknown or missing targets')
            if not item.get('surfaces') or not set(item['surfaces']) <= set(SURFACES):
                errors.append(context + 'invalid surfaces')
            if not item.get('axes') or not set(item['axes']) <= set(AXES):
                errors.append(context + 'invalid axes')
            if item.get('level') not in LEVELS:
                errors.append(context + 'invalid test level')
            for field in ('given', 'expected_output', 'expected_state'):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(context + 'missing ' + field)
            if not item.get('tests') and not item.get('gap'):
                errors.append(context + 'needs test evidence or an explicit gap')
            if item.get('tests') and item.get('gap'):
                errors.append(
                    context + 'split linked assertions and untested gaps into separate scenarios'
                )
            if targets == ['*'] and item.get('surfaces') != ['cli']:
                errors.append(context + 'wildcard scenarios are only valid for CLI routing')
            for target in targets if targets != ['*'] else []:
                if target in known | set(bot_only):
                    if set(item.get('surfaces', [])) - set(exposure(catalog, target)):
                        errors.append(
                            context + 'scenario references an unexposed surface: ' + target
                        )
            for reference in item.get('tests', []):
                try:
                    test_link(root, reference)
                except (OSError, ValueError, SyntaxError) as error:
                    errors.append(context + str(error))
    except (KeyError, TypeError, ValueError) as error:
        errors.append('Malformed catalog: ' + str(error))
    return errors


def exposure(catalog, command):
    if command in catalog['bot_only']:
        return {surface: 'direct' for surface in catalog['bot_only'][command]['surfaces']}
    result = {'cli': 'direct'}
    for surface, modes in catalog['surfaces'].items():
        for mode, commands in modes.items():
            if command in commands:
                result[surface] = mode
    return result


def describe(catalog, command):
    if command in catalog['bot_only']:
        return catalog['bot_only'][command]
    return next(group for group in catalog['groups'].values() if command in group['commands'])


def report(catalog, command=None):
    commands = [command] if command else [*catalog_commands(catalog), *catalog['bot_only']]
    rows = []
    for path in commands:
        behavior = describe(catalog, path)
        surfaces = exposure(catalog, path)
        scenarios = [
            item
            for item in catalog['scenarios']
            if path in item['commands']
            or item['commands'] == ['*']
            and path not in catalog['bot_only']
        ]
        gaps = []
        for surface in surfaces:
            for axis in AXES:
                matching = [
                    item
                    for item in scenarios
                    if surface in item['surfaces'] and axis in item['axes']
                ]
                levels = ('backend', 'interaction')
                if axis == 'output':
                    levels = (
                        ('presentation', 'interaction', 'backend')
                        if surface == 'cli'
                        else ('presentation', 'interaction')
                    )
                linked = [
                    item['id'] for item in matching if item.get('tests') and item['level'] in levels
                ]
                if not linked or any(item.get('gap') for item in matching):
                    gaps.append(
                        {
                            'surface': surface,
                            'axis': axis,
                            'reason': '; '.join(item['gap'] for item in matching if item.get('gap'))
                            or 'No linked scenario; not proven by catalog presence.',
                        }
                    )
        roles = {
            surface: behavior['role']
            if surface in ('cli', 'ui') or path in catalog['bot_only']
            else catalog['surface_requirements'][surface]['role']
            for surface in surfaces
        }
        runtime = {
            surface: behavior['runtime']
            if surface in ('cli', 'ui') or path in catalog['bot_only']
            else catalog['surface_requirements'][surface]['runtime']
            + '; action: '
            + behavior['runtime']
            for surface in surfaces
        }
        rows.append(
            {
                'command': path,
                'role': roles,
                'runtime': runtime,
                'invariants': behavior['invariants'],
                'risk': behavior.get('risk', 'account access'),
                'surfaces': surfaces,
                'not_exposed': sorted(set(SURFACES) - set(surfaces)),
                'scenarios': [
                    {
                        'id': item['id'],
                        'level': item['level'],
                        'evidence': 'linked_not_run' if item.get('tests') else 'declared_gap',
                    }
                    for item in scenarios
                ],
                'gaps': gaps,
            }
        )
    return {
        'status': 'declared_and_linked_only',
        'executed_tests': 0,
        'commands': rows,
        'gap_count': sum(len(row['gaps']) for row in rows),
    }


def python_test(name):
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(name)
    result = unittest.TestResult()
    suite.run(result)
    state = (
        'passed'
        if result.testsRun == 1
        and not result.skipped
        and not result.expectedFailures
        and result.wasSuccessful()
        else 'skipped'
        if result.skipped and result.wasSuccessful()
        else 'failed'
    )
    reason = (
        'completed'
        if state == 'passed'
        else 'skipped'
        if result.skipped
        else 'import_or_missing_test'
        if loader.errors
        else 'test_error'
        if result.errors
        else 'expected_failure'
        if result.expectedFailures
        else 'assertion_failure_or_wrong_count'
    )
    print(json.dumps({'test_state': state, 'tests_run': result.testsRun, 'reason': reason}))
    return 0 if state == 'passed' else 1


def fingerprint(root, references):
    paths = {root / item['file'] for item in references}
    paths.update((root / 'ci/command_behavior.py', root / 'ci/command_behavior.json'))
    paths.update((root / 'installer').glob('*.py'))
    paths.update((root / 'installer').glob('*.sh'))
    for directory in ('telegram', 'matrix-federation', 'matrix-referral'):
        for suffix in ('*.py', '*.js'):
            paths.update((root / 'bots' / directory).glob(suffix))
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.is_file()
    }
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {
        'source_sha256': digest,
        'catalog_sha256': hashes.get('ci/command_behavior.json'),
        'checker_sha256': hashes.get('ci/command_behavior.py'),
        'tests_sha256': {item['file']: hashes.get(item['file']) for item in references},
    }


def run_scenario(catalog, identifier, root=ROOT, runner=subprocess.run):
    scenario = next(item for item in catalog['scenarios'] if item['id'] == identifier)
    results = []
    before = fingerprint(root, scenario.get('tests', []))
    for reference in scenario.get('tests', []):
        filename, name = reference['file'], reference['test']
        if filename.endswith('.py'):
            target = filename[:-3].replace('/', '.') + '.' + name
            args = [sys.executable, str(root / 'ci/command_behavior.py'), '--python-test', target]
        else:
            args = [
                'node',
                '--test',
                '--test-reporter=tap',
                '--test-isolation=none',
                '--test-name-pattern',
                '^' + re.escape(name) + '$',
                filename,
            ]
        try:
            process = runner(
                args, cwd=root, capture_output=True, text=True, timeout=180, check=False
            )
            if filename.endswith('.py'):
                try:
                    result = json.loads(process.stdout.strip().splitlines()[-1])
                    state = result['test_state'] if process.returncode in (0, 1) else 'failed'
                    reason = result.get('reason', 'invalid_runner_result')
                    if state == 'passed' and (
                        process.returncode != 0 or result.get('tests_run') != 1
                    ):
                        state, reason = 'failed', 'exit_or_test_count_mismatch'
                except (ValueError, KeyError, IndexError):
                    state = 'failed'
                    reason = 'invalid_runner_result'
            else:
                selected = re.findall(
                    r'^ok \d+ - ' + re.escape(name) + r'([ \t]*#.*)?$', process.stdout, re.M
                )
                state = (
                    'passed'
                    if process.returncode == 0 and len(selected) == 1 and not selected[0]
                    else 'skipped'
                    if process.returncode == 0 and len(selected) == 1
                    else 'failed'
                )
                reason = (
                    'completed'
                    if state == 'passed'
                    else 'skipped_or_no_matching_test'
                    if process.returncode == 0
                    else 'test_runner_failed'
                )
        except subprocess.TimeoutExpired:
            state = 'failed'
            reason = 'timeout'
        except OSError:
            state = 'failed'
            reason = 'runner_unavailable'
        results.append({**reference, 'state': state, 'reason': reason})
    after = fingerprint(root, scenario.get('tests', []))
    drift = before != after
    return {
        'scenario': identifier,
        'level': scenario['level'],
        'state': 'passed'
        if results and not drift and all(item['state'] == 'passed' for item in results)
        else 'not_tested'
        if not results
        else 'failed',
        'tests': results,
        'completed_utc': datetime.now(timezone.utc).isoformat(),
        'fingerprint': before,
        'source_changed_during_run': drift,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--command', help='Exact CLI command or bot-only behavior ID')
    scope.add_argument(
        '--scenario', help='Show one declared scenario and its exact test references'
    )
    scope.add_argument(
        '--run', metavar='SCENARIO', help='Explicitly run isolated tests linked to one scenario'
    )
    parser.add_argument('--json', action='store_true', help='Print machine-readable data')
    parser.add_argument(
        '--gaps', action='store_true', help='List missing behavioral axes by command'
    )
    parser.add_argument(
        '--strict-gaps',
        action='store_true',
        help='Return nonzero while the selected scope has gaps',
    )
    parser.add_argument('--python-test', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.python_test:
        return python_test(args.python_test)
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
    errors = validate(catalog)
    if errors:
        print(
            json.dumps({'errors': errors}, ensure_ascii=False) if args.json else '\n'.join(errors)
        )
        return 1
    identifiers = {item['id'] for item in catalog['scenarios']}
    if args.command and args.command not in set(catalog_commands(catalog)) | set(
        catalog['bot_only']
    ):
        parser.error('Unknown command; use an exact registry path')
    if (args.scenario and args.scenario not in identifiers) or (
        args.run and args.run not in identifiers
    ):
        parser.error('Unknown scenario ID')
    if args.run:
        result = run_scenario(catalog, args.run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['state'] == 'passed' else 1
    if args.scenario:
        print(
            json.dumps(
                next(item for item in catalog['scenarios'] if item['id'] == args.scenario),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    result = report(catalog, args.command)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f'Catalog valid: {len(catalog_commands(catalog))} CLI commands; {len(catalog["scenarios"])} scenarios. Tests not run.'
        )
        print(
            'Gaps: '
            + str(result['gap_count'])
            + ' command/surface/axis combinations. Links are not passing tests.'
        )
        if args.command:
            row = result['commands'][0]
            for field in (
                'command',
                'role',
                'runtime',
                'invariants',
                'risk',
                'surfaces',
                'scenarios',
            ):
                print(field + ': ' + str(row[field]))
            for gap in row['gaps']:
                print('GAP ' + gap['surface'] + '/' + gap['axis'] + ': ' + gap['reason'])
        else:
            counts = Counter(row['risk'] for row in result['commands'] if row['gaps'])
            priorities = {
                'destructive': 0,
                'credentials': 0,
                'privilege': 0,
                'data': 1,
                'recovery': 1,
            }
            for risk, count in sorted(
                counts.items(), key=lambda item: (priorities.get(item[0], 2), -item[1], item[0])
            ):
                print(f'  {risk}: {count} behaviors with gaps')
            if args.gaps:
                for row in sorted(
                    result['commands'],
                    key=lambda row: (priorities.get(row['risk'], 2), row['command']),
                ):
                    if row['gaps']:
                        print(
                            row['command']
                            + ': '
                            + ', '.join(gap['surface'] + '/' + gap['axis'] for gap in row['gaps'])
                        )
            print('Inspect: --command "backup create"; --scenario ID; explicit execution: --run ID')
    return 1 if args.strict_gaps and result['gap_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
