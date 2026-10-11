"""Rich presentation for SSH, with a plain fallback and literal user values."""

import os
import sys
from contextlib import contextmanager

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text
except ImportError:
    Console = None


def literal(value):
    """Never allow terminal control sequences in labels or configuration values."""
    return ''.join(
        char
        if char in '\n\t' or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
        else '\\x' + format(ord(char), '02x')
        for char in str(value)
    )


class TerminalView:
    def __init__(self, console=None, plain=False):
        self.console = None if plain else console
        self.fullscreen = False
        self.screen_used = False
        self.page = []
        self.used_lines = 0
        if console is None and Console is not None and not plain:
            if sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb':
                self.console = Console(markup=False, highlight=False, emoji=False)

    @contextmanager
    def screen(self):
        if (
            self.console is None
            or not self.console.is_terminal
            or os.environ.get('TERM', '') in ('', 'dumb')
            or 'NO_COLOR' in os.environ
        ):
            yield
            return
        with self.console.screen(hide_cursor=False):
            self.fullscreen = self.console.is_alt_screen
            self.screen_used = self.fullscreen
            try:
                yield
            finally:
                self.fullscreen = False

    @property
    def compact(self):
        return self.console is None or self.console.width < 60

    def lines(self, renderable):
        return max(1, len(self.console.render_lines(renderable, pad=False)))

    def print(self, renderable):
        if self.fullscreen:
            self.page.append(renderable)
            self.used_lines += self.lines(renderable)
        self.console.print(renderable, overflow='fold')

    def snapshot(self):
        return list(self.page) if self.fullscreen and self.console else None

    def redraw(self, snapshot):
        if snapshot is None:
            return
        self.console.clear(home=True)
        self.page = []
        self.used_lines = 0
        for renderable in snapshot:
            if isinstance(renderable, Panel):
                renderable.width = self.console.width
            self.print(renderable)

    @contextmanager
    def busy(self, label):
        if (
            self.console is None
            or not self.console.is_terminal
            or self.console.no_color
            or os.environ.get('TERM') == 'dumb'
        ):
            self.message(label)
            yield
            return
        animation = Group(
            Spinner('dots', text=Text(literal(label)), style='cyan'),
            ProgressBar(total=None, pulse=True, width=None, pulse_style='cyan'),
        )
        with Live(animation, console=self.console, transient=True, refresh_per_second=8):
            yield

    def message(self, message, style=''):
        value = literal(message)
        if self.console is None:
            print(value, flush=True)
        else:
            self.print(Text(value, style=style))

    def heading(self, title, subtitle=''):
        if self.fullscreen:
            self.console.clear(home=True)
            self.page = []
            self.used_lines = 0
        if self.compact:
            self.message('\n' + title, 'bold cyan')
            if subtitle:
                self.message(subtitle, 'dim')
            return
        content = Text()
        content.append(literal(title), style='bold')
        if subtitle:
            content.append('\n' + literal(subtitle), style='dim')
        self.print(
            Panel(
                content,
                box=box.ROUNDED,
                border_style='cyan',
                padding=(0, 1) if self.fullscreen else (1, 2),
                width=self.console.width if self.fullscreen else min(self.console.width, 100),
            )
        )

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
        self.print(table)

    def choice_table(self, choices, show_hints=True):
        table = Table.grid(padding=(0, 1), pad_edge=False)
        table.add_column(width=2, justify='right')
        table.add_column(overflow='fold')
        for number, label, hint in choices:
            content = Text()
            content.append(literal(label), style='bold' if number != '0' else 'dim')
            if hint and show_hints:
                content.append('\n' + literal(hint), style='dim')
            table.add_row(Text(number, style='cyan'), content)
        return table

    def choice_pages(self, title, choices, exit_label, keep_hints=False):
        if not self.fullscreen or self.console is None:
            return [choices], True
        reserve = self.lines(Text('\n' + title)) + self.lines(Text(self.footer_text(exit_label)))
        navigation = self.lines(Text('Страница 99/99 · > далее · < ранее'))
        room = max(2, self.console.height - self.used_lines - reserve - navigation - 2)
        if self.lines(self.choice_table(choices)) <= room:
            return [choices], True
        if not keep_hints and self.lines(self.choice_table(choices, False)) <= room:
            return [choices], False
        exit_choice = choices[-1]
        pages, page = [], []
        for choice in choices[:-1]:
            if (
                page
                and self.lines(self.choice_table([*page, choice, exit_choice], keep_hints)) > room
            ):
                pages.append([*page, exit_choice])
                page = []
            page.append(choice)
        pages.append([*page, exit_choice])
        return pages, keep_hints

    def options(self, title, choices, show_hints=True):
        self.message('\n' + title, 'bold')
        if self.compact and not self.fullscreen:
            for number, label, hint in choices:
                self.message(number + '  ' + label, 'cyan')
                if hint and show_hints:
                    self.message('   ' + hint, 'dim')
            return
        self.print(self.choice_table(choices, show_hints))

    def wizard_step(self, index, names):
        self.heading(
            'Первоначальная настройка',
            'Шаг ' + str(index + 1) + ' из ' + str(len(names)) + ' · ' + names[index],
        )
        if not self.compact:
            self.print(
                ProgressBar(
                    total=len(names),
                    completed=index,
                    width=min(self.console.width - 2, 60),
                    complete_style='cyan',
                    finished_style='cyan',
                )
            )
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

    @staticmethod
    def footer_text(exit_label):
        if exit_label == 'Назад':
            return 'Назад: 0 или «назад» · Выход: «сохранить и выйти»'
        return 'Выход: 0 или «сохранить и выйти»'

    def footer(self, exit_label='Назад'):
        label = self.footer_text(exit_label)
        if self.fullscreen and self.console:
            padding = self.console.height - self.used_lines - self.lines(Text(label)) - 2
            if padding > 0:
                self.print(Text('\n' * (padding - 1)))
        self.message(label, 'dim')
