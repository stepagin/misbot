"""A catalog must reveal missing behavior, not manufacture passing coverage."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('command_behavior', ROOT / 'ci/command_behavior.py')
behavior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(behavior)


class CommandBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(behavior.CATALOG.read_text())

    def test_catalog_matches_all_registry_commands_and_exact_test_declarations(self):
        self.assertEqual(behavior.validate(self.catalog), [])
        self.assertEqual(
            len(behavior.catalog_commands(self.catalog)), len(behavior.vocabulary(ROOT)[0])
        )

    def test_new_removed_or_duplicate_command_fails_validation(self):
        first = next(iter(self.catalog['groups'].values()))['commands']
        first.append('fictional new operation')
        self.assertTrue(
            any('Unknown commands' in error for error in behavior.validate(self.catalog))
        )
        first.pop()
        saved = first.pop()
        self.assertTrue(
            any('Missing commands' in error for error in behavior.validate(self.catalog))
        )
        first.extend((saved, saved))
        self.assertTrue(any('Duplicate' in error for error in behavior.validate(self.catalog)))

    def test_ui_and_recognized_bot_route_drift_are_visible(self):
        self.catalog['surfaces']['ui']['direct'].remove('status')
        self.catalog['route_signatures'][0]['values'].append('/fictional_new_route')
        errors = behavior.validate(self.catalog)
        self.assertTrue(any('UI direct' in error for error in errors))
        self.assertTrue(any('bot route' in error for error in errors))

    def test_stale_test_and_unsafe_file_reference_fail(self):
        reference = self.catalog['scenarios'][0]['tests'][0]
        reference['test'] += '_missing'
        self.assertTrue(
            any('Missing or ambiguous test' in error for error in behavior.validate(self.catalog))
        )
        reference['file'] = '../outside.py'
        self.assertTrue(
            any('Unsafe test path' in error for error in behavior.validate(self.catalog))
        )

    def test_scenario_requires_expectations_and_test_or_explicit_gap(self):
        item = self.catalog['scenarios'][0]
        item['tests'] = []
        item['expected_state'] = ''
        errors = behavior.validate(self.catalog)
        self.assertTrue(any('expected_state' in error for error in errors))
        self.assertTrue(any('explicit gap' in error for error in errors))

    def test_routing_does_not_close_behavior_and_presentation_does_not_prove_state(self):
        self.catalog['scenarios'] = [self.catalog['scenarios'][0]]
        item = self.catalog['scenarios'][0]
        item['axes'] = list(behavior.AXES)
        result = behavior.report(self.catalog, 'status')
        self.assertEqual(result['executed_tests'], 0)
        gaps = {(gap['surface'], gap['axis']) for gap in result['commands'][0]['gaps']}
        self.assertTrue(all(('cli', axis) in gaps for axis in behavior.AXES))
        item.update(level='presentation')
        gaps = {
            (gap['surface'], gap['axis'])
            for gap in behavior.report(self.catalog, 'status')['commands'][0]['gaps']
        }
        self.assertNotIn(('cli', 'output'), gaps)
        self.assertIn(('cli', 'state'), gaps)
        self.assertIn(('cli', 'success'), gaps)

    def test_linked_assertion_does_not_hide_a_declared_untested_corner(self):
        linked = self.catalog['scenarios'][0]
        linked.update(commands=['status'], level='backend', axes=['failure'])
        missing = dict(linked, id='fixture.explicit-gap', gap='Unconsidered failure boundary')
        missing.pop('tests')
        self.catalog['scenarios'] = [linked, missing]
        gaps = behavior.report(self.catalog, 'status')['commands'][0]['gaps']
        self.assertTrue(
            any(gap['axis'] == 'failure' and 'Unconsidered' in gap['reason'] for gap in gaps)
        )

    def test_bot_permissions_are_not_inherited_from_root_cli(self):
        row = behavior.report(self.catalog, 'start')['commands'][0]
        self.assertIn('root', row['role']['cli'])
        self.assertIn('MAS admin', row['role']['telegram'])
        self.assertIn('panel access', row['role']['matrix_admin'])
        member = behavior.report(self.catalog, 'member.invite')['commands'][0]
        self.assertIn('member', member['role']['telegram'])
        self.assertIn('cli', member['not_exposed'])

    def test_nonzero_or_wrong_count_cannot_be_reported_passed(self):
        scenario = next(item for item in self.catalog['scenarios'] if item['id'] == 'update.noop')
        for code, count in ((1, 1), (0, 0), (0, 2)):
            with self.subTest(code=code, count=count):
                runner = lambda *args, **kwargs: SimpleNamespace(
                    returncode=code, stdout=json.dumps({'test_state': 'passed', 'tests_run': count})
                )
                result = behavior.run_scenario(self.catalog, scenario['id'], runner=runner)
                self.assertEqual(result['state'], 'failed')

    def test_timeout_and_runner_failure_are_safe_and_distinct(self):
        for error, reason in (
            (OSError('FICTIONAL_PRIVATE'), 'runner_unavailable'),
            (subprocess.TimeoutExpired('test', 180), 'timeout'),
        ):
            with self.subTest(reason=reason):

                def runner(*args, **kwargs):
                    raise error

                result = behavior.run_scenario(self.catalog, 'update.noop', runner=runner)
                self.assertEqual(result['tests'][0]['reason'], reason)
                self.assertNotIn('FICTIONAL_PRIVATE', json.dumps(result))

    def fixture(self, root, file, test):
        (root / 'ci').mkdir()
        shutil.copyfile(ROOT / 'ci/command_behavior.py', root / 'ci/command_behavior.py')
        (root / 'tests').mkdir()
        (root / 'tests/__init__.py').write_text('')
        return {
            'scenarios': [
                {'id': 'fixture', 'level': 'backend', 'tests': [{'file': file, 'test': test}]}
            ]
        }

    def test_real_python_runner_imports_from_repo_and_distinguishes_skip_failure_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.fixture(root, 'tests/test_sample.py', 'Sample.test_ok')
            (root / 'tests/test_sample.py').write_text(
                'import unittest\nclass Sample(unittest.TestCase):\n'
                ' def test_ok(self): self.assertEqual(2 + 2, 4)\n'
                ' @unittest.skip("optional fixture")\n'
                ' def test_skip(self): pass\n'
                ' def test_fail(self): self.fail("FICTIONAL_PRIVATE")\n'
            )
            for name, state, reason in (
                ('test_ok', 'passed', 'completed'),
                ('test_skip', 'skipped', 'skipped'),
                ('test_fail', 'failed', 'assertion_failure_or_wrong_count'),
                ('test_missing', 'failed', 'import_or_missing_test'),
            ):
                with self.subTest(test=name):
                    catalog['scenarios'][0]['tests'][0]['test'] = 'Sample.' + name
                    result = behavior.run_scenario(catalog, 'fixture', root=root)
                    self.assertEqual(result['tests'][0]['state'], state)
                    self.assertEqual(result['tests'][0]['reason'], reason)
                    self.assertNotIn('FICTIONAL_PRIVATE', json.dumps(result))
                    self.assertIn('completed_utc', result)
                    self.assertIsNotNone(
                        result['fingerprint']['tests_sha256']['tests/test_sample.py']
                    )

    @unittest.skipUnless(shutil.which('node'), 'Node is required for node:test evidence')
    def test_real_node_runner_does_not_count_skipped_or_unmatched_tests_as_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.fixture(root, 'tests/sample.test.js', 'one passes')
            (root / 'tests/sample.test.js').write_text(
                'const test = require("node:test");\n'
                'test("one passes", () => {});\n'
                'test("one skips", {skip: true}, () => {});\n'
            )
            for name, state in (
                ('one passes', 'passed'),
                ('one skips', 'skipped'),
                ('missing', 'failed'),
            ):
                with self.subTest(test=name):
                    catalog['scenarios'][0]['tests'][0]['test'] = name
                    result = behavior.run_scenario(catalog, 'fixture', root=root)
                    self.assertEqual(result['tests'][0]['state'], state)

    def test_changed_source_during_run_invalidates_evidence(self):
        fingerprints = [{'source_sha256': 'before'}, {'source_sha256': 'after'}]
        runner = lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps({'test_state': 'passed', 'tests_run': 1})
        )
        with patch.object(behavior, 'fingerprint', side_effect=fingerprints):
            result = behavior.run_scenario(self.catalog, 'update.noop', runner=runner)
        self.assertTrue(result['source_changed_during_run'])
        self.assertEqual(result['state'], 'failed')
