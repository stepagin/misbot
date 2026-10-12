"""A deleted installation must not resume a saved public-check step."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import state_store
import terminal_ui
from terminal_views import TerminalView


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'removed-installation'
        self.draft = self.base / 'wizard.json'
        self.record = {
            'format': 1,
            'directory': str(self.root),
            'step': 8,
            'values': {'DOMAIN': 'example.org', 'S3_SECRET_ACCESS_KEY': 'FICTIONAL_PRIVATE_VALUE'},
            'backup': {'S3_BUCKET': 'fictional-backups'},
            'confirmed_domains': ['example.org'],
        }
        state_store.write_json(self.draft, self.record)
        self.output = io.StringIO()
        output_patch = patch.object(sys, 'stdout', self.output)
        output_patch.start()
        self.addCleanup(output_patch.stop)

    def interface(self, choice):
        return terminal_ui.UI(
            self.root,
            draft=self.draft,
            reader=lambda *_: choice,
            runner=lambda _: self.fail('A missing installation must not run check'),
            view=TerminalView(plain=True),
        )

    def test_continue_saved_parameters_starts_from_domain_without_running_check(self):
        interface = self.interface('1')
        with patch.object(interface, 'wizard', return_value=0) as wizard:
            self.assertEqual(terminal_ui.interaction(interface), 0)
        wizard.assert_called_once()
        saved = json.loads(self.draft.read_text())
        self.assertEqual(saved['step'], 0)
        self.assertEqual(saved['values'], self.record['values'])
        self.assertEqual(saved['backup'], self.record['backup'])
        self.assertFalse(self.root.exists())

    def test_new_setup_preserves_old_private_draft_and_retained_runtime_files(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'data').mkdir()
        kept = self.root / 'data/fixture'
        kept.write_text('fictional retained data')
        interface = self.interface('2')
        with patch.object(interface, 'wizard', return_value=0):
            self.assertEqual(terminal_ui.interaction(interface), 0)
        saved = json.loads(self.draft.read_text())
        self.assertEqual(saved['step'], 0)
        self.assertEqual(saved['values'], {})
        self.assertEqual(saved['backup'], {})
        self.assertEqual(saved['confirmed_domains'], [])
        previous = list((self.base / 'previous').glob('*.json'))
        self.assertEqual(len(previous), 1)
        self.assertEqual(json.loads(previous[0].read_text()), self.record)
        self.assertEqual(previous[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.base / 'previous').stat().st_mode & 0o777, 0o700)
        self.assertEqual(kept.read_text(), 'fictional retained data')
        self.assertNotIn('FICTIONAL_PRIVATE_VALUE', self.output.getvalue())

    def test_exit_keeps_the_saved_draft_unchanged(self):
        before = self.draft.read_bytes()
        interface = self.interface('0')
        with patch.object(
            interface, 'wizard', side_effect=AssertionError('exit should not run wizard')
        ):
            self.assertEqual(terminal_ui.interaction(interface), 0)
        self.assertEqual(self.draft.read_bytes(), before)
        self.assertFalse((self.base / 'previous').exists())

    def test_draft_before_initial_application_continues_its_current_step(self):
        self.record['step'] = 6
        state_store.write_json(self.draft, self.record)
        interface = self.interface('0')
        with (
            patch.object(interface, 'wizard', return_value=0) as wizard,
            patch.object(
                interface,
                'restart_without_installation',
                side_effect=AssertionError('valid draft reset'),
            ),
        ):
            self.assertEqual(terminal_ui.interaction(interface), 0)
        wizard.assert_called_once()
        self.assertEqual(interface.step, 6)

    def test_clean_target_without_draft_opens_new_installation_menu(self):
        self.draft.unlink()
        interface = self.interface('0')
        with patch.object(interface, 'welcome', return_value=0) as welcome:
            self.assertEqual(terminal_ui.interaction(interface), 0)
        welcome.assert_called_once()

    def test_existing_installation_does_not_offer_new_setup(self):
        self.root.mkdir(mode=0o700)
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'example.org', 'databases_initialized': False},
        )
        interface = self.interface('2')
        with (
            patch('command_wrapper.trusted_installation', return_value=self.root),
            patch.object(interface, 'wizard', return_value=0) as wizard,
            patch.object(
                interface,
                'restart_without_installation',
                side_effect=AssertionError('existing setup reset'),
            ),
        ):
            self.assertEqual(terminal_ui.interaction(interface), 0)
        wizard.assert_called_once()
        self.assertEqual(interface.step, 8)

    def test_failed_preservation_does_not_clear_the_original_draft(self):
        before = self.draft.read_bytes()
        interface = self.interface('2')
        with patch.object(state_store, 'write_json', side_effect=OSError('fictional disk error')):
            with self.assertRaises(OSError):
                terminal_ui.interaction(interface)
        self.assertEqual(self.draft.read_bytes(), before)
        self.assertEqual(interface.model, self.record['values'])
        self.assertEqual(interface.step, 8)


if __name__ == '__main__':
    unittest.main()
