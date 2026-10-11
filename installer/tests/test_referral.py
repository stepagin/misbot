"""Provisioning invariants and restoration boundaries without real credentials."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE))
control_source = (
    BUNDLE / 'referral-control'
    if (BUNDLE / 'referral-control/bridge.py').is_file()
    else BUNDLE.parent / 'bots/telegram'
)
sys.path.insert(0, str(control_source))
import referral

spec = importlib.util.spec_from_file_location(
    'referral_provision', BUNDLE / 'referral-control/provision.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class API:
    def __init__(self):
        self.users = []
        self.sessions = {}
        self.calls = []
        self.active = True

    def items(self, resource):
        return iter(self.users)

    def active_user(self, identifier):
        if not self.active:
            raise RuntimeError('inactive')
        return next(x for x in self.users if x['id'] == identifier)

    def api(self, path, method='GET', data=None):
        self.calls.append((path, method))
        if path == 'users':
            item = {'id': 'BOT-USER', 'attributes': {'username': data['username'], 'admin': False}}
            self.users.append(item)
        elif path == 'personal-sessions':
            item = {
                'id': 'SESSION-' + str(len(self.sessions) + 1),
                'attributes': {'scope': data['scope'], 'access_token': 'test-only-token'},
            }
            self.sessions[item['id']] = item
        else:
            item = self.sessions[path.split('/')[1]]
        return {'data': item}


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metadata = {'domain': 'chat.example.org'}
        self.client, self.policy = referral.configure(self.root, self.metadata, {}, preview=True)
        self.control = self.root / 'data/referral-control'
        self.api = API()

    def tearDown(self):
        self.temp.cleanup()

    def test_domain_session_and_non_admin_scope_are_reused(self):
        first = module.provision(self.control, self.api)
        second = module.provision(self.control, self.api)
        self.assertEqual(first, second)
        self.assertEqual(len(self.api.users), 1)
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(first['config']['user_id'], '@referral-bot:chat.example.org')
        self.assertEqual(first['config']['device_id'], referral.DEVICE_ID)
        self.assertEqual(first['config']['invitation_lifetime_seconds'], 604800)
        self.assertEqual(first['config']['site_name'], 'chat.example.org')
        self.assertEqual(first['config']['site_url'], 'https://chat.example.org/')
        self.assertEqual(first['config']['web_url'], 'https://web.chat.example.org')
        self.assertNotIn('urn:mas:admin', first['metadata']['scope'])
        self.assertNotIn('urn:synapse:admin', first['metadata']['scope'])
        self.assertEqual((self.control / 'session.json').stat().st_mode & 0o777, 0o600)

    def test_existing_unknown_username_is_preserved(self):
        self.api.users = [
            {'id': 'SOMEONE', 'attributes': {'username': 'referral-bot', 'admin': False}}
        ]
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            module.provision(self.control, self.api)
        self.assertEqual(self.api.sessions, {})
        self.assertEqual(len(self.api.users), 1)

    def test_admin_and_welcome_identities_use_fixed_separate_devices_and_sessions(self):
        for options, tag, device in (
            ({'federation': True}, 'admin-panel', 'MATRIX-FEDERATION-01'),
            ({'welcome': True}, 'server-guide', 'MATRIX-WELCOME-01'),
        ):
            with self.subTest(tag=tag):
                api = API()
                first = module.provision(self.control, api, **options)
                second = module.provision(self.control, api, **options)
                self.assertEqual(first, second)
                self.assertEqual(len(api.sessions), 1)
                self.assertEqual(first['config']['user_id'], '@' + tag + ':chat.example.org')
                self.assertEqual(first['config']['device_id'], device)
                self.assertNotIn('urn:mas:admin', first['metadata']['scope'])
                with self.assertRaisesRegex(RuntimeError, 'immutable'):
                    module.provision(self.control, api, username='renamed-bot', **options)

    def test_saved_legacy_admin_tag_is_reused_without_creating_a_new_account(self):
        first = module.provision(self.control, self.api, federation=True, username='federation-bot')
        second = module.provision(self.control, self.api, federation=True)
        self.assertEqual(first, second)
        self.assertEqual(len(self.api.users), 1)
        self.assertEqual(second['config']['user_id'], '@federation-bot:chat.example.org')

    def test_inactive_bot_or_admin_role_is_not_repurposed(self):
        module.provision(self.control, self.api)
        self.api.active = False
        with self.assertRaises(RuntimeError):
            module.provision(self.control, self.api)
        self.api.active = True
        self.api.users[0]['attributes']['admin'] = True
        with self.assertRaisesRegex(RuntimeError, 'ordinary'):
            module.provision(self.control, self.api)
        self.assertEqual(len(self.api.sessions), 1)

    def test_recovery_after_user_creation_reuses_owned_user(self):
        original = self.api.api

        def interrupted(path, **kwargs):
            if path == 'personal-sessions':
                raise RuntimeError('temporary failure')
            return original(path, **kwargs)

        self.api.api = interrupted
        with self.assertRaises(RuntimeError):
            module.provision(self.control, self.api)
        self.api.api = original
        module.provision(self.control, self.api)
        self.assertEqual(len(self.api.users), 1)

    def test_seeded_service_secret_is_not_silently_regenerated(self):
        path = self.root / 'secrets/referral-client.json'
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            referral.configure(self.root, self.metadata, {}, preview=True)
        self.assertFalse(path.exists())

    def test_missing_provisioned_state_requires_restore(self):
        self.metadata['referral_provisioned'] = True
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            referral.validate_saved(self.root, self.metadata)

    def test_saved_crypto_state_requires_restore_instead_of_new_keys(self):
        self.metadata['referral_crypto_initialized'] = True
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            referral.validate_saved(self.root, self.metadata)

    def test_referral_container_never_mounts_machine_credentials(self):
        services = referral.services()
        bot = services['referral-bot']
        self.assertEqual(bot['user'], '10002:10001')
        self.assertEqual(bot['cap_drop'], ['ALL'])
        self.assertNotIn('ports', bot)
        self.assertNotIn('ports', services['referral-control'])
        self.assertTrue(
            all(
                'bot-config.json' not in p and 'secrets' not in p and 'docker.sock' not in p
                for p in bot['volumes']
            )
        )

    def test_changed_policy_updates_invitation_defaults_without_changing_session(self):
        first = module.provision(self.control, self.api)
        policy = json.loads((self.control / 'bot-policy.json').read_text())
        policy['servers']['chat.example.org'].update(
            invitation_lifetime_seconds=3600, invitation_usage_limit=3
        )
        (self.control / 'bot-policy.json').write_text(json.dumps(policy))
        second = module.provision(self.control, self.api)
        self.assertEqual(first['config']['access_token'], second['config']['access_token'])
        self.assertEqual(second['config']['invitation_lifetime_seconds'], 3600)
        self.assertEqual(second['config']['invitation_usage_limit'], 3)
        self.assertEqual(len(self.api.sessions), 1)

    def test_new_installation_uses_site_name_in_invitation_config(self):
        referral.configure(self.root, self.metadata, {'SITE_NAME': 'Наш сервер'}, preview=True)
        config = json.loads((self.root / 'config/referral-bot.json').read_text())
        self.assertEqual(config['site_name'], 'Наш сервер')
        self.assertEqual(
            module.provision(self.control, self.api)['config']['site_name'], 'Наш сервер'
        )

    def test_existing_config_updates_presentation_without_replacing_identity_or_data(self):
        first = module.provision(self.control, self.api)
        config = self.root / 'config/referral-bot.json'
        referral.private(config, dict(first['config'], custom_field='preserved'))
        referral.private(self.root / 'secrets/referral-session.json', first['metadata'])
        database = self.control / 'data/bot-control.sqlite3'
        database.write_bytes(b'fictional unchanged database fixture')
        checkpoint = self.root / 'data/referral-bot/state.json'
        checkpoint.write_text('{"processed:$fictional": 1}')
        self.metadata['referral_provisioned'] = True
        journal = self.control / 'session.json'
        before = {path: path.read_bytes() for path in (database, checkpoint, journal)}
        referral.configure(self.root, self.metadata, {'SITE_NAME': 'Новое имя'}, preview=True)
        updated = json.loads(config.read_text())
        for key in ('user_id', 'access_token', 'device_id', 'homeserver_url'):
            self.assertEqual(updated[key], first['config'][key])
        self.assertEqual(updated['site_name'], 'Новое имя')
        self.assertEqual(updated['custom_field'], 'preserved')
        second = module.provision(self.control, self.api)
        self.assertEqual(second['config']['site_name'], 'Новое имя')
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        content, modified = config.read_bytes(), config.stat().st_mtime_ns
        referral.configure(self.root, self.metadata, {'SITE_NAME': 'Новое имя'}, preview=True)
        self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns), (content, modified))

    def test_legacy_policy_without_presentation_fields_keeps_existing_session(self):
        policy = json.loads((self.control / 'bot-policy.json').read_text())
        for key in ('site_name', 'site_url', 'web_url'):
            policy['servers'][self.metadata['domain']].pop(key)
        (self.control / 'bot-policy.json').write_text(json.dumps(policy))
        first = module.provision(self.control, self.api)
        second = module.provision(self.control, self.api)
        self.assertEqual(first, second)
        self.assertNotIn('site_name', second['config'])
        self.assertEqual(len(self.api.sessions), 1)

    def test_adopted_site_and_web_chat_can_differ_from_matrix_identity(self):
        policy = json.loads((self.control / 'bot-policy.json').read_text())
        policy['servers'][self.metadata['domain']].update(
            site_name='Наш сервер',
            site_url='https://community.example.org/',
            web_url='https://chat.example.org',
        )
        (self.control / 'bot-policy.json').write_text(json.dumps(policy))
        result = module.provision(self.control, self.api)
        self.assertEqual(result['config']['site_name'], 'Наш сервер')
        self.assertEqual(result['config']['site_url'], 'https://community.example.org/')
        self.assertEqual(result['config']['web_url'], 'https://chat.example.org')
        self.assertEqual(result['config']['server_name'], 'chat.example.org')
        self.assertEqual(result['config']['user_id'], '@referral-bot:chat.example.org')
        for options in ({'federation': True}, {'welcome': True}):
            separate = module.provision(self.control, API(), **options)
            self.assertNotIn('site_name', separate['config'])


if __name__ == '__main__':
    unittest.main()
