"""Exercise explicit low-memory consent without modifying the host."""

from contextlib import ExitStack, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import lifecycle
import preflight


class HostResourceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.root / 'installation.json').write_text(
            json.dumps({'domain': 'example.org', 'databases_initialized': True})
        )
        self.memory = 980000
        self.swap = 2097152
        self.free = 12 * 1024**3
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path == Path('/proc/meminfo'):
                return f'MemTotal: {self.memory} kB\nSwapTotal: {self.swap} kB\n'
            return original_read(path, *args, **kwargs)

        self.stack.enter_context(patch.object(Path, 'read_text', read))
        original_is_dir = Path.is_dir
        self.stack.enter_context(
            patch.object(
                Path,
                'is_dir',
                lambda path: path == Path('/run/systemd/system') or original_is_dir(path),
            )
        )
        self.write = self.stack.enter_context(patch.object(Path, 'write_text'))
        self.run = self.stack.enter_context(patch.object(lifecycle, 'run'))
        self.stack.enter_context(patch.object(lifecycle, 'ROOT', self.root, create=True))
        self.stack.enter_context(patch.object(preflight.platform, 'system', return_value='Linux'))
        self.stack.enter_context(patch.object(preflight.platform, 'machine', return_value='amd64'))
        self.stack.enter_context(
            patch.object(
                preflight.shutil,
                'disk_usage',
                side_effect=lambda _: SimpleNamespace(free=self.free),
            )
        )
        self.stack.enter_context(patch.dict(os.environ))
        os.environ.pop('MISBOT_ALLOW_LOW_MEMORY', None)
        self.stderr = self.stack.enter_context(redirect_stderr(io.StringIO()))

    def paths(self):
        return (
            ('preflight', lambda: preflight.system_check(self.root)),
            ('prepare_host', lifecycle.prepare_host),
        )

    def test_low_physical_ram_is_rejected_despite_sufficient_swap(self):
        for name, action in self.paths():
            with self.subTest(path=name):
                with self.assertRaisesRegex(RuntimeError, '2 GiB RAM'):
                    action()
        self.write.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), '')

    def test_only_exact_one_accepts_low_ram(self):
        for value in ('', '0', 'true', 'yes', '01', '1 ', 'FICTIONAL_PRIVATE_VALUE'):
            os.environ['MISBOT_ALLOW_LOW_MEMORY'] = value
            for name, action in self.paths():
                with self.subTest(value=value, path=name):
                    with self.assertRaisesRegex(RuntimeError, '2 GiB RAM') as caught:
                        action()
                    self.assertNotIn('FICTIONAL_PRIVATE_VALUE', str(caught.exception))
        self.write.assert_not_called()
        self.run.assert_not_called()

    def test_opt_in_warns_for_both_paths_and_removal_restores_rejection(self):
        os.environ['MISBOT_ALLOW_LOW_MEMORY'] = '1'
        for name, action in self.paths():
            with self.subTest(path=name):
                self.stderr.seek(0)
                self.stderr.truncate()
                action()
                warning = self.stderr.getvalue()
                self.assertIn('WARNING:', warning)
                self.assertIn('experimental', warning)
                self.assertIn('Swap does not replace RAM', warning)
                self.assertIn('out-of-memory', warning)
        self.run.assert_called_once_with(
            ['sysctl', '--load', '/etc/sysctl.d/90-matrix-stack-rtc.conf']
        )
        os.environ.pop('MISBOT_ALLOW_LOW_MEMORY')
        for name, action in self.paths():
            with self.subTest(path=name):
                with self.assertRaisesRegex(RuntimeError, '2 GiB RAM'):
                    action()

    def test_normal_ram_keeps_default_threshold_and_does_not_warn(self):
        self.memory = 1800000
        for _, action in self.paths():
            action()
        os.environ['MISBOT_ALLOW_LOW_MEMORY'] = '1'
        for _, action in self.paths():
            action()
        self.assertEqual(self.stderr.getvalue(), '')

    def test_opt_in_does_not_bypass_disk_failures(self):
        os.environ['MISBOT_ALLOW_LOW_MEMORY'] = '1'
        self.free = 2 * 1024**3
        for name, action in self.paths():
            with self.subTest(path=name):
                with self.assertRaisesRegex(RuntimeError, '3 GiB free'):
                    action()
        self.run.assert_not_called()
        self.write.assert_not_called()

    def test_opt_in_does_not_bypass_dns_or_port_failures(self):
        os.environ['MISBOT_ALLOW_LOW_MEMORY'] = '1'
        values = {'DOMAIN': 'example.org', 'PUBLIC_IP': '8.8.8.8'}
        for failing_check in ('dns_check', 'network_check'):
            with self.subTest(check=failing_check), ExitStack() as stack:
                stack.enter_context(patch.object(configuration, 'load', return_value=values))
                stack.enter_context(patch.object(configuration, 'assert_auth_identity'))
                checks = {
                    name: stack.enter_context(patch.object(preflight, name))
                    for name in ('dns_check', 'caa_check', 'network_check', 'proxy_check')
                }
                checks[failing_check].side_effect = RuntimeError('Fictional required check failure')
                with self.assertRaisesRegex(RuntimeError, 'Fictional required check failure'):
                    preflight.check(self.root)
                checks[failing_check].assert_called_once()
                checks['proxy_check'].assert_not_called()
        self.run.assert_not_called()
        self.write.assert_not_called()

    def test_opt_in_keeps_fresh_install_disk_reserve(self):
        os.environ['MISBOT_ALLOW_LOW_MEMORY'] = '1'
        (self.root / 'installation.json').unlink()
        self.free = 9 * 1024**3
        with patch.object(preflight.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'at least 10 GiB free'):
                preflight.system_check(self.root)
        self.run.assert_not_called()
        self.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
