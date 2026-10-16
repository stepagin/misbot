"""Local bundle updates find their target without bypassing backend safeguards."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import command_wrapper
import misbot

BUNDLE = Path(__file__).resolve().parents[1]


class LocalUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Custom Stack'
        self.root.mkdir(mode=0o700)
        for name in ('install.sh', 'generate.py', 'lifecycle.py', 'existing_compose.py'):
            (self.root / name).write_text('# fictional test source\n')
        (self.root / 'release.json').write_text(json.dumps({'family': command_wrapper.FAMILY}))
        self.metadata()
        self.source = self.base / 'New Bundle'
        self.source.mkdir(mode=0o700)
        for name in (*command_wrapper.PUBLIC_FILES, 'install.sh', 'existing_update.py'):
            shutil.copyfile(BUNDLE / name, self.source / name)
        self.system = self.base / 'system'
        self.system.mkdir(mode=0o700)
        self.original_regular = command_wrapper.regular
        self.original_paths = command_wrapper.paths
        self.addCleanup(patch.stopall)
        patch.object(
            command_wrapper,
            'paths',
            side_effect=lambda system=Path('/'): self.original_paths(self.system),
        ).start()
        patch.object(command_wrapper, 'default_directory', return_value=None).start()
        patch.object(misbot, 'DEFAULT_UPDATE_DIRECTORY', self.base / 'conventional').start()
        patch.object(sys.stdin, 'isatty', return_value=False).start()
        patch.object(command_wrapper, 'regular', side_effect=self.source_owner).start()

    def metadata(self, backend='portable'):
        (self.root / 'installation.json').write_text(
            json.dumps({'domain': 'example.org', 'backend': backend})
        )

    def source_owner(self, path):
        info = self.original_regular(path)
        if Path(path).is_relative_to(self.source):
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)
        return info

    def plan(self, argv=None, source=None):
        command, options, values = misbot.parse(['update'] if argv is None else argv)
        return misbot.plan(command, options, values, source=source or self.source)

    def register(self):
        command_wrapper.install(self.root, source=BUNDLE, system=self.system)

    def test_saved_custom_target_and_new_source_for_both_backends(self):
        for backend in ('portable', 'existing-compose'):
            with self.subTest(backend=backend):
                self.metadata(backend)
                self.register()
                args = self.plan()
                self.assertIn(str(self.root), args)
                self.assertEqual(Path(args[1]).parent, self.source)
                self.assertEqual(
                    Path(args[1]).name,
                    'install.sh' if backend == 'portable' else 'existing_update.py',
                )
                self.assertIn('update', args)

    def test_valid_conventional_target_without_record(self):
        with patch.object(misbot, 'DEFAULT_UPDATE_DIRECTORY', self.root):
            self.assertIn(str(self.root), self.plan())

    def test_explicit_target_overrides_saved_target(self):
        with patch.object(
            command_wrapper, 'record', side_effect=AssertionError('must not read default')
        ):
            self.assertIn(str(self.root), self.plan(['update', '--directory', str(self.root)]))

    def test_installation_local_target_precedes_system_default(self):
        with patch.object(
            command_wrapper, 'record', side_effect=AssertionError('must not read default')
        ):
            args = self.plan(['update', '--source', str(self.source)], source=self.root)
            self.assertIn(str(self.root), args)

    def test_missing_target_does_not_create_installation_or_fetch_release(self):
        with self.assertRaises(misbot.InputError) as error:
            self.plan()
        self.assertIn('--directory', str(error.exception))
        self.assertIn(str(self.source / 'misbot.py'), str(error.exception))
        self.assertNotIn('init', str(error.exception))
        self.assertFalse(misbot.DEFAULT_UPDATE_DIRECTORY.exists())

    def test_stale_or_untrusted_record_never_falls_back(self):
        for record in ({'directory': str(self.base / 'missing')}, [], {'directory': None}):
            with (
                self.subTest(record=record),
                patch.object(command_wrapper, 'record', return_value=record),
                patch.object(misbot, 'DEFAULT_UPDATE_DIRECTORY', self.root),
            ):
                with self.assertRaises(misbot.InputError):
                    self.plan()
        with patch.object(command_wrapper, 'record', side_effect=AttributeError('bad record')):
            with self.assertRaises(misbot.InputError):
                self.plan()

    def test_record_validation_rejects_changed_hashes(self):
        self.register()
        _, lib = self.original_paths(self.system)
        (lib / 'misbot.py').write_text('# modified outside registration\n')
        with patch.object(misbot, 'DEFAULT_UPDATE_DIRECTORY', self.root):
            with self.assertRaises(misbot.InputError) as error:
                self.plan()
        self.assertIn('--directory', str(error.exception))

    def test_unsafe_target_and_source_paths_are_rejected(self):
        link = self.base / 'linked'
        link.symlink_to(self.root, target_is_directory=True)
        for root in ('relative', '/', str(link), str(self.root / '..' / self.root.name)):
            with self.subTest(root=root), self.assertRaises(misbot.InputError):
                self.plan(['update', '--directory', root])
        link.unlink()
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(misbot.InputError):
            self.plan(['update', '--directory', str(self.root), '--source', str(link)])

    def test_explicit_compact_outer_directory_and_recovery(self):
        compact = self.base / 'compact'
        compact.mkdir()
        shutil.move(self.source, compact / '.misbot')
        self.source = compact / '.misbot'
        for backend in ('portable', 'existing-compose'):
            self.metadata(backend)
            for command in (['update'], ['update', 'apply'], ['update', 'recover']):
                with self.subTest(backend=backend, command=command):
                    args = self.plan(
                        [*command, '--directory', str(self.root), '--source', str(compact)]
                    )
                    self.assertEqual(Path(args[1]).parent, self.source)
                    self.assertNotIn(str(compact), args)

    def test_symlink_hidden_source_is_rejected(self):
        compact = self.base / 'compact'
        compact.mkdir()
        (compact / '.misbot').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(misbot.InputError):
            self.plan(['update', '--directory', str(self.root), '--source', str(compact)])

    def test_installed_runtime_needs_a_new_source(self):
        with self.assertRaises(misbot.InputError) as error:
            self.plan(['update', '--directory', str(self.root)], source=self.root)
        self.assertIn('--source', str(error.exception))

    def test_recovery_hints_preserve_action_source_and_target(self):
        with self.assertRaises(misbot.InputError) as error:
            self.plan(['update', 'recover', '--directory', str(self.root)], source=self.root)
        self.assertIn('update recover --directory', str(error.exception))
        self.assertIn(str(self.root), str(error.exception))
        with self.assertRaises(misbot.InputError) as error:
            self.plan(['update', 'recover', '--source', str(self.source)])
        self.assertIn('update recover --directory', str(error.exception))
        self.assertIn('--source', str(error.exception))
        self.assertIn(str(self.source), str(error.exception))

    def test_missing_target_compact_hint_uses_public_launcher(self):
        compact = self.base / 'compact'
        compact.mkdir()
        hidden = compact / '.misbot'
        hidden.mkdir()
        with self.assertRaises(misbot.InputError) as error:
            self.plan(source=hidden)
        self.assertIn(str(compact / 'misbot.py'), str(error.exception))
        self.assertNotIn('.misbot', str(error.exception))

    def test_terminal_prompt_resolves_target_and_cancellation_is_clean(self):
        with (
            patch.object(sys.stdin, 'isatty', return_value=True),
            patch.object(sys.stdout, 'isatty', return_value=True),
        ):
            with patch('builtins.input', return_value=str(self.root)):
                self.assertIn(str(self.root), self.plan())
            for failure, expected in ((EOFError, 2), (KeyboardInterrupt, 130)):
                with (
                    patch('builtins.input', side_effect=failure),
                    patch.object(misbot, 'os', SimpleNamespace(geteuid=lambda: 0)),
                    patch.object(misbot, '__file__', str(self.source / 'misbot.py')),
                    contextlib.redirect_stderr(io.StringIO()) as output,
                ):
                    self.assertEqual(misbot.main(['update']), expected)
                    self.assertNotIn('Traceback', output.getvalue())

    def test_help_never_discovers_target_prompts_or_executes(self):
        for argv in (
            ['update', '--help'],
            ['update', 'help'],
            ['help', 'update'],
            ['update', 'apply', '--help'],
        ):
            with (
                self.subTest(argv=argv),
                patch.object(command_wrapper, 'record', side_effect=AssertionError('private read')),
                patch.object(misbot, 'plan', side_effect=AssertionError('mutation')),
                patch('builtins.input', side_effect=AssertionError('prompt')),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(misbot.main(argv), 0)

    def test_check_latest_and_explicit_actions_keep_their_routes(self):
        for action, backend in (
            ('check', 'release-check'),
            ('latest', 'release-update'),
            ('apply', 'update'),
            ('recover', 'update-recover'),
        ):
            with self.subTest(action=action):
                command, _, _ = misbot.parse(['update', action])
                self.assertEqual(command.backend, backend)
        for argv in (['update', '--unknown'], ['update', '--force'], ['update', 'unknown']):
            with self.assertRaises(misbot.InputError):
                misbot.parse(argv)

    def test_compact_launcher_subprocess_selects_new_bundle_and_saved_target(self):
        self.register()
        compact = self.base / 'compact'
        compact.mkdir()
        shutil.move(self.source, compact / '.misbot')
        self.source = compact / '.misbot'
        shutil.copyfile(BUNDLE / 'launch.py', compact / 'misbot.py')
        # Run the real compact launcher. Only process exec/root identity are faked;
        # record hashes and installation validation still read the real fixtures.
        script = """
