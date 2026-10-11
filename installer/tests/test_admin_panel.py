import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_panel
import releases
import release_update
import state_store
from welcome_message import DEFAULT_WELCOME

EXPECTED_WELCOME = (
    'Привет! Добро пожаловать в example.org.\n\n'
    'Я — серверный помощник. В этот чат будут приходить новости и объявления сервера.\n\n'
    'Чтобы пригласить друга, открой реферального бота и отправь команду «пригласить». '
    'Он выдаст код для регистрации.\n'
    'https://matrix.to/#/@referral-bot:example.org\n\n'
    'Я на сообщения не отвечаю. Если нужна помощь, напиши администратору:\n'
    'https://matrix.to/#/@matrix-admin:example.org'
)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir(mode=0o700)
        self.now = 2000000000
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'example.org', 'applied_release_version': '1.1.0'},
        )
        state_store.write_json(self.root / 'release.json', {'version': '1.1.0'})
        self.panel = admin_panel.Panel(self.root, lambda: self.now)
        self.users = []
        self.panel.user_page = lambda cursor: [u for u in self.users if u['mxid'] > cursor]

    def tearDown(self):
        self.temp.cleanup()

    def pending(self):
        return self.panel.handle('panel-outbox', None, None)['items']

    def test_new_registration_welcome_owner_notification_and_restart_dedupe(self):
        self.users = [
            {'mxid': '@alice:example.org', 'created': self.now, 'active': True},
            {'mxid': '@old:example.org', 'created': self.now - 100, 'active': True},
        ]
        self.panel.observe_users()
        self.panel.observe_users()
        items = self.pending()
        self.assertEqual(len(items), 2)
        self.assertEqual({i['via'] for i in items}, {'admin', 'welcome'})
        self.assertEqual(next(i['body'] for i in items if i['via'] == 'welcome'), EXPECTED_WELCOME)
        self.assertIn('@alice:example.org', next(i['body'] for i in items if i['via'] == 'admin'))
        for item in items:
            self.panel.handle('panel-ack', {'id': item['id'], 'delivered': True}, None)
        restarted = admin_panel.Panel(self.root, lambda: self.now)
        restarted.user_page = self.panel.user_page
        restarted.observe_users()
        self.assertEqual(restarted.handle('panel-outbox', None, None)['items'], [])

    def test_default_uses_portable_site_name_and_saved_bot_and_owner_in_both_config_formats(self):
        for split in (False, True):
            with self.subTest(split=split):
                if split:
                    (self.root / 'settings.env').unlink()
                    (self.root / 'settings').mkdir(mode=0o700)
                    state_store.write_text(
                        self.root / 'settings/server.env', 'DOMAIN=example.org\n'
                    )
                    state_store.write_text(
                        self.root / 'settings/site.env', 'SITE_NAME=Наш сервер\n'
                    )
                else:
                    state_store.write_text(
                        self.root / 'settings.env', 'DOMAIN=example.org\nSITE_NAME=Наш сервер\n'
                    )
                state_store.write_json(
                    self.root / 'config/referral-bot.json',
                    {'user_id': '@invitations:example.org', 'access_token': 'FICTIONAL_SECRET'},
                )
                state_store.write_json(
                    self.root / 'config/admin-panel-identity.json',
                    {'administrator_mxid': '@owner:example.org'},
                )
                self.assertEqual(
                    self.panel.welcome_text(DEFAULT_WELCOME),
                    EXPECTED_WELCOME.replace('в example.org.', 'в Наш сервер.')
                    .replace('@referral-bot:', '@invitations:')
                    .replace('@matrix-admin:', '@owner:'),
                )

    def test_adopted_display_name_is_separate_from_matrix_identity(self):
        state_store.write_json(
            self.root / 'installation.json',
            {
                'domain': 'matrix.example.org',
                'backend': 'existing-compose',
                'existing': {'features': {'primary_domain': 'example.org'}},
            },
        )
        state_store.write_json(
            self.root / 'config/referral-bot.json', {'user_id': '@referral-bot:matrix.example.org'}
        )
        # Legacy owner configuration remains supported before identity.json exists.
        state_store.write_json(
            self.root / 'config/federation-bot.json',
            {'administrator_mxids': ['@owner:matrix.example.org']},
        )
        panel = admin_panel.Panel(self.root, lambda: self.now)
        expected = EXPECTED_WELCOME.replace(':example.org', ':matrix.example.org').replace(
            '@matrix-admin:', '@owner:'
        )
        self.assertEqual(panel.welcome_text(DEFAULT_WELCOME), expected)
        site = self.root / '.misbot/features/site.json'
        site.parent.mkdir(mode=0o700, parents=True)
        for name, title in [('Наш сервер', 'Наш сервер'), ('', 'example.org')]:
            state_store.write_json(site, {'SITE_NAME': name})
            self.assertEqual(
                panel.welcome_text(DEFAULT_WELCOME),
                expected.replace('в example.org.', 'в ' + title + '.'),
            )

    def test_custom_edit_survives_restart_and_does_not_rewrite_queued_greetings(self):
        self.users = [{'mxid': '@alice:example.org', 'created': self.now, 'active': True}]
        self.panel.observe_users()
        queued = self.pending()
        custom = 'Привет, MiXeD! {domain}\n{"data": 1}\n{unknown}'
        self.assertEqual(self.panel.handle('panel-welcome', custom, '$w'), {'saved': True})
        restarted = admin_panel.Panel(self.root, lambda: self.now)
        self.assertEqual(restarted.handle('panel-welcome', None, '$show'), {'text': custom})
        self.assertEqual(self.pending(), queued)
        self.assertEqual(
            restarted.welcome_text(custom), 'Привет, MiXeD! example.org\n{"data": 1}\n{unknown}'
        )

    def test_substituted_values_are_not_treated_as_more_template_fields(self):
        state_store.write_text(
            self.root / 'settings.env', 'DOMAIN=example.org\nSITE_NAME=Наш {domain}\n'
        )
        self.assertEqual(
            self.panel.welcome_text('{server_name} / {domain}'), 'Наш {domain} / example.org'
        )

    def test_invalid_referral_identity_keeps_registration_retryable_without_messages(self):
        path = self.root / 'config/referral-bot.json'
        state_store.write_json(path, {'user_id': 'FICTIONAL_SECRET'})
        self.users = [{'mxid': '@alice:example.org', 'created': self.now, 'active': True}]
        with self.assertRaisesRegex(RuntimeError, '^Invalid local welcome identity$'):
            self.panel.observe_users()
        self.assertEqual(self.pending(), [])
        state_store.write_json(path, {'user_id': '@referral-bot:example.org'})
        self.panel.observe_users()
        self.assertEqual(len(self.pending()), 2)

    def test_invalid_owner_identity_is_not_exposed_in_the_message_or_error(self):
        state_store.write_json(
            self.root / 'config/admin-panel-identity.json',
            {'administrator_mxid': 'FICTIONAL_SECRET'},
        )
        with self.assertRaisesRegex(RuntimeError, '^Invalid local welcome identity$'):
            self.panel.welcome_text(DEFAULT_WELCOME)

    def test_saved_referral_identity_is_excluded_from_registration_and_statistics(self):
        state_store.write_json(
            self.root / 'config/referral-bot.json', {'user_id': '@invitations:example.org'}
        )
        statements = []

        def query(sql):
            statements.append(sql)
            return [] if 'json_agg' in sql else {'messages': 0, 'registrations': 0}

        self.panel.query = query
        self.panel.user_page('')
        self.panel.statistics('day')
        for sql in statements:
            self.assertIn('@invitations:example.org', sql)

    def test_registration_off_does_not_disable_custom_welcome(self):
        self.panel.handle('panel-preferences', {'key': 'registrations', 'enabled': False}, '$p')
        self.panel.handle('panel-welcome', 'Привет на {domain}!', '$w')
        self.users = [{'mxid': '@alice:example.org', 'created': self.now, 'active': True}]
        self.panel.observe_users()
        items = self.pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['body'], 'Привет на example.org!')
        self.assertEqual(items[0]['via'], 'welcome')

    def test_broadcast_active_users_header_and_replayed_confirmation(self):
        self.users = [
            {'mxid': '@alice:example.org', 'created': self.now - 100, 'active': True},
            {'mxid': '@blocked:example.org', 'created': self.now - 100, 'active': False},
        ]
        first = self.panel.handle('panel-broadcast', 'Новости', '$broadcast')
        self.panel.handle('panel-broadcast', 'Новости', '$broadcast')
        self.assertEqual(first['queued'], 1)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(
            self.pending()[0]['body'], 'Сообщение от администратора сервера example.org,\n\nНовости'
        )

    def test_latest_minor_only_once_and_three_day_interval(self):
        release = {
            'version': '1.3.2',
            'notes': 'Нововведения',
            'url': 'https://github.com/stepagin/misbot/releases/tag/v1.3.2',
            'assets': [],
        }
        with patch.object(releases, 'latest', return_value=release) as fetch:
            self.panel.check_releases()
            self.panel.check_releases()
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(self.pending()), 1)
            self.assertIn('1.3.2', self.pending()[0]['body'])
            self.now += releases.INTERVAL
            self.panel.check_releases()
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(self.pending()), 1)
            self.now += releases.INTERVAL
            fetch.return_value = {**release, 'version': '1.4.0'}
            self.panel.check_releases()
            self.assertEqual(len(self.pending()), 2)

    def test_patch_silent_but_manual_comparison_reports_it(self):
        release = {'version': '1.1.9', 'notes': 'fix', 'url': 'example', 'assets': []}
        with patch.object(releases, 'latest', return_value=release):
            result = self.panel.handle('panel-version', None, '$v')
        self.assertTrue(result['update_available'])
        self.assertEqual(self.pending(), [])

    def test_disabled_notifications_and_release_fetch_failure_are_durable(self):
        self.panel.handle('panel-preferences', {'key': 'updates', 'enabled': False}, '$p')
        with patch.object(
            releases,
            'latest',
            return_value={'version': '1.3.0', 'notes': 'notes', 'url': 'example', 'assets': []},
        ):
            self.panel.check_releases(True)
        self.assertEqual(self.pending(), [])
        with patch.object(releases, 'latest', side_effect=RuntimeError('offline')) as fetch:
            with self.assertRaises(RuntimeError):
                self.panel.check_releases(True)
            self.panel.check_releases()
            self.assertEqual(fetch.call_count, 1)

    def test_monthly_report_calendar_window_once_and_disabled(self):
        # Initial state is May 2033; advance into June.
        from datetime import datetime, timezone

        self.now = int(datetime(2033, 6, 1, tzinfo=timezone.utc).timestamp())
        self.panel.user_page = lambda cursor: []
        self.panel.statistics = lambda period, start=None, end=None: dict(
            messages=12, registrations=3, note='note'
        )
        with patch.object(releases, 'latest', return_value=None):
            self.panel.maintenance()
            self.panel.maintenance()
        self.assertEqual(len(self.pending()), 1)
        self.assertIn('Сообщений: 12', self.pending()[0]['body'])
        self.panel.handle('panel-preferences', {'key': 'statistics', 'enabled': False}, '$off')
        self.now = int(datetime(2033, 7, 1, tzinfo=timezone.utc).timestamp())
        self.panel.maintenance()
        self.assertEqual(self.pending(), [])

    def test_stats_query_reads_only_metadata_excludes_bot_rooms_and_no_fake_call_counts(self):
        seen = []
        self.panel.query = lambda sql: seen.append(sql) or dict(messages=5, registrations=2)
        result = self.panel.statistics('week')
        self.assertIsNone(result['calls'])
        self.assertIsNone(result['average_call_seconds'])
        self.assertIn('room_memberships', seen[0])
        self.assertIn('m.room.encrypted', seen[0])
        self.assertNotIn('event_json', seen[0])

    def test_only_exact_owner_even_if_other_account_has_admin_rights(self):
        class Backend:
            root = self.root

            def authorize(self, actor):
                pass

        with self.assertRaises(RuntimeError):
            admin_panel.authorize(Backend(), '@another-admin:example.org')
        admin_panel.authorize(Backend(), '@matrix-admin:example.org')

    def test_persisted_update_result_sent_once_after_restart(self):
        state_store.write_json(
            release_update.job_path(self.root),
            {
                'id': 'fake-job',
                'before': '1.1.0',
                'version': '1.2.0',
                'state': 'failed',
                'result': {'installed': '1.1.0', 'detail': 'disk check failed'},
            },
        )
        release_update.result_notice(self.panel)
        release_update.result_notice(admin_panel.Panel(self.root, lambda: self.now))
        self.assertEqual(len(self.pending()), 1)
        self.assertIn('disk check failed', self.pending()[0]['body'])

    def test_detached_worker_reports_disk_failure_and_recovers_a_dead_process(self):
        job = {
            'id': 'disk-job',
            'before': '1.1.0',
            'version': '1.2.0',
            'state': 'queued',
            'created': self.now,
        }
        state_store.write_json(release_update.job_path(self.root), job)
        with patch.object(
            release_update, 'apply', side_effect=RuntimeError('Недостаточно свободного диска')
        ):
            release_update.run_job(self.root, job['id'])
        failed = json.loads(release_update.job_path(self.root).read_text())
        self.assertEqual(failed['state'], 'failed')
        self.assertIn('свободного диска', failed['result']['detail'])
        job.update(state='running', pid=999999, started='fixture')
        state_store.write_json(release_update.job_path(self.root), job)
        with patch.object(state_store, 'process_identity', side_effect=OSError('missing process')):
            release_update.recover_job(self.root)
        self.assertEqual(
            json.loads(release_update.job_path(self.root).read_text())['state'], 'interrupted'
        )

    def test_unsafe_text_and_preferences_rejected(self):
        for body in ('', 'x' * 8001):
            with self.assertRaises(ValueError):
                self.panel.handle('panel-welcome', body, '$w')
        with self.assertRaises(ValueError):
            self.panel.handle('panel-preferences', {'key': 'updates', 'enabled': 'false'}, '$p')


if __name__ == '__main__':
    unittest.main()
