"""Native Telegram lifecycle retains private identity and live SQLite across recovery."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from contextlib import nullcontext
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_archive
import configuration
import lifecycle
import state_store
import telegram_runtime as runtime
import update

BUNDLE = Path(__file__).resolve().parents[1]


class TelegramRuntimeTests(unittest.TestCase):
    def test_startup_retries_missing_and_refused_socket_until_real_controller_reply(self):
        folder = self.root.parent
        path = folder / 'control.sock'
        start, listening = threading.Event(), threading.Event()
        errors, requests, attempts = [], [], []

        def controller():
            try:
                self.assertTrue(start.wait(2))
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.settimeout(2)
                    server.bind(str(path))
                    server.listen()
                    listening.set()
                    connection, _ = server.accept()
                    with connection, connection.makefile('rb') as stream:
                        requests.append(json.loads(stream.readline()))
                        connection.sendall(b'{"ok":true,"result":{"example.org":{}}}\n')
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=controller, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(start.set)

        def probe():
            attempts.append(True)
            try:
                return runtime.ready(self.root)
            except RuntimeError:
                if len(attempts) == 1:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
                        stale.bind(str(path))
                elif len(attempts) == 2:
                    path.unlink()
                    start.set()
                    self.assertTrue(listening.wait(2))
                raise

        with (
            patch.object(runtime, 'socket_directory', return_value=folder),
            patch.object(lifecycle, 'emit'),
            patch.object(lifecycle.time, 'sleep'),
        ):
            lifecycle.wait(probe, attempts=3, service='telegram-controller')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(requests, [{'action': 'servers'}])

    def test_readiness_only_normalizes_expected_transport_errors(self):
        for kind in (
            FileNotFoundError,
            ConnectionRefusedError,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
        ):
            with self.subTest(kind=kind), patch.object(runtime, 'rpc', side_effect=kind()):
                with self.assertRaisesRegex(RuntimeError, 'transport is not ready'):
                    runtime.ready(self.root)
        for error in (
            PermissionError(),
            OSError('unexpected'),
            KeyError('result'),
            json.JSONDecodeError('malformed response', '', 0),
        ):
            with (
                self.subTest(kind=type(error)),
                patch.object(runtime, 'rpc', side_effect=error),
                patch.object(lifecycle, 'emit'),
                patch.object(lifecycle.time, 'sleep') as sleep,
            ):
                with self.assertRaises(type(error)):
                    lifecycle.wait(lambda: runtime.ready(self.root), attempts=3)
                sleep.assert_not_called()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'source'
        self.root.mkdir(mode=0o700)
        self.state = {
            'domain': 'example.org',
            'preview': True,
            'telegram_runtime': {'format': 1, 'server_name': 'example.org'},
        }
        for name in ('config', runtime.CONTROL + '/data', runtime.WORKER_DATA, 'backups'):
            (self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in self.root.rglob('*'):
            if path.is_dir():
                path.chmod(0o700)
        state_store.write_json(self.root / 'installation.json', self.state)
        self.worker = {
            'server_name': 'example.org',
            'token': '123456:FICTIONAL_TOKEN',
            'username': 'fictional_bot',
            'client_id': 123456,
        }
        state_store.write_json(self.root / runtime.WORKER_CONFIG, self.worker)
        state_store.write_json(
            self.root / runtime.CONTROL / 'bot-config.json',
            {
                'servers': {
                    'example.org': {
                        'client_id': 'FICTIONAL_MAS_CLIENT',
                        'client_secret': 'FICTIONAL_SECRET',
                        'mas_url': 'https://auth.example.org',
                        'synapse_url': 'https://matrix.example.org',
                        'stack_root': '/old/source/root',
                        'management_actions': ['status', 'backup', 'update'],
                        'federation_backend': 'misbot',
                    }
                }
            },
        )
        state_store.write_json(
            self.root / runtime.CONTROL / 'bot-policy.json',
            {
                'servers': {
                    'example.org': {
                        'invitation_lifetime_seconds': 600,
                        'invitation_usage_limit': 2,
                        'registration_required': True,
                    }
                }
            },
        )
        with sqlite3.connect(self.root / runtime.CONTROL_DB) as db:
            db.executescript(
                'CREATE TABLE authorizations(server TEXT,uid INTEGER,user_id TEXT,expires INTEGER);CREATE TABLE management_jobs(id TEXT,server TEXT,uid INTEGER,operation TEXT,state TEXT,result TEXT);CREATE TABLE telegram_accounts(server TEXT,user_id TEXT,uid INTEGER);'
            )
            db.execute(
                'INSERT INTO authorizations VALUES (?,?,?,?)',
                ('example.org', 11, 'MAS_USER', 2000000000),
            )
            db.execute(
                'INSERT INTO telegram_accounts VALUES (?,?,?)', ('example.org', 'MAS_USER', 11)
            )
            db.execute(
                'INSERT INTO management_jobs VALUES (?,?,?,?,?,?)',
                ('job', 'example.org', 11, 'backup', 'running', None),
            )
        with sqlite3.connect(self.root / runtime.WORKER_DB) as db:
            db.executescript(
                'CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE users(uid INTEGER PRIMARY KEY,server TEXT,waiting TEXT);'
            )
            for key, value in {
                'server_binding': json.dumps({'format': 1, 'server_name': 'example.org'}),
                'offset': '100',
                'outbox': '[{"id":"pending","body":"retained"}]',
            }.items():
                db.execute('INSERT INTO state VALUES (?,?)', (key, value))
            db.execute('INSERT INTO users VALUES (?,?,?)', (11, 'example.org', 'confirmation'))
        for name in (runtime.WORKER_DB, runtime.CONTROL_DB):
            (self.root / name).chmod(0o600)
        state_store.write_json(
            self.root / runtime.IMAGE_LOCK, {'format': 1, 'image_id': 'sha256:' + 'a' * 64}
        )

    def source_bundle(self):
        (self.root / 'telegram-bot').mkdir(exist_ok=True)
        for name in runtime.SOURCES:
            shutil.copyfile(
                BUNDLE.parent / 'bots/telegram' / name, self.root / 'telegram-bot' / name
            )
        (self.root / 'versions.json').write_bytes((BUNDLE / 'versions.json').read_bytes())
        self.source_manifest()
        state_store.write_text(
            self.root / 'compose.yaml',
            yaml.safe_dump({'services': runtime.service(self.root, self.state)}),
        )

    def generate(self):
        result = subprocess.run(
            [
                sys.executable,
                str(BUNDLE / 'generate.py'),
                '--directory',
                str(self.root),
                '--preview',
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return yaml.safe_load((self.root / 'compose.yaml').read_text())['services']

    def source_manifest(self):
        (self.root / 'SHA256SUMS').write_text(
            ''.join(
                hashlib.sha256((self.root / 'telegram-bot' / name).read_bytes()).hexdigest()
                + '  telegram-bot/'
                + name
                + '\n'
                for name in runtime.SOURCES
            )
        )

    def test_disabled_runtime_does_not_add_service_or_archive_payload(self):
        disabled = {'domain': 'example.org'}
        self.assertFalse(runtime.enabled(disabled))
        self.assertEqual(runtime.service(self.root, disabled), {})
        self.assertIsNone(runtime.validate(self.root, disabled))
        with patch.object(runtime, 'snapshot_database') as snapshot:
            runtime.add_backup(None, self.root, disabled, None)
        snapshot.assert_not_called()
        with patch.object(lifecycle, 'state', return_value=disabled):
            self.assertNotIn('telegram-worker', lifecycle.expected_services())

    def test_actual_generator_adds_only_the_optional_worker_and_keeps_private_state(self):
        retained = {
            name: (self.root / name).read_bytes()
            for name in (runtime.WORKER_CONFIG, runtime.CONTROL_DB, runtime.WORKER_DB)
        }
        for _ in range(2):
            services = self.generate()
            self.assertEqual(services['synapse']['ports'], ['127.0.0.1:8008:8008'])
            self.assertEqual(services['mas']['ports'], ['127.0.0.1:8080:8080'])
        self.assertEqual(len(services), 13)
        controller = configuration.read_private_json(
            self.root / runtime.CONTROL / 'bot-config.json'
        )['servers']['example.org']
        self.assertEqual(controller['synapse_url'], 'http://127.0.0.1:8008')
        self.assertEqual(controller['mas_url'], 'http://127.0.0.1:8080')
        worker = services['telegram-worker']
        self.assertEqual(worker['user'], '10001:10001')
        self.assertTrue(worker['read_only'])
        self.assertEqual(len(worker['volumes']), 3)
        self.assertNotIn('ports', worker)
        self.assertNotIn('secrets', worker)
        for name, content in retained.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def test_disabled_generator_keeps_matrix_and_mas_ports_private(self):
        self.state.pop('telegram_runtime')
        state_store.write_json(self.root / 'installation.json', self.state)
        services = self.generate()
        self.assertEqual(len(services), 12)
        self.assertNotIn('telegram-worker', services)
        self.assertNotIn('ports', services['synapse'])
        self.assertNotIn('ports', services['mas'])

    def test_external_mas_generator_only_publishes_synapse_loopback(self):
        configuration.write_model(
            self.root,
            {
                'DOMAIN': 'example.org',
                'MAS_MODE': 'external',
                'MAS_DOMAIN': 'auth.example.org',
                'MAS_SHARED_SECRET': 'FICTIONAL_SHARED_SECRET',
                'MAS_CLIENT_ID': '01K5WZ00000000000000000004',
                'MAS_CLIENT_SECRET': 'FICTIONAL_CLIENT_SECRET',
            },
            preview=True,
        )
        for _ in range(2):
            services = self.generate()
            self.assertNotIn('mas', services)
            self.assertEqual(services['synapse']['ports'], ['127.0.0.1:8008:8008'])
            controller = configuration.read_private_json(
                self.root / runtime.CONTROL / 'bot-config.json'
            )['servers']['example.org']
            self.assertEqual(controller['mas_url'], 'https://auth.example.org')
            self.assertEqual(controller['synapse_url'], 'http://127.0.0.1:8008')
            self.assertNotIn(
                '127.0.0.1:8080:8080',
                [port for service in services.values() for port in service.get('ports', [])],
            )

    def test_repeat_and_relocation_preserve_credentials_policy_binding_offset_and_outbox(self):
        before = {
            name: (self.root / name).read_bytes()
            for name in (
                runtime.WORKER_CONFIG,
                runtime.WORKER_DB,
                runtime.CONTROL_DB,
                runtime.CONTROL + '/bot-policy.json',
            )
        }
        for _ in range(2):
            runtime.configure(self.root, self.state, preview=True)
        after_config = configuration.read_private_json(
            self.root / runtime.CONTROL / 'bot-config.json'
        )['servers']['example.org']
        self.assertEqual(after_config['stack_root'], str(self.root))
        self.assertEqual(after_config['client_id'], 'FICTIONAL_MAS_CLIENT')
        self.assertEqual(after_config['client_secret'], 'FICTIONAL_SECRET')
        target = self.root.parent / 'relocated'
        shutil.copytree(self.root, target)
        runtime.configure(target, self.state, preview=True)
        moved = configuration.read_private_json(target / runtime.CONTROL / 'bot-config.json')[
            'servers'
        ]['example.org']
        self.assertEqual(moved['stack_root'], str(target))
        self.assertNotEqual(runtime.unit_name(self.root), runtime.unit_name(target))
        self.assertNotIn(str(self.root), json.dumps(runtime.service(target, self.state)))
        for name, data in before.items():
            self.assertEqual((target / name).read_bytes(), data)
        state_store.write_json(
            target / runtime.WORKER_CONFIG, {**self.worker, 'token': '123456:CHANGED'}
        )
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            runtime.validate(target, self.state)

    def test_backup_restore_checks_both_databases_without_resetting_live_job(self):
        runtime.configure(self.root, self.state, preview=True)
        state_store.write_json(self.root / 'installation.json', self.state)
        archive = self.root.parent / 'files.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name in ('installation.json', runtime.WORKER_CONFIG, runtime.IMAGE_LOCK):
                backup_archive.add_tree(tar, self.root / name, name)
            runtime.add_backup(tar, self.root, self.state, lambda *args, **kwargs: 'inactive')
        runtime.verify_archive(archive)
        with tarfile.open(archive) as tar:
            self.assertFalse(
                any(member.name.endswith(('-wal', '-shm')) for member in tar.getmembers())
            )
            self.assertEqual(tar.getmember(runtime.WORKER_DB).uid, 10001)
        with sqlite3.connect(self.root / runtime.CONTROL_DB) as db:
            self.assertEqual(
                db.execute('SELECT state FROM management_jobs').fetchone()[0], 'running'
            )
        restored = self.root.parent / 'restored'
        restored.mkdir(mode=0o700)
        backup_archive.extract(archive, restored)
        runtime.configure(restored, self.state, preview=True)
        runtime.validate(restored, self.state)
        for name in (runtime.WORKER_CONFIG, runtime.CONTROL + '/bot-policy.json'):
            self.assertEqual((restored / name).read_bytes(), (self.root / name).read_bytes())
        with sqlite3.connect(restored / runtime.WORKER_DB) as db:
            self.assertEqual(
                db.execute("SELECT value FROM state WHERE key='offset'").fetchone()[0], '100'
            )
            self.assertIn(
                'pending', db.execute("SELECT value FROM state WHERE key='outbox'").fetchone()[0]
            )

    def test_full_backup_archives_verified_sqlite_instead_of_live_wal(self):
        configuration.write_model(self.root, {'DOMAIN': self.state['domain']}, preview=True)
        self.state['config_format_version'] = 2
        self.source_bundle()
        runtime.configure(self.root, self.state, preview=True)
        state_store.write_json(self.root / 'installation.json', self.state)

        def dump(command, **kwargs):
            kwargs['stdout'].write(b'fictional postgres dump')
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(lifecycle, 'paused_writers', return_value=nullcontext()),
            patch.object(lifecycle, 'postgres_command', return_value=['fictional-pg-dump']),
            patch.object(lifecycle.subprocess, 'run', side_effect=dump),
            patch.object(lifecycle, 'postgres', return_value='-- fictional roles\n'),
            patch.object(lifecycle, 'database_inventory', return_value={'users': 1}),
            patch.object(lifecycle, 'run', return_value='inactive'),
            patch.object(lifecycle, 'emit'),
        ):
            archive = lifecycle.backup()
        outer = self.root.parent / 'full-backup'
        outer.mkdir(mode=0o700)
        backup_archive.extract(archive, outer)
        snapshot = backup_archive.verify_snapshot(outer)
        runtime.verify_archive(snapshot / 'files.tar.gz')
        with tarfile.open(snapshot / 'files.tar.gz') as tar:
            names = [member.name for member in tar.getmembers()]
            self.assertEqual(names.count(runtime.CONTROL_DB), 1)
            self.assertEqual(names.count(runtime.WORKER_DB), 1)
            self.assertNotIn(runtime.CONTROL + '/data/backup.sqlite3', names)
            self.assertFalse(any(name.endswith(('-wal', '-shm')) for name in names))

    def test_foreign_binding_shared_matrix_auth_or_unknown_schema_are_refused(self):
        with sqlite3.connect(self.root / runtime.WORKER_DB) as db:
            db.execute('PRAGMA user_version=1')
        with self.assertRaisesRegex(RuntimeError, 'migration'):
            runtime.validate(self.root, self.state)
        with sqlite3.connect(self.root / runtime.WORKER_DB) as db:
            db.execute('PRAGMA user_version=0')
            db.execute(
                "UPDATE state SET value=? WHERE key='server_binding'",
                (json.dumps({'format': 1, 'server_name': 'foreign.example'}),),
            )
        with self.assertRaisesRegex(RuntimeError, 'server binding'):
            runtime.validate(self.root, self.state)
        with sqlite3.connect(self.root / runtime.WORKER_DB) as db:
            db.execute(
                "UPDATE state SET value=? WHERE key='server_binding'",
                (json.dumps({'format': 1, 'server_name': 'example.org'}),),
            )
        matrix = self.root / 'data/referral-control/data/bot-control.sqlite3'
        matrix.parent.mkdir(parents=True)
        shutil.copyfile(self.root / runtime.CONTROL_DB, matrix)
        with self.assertRaisesRegex(RuntimeError, 'Matrix controller'):
            runtime.validate(self.root, self.state)

    def test_native_backend_is_required_and_retired_accounts_remain_valid(self):
        with sqlite3.connect(self.root / runtime.CONTROL_DB) as db:
            db.execute(
                'INSERT INTO telegram_accounts VALUES (?,?,?)',
                ('example.org', 'PREVIOUS_MAS_USER', 'retired:' + 'a' * 32),
            )
        runtime.validate(self.root, self.state)
        path = self.root / runtime.CONTROL / 'bot-config.json'
        config = configuration.read_private_json(path)
        del config['servers']['example.org']['federation_backend']
        state_store.write_json(path, config)
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'federation_backend=misbot'):
            runtime.configure(self.root, self.state, preview=True)
        self.assertEqual(path.read_bytes(), before)

    def test_standalone_wal_validation_normalizes_sidecars_without_ignoring_wal(self):
        worker = self.root / runtime.WORKER_DB
        with sqlite3.connect(worker) as db:
            db.execute('PRAGMA journal_mode=WAL')
        db.close()
        saved = self.root.parent / 'worker-snapshot.sqlite3'
        runtime.snapshot_database(worker, saved)
        self.assertEqual(saved.read_bytes()[18:20], b'\x02\x02')
        shutil.copyfile(saved, worker)
        real_regular, real_stat = runtime.regular, os.fstat

        def as_root(descriptor):
            info = real_stat(descriptor)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=0, st_gid=0)

        with (
            patch.object(runtime.os, 'geteuid', return_value=0),
            patch.object(runtime.os, 'fstat', side_effect=as_root),
            patch.object(runtime.os, 'fchown') as chown,
            patch.object(
                runtime,
                'regular',
                side_effect=lambda path, owner, **kwargs: real_regular(
                    path, owner, ownership=False
                ),
            ),
        ):
            runtime.database(worker, 'example.org', worker=True)
        self.assertGreaterEqual(chown.call_count, 2)
        for call in chown.call_args_list:
            self.assertEqual(call.args[1:], (10001, 10001))
        with sqlite3.connect(worker) as db:
            self.assertEqual(
                db.execute("SELECT value FROM state WHERE key='offset'").fetchone()[0], '100'
            )

    def test_unsafe_worker_sidecars_are_rejected_before_sqlite_opens_them(self):
        worker = self.root / runtime.WORKER_DB
        sidecar = Path(str(worker) + '-wal')
        unrelated = self.root.parent / 'unrelated'
        unrelated.write_text('retained')
        unrelated.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'fifo'):
            if kind == 'symlink':
                sidecar.symlink_to(unrelated)
            elif kind == 'hardlink':
                os.link(unrelated, sidecar)
            else:
                os.mkfifo(sidecar, 0o600)
            with self.subTest(kind=kind), patch.object(runtime.sqlite3, 'connect') as connect:
                with self.assertRaisesRegex(RuntimeError, 'sidecar'):
                    runtime.database(worker, 'example.org', worker=True, ownership=False)
                connect.assert_not_called()
            sidecar.unlink()
        self.assertEqual(unrelated.read_text(), 'retained')

    def test_activation_refuses_live_controller_and_failures_disable_it_again(self):
        (self.root / 'restore-journal.json').write_text('{"stage":"ready"}')
        for action, flag in (
            (lifecycle.restore_activate, 'restore_pending'),
            (lifecycle.move_abort, 'migration_frozen'),
        ):
            saved = {**self.state, flag: True}
            state_store.write_json(self.root / 'installation.json', saved)
            calls = []

            def fail_after_controller():
                lifecycle.telegram_runtime.controller(self.root, lifecycle.run)
                raise RuntimeError('failure after controller start')

            with (
                self.subTest(action=action.__name__),
                patch.object(lifecycle, 'ROOT', self.root, create=True),
                patch.object(lifecycle, 'service_rows', return_value=[]),
                patch.object(lifecycle, 'run', return_value='inactive'),
                patch.object(lifecycle.preflight, 'check'),
                patch.object(lifecycle, 'clear_restored_one_time_keys'),
                patch.object(lifecycle, 'up', side_effect=fail_after_controller),
                patch.object(lifecycle, 'dc') as compose,
                patch.object(
                    runtime,
                    'controller',
                    side_effect=lambda *args, **kwargs: calls.append(kwargs.get('start', True)),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, 'failure after controller start'):
                    action()
                self.assertEqual(calls, [True, False])
                compose.assert_called_once_with('stop')
                self.assertTrue(
                    configuration.read_private_json(self.root / 'installation.json')[flag]
                )
        state_store.write_json(
            self.root / 'installation.json', {**self.state, 'restore_pending': True}
        )
        with (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(lifecycle, 'service_rows', return_value=[]),
            patch.object(lifecycle, 'run', return_value='active'),
            patch.object(lifecycle, 'clear_restored_one_time_keys') as clear,
        ):
            with self.assertRaisesRegex(RuntimeError, 'controller is still active'):
                lifecycle.restore_activate()
            clear.assert_not_called()

    def test_controller_refuses_staged_or_foreign_unit_and_preserves_bind_directory(self):
        runtime.configure(self.root, self.state, preview=True)
        system = self.root.parent / 'units'
        system.mkdir()
        commands = []
        run = lambda command, **kwargs: commands.append(command) or ''
        for flag in ('restore_pending', 'migration_frozen'):
            state_store.write_json(self.root / 'installation.json', {**self.state, flag: True})
            with self.assertRaisesRegex(RuntimeError, 'Staged/frozen'):
                runtime.controller(self.root, run, system=system)
        state_store.write_json(self.root / 'installation.json', self.state)
        folder = self.root.parent / 'misbot-telegram-test'
        with (
            patch.object(runtime, 'socket_directory', return_value=folder),
            patch.object(runtime.os, 'chown'),
        ):
            runtime.controller(self.root, run, system=system)
            text = (system / runtime.unit_name(self.root)).read_text()
            self.assertIn('RuntimeDirectoryPreserve=yes', text)
            self.assertIn('\nGroup=0\n', text)
            self.assertIn(str(self.root / 'referral-control/bridge.py'), text)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o750)
            runtime.controller(self.root, run, start=False, system=system, remove=True)
            self.assertFalse(folder.exists())
            state_store.write_text(system / runtime.unit_name(self.root), '# Foreign unit\n')
            with self.assertRaisesRegex(RuntimeError, 'Another installation'):
                runtime.controller(self.root, run, system=system)

    def test_source_rebuild_and_rollback_restore_pin_but_keep_newer_sqlite(self):
        self.source_bundle()
        present, builds, commands = set(), [], []

        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ['docker', 'image', 'inspect']:
                return command[3] if command[3] in present else ''
            if command[:2] == ['docker', 'build']:
                folder = Path(command[-1])
                self.assertEqual({p.name for p in folder.iterdir()}, set(runtime.SOURCES))
                image = 'sha256:' + ('a' if not builds else 'b') * 64
                Path(command[command.index('--iidfile') + 1]).write_text(image)
                present.add(image)
                builds.append(image)
            if command[:2] == ['docker', 'run']:
                self.assertIn('--help', command)
                self.assertIn('none', command)
                self.assertNotIn('--mount', command)
            return ''

        runtime.prepare_image(self.root, run)
        entries = {
            'telegram-bot/' + name: self.root / 'telegram-bot' / name for name in runtime.SOURCES
        }
        with patch.object(update, 'validate_manifest', return_value=(entries, 'fixture')):
            saved = update.snapshot(self.root, entries, 'telegram-update')
        worker_source = self.root / 'telegram-bot/worker.py'
        worker_source.write_text(worker_source.read_text() + '\n# changed verified worker source\n')
        self.source_manifest()
        runtime.prepare_image(self.root, run)
        self.assertEqual(len(builds), 2)
        with sqlite3.connect(self.root / runtime.WORKER_DB) as db:
            db.execute("UPDATE state SET value='200' WHERE key='offset'")
        with sqlite3.connect(self.root / runtime.CONTROL_DB) as db:
            db.execute("UPDATE management_jobs SET result='new result' WHERE id='job'")
        update.restore(self.root, saved, entries)
        runtime.prepare_image(self.root, run)
        self.assertEqual(len(builds), 2)
        self.assertEqual(
            yaml.safe_load((self.root / 'compose.yaml').read_text())['services']['telegram-worker'][
                'image'
            ],
            builds[0],
        )
        with sqlite3.connect(self.root / runtime.WORKER_DB) as db:
            self.assertEqual(
                db.execute("SELECT value FROM state WHERE key='offset'").fetchone()[0], '200'
            )
        with sqlite3.connect(self.root / runtime.CONTROL_DB) as db:
            self.assertEqual(
                db.execute('SELECT result FROM management_jobs').fetchone()[0], 'new result'
            )


if __name__ == '__main__':
    unittest.main()
