"""Rendering must preserve literal text and work without Rich or terminal color."""
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import terminal_views as views


class TerminalViewTests(unittest.TestCase):
    def test_plain_fallback_without_rich_escapes_control_characters(self):
        with patch.object(views,'Console',None),patch('sys.stdout',new=io.StringIO()) as output:
            view=views.TerminalView()
            view.heading('Misbot','[link=https://example.org/]literal[/link]\x1b[2J')
            view.options('Menu',[('1','Status','No changes')])
        self.assertNotIn('\x1b',output.getvalue())
        self.assertIn('[link=https://example.org/]',output.getvalue())
        self.assertIn('\\x1b[2J',output.getvalue())

    @unittest.skipIf(views.Console is None,'Rich is an optional rendering dependency')
    def test_rich_never_treats_configuration_values_as_markup(self):
        output=io.StringIO()
        console=views.Console(file=output,width=80,force_terminal=False,record=True)
        view=views.TerminalView(console=console)
        payload='[bold]literal[/bold]\x1b]8;;https://example.org/\x07'
        view.heading(payload)
        view.details([('Site',payload)])
        result=console.export_text()
        self.assertIn('[bold]literal[/bold]',result)
        self.assertNotIn('\x1b',result)
        self.assertNotIn('\x07',result)

    @unittest.skipIf(views.Console is None,'Rich is an optional rendering dependency')
    def test_small_terminal_keeps_long_labels_and_continuation(self):
        output=io.StringIO()
        console=views.Console(file=output,width=24,force_terminal=False)
        view=views.TerminalView(console=console)
        view.options('Menu',[('1','Продолжить прерванную операцию','Проверьте состояние сервера')])
        self.assertTrue(view.compact)
        self.assertIn('прерванную',output.getvalue())
        self.assertIn('операцию',output.getvalue())
        self.assertIn('состояние',output.getvalue())
        self.assertTrue(all(len(line)<=24 for line in output.getvalue().splitlines()))

    def test_dumb_terminal_keeps_plain_output(self):
        with patch.dict(os.environ,{'TERM':'dumb'}),patch('sys.stdout.isatty',return_value=True):
            self.assertIsNone(views.TerminalView().console)

    @unittest.skipIf(views.Console is None,'Rich is an optional rendering dependency')
    def test_no_color_uses_no_color_console(self):
        with patch.dict(os.environ,{'TERM':'xterm','NO_COLOR':'1'}),patch('sys.stdout.isatty',return_value=True):
            self.assertTrue(views.TerminalView().console.no_color)


if __name__=='__main__':unittest.main()
