"""Rich presentation for SSH, with a plain fallback and literal user values."""
import os
import sys

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text
except ImportError:
    Console = None


def literal(value):
    """Never allow terminal control sequences in labels or configuration values."""
    return ''.join(
        char if char in '\n\t' or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
        else '\\x' + format(ord(char), '02x') for char in str(value)
    )


class TerminalView:
    def __init__(self, console=None, plain=False):
        self.console = console
        if console is None and Console is not None and not plain:
            if sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb':
                self.console = Console(markup=False, highlight=False, emoji=False)

    @property
    def compact(self):
        return self.console is None or self.console.width < 60

    def message(self, message, style=''):
        value = literal(message)
        if self.console is None:
            print(value, flush=True)
        else:
            self.console.print(Text(value, style=style), overflow='fold')

    def heading(self, title, subtitle=''):
        if self.compact:
            self.message('\n' + title, 'bold cyan')
            if subtitle:
                self.message(subtitle, 'dim')
            return
        content = Text()
        content.append(literal(title), style='bold')
        if subtitle:
            content.append('\n' + literal(subtitle), style='dim')
        self.console.print(Panel(content, box=box.ROUNDED, border_style='cyan',
                                 padding=(1, 2), width=min(self.console.width, 100)))

    def details(self, rows):
        if self.compact:
            for label, value in rows:
                self.message(literal(label) + ': ' + literal(value))
            return
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(style='dim', ratio=1, overflow='fold')
        table.add_column(ratio=3, overflow='fold')
        for label, value in rows:
            table.add_row(Text(literal(label)), Text(literal(value)))
        self.console.print(table)

    def options(self, title, choices):
        self.message('\n' + title, 'bold')
        if self.compact:
            for number, label, hint in choices:
                self.message(number + '  ' + label, 'cyan')
                if hint:
                    self.message('   ' + hint, 'dim')
            return
        table = Table.grid(padding=(0, 1), pad_edge=False)
        table.add_column(width=2, justify='right')
        table.add_column(overflow='fold')
        for number, label, hint in choices:
            content = Text()
            content.append(literal(label), style='bold' if number != '0' else 'dim')
            if hint:
                content.append('\n' + literal(hint), style='dim')
            table.add_row(Text(number, style='cyan'), content)
        self.console.print(table)

    def wizard_step(self, index, names):
        self.heading('Первоначальная настройка',
                     'Шаг ' + str(index + 1) + ' из ' + str(len(names)) + ' · ' + names[index])
        if not self.compact:
            self.console.print(ProgressBar(total=len(names), completed=index,
                                           width=min(self.console.width - 2, 60),
                                           complete_style='cyan', finished_style='cyan'))
        if index + 1 < len(names):
            self.message('Далее: ' + names[index + 1], 'dim')

    def result(self, title, code, continuation=''):
        if code == 0:
            self.message('Готово: ' + title, 'green')
        elif code == 130:
            self.message('Операция прервана: ' + title, 'yellow')
        else:
            self.message('Операция не завершена: ' + title + ' (код ' + str(code) + ')', 'yellow')
        if code and continuation:
            self.message(continuation)

    def footer(self, exit_label='Назад'):
        if exit_label == 'Назад':
            self.message('Назад: 0 или «назад» · Выход: «сохранить и выйти»', 'dim')
        else:
            self.message('Выход: 0 или «сохранить и выйти»', 'dim')
