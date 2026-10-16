"""Vault ownership, lost replies, cache outages and restore integrity; fake secrets only."""

import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import vault_cache as vault


class Service:
    def __init__(self):
        self.current = None
        self.writes = 0
        self.lose_reply = False

    def read(self, version=None):
        if self.current and version and version != self.current[0]:
            return None
        return copy.deepcopy(self.current)

    def write(self, payload, version):
        if version != (self.current[0] if self.current else 0):
            raise RuntimeError('CAS mismatch')
        self.current = (version + 1, copy.deepcopy(payload))
        self.writes += 1
        if self.lose_reply:
            self.lose_reply = False
            raise RuntimeError('lost commit reply')
        return version + 1


class VaultTests(unittest.TestCase):
    def test_ready_local_restore_disconnect_allows_only_retired_external_owner(self):
        retired = self.root / 'config/external-postgres-owner.json'
        self.private(retired, '{"owner":"FICTIONAL_EXTERNAL_OWNER"}')
        self.connect()
        remote = copy.deepcopy(self.service.current)
        retired.unlink()
        self.private(
            self.root / 'installation.json',
            json.dumps(
                {'domain': 'chat.example.org', 'database_mode': 'local', 'restore_pending': True}
            ),
        )
        self.private(self.root / 'restore-journal.json', '{"stage":"ready","local_postgres":true}')
        self.private(
            self.root / 'settings.env',
            'DOMAIN=chat.example.org\nPOSTGRES_MODE=local\nPOSTGRES_HOST=\n',
        )
        with (
            patch.object(
                lifecycle,
                'service_rows',
                return_value=[{'Service': 'postgres', 'State': 'running'}],
            ),
            patch.object(vault, 'Client') as client,
        ):
            self.assertTrue(vault.disconnect(self.root))
            self.assertFalse(vault.disconnect(self.root))
            client.assert_not_called()
        self.assertEqual(self.service.current, remote)
        self.assertTrue((self.root / 'secrets/admin-access.json').is_file())
        self.assertFalse(retired.exists())

    def test_retired_owner_exception_refuses_incomplete_active_or_missing_secret(self):
        retired = self.root / 'config/external-postgres-owner.json'
        self.private(retired, '{"owner":"FICTIONAL_EXTERNAL_OWNER"}')
        self.connect()
        retired.unlink()
        for metadata, progress, settings, rows in (
            (
                {'database_mode': 'local', 'restore_pending': False},
                {'stage': 'ready', 'local_postgres': True},
                'local',
                [],
            ),
            (
                {'database_mode': 'external', 'restore_pending': True},
                {'stage': 'ready', 'local_postgres': True},
                'local',
                [],
            ),
            (
                {'database_mode': 'local', 'restore_pending': True},
                {'stage': 'importing', 'local_postgres': True},
                'local',
                [],
            ),
            (
                {'database_mode': 'local', 'restore_pending': True},
                {'stage': 'ready', 'local_postgres': False},
                'local',
                [],
            ),
            (
                {'database_mode': 'local', 'restore_pending': True},
                {'stage': 'ready', 'local_postgres': True},
                'external',
                [],
            ),
            (
                {'database_mode': 'local', 'restore_pending': True},
                {'stage': 'ready', 'local_postgres': True},
                'local',
                [{'Service': 'mas', 'State': 'running'}],
            ),
        ):
            with self.subTest(metadata=metadata, progress=progress, settings=settings, rows=rows):
                self.private(
                    self.root / 'installation.json',
                    json.dumps({'domain': 'chat.example.org', **metadata}),
                )
                self.private(self.root / 'restore-journal.json', json.dumps(progress))
                self.private(
                    self.root / 'settings.env',
                    'DOMAIN=chat.example.org\nPOSTGRES_MODE=' + settings + '\nPOSTGRES_HOST=\n',
                )
                with (
                    patch.object(lifecycle, 'service_rows', return_value=rows),
                    patch.object(vault, 'Client') as client,
                ):
                    with self.assertRaises(RuntimeError):
                        vault.disconnect(self.root)
                    client.assert_not_called()
                self.assertTrue(vault.descriptor(self.root).is_file())
        self.private(
            self.root / 'installation.json',
            json.dumps(
                {'domain': 'chat.example.org', 'database_mode': 'local', 'restore_pending': True}
            ),
        )
        self.private(self.root / 'restore-journal.json', '{"stage":"ready","local_postgres":true}')
        self.private(
            self.root / 'settings.env',
            'DOMAIN=chat.example.org\nPOSTGRES_MODE=local\nPOSTGRES_HOST=\n',
        )
        (self.root / 'config/mas-signing.pem').unlink()
        with patch.object(lifecycle, 'service_rows', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'missing secret files'):
                vault.disconnect(self.root)
        self.assertTrue(vault.descriptor(self.root).is_file())

    def test_staged_disconnect_requires_ready_and_stopped_non_postgres_services(self):
        metadata = {'restore_pending': True}
        self.private(self.root / 'restore-journal.json', '{"stage":"importing"}')
        with self.assertRaisesRegex(RuntimeError, 'original restore'):
            vault.assert_change_allowed(self.root, metadata, 'disconnect')
        self.private(self.root / 'restore-journal.json', '{"stage":"ready"}')
        for action in ('sync', 'connect', 'auth', 'recover'):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(RuntimeError, 'only vault-disconnect'),
            ):
                vault.assert_change_allowed(self.root, metadata, action)
        with patch.object(
            lifecycle, 'service_rows', return_value=[{'Service': 'synapse', 'State': 'running'}]
        ):
            with self.assertRaisesRegex(RuntimeError, 'were not changed'):
                vault.assert_change_allowed(self.root, metadata, 'disconnect')
        with patch.object(
            lifecycle, 'service_rows', return_value=[{'Service': 'postgres', 'State': 'running'}]
        ):
            vault.assert_change_allowed(self.root, metadata, 'disconnect')

    def test_disconnect_interrupted_after_descriptor_removal_finishes_without_vault(self):
        self.connect()
        remote = copy.deepcopy(self.service.current)
        original = Path.unlink

        def interrupted(path, *args, **kwargs):
            if path.name == 'vault-connection.env':
                raise InterruptedError('FICTIONAL interruption after descriptor unlink')
            return original(path, *args, **kwargs)

        with patch.object(vault, 'Client') as client, patch.object(Path, 'unlink', interrupted):
            with self.assertRaises(InterruptedError):
                vault.disconnect(self.root)
            client.assert_not_called()
        self.assertFalse(vault.descriptor(self.root).exists())
        self.assertTrue((self.root / 'config/vault-connection.env').is_file())
        with patch.object(vault, 'Client') as client:
            self.assertTrue(vault.disconnect(self.root))
            self.assertFalse(vault.disconnect(self.root))
            client.assert_not_called()
        self.assertEqual(remote, self.service.current)
        self.assertTrue((self.root / 'secrets/admin-access.json').is_file())

    def test_optional_snapshot_does_not_swallow_installer_signal(self):
        self.connect()
        emit = Mock()
        with patch.object(vault, 'sync', side_effect=vault.state_store.OperationInterrupted()):
            with self.assertRaises(vault.state_store.OperationInterrupted):
                vault.sync_optional(self.root, emit)
        emit.assert_not_called()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / 'stack'
        for name in ('config', 'secrets', 'data/synapse', 'data/referral-bot'):
            (self.root / name).mkdir(parents=True)
        self.private(self.root / 'installation.json', json.dumps({'domain': 'chat.example.org'}))
        self.private(self.root / 'settings.env', 'DOMAIN=chat.example.org\n')
        self.private(self.root / 'config/mas-signing.pem', 'EXAMPLE_SIGNING_FILE')
        self.private(self.root / 'secrets/admin-access.json', '{"password":"EXAMPLE_NOT_REAL"}')
        self.env = Path(self.folder.name) / 'vault.env'
        self.private(
            self.env,
            'VAULT_ADDR=https://vault.example.org\nVAULT_KV_MOUNT=matrix\n'
            'VAULT_SECRET_PATH=installer/test\nVAULT_ROLE_ID_FILE=/root/example-role\n'
            'VAULT_SECRET_ID_FILE=/root/example-secret\n',
        )
        self.service = Service()

    def tearDown(self):
        self.folder.cleanup()

    def private(self, path, text):
        path.write_text(text)
        path.chmod(0o600)

    def connect(self):
        with patch.object(vault, 'Client', return_value=self.service):
            return vault.connect(self.root, self.env)

    def recover(self):
        with (
            patch.object(lifecycle, 'service_rows', return_value=[]),
            patch.object(vault, 'RECOVERY_IDS', vault.RECOVERY_IDS | {os.geteuid(), os.getegid()}),
        ):
            return vault.recover(self.root, self.service)

    def test_connect_repeat_and_lost_write_reply_do_not_duplicate_versions(self):
        self.service.lose_reply = True
        with self.assertRaisesRegex(RuntimeError, 'lost commit reply'):
            self.connect()
        self.assertTrue(json.loads(vault.descriptor(self.root).read_text())['dirty'])
        count, version = self.connect()
        self.assertEqual((count, version, self.service.writes), (3, 1, 1))
        self.assertEqual(self.connect(), (3, 1))
        self.assertEqual(self.service.writes, 1)

    def test_foreign_snapshot_and_independent_version_change_are_refused(self):
        self.service.current = (1, {'owner': 'foreign'})
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            self.connect()
        self.assertFalse(vault.descriptor(self.root).exists())
        self.service.current = None
        self.connect()
        self.service.current = (2, self.service.current[1])
        self.private(
            self.root / 'settings.env', 'DOMAIN=chat.example.org\nACME_EMAIL=owner@example.org\n'
        )
        with self.assertRaisesRegex(RuntimeError, 'changed independently'):
            vault.sync(self.root, self.service)
        self.assertEqual(self.service.writes, 1)

    def test_outage_keeps_cache_and_marks_snapshot_stale(self):
        self.connect()
        before = (self.root / 'secrets/admin-access.json').read_bytes()
        events = []
        with patch.object(vault, 'Client', side_effect=OSError('EXAMPLE_PRIVATE_ERROR')):
            vault.sync_optional(self.root, lambda *args, **kwargs: events.append(kwargs))
        self.assertEqual(before, (self.root / 'secrets/admin-access.json').read_bytes())
        self.assertTrue(json.loads(vault.descriptor(self.root).read_text())['dirty'])
        self.assertTrue(events[0]['local_cache_retained'])
        self.assertNotIn('EXAMPLE_PRIVATE_ERROR', str(events))
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            self.recover()
        vault.sync(self.root, self.service)
        self.assertFalse(json.loads(vault.descriptor(self.root).read_text())['dirty'])

    def test_missing_file_restores_and_repeat_does_not_overwrite(self):
        self.connect()
        path = self.root / 'secrets/admin-access.json'
        expected = path.read_bytes()
        path.unlink()
        self.assertEqual(self.recover(), 1)
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.recover(), 0)
        self.private(path, 'NEWER_EXAMPLE_PASSWORD')
        with self.assertRaisesRegex(RuntimeError, 'Existing secret differs'):
            self.recover()
        self.assertEqual(path.read_text(), 'NEWER_EXAMPLE_PASSWORD')

    def test_corrupt_opaque_file_refuses_all_publication(self):
        self.connect()
        for name in ('config/mas-signing.pem', 'secrets/admin-access.json'):
            (self.root / name).unlink()
        self.service.current[1]['files']['secrets/admin-access.json'] = base64.b64encode(
            b'BROKEN'
        ).decode()
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            self.recover()
        self.assertFalse((self.root / 'config/mas-signing.pem').exists())

    def test_live_services_and_symlink_parent_prevent_recovery(self):
        self.connect()
        with patch.object(
            lifecycle, 'service_rows', return_value=[{'Service': 'mas', 'State': 'running'}]
        ):
            with self.assertRaisesRegex(RuntimeError, 'Stop server'):
                vault.recover(self.root, self.service)
        original = self.root / 'secrets'
        renamed = self.root / 'original-secrets'
        original.rename(renamed)
        original.symlink_to(renamed, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe Vault recovery path'):
            self.recover()

    def test_connection_rejects_insecure_origin_path_and_public_env(self):
        template = self.env.read_text()
        for old, new in [
            ('https://vault.example.org', 'http://vault.example.org'),
            ('https://vault.example.org', 'https://user:EXAMPLE@vault.example.org'),
            ('installer/test', '../test'),
        ]:
            self.private(self.env, template.replace(old, new))
            with self.assertRaises(RuntimeError):
                vault.connection(self.env)
        self.private(self.env, template)
        self.env.chmod(0o644)
        with self.assertRaises(RuntimeError):
            vault.connection(self.env)
