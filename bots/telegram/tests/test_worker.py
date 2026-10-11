import io
from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import Bot, Health


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
        self.assertEqual(
            [row[0]['callback_data'] for row in buttons], ['invite', 'tokens|0', 'federation']
        )
        self.assertNotIn('/server', payload['text'])
        self.assertLess(len(payload['text']), 180)

    def test_normal_member_menu_hides_federation(self):
        self.identity = {'authenticated': True, 'username': 'member', 'admin': False}
        with patch('worker.bridge_call', side_effect=self.control):
            self.bot.process(self.message())
        buttons = self.bot.telegram.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual([row[0]['callback_data'] for row in buttons], ['invite', 'tokens|0'])

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
                'invite',
                'tokens|0',
                'manage|status',
                'manage|check',
                'manage|start',
                'manage|backup',
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
        self.bot.db.execute("INSERT INTO users VALUES (123,'other.example',NULL)")
        with patch('worker.bridge_call', return_value={**self.policy, 'other.example': {}}):
            self.assertEqual(self.bot.selected(123), 'misbot.ru')

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
        self.assertEqual(
            payload['reply_markup']['inline_keyboard'],
            [
                [{'text': 'Копировать код', 'copy_text': {'text': code}}],
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
                    self.assertNotIn('reply_markup', payload)

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
    def health_reply(error=None, last_poll=None):
        handler = Health.__new__(Health)
        handler.path = '/health'
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.server = SimpleNamespace(
            bot=SimpleNamespace(
                last_poll_success=time.monotonic() if last_poll is None else last_poll
            )
        )
        with patch('worker.bridge_call', side_effect=error, return_value={'misbot.ru': {}}):
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


if __name__ == '__main__':
    unittest.main()
