"""Password changes must remain recoverable after partial success."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import credentials
import lifecycle
import mas_admin
import state_store


class CredentialsTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'secrets').mkdir()
        (root / 'stages').mkdir()
        state_store.write_json(root / 'installation.json', {'domain': 'example.invalid'})
        previous = {'username': 'matrix-admin', 'password': 'Example-previous-password!'}
        state_store.write_json(root / 'secrets/admin-access.json', previous)
        state_store.write_json(root / 'input.json', {'password': '-Example-replacement-password!'})
        return previous

    def context(self, root):
        # Root ownership itself is checked on the VPS; these tests isolate transactions.
        return patch.multiple(credentials.lifecycle,
            state=lambda: json.loads((root / 'installation.json').read_text()),
            backup=Mock(), restore_check=Mock(), emit=Mock(),
            service_rows=Mock(return_value=[{'State': 'running', 'Health': 'healthy'} for _ in range(11)]))

    def reader(self):
        return patch.object(credentials, 'private_json', side_effect=lambda p: json.loads(Path(p).read_text()))

    def test_invalid_input_does_not_start_backup_or_change_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = self.fixture(root)
            state_store.write_json(root / 'input.json', {'password': 'short'})
            with self.context(root), self.reader():
                with self.assertRaises(RuntimeError): credentials.rotate(root, root / 'input.json')
                credentials.lifecycle.backup.assert_not_called()
            self.assertEqual(json.loads((root / 'secrets/admin-access.json').read_text()), previous)

    def test_success_changes_only_saved_password_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = self.fixture(root)
            with self.context(root), self.reader(), patch.object(credentials, 'apply') as apply:
                credentials.rotate(root, root / 'input.json')
                self.assertEqual(apply.call_args.args[1]['password'], '-Example-replacement-password!')
            updated = json.loads((root / 'secrets/admin-access.json').read_text())
            self.assertEqual(updated['username'], previous['username'])
            self.assertNotEqual(updated['password'], previous['password'])
            self.assertFalse(json.loads((root / 'installation.json').read_text()).get('credential_transition_pending'))

    def test_lost_response_restores_old_password(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = self.fixture(root)
            with self.context(root), self.reader(), patch.object(credentials, 'apply', side_effect=[RuntimeError('lost response'), None]) as apply:
                with self.assertRaisesRegex(RuntimeError, 'original password restored'):
                    credentials.rotate(root, root / 'input.json')
                self.assertEqual(apply.call_args.args[1], previous)
            self.assertEqual(json.loads((root / 'secrets/admin-access.json').read_text()), previous)

    def test_readiness_failure_after_password_apply_restores_old_password(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = self.fixture(root)
            with self.context(root), self.reader(), patch.object(credentials, 'apply') as apply, \
                    patch.object(credentials, 'wait_services', side_effect=[None, RuntimeError('service readiness failed'), None]):
                with self.assertRaisesRegex(RuntimeError, 'original password restored'):
                    credentials.rotate(root, root / 'input.json')
                self.assertEqual(apply.call_count, 2)
                self.assertEqual(apply.call_args.args[1], previous)
            self.assertEqual(json.loads((root / 'secrets/admin-access.json').read_text()), previous)
            self.assertFalse(json.loads((root / 'installation.json').read_text()).get('credential_transition_pending'))

    def test_failed_readiness_during_recovery_retains_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = self.fixture(root)
            saved = root / 'stages/credentials/fictional'; saved.mkdir(parents=True)
            state_store.write_json(saved / 'previous.json', previous)
            state_store.write_json(root / 'installation.json', {'domain': 'example.invalid',
                'credential_transition_pending': True, 'credential_transition_dir': str(saved.relative_to(root))})
            with self.context(root), self.reader(), patch.object(credentials, 'apply'), \
                    patch.object(credentials, 'wait_services', side_effect=RuntimeError('service readiness failed')):
                with self.assertRaisesRegex(RuntimeError, 'service readiness failed'): credentials.recover(root)
            self.assertTrue(json.loads((root / 'installation.json').read_text())['credential_transition_pending'])
            self.assertEqual(json.loads((root / 'secrets/admin-access.json').read_text()), previous)

    def test_unavailable_recovery_keeps_checkpoint_then_recovers_later(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = self.fixture(root)
            with self.context(root), self.reader(), patch.object(credentials, 'apply', side_effect=RuntimeError('unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'admin-password-recover'):
                    credentials.rotate(root, root / 'input.json')
            self.assertTrue(json.loads((root / 'installation.json').read_text())['credential_transition_pending'])
            with self.context(root), self.reader(), patch.object(credentials, 'apply') as apply:
                credentials.recover(root)
                self.assertEqual(apply.call_args.args[1], previous)

    def test_world_readable_and_symlink_inputs_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.fixture(root)
            (root / 'input.json').chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'mode 600'): credentials.private_json(root / 'input.json')
            (root / 'linked.json').symlink_to(root / 'input.json')
            with self.assertRaisesRegex(RuntimeError, 'mode 600'): credentials.private_json(root / 'linked.json')

    def test_password_api_refuses_nonadmin_without_modifying_password(self):
        request = Mock(return_value=(200, {'data': {'id': 'EXAMPLE_ID', 'attributes': {'admin': False}}}))
        with self.assertRaises(RuntimeError):
            mas_admin.set_password(request, {'username': 'matrix-admin', 'password': 'Example-password!'})
        self.assertEqual(request.call_count, 1)

    def test_pending_password_change_blocks_normal_startup(self):
        with patch.object(lifecycle, 'ROOT', Path('/opt/example'), create=True), \
             patch.object(lifecycle, 'state', return_value={'credential_transition_pending': True}), \
             patch.object(lifecycle, 'generate') as generate:
            with self.assertRaisesRegex(RuntimeError, 'admin-password-recover'): lifecycle.up()
            generate.assert_not_called()

    def test_starting_service_cannot_be_reported_as_ready(self):
        rows = [{'State': 'running', 'Health': 'healthy'} for _ in range(10)]
        rows.append({'State': 'running', 'Health': 'starting'})
        def inspect(check, **kwargs):
            self.assertFalse(check())
            rows[-1]['Health'] = 'healthy'
            self.assertTrue(check())
        with patch.object(lifecycle, 'state', return_value={'database_mode': 'local'}), \
             patch.object(lifecycle, 'service_rows', return_value=rows), \
             patch.object(lifecycle, 'wait', side_effect=inspect):
            credentials.wait_services()

    def test_pending_password_change_blocks_incomplete_backup_and_migration(self):
        with patch.object(lifecycle, 'state', return_value={'credential_transition_pending': True}):
            with self.assertRaisesRegex(RuntimeError, 'admin-password-recover'): lifecycle.backup()
            with self.assertRaisesRegex(RuntimeError, 'password change'): lifecycle.move_export()