import json, os, pathlib, runpy, sys
from types import SimpleNamespace
from unittest.mock import patch
source, system, launcher = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(source))
import command_wrapper as wrapper
uid = os.geteuid()
paths, record, trusted, regular = wrapper.paths, wrapper.record, wrapper.trusted_installation, wrapper.regular
wrapper.paths = lambda system_root=pathlib.Path('/'): paths(system)
def as_fixture_owner(function, *args):
    with patch.object(os, 'geteuid', return_value=uid):
        return function(*args)
wrapper.record = lambda: as_fixture_owner(record)
wrapper.trusted_installation = lambda root: as_fixture_owner(trusted, root)
def source_owner(path):
    info = regular(path)
    return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022) if pathlib.Path(path).is_relative_to(source) else info
wrapper.regular = source_owner
os.geteuid = lambda: 0
def capture(program, argv):
    print(json.dumps(argv))
    raise SystemExit(0)
os.execv = capture
sys.argv = [str(launcher), 'update']
runpy.run_path(str(launcher), run_name='__main__')
"""
        result = subprocess.run(
            [
                sys.executable,
                '-B',
                '-c',
                script,
                str(self.source),
                str(self.system),
                str(compact / 'misbot.py'),
            ],
            cwd=self.base,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(result.stdout)
        self.assertEqual(args[1], str(self.source / 'install.sh'))
        self.assertEqual(args[2:], ['update', str(self.root)])


if __name__ == '__main__':
    unittest.main()
