"""Every public command must reach an entry point that accepts its actual argv."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_commands
import command_wrapper
import misbot

BUNDLE = Path(__file__).resolve().parents[1]
VALUES = {
    'domain': 'example.org',
    'purpose': 'media',
    'calendar': '*-*-* 03:00:00 UTC',
    'sha256': '0' * 64,
    'public-ip': '203.0.113.20',
    'confirm': 'source-stopped',
    'tag': 'server-panel',
    'server': 'peer.example.org',
    'request': 'MB-0123456789abcdef0123456789abcdef',
    'object': 'backups/example.enc',
}

# Documented shell operations, verified against the actual backend parsers below.
SHELL_TARGETS = {
    'init': ('command_wrapper.py', 'install'),
    'generate': ('lifecycle.py', 'generate'),
    'preflight': ('preflight.py', None),
    'status': ('lifecycle.py', 'status'),
    'storage-status': ('lifecycle.py', 'status'),
    'check': ('lifecycle.py', 'check'),
    'up': ('lifecycle.py', 'up'),
    'stop': ('lifecycle.py', 'stop'),
    'user-password': ('user_credentials.py', 'reset'),
    'user-password-recover': ('user_credentials.py', 'recover'),
    'media-local': ('media.py', 'local'),
    'media-s3': ('media.py', 's3'),
    'media-recover': ('media.py', 'recover'),
    'database-external': ('database.py', 'external'),
    'database-recover': ('database.py', 'recover'),
    'database-abort': ('database.py', 'abort'),
    'backup': ('lifecycle.py', 'backup'),
    'backup-recover': ('lifecycle.py', 'backup-recover'),
    'restore-check': ('lifecycle.py', 'restore-check'),
    'backup-key': ('external_backup.py', 'key'),
    'backup-external': ('external_backup.py', 'export'),
    'backup-cleanup': ('external_backup.py', 'cleanup'),
    'backup-fetch': ('external_backup.py', 'fetch'),
    'restore': ('restore.py', None),
    'restore-local': ('restore.py', None),
    'restore-activate': ('lifecycle.py', 'restore-activate'),
    'move-export': ('lifecycle.py', 'move-export'),
    'move-abort': ('lifecycle.py', 'move-abort'),
    'backup-schedule-local': ('backup_schedule.py', 'configure'),
    'backup-schedule': ('backup_schedule.py', 'configure'),
    'backup-schedule-status': ('backup_schedule.py', 'status'),
    'backup-schedule-disable': ('backup_schedule.py', 'disable'),
    'update': ('update.py', 'apply'),
    'update-recover': ('update.py', 'recover'),
    'admin-password': ('credentials.py', 'rotate'),
    'admin-password-recover': ('credentials.py', 'recover'),
    'database-password': ('pg_credentials.py', 'rotate'),
    'database-password-recover': ('pg_credentials.py', 'recover'),
    's3-credentials': ('s3_credentials.py', 'rotate'),
    's3-credentials-recover': ('s3_credentials.py', 'recover'),
    's3-credentials-cleanup': ('s3_credentials.py', 'cleanup'),
    'vault-connect': ('vault_cache.py', 'connect'),
    'vault-sync': ('vault_cache.py', 'sync'),
    'vault-recover': ('vault_cache.py', 'recover'),
    'vault-auth': ('vault_cache.py', 'auth'),
    'vault-disconnect': ('vault_cache.py', 'disconnect'),
}


class Parsed(BaseException):
    def __init__(self, namespace):
        self.namespace = namespace


class Executed(BaseException):
    def __init__(self, args):
        self.args_received = args


class CommandContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.installation(self.base / 'Matrix Test')
        self.existing = self.installation(self.base / 'Existing Test', existing=True)
        self.profile = self.base / 'private profile.env'
        self.profile.write_text('# fictional private fixture\n')
        self.real_regular = command_wrapper.regular

    def installation(self, root, existing=False):
        root.mkdir(mode=0o700)
        names = set(command_wrapper.PUBLIC_FILES) | {'install.sh', 'wizard_state.py'}
        names.update(
            line.split('  ', 1)[1]
            for line in (BUNDLE / 'SHA256SUMS').read_text().splitlines()
            if line.endswith('.py') and '/' not in line.split('  ', 1)[1]
        )
        for name in names:
            shutil.copyfile(BUNDLE / name, root / name)
        (root / 'installation.json').write_text(
            json.dumps(
                {
                    'domain': 'example.org',
                    'config_format_version': 2,
                    'backend': 'existing-compose' if existing else 'portable',
                }
            )
        )
        return root

    def source_owner(self, path):
        info = self.real_regular(path)
        if Path(path).parent == BUNDLE:
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)
        return info

    def arguments(self, command, *, optional=False):
        args = command.path.split()
        for name in command.arguments:
            value = VALUES.get(name, str(self.profile))
            if name == 'confirm' and command.backend == 'move-abort':
                value = 'target-stopped'
            args.extend(['--' + name, value])
        if optional:
            for name in command.optional_arguments:
                value = {
                    'mode': 'landing',
                    'url': 'https://community.example.org/',
                    'name': 'Team with spaces',
                    'description': 'Fictional description; $(printf untouched)',
                    'support_url': '',
                    'administrator': '@owner:example.org',
                }.get(name, str(self.profile))
                args.extend(['--' + name.replace('_', '-'), value])
            args.extend('--' + name.replace('_', '-') for name in command.switches)
        if command.backend in ('update', 'update-recover'):
            args.extend(['--source', str(BUNDLE)])
        return args

    def planned(self, command, existing=False, optional=False, public=False):
        root = self.existing if existing else self.root
        args = self.arguments(command, optional=optional) + ['--directory', str(root)]
        parsed, options, values = misbot.parse(args)
        source = BUNDLE
        if public:
            source = self.base / 'public'
            source.mkdir(exist_ok=True)
            (source / 'command.json').write_text('{}')
        with (
            patch.object(command_wrapper, 'default_directory', return_value=root),
            patch.object(command_wrapper, 'regular', side_effect=self.source_owner),
        ):
            return misbot.plan(parsed, options, values, source=source)

    def accepted_by_backend(self, args):
        original = argparse.ArgumentParser.parse_args

        def capture(parser, *values, **kwargs):
            namespace = original(parser, *values, **kwargs)
            raise Parsed(namespace)

        # Stop immediately after the real parser, before any operation or root check.
        with (
            patch.object(sys, 'argv', args[1:]),
            patch.object(argparse.ArgumentParser, 'parse_args', capture),
            patch('signal.signal'),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(Parsed) as result:
                runpy.run_path(args[1], run_name='__main__')
        return result.exception.namespace

    def test_every_command_help_is_public_and_never_selects_an_installation(self):
        for existing in (False, True):
            for command in cli_commands.COMMANDS:
                with (
                    self.subTest(command=command.path, existing=existing),
                    patch.object(
                        command_wrapper,
                        'default_backend',
                        return_value='existing-compose' if existing else 'portable',
                    ),
                    patch.object(
                        command_wrapper,
                        'default_directory',
                        side_effect=AssertionError('private read'),
                    ),
                    patch.object(misbot, 'plan', side_effect=AssertionError('operation from help')),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    code = misbot.main([*command.path.split(), '--help'])
                    supported = not existing or command.backend in cli_commands.EXISTING_BACKENDS
                    self.assertEqual(code, 0 if supported else 2)
                    if supported:
                        self.assertTrue(output.getvalue().strip())

    def test_all_native_and_existing_python_routes_are_accepted_by_the_actual_parser(self):
        for existing in (False, True):
            for command in cli_commands.COMMANDS:
                if existing and command.backend not in cli_commands.EXISTING_BACKENDS:
                    continue
                if not existing and command.backend.startswith('features-'):
                    continue
                for optional in (False, True):
                    with self.subTest(command=command.path, existing=existing, optional=optional):
                        args = self.planned(command, existing, optional)
                        if args[0] == '/usr/bin/python3':
                            namespace = self.accepted_by_backend(args)
                            self.assertEqual(
                                Path(namespace.directory), self.existing if existing else self.root
                            )

    def test_unsupported_backends_are_rejected_before_execution(self):
        for command in cli_commands.COMMANDS:
            if command.backend not in cli_commands.EXISTING_BACKENDS:
                with self.subTest(command=command.path):
                    with self.assertRaises(misbot.InputError):
                        self.planned(command, existing=True)
            if command.backend.startswith('features-'):
                with self.assertRaises(misbot.InputError):
                    self.planned(command)

    def test_registered_public_runtime_keeps_the_same_execution_routes(self):
        for existing in (False, True):
            for command in cli_commands.COMMANDS:
                if existing and command.backend not in cli_commands.EXISTING_BACKENDS:
                    continue
                if not existing and command.backend.startswith('features-'):
                    continue
                with self.subTest(command=command.path, existing=existing):
                    public = self.planned(command, existing, optional=True, public=True)
                    bundle = self.planned(command, existing, optional=True)
                    # Bootstrap UI is executed from the full bundle; installed UI from its backend.
                    if (
                        command.backend in ('ui', 's3-check', 's3-cleanup') and not existing
                    ) or command.backend.startswith('draft-'):
                        bundle[1] = str(
                            (self.existing if existing else self.root) / Path(bundle[1]).name
                        )
                    if (
                        not command.installed
                        and not existing
                        and command.execution is None
                        and command.backend not in ('ui', 's3-check', 's3-cleanup')
                    ):
                        bundle[1] = str(self.root / 'install.sh')
                    self.assertEqual(public, bundle)

    def test_each_required_argument_and_unknown_flag_fail_before_backend_access(self):
        for command in cli_commands.COMMANDS:
            complete = self.arguments(command)
            cases = [complete + ['--unknown-private-option', 'FICTIONAL_SECRET']]
            for name in command.arguments:
                index = complete.index('--' + name)
                cases.append(complete[:index] + complete[index + 2 :])
            for args in cases:
                with (
                    self.subTest(command=command.path, args=len(args)),
                    patch.object(misbot, 'plan', side_effect=AssertionError('backend access')),
                    contextlib.redirect_stderr(io.StringIO()) as output,
                ):
                    self.assertEqual(misbot.main(args), 2)
                    self.assertNotIn('FICTIONAL_SECRET', output.getvalue())

    def test_every_command_requires_root_without_replaying_private_arguments(self):
        for command in cli_commands.COMMANDS:
            args = self.arguments(command, optional=True)
            if '--config' in args:
                args[args.index('--config') + 1] = '/root/FICTIONAL_SECRET.env'
            with (
                self.subTest(command=command.path),
                patch.object(misbot, 'os', SimpleNamespace(geteuid=lambda: 1000)),
                patch.object(misbot.shutil, 'which', return_value='/usr/bin/sudo'),
                patch.object(misbot, 'plan', side_effect=AssertionError('backend before root')),
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                self.assertEqual(misbot.main(args), 77)
                self.assertNotIn('FICTIONAL_SECRET', output.getvalue())

    def test_groups_and_nested_sections_show_help_without_running_any_operation(self):
        groups = {
            command.path.rsplit(' ', 1)[0]
            for command in cli_commands.COMMANDS
            if ' ' in command.path
        }
        groups |= set(cli_commands.TOPICS)
        for group in groups - set(cli_commands.BY_PATH):
            for style in (group.split(), [*group.split(), '--help'], ['help', *group.split()]):
                with (
                    self.subTest(group=group, style=style),
                    patch.object(command_wrapper, 'default_backend', return_value='portable'),
                    patch.object(misbot, 'plan', side_effect=AssertionError('group executed')),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(misbot.main(style), 0)
                    for command in cli_commands.COMMANDS:
                        if command.path.startswith(group + ' '):
                            self.assertIn('misbot ' + command.path, output.getvalue())

    def test_named_text_starting_with_dash_reaches_backend_as_a_literal(self):
        command, options, values = misbot.parse(
            ['homepage', 'apply', '--directory', str(self.root), '--description=--plain']
        )
        args = misbot.plan(command, options, values, source=BUNDLE)
        namespace = self.accepted_by_backend(args)
        self.assertEqual(namespace.description, '--plain')

    def test_positional_value_cannot_turn_an_operation_into_backend_help(self):
        command, options, values = misbot.parse(
            ['federation', 'check', '--directory', str(self.root), '--server=--help']
        )
        args = misbot.plan(command, options, values, source=BUNDLE)
        namespace = self.accepted_by_backend(args)
        self.assertEqual(namespace.action, 'check')
        self.assertEqual(namespace.argument, '--help')

    def test_word_help_in_an_option_value_is_preserved(self):
        command, _, values = misbot.parse(['homepage', 'apply', '--description', 'help'])
        self.assertEqual(command.path, 'homepage apply')
        self.assertEqual(values['description'], 'help')

    def test_help_flag_positions_and_version_help_are_public(self):
        for args in (
            ['--help', 'update'],
            ['update', '--help', '--source', '/nonexistent'],
            ['version', '--help'],
        ):
            with (
                self.subTest(args=args),
                patch.object(command_wrapper, 'default_backend', return_value='portable'),
                patch.object(misbot, 'plan', side_effect=AssertionError('help executed')),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(misbot.main(args), 0)
                self.assertTrue(output.getvalue())

    def test_every_supported_operation_is_executed_once_with_its_planned_argv(self):
        def execute(program, args):
            self.assertEqual(program, args[0])
            raise Executed(args)

        for existing in (False, True):
            root = self.existing if existing else self.root
            for command in cli_commands.COMMANDS:
                if (existing and command.backend not in cli_commands.EXISTING_BACKENDS) or (
                    not existing and command.backend.startswith('features-')
                ):
                    continue
                args = self.arguments(command, optional=True) + ['--directory', str(root)]
                expected = self.planned(command, existing, optional=True)
                with (
                    self.subTest(command=command.path, existing=existing),
                    patch.object(misbot, 'os', SimpleNamespace(geteuid=lambda: 0, execv=execute)),
                    patch.object(command_wrapper, 'regular', side_effect=self.source_owner),
                ):
                    with self.assertRaises(Executed) as result:
                        misbot.main(args)
                self.assertEqual(result.exception.args_received, expected)

    def test_help_layout_wraps_prose_and_examples_keep_the_same_shell_arguments(self):
        command = cli_commands.BY_PATH['backup schedule external']
        examples = []
        for width in (40, 80, 120):
            with (
                self.subTest(width=width),
                patch.object(
                    misbot.shutil, 'get_terminal_size', return_value=os.terminal_size((width, 24))
                ),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                misbot.operation_help(command)
            rendered = output.getvalue()
            self.assertIn('\n\nНеобязательные:', rendered)
            self.assertIn('\n\nУсловия:', rendered)
            example = rendered.split('\nПример:\n', 1)[1].split('\n\n', 1)[0]
            import shlex

            examples.append(shlex.split(example.replace('\\\n', '')))
            for line in rendered.splitlines():
                self.assertLessEqual(len(line), min(width, 100), line)
        self.assertTrue(all(example == examples[0] for example in examples))

    def shell_sandbox(self):
        commands = self.base / 'commands'
        commands.mkdir()
        trace = self.base / 'shell-trace.jsonl'
        shim = r"""#!/usr/bin/python3
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path(os.environ['MISBOT_CLI_TEST_TRACE']).open('a') as handle:
    handle.write(json.dumps({'tool': name, 'args': args}) + '\n')
