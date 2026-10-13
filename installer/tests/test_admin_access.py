"""Privilege changes bind a registered local account and preserve original access."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_identity
import admin_panel
import federation_admin_api
import federation_protocol
import federation_runtime
import state_store


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('config', 'secrets', 'data/referral-control'):
            (self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        state_store.write_json(self.root / 'installation.json', {'domain': 'example.org'})
        state_store.write_json(
            self.root / 'config/federation-bot.json',
            {
                'user_id': '@admin-panel:example.org',
                'device_id': 'FIXED',
                'access_token': 'fictional',
                'administrator_mxids': ['@matrix-admin:example.org'],
            },
        )
        self.mxid = '@personal:example.org'
        self.users = {'username': 'personal', 'user_id': 'FICTIONAL_USER_ID', 'admin': False}

    def tearDown(self):
        self.temp.cleanup()

    def test_registered_personal_account_receives_real_rights_and_original_access_stays(self):
        with (
            patch.object(admin_identity, 'api', return_value=self.users) as api,
            patch.object(admin_identity.lifecycle, 'dc', return_value='') as dc,
        ):
            result = admin_identity.access(self.root, 'add', self.mxid)
        self.assertEqual(result['administrators'], ['@matrix-admin:example.org', self.mxid])
        self.assertEqual(
            api.call_args_list[1].args,
            (
                self.root,
                'set-admin',
                self.mxid,
                'FICTIONAL_USER_ID',
                True,
            ),
        )
        config = json.loads((self.root / 'config/federation-bot.json').read_text())
        self.assertEqual(config['device_id'], 'FIXED')
        self.assertEqual(config['access_token'], 'fictional')
        self.assertEqual(config['administrator_mxids'], result['administrators'])
        dc.assert_called_once_with('up', '-d', '--force-recreate', '--no-deps', 'federation-bot')
        self.assertFalse((self.root / 'secrets/admin-access-pending.json').exists())
        backend = Mock(root=self.root)
        admin_panel.authorize(backend, self.mxid)
        backend.authorize.assert_called_once_with(self.mxid)
        with self.assertRaises(federation_protocol.ProtocolError):
            admin_panel.authorize(backend, '@unlisted:example.org')

    def test_missing_foreign_and_service_accounts_do_not_publish_or_create_accounts(self):
        for mxid in (
            '@someone:foreign.example',
            '@admin-panel:example.org',
            '@server-guide:example.org',
        ):
            with self.subTest(mxid=mxid), patch.object(admin_identity, 'api') as api:
                with self.assertRaises(ValueError):
                    admin_identity.access(self.root, 'add', mxid)
                api.assert_not_called()
        with (
            patch.object(admin_identity, 'api', side_effect=RuntimeError('not found')),
            patch.object(admin_identity, 'publish') as publish,
        ):
            with self.assertRaises(RuntimeError):
                admin_identity.access(self.root, 'add', self.mxid)
            publish.assert_not_called()
        self.assertFalse((self.root / 'secrets/admin-access-pending.json').exists())

    def test_interrupted_change_pins_account_id_and_only_explicit_recovery_repeats_it(self):
        with (
            patch.object(admin_identity, 'api', return_value=self.users),
            patch.object(admin_identity, 'publish', side_effect=RuntimeError('interrupted')),
        ):
            with self.assertRaises(RuntimeError):
                admin_identity.access(self.root, 'add', self.mxid)
        pending = self.root / 'secrets/admin-access-pending.json'
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        with patch.object(admin_identity, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'recover'):
                admin_identity.access(self.root, 'add', '@different:example.org')
            api.assert_not_called()
        with (
            patch.object(admin_identity, 'api', return_value=self.users) as api,
            patch.object(admin_identity.lifecycle, 'dc', return_value=''),
        ):
            result = admin_identity.access(self.root, 'recover')
        api.assert_called_once_with(self.root, 'set-admin', self.mxid, 'FICTIONAL_USER_ID', True)
        self.assertIn(self.mxid, result['administrators'])
        self.assertFalse(pending.exists())
        self.assertEqual(admin_identity.access(self.root, 'recover')['status'], 'unchanged')

    def test_removal_revokes_only_selected_account_and_protects_original(self):
        with (
            patch.object(admin_identity, 'api', return_value=self.users),
            patch.object(admin_identity.lifecycle, 'dc', return_value=''),
        ):
            admin_identity.access(self.root, 'add', self.mxid)
            result = admin_identity.access(self.root, 'remove', self.mxid)
        self.assertEqual(result['administrators'], ['@matrix-admin:example.org'])
        with self.assertRaises(ValueError):
            admin_identity.access(self.root, 'remove', '@matrix-admin:example.org')

    def test_remove_recovery_recreates_bot_even_after_access_list_was_published(self):
        with (
            patch.object(admin_identity, 'api', return_value=self.users),
            patch.object(admin_identity.lifecycle, 'dc', return_value=''),
        ):
            admin_identity.access(self.root, 'add', self.mxid)
        with (
            patch.object(admin_identity, 'api', return_value=self.users),
            patch.object(admin_identity.lifecycle, 'dc', side_effect=RuntimeError('interrupted')),
        ):
            with self.assertRaises(RuntimeError):
                admin_identity.access(self.root, 'remove', self.mxid)
        self.assertNotIn(self.mxid, admin_identity.access(self.root, 'list')['administrators'])
        with (
            patch.object(admin_identity, 'api', return_value=self.users) as api,
            patch.object(admin_identity.lifecycle, 'dc', return_value='') as dc,
        ):
            admin_identity.access(self.root, 'recover')
        api.assert_called_once_with(self.root, 'set-admin', self.mxid, 'FICTIONAL_USER_ID', False)
        dc.assert_called_once()
        self.assertFalse((self.root / 'secrets/admin-access-pending.json').exists())

    def test_choose_cannot_publish_twenty_first_administrator(self):
        path = self.root / 'config/admin-panel-identity.json'
        state_store.write_json(
            path,
            {
                'user_id': '@admin-panel:example.org',
                'administrator_mxid': '@matrix-admin:example.org',
                'administrator_mxids': ['@matrix-admin:example.org']
                + ['@personal' + str(index) + ':example.org' for index in range(19)],
            },
        )
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, '20'):
            admin_identity.choose(self.root, 'admin-panel', '@another:example.org')
        self.assertEqual(path.read_bytes(), before)

    def test_cap_is_checked_after_adding_missing_primary_to_previous_access_list(self):
        state_store.write_json(
            self.root / 'config/admin-panel-identity.json',
            {
                'user_id': '@admin-panel:example.org',
                'administrator_mxid': '@matrix-admin:example.org',
                'administrator_mxids': [
                    '@personal' + str(index) + ':example.org' for index in range(20)
                ],
            },
        )
        with self.assertRaisesRegex(RuntimeError, 'access list'):
            federation_runtime.identity(self.root, {'domain': 'example.org'})

    def test_previous_multi_account_configuration_survives_identity_read(self):
        path = self.root / 'config/federation-bot.json'
        config = json.loads(path.read_text())
        config['administrator_mxids'].append(self.mxid)
        state_store.write_json(path, config)
        result = federation_runtime.identity(self.root, {'domain': 'example.org'})
        self.assertEqual(result['administrator_mxids'], config['administrator_mxids'])
        self.assertEqual(
            admin_identity.access(self.root, 'list')['administrators'],
            config['administrator_mxids'],
        )

    def test_live_rights_api_refuses_changed_locked_or_missing_account_before_mutation(self):
        user = {'id': 'FICTIONAL_USER_ID', 'attributes': {'username': 'personal', 'admin': False}}
        request = Mock(
            side_effect=[
                (200, {'data': user}),
                (
                    200,
                    {
                        'data': {
                            **user,
                            'attributes': {'username': 'personal', 'admin': True},
                        }
                    },
                ),
            ]
        )
        result = federation_admin_api.account(
            request, 'personal', 'set-admin', True, 'FICTIONAL_USER_ID'
        )
        self.assertTrue(result['admin'])
        self.assertEqual(
            request.call_args_list[1].args,
            (
                '/api/admin/v1/users/FICTIONAL_USER_ID/set-admin',
                {'admin': True},
            ),
        )
        for status, candidate in (
            (404, None),
            (200, {'data': {**user, 'id': 'CHANGED'}}),
            (200, {'data': {**user, 'attributes': {'username': 'personal', 'locked_at': 1}}}),
        ):
            with self.subTest(status=status, candidate=candidate):
                request = Mock(return_value=(status, candidate))
                with self.assertRaises(RuntimeError):
                    federation_admin_api.account(
                        request, 'personal', 'set-admin', True, 'FICTIONAL_USER_ID'
                    )
                self.assertEqual(request.call_count, 1)

    def test_revoke_does_not_accept_locked_response_that_still_has_admin_rights(self):
        user = {'id': 'FICTIONAL_USER_ID', 'attributes': {'username': 'personal', 'admin': True}}
        request = Mock(
            side_effect=[
                (200, {'data': user}),
                (
                    200,
                    {
                        'data': {
                            **user,
                            'attributes': {**user['attributes'], 'locked_at': 1},
                        }
                    },
                ),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, 'Privilege change'):
            federation_admin_api.account(
                request, 'personal', 'set-admin', False, 'FICTIONAL_USER_ID'
            )


if __name__ == '__main__':
    unittest.main()
