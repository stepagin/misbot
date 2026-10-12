#!/usr/bin/env python3
"""Build a deterministic source-only archive, using an explicit allowlist."""

import argparse
import gzip
import hashlib
import io
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, default=root.parent / 'dist/matrix-installer.tar.gz')
parser.add_argument(
    '--check', action='store_true', help='verify that SHA256SUMS matches the current source files'
)
args = parser.parse_args()
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
    'terminal_views.py',
    'terminal_diagnostics.py',
    'terminal_process.py',
    'wizard_state.py',
    'wizard_dns.py',
    'ui_actions.py',
    'registration.py',
    'registration_api.py',
    'configuration.md',
    'misbot.py',
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
manifest = ''.join(
    hashlib.sha256(payloads[name]).hexdigest() + '  ' + name + '\n' for name in names
).encode()
if args.check:
    if not (root / 'SHA256SUMS').is_file() or (root / 'SHA256SUMS').read_bytes() != manifest:
        parser.exit(1, 'SHA256SUMS is stale; run python3 installer/build.py\n')
    print('SHA256SUMS matches the current source files')
    raise SystemExit(0)
(root / 'SHA256SUMS').write_bytes(manifest)
payloads['SHA256SUMS'] = manifest
args.output.parent.mkdir(parents=True, exist_ok=True)
with args.output.open('wb') as output:
    with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for name, data in payloads.items():
                member = tarfile.TarInfo('matrix-installer/' + name)
                member.size = len(data)
                member.mode = 0o755 if name == 'install.sh' else 0o644
                member.mtime = 0
                archive.addfile(member, io.BytesIO(data))
print(args.output)
digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
args.output.with_name(args.output.name + '.sha256').write_text(
    digest + '  ' + args.output.name + '\n', encoding='ascii'
)
print('SHA256 ' + digest)
