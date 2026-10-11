"""Run source quality checks using the isolated development environment."""

import subprocess
import sys


def main() -> None:
    for command in (
        [sys.executable, '-m', 'ruff', 'format', '--check', '.'],
        [sys.executable, '-m', 'ruff', 'check', '.'],
        [sys.executable, '-m', 'mypy'],
        ['npm', 'run', 'format:check'],
        ['npm', 'run', 'lint'],
    ):
        subprocess.run(command, check=True)


if __name__ == '__main__':
    main()
