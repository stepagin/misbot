"""Bootstrap checks use fictional release archives and never call apt/network."""

from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import shlex
import select
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load('installer_bootstrap_test', ROOT / 'installer/bootstrap.py')
builder = load('ci_bootstrap_test', ROOT / 'ci/bootstrap.py')


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / bootstrap.ARCHIVE
        self.checksum = self.root / (bootstrap.ARCHIVE + '.sha256')
        self.files = {
            'misbot.py': b'import sys\nraise SystemExit(17)\n',
            'release.json': json.dumps({'family': bootstrap.FAMILY, 'version': '1.2.0'}).encode(),
        }
        self.files['SHA256SUMS'] = self.manifest(self.files)
        self.metadata = {
            'tag_name': 'v1.2.0',
            'draft': False,
            'prerelease': False,
            'assets': [{'name': bootstrap.ARCHIVE}, {'name': bootstrap.ARCHIVE + '.sha256'}],
        }
        self.urls = []

    def manifest(self, files):
        return ''.join(
            hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in files.items()
        ).encode()

    def archive_write(self, files=None, extra=None):
        with tarfile.open(self.archive, 'w:gz') as package:
            for name, data in (files or self.files).items():
                member = tarfile.TarInfo('matrix-installer/' + name)
                member.size = len(data)
                package.addfile(member, io.BytesIO(data))
            if extra:
                package.addfile(extra)
        self.checksum.write_text(
            hashlib.sha256(self.archive.read_bytes()).hexdigest() + '  ' + bootstrap.ARCHIVE + '\n'
        )

    def fake_download(self, url, target, allow_missing=False):
        self.urls.append(url)
        if '/releases/latest' in url or '/releases/tags/' in url:
            target.write_text(json.dumps(self.metadata))
        else:
            target.write_bytes(
                (self.checksum if url.endswith('.sha256') else self.archive).read_bytes()
            )
        return True

    def main_context(self):
        context = ExitStack()
        self.addCleanup(context.close)
        context.enter_context(patch.object(bootstrap, 'BASE', self.root / 'state'))
        context.enter_context(patch.object(bootstrap, 'existing_command', return_value=False))
        context.enter_context(patch.object(bootstrap, 'protected'))
        context.enter_context(patch.object(bootstrap, 'prepare_packages'))
        context.enter_context(patch.object(bootstrap, 'download', side_effect=self.fake_download))
        context.enter_context(patch.object(bootstrap.os, 'geteuid', return_value=0))
        context.enter_context(patch.object(bootstrap.sys.stdin, 'isatty', return_value=True))
        context.enter_context(patch.object(bootstrap.sys.stdout, 'isatty', return_value=True))
        context.enter_context(patch.object(bootstrap.os, 'umask'))
        context.enter_context(patch.dict(os.environ))
        return context

    def test_latest_verified_bundle_reused_and_ui_exit_preserved(self):
        self.archive_write()
        self.main_context()
        self.assertEqual(bootstrap.main([]), 17)
        self.assertEqual(bootstrap.main([]), 17)
        source = self.root / 'state/1.2.0/matrix-installer'
        self.assertEqual((source / 'misbot.py').read_bytes(), self.files['misbot.py'])
        self.assertFalse(list((self.root / 'state').glob('.download-*')))
        self.assertTrue(all('/v1.2.0/' in url for url in self.urls if '/download/' in url))

    def test_network_failure_cleans_temp_and_never_launches_ui(self):
        self.main_context()
        with (
            patch.object(bootstrap, 'download', side_effect=RuntimeError('offline')),
            patch.object(bootstrap.subprocess, 'run') as run,
        ):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                bootstrap.main([])
            run.assert_not_called()
        self.assertFalse(list((self.root / 'state').glob('.download-*')))

    def test_existing_install_opens_ui_without_packages_download_or_version_change(self):
        self.main_context()
        with (
            patch.object(bootstrap, 'existing_command', return_value=True),
            patch.object(bootstrap.subprocess, 'run', return_value=Mock(returncode=23)) as run,
        ):
            self.assertEqual(bootstrap.main(['--version', '1.2.0']), 23)
            run.assert_called_once_with([str(bootstrap.COMMAND), 'ui'], check=False)
        bootstrap.prepare_packages.assert_not_called()
        bootstrap.download.assert_not_called()
        self.assertFalse((self.root / 'state').exists())

    def test_modified_retained_bundle_is_not_overwritten(self):
        self.archive_write()
        self.main_context()
        bootstrap.main([])
        source = self.root / 'state/1.2.0/matrix-installer/misbot.py'
        source.write_text('preserve my changes')
        with self.assertRaisesRegex(RuntimeError, 'изменён'):
            bootstrap.main([])
        self.assertEqual(source.read_text(), 'preserve my changes')

    def test_wrong_outer_checksum_rejected(self):
        self.archive_write()
        self.checksum.write_text('0' * 64 + '  ' + bootstrap.ARCHIVE + '\n')
        with self.assertRaisesRegex(RuntimeError, 'Контрольная сумма'):
            bootstrap.archive_files(self.archive, self.checksum, 'v1.2.0')

    def test_changed_or_unlisted_content_and_manifest_duplicates_rejected(self):
        for files in (
            dict(self.files, **{'misbot.py': b'unexpected'}),
            dict(self.files, extra=b'unlisted'),
            dict(self.files, SHA256SUMS=self.files['SHA256SUMS'] * 2),
        ):
            with self.subTest(files=files):
                self.archive_write(files)
                with self.assertRaises(RuntimeError):
                    bootstrap.archive_files(self.archive, self.checksum, 'v1.2.0')

    def test_wrong_release_family_or_version_rejected(self):
        for release in (
            {'family': bootstrap.FAMILY, 'version': '2.0.0'},
            {'family': 'other', 'version': '1.2.0'},
        ):
            files = dict(self.files, **{'release.json': json.dumps(release).encode()})
            del files['SHA256SUMS']
            files['SHA256SUMS'] = self.manifest(files)
            self.archive_write(files)
            with self.assertRaisesRegex(RuntimeError, 'версии'):
                bootstrap.archive_files(self.archive, self.checksum, 'v1.2.0')

    def test_traversal_links_devices_duplicates_and_abnormal_paths_rejected(self):
        for name, kind in (
            ('matrix-installer/../../outside', tarfile.REGTYPE),
            ('/matrix-installer/absolute', tarfile.REGTYPE),
            ('matrix-installer/./dot', tarfile.REGTYPE),
            ('matrix-installer/link', tarfile.SYMTYPE),
            ('matrix-installer/hardlink', tarfile.LNKTYPE),
            ('matrix-installer/device', tarfile.CHRTYPE),
            ('matrix-installer/misbot.py', tarfile.REGTYPE),
            ('matrix-installer/back\\slash', tarfile.REGTYPE),
        ):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/outside'
                self.archive_write(extra=member)
                with self.assertRaisesRegex(RuntimeError, 'небезопасные'):
                    bootstrap.archive_files(self.archive, self.checksum, 'v1.2.0')
        self.assertFalse((self.root / 'outside').exists())

    def test_explicit_version_falls_back_only_on_missing_tag(self):
        def download(url, path, allow_missing=False):
            self.urls.append(url)
            if url.endswith('/1.2.0'):
                return False
            path.write_text(json.dumps(self.metadata))
            return True

        with patch.object(bootstrap, 'download', side_effect=download):
            self.assertEqual(bootstrap.select_release('1.2.0', self.root), 'v1.2.0')
        self.assertEqual(self.urls, [bootstrap.API + '/tags/1.2.0', bootstrap.API + '/tags/v1.2.0'])

    def test_invalid_release_metadata_rejected(self):
        for values in (
            {'tag_name': '../../evil'},
            {'tag_name': 'v2.0.0'},
            {'draft': True},
            {'prerelease': True},
            {'assets': []},
        ):
            with self.subTest(values=values):
                self.metadata.update(values)
                with patch.object(bootstrap, 'download', side_effect=self.fake_download):
                    with self.assertRaises(RuntimeError):
                        bootstrap.select_release('1.2.0', self.root)
                self.setUp_metadata()

    def setUp_metadata(self):
        self.metadata = {
            'tag_name': 'v1.2.0',
            'draft': False,
            'prerelease': False,
            'assets': [{'name': bootstrap.ARCHIVE}, {'name': bootstrap.ARCHIVE + '.sha256'}],
        }

    def test_curl_is_https_only_without_user_config_and_404_is_not_success(self):
        with patch.object(
            bootstrap.subprocess, 'run', return_value=Mock(returncode=22, stdout='404')
        ) as run:
            self.assertFalse(
                bootstrap.download(bootstrap.API + '/tags/1.2.0', self.root / 'x', True)
            )
            args = run.call_args.args[0]
            self.assertEqual(args[:2], ['/usr/bin/curl', '-q'])
            self.assertEqual(args[args.index('--proto') + 1], '=https')
            self.assertEqual(args[args.index('--proto-redir') + 1], '=https')
            with self.assertRaises(RuntimeError):
                bootstrap.download(bootstrap.API + '/latest', self.root / 'x')

    def test_package_failure_stops_before_release_download(self):
        self.main_context()
        with patch.object(
            bootstrap,
            'prepare_packages',
            side_effect=subprocess.CalledProcessError(100, ['apt-get']),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.main([])
        bootstrap.download.assert_not_called()

    def test_only_fixed_missing_packages_and_bounded_commands(self):
        with (
            patch.object(bootstrap.shutil, 'which', return_value=None),
            patch.object(bootstrap.importlib.util, 'find_spec', return_value=None),
            patch.object(bootstrap.subprocess, 'run') as run,
        ):
            bootstrap.prepare_packages()
        self.assertEqual(len(run.call_args_list), 2)
        self.assertEqual(run.call_args_list[0].kwargs['timeout'], 600)
        self.assertEqual(run.call_args_list[1].kwargs['timeout'], 900)
        self.assertIn('python3-yaml', run.call_args_list[1].args[0])
        self.assertIn('python3-rich', run.call_args_list[1].args[0])

    def test_protected_rejects_symlink_and_writable_ancestor(self):
        file = self.root / 'file'
        file.write_text('safe')
        link = self.root / 'link'
        link.symlink_to(file)
        for path in (file, link):
            with self.assertRaises(RuntimeError):
                bootstrap.protected(path)

    def test_foreign_or_corrupt_existing_helper_is_never_executed(self):
        library = self.root / 'lib'
        library.mkdir()
        command = self.root / 'misbot'
        command.write_text('command')
        (library / 'command_wrapper.py').write_text('raise AssertionError("must not execute")')
        with (
            patch.object(bootstrap, 'COMMAND', command),
            patch.object(bootstrap, 'LIBRARY', library),
            patch.object(bootstrap, 'protected'),
        ):
            for record in (
                {'family': 'foreign'},
                {
                    'family': bootstrap.FAMILY,
                    'format': 1,
                    'hashes': {'command_wrapper.py': '0' * 64},
                },
            ):
                (library / 'command.json').write_text(json.dumps(record))
                with self.assertRaises(RuntimeError):
                    bootstrap.existing_command()

    def test_generated_asset_help_and_invalid_arguments_are_safe_without_terminal(self):
        script = self.root / 'install.sh'
        script.write_text(builder.render())
        subprocess.run(['bash', '-n', script], check=True)
        result = subprocess.run(['bash', script, '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--version', result.stdout)
        result = subprocess.run(
            ['bash', script, '--version', '../../x'], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        result = subprocess.run(['bash', script], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def generated_asset(self, terminate=False, hangup=False):
        self.files['misbot.py'] = (
            b'import sys\nassert sys.stdin.isatty() and sys.stdout.isatty()\nprint("WIZARD_TERMINAL_OK", flush=True)\nraise SystemExit(17)\n'
        )
        del self.files['SHA256SUMS']
        self.files['SHA256SUMS'] = self.manifest(self.files)
        self.archive_write()
        original = (ROOT / 'installer/bootstrap.py').read_text()
        # No production test bypass: inject mocks only into this temporary copy.
        harness = f"""\nBASE = Path({str(self.root / 'state')!r})
os.geteuid = lambda: 0
protected = lambda *args, **kwargs: None
existing_command = lambda: False
prepare_packages = lambda: None
def download(url, target, allow_missing=False):
    if "/releases/latest" in url:
        target.write_text({json.dumps(self.metadata)!r})
    else:
        source = Path({str(self.root)!r}) / (ARCHIVE + (".sha256" if url.endswith(".sha256") else ""))
        target.write_bytes(source.read_bytes())
    return True
"""
        if terminate:
            harness = harness.replace(
                '    if "/releases/latest" in url:',
                '    target.write_text("partial")\n    os.kill(os.getpid(), signal.SIGTERM)\n    if "/releases/latest" in url:',
            )
        if hangup:
            harness = harness.replace('signal.SIGTERM', 'signal.SIGHUP')
        modified = original.replace(
            "if __name__ == '__main__':", harness + "\nif __name__ == '__main__':"
        )
        script = self.root / 'install.sh'
        assembled = builder.render().replace(shlex.quote(original), shlex.quote(modified))
        # Root check is bypassed solely in isolated script; OS/TTY checks remain real.
        script.write_text(assembled.replace('if [[ $EUID != 0 ]]; then', 'if false; then'))
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(['bash', script], stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = bytearray()
            deadline = time.monotonic() + 10
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                    self.fail('Generated bootstrap did not finish within 10 seconds')
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            self.assertEqual(
                process.wait(timeout=10),
                (129 if hangup else 143) if terminate else 17,
                output.decode(),
            )
            if not terminate:
                self.assertIn(b'WIZARD_TERMINAL_OK', output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            os.close(master)
        self.assertFalse(list((self.root / 'state').glob('.download-*')))

    def test_shell_missing_python_apt_failure_stops_before_runtime(self):
        script = self.root / 'install.sh'
        failure = self.root / 'apt-failure'
        log = self.root / 'apt.log'
        failure.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" > ' + shlex.quote(str(log)) + '\nexit 100\n'
        )
        failure.chmod(0o700)
        shell = builder.render().replace('if [[ $EUID != 0 ]]; then', 'if false; then')
        shell = shell.replace('if [[ ! -t 0 || ! -t 1 ]]; then', 'if false; then')
        shell = shell.replace('if [[ ! -x /usr/bin/python3 ]]; then', 'if true; then')
        shell = shell.replace('/usr/bin/timeout', shlex.quote(str(failure)))
        script.write_text(shell)
        result = subprocess.run(['bash', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 100)
        self.assertIn('update -qq', log.read_text())
        self.assertNotIn('install -y', log.read_text())
        self.assertNotIn('Misbot:', result.stderr)

    def test_bootstrap_asset_is_deterministic_and_contains_current_sources(self):
        installer = self.root / 'installer'
        installer.mkdir()
        for name in ('bootstrap.py', 'bootstrap.sh'):
            (installer / name).write_bytes((ROOT / 'installer' / name).read_bytes())
        with patch.object(builder, 'ROOT', self.root):
            builder.build()
            first = (self.root / 'dist/install.sh').read_bytes()
            builder.build()
            self.assertEqual((self.root / 'dist/install.sh').read_bytes(), first)
            self.assertEqual(first.decode(), builder.render())
        self.assertIn(shlex.quote((installer / 'bootstrap.py').read_text()), first.decode())

    def test_generated_asset_preserves_real_pty_and_exit_code_with_isolated_downloads(self):
        self.generated_asset()

    def test_generated_asset_sigterm_cleans_partial_download(self):
        self.generated_asset(terminate=True)
        self.assertFalse((self.root / 'state/1.2.0').exists())

    def test_generated_asset_ssh_hangup_cleans_partial_download(self):
        self.generated_asset(terminate=True, hangup=True)
        self.assertFalse((self.root / 'state/1.2.0').exists())

    def test_protected_trust_checks_without_world_writable_temp_parent(self):
        path = Path('/owned/file')
        directory = Mock(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_nlink=2)
        safe = Mock(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)

        def records(item):
            return safe if item == path else directory

        with patch.object(Path, 'lstat', autospec=True, side_effect=records):
            bootstrap.protected(path)
            for mode, uid, links in (
                (stat.S_IFREG | 0o622, 0, 1),
                (stat.S_IFREG | 0o600, 1000, 1),
                (stat.S_IFLNK | 0o777, 0, 1),
                (stat.S_IFREG | 0o600, 0, 2),
            ):
                safe.st_mode, safe.st_uid, safe.st_nlink = mode, uid, links
                with self.assertRaises(RuntimeError):
                    bootstrap.protected(path)

    def test_nonobject_release_and_malformed_asset_metadata_are_safe_errors(self):
        for metadata in (
            [],
            None,
            dict(self.metadata, assets=[None]),
            dict(self.metadata, assets={}),
        ):
            self.metadata = metadata
            with patch.object(bootstrap, 'download', side_effect=self.fake_download):
                with self.assertRaises(RuntimeError):
                    bootstrap.select_release(None, self.root)
            self.setUp_metadata()


if __name__ == '__main__':
    unittest.main()
