"""Build the standalone release bootstrap asset without a runtime dependency."""

from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[1]


def render():
    shell = (ROOT / 'installer/bootstrap.sh').read_text()
    python = (ROOT / 'installer/bootstrap.py').read_text()
    # Python receives code as an argument: the wizard inherits the original terminal.
    return shell + 'exec /usr/bin/python3 -I -B -c ' + shlex.quote(python) + ' "$@"\n'


def build():
    target = ROOT / 'dist/install.sh'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render())
    target.chmod(0o755)
    print(target)


if __name__ == '__main__':
    build()
