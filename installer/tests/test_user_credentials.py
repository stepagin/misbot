"""Ordinary password resets must not overwrite another identity or lose retry data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import state_store
import user_credentials as uc
import user_password_api as ua


class UserPasswordTests(unittest.TestCase):
    values = {'username': 'example-user', 'password': '-Fictional-new-password_123!'}
    identity = {'username': 'example-user', 'user_id': 'FICTIONAL_ID'}

    def fixture(self, root):
        (root / 'secrets').mkdir(mode=0o700)
        state_store.write_json(root / 'input.json', self.values)
        state_store.write_json(root / 'installation.json', {'domain': 'example.invalid'})
        self.metadata = (root / 'installation.json').read_bytes()
        return patch.multiple(uc.lifecycle,
            state=lambda: json.loads((root / 'installation.json').read_text()),
            backup=Mock(), restore_check=Mock(), emit=Mock())

    def api_user(self, attributes=None, identity='FICTIONAL_ID'):
        return {'data': {'id': identity, 'attributes':
            {'username': self.values['username'], 'can_request_admin': False, **(attributes or {})}}}

    def test_short_invalid_username_and_extra_fields_refused_before_api_or_backup(self):
        cases = [dict(self.values, password='short'), dict(self.values, username='@user:other.invalid'),
                 dict(self.values, username='matrix-admin'), dict(self.values, username='../wrong'),
                 dict(self.values, password='Fictional-control-character\n'), dict(self.values, extra=True)]
        for values in cases:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with self.fixture(root), patch.object(uc, 'api') as api:
                    state_store.write_json(root / 'input.json', values)
                    with self.assertRaises(RuntimeError): uc.reset(root, root / 'input.json')
                    api.assert_not_called(); uc.lifecycle.backup.assert_not_called()
                self.assertFalse((root / 'secrets/user-password-pending.json').exists())

    def test_missing_inactive_admin_wrong_name_and_changed_id_never_issue_password_post(self):
        cases = [(404, None), (200, self.api_user({'locked_at': 'FICTIONAL'})),
                 (200, self.api_user({'deactivated_at': 'FICTIONAL'})),
                 (200, self.api_user({'can_request_admin': True})),
                 (200, self.api_user({'admin': True})),
                 (200, self.api_user({'username': 'another-user'})),
                 (200, self.api_user(identity='ANOTHER_ID'))]
        for response in cases:
            with self.subTest(response=response):
                request = Mock(return_value=response)
                with self.assertRaises(RuntimeError): ua.set_password(request, dict(self.values, user_id='FICTIONAL_ID'))
                self.assertEqual(request.call_count, 1)

    def test_api_changes_only_password_for_confirmed_existing_id(self):
        request = Mock(side_effect=[(200, self.api_user()), (204, None)])
        self.assertEqual(ua.set_password(request, dict(self.values, user_id='FICTIONAL_ID')), self.identity)
        self.assertEqual(request.call_args.args, ('/api/admin/v1/users/FICTIONAL_ID/set-password',
            {'password': self.values['password']}))
        self.assertFalse(any('set-admin' in call.args[0] for call in request.call_args_list))

    def test_success_backs_up_before_apply_and_keeps_server_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); events = []
            with self.fixture(root), patch.object(uc, 'api', side_effect=lambda r, action, v:
                    events.append(action) or self.identity), patch.object(uc.credentials, 'wait_services'):
                uc.lifecycle.backup.side_effect = lambda: events.append('backup')
                uc.lifecycle.restore_check.side_effect = lambda: events.append('restore')
                uc.reset(root, root / 'input.json')
            self.assertEqual(events, ['inspect', 'backup', 'restore', 'password'])
            self.assertEqual((root / 'installation.json').read_bytes(), self.metadata)
            self.assertFalse((root / 'secrets/user-password-pending.json').exists())

    def test_unavailable_inspection_does_not_backup_or_publish_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.fixture(root), patch.object(uc, 'api', side_effect=RuntimeError('FICTIONAL_PRIVATE_RESPONSE')):
                with self.assertRaisesRegex(RuntimeError, 'No password changed') as error: uc.reset(root, root / 'input.json')
                self.assertNotIn('FICTIONAL_PRIVATE_RESPONSE', str(error.exception))
                uc.lifecycle.backup.assert_not_called()
            self.assertFalse((root / 'secrets/user-password-pending.json').exists())

    def test_lost_reply_preserves_exact_retry_and_blocks_other_reset_then_forward_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.fixture(root), patch.object(uc, 'api', side_effect=[self.identity, RuntimeError('lost reply')]), \
                    patch.object(uc.credentials, 'wait_services'):
                with self.assertRaisesRegex(RuntimeError, 'user-password-recover'): uc.reset(root, root / 'input.json')
            path = root / 'secrets/user-password-pending.json'
            with self.fixture_without_creating(root), patch.object(uc, 'api') as api, patch.object(uc.credentials, 'wait_services'):
                api.return_value = self.identity
                with self.assertRaisesRegex(RuntimeError, 'pending'): uc.reset(root, root / 'input.json')
                api.assert_not_called()
                uc.recover(root)
                self.assertEqual(api.call_args.args[1:], ('password', dict(self.values, user_id='FICTIONAL_ID')))
                uc.lifecycle.backup.assert_not_called()
                with self.assertRaisesRegex(RuntimeError, 'No pending'): uc.recover(root)
            self.assertFalse(path.exists())

    def fixture_without_creating(self, root):
        return patch.multiple(uc.lifecycle,
            state=lambda: json.loads((root / 'installation.json').read_text()),
            backup=Mock(), restore_check=Mock(), emit=Mock())

    def test_readiness_and_signal_errors_keep_pending_until_recovery_is_ready(self):
        for failure in (RuntimeError('stopped service'), state_store.OperationInterrupted('TERM')):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with self.fixture(root), patch.object(uc, 'api', return_value=self.identity), \
                        patch.object(uc.credentials, 'wait_services', side_effect=[None, failure]):
                    with self.assertRaisesRegex(RuntimeError, 'may already be active'): uc.reset(root, root / 'input.json')
                path = root / 'secrets/user-password-pending.json'
                self.assertTrue(path.exists())
                with self.fixture_without_creating(root), patch.object(uc, 'api', return_value=self.identity), \
                        patch.object(uc.credentials, 'wait_services', side_effect=failure):
                    with self.assertRaisesRegex(RuntimeError, 'user-password-recover'): uc.recover(root)
                self.assertTrue(path.exists())

    def test_different_server_checkpoint_refused_before_api(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.fixture(root), patch.object(uc, 'api') as api:
                state_store.write_json(root / 'secrets/user-password-pending.json', {'format': 1,
                    'server_name': 'other.invalid', 'credentials': dict(self.values, user_id='FICTIONAL_ID')})
                with self.assertRaisesRegex(RuntimeError, 'checkpoint'): uc.recover(root)
                api.assert_not_called()

    def test_private_file_rejects_public_symlink_hardlink_and_oversize(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root / 'input.json'
            state_store.write_json(path, self.values)
            self.assertEqual(uc.private_json(path), self.values)
            path.chmod(0o644)
            with self.assertRaises(RuntimeError): uc.private_json(path)
            path.chmod(0o600)
            alias = root / 'alias'; alias.symlink_to(path)
            with self.assertRaises(OSError): uc.private_json(alias)
            os.link(path, root / 'hardlink')
            with self.assertRaises(RuntimeError): uc.private_json(path)
            (root / 'hardlink').unlink(); path.write_text('x' * 16385)
            with self.assertRaises(RuntimeError): uc.private_json(path)

    def test_private_directory_symlink_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'elsewhere').mkdir(mode=0o700)
            (root / 'secrets').symlink_to(root / 'elsewhere')
            with self.assertRaises(RuntimeError): uc.checkpoint(root)

    def test_fifo_refused_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'; os.mkfifo(path, mode=0o600)
            with self.assertRaises(RuntimeError): uc.private_json(path)

    def test_api_password_and_client_secret_only_on_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.fixture(root):
                client = {'client_id': 'FICTIONAL_CLIENT', 'client_secret': 'FICTIONAL_CLIENT_SECRET'}
                state_store.write_json(root / 'secrets/referral-client.json', client)
                (root / 'user_password_api.py').write_text('PUBLIC_SCRIPT')
                with patch.object(uc.lifecycle, 'dc', return_value=json.dumps(self.identity)) as dc:
                    uc.api(root, 'password', dict(self.values, user_id='FICTIONAL_ID'))
                self.assertNotIn(self.values['password'], repr(dc.call_args.args))
                self.assertNotIn(client['client_secret'], repr(dc.call_args.args))
                self.assertEqual(json.loads(dc.call_args.kwargs['stdin'])['client'], client)


if __name__ == '__main__': unittest.main()
