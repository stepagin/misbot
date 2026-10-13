"""Local build numbers stay out of source metadata and stable CI artifacts."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LocalBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copyfile(ROOT / 'LICENSE', self.root / 'LICENSE')
        for name in ('installer', 'bots'):
            shutil.copytree(
                ROOT / name,
                self.root / name,
                ignore=shutil.ignore_patterns('__pycache__', 'node_modules', 'tests'),
            )
        self.env = dict(os.environ, CI='', GITHUB_ACTIONS='')
        self.version = json.loads((self.root / 'installer/release.json').read_text())['version']
        self.build('--release')
        self.original = {
            name: (self.root / 'installer' / name).read_bytes()
            for name in ('release.json', 'SHA256SUMS')
        }

    def build(self, *args, env=None, ok=True):
        result = subprocess.run(
            [sys.executable, 'installer/build.py', *args],
            cwd=self.root,
            env=env or self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def assert_source_unchanged(self):
        for name, content in self.original.items():
            self.assertEqual((self.root / 'installer' / name).read_bytes(), content)

    def assert_archives(self, version, directory=None):
        directory = directory or self.root / 'dist'
        for filename, prefix in (
            ('matrix-installer.tar.gz', 'matrix-installer/'),
            ('misbot-setup-' + version + '.tar.gz', 'matrix-installer/.misbot/'),
        ):
            path = directory / filename
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.with_name(filename + '.sha256').read_text().split()[0],
            )
            with tarfile.open(path) as archive:
                files = {member.name: archive.extractfile(member).read() for member in archive}
            self.assertEqual(json.loads(files[prefix + 'release.json'])['version'], version)
            for name, content in files.items():
                if name.endswith('/SHA256SUMS'):
                    base = name.removesuffix('SHA256SUMS')
                    for line in content.decode().splitlines():
                        checksum, relative = line.split('  ', 1)
                        self.assertEqual(
                            hashlib.sha256(files[base + relative]).hexdigest(), checksum
                        )
            self.assertFalse(any('.local-build' in name for name in files))
            self.assertFalse(any('/ci/' in name for name in files))
        return files

    def test_local_builds_increment_only_archive_metadata_across_output_directories(self):
        self.build()
        self.assert_archives(self.version + '-build.1')
        output = self.root / 'custom/matrix-installer.tar.gz'
        self.build('--output', str(output))
        files = self.assert_archives(self.version + '-build.2', output.parent)
        self.assertIn(self.version + '-build.2', files['matrix-installer/README.md'].decode())
        self.assert_source_unchanged()
        self.build('--check')
        self.assertEqual(
            json.loads((self.root / 'dist/.local-build.json').read_text())[self.version], 2
        )

    def test_release_and_ci_builds_are_stable_reproducible_and_do_not_increment(self):
        self.build()
        counter = (self.root / 'dist/.local-build.json').read_bytes()
        self.build('--release')
        self.assert_archives(self.version)
        expected = (self.root / 'dist/matrix-installer.tar.gz').read_bytes()
        compact = (self.root / ('dist/misbot-setup-' + self.version + '.tar.gz')).read_bytes()
        for marker in ('CI', 'GITHUB_ACTIONS'):
            with self.subTest(marker=marker):
                self.build(env=dict(self.env, **{marker: 'true'}))
                self.assert_archives(self.version)
                self.assertEqual(
                    (self.root / 'dist/matrix-installer.tar.gz').read_bytes(), expected
                )
                self.assertEqual(
                    (self.root / ('dist/misbot-setup-' + self.version + '.tar.gz')).read_bytes(),
                    compact,
                )
                self.assertEqual((self.root / 'dist/.local-build.json').read_bytes(), counter)
        self.assert_source_unchanged()

    def test_check_does_not_create_counter_or_build_archives(self):
        archive = self.root / 'dist/matrix-installer.tar.gz'
        original = archive.read_bytes()
        self.build('--check')
        self.assertFalse((self.root / 'dist/.local-build.json').exists())
        self.assertEqual(archive.read_bytes(), original)
        self.assert_source_unchanged()

    def test_existing_archive_numbers_are_preserved(self):
        (self.root / ('dist/misbot-setup-' + self.version + '-build.9.tar.gz')).touch()
        self.build()
        self.assert_archives(self.version + '-build.10')

    def test_parallel_build_is_refused_without_removing_another_build_lock(self):
        lock = self.root / 'dist/.local-build.lock'
        lock.write_text('another build\n')
        result = self.build(ok=False)
        self.assertIn('Local build is locked', result.stderr)
        self.assertEqual(lock.read_text(), 'another build\n')
        self.assertFalse((self.root / 'dist/.local-build.json').exists())

    def test_invalid_counter_is_not_reset(self):
        state = self.root / 'dist/.local-build.json'
        state.write_text(json.dumps({self.version: -1}))
        result = self.build(ok=False)
        self.assertIn('Invalid local build counter', result.stderr)
        self.assertEqual(json.loads(state.read_text()), {self.version: -1})
        self.assertFalse((self.root / 'dist/.local-build.lock').exists())


if __name__ == '__main__':
    unittest.main()
