import io
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import BotError
from worker import Bot, Health, TelegramError


class VisibleText(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    def handle_data(self, data):
        self.parts.append(data)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = Bot(
            {'token': 'test', 'username': 'test', 'server_name': 'misbot.ru'}, self.tmp.name
        )
        self.bot.telegram = Mock(return_value={'message_id': 1})
        self.policy = {
            'misbot.ru': {
                'usage_limit': 1,
                'lifetime_seconds': 604800,
                'registration_required': False,
            }
        }
        self.identity = {'authenticated': True, 'username': 'operator', 'admin': True}

    def tearDown(self):
        self.bot.db.close()
        self.tmp.cleanup()

    @staticmethod
    def message(chat=123, sender=123, chat_type='private'):
        return {
            'update_id': 1,
            'message': {
                'text': '/start',
                'from': {'id': sender},
                'chat': {'id': chat, 'type': chat_type},
            },
        }

    def control(self, request):
        if request['action'] == 'servers':
            return self.policy
        if request['action'] == 'identity':
            return self.identity
        if request['action'] == 'auth_start':
            return {'user_code': 'ABCD-EFGH', 'url': 'https://mas.misbot.ru/link'}
        raise AssertionError('Unexpected action: ' + request['action'])

    def test_start_reaches_control_and_sends_admin_menu(self):
        self.identity = {'authenticated': True, 'username': 'stepan', 'admin': True}
        with patch('worker.bridge_call', side_effect=self.control) as control:
            self.bot.process(self.message())
        self.assertTrue(control.called)
        method, payload = self.bot.telegram.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertEqual(payload['chat_id'], 123)
        buttons = payload['reply_markup']['inline_keyboard']
        self.assertEqual([row[0]['callback_data'] for row in buttons], ['federation', 'more'])
        self.assertNotIn('/server', payload['text'])
        self.assertLess(len(payload['text']), 180)

    def test_normal_member_menu_hides_federation(self):
        self.identity = {'authenticated': True, 'username': 'member', 'admin': False}
        with patch('worker.bridge_call', side_effect=self.control):
            self.bot.process(self.message())
        buttons = self.bot.telegram.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual(
            [row[0]['callback_data'] for row in buttons], ['invite', 'tokens|0', 'more']
        )

    def test_imported_stack_menu_has_management_and_hides_federation(self):
        self.identity = {'authenticated': True, 'username': 'operator', 'admin': True}
        self.policy['misbot.ru'].update(
            federation_enabled=False, management_actions=['status', 'check', 'start', 'backup']
        )
        with patch('worker.bridge_call', side_effect=self.control):
            self.bot.process(self.message())
        buttons = self.bot.telegram.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual(
            [row[0]['callback_data'] for row in buttons],
            [
                'manage|status',
                'manage|check',
                'maintenance',
                'more',
            ],
        )

    def test_management_write_only_prepares_confirmation(self):
        self.identity = {'authenticated': True, 'username': 'operator', 'admin': True}

        def control(request):
            if request['action'] == 'manage_prepare':
                self.assertEqual(request['operation'], 'backup')
                return {'confirmation': 'fictional-confirmation', 'operation': 'backup'}
            return self.control(request)

        update = self.message()
        update['message']['text'] = '/backup'
        with patch('worker.bridge_call', side_effect=control):
            self.bot.process(update)
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('временно остановятся', payload['text'])
        self.assertEqual(
            payload['reply_markup']['inline_keyboard'][0][0]['callback_data'],
            'manage_apply|fictional-confirmation',
        )

    def test_guest_start_and_direct_invite_request_only_offer_login(self):
        self.identity = {'authenticated': False}
        for command in ('/start', '/invite', '/tokens', '/federation', 'revoke|test'):
            update = self.message()
            update['message']['text'] = command
            with (
                self.subTest(command=command),
                patch('worker.bridge_call', side_effect=self.control),
            ):
                self.bot.process(update)
            payload = self.bot.telegram.call_args.args[1]
            buttons = payload['reply_markup']['inline_keyboard']
            self.assertEqual(buttons[0][0]['text'], 'Войти в Misbot')
            self.assertEqual(buttons[1][0]['callback_data'], 'auth_check')
            self.assertNotIn('Федерация', payload['text'])

    def test_bot_server_is_fixed_even_with_old_server_selection(self):
        with patch('worker.bridge_call', return_value=self.policy):
            self.bot.selected(123)
            self.bot.db.execute("INSERT INTO users VALUES (123,'other.example',NULL)")
            self.assertEqual(self.bot.selected(123), 'misbot.ru')

    def test_worker_requires_one_explicit_domain_and_rejects_multi_server_config(self):
        for extras in (
            {},
            {'server_name': None},
            {'server_name': ''},
            {'server_name': ['misbot.ru', 'other.example']},
            {'server_name': 'https://misbot.ru'},
            {'server_name': 'misbot.ru', 'servers': {'misbot.ru': {}, 'other.example': {}}},
        ):
            with self.subTest(extras=extras), self.assertRaisesRegex(BotError, 'server_name'):
                Bot({'token': 'test', 'username': 'test', **extras}, self.tmp.name)

    def test_bridge_mismatch_extra_servers_and_malformed_policy_block_operations(self):
        for policy in (
            {},
            {'other.example': {}},
            {**self.policy, 'other.example': {}},
            ['misbot.ru'],
            {'misbot.ru': None},
        ):
            with (
                self.subTest(policy=policy),
                patch('worker.bridge_call', return_value=policy) as control,
            ):
                with self.assertRaisesRegex(BotError, 'Привязка'):
                    self.bot.call('manage_read', 123, operation='status')
                self.assertEqual(
                    [call.args[0]['action'] for call in control.call_args_list], ['servers']
                )

    def test_different_telegram_accounts_use_same_pinned_server(self):
        with patch('worker.bridge_call', side_effect=self.control) as control:
            self.bot.selected(123)
            original = self.bot.state('server_binding')
            self.bot.process(self.message())
            self.bot.process(self.message(chat=456, sender=456))
        identities = [
            call.args[0] for call in control.call_args_list if call.args[0]['action'] == 'identity'
        ]
        self.assertEqual({request['uid'] for request in identities}, {123, 456})
        self.assertEqual({request['server'] for request in identities}, {'misbot.ru'})
        self.assertEqual(self.bot.state('server_binding'), original)
        self.assertEqual(
            {row[0] for row in self.bot.db.execute('SELECT server FROM users')}, {'misbot.ru'}
        )

    def test_mutable_worker_config_cannot_rebind_running_instance(self):
        self.bot.config['server_name'] = 'other.example'
        with patch('worker.bridge_call', return_value=self.policy):
            self.assertEqual(self.bot.selected(123), 'misbot.ru')

    def test_obsolete_server_argument_cannot_override_instance_binding(self):
        with patch('worker.bridge_call', side_effect=self.control) as control:
            self.bot.call('identity', 123, server='other.example')
        self.assertEqual(control.call_args.args[0]['server'], 'misbot.ru')

    def test_worker_restart_cannot_change_saved_server_binding(self):
        with patch('worker.bridge_call', return_value=self.policy):
            self.bot.selected(123)
        original = self.bot.state('server_binding')
        with self.assertRaisesRegex(BotError, 'другому серверу'):
            Bot({**self.bot.config, 'server_name': 'other.example'}, self.tmp.name)
        self.assertEqual(self.bot.state('server_binding'), original)

    def test_first_pin_requires_matching_bridge_and_allows_correction_before_binding(self):
        wrong = Bot({**self.bot.config, 'server_name': 'other.example'}, self.tmp.name)
        try:
            with patch('worker.bridge_call', return_value=self.policy):
                with self.assertRaisesRegex(BotError, 'Привязка'):
                    wrong.servers()
            self.assertIsNone(wrong.state('server_binding'))
        finally:
            wrong.db.close()
        corrected = Bot(self.bot.config, self.tmp.name)
        try:
            self.assertIsNone(corrected.state('server_binding'))
            with patch('worker.bridge_call', return_value=self.policy):
                corrected.servers()
            self.assertEqual(
                corrected.state('server_binding'),
                {'format': 1, 'server_name': 'misbot.ru'},
            )
            with self.assertRaisesRegex(BotError, 'другому серверу'):
                Bot({**self.bot.config, 'server_name': 'other.example'}, self.tmp.name)
        finally:
            corrected.db.close()

    @staticmethod
    def legacy_database(folder, servers, binding=None):
        db = sqlite3.connect(Path(folder) / 'worker.sqlite3')
        try:
            db.executescript(
                'CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE users(uid INTEGER PRIMARY KEY,server TEXT,waiting TEXT);'
            )
            db.executemany(
                'INSERT INTO users VALUES (?,?,?)',
                [(index + 1, name, None) for index, name in enumerate(servers)],
            )
            if binding is not None:
                db.execute("INSERT INTO state VALUES ('server_binding',?)", (binding,))
            db.commit()
        finally:
            db.close()

    def test_foreign_legacy_database_cannot_be_silently_reassigned(self):
        with tempfile.TemporaryDirectory() as folder:
            self.legacy_database(folder, ['other.example'])
            with self.assertRaisesRegex(BotError, 'данные не изменены'):
                Bot(self.bot.config, folder)
            with sqlite3.connect(Path(folder) / 'worker.sqlite3') as db:
                self.assertEqual(
                    db.execute('SELECT server FROM users').fetchall(), [('other.example',)]
                )
                self.assertEqual(db.execute('SELECT * FROM state').fetchall(), [])

    def test_legacy_local_database_preserves_historical_choices_and_pins_own_server(self):
        with tempfile.TemporaryDirectory() as folder:
            self.legacy_database(folder, ['misbot.ru', 'other.example'])
            upgraded = Bot(self.bot.config, folder)
            try:
                self.assertIsNone(upgraded.state('server_binding'))
                with patch('worker.bridge_call', return_value=self.policy):
                    self.assertEqual(upgraded.selected(2), 'misbot.ru')
                self.assertEqual(
                    upgraded.state('server_binding'), {'format': 1, 'server_name': 'misbot.ru'}
                )
                self.assertEqual(
                    upgraded.db.execute('SELECT server FROM users ORDER BY uid').fetchall(),
                    [('misbot.ru',), ('other.example',)],
                )
            finally:
                upgraded.db.close()

    def test_unknown_saved_binding_formats_fail_closed_without_replacement(self):
        for saved in (
            '{"format":2,"server_name":"misbot.ru"}',
            '{"format":true,"server_name":"misbot.ru"}',
            '[]',
            'broken-json',
        ):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as folder:
                self.legacy_database(folder, ['misbot.ru'], saved)
                with self.assertRaises(BotError):
                    Bot(self.bot.config, folder)
                with sqlite3.connect(Path(folder) / 'worker.sqlite3') as db:
                    self.assertEqual(
                        db.execute("SELECT value FROM state WHERE key='server_binding'").fetchone()[
                            0
                        ],
                        saved,
                    )

    def test_notifications_do_not_read_feed_from_mismatched_bridge(self):
        with patch('worker.bridge_call', return_value={'other.example': {}}) as control:
            with self.assertRaises(BotError):
                self.bot.notifications()
        self.assertEqual([call.args[0]['action'] for call in control.call_args_list], ['servers'])

    def test_group_messages_and_foreign_private_chat_ignored(self):
        with patch('worker.bridge_call') as control:
            self.bot.process(self.message(chat_type='group'))
            self.bot.process(self.message(chat=456))
        control.assert_not_called()
        self.bot.telegram.assert_not_called()

    def test_invitation_code_is_monospace_and_copy_button_has_plain_code(self):
        code = 'MIS-example_<&>'
        item = {
            'id': 'test',
            'attributes': {
                'token': code,
                'valid': True,
                'expires_at': '2026-10-04T00:00:00Z',
                'times_used': 0,
                'usage_limit': 1,
            },
        }
        self.bot.token_message(123, 'misbot.ru', item)
        method, payload = self.bot.telegram.call_args.args
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertIn('<code>MIS-example_&lt;&amp;&gt;</code>', payload['text'])
        self.assertEqual(
            payload['reply_markup']['inline_keyboard'][0][0]['copy_text']['text'], code
        )
        self.assertIn('Срок действия: 04.10.2026 03:00 МСК', payload['text'])
        self.assertNotIn('Код подходит', payload['text'])

    def test_expiration_converts_across_year_boundary(self):
        item = {
            'id': 'test',
            'attributes': {
                'token': 'MIS-test',
                'valid': False,
                'expires_at': '2026-12-31T23:30:00Z',
                'times_used': 1,
                'usage_limit': 1,
            },
        }
        self.bot.token_message(123, 'misbot.ru', item)
        self.assertIn(
            'Срок действия: 01.01.2027 02:30 МСК', self.bot.telegram.call_args.args[1]['text']
        )

    def test_authentication_code_is_monospace(self):
        self.identity = {'authenticated': False}
        with patch('worker.bridge_call', side_effect=self.control):
            update = self.message()
            update['message']['text'] = '/auth'
            self.bot.process(update)
        method, payload = self.bot.telegram.call_args.args
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertIn('<code>ABCD-EFGH</code>', payload['text'])

    def test_login_preserves_special_characters_lines_and_confirmation_button(self):
        code = 'A<&>_"\'\nNEXT'
        url = 'https://mas.example.org/link?lang=ru&request=fictional'
        self.bot.call = Mock(return_value={'user_code': code, 'url': url})
        self.bot.login(123, 123)
        payload = self.bot.telegram.call_args.args[1]
        visible = VisibleText(payload['text'])
        self.assertEqual(visible.tags, [('code', [])])
        self.assertIn('Код подтверждения: ' + code + '\n\n', ''.join(visible.parts))
        self.assertIn('нажмите «Проверить вход»', ''.join(visible.parts))
        self.assertEqual(
            payload['reply_markup']['inline_keyboard'],
            [
                [{'text': 'Войти в Misbot', 'url': url}],
                [{'text': 'Проверить вход', 'callback_data': 'auth_check'}],
            ],
        )

    def test_invitation_without_expiry_keeps_real_limit_status_and_copy_payload(self):
        code = 'MIS-<&>"\'\nCODE'
        item = {
            'id': 'own-code',
            'attributes': {
                'token': code,
                'valid': False,
                'revoked_at': '2026-10-01T00:00:00Z',
                'expires_at': None,
                'times_used': 2,
                'usage_limit': 3,
            },
        }
        self.bot.token_message(123, 'example.org', item)
        payload = self.bot.telegram.call_args.args[1]
        visible = VisibleText(payload['text'])
        self.assertEqual(visible.tags, [('code', [])])
        body = ''.join(visible.parts)
        self.assertIn('Код: ' + code + '\n\n', body)
        self.assertIn('Срок действия: без срока действия', body)
        self.assertIn('Регистраций: 2/3 · отозван', body)
        self.assertNotIn('Для одной регистрации', body)
        buttons = payload['reply_markup']['inline_keyboard']
        self.assertEqual(buttons[0], [{'text': 'Копировать код', 'copy_text': {'text': code}}])
        self.assertEqual(
            [item['url'] for item in buttons[1]],
            [
                'https://play.google.com/store/apps/details?id=io.element.android.x',
                'https://apps.apple.com/app/element-x-secure-chat-call/id1631335820',
            ],
        )

    def test_running_success_failure_and_interruption_have_distinct_results(self):
        for state, ok, expected in [
            ('running', False, 'Операция выполняется.'),
            ('finished', True, 'Резервная копия создана.'),
            ('finished', False, 'Завершение операции не подтверждено.'),
            ('interrupted', False, 'Завершение операции не подтверждено.'),
        ]:
            with self.subTest(state=state, ok=ok):
                self.bot.job_message(
                    123,
                    {
                        'state': state,
                        'operation': 'backup',
                        'job': 'fictional-job',
                        'result': {'ok': ok},
                    },
                )
                payload = self.bot.telegram.call_args.args[1]
                self.assertTrue(payload['text'].startswith(expected))
                if state == 'running':
                    self.assertEqual(
                        payload['reply_markup']['inline_keyboard'][0][0]['callback_data'],
                        'manage_job|fictional-job',
                    )
                else:
                    self.assertEqual(payload['reply_markup']['inline_keyboard'], [])

    def test_check_uses_text_states_and_keeps_recovery_actions(self):
        self.bot.call = Mock(
            return_value={
                'ok': False,
                'backup_pending': True,
                'checks': [{'name': 'synapse', 'passed': True}, {'name': 'mas', 'passed': False}],
            }
        )
        self.bot.manage(123, 123, 'check')
        body = self.bot.telegram.call_args.args[1]['text']
        self.assertIn('Проверено: synapse', body)
        self.assertIn('Нужна проверка: mas', body)
        for command in ('misbot backup recover', 'misbot status', 'misbot check'):
            self.assertIn(command, body)
        self.bot.call.assert_called_once_with('manage_read', 123, operation='check')

    @staticmethod
    def callback(action, message_id=1):
        return {
            'update_id': 2,
            'callback_query': {
                'id': 'fictional-callback',
                'data': action,
                'from': {'id': 123},
                'message': {'message_id': message_id, 'chat': {'id': 123, 'type': 'private'}},
            },
        }

    def test_main_menu_prioritizes_reads_and_groups_maintenance(self):
        self.policy['misbot.ru']['management_actions'] = [
            'statistics',
            'status',
            'check',
            'restart',
            'update',
            'backup',
            'backup_verify',
        ]
        with patch('worker.bridge_call', side_effect=self.control):
            self.bot.process(self.message())
            main = self.bot.telegram.call_args.args[1]
            self.bot.process(self.callback('maintenance'))
            maintenance = self.bot.telegram.call_args.args[1]
            self.bot.process(self.callback('backups'))
            backups = self.bot.telegram.call_args.args[1]
        self.assertEqual(
            [row[0]['callback_data'] for row in main['reply_markup']['inline_keyboard']],
            [
                'manage|statistics',
                'manage|status',
                'manage|check',
                'federation',
                'maintenance',
                'more',
            ],
        )
        self.assertNotIn('invite', str(main))
        self.assertIn('manage|restart', str(maintenance))
        self.assertIn('backups', str(maintenance))
        self.assertNotIn('manage|backup', str(maintenance))
        self.assertIn('manage|backup', str(backups))
        self.assertEqual(self.bot.telegram.call_args.args[0], 'editMessageText')

    def test_navigation_reuses_message_and_survives_worker_restart(self):
        self.bot.screen(123, 'Первый экран', [[self.bot.button('Далее', 'more')]])
        self.bot.screen(123, 'Второй экран', [[self.bot.button('Назад', 'menu')]])
        self.assertEqual(self.bot.telegram.call_args.args[0], 'editMessageText')
        self.assertEqual(self.bot.telegram.call_args.args[1]['message_id'], 1)
        restarted = Bot(self.bot.config, self.tmp.name)
        try:
            restarted.telegram = Mock(return_value={'message_id': 1})
            restarted.screen(123, 'Третий экран')
            self.assertEqual(restarted.telegram.call_args.args[0], 'editMessageText')
        finally:
            restarted.db.close()

    def test_same_screen_does_not_create_or_edit_message(self):
        self.bot.screen(123, 'Меню', [[self.bot.button('Назад', 'menu')]])
        self.bot.telegram.reset_mock()
        self.bot.screen(123, 'Меню', [[self.bot.button('Назад', 'menu')]])
        self.bot.telegram.assert_not_called()

    def test_explicit_start_recreates_deleted_panel_with_identical_cached_content(self):
        with patch('worker.bridge_call', side_effect=self.control):
            self.bot.process(self.message())
            previous = self.bot.state('navigation:123')['payload']
            self.bot.telegram.reset_mock()
            self.bot.telegram.side_effect = [
                TelegramError(400, 'Bad Request: message to edit not found'),
                {'message_id': 2},
            ]
            self.bot.process(self.message())
        self.assertEqual(
            [call.args[0] for call in self.bot.telegram.call_args_list],
            ['editMessageText', 'sendMessage'],
        )
        current = self.bot.state('navigation:123')
        self.assertEqual(current['message_id'], 2)
        self.assertEqual(current['payload'], previous)

    def test_missing_panel_replaced_but_temporary_failure_not_duplicated(self):
        self.bot.screen(123, 'Первый экран')
        self.bot.telegram.side_effect = [
            TelegramError(400, 'Bad Request: message to edit not found'),
            {'message_id': 2},
        ]
        self.bot.screen(123, 'Второй экран')
        self.assertEqual(self.bot.state('navigation:123')['message_id'], 2)
        self.bot.telegram.reset_mock()
        self.bot.telegram.side_effect = TelegramError(429, 'Too Many Requests')
        with self.assertRaises(BotError):
            self.bot.screen(123, 'Третий экран')
        self.assertEqual(self.bot.telegram.call_count, 1)
        self.assertEqual(self.bot.state('navigation:123')['payload']['text'], 'Второй экран')

    def test_message_not_modified_is_success(self):
        self.bot.screen(123, 'Первый экран')
        self.bot.telegram.side_effect = TelegramError(400, 'Bad Request: message is not modified')
        self.bot.screen(123, 'Второй экран')
        self.assertEqual(self.bot.state('navigation:123')['payload']['text'], 'Второй экран')

    def test_old_confirmation_callback_does_not_execute(self):
        self.bot.screen(
            123, 'Подтверждение', [[self.bot.button('Подтвердить', 'fed_apply|fictional')]]
        )
        self.bot.screen(123, 'Федерация', [[self.bot.button('Назад', 'menu')]])
        with patch('worker.bridge_call') as control:
            self.bot.process(self.callback('fed_apply|fictional'))
        control.assert_not_called()
        self.assertIn('устарела', self.bot.telegram.call_args.args[1]['text'])

    def test_callback_after_expired_login_opens_login_without_operation(self):
        self.bot.screen(123, 'Сервер', [[self.bot.button('Проверить', 'manage|check')]])
        self.identity = {'authenticated': False}
        with patch('worker.bridge_call', side_effect=self.control) as control:
            self.bot.process(self.callback('manage|check'))
        actions = [call.args[0]['action'] for call in control.call_args_list]
        self.assertNotIn('manage_read', actions)
        self.assertIn('auth_start', actions)
        self.assertIn('Код подтверждения', self.bot.telegram.call_args.args[1]['text'])

    def test_api_switch_prepares_and_requires_fresh_confirmation(self):
        def control(request):
            if request['action'] == 'fed_read':
                return {'enabled': True}
            if request['action'] == 'fed_prepare':
                self.assertEqual(request['operation'], 'api-disable')
                self.assertEqual(request['target'], '')
                return {'confirmation': 'fictional-api', 'operation': 'api-disable', 'target': ''}
            return self.control(request)

        with patch('worker.bridge_call', side_effect=control) as bridge:
            self.bot.federation_api(123, 123)
            self.bot.process(self.callback('fed_prepare|api-disable'))
        self.assertNotIn('fed_apply', [call.args[0]['action'] for call in bridge.call_args_list])
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('федерация сохранится', payload['text'])
        self.assertEqual(
            payload['reply_markup']['inline_keyboard'][0][0]['callback_data'],
            'fed_apply|fictional-api',
        )

    def test_federation_lists_filter_states_and_paginate(self):
        records = [
            {
                'id': str(i),
                'server': f'peer{i}.example',
                'direction': 'incoming',
                'state': 'pending',
            }
            for i in range(6)
        ] + [
            {'id': 'out', 'server': 'out.example', 'direction': 'outgoing', 'state': 'pending'},
            {'id': 'no', 'server': 'no.example', 'direction': 'incoming', 'state': 'rejected'},
        ]
        self.bot.call = Mock(return_value={'requests': records})
        self.bot.federation_requests(123, 123, 'incoming')
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('fed_requests|incoming|5', str(payload))
        self.assertNotIn('out.example', str(payload))
        self.assertNotIn('no.example', str(payload))
        self.bot.federation_requests(123, 123, 'rejected')
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('no.example', str(payload))
        self.assertIn('fed_prepare|clear', str(payload))

    def test_missing_request_cannot_offer_acceptance(self):
        self.bot.call = Mock(return_value={'requests': []})
        with self.assertRaisesRegex(BotError, 'уже закрыта'):
            self.bot.federation_card(123, 123, 'fictional-request')
        self.bot.telegram.assert_not_called()

    def test_outgoing_card_explains_delivery_and_offers_only_revoke(self):
        self.bot.call = Mock(
            return_value={
                'requests': [
                    {
                        'id': 'out',
                        'server': 'out.example',
                        'direction': 'outgoing',
                        'state': 'pending',
                        'expires_at': 1791072000,
                        'delivery_pending': True,
                    }
                ]
            }
        )
        self.bot.federation_card(123, 123, 'out')
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('ещё не подтверждена', payload['text'])
        self.assertIn('fed_prepare|cancel|out', str(payload))
        self.assertNotIn('fed_prepare|approve', str(payload))

    def test_failed_outgoing_delivery_offers_retry_without_claiming_automatic_delivery(self):
        self.bot.call = Mock(
            return_value={
                'requests': [
                    {
                        'id': 'MB-' + 'a' * 32,
                        'server': 'peer.example',
                        'direction': 'outgoing',
                        'state': 'pending',
                        'safe_error': 'timeout',
                    }
                ]
            }
        )
        self.bot.federation_card(123, 123, 'MB-' + 'a' * 32)
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('Другой сервер не ответил', payload['text'])
        self.assertIn('fed_prepare|retry|MB-', str(payload))
        self.assertIn('fed_prepare|cancel|MB-', str(payload))
        self.assertNotIn('Misbot повторит', payload['text'])

    def test_approved_or_applying_cards_only_offer_continuation(self):
        for state, label in (
            ('accepted', 'одобрена'),
            ('applying', 'правила федерации применяются'),
            ('partially_applied', 'правила применены частично'),
        ):
            for direction in ('incoming', 'outgoing'):
                with self.subTest(state=state, direction=direction):
                    self.bot.call = Mock(
                        return_value={
                            'requests': [
                                {
                                    'id': 'MB-' + 'a' * 32,
                                    'server': 'peer.example',
                                    'direction': direction,
                                    'state': state,
                                }
                            ]
                        }
                    )
                    self.bot.federation_card(123, 123, 'MB-' + 'a' * 32)
                    payload = self.bot.telegram.call_args.args[1]
                    self.assertIn(label, payload['text'])
                    self.assertIn('fed_prepare|retry|MB-', str(payload))
                    for operation in ('approve', 'reject', 'cancel'):
                        self.assertNotIn('fed_prepare|' + operation, str(payload))

    def test_federation_complete_job_reads_nested_error_and_state(self):
        self.bot.federation_job(
            123,
            {
                'operation_id': 'FO-fixture',
                'status': 'complete',
                'action': 'invite',
                'result': {
                    'state': 'pending',
                    'direction': 'outgoing',
                    'server': 'peer.example',
                    'code': 'timeout',
                },
            },
        )
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('обмен с другим сервером ещё не подтверждён', payload['text'])
        self.assertIn('Другой сервер не ответил', payload['text'])
        self.assertIn('peer.example', payload['text'])
        self.assertNotIn('Операция завершена', payload['text'])
        self.assertIn('fed_requests|outgoing|0', str(payload))

    def test_federation_failed_job_translates_nested_backend_code(self):
        self.bot.federation_job(
            123,
            {
                'operation_id': 'FO-fixture',
                'status': 'failed',
                'action': 'invite',
                'result': {'code': 'peer_upgrade_required'},
            },
        )
        self.assertIn('требуется обновление Misbot', self.bot.telegram.call_args.args[1]['text'])

    def test_federation_job_distinguishes_saved_api_setting_from_partial_connection(self):
        self.bot.federation_job(
            123,
            {
                'operation_id': 'FO-api',
                'status': 'complete',
                'action': 'api-disable',
                'result': {'enabled': False, 'status': 'changed'},
            },
        )
        self.assertIn('Misbot API выключен', self.bot.telegram.call_args.args[1]['text'])
        self.assertIn('Настройка сохранена', self.bot.telegram.call_args.args[1]['text'])
        self.bot.federation_job(
            123,
            {
                'operation_id': 'FO-connect',
                'status': 'complete',
                'action': 'approve',
                'result': {'state': 'partially_applied', 'direction': 'incoming'},
            },
        )
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('Подключение ещё не завершено', payload['text'])
        self.assertIn('fed_requests|incoming|0', str(payload))

    def test_federation_terminal_delivery_pending_does_not_claim_remote_completion(self):
        self.bot.federation_job(
            123,
            {
                'operation_id': 'FO-reject',
                'status': 'complete',
                'action': 'reject',
                'result': {'state': 'rejected', 'delivery_pending': True},
            },
        )
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('обмен с другим сервером ещё не подтверждён', payload['text'])
        self.assertIn('Доставка результата будет повторена', payload['text'])

    def test_federation_retry_prepares_confirmation_for_same_request(self):
        request_id = 'MB-' + 'a' * 32
        self.bot.call = Mock(
            return_value={
                'confirmation': 'fictional-retry',
                'operation': 'retry',
                'target': request_id,
            }
        )
        self.bot.prepare(123, 123, 'retry', request_id)
        self.bot.call.assert_called_once_with(
            'fed_prepare',
            123,
            operation='retry',
            target=request_id,
        )
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('Новая заявка не создаётся', payload['text'])
        self.assertIn('fed_apply|fictional-retry', str(payload))

    def test_durable_job_result_does_not_replace_navigation(self):
        self.bot.screen(123, 'Меню')
        self.bot.job_message(123, {'job': 'own-job', 'operation': 'backup', 'state': 'running'})
        self.assertEqual(self.bot.state('navigation:123')['payload']['text'], 'Меню')
        self.bot.job_message(
            123,
            {'job': 'own-job', 'operation': 'backup', 'state': 'finished', 'result': {'ok': True}},
        )
        self.assertEqual(self.bot.telegram.call_args.args[0], 'editMessageText')
        self.assertEqual(self.bot.state('navigation:123')['payload']['text'], 'Меню')

    def test_notification_ack_failure_retries_without_second_message(self):
        attempts = []

        def control(request):
            if request['action'] == 'servers':
                return self.policy
            if request['action'] == 'federation_notifications':
                return {
                    'notifications': [{'uid': 123, 'id': 'event-1', 'message': 'Новая заявка.'}]
                }
            attempts.append(request)
            if len(attempts) == 1:
                raise BotError('Временно недоступно')
            return {'ok': True}

        with patch('worker.bridge_call', side_effect=control):
            with self.assertRaises(BotError):
                self.bot.notifications()
            self.bot.notifications()
        self.assertEqual(self.bot.telegram.call_count, 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]['id'], 'event-1')

    def test_unavailable_statistics_do_not_invent_counts(self):
        self.bot.call = Mock(
            return_value={'ok': False, 'statistics': {'disk_free_bytes': 3 * 1024**3}}
        )
        self.bot.manage(123, 123, 'statistics', 'week')
        payload = self.bot.telegram.call_args.args[1]
        self.assertIn('3.0 ГБ', payload['text'])
        self.assertIn('нет данных', payload['text'])
        self.assertNotIn('Сообщений за период: 0', payload['text'])
        self.bot.call.assert_called_once_with(
            'manage_read', 123, operation='statistics', period='week'
        )

    @staticmethod
    def health_reply(error=None, last_poll=None):
        handler = Health.__new__(Health)
        handler.path = '/health'
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.server = SimpleNamespace(
            bot=SimpleNamespace(
                last_poll_success=time.monotonic() if last_poll is None else last_poll,
                servers=Mock(side_effect=error, return_value={'misbot.ru': {}}),
            )
        )
        handler.do_GET()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue()

    def test_health_reports_unusable_control_socket(self):
        for error in [PermissionError(), FileNotFoundError(), TimeoutError()]:
            with self.subTest(error=type(error).__name__):
                status, body = self.health_reply(error)
                self.assertEqual(status, 503)
                self.assertEqual(body, b'{"ok":false}')

    def test_health_checks_control_socket_before_success(self):
        self.assertEqual(self.health_reply(), (200, b'{"ok":true}'))

    def test_health_rejects_absent_or_stale_telegram_polling(self):
        for last_poll in (0, time.monotonic() - 100):
            self.assertEqual(self.health_reply(last_poll=last_poll), (503, b'{"ok":false}'))

    def test_health_reports_server_binding_mismatch_as_unready(self):
        handler = Health.__new__(Health)
        handler.path = '/health'
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        self.bot.last_poll_success = time.monotonic()
        handler.server = SimpleNamespace(bot=self.bot)
        with patch('worker.bridge_call', return_value={**self.policy, 'other.example': {}}):
            handler.do_GET()
        self.assertEqual(handler.send_response.call_args.args[0], 503)


if __name__ == '__main__':
    unittest.main()
