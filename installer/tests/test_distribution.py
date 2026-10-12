"""The compact entry point launches the same runtime and keeps update compatibility."""

import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DistributionTests(unittest.TestCase):
    def test_compact_launch_help_version_and_manifests(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'installer/build.py'),
                    '--output',
                    str(base / 'matrix-installer.tar.gz'),
                ],
                check=True,
                capture_output=True,
            )
            compact = base / 'misbot-setup-1.2.0.tar.gz'
            checksum = compact.with_name(compact.name + '.sha256').read_text().split()[0]
            self.assertEqual(hashlib.sha256(compact.read_bytes()).hexdigest(), checksum)
            with tarfile.open(compact) as archive:
                for item in archive.getmembers():
                    self.assertTrue(item.isfile())
                    self.assertNotIn('..', Path(item.name).parts)
                archive.extractall(base / 'expanded', filter='data')
            root = base / 'expanded/matrix-installer'
            self.assertEqual(
                {p.name for p in root.iterdir()},
                {'.misbot', 'misbot.py', 'README.md', 'SHA256SUMS'},
            )
            for manifest in (root / 'SHA256SUMS', root / '.misbot/SHA256SUMS'):
                for line in manifest.read_text().splitlines():
                    expected, name = line.split('  ', 1)
                    self.assertEqual(
                        hashlib.sha256((manifest.parent / name).read_bytes()).hexdigest(), expected
                    )
            for args, expected in (
                (['version'], 'Misbot 1.2.0'),
                (['help'], 'команды управления Matrix'),
            ):
                result = subprocess.run(
                    [sys.executable, str(root / 'misbot.py'), *args], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
            self.assertFalse(list(root.rglob('__pycache__')))
            self.assertTrue((root / '.misbot/LICENSE').is_file())
            self.assertTrue((root / '.misbot/third-party-licenses.md').is_file())
            with tarfile.open(base / 'matrix-installer.tar.gz') as legacy:
                names = set(legacy.getnames())
                self.assertIn('matrix-installer/install.sh', names)
                self.assertIn('matrix-installer/generate.py', names)
                self.assertIn('matrix-installer/update.py', names)
                self.assertNotIn('matrix-installer/.misbot/update.py', names)

    def test_missing_runtime_is_reported_without_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            entry = Path(folder) / 'misbot.py'
            entry.write_bytes((ROOT / 'installer/launch.py').read_bytes())
            result = subprocess.run(
                [sys.executable, str(entry), 'version'], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn('Комплект неполный', result.stderr)
            self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
