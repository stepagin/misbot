#!/usr/bin/env python3
"""Build a deterministic source-only archive, using an explicit allowlist."""

import argparse
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, default=root.parent / 'dist/matrix-installer.tar.gz')
parser.add_argument(
    '--check', action='store_true', help='verify that SHA256SUMS matches the current source files'
)
parser.add_argument(
    '--release',
    action='store_true',
    help='build the specified stable version without a local counter',
)
args = parser.parse_args()


@contextmanager
def build_version(version):
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        parser.error('release.json must contain the owner-specified major.minor.patch version')
    if args.release or any(
        os.environ.get(name, '').lower() not in ('', '0', 'false')
        for name in ('CI', 'GITHUB_ACTIONS')
    ):
        yield version
        return
    state_dir = root.parent / 'dist'
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / '.local-build.lock'
    try:
        lock_file = lock.open('x')
    except FileExistsError:
        parser.error('Local build is locked; check running builds before removing ' + str(lock))
    try:
        with lock_file:
            lock_file.write(str(os.getpid()) + '\n')
        state_path = state_dir / '.local-build.json'
        counters = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not isinstance(counters, dict) or any(
            type(number) is not int or number < 0 for number in counters.values()
        ):
            parser.error('Invalid local build counter: ' + str(state_path))
        previous = counters.get(version, 0)
        # Preserve numbering when adopting archives built before the local counter existed.
        for path in state_dir.glob('misbot-setup-' + version + '-build.*.tar.gz'):
            match = re.fullmatch(
                re.escape('misbot-setup-' + version) + r'-build\.([0-9]+)\.tar\.gz', path.name
            )
            if match:
                previous = max(previous, int(match[1]))
        counters[version] = previous + 1
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(counters, indent=2) + '\n')
        temporary.replace(state_path)
        yield version + '-build.' + str(counters[version])
    finally:
        lock.unlink()


def manifest_for(files):
    return ''.join(
        hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in files.items()
    ).encode()


names = [
    'LICENSE',
    'third-party-licenses.md',
    'data_migrations.py',
    'data-migrations.md',
    'release-notes.md',
    'admin_panel.py',
    'server_management.py',
    'welcome_message.py',
    'welcome_settings.py',
    'releases.py',
    'release_update.py',
    'remove_server.py',
    'admin_identity.py',
    'admin-panel.md',
    'existing_compose.py',
    'existing_s3.py',
    'existing_update.py',
    'existing_features.py',
    'existing_schedule.py',
    'misbot_s3_provider.py',
    'existing-compose.md',
    'telegram_backup.py',
    'existing_proxy.py',
    'telegram_container.py',
    'homepage.py',
    'homepage.md',
    'site/index.html',
    'site/site.css',
    'configuration-examples/site.env.example',
    'federation.py',
    'federation_runtime.py',
    'federation_protocol.py',
    'federation_store.py',
    'federation_service.py',
    'federation_admin_api.py',
    'federation_matrix_api.py',
    'federation.md',
    'misbot-api.openapi.json',
    'configuration-examples/server.env.example',
    'configuration-examples/policy.env.example',
    'configuration-examples/media-s3.env.example',
    'configuration-examples/backup.env.example',
    'configuration-examples/auth.env.example',
    'configuration-examples/database.env.example',
    'configuration.py',
    'configuration_schema.py',
    'terminal_ui.py',
    'terminal_input.py',
    'terminal_views.py',
    'terminal_diagnostics.py',
    'terminal_process.py',
    'terminal_output.py',
    'startup_process.py',
    'telegram_login_check.py',
    'wizard_state.py',
    'wizard_dns.py',
    'ui_actions.py',
    'registration.py',
    'registration_api.py',
    'configuration.md',
    'misbot.py',
    'launch.py',
    'cli_commands.py',
    'command_wrapper.py',
    's3_config.py',
    's3_diagnostics.py',
    'cli.md',
    'install.sh',
    'generate.py',
    'lifecycle.py',
    'operation_models.py',
    'database.py',
    'postgres_setup.py',
    'pg_credentials.py',
    's3_credentials.py',
    'vault_cache.py',
    'mas_admin.py',
    'credentials.py',
    'user_credentials.py',
    'user_password_api.py',
    'preflight.py',
    'state_store.py',
    'backup_archive.py',
    'backup_writers.py',
    'backup_crypto.py',
    'external_backup.py',
    'backup_schedule.py',
    'media_snapshot.py',
    'restore.py',
    'media.py',
    'media_transfer.py',
    'verify.py',
    'versions.json',
    'README.md',
    'lifecycle.md',
    'architecture.md',
    'storage.md',
    'vault.md',
    'credentials.md',
    'backup.md',
    'administration.md',
    'users.md',
    'update.py',
    'release.json',
    'synapse-s3/Dockerfile',
    'referral.py',
    'referral-control/Dockerfile',
    'referral-control/requirements.txt',
    'referral-control/provision.py',
]
sources = {name: root / name for name in names}
sources['LICENSE'] = root.parent / 'LICENSE'
# Only named public source files enter the archive. The installed layout still
# has referral-bot/ and referral-control/, without duplicate files in Git.
source_names = {
    'referral-bot': [
        'Dockerfile',
        'package.json',
        'package-lock.json',
        'main.js',
        'logic.js',
        'signing.js',
    ],
    'referral-control': ['bridge.py', 'common.py', 'mas.py', 'control.py'],
    'telegram-bot': ['Dockerfile', 'requirements.txt', 'worker.py', 'common.py', 'README.md'],
}
for destination, files in source_names.items():
    source = (
        root.parent / 'bots' / ('matrix-referral' if destination == 'referral-bot' else 'telegram')
    )
    for name in files:
        target = destination + '/' + name
        names.append(target)
        sources[target] = source / name
