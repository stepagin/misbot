"""Run local/CI checks and build verified archives without publishing."""

import argparse
from collections.abc import Sequence
import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from typing import IO

ROOT = Path(__file__).resolve().parents[1]


class CheckError(RuntimeError):
    pass


def run(label: str, command: list[str]) -> None:
    print('Check: ' + label, flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def quality_checks() -> None:
    for command in (
        [sys.executable, '-m', 'ruff', 'format', '--check', '.'],
        [sys.executable, '-m', 'ruff', 'check', '.'],
        [sys.executable, '-m', 'mypy'],
        ['npm', 'run', 'format:check'],
        ['npm', 'run', 'lint'],
    ):
        run('source quality', command)


def test_checks(python: str) -> None:
    for folder in ('installer/tests', 'bots/telegram/tests', 'deployment/tests', 'ci/tests'):
        run(folder, [python, '-m', 'unittest', 'discover', '-s', folder, '-p', 'test_*.py'])
    run('referral bot', ['npm', 'test', '--prefix', 'bots/matrix-referral'])
    files = sorted(ROOT.glob('bots/matrix-federation/tests/*.test.js'))
    if not files:
        raise CheckError('Federation bot tests were not found')
    run('federation bot', ['node', '--test', *[str(path.relative_to(ROOT)) for path in files]])
    for path in sorted(ROOT.glob('installer/**/*.sh')):
        run('shell syntax', ['bash', '-n', str(path.relative_to(ROOT))])


def stream_digest(stream: IO[bytes]) -> str:
    result = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        result.update(chunk)
    return result.hexdigest()


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return stream_digest(stream)


def verify_archive(path: Path) -> str:
    checksum = digest(path)
    if path.with_name(path.name + '.sha256').read_text(encoding='ascii') != (
        checksum + '  ' + path.name + '\n'
    ):
        raise CheckError('Archive checksum does not match')
    manifest = (ROOT / 'installer/SHA256SUMS').read_bytes()
    entries = [line.split('  ', 1) for line in manifest.decode('utf-8').splitlines()]
    names = ['matrix-installer/' + name for _, name in entries]
    names.append('matrix-installer/SHA256SUMS')
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        if (
            len(names) != len(set(names))
            or len(members) != len(names)
            or {member.name for member in members} != set(names)
            or not all(member.isfile() for member in members)
        ):
            raise CheckError('Archive contains missing, duplicate or unexpected files')
        for expected, name in [*entries, (hashlib.sha256(manifest).hexdigest(), 'SHA256SUMS')]:
            stream = archive.extractfile('matrix-installer/' + name)
            if stream is None:
                raise CheckError('Archive contains an unreadable file')
            with stream:
                if stream_digest(stream) != expected:
                    raise CheckError('Archive contents do not match the source manifest')
    return checksum


def build_checks() -> None:
    run('build archive and checksums', [sys.executable, 'installer/build.py'])
    first = verify_archive(ROOT / 'dist/matrix-installer.tar.gz')
    with tempfile.TemporaryDirectory(prefix='misbot-rebuild-') as folder:
        second = Path(folder) / 'matrix-installer.tar.gz'
        run('reproducible build', [sys.executable, 'installer/build.py', '--output', str(second)])
        if verify_archive(second) != first:
            raise CheckError('Repeated builds produced different archives')
    run('source manifest', [sys.executable, 'installer/build.py', '--check'])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'action', nargs='?', choices=('quality', 'tests', 'build', 'all'), default='quality'
    )
    parser.add_argument(
        '--test-python',
        default=sys.executable,
        help='Python interpreter with runtime test dependencies',
    )
    parser.add_argument(
        '--check-manifest',
        action='store_true',
        help='Require current SHA256SUMS before any tests or build (CI)',
    )
    args = parser.parse_args(argv)
    if args.check_manifest and args.action not in ('build', 'all'):
        parser.error('--check-manifest requires build or all')
    try:
        if args.check_manifest:
            run('committed source manifest', [sys.executable, 'installer/build.py', '--check'])
        if args.action in ('quality', 'all'):
            quality_checks()
        if args.action in ('tests', 'all'):
            test_checks(args.test_python)
        if args.action in ('build', 'all'):
            build_checks()
    except subprocess.CalledProcessError as error:
        print('Checks stopped after a failed command.', file=sys.stderr)
        return 128 - error.returncode if error.returncode < 0 else error.returncode
    except KeyboardInterrupt:
        print('Checks interrupted.', file=sys.stderr)
        return 130
    except CheckError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, tarfile.TarError):
        print(
            'Cannot run checks or read artifacts; check tools and file permissions.',
            file=sys.stderr,
        )
        return 1
    print('Checks completed.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