if name == 'id':
    print('0')
elif name == 'dirname':
    print(Path(args[-1]).parent)
elif name == 'docker' and args[:2] == ['info', '--format']:
    print('x86_64')
elif name == 'python3' and '-c' in args:
    code = args[args.index('-c') + 1]
    if 'get("backend"' in code:
        print('portable')
    elif 'configuration_schema.domain' in code:
        print(args[-1])
    elif 'MEDIA_MODE' in code:
        print('local')
elif name == 'python3':
    raise SystemExit(int(os.environ.get('MISBOT_CLI_TEST_EXIT', '0')))
"""
        for name in (
            'id',
            'dirname',
            'python3',
            'apt-get',
            'systemctl',
            'docker',
            'curl',
            'openssl',
            'ip',
            'swapon',
            'dig',
        ):
            path = commands / name
            path.write_text(shim)
            path.chmod(0o700)
        return trace, {**os.environ, 'PATH': str(commands), 'MISBOT_CLI_TEST_TRACE': str(trace)}

    def test_every_shell_route_reaches_the_documented_backend_and_its_real_parser(self):
        trace, environment = self.shell_sandbox()
        observed = set()
        for command in cli_commands.COMMANDS:
            if command.backend.startswith('features-'):
                continue
            args = self.planned(command)
            if args[0] != '/bin/bash':
                continue
            with self.subTest(command=command.path):
                trace.unlink(missing_ok=True)
                result = subprocess.run(
                    args, env=environment, cwd=self.base, capture_output=True, text=True, timeout=10
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                records = [json.loads(line) for line in trace.read_text().splitlines()]
                calls = [
                    r['args'] for r in records if r['tool'] == 'python3' and '-c' not in r['args']
                ]
                self.assertTrue(calls)
                final = calls[-1]
                script, action = SHELL_TARGETS[command.backend]
                self.assertEqual(Path(final[0]).name, script)
                for argv in calls:
                    self.accepted_by_backend(['/usr/bin/python3', *argv])
                namespace = self.accepted_by_backend(['/usr/bin/python3', *final])
                if action is not None:
                    self.assertEqual(namespace.action, action)
                observed.add(command.backend)
        self.assertEqual(observed, set(SHELL_TARGETS))

    def test_actual_process_dispatch_preserves_backend_failure_and_signal(self):
        # Privilege simulation belongs only to the router; the child has no extra rights.
        script = self.root / 'configuration.py'
        program = """import os,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import misbot
