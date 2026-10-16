"""Discarding a private wizard draft never removes a server or recovery files."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import command_wrapper
import misbot
import state_store
import wizard_state


class WizardStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir(mode=0o700)
        self.root = Path(self.temp.name) / 'matrix-stack'
        home = patch.object(Path, 'home', return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.path = wizard_state.draft_path(self.root)

    def save(self, **values):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_store.write_json(
            self.path,
            dict(
                format=1,
                directory=str(self.root),
                step=6,
                values={'DOMAIN': 'fixture.example', 'GOOGLE_CLIENT_SECRET': 'PRIVATE_FIXTURE'},
                backup={},
            )
            | values,
        )

    def test_show_absent_draft_does_not_create_any_directory(self):
        self.assertEqual(wizard_state.manage(self.root, 'show')['state'], 'absent')
        self.assertFalse(self.path.parent.exists())
        self.assertFalse(self.root.exists())

    def test_control_characters_are_rejected_without_terminal_sequences_or_file_changes(self):
        invalid = '/tmp/fictional\x1b[2J'
        with self.assertRaises(ValueError):
            wizard_state.validate_directory(invalid)
        with (
            patch.object(misbot.os, 'geteuid', return_value=0),
            patch('sys.stderr', new=io.StringIO()) as output,
        ):
            self.assertEqual(misbot.main(['remove', '--directory', invalid]), 2)
        self.assertNotIn('\x1b', output.getvalue())
        self.assertFalse(self.root.exists())

    def test_show_reveals_only_location_and_step(self):
        self.save()
        before = self.path.read_bytes()
        result = wizard_state.manage(self.root, 'show')
        self.assertEqual(result['state'], 'saved')
        self.assertEqual(result['step'], 7)
        self.assertNotIn('PRIVATE_FIXTURE', json.dumps(result))
        self.assertNotIn('values', result)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reset_requires_exact_confirmation_then_is_idempotent(self):
        self.save()
        with self.assertRaises(ValueError):
            wizard_state.manage(self.root, 'reset', 'yes')
        self.assertTrue(self.path.exists())
        self.assertEqual(
            wizard_state.manage(self.root, 'reset', 'discard-draft')['state'], 'discarded'
        )
        self.assertEqual(
            wizard_state.manage(self.root, 'reset', 'discard-draft')['state'], 'absent'
        )

    def test_reset_preserves_active_settings_checkpoints_and_other_drafts(self):
        self.save()
        self.root.mkdir(mode=0o700)
        protected = [
            self.root / 'installation.json',
            self.root / 'settings.env',
            self.root / 'federation-transition.json',
            self.path.parent / 'other-draft.json',
            self.path.parent / 'previous/saved.json',
        ]
        for path in protected:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            state_store.write_text(path, 'PRIVATE_FIXTURE_CHECKPOINT')
        wizard_state.manage(self.root, 'reset', 'discard-draft')
        self.assertFalse(self.path.exists())
        for path in protected:
            self.assertEqual(path.read_text(), 'PRIVATE_FIXTURE_CHECKPOINT')

    def test_foreign_or_malformed_draft_is_retained(self):
        for values in ({'directory': str(self.root) + '-other'}, {'step': -1}, {'values': []}):
            with self.subTest(values=values):
                self.save(**values)
                before = self.path.read_bytes()
                with self.assertRaises(RuntimeError):
                    wizard_state.manage(self.root, 'reset', 'discard-draft')
                self.assertEqual(self.path.read_bytes(), before)

    def test_symlinks_hardlinks_and_public_permissions_are_rejected(self):
        self.save()
        target = self.path.parent / 'target.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises((RuntimeError, ValueError)):
            wizard_state.manage(self.root, 'reset', 'discard-draft')
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises((RuntimeError, ValueError)):
            wizard_state.manage(self.root, 'reset', 'discard-draft')
        self.path.unlink()
        target.rename(self.path)
        self.path.chmod(0o644)
        with self.assertRaises((RuntimeError, ValueError)):
            wizard_state.manage(self.root, 'reset', 'discard-draft')
        self.assertTrue(self.path.exists())

    def test_relative_parent_or_root_directory_is_rejected(self):
        for root in (Path('relative'), Path('/'), self.root / '..' / 'other'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                wizard_state.manage(root, 'reset', 'discard-draft')

    def test_cli_draft_and_remove_work_without_installation_metadata(self):
        source = Path(misbot.__file__).parent
        with patch.object(command_wrapper, 'default_directory', return_value=None):
            for action in ('show', 'reset'):
                arguments = ['config', 'draft', action, '--directory', str(self.root)]
                if action == 'reset':
                    arguments += ['--confirm', 'discard-draft']
                command, options, values = misbot.parse(arguments)
                planned = misbot.plan(command, options, values, source)
                self.assertEqual(Path(planned[1]), source / 'wizard_state.py')
                self.assertIn(action, planned)
            with (
                patch.object(misbot.os, 'geteuid', return_value=0),
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                self.assertEqual(misbot.main(['remove', '--directory', str(self.root)]), 2)
            self.assertIn(str(self.root), output.getvalue())
            self.assertIn('config draft reset --confirm discard-draft', output.getvalue())
            self.assertNotIn('config init', output.getvalue())


if __name__ == '__main__':
    unittest.main()
