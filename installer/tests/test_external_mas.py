"""External MAS contracts without live accounts, network, Docker or secret output."""

from contextlib import ExitStack, nullcontext
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_archive
import cli_commands
import configuration
import configuration_schema as schema
import credentials
import database
import federation_runtime
import lifecycle
import mas_admin
import pg_credentials
import postgres_setup
import registration_api
import remove_server
import restore
import state_store
import user_password_api

BUNDLE = Path(__file__).resolve().parents[1]
VALUES = {
    'MAS_MODE': 'external',
    'MAS_DOMAIN': 'auth.example.net',
    'MAS_SHARED_SECRET': 'FICTIONAL_SHARED_SECRET',
    'MAS_CLIENT_ID': '01K5WZ00000000000000000004',
    'MAS_CLIENT_SECRET': 'FICTIONAL_CLIENT_SECRET',
}


class ExternalMASTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'stack'
        self.output = io.StringIO()
        self.stdout = patch('sys.stdout', self.output)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)

    def generate(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(BUNDLE / 'generate.py'),
                '--directory',
                str(self.root),
                '--preview',
                *args,
            ],
            capture_output=True,
            text=True,
        )

    def seed(self, extra=None):
        result = self.generate('--domain', 'example.org', '--initialize-env')
        self.assertEqual(result.returncode, 0, result.stderr)
        configuration.patch(self.root, dict(VALUES, **(extra or {})))
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.root / 'installation.json').read_text())

    def write_state(self, metadata):
        state_store.write_json(self.root / 'installation.json', metadata)

    def test_external_preview_excludes_local_mas_service_database_and_keys(self):
        metadata = self.seed()
        compose = yaml.safe_load((self.root / 'compose.yaml').read_text())
        synapse = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
        self.assertNotIn('mas', compose['services'])
        self.assertIn('postgres', compose['services'])
        self.assertEqual(
            synapse['matrix_authentication_service']['endpoint'], 'https://auth.example.net/'
        )
        self.assertEqual(
            synapse['matrix_authentication_service']['secret'], 'REDACTED_MAS_SHARED_SECRET'
        )
        self.assertEqual(metadata['mas_mode'], 'external')
        self.assertEqual(list((self.root / 'config').glob('mas*')), [])
        self.assertNotIn('CREATE ROLE mas', (self.root / 'config/init-db.sql').read_text())
        self.assertNotIn('auth.example.net', (self.root / 'config/Caddyfile').read_text())
        for name in ('db_mas', 'mas_shared', 'mas_encryption'):
            self.assertFalse((self.root / 'secrets' / name).exists())
        client = configuration.read_private_json(self.root / 'secrets/referral-client.json')
        self.assertEqual(client['mas_url'], 'https://auth.example.net')
        controller = configuration.read_private_json(
            self.root / 'data/referral-control/bot-config.json'
        )
        self.assertEqual(controller['servers']['example.org']['mas_url'], client['mas_url'])

    def test_external_postgres_with_external_mas_owns_only_synapse_role(self):
        self.seed(
            {
                'POSTGRES_MODE': 'external',
                'POSTGRES_HOST': 'db.example.net',
                'POSTGRES_ADMIN_PASSWORD': 'FICTIONAL_PG_SECRET',
            }
        )
        compose = yaml.safe_load((self.root / 'compose.yaml').read_text())
        self.assertNotIn('mas', compose['services'])
        self.assertNotIn('postgres', compose['services'])
        self.assertIn('postgres-client', compose['services'])
        self.assertNotIn('depends_on', compose['services']['synapse'])
        self.assertNotIn('CREATE DATABASE mas', (self.root / 'config/init-db.sql').read_text())

    def test_external_postgres_initialization_and_removal_leave_foreign_mas_untouched(self):
        self.seed(
            {
                'POSTGRES_MODE': 'external',
                'POSTGRES_HOST': 'db.example.net',
                'POSTGRES_ADMIN_PASSWORD': 'FICTIONAL_PG_SECRET',
            }
        )
        state_store.write_text(self.root / 'secrets/db_synapse', 'a' * 64)
        role_exists = False
        database_exists = False
        statements = []

        def postgres(tool, *args, stdin=None):
            nonlocal role_exists, database_exists
            statement = args[args.index('-c') + 1] if '-c' in args else stdin or ''
            statements.append(statement)
            if 'SELECT json_build_object' in statement:
                self.assertIn("IN ('synapse')", statement)
                self.assertNotIn("'mas'", statement)
                checkpoint = self.root / 'config/external-postgres-owner.json'
                owner = json.loads(checkpoint.read_text())['owner'] if checkpoint.exists() else ''
                roles = (
                    [{'name': 'synapse', 'marker': 'matrix-installer:' + owner}]
                    if role_exists
                    else []
                )
                databases = (
                    [
                        {
                            'name': 'synapse',
                            'owner': 'synapse',
                            'encoding': 'UTF8',
                            'collate': 'C',
                            'ctype': 'C',
                        }
                    ]
                    if database_exists
                    else []
                )
                return json.dumps({'roles': roles, 'databases': databases})
            if 'CREATE ROLE synapse' in statement:
                role_exists = True
            if statement.startswith('CREATE DATABASE synapse'):
                database_exists = True
            return ''

        result = postgres_setup.initialize(self.root, postgres)
        self.assertEqual(result, {'installer_owned_roles': 1, 'installer_owned_databases': 1})
        with patch.object(lifecycle, 'postgres', side_effect=postgres):
            remove_server.delete_external_database(self.root)
        self.assertTrue(any('DROP DATABASE synapse' in sql for sql in statements))
        self.assertFalse(any('ROLE mas' in sql or 'DATABASE mas' in sql for sql in statements))

    def test_external_mode_and_shared_credential_identity_cannot_change_after_first_start(self):
        metadata = self.seed()
        metadata.update(databases_initialized=True, storage_committed=True)
        self.write_state(metadata)
        configuration.assert_auth_identity(self.root, configuration.load(self.root), metadata)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for changes in (
            {'MAS_MODE': 'local'},
            {'MAS_DOMAIN': 'other.example.net'},
            {'MAS_SHARED_SECRET': 'FICTIONAL_OTHER'},
            {'MAS_CLIENT_SECRET': 'FICTIONAL_OTHER'},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                configuration.patch(self.root, changes)
            self.assertEqual(
                before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            )

    def test_external_generation_repeat_preserves_configured_service_identity(self):
        self.seed()
        path = self.root / 'secrets/referral-client.json'
        before = path.read_bytes()
        self.assertEqual(self.generate().returncode, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_external_admin_password_is_preserved_and_not_claimed_as_saved(self):
        metadata = self.seed()
        metadata['databases_initialized'] = True
        self.write_state(metadata)
        state_store.write_json(
            self.root / 'secrets/admin-access.json',
            {'username': 'matrix-admin', 'password': 'FICTIONAL_UNUSED_PASSWORD'},
        )
        state_store.write_text(self.root / 'mas_admin.py', 'public helper')
        with (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(lifecycle, 'emit'),
            patch.object(lifecycle, 'postgres') as sql,
            patch.object(
                lifecycle,
                'dc',
                return_value=json.dumps({'user_id': 'EXTERNAL_ADMIN', 'password_managed': False}),
            ),
        ):
            lifecycle.create_admin()
        sql.assert_not_called()
        saved = configuration.read_private_json(self.root / 'secrets/admin-access.json')
        self.assertNotIn('password', saved)
        self.assertFalse(saved['password_managed'])
        self.assertEqual(saved['mas_user_id'], 'EXTERNAL_ADMIN')
        self.assertEqual(
            json.loads((self.root / 'installation.json').read_text())['external_mas_admin_id'],
            'EXTERNAL_ADMIN',
        )
        self.assertNotIn('FICTIONAL_UNUSED_PASSWORD', self.output.getvalue())

    def test_external_bootstrap_keeps_ownership_checkpoint_until_host_commits_identity(self):
        checkpoint = self.base / 'bootstrap.json'
        user = {'id': 'EXTERNAL_ADMIN', 'attributes': {'admin': False}}
        req = Mock(
            side_effect=[
                (404, None),
                (201, {'data': user}),
                (204, None),
                (200, {'data': {'id': user['id'], 'attributes': {'admin': True}}}),
            ]
        )
        result = mas_admin.bootstrap(
            req,
            {'username': 'matrix-admin', 'password': 'FICTIONAL_PASSWORD'},
            checkpoint,
            retain_checkpoint=True,
        )
        self.assertEqual(result, {'user_id': user['id'], 'password_managed': True})
        self.assertTrue(checkpoint.exists())
        repeated = Mock(
            side_effect=[
                (200, {'data': user}),
                (204, None),
                (200, {'data': {'id': user['id'], 'attributes': {'admin': True}}}),
            ]
        )
        mas_admin.bootstrap(
            repeated,
            {'username': 'matrix-admin', 'password': 'FICTIONAL_PASSWORD'},
            checkpoint,
            retain_checkpoint=True,
        )
        self.assertEqual(repeated.call_args_list[1].args[1]['password'], 'FICTIONAL_PASSWORD')

    def test_start_publishes_matrix_https_before_external_account_provisioning(self):
        metadata = self.seed()
        metadata.update(
            databases_initialized=True,
            storage_committed=True,
            database_mode='local',
            media_mode='local',
        )
        self.write_state(metadata)
        signing = self.root / 'data/synapse/example.org.signing.key'
        signing.parent.mkdir(parents=True)
        state_store.write_text(signing, 'FICTIONAL_SIGNING_KEY')
        cert, key = self.base / 'cert', self.base / 'key'
        cert.touch()
        key.touch()
        state_store.write_json(self.root / 'release.json', {'version': '1.2.0'})
        state_store.write_text(self.root / 'SHA256SUMS', 'fictional manifest')
        events = []

        def compose(*args, **kw):
            events.append(args)
            return 'owned' if args[:2] == ('ps', '-q') else ''

        def wait(ready, **kw):
            self.assertTrue(ready())
            events.append(('ready', kw['service']))

        with ExitStack() as stack:
            stack.enter_context(patch.object(lifecycle, 'ROOT', self.root, create=True))
            for name in (
                'up_admission_check',
                'generate',
                'dns_check',
                'upgrade_legacy_network',
                'certificate_watch',
                'check',
                'emit',
            ):
                stack.enter_context(patch.object(lifecycle, name))
            stack.enter_context(
                patch.object(
                    lifecycle,
                    'external_mas_probe',
                    side_effect=lambda _: events.append(('external-probe',)),
                )
            )
            stack.enter_context(patch.object(lifecycle, 'dc', side_effect=compose))
            stack.enter_context(patch.object(lifecycle, 'postgres', return_value='1'))
            stack.enter_context(patch.object(lifecycle, 'wait', side_effect=wait))
            stack.enter_context(patch.object(lifecycle, 'run', return_value=''))
            stack.enter_context(
                patch.object(lifecycle, 'certificate_paths', return_value=(cert, key))
            )
            stack.enter_context(
                patch.object(
                    lifecycle, 'create_admin', side_effect=lambda: events.append(('administrator',))
                )
            )
            stack.enter_context(
                patch.object(
                    lifecycle,
                    'service_rows',
                    side_effect=lambda: [
                        {'Service': n, 'State': 'running'} for n in lifecycle.expected_services()
                    ],
                )
            )
            stack.enter_context(patch.object(lifecycle.referral, 'provision'))
            stack.enter_context(patch.object(lifecycle.referral, 'note_ready'))
            stack.enter_context(patch.object(federation_runtime, 'provision'))
            stack.enter_context(patch.object(federation_runtime, 'validate'))
            stack.enter_context(patch.object(federation_runtime, 'controller'))
            stack.enter_context(patch.object(lifecycle.vault_cache, 'sync_optional'))
            lifecycle.up()
        self.assertLess(
            events.index(('up', '-d', 'caddy', 'tls-router')), events.index(('administrator',))
        )
        self.assertLess(
            events.index(('ready', 'caddy tls-router')), events.index(('administrator',))
        )
        self.assertNotIn(('up', '-d', 'synapse', 'mas'), events)
        self.assertEqual(events[0], ('external-probe',))

    @patch.object(lifecycle, 'emit', lambda *args, **kwargs: None)
    def test_failed_external_admin_probe_blocks_generation_and_start(self):
        metadata = self.seed()
        metadata['databases_initialized'] = True
        self.write_state(metadata)
        with (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(lifecycle, 'up_admission_check'),
            patch.object(lifecycle, 'external_mas_probe', side_effect=RuntimeError('unavailable')),
            patch.object(lifecycle, 'generate') as generate,
            patch.object(lifecycle, 'dc') as dc,
        ):
            with self.assertRaises(RuntimeError):
                lifecycle.up()
        generate.assert_not_called()
        dc.assert_not_called()

    def test_unsupported_database_migration_and_password_changes_fail_before_mutation(self):
        self.root.mkdir(mode=0o700)
        self.write_state({'domain': 'example.org', 'mas_mode': 'external'})
        with (
            patch.object(lifecycle, 'state', side_effect=AssertionError('Unrelated runtime state')),
            patch.object(lifecycle, 'postgres') as sql,
            patch.object(lifecycle, 'backup') as backup,
        ):
            for action in (
                lambda: database.external(self.root, self.base / 'unused.env'),
                lambda: database.recover(self.root),
                lambda: database.abort(self.root),
                lambda: pg_credentials.rotate(self.root),
                lambda: pg_credentials.recover(self.root),
            ):
                with self.assertRaises(RuntimeError):
                    action()
        sql.assert_not_called()
        backup.assert_not_called()

    def test_unmanaged_admin_password_cannot_enter_rotation_with_missing_old_password(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'secrets').mkdir(mode=0o700)
        state_store.write_json(
            self.root / 'secrets/admin-access.json',
            {
                'username': 'matrix-admin',
                'matrix_id': '@matrix-admin:example.org',
                'mas_user_id': '01ARZ3NDEKTSV4RRFFQ69G5FAV',
                'password_managed': False,
            },
        )
        input_file = self.base / 'password.json'
        state_store.write_json(input_file, {'password': 'FICTIONAL_NEW_PASSWORD_123'})
        with (
            patch.object(lifecycle, 'state', return_value={}),
            patch.object(
                credentials, 'private_json', side_effect=lambda path: json.loads(path.read_text())
            ),
            patch.object(lifecycle, 'backup') as backup,
        ):
            with self.assertRaisesRegex(RuntimeError, 'не хранит'):
                credentials.rotate(self.root, input_file)
        backup.assert_not_called()

    def test_external_activation_requires_explicit_state_confirmation_before_writers(self):
        self.root.mkdir(mode=0o700)
        metadata = {'domain': 'example.org', 'mas_mode': 'external', 'restore_pending': True}
        self.write_state(metadata)
        state_store.write_json(self.root / 'restore-journal.json', {'stage': 'ready'})
        with (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(lifecycle, 'verify_external_mas_state') as verify,
            patch.object(lifecycle, 'clear_restored_one_time_keys') as clear,
            patch.object(lifecycle, 'up') as up,
        ):
            with self.assertRaisesRegex(RuntimeError, 'preserved-state'):
                lifecycle.restore_activate()
        verify.assert_not_called()
        clear.assert_not_called()
        up.assert_not_called()
        self.assertTrue(
            json.loads((self.root / 'installation.json').read_text())['restore_pending']
        )

    def test_external_activation_failure_restores_guard_and_stops_target(self):
        self.root.mkdir(mode=0o700)
        self.write_state({'domain': 'example.org', 'mas_mode': 'external', 'restore_pending': True})
        state_store.write_json(self.root / 'restore-journal.json', {'stage': 'ready'})
        with (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(lifecycle, 'verify_external_mas_state') as verify,
            patch.object(lifecycle, 'service_rows', return_value=[]),
            patch.object(lifecycle.preflight, 'check'),
            patch.object(lifecycle, 'clear_restored_one_time_keys'),
            patch.object(lifecycle, 'up', side_effect=RuntimeError('startup failed')),
            patch.object(lifecycle, 'dc') as dc,
        ):
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                lifecycle.restore_activate('preserved-state')
        verify.assert_called_once()
        dc.assert_called_once_with('stop')
        self.assertTrue(
            json.loads((self.root / 'installation.json').read_text())['restore_pending']
        )

    def test_synapse_only_backup_and_staged_restore_never_dump_or_import_mas(self):
        metadata = self.seed()
        metadata.update(
            databases_initialized=True,
            storage_committed=True,
            database_mode='local',
            media_mode='local',
            external_mas_admin_id='SAVED_ADMIN',
            federation_seeded=False,
        )
        self.write_state(metadata)
        for name in ('release.json', 'versions.json'):
            state_store.write_text(self.root / name, (BUNDLE / name).read_text())
        digest = hashlib.sha256((self.root / 'release.json').read_bytes()).hexdigest()
        state_store.write_text(
            self.root / 'SHA256SUMS',
            digest
            + '  release.json\n'
            + hashlib.sha256((self.root / 'versions.json').read_bytes()).hexdigest()
            + '  versions.json\n',
        )
        dumps = []

        def dumped(args, **kw):
            dumps.append(args[-1])
            kw['stdout'].write(b'fictional Synapse dump')
            return subprocess.CompletedProcess(args, 0)

        with (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(lifecycle, 'emit'),
            patch.object(lifecycle, 'paused_writers', return_value=nullcontext()),
            patch.object(
                lifecycle, 'postgres_command', side_effect=lambda tool, *args: [tool, *args]
            ),
            patch.object(lifecycle, 'database_inventory', return_value={'users': 1}),
            patch.object(lifecycle.subprocess, 'run', side_effect=dumped),
        ):
            archive = lifecycle.backup()
        self.assertEqual(dumps, ['synapse'])
        outer = self.base / 'unpacked'
        outer.mkdir()
        backup_archive.extract(archive, outer)
        snapshot = backup_archive.verify_snapshot(outer)
        self.assertEqual(backup_archive.snapshot_databases(snapshot), ('synapse',))
        boundary = json.loads((snapshot / 'backup-format.json').read_text())
        self.assertEqual(boundary['external_mas']['snapshot'], 'not-included')
        self.assertEqual(boundary['format'], 3)
        target = self.base / 'restored'
        imported = []
        commands = []

        def configure(root, action):
            lifecycle.ROOT = root

        def sql(tool, *args, **kw):
            commands.append((tool, *args))
            return '1'

        def restored(args, **kw):
            imported.append(args[-1])
            return subprocess.CompletedProcess(args, 0)

        extract = backup_archive.extract
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    backup_archive,
                    'extract',
                    side_effect=lambda archive, target, **kw: extract(
                        archive, target, owners=False
                    ),
                )
            )
            stack.enter_context(patch.object(lifecycle, 'ROOT', self.root, create=True))
            stack.enter_context(patch.object(lifecycle, 'configure', side_effect=configure))
            for name in ('ownership_check', 'generate', 'emit', 'clear_restored_one_time_keys'):
                stack.enter_context(patch.object(lifecycle, name))
            stack.enter_context(patch.object(lifecycle, 'dc', return_value=''))
            stack.enter_context(
                patch.object(lifecycle, 'wait', side_effect=lambda ready, **kw: ready())
            )
            stack.enter_context(patch.object(lifecycle, 'postgres', side_effect=sql))
            stack.enter_context(
                patch.object(
                    lifecycle, 'postgres_command', side_effect=lambda tool, *args: [tool, *args]
                )
            )
            stack.enter_context(
                patch.object(lifecycle, 'database_inventory', return_value={'users': 1})
            )
            stack.enter_context(patch.object(restore.subprocess, 'run', side_effect=restored))
            stack.enter_context(
                patch.object(
                    restore.update,
                    'validate_manifest',
                    return_value=({'release.json': digest}, None),
                )
            )
            stack.enter_context(patch.object(restore.update, 'copy_sources'))
            stack.enter_context(patch.object(restore.referral, 'verify_archive'))
            stack.enter_context(patch.object(federation_runtime, 'verify_archive'))
            restore.restore(
                archive, target, hashlib.sha256(archive.read_bytes()).hexdigest(), '8.8.8.8'
            )
        self.assertEqual(imported, ['synapse'])
        self.assertFalse(any('mas' in command for command in commands))
        saved = json.loads((target / 'installation.json').read_text())
        self.assertTrue(saved['restore_pending'])
        self.assertEqual(saved['mas_mode'], 'external')
        self.assertEqual(saved['external_mas_admin_id'], 'SAVED_ADMIN')
        self.assertTrue(saved['databases_initialized'])

    def test_cli_optional_activation_flag_reaches_shell_backend(self):
        command = cli_commands.BY_PATH['backup activate']
        values = {'confirm': 'source-stopped', 'external_mas_confirm': 'preserved-state'}
        args = cli_commands.execution_argv(command, values, self.root, BUNDLE)
        self.assertEqual(
            args[-4:], ['restore-activate', 'source-stopped', str(self.root), 'preserved-state']
        )


