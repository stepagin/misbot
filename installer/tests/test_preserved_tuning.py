"""Preserved application preferences cannot replace installer-owned runtime wiring."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

BUNDLE = Path(__file__).resolve().parents[1]


class PreservedTuningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'stack'
        result = self.generate('--domain', 'example.org', '--initialize-env', '--preview')
        self.assertEqual(result.returncode, 0, result.stderr)

    def generate(self, *args):
        return subprocess.run(
            [sys.executable, str(BUNDLE / 'generate.py'), '--directory', str(self.root), *args],
            capture_output=True,
            text=True,
        )

    def test_preferences_survive_regeneration_and_configuration_copy(self):
        tuning = {
            'caches': {'global_factor': 1.0},
            'default_room_version': '12',
            'rc_message': {'per_second': 0.2, 'burst_count': 10},
            'trusted_key_servers': [{'server_name': 'keys.example.net'}],
        }
        (self.root / 'config/synapse-tuning.yaml').write_text(yaml.safe_dump(tuning))
        for _ in range(2):
            result = self.generate('--preview')
            self.assertEqual(result.returncode, 0, result.stderr)
            config = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
            self.assertEqual(config['server_name'], 'example.org')
            self.assertEqual(config['database']['args']['host'], 'postgres')
            self.assertEqual(config['app_service_config_files'], ['/config/rtc-appservice.yaml'])
            for key, value in tuning.items():
                self.assertEqual(config[key], value)
        # Reusing the persisted configuration in another directory is the generation
        # part of restore. It must not depend on the old absolute installation path.
        import shutil

        copied = self.root.with_name('copied')
        shutil.copytree(self.root, copied)
        self.root = copied
        result = self.generate('--preview')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = yaml.safe_load((copied / 'config/homeserver.yaml').read_text())
        self.assertEqual(config['caches'], tuning['caches'])

    def test_invalid_preferences_fail_before_runtime_files_are_replaced(self):
        result = self.generate('--preview')
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = ['config/homeserver.yaml', 'config/mas.yaml', 'compose.yaml', 'images.lock.json']
        before = {name: (self.root / name).read_bytes() for name in paths}
        for value in (
            {'server_name': 'other.example.org'},
            {'database': {'args': {'host': 'other.example.org'}}},
            {'matrix_authentication_service': {'enabled': False}},
            {'experimental_features': {'msc4512_enabled': False}},
            {'enable_registration': True},
            [],
            {'caches': 'x' * 65536},
        ):
            with self.subTest(value_type=type(value).__name__):
                (self.root / 'config/synapse-tuning.yaml').write_text(yaml.safe_dump(value))
                result = self.generate('--preview')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Invalid config/synapse-tuning.yaml', result.stderr)
                self.assertEqual({name: (self.root / name).read_bytes() for name in paths}, before)
        path = self.root / 'config/synapse-tuning.yaml'
        path.unlink()
        path.symlink_to(self.root / 'config/homeserver.yaml')
        self.assertNotEqual(self.generate('--preview').returncode, 0)

    def test_existing_mas_branding_and_password_schemes_are_retained(self):
        # Production generation reads saved MAS defaults. Host privilege changes
        # are bypassed while real key initialization and rendering are exercised.
        self.root = self.root.with_name('production')
        result = self.generate('--domain', 'example.org', '--initialize-env')
        self.assertEqual(result.returncode, 0, result.stderr)
        defaults = {
            'branding': {'service_name': 'Example community'},
            'passwords': {'schemes': [{'version': 1, 'algorithm': 'argon2id'}]},
        }
        (self.root / 'config/mas-defaults.yaml').write_text(yaml.safe_dump(defaults))
        before = {p.name: p.read_bytes() for p in (self.root / 'secrets').iterdir()}
        code = """
import os, runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import configuration, state_store
state_store.require_root = lambda *args: None
os.chown = lambda *args: None
bundle, root = sys.argv[1:]
configuration.patch(Path(root), {'PUBLIC_IP': '8.8.8.8'})
sys.argv = ['generate.py', '--directory', root]
runpy.run_path(str(Path(bundle) / 'generate.py'), run_name='__main__')
"""
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, '-c', code, str(BUNDLE), str(self.root)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = yaml.safe_load((self.root / 'config/mas.yaml').read_text())
            self.assertEqual(config['branding'], defaults['branding'])
            self.assertEqual(config['passwords']['schemes'], defaults['passwords']['schemes'])
            self.assertEqual(
                {p.name: p.read_bytes() for p in (self.root / 'secrets').iterdir()}, before
            )
            metadata = json.loads((self.root / 'installation.json').read_text())
            self.assertEqual(metadata['domain'], 'example.org')


if __name__ == '__main__':
    unittest.main()
