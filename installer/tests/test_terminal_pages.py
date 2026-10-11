"""Menu transitions fit the terminal; waiting remains visible and cancellable."""

import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import state_store
import terminal_ui as ui
import terminal_views as views
import ui_actions


@unittest.skipIf(views.Console is None, 'Rich is optional')
class MenuPageTests(unittest.TestCase):
    def view(self, width=80, height=24):
        output = io.StringIO()
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
            console = views.Console(
                file=output, force_terminal=True, no_color=False, width=width, height=height
            )
        return views.TerminalView(console=console), output

    def test_main_menu_fits_height_and_keeps_every_action_and_exit(self):
        for width, height in ((80, 24), (120, 32), (40, 24), (80, 18)):
            with self.subTest(width=width, height=height):
                view, _ = self.view(width, height)
                with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
                    view.heading('Misbot 1.2.0', 'Управление сервером example.org')
                    view.details(
                        [('Установка', '/opt/fictional-stack'), ('Режим', 'Установка Misbot')]
                    )
                    prefix = view.snapshot()
                    choices = [*ui_actions.MAIN, ('0', 'Выйти из Misbot', '')]
                    pages, hints = view.choice_pages('Главное меню', choices, 'Выйти из Misbot')
                    numbers = []
                    for page in pages:
                        view.redraw(prefix)
                        view.options('Главное меню', page, hints)
                        if len(pages) > 1:
                            view.message('Страница 1/9 · > далее · < ранее')
                        view.footer('Выйти из Misbot')
                        self.assertLessEqual(view.used_lines, height - 2)
                        self.assertEqual(page[-1][0], '0')
                        numbers.extend(choice[0] for choice in page[:-1])
                    self.assertEqual(numbers, [item[0] for item in ui_actions.MAIN])

    def test_pages_keep_numbers_and_back_while_resize_and_invalid_input(self):
        view, output = self.view(40, 18)
        answers = iter(['>', '<', 'invalid', '11'])
        frames = []

        def reader(*args):
            frames.append(view.used_lines)
            self.assertLessEqual(view.used_lines, view.console.height - 2)
            answer = next(answers)
            if answer == '<':
                view.console.width = 80
                view.console.height = 32
            return answer

        with tempfile.TemporaryDirectory() as folder:
            interface = ui.UI(folder, reader=reader, draft=Path(folder) / 'draft', view=view)
            with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
                view.heading('Misbot')
                self.assertEqual(interface.choose('Главное меню', ui_actions.MAIN), '11')
        self.assertEqual(len(frames), 4)
        self.assertIn('Страница', output.getvalue())
        self.assertIn('Выберите номер из списка', output.getvalue())

    def test_main_menu_redraws_after_settings_site_and_about(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'target'
            root.mkdir(mode=0o700)
            state_store.write_json(
                root / 'installation.json',
                {
                    'domain': 'example.org',
                    'databases_initialized': True,
                    'config_format_version': 2,
                    'mas_issuer': 'https://mas.example.org/',
                },
            )
            configuration.write_model(root, {'DOMAIN': 'example.org'})
            view, output = self.view()
            answers = iter(['7', '2', 'fictional@example.org', '0', '5', '0', '11', '', '0'])
            frames = []

            def reader(*args):
                frame = output.getvalue().rsplit('\x1b[2J\x1b[H', 1)[-1]
                frames.append(frame)
                return next(answers)

            interface = ui.UI(root, draft=Path(folder) / 'draft', reader=reader, view=view)
            before = configuration.load(root, True)
            with (
                patch.dict(os.environ, {'TERM': 'xterm'}, clear=True),
                view.screen(),
                patch.object(interface, 'run', return_value=0),
                patch.object(configuration, 'apply_settings') as apply,
            ):
                self.assertEqual(interface.menu(), 0)
            apply.assert_not_called()
            self.assertEqual(configuration.load(root, True), before)
            main_frames = [frame for frame in frames if 'Главное меню' in frame]
            self.assertEqual(len(main_frames), 4)
            for frame in main_frames:
                self.assertIn('Misbot 1.2.0', frame)
                self.assertNotIn('fictional@example.org', frame)
            self.assertNotIn('Главное меню', frames[2])
            self.assertIn('fictional@example.org', frames[3])

    def test_busy_animates_without_input_and_restores_cursor_after_failure(self):
        for error in (None, RuntimeError('fictional'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                view, output = self.view()
                with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
                    try:
                        with view.busy('Ожидаем проверку [literal]'):
                            time.sleep(0.3)
                            if error:
                                raise error
                    except (RuntimeError, KeyboardInterrupt) as caught:
                        self.assertIs(caught, error)
                rendered = output.getvalue()
                self.assertGreater(rendered.count('Ожидаем проверку [literal]'), 1)
                self.assertIn('\x1b[?25l', rendered)
                self.assertIn('\x1b[?25h', rendered)

    def test_plain_and_no_color_loading_use_one_static_message(self):
        for plain, environment in (
            (True, {'TERM': 'xterm'}),
            (False, {'TERM': 'xterm', 'NO_COLOR': '1'}),
        ):
            with self.subTest(plain=plain), patch.dict(os.environ, environment, clear=True):
                view, output = self.view()
                if plain:
                    view = views.TerminalView(plain=True)
                else:
                    view.console.no_color = True
                with patch('sys.stdout', new=output), view.busy('Ожидаем проверку'):
                    pass
                self.assertNotIn('\x1b', output.getvalue())
                self.assertEqual(output.getvalue().count('Ожидаем проверку'), 1)


if __name__ == '__main__':
    unittest.main()
