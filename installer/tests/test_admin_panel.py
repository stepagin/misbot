from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_panel
import releases
import release_update
import state_store
from welcome_message import DEFAULT_WELCOME, LEGACY_WELCOMES

EXPECTED_WELCOME = (
    'Здравствуйте. Добро пожаловать в example.org.\n\n'
    'Я — серверный помощник. В этот чат будут приходить новости и объявления сервера.\n\n'
    'Чтобы пригласить друга, откройте реферального бота и отправьте команду «пригласить». '
    'Он выдаст код для регистрации.\n'
    'https://matrix.to/#/@referral-bot:example.org\n\n'
    'Я не отвечаю на сообщения. Если нужна помощь, напишите администратору:\n'
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

    def test_notices_address_each_selected_admin_and_keep_saved_queue_deduplication(self):
        state_store.write_json(
            self.root / 'config/admin-panel-identity.json',
            {
                'administrator_mxid': '@matrix-admin:example.org',
                'administrator_mxids': ['@matrix-admin:example.org', '@personal:example.org'],
            },
        )
        self.panel.notice('several-admins', 'Проверка сервера завершена.')
        self.panel.notice('several-admins', 'Проверка сервера завершена.')
        items = self.pending()
        self.assertEqual(
            {item['recipient'] for item in items},
            {
                '@matrix-admin:example.org',
                '@personal:example.org',
            },
        )
        self.assertEqual(len(items), 2)

    def test_server_tasks_remain_bound_to_requesting_personal_administrator(self):
        actor = '@personal:example.org'
        with patch('server_management.queue_operation', return_value={'status': 'queued'}) as queue:
            self.panel.handle(
                'panel-server-operation', {'operation': 'restart'}, '$personal', actor=actor
            )
        queue.assert_called_once_with(self.root, 'restart', actor=actor, event_id='$personal')
        with patch(
            'server_management.operation_status', return_value={'status': 'complete'}
        ) as result:
            self.panel.handle('panel-server-result', 'SO-fictional', '$result', actor=actor)
        result.assert_called_once_with(self.root, 'SO-fictional', actor=actor)

    def test_waiting_invitations_retry_within_five_seconds_without_starving_other_users(self):
        with self.panel.database() as db:
            for index in range(40):
                self.panel.enqueue(
                    db,
                    'welcome:test' + str(index),
                    '@user' + str(index) + ':example.org',
                    'welcome',
                    'Здравствуйте',
                )
        visited = set()
        for _ in range(20):
            items = self.pending()
            self.assertEqual(len(items), 2)
            self.assertFalse(visited & {item['id'] for item in items})
            visited.update(item['id'] for item in items)
            for item in items:
                self.panel.handle(
                    'panel-ack',
                    {'id': item['id'], 'delivered': False, 'waiting_for_join': True},
                    None,
                )
            self.now += 5
        self.assertEqual(len(visited), 40)
        # Restart keeps pending work. Earlier retry deadlines now win, so the
        # two first invitations can deliver once their recipients have joined.
        restarted = admin_panel.Panel(self.root, lambda: self.now)
        ready = restarted.handle('panel-outbox', None, None)['items']
        self.assertEqual(
            {item['recipient'] for item in ready}, {'@user0:example.org', '@user1:example.org'}
        )
        for item in ready:
            restarted.handle('panel-ack', {'id': item['id'], 'delivered': True}, None)
        self.assertFalse({item['id'] for item in ready} & {item['id'] for item in self.pending()})

    def test_pending_join_receipt_keeps_queue_and_uses_short_deadline(self):
        self.panel.notice('notice', 'Проверено')
        item = self.pending()[0]
        self.panel.handle(
            'panel-ack', {'id': item['id'], 'delivered': False, 'waiting_for_join': True}, None
        )
        self.assertEqual(self.pending(), [])
        self.now += 4
        self.assertEqual(self.pending(), [])
        self.now += 1
        self.assertEqual(self.pending(), [item])
        self.panel.handle('panel-ack', {'id': item['id'], 'delivered': False}, None)
        self.now += 299
        self.assertEqual(self.pending(), [])
        self.now += 1
        self.assertEqual(self.pending(), [item])
        for argument in (
            {'id': item['id'], 'delivered': False, 'waiting_for_join': 'yes'},
            {'id': item['id'], 'delivered': True, 'waiting_for_join': True},
        ):
            with self.assertRaisesRegex(ValueError, 'Invalid delivery receipt'):
                self.panel.handle('panel-ack', argument, None)
        self.assertEqual(self.pending(), [item])

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

    def test_start_upgrades_old_stock_text_without_greeting_known_users_again(self):
        self.users = [{'mxid': '@alice:example.org', 'created': self.now, 'active': True}]
        self.panel.observe_users()
        for item in self.pending():
            self.panel.handle('panel-ack', {'id': item['id'], 'delivered': True}, None)
        self.panel.handle('panel-welcome', LEGACY_WELCOMES[2], '$old-stock')
        before = self.panel.settings()
        restarted = admin_panel.Panel(self.root, lambda: self.now)
        self.assertEqual(restarted.settings(), before | {'welcome': EXPECTED_WELCOME})
        restarted.user_page = self.panel.user_page
        restarted.observe_users()
        self.assertEqual(self.pending(), [])
        self.users.append({'mxid': '@bob:example.org', 'created': self.now, 'active': True})
        restarted.observe_users()
        greetings = [item for item in self.pending() if item['via'] == 'welcome']
        self.assertEqual(len(greetings), 1)
        self.assertEqual(greetings[0]['recipient'], '@bob:example.org')
        self.assertEqual(greetings[0]['body'], EXPECTED_WELCOME)

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
            self.pending()[0]['body'], 'Сообщение от администратора сервера example.org:\n\nНовости'
        )

    def test_latest_release_only_once_and_three_day_interval(self):
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

    def test_patch_notification_matches_manual_comparison(self):
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'example.org', 'applied_release_version': '1.2.0'},
        )
        release = {'version': '1.2.1', 'notes': 'fix', 'url': 'https://example.org', 'assets': []}
        with patch.object(releases, 'latest', return_value=release):
            result = self.panel.handle('panel-version', None, '$v')
        self.assertTrue(result['update_available'])
        self.assertEqual(len(self.pending()), 1)
        self.assertIn('Misbot 1.2.1. Установлена 1.2.0.', self.pending()[0]['body'])
        self.assertIn('`sudo misbot update latest`', self.pending()[0]['body'])

    def test_stable_release_after_installed_build_is_notified(self):
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'example.org', 'applied_release_version': '1.2.0-build.123'},
        )
        release = {'version': '1.2.0', 'notes': 'stable', 'url': 'https://example.org'}
        with patch.object(releases, 'latest', return_value=release):
            self.panel.check_releases()
        self.assertEqual(len(self.pending()), 1)
        self.assertIn('Misbot 1.2.0. Установлена 1.2.0-build.123.', self.pending()[0]['body'])

    def test_major_release_notification_matches_manual_comparison(self):
        release = {'version': '2.0.0', 'notes': 'major', 'url': 'https://example.org'}
        with patch.object(releases, 'latest', return_value=release):
            result = self.panel.handle('panel-version', None, '$v')
        self.assertTrue(result['update_available'])
        self.assertEqual(len(self.pending()), 1)
        self.assertIn('Misbot 2.0.0. Установлена 1.1.0.', self.pending()[0]['body'])

    def test_equal_older_or_missing_release_is_not_notified(self):
        for candidate in ('1.1.0', '1.0.9', None):
            with self.subTest(candidate=candidate):
                release = (
                    {'version': candidate, 'notes': '', 'url': 'https://example.org'}
                    if candidate
                    else None
                )
                with patch.object(releases, 'latest', return_value=release):
                    result = self.panel.handle('panel-version', None, '$v')
                self.assertFalse(result['update_available'])
                self.assertEqual(self.pending(), [])
        self.assertNotIn('notified_version', self.panel.settings())

    def test_release_notification_survives_restart_without_duplicate_after_ack(self):
        release = {'version': '1.1.2', 'notes': 'fix', 'url': 'https://example.org'}
        with patch.object(releases, 'latest', return_value=release) as fetch:
            self.panel.check_releases()
            item = self.pending()[0]
            self.panel = admin_panel.Panel(self.root, lambda: self.now)
            self.panel.check_releases(True)
            self.assertEqual(self.pending(), [item])
            self.panel.handle('panel-ack', {'id': item['id'], 'delivered': True}, None)
            self.panel = admin_panel.Panel(self.root, lambda: self.now)
            self.now += releases.INTERVAL
            self.panel.check_releases()
            self.assertEqual(self.pending(), [])
            self.assertEqual(self.panel.settings()['notified_version'], '1.1.2')
            fetch.return_value = {**release, 'version': '1.1.1'}
            self.panel.check_releases(True)
            self.assertEqual(self.pending(), [])
            fetch.return_value = {**release, 'version': '1.1.3'}
            self.panel.check_releases(True)
            self.assertEqual(len(self.pending()), 1)
            self.assertIn('Misbot 1.1.3.', self.pending()[0]['body'])

    def test_disabled_notifications_and_release_fetch_failure_are_durable(self):
        self.panel.handle('panel-preferences', {'key': 'updates', 'enabled': False}, '$p')
        with patch.object(
            releases,
            'latest',
            return_value={'version': '1.1.1', 'notes': 'fix', 'url': 'https://example.org'},
        ) as fetch:
            self.panel.maintenance()
            fetch.assert_not_called()
            result = self.panel.handle('panel-version', None, '$v')
            self.assertTrue(result['update_available'])
            self.assertEqual(self.pending(), [])
            self.assertNotIn('notified_version', self.panel.settings())
            self.panel.handle('panel-preferences', {'key': 'updates', 'enabled': True}, '$on')
            self.now += releases.INTERVAL
            self.panel.maintenance()
            self.assertEqual(len(self.pending()), 1)
            self.panel.handle('panel-preferences', {'key': 'updates', 'enabled': False}, '$off')
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

    def test_update_notices_distinguish_completion_failure_and_interruption(self):
        for state, label in [
            ('complete', 'готово'),
            ('failed', 'завершение не подтверждено'),
            ('interrupted', 'прервано'),
        ]:
            with self.subTest(state=state):
                state_store.write_json(
                    release_update.job_path(self.root),
                    {
                        'id': 'fictional-' + state,
                        'before': '1.1.0',
                        'version': '1.2.0',
                        'state': state,
                        'stage': 'apply',
                        'result': {
                            'installed': '1.1.0',
                            'exit_code': 7,
                            'detail': 'Проверьте sudo misbot status.',
                        },
                    },
                )
                release_update.result_notice(self.panel)
                release_update.result_notice(self.panel)
                pending = self.pending()
                self.assertEqual(len(pending), 1)
                body = pending[0]['body']
                self.assertTrue(body.startswith(f'Обновление Misbot: {label} ({state})\n'))
                for fact in (
                    'Было: 1.1.0',
                    'запрошено: 1.2.0',
                    'установлено: 1.1.0',
                    'Этап: apply',
                    'код завершения: 7',
                    'sudo misbot status',
                ):
                    self.assertIn(fact, body)
                self.panel.handle('panel-ack', {'id': pending[0]['id'], 'delivered': True}, None)
        with self.panel.database() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM outbox WHERE state='sent'").fetchone()[0], 3
            )

    def test_updated_registration_copy_still_supports_disabling_pending_notices(self):
        self.users = [{'mxid': '@alice:example.org', 'created': self.now, 'active': True}]
        self.panel.observe_users()
        self.assertEqual(len(self.pending()), 2)
        self.assertEqual(
            next(i['body'] for i in self.pending() if i['via'] == 'admin'),
            'Новый пользователь зарегистрировался:\n@alice:example.org',
        )
        self.panel.handle('panel-preferences', {'key': 'registrations', 'enabled': False}, '$off')
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0]['via'], 'welcome')

    def test_copy_upgrade_keeps_existing_welcome_and_pending_delivery(self):
        previous = (
            EXPECTED_WELCOME.replace('Здравствуйте.', 'Привет!')
            .replace('откройте', 'открой')
            .replace('отправьте', 'отправь')
            .replace('Я не отвечаю на сообщения.', 'Я на сообщения не отвечаю.')
            .replace('напишите', 'напиши')
        )
        self.panel.handle('panel-welcome', previous, '$saved')
        self.users = [{'mxid': '@alice:example.org', 'created': self.now, 'active': True}]
        self.panel.observe_users()
        settings, pending = self.panel.settings(), self.pending()
        restarted = admin_panel.Panel(self.root, lambda: self.now)
        restarted.user_page = self.panel.user_page
        restarted.observe_users()
        self.assertEqual(restarted.settings(), settings)
        self.assertEqual(restarted.handle('panel-outbox', None, None)['items'], pending)
        self.assertEqual(next(i['body'] for i in pending if i['via'] == 'welcome'), previous)

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

    def test_referral_link_uses_saved_identity_and_never_returns_session_secrets(self):
        state_store.write_json(
            self.root / 'config/referral-bot.json',
            {'user_id': '@invites:example.org', 'access_token': 'FICTIONAL_SECRET'},
        )
        self.assertEqual(
            self.panel.handle('panel-referral', None, '$referral'),
            {'url': 'https://matrix.to/#/@invites:example.org'},
        )

    def test_server_reads_use_the_same_allowlisted_backend_and_statistics_period(self):
        run = Mock(return_value={'operation': 'statistics', 'ok': True})
        with patch.dict(sys.modules, {'server_management': SimpleNamespace(run_control=run)}):
            result = self.panel.handle(
                'panel-server-read', {'operation': 'statistics', 'period': 'week'}, '$read'
            )
        self.assertTrue(result['ok'])
        args, kwargs = run.call_args
        self.assertEqual(args[0]['stack_root'], str(self.root))
        self.assertEqual(
            set(args[0]['management_actions']), admin_panel.SERVER_READS | admin_panel.SERVER_WRITES
        )
        self.assertEqual(args[1], 'statistics')
        self.assertEqual(kwargs, {'period': 'week'})

    def test_server_writes_queue_the_original_event_and_configured_owner(self):
        state_store.write_json(
            self.root / 'config/admin-panel-identity.json',
            {'administrator_mxid': '@owner:example.org'},
        )
        queue = Mock(return_value={'operation_id': 'SO-fictional', 'status': 'queued'})
        with patch.dict(sys.modules, {'server_management': SimpleNamespace(queue_operation=queue)}):
            result = self.panel.handle(
                'panel-server-operation', {'operation': 'backup'}, '$proposal'
            )
        self.assertEqual(result['status'], 'queued')
        queue.assert_called_once_with(
            self.root, 'backup', actor='@owner:example.org', event_id='$proposal'
        )

    def test_server_dispatch_rejects_unknown_arguments_and_write_read_confusion(self):
        for action, value in (
            ('panel-server-read', {'operation': 'restart'}),
            ('panel-server-operation', {'operation': 'status'}),
            ('panel-server-read', {'operation': 'statistics', 'period': 'hour'}),
            ('panel-server-read', {'operation': 'check', 'period': 'day'}),
            ('panel-server-operation', {'operation': 'backup', 'command': 'FICTIONAL'}),
            ('panel-server-read', {'operation': ['status']}),
            ('panel-server-read', {'operation': 'statistics', 'period': False}),
            ('panel-server-read', 'status'),
            ('panel-server-read', None),
        ):
            with self.subTest(action=action, value=value), self.assertRaises(ValueError):
                self.panel.handle(action, value, '$event')
        with self.assertRaises(ValueError):
            self.panel.handle('panel-server-operation', {'operation': 'backup'}, None)

    def test_server_result_is_checked_against_the_current_owner(self):
        read = Mock(return_value={'operation_id': 'SO-fictional', 'state': 'complete'})
        with patch.dict(sys.modules, {'server_management': SimpleNamespace(operation_status=read)}):
            self.panel.handle('panel-server-result', 'SO-fictional', '$check')
        read.assert_called_once_with(self.root, 'SO-fictional', actor='@matrix-admin:example.org')
        for value in ('', ['SO-fictional'], None, 'x' * 101):
            with self.assertRaises(ValueError):
                self.panel.handle('panel-server-result', value, '$check')

    def test_narrow_rpc_checks_owner_uid_and_readiness_before_queuing_server_mutations(self):
        import federation_service

        backend = SimpleNamespace(
            root=self.root,
            guard=nullcontext,
            authorize=Mock(),
            ready=Mock(),
        )
        request = {
            'action': 'panel-server-operation',
            'argument': {'operation': 'restart'},
            'actor': '@matrix-admin:example.org',
            'event_id': '$proposal',
        }
        with patch.object(admin_panel.Panel, 'handle', return_value={'status': 'queued'}) as handle:
            for actor, uid in (
                ('@another-admin:example.org', 10003),
                ('@matrix-admin:example.org', 10001),
            ):
                with self.assertRaises(RuntimeError):
                    federation_service.bot_call(backend, dict(request, actor=actor), uid)
            handle.assert_not_called()
            backend.ready.side_effect = RuntimeError('pending transition')
            with self.assertRaises(RuntimeError):
                federation_service.bot_call(backend, request, 10003)
            handle.assert_not_called()
            backend.ready.side_effect = None
            result = federation_service.bot_call(backend, request, 10003)
            self.assertEqual(result['status'], 'queued')
            handle.assert_called_once_with(
                'panel-server-operation',
                {'operation': 'restart'},
                '$proposal',
                actor='@matrix-admin:example.org',
            )


if __name__ == '__main__':
    unittest.main()