for name in (
    'Dockerfile',
    'main.js',
    'logic.js',
    'welcome.js',
    'runtime.js',
    'package.json',
    'package-lock.json',
    'signing.js',
):
    target = 'federation-bot/' + name
    names.append(target)
    sources[target] = (
        root.parent
        / 'bots'
        / (
            'matrix-federation'
            if name in ('Dockerfile', 'main.js', 'logic.js', 'welcome.js', 'runtime.js')
            else 'matrix-referral'
        )
        / name
    )
payloads = {name: sources[name].read_bytes() for name in names}
manifest = manifest_for(payloads)
if args.check:
    if not (root / 'SHA256SUMS').is_file() or (root / 'SHA256SUMS').read_bytes() != manifest:
        parser.exit(1, 'SHA256SUMS is stale; run python3 installer/build.py\n')
    print('SHA256SUMS matches the current source files')
    raise SystemExit(0)
(root / 'SHA256SUMS').write_bytes(manifest)


def write_archive(path, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for name, data in files.items():
                    member = tarfile.TarInfo('matrix-installer/' + name)
                    member.size = len(data)
                    member.mode = 0o755 if Path(name).name == 'install.sh' else 0o644
                    member.mtime = 0
                    archive.addfile(member, io.BytesIO(data))
    print(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + '.sha256').write_text(
        digest + '  ' + path.name + '\n', encoding='ascii'
    )
    print('SHA256 ' + digest)


metadata = json.loads(payloads['release.json'])
with build_version(metadata['version']) as product_version:
    if metadata['version'] != product_version:
        metadata['version'] = product_version
        payloads['release.json'] = (
            json.dumps(metadata, ensure_ascii=False, indent=2) + '\n'
        ).encode()
    payloads['SHA256SUMS'] = manifest_for(payloads)
    # Preserve the established release asset/layout for existing update clients.
    write_archive(args.output, payloads)
    compact = {'.misbot/' + name: data for name, data in payloads.items()}
    compact['misbot.py'] = (root / 'launch.py').read_bytes()
    compact['README.md'] = (
        '# Misbot ' + product_version + '\n\n'
        'Запуск: `sudo python3 misbot.py ui`. Служебные исходники и документация '
        'находятся в `.misbot/`. Установка по умолчанию: `/opt/matrix-stack`.\n\n'
        'Черновик хранится приватно в `~/.local/state/misbot/wizard/`, отдельно '
        'от комплекта и данных сервера. Команды просмотра и сброса: '
        '`sudo python3 misbot.py config draft show` и '
        '`sudo python3 misbot.py config draft reset --confirm discard-draft`. '
        'Параметр `--directory /абсолютный/путь` выбирает другой черновик.\n\n'
        'Проверка комплекта: `sha256sum -c SHA256SUMS`. '
        'Лицензия Misbot MIT: `.misbot/LICENSE`; '
        'сторонние уведомления: `.misbot/third-party-licenses.md`.\n\n'
        'Обновление существующего сервера: `sudo python3 misbot.py update`. '
        'Комплект и каталог сервера выбираются автоматически; если каталог не найден, '
        'команда запросит путь в терминале или предложит `--directory /абсолютный/путь`. '
        'Справка без изменений: `python3 misbot.py update --help`. '
        'Ключи и данные находятся в каталоге установки.\n'
    ).encode()
    compact['SHA256SUMS'] = ''.join(
        hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in compact.items()
    ).encode()
    write_archive(args.output.with_name('misbot-setup-' + product_version + '.tar.gz'), compact)
