"""Bootstrap a verified release, or reopen the existing protected system command."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile

REPOSITORY = 'https://github.com/stepagin/misbot'
API = 'https://api.github.com/repos/stepagin/misbot/releases'
FAMILY = 'misbot-matrix-portable'
BASE = Path('/var/lib/misbot-bootstrap')
COMMAND = Path('/usr/local/bin/misbot')
LIBRARY = Path('/usr/local/lib/misbot')
ARCHIVE = 'matrix-installer.tar.gz'
VERSION = re.compile(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
MAX_SIZE = 256 * 1024 * 1024


def protected(path, directory=False):
    """Refuse symlinks and writable ancestors before reading or running root code."""
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Небезопасный родительский каталог: ' + str(parent))
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
        or (not directory and info.st_nlink != 1)
    ):
        raise RuntimeError('Небезопасный файл или каталог: ' + str(path))


def existing_command():
    if not any(path.exists() or path.is_symlink() for path in (COMMAND, LIBRARY)):
        return False
    protected(COMMAND)
    protected(LIBRARY, directory=True)
    for name in ('command.json', 'command_wrapper.py'):
        protected(LIBRARY / name)
    record = json.loads((LIBRARY / 'command.json').read_text())
    if not isinstance(record, dict) or record.get('family') != FAMILY or record.get('format') != 1:
        raise RuntimeError('Системная команда misbot принадлежит другому продукту.')
    hashes = record.get('hashes')
    if not isinstance(hashes, dict):
        raise RuntimeError('Некорректная запись системной команды Misbot.')
    expected = hashes.get('command_wrapper.py')
    if digest_file(LIBRARY / 'command_wrapper.py') != expected:
        raise RuntimeError('Проверка системной команды Misbot не пройдена; файлы сохранены.')
    # Protected installed code supplies its own version-compatible ownership checks.
    spec = importlib.util.spec_from_file_location(
        '_misbot_bootstrap_wrapper', LIBRARY / 'command_wrapper.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.record()
    print('Misbot уже установлен. Открываем его интерфейс без обновления.', flush=True)
    return True


def prepare_packages():
    packages = [
        package
        for command, package in (('curl', 'curl'), ('dig', 'dnsutils'), ('ip', 'iproute2'))
        if not shutil.which(command)
    ]
    for module, package in (('yaml', 'python3-yaml'), ('rich', 'python3-rich')):
        if importlib.util.find_spec(module) is None:
            packages.append(package)
    if not Path('/etc/ssl/certs/ca-certificates.crt').is_file():
        packages.append('ca-certificates')
    if not packages:
        return
    print('Подготавливаем пакеты: ' + ', '.join(packages), flush=True)
    options = [
        '-o',
        'Acquire::http::Timeout=30',
        '-o',
        'Acquire::https::Timeout=30',
        '-o',
        'DPkg::Lock::Timeout=60',
    ]
    environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
    for arguments, timeout in (
        (['update', '-qq'], 600),
        (['install', '-y', '--no-install-recommends', *packages], 900),
    ):
        subprocess.run(
            ['/usr/bin/apt-get', *options, *arguments], env=environment, timeout=timeout, check=True
        )


def download(url, target, allow_missing=False):
    result = subprocess.run(
        [
            '/usr/bin/curl',
            '-q',
            '--fail',
            '--silent',
            '--show-error',
            '--location',
            '--proto',
            '=https',
            '--proto-redir',
            '=https',
            '--connect-timeout',
            '20',
            '--max-time',
            '300',
            '--retry',
            '2',
            '--max-filesize',
            str(MAX_SIZE),
            '--output',
            str(target),
            '--write-out',
            '%{http_code}',
            url,
        ],
        capture_output=True,
        text=True,
        timeout=950,
    )
    if allow_missing and result.returncode == 22 and result.stdout == '404':
        return False
    if result.returncode or result.stdout != '200':
        raise RuntimeError(
            'Не удалось скачать релиз из GitHub. Проверьте сеть и наличие опубликованной версии.'
        )
    return True


def select_release(version, folder):
    metadata = folder / 'release.json'
    endpoints = (
        [API + '/latest']
        if version is None
        else [
            API + '/tags/' + version,
            API + '/tags/' + ('v' + version if not version.startswith('v') else version[1:]),
        ]
    )
    for endpoint in endpoints:
        if download(endpoint, metadata, allow_missing=version is not None):
            break
    else:
        raise RuntimeError('Указанный релиз не опубликован.')
    if metadata.stat().st_size > 1024 * 1024:
        raise RuntimeError('Слишком большой ответ каталога релизов.')
    release = json.loads(metadata.read_text())
    if not isinstance(release, dict):
        raise RuntimeError('Некорректный ответ каталога релизов.')
    tag = release.get('tag_name', '')
    if (
        not isinstance(tag, str)
        or not VERSION.fullmatch(tag)
        or release.get('draft') is not False
        or release.get('prerelease') is not False
        or (version is not None and tag.removeprefix('v') != version.removeprefix('v'))
    ):
        raise RuntimeError('Ожидался опубликованный стабильный релиз Misbot.')
    raw_assets = release.get('assets')
    if not isinstance(raw_assets, list) or not all(
        isinstance(asset, dict) and isinstance(asset.get('name'), str) for asset in raw_assets
    ):
        raise RuntimeError('Некорректный список файлов релиза.')
    assets = {asset['name'] for asset in raw_assets}
    if not {ARCHIVE, ARCHIVE + '.sha256'} <= assets:
        raise RuntimeError('В релизе отсутствует установочный архив или контрольная сумма.')
    return tag


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive_files(archive, checksum, version):
    if checksum.stat().st_size > 1024:
        raise RuntimeError('Некорректный файл контрольной суммы архива.')
    expected = re.fullmatch(r'([a-f0-9]{64})  matrix-installer\.tar\.gz\n?', checksum.read_text())
    if expected is None or digest_file(archive) != expected[1]:
        raise RuntimeError('Контрольная сумма скачанного архива не совпадает.')
    # Parse everything before writing anything; do not use tar extraction permissions/links.
    with tarfile.open(archive, 'r:gz') as package:
        files = {}
        total = 0
        for member in package:
            path = PurePosixPath(member.name)
            total += member.size
            if (
                not member.isfile()
                or member.size < 0
                or total > MAX_SIZE
                or len(path.parts) < 2
                or path.parts[0] != 'matrix-installer'
                or path.is_absolute()
                or '..' in path.parts
                or str(path) != member.name
                or member.name in files
                or any(ord(c) < 32 for c in member.name)
                or '\\' in member.name
                or len(files) >= 10000
            ):
                raise RuntimeError('Архив содержит небезопасные или повторяющиеся пути.')
            files[member.name] = package.extractfile(member).read()
    manifest = files['matrix-installer/SHA256SUMS'].decode('utf-8')
    listed = set()
    for line in manifest.splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  (.+)', line)
        if match is None:
            raise RuntimeError('Некорректный список контрольных сумм.')
        name = 'matrix-installer/' + match[2]
        if (
            name in listed
            or name not in files
            or hashlib.sha256(files[name]).hexdigest() != match[1]
        ):
            raise RuntimeError('Содержимое архива не совпадает со списком контрольных сумм.')
        listed.add(name)
    if set(files) != listed | {'matrix-installer/SHA256SUMS'}:
        raise RuntimeError('В архиве есть файлы вне списка контрольных сумм.')
    release = json.loads(files['matrix-installer/release.json'])
    if (
        not isinstance(release, dict)
        or release.get('family') != FAMILY
        or release.get('version') != version.removeprefix('v')
    ):
        raise RuntimeError('Архив не соответствует выбранной версии Misbot.')
    if 'matrix-installer/misbot.py' not in files:
        raise RuntimeError('В архиве отсутствует интерфейс Misbot.')
    return files


def retain_bundle(files, folder, version):
    destination = BASE / version
    if destination.exists() or destination.is_symlink():
        protected(destination, directory=True)
        found = set()
        for current, directories, names in os.walk(destination, followlinks=False):
            for name in directories:
                protected(Path(current) / name, directory=True)
            for name in names:
                path = Path(current) / name
                protected(path)
                relative = str(path.relative_to(destination))
                if relative not in files or path.read_bytes() != files[relative]:
                    raise RuntimeError(
                        'Сохранённый комплект изменён; файлы не перезаписаны: ' + str(destination)
                    )
                found.add(relative)
        if found != set(files):
            raise RuntimeError(
                'Сохранённый комплект неполон; файлы не перезаписаны: ' + str(destination)
            )
    else:
        staged = folder / 'bundle'
        staged.mkdir(mode=0o700)
        for name, data in files.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data)
            path.chmod(0o700 if path.name == 'install.sh' else 0o600)
        staged.rename(destination)
    return destination / 'matrix-installer'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    args = parser.parse_args(argv)
    if args.version is not None and not VERSION.fullmatch(args.version):
        parser.error('Expected a stable version, for example 1.2.0')
    if sys.version_info < (3, 11):
        raise RuntimeError('Нужен системный Python 3.11 или новее.')
    if os.geteuid() != 0 or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('Запустите загрузчик через sudo в интерактивном SSH-терминале.')
    os.umask(0o077)
    for name in list(os.environ):
        if name.startswith('PYTHON'):
            os.environ.pop(name)
    if existing_command():
        return subprocess.run([str(COMMAND), 'ui'], check=False).returncode
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    prepare_packages()
    if not BASE.exists():
        protected(BASE.parent, directory=True)
        BASE.mkdir(mode=0o700)
    protected(BASE, directory=True)
    lock_path = BASE / '.lock'
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        protected(lock_path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Другой загрузчик Misbot уже работает.') from None
        with tempfile.TemporaryDirectory(prefix='.download-', dir=BASE) as temporary:
            folder = Path(temporary)
            tag = select_release(args.version, folder)
            print('Подготавливаем Misbot ' + tag.removeprefix('v') + '…', flush=True)
            url = REPOSITORY + '/releases/download/' + tag + '/'
            archive = folder / ARCHIVE
            checksum = folder / (ARCHIVE + '.sha256')
            download(url + ARCHIVE, archive)
            download(url + ARCHIVE + '.sha256', checksum)
            files = archive_files(archive, checksum, tag)
            source = retain_bundle(files, folder, tag.removeprefix('v'))
        # Downloads are gone before the interactive UI starts; sources remain for recovery.
        print('Проверенный комплект: ' + str(source), flush=True)
        return subprocess.run(
            ['/usr/bin/python3', '-E', '-s', '-B', str(source / 'misbot.py'), 'ui'],
            cwd=source,
            check=False,
        ).returncode


def terminate(signum, _frame):
    # Raising unwinds TemporaryDirectory and stops any active subprocess.
    raise SystemExit(128 + signum)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nЗагрузка прервана. Повторите ту же команду.', file=sys.stderr)
        raise SystemExit(130)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as error:
        print(
            'Misbot: подготовка не завершена. '
            + (
                str(error)
                if isinstance(error, RuntimeError)
                else 'Проверьте сеть, свободное место и права доступа; повторите команду.'
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
