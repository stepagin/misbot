"""Failed updates must fail the CLI even when the child exits successfully."""

import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import command_wrapper
import release_update
import releases
import state_store


class ReleaseUpdateExitTests(unittest.TestCase):
    def test_cli_exit_agrees_with_result_and_preserves_failure_detail(self):
        for child_code, installed, expected in ((23, '1.2.0', 1), (0, '1.2.0', 1), (0, '1.2.1', 0)):
            with self.subTest(child_code=child_code, installed=installed):
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    output = io.StringIO()
                    with (
                        patch.object(sys, 'argv', ['release_update.py', '--directory', folder]),
                        patch.object(state_store, 'require_root'),
                        patch.object(command_wrapper, 'trusted_installation'),
                        patch.object(releases, 'latest', return_value={'version': '1.2.1'}),
                        patch.object(releases, 'installed', side_effect=['1.2.0', installed]),
                        patch.object(releases, 'download', return_value=root / 'source'),
                        patch.object(
                            subprocess,
                            'run',
                            return_value=subprocess.CompletedProcess([], child_code),
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaises(SystemExit) as result,
                    ):
                        runpy.run_path(release_update.__file__, run_name='__main__')
                    self.assertEqual(result.exception.code, expected)
                    record = json.loads(output.getvalue())
                    self.assertEqual(record['success'], expected == 0)
                    self.assertEqual(record['installed'], installed)
                    self.assertEqual(record['exit_code'], child_code)
                    if expected:
                        self.assertIn('update recover', record['detail'])

    def test_already_current_is_success_without_starting_update(self):
        output = io.StringIO()
        with (
            patch.object(sys, 'argv', ['release_update.py', '--directory', '/fictional']),
            patch.object(state_store, 'require_root'),
            patch.object(command_wrapper, 'trusted_installation'),
            patch.object(releases, 'latest', return_value={'version': '1.2.0'}),
            patch.object(releases, 'installed', return_value='1.2.0'),
            patch.object(releases, 'download') as download,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(release_update.main(), 0)
        download.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['installed'], '1.2.0')

    def test_background_failure_is_persisted_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release_update.job_path(root).parent.mkdir(parents=True)
            state_store.write_json(
                release_update.job_path(root),
                {
                    'id': 'fictional-job',
                    'state': 'queued',
                    'version': '1.2.1',
                },
            )
            with (
                patch.object(
                    sys,
                    'argv',
                    ['release_update.py', '--directory', folder, '--job', 'fictional-job'],
                ),
                patch.object(state_store, 'require_root'),
                patch.object(command_wrapper, 'trusted_installation'),
                patch.object(
                    release_update,
                    'apply',
                    return_value={'success': False, 'installed': '1.2.0', 'exit_code': 0},
                ),
            ):
                self.assertEqual(release_update.main(), 1)
            saved = json.loads(release_update.job_path(root).read_text())
            self.assertEqual(saved['state'], 'failed')
            self.assertFalse(saved['result']['success'])
            self.assertIn('finished', saved)


if __name__ == '__main__':
    unittest.main()
