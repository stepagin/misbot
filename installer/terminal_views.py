"""Rich presentation for SSH, with a plain fallback and literal user values."""

import os
import sys
from collections import deque
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


class BusyFrame:
    """One terminal region: animated heading above a bounded window of output."""

    def __init__(self, view, label, animated):
        self.view = view
        self.label = literal(label)
        self.animated = animated
        self.logs = deque(maxlen=100)
        if animated:
            self.spinner = Spinner('dots', text=Text(self.label), style='cyan')

    def log(self, line):
        value = literal(line)[:4096]
        self.logs.append(value)
        if not self.animated:
            self.view.message(value)

    def __rich__(self):
        console = self.view.console
        budget = max(1, console.height - 5)
        lines = []
        used = 0
        for value in reversed(list(self.logs)):
            rendered = Text(value, overflow='fold')
            height = self.view.lines(rendered)
            if used + height > budget:
                if not lines:
                    lines.append(Text(value, overflow='ellipsis', no_wrap=True))
                break
            lines.append(rendered)
            used += height
        return Group(
            self.spinner,
            ProgressBar(total=None, pulse=True, width=None, pulse_style='cyan'),
            *reversed(lines),
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
        animated = bool(
            self.console is not None
            and self.console.is_terminal
            and not self.console.no_color
            and os.environ.get('TERM') != 'dumb'
        )
        progress = BusyFrame(self, label, animated)
        if not animated:
            self.message(label)
            yield progress
            return
        if self.fullscreen:
            self.console.clear(home=True)
            self.page = []
            self.used_lines = 0
        with Live(
            progress,
            console=self.console,
            transient=True,
            refresh_per_second=8,
            redirect_stdout=False,
            redirect_stderr=False,
        ):
            yield progress
        # Preserve the final bounded output for read-only tasks such as bot settings.
        self.message(label)
        if self.fullscreen:
            budget = max(1, self.console.height - self.used_lines - 5)
            selected = []
            used = 0
            for line in reversed(list(progress.logs)):
                size = self.lines(Text(line))
                if used + size > budget:
                    if not selected:
                        self.print(Text(line, overflow='ellipsis', no_wrap=True))
                        self.message('Последняя строка сокращена по ширине окна.', 'dim')
                    break
                selected.append(line)
                used += size
            for line in reversed(selected):
                self.message(line)
        else:
            for line in progress.logs:
                self.message(line)

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

    def details(self, rows, *, measure=False):
        rows = list(rows)
        if self.compact:
            if measure:
                if self.console is None:
                    return len(rows)
                return sum(
                    self.lines(Text(literal(label) + ': ' + literal(value)))
                    for label, value in rows
                )
            for label, value in rows:
                self.message(literal(label) + ': ' + literal(value))
            return
        table = Table.grid(padding=(0, 2), expand=True)
        label_width = max((Text(literal(label)).cell_len for label, _ in rows), default=1)
        table.add_column(
            style='dim',
            width=min(label_width, max(16, self.console.width * 2 // 3)),
            overflow='fold',
        )
        table.add_column(ratio=1, overflow='fold')
        for label, value in rows:
            table.add_row(Text(literal(label)), Text(literal(value)))
        if measure:
            return self.lines(table)
        self.print(table)

    def choice_table(self, choices, show_hints=True):
        table = Table.grid(padding=(0, 1), pad_edge=False)
        table.add_column(
            width=max(2, max((len(choice[0]) for choice in choices), default=0)), justify='right'
        )
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