class ExternalMASTransportTests(unittest.TestCase):
    class Response:
        def __init__(self, value, status=200):
            self.data = json.dumps(value).encode()
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, *args):
            return self.data

    def test_all_private_transports_use_https_and_refuse_redirects(self):
        client = {
            'mas_url': 'https://auth.example.net',
            'client_id': 'FICTIONAL_ID',
            'client_secret': 'FICTIONAL_SECRET',
        }
        cases = (
            (mas_admin, {'action': 'probe'}),
            (registration_api, {'path': '/api/admin/v1/user-registration-tokens'}),
            (user_password_api, {'action': 'inspect', 'credentials': {'username': 'member'}}),
        )
        for module, action in cases:
            with self.subTest(module=module.__name__):
                requests = []

                def opened(req, **kw):
                    requests.append(req.full_url)
                    if req.full_url.endswith('/oauth2/token'):
                        return self.Response({'access_token': 'FICTIONAL_BEARER'})
                    if req.full_url.endswith('/.well-known/openid-configuration'):
                        return self.Response(
                            {
                                'issuer': 'https://auth.example.net/',
                                'token_endpoint': 'https://auth.example.net/oauth2/token',
                                'authorization_endpoint': 'https://auth.example.net/authorize',
                            }
                        )
                    if '/users/by-username/' in req.full_url:
                        return self.Response(
                            {
                                'data': {
                                    'id': 'SAVED_USER',
                                    'attributes': {'username': 'member', 'admin': False},
                                }
                            }
                        )
                    return self.Response({'data': []})

                with (
                    patch('sys.stdin', io.StringIO(json.dumps(dict(action, client=client)))),
                    patch('sys.stdout', io.StringIO()) as output,
                    patch.object(
                        module.urllib.request, 'build_opener', return_value=Mock(open=opened)
                    ) as build,
                ):
                    module.main()
                self.assertTrue(
                    all(url.startswith('https://auth.example.net/') for url in requests)
                )
                self.assertNotIn('/health', repr(requests))
                redirect = next(
                    handler
                    for handler in build.call_args.args
                    if isinstance(handler, module.urllib.request.HTTPRedirectHandler)
                )
                self.assertIsNone(
                    redirect.redirect_request(
                        None, None, None, None, None, 'https://other.example.org'
                    )
                )
                self.assertNotIn('FICTIONAL_SECRET', output.getvalue())
                self.assertNotIn('FICTIONAL_BEARER', output.getvalue())

    def test_controller_pagination_cannot_downgrade_external_mas_https(self):
        directory = BUNDLE.parent / 'bots/telegram'
        spec = importlib.util.spec_from_file_location(
            'external_mas_controller', directory / 'mas.py'
        )
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(directory))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(directory))
        api = module.MAS(
            {
                'mas_url': 'https://auth.example.net',
                'client_id': 'FICTIONAL_ID',
                'client_secret': 'FICTIONAL_SECRET',
            }
        )
        with (
            patch.object(module.requests, 'post') as token,
            patch.object(module.requests, 'request') as request,
        ):
            with self.assertRaises(module.BotError):
                api.request('http://auth.example.net/api/admin/v1/users')
        token.assert_not_called()
        request.assert_not_called()

    def test_changed_saved_external_account_refuses_before_session_or_mutation(self):
        req = Mock(
            return_value=(
                200,
                {
                    'data': {
                        'id': 'REPLACEMENT',
                        'attributes': {'username': 'matrix-admin', 'admin': True},
                    }
                },
            )
        )
        with self.assertRaises(RuntimeError):
            mas_admin.verify_state(
                req, [{'username': 'matrix-admin', 'user_id': 'ORIGINAL', 'admin': True}]
            )
        req.assert_called_once_with('/api/admin/v1/users/by-username/matrix-admin')

    def test_saved_device_requires_original_scope_and_active_session(self):
        users = [
            {
                'username': 'referral-bot',
                'user_id': 'ORIGINAL',
                'admin': False,
                'session_id': 'ORIGINAL_SESSION',
                'scope': 'scope-one scope-two',
            }
        ]
        for scope, revoked in (('scope-one', None), ('scope-one scope-two', 'past')):
            req = Mock(
                side_effect=[
                    (
                        200,
                        {
                            'data': {
                                'id': 'ORIGINAL',
                                'attributes': {'username': 'referral-bot', 'admin': False},
                            }
                        },
                    ),
                    (
                        200,
                        {
                            'data': {
                                'type': 'personal-session',
                                'id': 'ORIGINAL_SESSION',
                                'attributes': {
                                    'actor_user_id': 'ORIGINAL',
                                    'scope': scope,
                                    'revoked_at': revoked,
                                },
                            }
                        },
                    ),
                ]
            )
            with self.subTest(scope=scope, revoked=revoked), self.assertRaises(RuntimeError):
                mas_admin.verify_state(req, users)

    def test_saved_device_requires_exact_session_and_actor_identity(self):
        saved = {
            'username': 'referral-bot',
            'user_id': 'ORIGINAL',
            'admin': False,
            'session_id': 'ORIGINAL_SESSION',
            'scope': 'scope-one scope-two',
        }
        user = {
            'id': 'ORIGINAL',
            'attributes': {'username': 'referral-bot', 'admin': False},
        }
        session = {
            'type': 'personal-session',
            'id': 'ORIGINAL_SESSION',
            'attributes': {
                'actor_user_id': 'ORIGINAL',
                'owner_user_id': None,
                'owner_client_id': VALUES['MAS_CLIENT_ID'],
                'scope': 'scope-two scope-one',
                'revoked_at': None,
            },
        }
        request = Mock(side_effect=[(200, {'data': user}), (200, {'data': session})])
        self.assertEqual(
            mas_admin.verify_state(request, [saved]),
            {'passed': True, 'accounts_and_sessions': 1},
        )
        self.assertEqual(
            request.call_args_list[-1].args,
            ('/api/admin/v1/personal-sessions/ORIGINAL_SESSION',),
        )
        for changed in (
            {**session, 'id': 'OTHER_SESSION'},
            {**session, 'id': None},
            {**session, 'type': 'oauth2-session'},
            {**session, 'type': None},
            {**session, 'attributes': {**session['attributes'], 'actor_user_id': 'OTHER_USER'}},
            {**session, 'attributes': {**session['attributes'], 'actor_user_id': None}},
            {
                **session,
                'attributes': {
                    key: value
                    for key, value in session['attributes'].items()
                    if key != 'actor_user_id'
                },
                'relationships': {'user': {'data': {'id': 'ORIGINAL'}}},
            },
        ):
            request = Mock(side_effect=[(200, {'data': user}), (200, {'data': changed})])
            with self.subTest(session=changed), self.assertRaises(RuntimeError):
                mas_admin.verify_state(request, [saved])
            self.assertTrue(all(len(call.args) == 1 for call in request.call_args_list))


class ExternalMASArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.outer = self.base / 'outer'
        self.outer.mkdir()
        self.snapshot = self.outer / '20261013T141000Z-aaaaaa'
        self.snapshot.mkdir()
        self.metadata = {
            'domain': 'example.org',
            'mas_mode': 'external',
            'mas_issuer': 'https://auth.example.net/',
            'external_mas_admin_id': 'SAVED_ADMIN',
        }
        self.format = {
            'format': 3,
            'family': 'misbot-matrix-portable',
            'server_name': 'example.org',
            'mas_mode': 'external',
            'database_inventory': {'synapse': {'users': 1}},
            'external_mas': {
                'issuer': 'https://auth.example.net/',
                'server_name': 'example.org',
                'snapshot': 'not-included',
                'admin_user_id': 'SAVED_ADMIN',
            },
        }
        self.payload(self.metadata)
        for name in ('synapse.dump', 'roles.sql'):
            (self.snapshot / name).write_bytes(b'fictional owned data')
        (self.snapshot / 'mode.txt').write_text('consistent-synapse-only\n')
        (self.snapshot / 'backup-format.json').write_text(json.dumps(self.format))
        self.checksums()

    def payload(self, metadata):
        value = json.dumps(metadata).encode()
        with tarfile.open(self.snapshot / 'files.tar.gz', 'w:gz') as tar:
            info = tarfile.TarInfo('installation.json')
            info.size = len(value)
            tar.addfile(info, io.BytesIO(value))

    def checksums(self):
        values = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.snapshot.iterdir()
            if p.name != 'SHA256.json'
        }
        (self.snapshot / 'SHA256.json').write_text(json.dumps(values))

    def test_external_archive_verifies_without_placeholder_mas_dump(self):
        self.assertEqual(backup_archive.verify_snapshot(self.outer), self.snapshot)
        self.assertEqual(
            backup_archive.validate_authentication_boundary(self.snapshot, self.metadata),
            ('synapse',),
        )
        self.assertFalse((self.snapshot / 'mas.dump').exists())

    def test_external_archive_refuses_unrelated_mas_dump(self):
        (self.snapshot / 'mas.dump').write_bytes(b'foreign authentication data')
        self.checksums()
        with self.assertRaisesRegex(RuntimeError, 'unrelated MAS'):
            backup_archive.verify_snapshot(self.outer)

    def test_external_archive_refuses_mismatched_issuer_or_identity(self):
        for changes in (
            {'mas_issuer': 'https://other.example.net/'},
            {'external_mas_admin_id': 'REPLACEMENT'},
            {'mas_mode': 'local'},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                backup_archive.validate_authentication_boundary(
                    self.snapshot, dict(self.metadata, **changes)
                )

    def test_legacy_archive_formats_keep_both_database_contracts(self):
        for version in (1, 2):
            (self.snapshot / 'backup-format.json').write_text(json.dumps({'format': version}))
            self.assertEqual(backup_archive.snapshot_databases(self.snapshot), ('synapse', 'mas'))
        (self.snapshot / 'backup-format.json').unlink()
        self.assertEqual(backup_archive.snapshot_databases(self.snapshot), ('synapse', 'mas'))

    def test_unknown_backup_format_is_refused(self):
        (self.snapshot / 'backup-format.json').write_text(json.dumps({'format': 99}))
        with self.assertRaises(RuntimeError):
            backup_archive.snapshot_databases(self.snapshot)


if __name__ == '__main__':
    unittest.main()
