import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mas_admin
import lifecycle


class BootstrapTests(unittest.TestCase):
    credentials = {'username': 'matrix-admin', 'password': '-Example-test-password_123!'}
    user = {'id': 'EXAMPLE_USER_ID', 'attributes': {'admin': False}}

    def test_interrupted_password_request_resumes_owned_account(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / 'checkpoint.json'
            request = Mock(side_effect=[(404, None), (201, {'data': self.user}), (503, None)])
            with self.assertRaises(RuntimeError):
                mas_admin.bootstrap(request, self.credentials, checkpoint)
            self.assertEqual(json.loads(checkpoint.read_text())['user_id'], self.user['id'])
            request = Mock(
                side_effect=[
                    (200, {'data': self.user}),
                    (204, None),
                    (200, {'data': {'id': self.user['id'], 'attributes': {'admin': True}}}),
                ]
            )
            mas_admin.bootstrap(request, self.credentials, checkpoint)
            self.assertEqual(
                request.call_args_list[1].args[1]['password'], self.credentials['password']
            )
            self.assertFalse(checkpoint.exists())

    def test_foreign_account_and_inactive_account_are_preserved(self):
        for attributes in ({'admin': False}, {'admin': True, 'locked_at': 'EXAMPLE_DATE'}):
            with self.subTest(attributes=attributes), tempfile.TemporaryDirectory() as folder:
                request = Mock(
                    return_value=(200, {'data': {'id': self.user['id'], 'attributes': attributes}})
                )
                with self.assertRaises(RuntimeError):
                    mas_admin.bootstrap(request, self.credentials, Path(folder) / 'checkpoint.json')
                self.assertEqual(request.call_count, 1)

    def test_successful_repeat_does_not_reset_existing_admin_password(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Mock(
                return_value=(200, {'data': {'id': self.user['id'], 'attributes': {'admin': True}}})
            )
            mas_admin.bootstrap(request, self.credentials, Path(folder) / 'checkpoint.json')
            self.assertEqual(request.call_count, 1)

    def test_password_and_service_credential_are_only_in_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'secrets').mkdir()
            (root / 'secrets/admin-access.json').write_text(json.dumps(self.credentials))
            (root / 'secrets/admin-access.json').chmod(0o600)
            client = {
                'client_id': 'EXAMPLE_CLIENT',
                'client_secret': 'EXAMPLE_PRIVATE_CLIENT_SECRET',
            }
            (root / 'secrets/referral-client.json').write_text(json.dumps(client))
            (root / 'mas_admin.py').write_text('PUBLIC_SCRIPT')
            with (
                patch.object(lifecycle, 'ROOT', root, create=True),
                patch.object(lifecycle, 'postgres', return_value=''),
                patch.object(lifecycle, 'dc') as dc,
                patch.object(lifecycle, 'emit'),
            ):
                lifecycle.create_admin()
            self.assertNotIn(self.credentials['password'], repr(dc.call_args.args))
            self.assertNotIn(client['client_secret'], repr(dc.call_args.args))
            self.assertEqual(
                json.loads(dc.call_args.kwargs['stdin'])['credentials'], self.credentials
            )
