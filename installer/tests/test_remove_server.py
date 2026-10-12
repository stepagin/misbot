import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remove_server
import configuration
import update
import state_store

PROFILE = {
    'S3_ENDPOINT_URL': 'https://s3.example.org',
    'S3_REGION': 'example-region',
    'S3_BUCKET': 'fictional-bucket',
    'S3_PREFIX': 'example-server/',
    'S3_ACCESS_KEY_ID': 'FICTIONAL_ACCESS',
    'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
}


class S3:
    def __init__(self):
        self.versions = [
            {'Key': 'example-server/media', 'VersionId': '1'},
            {'Key': 'example-server/media', 'VersionId': '2'},
        ]
        self.markers = [{'Key': 'example-server/removed', 'VersionId': '3'}]
        self.current = []
        self.uploads = [{'Key': 'example-server/incomplete', 'UploadId': 'up'}]
        self.calls = []
        self.error = False

    def get_paginator(self, name):
        service = self

        class Pages:
            def paginate(self, **args):
                service.calls.append((name, args))
                if name == 'list_object_versions':
                    return iter(
                        [
                            {
                                'Versions': list(service.versions),
                                'DeleteMarkers': list(service.markers),
                            }
                        ]
                    )
                if name == 'list_objects_v2':
                    return iter([{'Contents': list(service.current)}])
                return iter([{'Uploads': list(service.uploads)}])

        return Pages()

    def delete_objects(self, **args):
        self.calls.append(('delete_objects', args))
        if self.error:
            return {'Errors': [{'Key': 'example-server/media'}]}
        self.versions = []
        self.markers = []
        self.current = []
        return {}

    def abort_multipart_upload(self, **args):
        self.calls.append(('abort', args))
        self.uploads = []


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'server'
        self.root.mkdir(mode=0o700)
        state_store.write_json(
            self.root / 'installation.json', {'domain': 'example.org', 'config_format_version': 2}
        )
        files = {name: b'# fixture public source\n' for name in update.ESSENTIAL}
        files['release.json'] = json.dumps(
            {
                'family': update.FAMILY,
                'version': '1.1.0',
                'update_scope': 'source-configuration-and-approved-components',
            }
        ).encode()
        files['federation-bot/logic.js'] = b'// public fixture\n'
        for name, raw in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        (self.root / 'SHA256SUMS').write_text(
            ''.join(
                hashlib.sha256(raw).hexdigest() + '  ' + name + '\n' for name, raw in files.items()
            )
        )
        for name in ('config', 'secrets', 'data', 'stages', 'settings', 'backups'):
            (self.root / name).mkdir(mode=0o700)
            (self.root / name / 'private-fixture').write_text('fictional-data')

    def tearDown(self):
        self.temp.cleanup()

    def record(self):
        return {
            'format': 1,
            'domain': 'example.org',
            'directory': str(self.root),
            'local': dict.fromkeys(remove_server.LOCAL, True),
            's3': [],
            'external_database': False,
            'vault': False,
            'completed': [],
        }

    def test_no_deletion_until_every_confirmation_and_exact_domain(self):
        answers = iter(['да', 'да', 'да', 'да', 'да', 'WRONG'])
        with self.assertRaises(RuntimeError):
            remove_server.plan(self.root, [], {}, lambda _: next(answers), lambda _: None)
        self.assertTrue((self.root / 'data/private-fixture').is_file())
        self.assertFalse((self.root / 'remove-journal.json').exists())
        self.assertIsNone(remove_server.plan(self.root, [], {}, lambda _: 'нет', lambda _: None))

    def test_init_only_full_removal_keeps_utility_and_forgets_all_bot_settings(self):
        (self.root / 'federation-bot/untracked-private.env').write_text('fictional secret')
        with (
            patch.object(remove_server, 'remove_containers'),
            patch.object(remove_server, 'host_cleanup'),
        ):
            remove_server.execute(self.root, self.record(), [], stop=lambda: None)
        self.assertTrue((self.root / 'install.sh').is_file())
        self.assertTrue((self.root / 'release.json').is_file())
        self.assertTrue((self.root / '.misbot-empty.json').is_file())
        self.assertFalse((self.root / 'installation.json').exists())
        for name in ('data', 'config', 'settings', 'secrets', 'stages', 'backups'):
            self.assertFalse((self.root / name).exists())
        self.assertTrue((self.root / 'federation-bot/logic.js').exists())
        self.assertFalse((self.root / 'federation-bot/untracked-private.env').exists())

    def test_partial_selection_retains_chosen_data_and_marks_decommissioned(self):
        record = self.record()
        record['local']['data'] = False
        with patch.object(remove_server, 'remove_containers'):
            remove_server.execute(self.root, record, [], stop=lambda: None)
        self.assertTrue((self.root / 'data/private-fixture').exists())
        self.assertTrue(json.loads((self.root / 'installation.json').read_text())['decommissioned'])

    def test_s3_failure_keeps_local_credentials_and_journal_for_retry(self):
        record = self.record()
        record['s3'] = [remove_server.s3_identity(PROFILE)]
        targets = [{'profile': PROFILE, 'identity': record['s3'][0]}]
        with patch.object(remove_server, 'delete_s3', side_effect=RuntimeError('retention')):
            with self.assertRaises(RuntimeError):
                remove_server.execute(self.root, record, targets, stop=lambda: None)
        self.assertTrue((self.root / 'secrets/private-fixture').exists())
        self.assertTrue((self.root / 'remove-journal.json').exists())
        self.assertEqual(record['completed'], ['services-stopped'])

    def test_s3_deletes_versions_markers_uploads_and_only_selected_prefix(self):
        service = S3()
        remove_server.delete_s3(PROFILE, service)
        self.assertFalse(service.versions + service.markers + service.uploads)
        for name, args in service.calls:
            self.assertEqual(args['Bucket'], PROFILE['S3_BUCKET'])
            if name.startswith('list_'):
                self.assertEqual(args['Prefix'], PROFILE['S3_PREFIX'])
        self.assertFalse(any(name == 'delete_bucket' for name, args in service.calls))

    def test_s3_foreign_key_and_per_object_errors_refuse_completion(self):
        service = S3()
        service.versions = [{'Key': 'other/data', 'VersionId': '1'}]
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            remove_server.delete_s3(PROFILE, service)
        self.assertFalse(any(name == 'delete_objects' for name, args in service.calls))
        service = S3()
        service.error = True
        with self.assertRaisesRegex(RuntimeError, 'refused'):
            remove_server.delete_s3(PROFILE, service)

    def test_symlink_inside_local_data_never_deletes_external_target(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('private fixture')
        (self.root / 'data/external').symlink_to(outside, target_is_directory=True)
        remove_server.remove_path(self.root / 'data')
        self.assertTrue((outside / 'keep').is_file())

    def test_mount_in_scope_refuses_removal(self):
        with patch.object(
            remove_server.os.path, 'ismount', side_effect=lambda p: Path(p) == self.root / 'data'
        ):
            with self.assertRaisesRegex(RuntimeError, 'mounted'):
                remove_server.remove_path(self.root / 'data')
        self.assertTrue((self.root / 'data/private-fixture').is_file())

    def test_retry_finalizes_after_installation_metadata_was_already_removed(self):
        record = self.record()
        with (
            patch.object(remove_server, 'remove_containers'),
            patch.object(remove_server, 'host_cleanup'),
        ):
            remove_server.execute(self.root, record, [], stop=lambda: None)
        state_store.write_json(self.root / 'remove-journal.json', record)
        remove_server.execute(
            self.root, record, [], stop=lambda: self.fail('Service stop repeated')
        )
        self.assertFalse((self.root / 'remove-journal.json').exists())
        self.assertTrue((self.root / '.misbot-empty.json').exists())

    def test_active_owned_update_worker_is_stopped_and_foreign_unit_refused(self):
        import release_update

        folder = self.root / 'data/admin-panel'
        folder.mkdir(mode=0o700)
        identifier = 'a' * 64
        unit = release_update.unit(self.root, identifier)
        job = {'id': identifier, 'unit': unit, 'state': 'running'}
        state_store.write_json(release_update.job_path(self.root), job)
        with (
            patch.object(
                remove_server.subprocess,
                'run',
                return_value=type('Result', (), {'returncode': 0, 'stdout': 'loaded\n'})(),
            ),
            patch.object(remove_server.lifecycle, 'run') as stop,
        ):
            remove_server.stop_update(self.root)
            stop.assert_called_once_with(['systemctl', 'stop', unit + '.service'])
        job['unit'] = 'foreign-service'
        state_store.write_json(release_update.job_path(self.root), job)
        with patch.object(remove_server.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Foreign'):
                remove_server.stop_update(self.root)
            command.assert_not_called()

    def test_full_removal_without_optional_backup_packages_stops_owned_services(self):
        # A fresh interpreter also catches imports hidden by the test runner's cache.
        state_store.write_json(
            self.root / 'homepage-transition.json', {'format': 1, 'phase': 'prepared'}
        )
        program = r"""import importlib.abc
import json
import sys
from pathlib import Path
from unittest.mock import patch


class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('cryptography', 'boto3'):
            raise ModuleNotFoundError('Optional dependency unavailable', name=fullname)


sys.meta_path.insert(0, BlockOptional())
sys.path.insert(0, sys.argv[1])
import remove_server
import backup_schedule
import federation_runtime

root = Path(sys.argv[2])
record = json.loads(sys.argv[3])
units = root.parent / 'units'
units.mkdir()
base = backup_schedule.unit_name(root)
for suffix in ('timer', 'service'):
    (units / (base + '.' + suffix)).write_text('# Matrix installer backup: ' + str(root) + '\n')
controller = federation_runtime.controller
unit = units / federation_runtime.unit_name(root)
unit.write_text('# Misbot federation controller ' + str(root) + '\n')
commands = []
containers = ['fixture-container']


def run(command, **kwargs):
    commands.append(command)
    if command[:3] == ['docker', 'ps', '-aq']:
        return '\n'.join(containers)
    if command[:2] == ['docker', 'inspect']:
        return json.dumps(str(root)) + '\n'
    if command[:3] == ['docker', 'network', 'ls']:
        return ''
    if command[:2] == ['docker', 'compose'] and command[-1] == 'down':
        containers.clear()
    return ''


with (
    patch.object(backup_schedule, 'UNIT_DIRECTORY', units),
    patch.object(
        federation_runtime,
        'controller',
        lambda root, run, start, remove: controller(root, run, start, system=units, remove=remove),
    ),
    patch.object(remove_server, 'stop_turn'),
    patch.object(remove_server, 'host_cleanup'),
    patch.object(remove_server.shutil, 'which', return_value='/fake/docker'),
    patch.object(remove_server.lifecycle, 'run', side_effect=run),
):
    remove_server.execute(root, record, [])
assert not containers
assert not list(units.iterdir())
print(json.dumps({'commands': commands, 'completed': record['completed']}))
"""
        result = subprocess.run(
            [
                sys.executable,
                '-c',
                program,
                str(Path(remove_server.__file__).parent),
                str(self.root),
                json.dumps(self.record()),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout.splitlines()[-1])
        commands = saved['commands']
        self.assertTrue(any(command[-1] == 'stop' for command in commands))
        self.assertTrue(any(command[-1] == 'down' for command in commands))
        self.assertTrue(any(command[:2] == ['systemctl', 'disable'] for command in commands))
        self.assertEqual(saved['completed'][0], 'services-stopped')
        self.assertTrue((self.root / '.misbot-empty.json').exists())
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse((self.root / 'remove-journal.json').exists())
        self.assertFalse((self.root / 'homepage-transition.json').exists())

    def test_stop_failure_preserves_data_and_retry_completes_same_plan(self):
        record = self.record()
        cause = ModuleNotFoundError('FICTIONAL_SECRET_DO_NOT_ECHO', name='cryptography')
        with patch.object(remove_server, 'stop_services', side_effect=cause):
            with self.assertRaises(remove_server.RemovalError) as raised:
                remove_server.execute(self.root, record, [])
        self.assertEqual(raised.exception.stage, 'services-stopped')
        message = remove_server.failure_message(raised.exception)
        self.assertIn('Остановка служб', message)
        self.assertIn('python3-cryptography', message)
        self.assertNotIn('FICTIONAL_SECRET_DO_NOT_ECHO', message)
        saved = json.loads((self.root / 'remove-journal.json').read_text())
        self.assertEqual(saved['completed'], [])
        for name in ('data', 'secrets', 'settings', 'config', 'backups'):
            self.assertTrue((self.root / name / 'private-fixture').exists())
        with (
            patch.object(remove_server, 'stop_services') as stop,
            patch.object(remove_server, 'remove_containers'),
            patch.object(remove_server, 'host_cleanup'),
        ):
            remove_server.execute(self.root, saved, [])
        stop.assert_called_once_with(self.root)
        self.assertFalse((self.root / 'remove-journal.json').exists())
        self.assertFalse((self.root / 'data').exists())

    def test_failure_diagnostics_never_repeat_exception_values(self):
        cases = (
            (RuntimeError('FICTIONAL_SECRET_DO_NOT_ECHO'), 'не подтверждена'),
            (PermissionError('FICTIONAL_SECRET_DO_NOT_ECHO'), 'прав'),
            (OSError(28, 'FICTIONAL_SECRET_DO_NOT_ECHO'), 'места'),
            (KeyboardInterrupt('FICTIONAL_SECRET_DO_NOT_ECHO'), 'прервана'),
        )
        for cause, expected in cases:
            with self.subTest(cause=type(cause).__name__):
                error = remove_server.RemovalError('local-secrets', cause)
                message = remove_server.failure_message(error)
                self.assertIn('Удаление локальных секретов и настроек', message)
                self.assertIn(expected, message)
                self.assertNotIn('FICTIONAL_SECRET_DO_NOT_ECHO', message)

    def test_entrypoint_reports_interruption_and_safe_error_without_traceback(self):
        for cause, code in (
            (KeyboardInterrupt('private'), 130),
            (state_store.OperationInterrupted('private'), 130),
            (RuntimeError('private'), 1),
        ):
            with (
                self.subTest(code=code),
                patch.object(remove_server, 'main', side_effect=cause),
                patch.object(sys, 'stderr', new_callable=io.StringIO) as output,
            ):
                self.assertEqual(remove_server.entrypoint(), code)
                self.assertIn('Подготовка и подтверждение', output.getvalue())
                self.assertNotIn('private', output.getvalue())
                self.assertNotIn('Traceback', output.getvalue())

    def test_interrupted_step_retains_journal_for_confirmation_and_retry(self):
        with (
            patch.object(remove_server, 'stop_services', side_effect=KeyboardInterrupt),
            patch.object(
                remove_server,
                'main',
                side_effect=lambda: remove_server.execute(self.root, self.record(), []),
            ),
            patch.object(sys, 'stderr', new_callable=io.StringIO) as output,
        ):
            self.assertEqual(remove_server.entrypoint(), 130)
        self.assertIn('Остановка служб', output.getvalue())
        saved = json.loads((self.root / 'remove-journal.json').read_text())
        self.assertEqual(saved['completed'], [])
        self.assertTrue((self.root / 'data/private-fixture').exists())

    def test_resume_requires_exact_confirmation_before_repeating_removal(self):
        (self.root / 'settings/private-fixture').unlink()
        (self.root / 'settings').rmdir()
        configuration.write_model(self.root, {'DOMAIN': 'example.org'})
        record = self.record()
        state_store.write_json(self.root / 'remove-journal.json', record)
        before = (self.root / 'remove-journal.json').read_bytes()
        with (
            patch.object(sys, 'argv', ['remove_server.py', '--directory', str(self.root)]),
            patch.object(remove_server.state_store, 'require_root'),
            patch.object(sys.stdin, 'isatty', return_value=True),
            patch.object(sys.stdout, 'isatty', return_value=True),
            patch('builtins.input', return_value='нет'),
            patch.object(remove_server, 'execute') as execute,
        ):
            remove_server.main()
        execute.assert_not_called()
        self.assertEqual((self.root / 'remove-journal.json').read_bytes(), before)
        self.assertTrue((self.root / 'data/private-fixture').exists())
        with (
            patch.object(sys, 'argv', ['remove_server.py', '--directory', str(self.root)]),
            patch.object(remove_server.state_store, 'require_root'),
            patch.object(sys.stdin, 'isatty', return_value=True),
            patch.object(sys.stdout, 'isatty', return_value=True),
            patch('builtins.input', return_value='УДАЛИТЬ СЕРВЕР example.org'),
            patch.object(remove_server, 'execute') as execute,
        ):
            remove_server.main()
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[1], record)


if __name__ == '__main__':
    unittest.main()
