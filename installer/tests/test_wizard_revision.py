"""Safe summary editing and resuming a partially started installation."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import configuration_schema as schema
import state_store
import terminal_ui as ui
import terminal_views as views


class WizardRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'target'
        self.output = io.StringIO()
        output = patch('sys.stdout', self.output)
        output.start()
        self.addCleanup(output.stop)
        self.interface = ui.UI(
            self.root, draft=self.base / 'draft.json', view=views.TerminalView(plain=True)
        )
        self.interface.model = {'DOMAIN': 'example.org'}

    def answers(self, values):
        values = iter(values)
        self.interface.reader = lambda *_: next(values)

    def test_mas_prompt_uses_actual_domain_and_empty_keeps_automatic_value(self):
        self.answers([''])
        self.interface.field('MAS_DOMAIN')
        self.assertIn('mas.example.org', self.output.getvalue())
        self.assertNotIn('<DOMAIN>', self.output.getvalue())
        self.assertEqual(self.interface.model['MAS_DOMAIN'], '')

    def test_authorization_step_does_not_ask_for_invitation_lifetime(self):
        self.interface.model['INVITATION_LIFETIME_SECONDS'] = '3600'
        self.answers(['нет', 'нет'])
        with patch.object(self.interface, 'field') as field:
            self.interface.setup_step(4)
        field.assert_not_called()
        self.assertEqual(self.interface.model['INVITATION_LIFETIME_SECONDS'], '3600')

    def test_summary_unknown_word_stays_on_summary_and_enter_saves_without_applying(self):
        self.answers(['unexpected', ''])
        self.interface.step = 6
        with (
            patch.object(self.interface, 'run') as run,
            patch.object(self.interface, 'setup_step') as step,
        ):
            with self.assertRaises(ui.SaveExit):
                self.interface.setup_summary()
        step.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.interface.step, 6)
        self.assertFalse(self.root.exists())
        self.assertIn('номер поля или команду', self.output.getvalue())
        self.assertIn('Внешние копии: отложены', self.output.getvalue())
        self.assertNotIn('явно отложены', self.output.getvalue())

    def test_summary_number_edits_only_that_field_and_returns_to_summary(self):
        self.interface.dns_verified = self.interface.dns_values()
        model = schema.validate(self.interface.model)
        fields = [
            key
            for key, value in model.items()
            if key
            in ('DOMAIN', 'MAS_DOMAIN', 'PUBLIC_IP', 'ACME_EMAIL', 'MEDIA_MODE', 'POSTGRES_MODE')
            or (value and value != schema.FIELDS[key].default)
        ]
        number = str(fields.index('ACME_EMAIL') + 1)
        self.answers([number, 'operator@example.org', 'применить'])
        self.interface.setup_summary()
        self.assertEqual(self.interface.model['ACME_EMAIL'], 'operator@example.org')
        self.assertEqual(self.interface.backup, {})
        self.assertEqual(self.interface.model['DOMAIN'], 'example.org')
        self.assertIn('применить — начать установку', self.output.getvalue())

    def test_optional_admin_name_is_validated_and_persisted_in_draft(self):
        self.answers(['server-guide', 'With Spaces', 'panel-team'])
        self.interface.setup_admin_tag()
        saved = json.loads(self.interface.draft.read_text())
        self.assertEqual(saved['admin_tag'], 'panel-team')
        self.assertNotIn('admin_tag', saved['values'])
        self.assertEqual(ui.UI(self.root, draft=self.interface.draft).admin_tag, 'panel-team')

    def create_initialized(self):
        self.root.mkdir(mode=0o700)
        configuration.write_model(
            self.root, {'DOMAIN': 'example.org', 'ACME_EMAIL': 'saved@example.org'}
        )
        state_store.write_json(
            self.root / 'installation.json',
            {
                'domain': 'example.org',
                'config_format_version': 2,
                'databases_initialized': True,
                'mas_issuer': 'https://mas.example.org/',
            },
        )
        state_store.write_text(self.root / 'identity.fixture', 'FICTIONAL_IDENTITY')

    def test_resume_after_database_creation_runs_start_then_check_without_reapplying(self):
        self.create_initialized()
        self.interface.step = 7
        self.interface.model['ACME_EMAIL'] = 'stale@example.org'
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with (
            patch.object(self.interface, 'run', return_value=0) as run,
            patch.object(self.interface, 'finish'),
            patch.object(configuration, 'write_model') as write,
        ):
            self.assertEqual(self.interface.wizard(), 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], ['start', 'check'])
        write.assert_not_called()
        self.assertEqual(self.interface.model['ACME_EMAIL'], 'saved@example.org')
        self.assertEqual(
            before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        )

    def test_retry_failed_start_uses_same_step_after_database_initialization(self):
        self.create_initialized()
        self.interface.step = 7
        self.answers(['повторить'])
        with (
            patch.object(self.interface, 'run', side_effect=[1, 0, 0]) as run,
            patch.object(self.interface, 'finish'),
            patch.object(configuration, 'write_model') as write,
        ):
            self.assertEqual(self.interface.wizard(), 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], ['start', 'start', 'check'])
        write.assert_not_called()
        self.assertNotIn('config validate', self.output.getvalue())

    @unittest.skipIf(views.Console is None, 'Rich is optional')
    def test_details_uses_available_width_for_labels(self):
        output = io.StringIO()
        view = views.TerminalView(
            console=views.Console(file=output, force_terminal=False, width=120)
        )
        label = '1. Максимальный размер вложения, МиБ (1–65536)'
        view.details([(label, '256')])
        self.assertIn(label, output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    @unittest.skipIf(views.Console is None, 'Rich is optional')
    def test_summary_pages_keep_commands_and_numbers_in_short_terminal(self):
        import os

        for width, height in ((80, 24), (120, 32), (40, 18)):
            with self.subTest(width=width, height=height):
                output = io.StringIO()
                console = views.Console(
                    file=output, force_terminal=True, width=width, height=height
                )
                self.interface.view = views.TerminalView(console=console)
                self.interface.model = {'DOMAIN': 'example.org'}
                pages = []
                responses = iter(['>', '<', 'unexpected', ''])

                def reader(*_):
                    pages.append(self.interface.view.used_lines)
                    self.assertLessEqual(self.interface.view.used_lines, height - 2)
                    return next(responses)

                self.interface.reader = reader
                with (
                    patch.dict(os.environ, {'TERM': 'xterm'}, clear=True),
                    self.interface.view.screen(),
                ):
                    with self.assertRaises(ui.SaveExit):
                        self.interface.setup_summary()
                self.assertEqual(len(pages), 4)
                self.assertIn('номер поля', output.getvalue())
                self.assertIn('сохранить и выйти', output.getvalue())


if __name__ == '__main__':
    unittest.main()
