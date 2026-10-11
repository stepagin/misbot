"""Check automation stops on failures and verifies actual archive contents."""

import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

spec = importlib.util.spec_from_file_location(
    'ci_check', Path(__file__).resolve().parents[1] / 'check.py'
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'installer').mkdir()
        self.archive = self.root / 'matrix-installer.tar.gz'
        self.payloads = {
            'LICENSE': b'Fictional MIT notice\n',
            'README.md': b'Fictional public source\n',
        }
        manifest = ''.join(
            hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
            for name, data in self.payloads.items()
        ).encode()
        (self.root / 'installer/SHA256SUMS').write_bytes(manifest)
        self.payloads['SHA256SUMS'] = manifest

    def write_archive(self, payloads=None, extra=None):
        with tarfile.open(self.archive, 'w:gz') as package:
            for name, data in (payloads or self.payloads).items():
                member = tarfile.TarInfo('matrix-installer/' + name)
                member.size = len(data)
                package.addfile(member, io.BytesIO(data))
            if extra:
                package.addfile(extra)
        self.archive.with_name(self.archive.name + '.sha256').write_text(
            hashlib.sha256(self.archive.read_bytes()).hexdigest() + '  ' + self.archive.name + '\n'
        )

    def test_archive_with_matching_checksums_and_manifest_passes(self):
        self.write_archive()
        with patch.object(check, 'ROOT', self.root):
            self.assertEqual(
                check.verify_archive(self.archive),
                hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            )

    def test_wrong_outer_checksum_or_filename_is_rejected(self):
        self.write_archive()
        path = self.archive.with_name(self.archive.name + '.sha256')
        for value in (
            '0' * 64 + '  ' + self.archive.name + '\n',
            check.digest(self.archive) + '  other.tar.gz\n',
        ):
            with self.subTest(checksum=value), patch.object(check, 'ROOT', self.root):
                path.write_text(value)
                with self.assertRaisesRegex(check.CheckError, 'checksum'):
                    check.verify_archive(self.archive)

    def test_changed_content_with_valid_outer_checksum_is_rejected(self):
        changed = dict(self.payloads, **{'README.md': b'Unexpected source\n'})
        self.write_archive(changed)
        with patch.object(check, 'ROOT', self.root):
            with self.assertRaisesRegex(check.CheckError, 'contents'):
                check.verify_archive(self.archive)

    def test_changed_embedded_manifest_is_rejected(self):
        self.write_archive(dict(self.payloads, SHA256SUMS=b'Unexpected manifest\n'))
        with patch.object(check, 'ROOT', self.root):
            with self.assertRaisesRegex(check.CheckError, 'contents'):
                check.verify_archive(self.archive)

    def test_missing_extra_duplicate_and_linked_files_are_rejected(self):
        extra = tarfile.TarInfo('matrix-installer/unexpected.txt')
        duplicate = tarfile.TarInfo('matrix-installer/LICENSE')
        link = tarfile.TarInfo('matrix-installer/LICENSE')
        link.type = tarfile.SYMTYPE
        link.linkname = '../outside'
        missing = {k: v for k, v in self.payloads.items() if k != 'LICENSE'}
        for payloads, item in (
            (missing, None),
            (self.payloads, extra),
            (self.payloads, duplicate),
            (missing, link),
        ):
            with (
                self.subTest(extra=item.name if item else 'missing'),
                patch.object(check, 'ROOT', self.root),
            ):
                self.write_archive(payloads, item)
                with self.assertRaisesRegex(check.CheckError, 'files'):
                    check.verify_archive(self.archive)

    def test_commands_use_repository_directory_instead_of_callers_directory(self):
        with patch.object(check, 'ROOT', self.root), patch('sys.stdout', new=io.StringIO()):
            check.run(
                'working directory',
                [
                    sys.executable,
                    '-c',
                    'from pathlib import Path; Path("cwd.txt").write_text(str(Path.cwd()))',
                ],
            )
        self.assertEqual((self.root / 'cwd.txt').read_text(), str(self.root))

    def test_command_failure_stops_before_tests_and_build_and_preserves_exit_code(self):
        for code in (3, -2):
            with (
                self.subTest(returncode=code),
                patch.object(
                    check,
                    'quality_checks',
                    side_effect=subprocess.CalledProcessError(code, ['fictional-check']),
                ),
                patch.object(check, 'test_checks') as tests,
                patch.object(check, 'build_checks') as build,
                patch('sys.stderr', new=io.StringIO()),
            ):
                self.assertEqual(check.main(['all']), 130 if code == -2 else code)
                tests.assert_not_called()
                build.assert_not_called()

    def test_stale_manifest_stops_before_tests_can_rebuild_it(self):
        with (
            patch.object(
                check, 'run', side_effect=subprocess.CalledProcessError(1, ['fictional-build'])
            ),
            patch.object(check, 'quality_checks') as quality,
            patch.object(check, 'test_checks') as tests,
            patch.object(check, 'build_checks') as build,
            patch('sys.stderr', new=io.StringIO()),
        ):
            self.assertEqual(check.main(['all', '--check-manifest']), 1)
        quality.assert_not_called()
        tests.assert_not_called()
        build.assert_not_called()

    def test_interrupt_stops_before_tests_and_build(self):
        with (
            patch.object(check, 'quality_checks', side_effect=KeyboardInterrupt),
            patch.object(check, 'test_checks') as tests,
            patch.object(check, 'build_checks') as build,
            patch('sys.stderr', new=io.StringIO()),
        ):
            self.assertEqual(check.main(['all']), 130)
        tests.assert_not_called()
        build.assert_not_called()

    def test_unexpected_inputs_do_not_run_checks(self):
        for args in (['unknown'], ['tests', '--check-manifest']):
            with (
                self.subTest(args=args),
                patch.object(check, 'run') as run,
                patch('sys.stderr', new=io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as exit_code:
                    check.main(args)
                self.assertEqual(exit_code.exception.code, 2)
                run.assert_not_called()

    def test_different_second_build_is_rejected(self):
        with (
            patch.object(check, 'run'),
            patch.object(check, 'verify_archive', side_effect=['a' * 64, 'b' * 64]),
        ):
            with self.assertRaisesRegex(check.CheckError, 'different archives'):
                check.build_checks()

    def test_ci_uses_shared_checked_pipeline_for_pull_requests_and_releases(self):
        for name in ('quality', 'release'):
            workflow = yaml.safe_load(
                (check.ROOT / '.github/workflows' / (name + '.yml')).read_text()
            )
            if name == 'quality':
                self.assertIn('pull_request', workflow.get('on', workflow.get(True)))
            job = workflow['jobs']['check' if name == 'quality' else 'build']
            self.assertIn(
                'python ci/check.py all --check-manifest',
                [step.get('run') for step in job['steps']],
            )


if __name__ == '__main__':
    unittest.main()
