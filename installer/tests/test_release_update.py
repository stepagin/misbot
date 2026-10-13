"""Stable release updates remain available to installed numbered builds."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_update
import releases
import state_store


class ReleaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data/admin-panel').mkdir(parents=True, mode=0o700)
        state_store.write_json(
            self.root / 'installation.json',
            {
                'backend': 'portable',
                'applied_release_version': '1.2.0-build.123',
            },
        )

    def test_queue_accepts_stable_promotion_from_build(self):
        with patch.object(release_update.subprocess, 'run') as launch:
            result = release_update.queue(self.root, '1.2.0', '$fixture')
        self.assertEqual(result['status'], 'queued')
        launch.assert_called_once()
        job = json.loads(release_update.job_path(self.root).read_text())
        self.assertEqual(job['before'], '1.2.0-build.123')
        self.assertEqual(job['version'], '1.2.0')

    def test_queue_preserves_stable_target_and_major_downgrade_gates(self):
        for number, error in (
            ('1.2.0-build.124', ValueError),
            ('1.1.9', RuntimeError),
            ('2.0.0', RuntimeError),
        ):
            with (
                self.subTest(number=number),
                patch.object(release_update.subprocess, 'run') as launch,
            ):
                with self.assertRaises(error):
                    release_update.queue(self.root, number, '$fixture')
                launch.assert_not_called()
                self.assertFalse(release_update.job_path(self.root).exists())

    def test_apply_promotes_build_to_stable_and_confirms_installed_result(self):
        source = self.root / 'source'

        def applied(*args, **kwargs):
            state_store.write_json(
                self.root / 'installation.json',
                {
                    'backend': 'portable',
                    'applied_release_version': '1.2.0',
                },
            )
            return subprocess.CompletedProcess(args[0], 0)

        with (
            patch.object(releases, 'latest', return_value={'version': '1.2.0'}),
            patch.object(releases, 'download', return_value=source) as download,
            patch.object(release_update.subprocess, 'run', side_effect=applied) as apply,
        ):
            result = release_update.apply(self.root, '1.2.0')
        self.assertTrue(result['success'])
        self.assertEqual(result['installed'], '1.2.0')
        download.assert_called_once()
        self.assertIn(str(source / 'update.py'), apply.call_args.args[0])

    def test_apply_rejects_major_and_skips_older_stable_without_downloading(self):
        for number in ('1.1.9', '2.0.0'):
            with (
                self.subTest(number=number),
                patch.object(releases, 'latest', return_value={'version': number}),
                patch.object(releases, 'download') as download,
                patch.object(release_update.subprocess, 'run') as apply,
            ):
                if number == '2.0.0':
                    with self.assertRaisesRegex(RuntimeError, 'Major'):
                        release_update.apply(self.root)
                else:
                    self.assertEqual(
                        release_update.apply(self.root)['installed'], '1.2.0-build.123'
                    )
                download.assert_not_called()
                apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