misbot.os=SimpleNamespace(geteuid=lambda:0,execv=os.execv)
raise SystemExit(misbot.main(['--directory',sys.argv[2],'config','validate']))
"""
        for body, code in (
            ('raise SystemExit(23)\n', 23),
            ('import os,signal\nos.kill(os.getpid(),signal.SIGTERM)\n', -15),
        ):
            script.write_text(body)
            result = subprocess.run(
                [sys.executable, '-c', program, str(BUNDLE), str(self.root)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, code, result.stderr)

    def test_removed_private_installation_can_still_remove_its_owned_system_command(self):
        system = self.base / 'system'
        command_wrapper.install(self.root, system=system)
        record = command_wrapper.record(system)
        shutil.rmtree(self.root)
        command, options, values = misbot.parse(
            ['config', 'command', 'remove', '--directory', str(self.root)]
        )
        argv = misbot.plan(command, options, values, source=BUNDLE)
        namespace = self.accepted_by_backend(argv)
        self.assertEqual(namespace.action, 'remove')
        self.assertEqual(namespace.directory, self.root)
        self.assertTrue(command_wrapper.remove(self.root, system=system))
        self.assertFalse(command_wrapper.paths(system)[0].exists())
        self.assertEqual(record['directory'], str(self.root))

    def test_missing_runtime_explains_how_to_start_from_a_full_bundle(self):
        public = self.base / 'public'
        public.mkdir()
        (public / 'command.json').write_text('{}')
        shutil.rmtree(self.root)
        with patch.object(command_wrapper, 'default_directory', return_value=self.root):
            for args in (['ui'], ['config', 'init', '--domain', 'example.org']):
                with self.subTest(args=args):
                    command, options, values = misbot.parse(args)
                    with self.assertRaises(misbot.InputError) as result:
                        misbot.plan(command, options, values, source=public)
                    self.assertIn('sudo python3 misbot.py ui', str(result.exception))
                    self.assertIn('Исходники установки отсутствуют', str(result.exception))

    def test_missing_installation_metadata_is_reported_before_backend_execution(self):
        (self.root / 'installation.json').unlink()
        command, options, values = misbot.parse(['check', '--directory', str(self.root)])
        with self.assertRaises(misbot.InputError) as result:
            misbot.plan(command, options, values, source=BUNDLE)
        self.assertIn('нет installation.json', str(result.exception))

    def test_shell_values_cannot_turn_an_operation_into_backend_help(self):
        trace, environment = self.shell_sandbox()
        cases = (
            ('backup schedule local', 'calendar', 'calendar'),
            ('backup restore', 'sha256', 'sha256'),
            ('backup fetch', 'object', 'object_key'),
        )
        for path, option, destination in cases:
            with self.subTest(command=path):
                command = cli_commands.BY_PATH[path]
                args = self.arguments(command) + ['--directory', str(self.root)]
                index = args.index('--' + option)
                args[index : index + 2] = ['--' + option + '=--help']
                command, options, values = misbot.parse(args)
                argv = misbot.plan(command, options, values, source=BUNDLE)
                trace.unlink(missing_ok=True)
                result = subprocess.run(
                    argv, env=environment, cwd=self.base, capture_output=True, text=True, timeout=10
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                records = [json.loads(line) for line in trace.read_text().splitlines()]
                final = [
                    r['args'] for r in records if r['tool'] == 'python3' and '-c' not in r['args']
                ][-1]
                namespace = self.accepted_by_backend(['/usr/bin/python3', *final])
                self.assertEqual(getattr(namespace, destination), '--help')


if __name__ == '__main__':
    unittest.main()
