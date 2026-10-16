#!/usr/bin/env python3
"""Installation lifecycle. Secrets are never written to command output or logs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import shlex
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from contextlib import contextmanager, nullcontext
import referral
import preflight
import configuration
import configuration_schema
import state_store
import startup_process
import backup_archive
import backup_writers
import telegram_runtime
from operation_models import OperationError
import media_transfer
import media_snapshot
import vault_cache


def emit(check, **values):
    line = json.dumps({'check': check, **values})
    print(line, flush=True)
    with log.open('a') as handle:
        handle.write(line + '\n')


GENERATION_ERRORS = {
    'public_identity_changed': 'Public service addresses are immutable after first startup. Restore the original PUBLIC_DOMAIN, WEB_DOMAIN, SFU_DOMAIN and MAS_DOMAIN before retrying generate/up; existing identities and keys were not changed',
    'mas_identity_changed': 'MAS issuer is immutable after database initialization. Restore the original MAS_DOMAIN and the saved MAS configuration, then retry generate; existing identities and keys were not changed',
    'oauth_google_pair_incomplete': 'GOOGLE requires both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET. Edit {SETTINGS} privately; fill both or clear both to disable Google. Then run: {GENERATE}. Existing data and keys were not changed',
    'oauth_telegram_pair_incomplete': 'TELEGRAM requires both TELEGRAM_CLIENT_ID and TELEGRAM_CLIENT_SECRET. Edit {SETTINGS} privately; fill both or clear both to disable Telegram. Then run: {GENERATE}. Existing data and keys were not changed',
    'postgres_identity_mismatch': 'External PostgreSQL identity checkpoint does not match this server; restore the original checkpoint from backup',
    'postgres_identity_missing': 'Cannot confirm the installed external PostgreSQL destination; restore its checkpoint/config from backup before retrying generate/up',
    'postgres_destination_changed': 'Changing installed external PostgreSQL host or port requires a separate data migration. Restore the original POSTGRES_HOST/POSTGRES_PORT from your verified backup, then retry install.sh up; keep both datasets and the ownership checkpoint',
}


def run(command, stdin=None, allowed=(0,), configuration_errors=None):
    # Compose exec can otherwise consume the remaining commands of `ssh ... bash -s`.
    if startup_process.ACTIVE.get() or startup_process.DEADLINE.get() is not None:
        result = startup_process.run(command, stdin=stdin, emit=emit)
    else:
        result = subprocess.run(
            command,
            input='' if stdin is None else stdin,
            capture_output=True,
            text=True,
            **state_store.child_process_options(),
        )
    if result.returncode not in allowed:
        if (
            Path(command[0]).name == 'docker'
            and 'Cannot connect to the Docker daemon' in result.stderr + result.stdout
        ):
            root = Path(globals().get('ROOT', '/opt/matrix-stack'))
            raise RuntimeError(preflight.docker_unavailable_message(root))
        if configuration_errors:
            for line in result.stdout.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(record, dict)
                    and record.get('check') == 'configuration_input'
                    and isinstance(record.get('code'), str)
                    and record['code'] in configuration_errors
                ):
                    # Translate only known codes, never replay child messages/stderr.
                    root = Path(globals().get('ROOT', '/opt/matrix-stack'))
                    message = (
                        configuration_errors[record['code']]
                        .replace('{SETTINGS}', str(preflight.settings_path(root, 'auth')))
                        .replace(
                            '{GENERATE}',
                            'sudo bash ' + shlex.quote(str(root / 'install.sh')) + ' generate',
                        )
                    )
                    raise RuntimeError(message)
        # CLI stderr can contain interpolated credentials. Never dump it automatically.
        raise RuntimeError(
            'Command failed: '
            + Path(command[0]).name
            + '; inspect the relevant service logs privately'
        )
    return result.stdout


def state():
    return json.loads((ROOT / 'installation.json').read_text())


def dc(*parts, **kwargs):
    return run(COMPOSE + list(parts), **kwargs)


def external_database():
    return state().get('database_mode') == 'external' or (
        not state().get('storage_committed')
        and preflight.settings(ROOT).get('POSTGRES_MODE', 'local') == 'external'
    )


def external_mas():
    try:
        metadata = state()
    except FileNotFoundError:
        return False
    if metadata.get('databases_initialized') or metadata.get('storage_committed'):
        return metadata.get('mas_mode', 'local') == 'external'
    if (ROOT / 'settings').exists() or (ROOT / 'settings.env').exists():
        return preflight.settings(ROOT).get('MAS_MODE', 'local') == 'external'
    return metadata.get('mas_mode', 'local') == 'external'


def owned_databases(metadata=None):
    return __import__('configuration_schema').owned_databases(metadata or state())


def expected_services(metadata=None):
    metadata = metadata or state()
    names = {
        'synapse',
        'jwt',
        'livekit',
        'web',
        'admin',
        'caddy',
        'tls-router',
        'referral-control',
        'referral-bot',
    }
    if metadata.get('mas_mode', 'local') == 'local':
        names.add('mas')
    if metadata.get('database_mode', 'local') == 'local':
        names.add('postgres')
    if metadata.get('federation_seeded'):
        names.add('federation-bot')
    if telegram_runtime.enabled(metadata):
        names.add('telegram-worker')
    return names


def external_mas_probe(values):
    import configuration_schema

    payload = {
        'action': 'probe',
        'client': {
            'mas_url': configuration_schema.mas_api_base(values),
            'client_id': values['MAS_CLIENT_ID'],
            'client_secret': values['MAS_CLIENT_SECRET'],
        },
    }
    try:
        result = json.loads(run(['python3', str(ROOT / 'mas_admin.py')], stdin=json.dumps(payload)))
        if result != {'passed': True}:
            raise ValueError()
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError(
            'Подключение к внешнему MAS не подтверждено. Проверьте HTTPS-домен, '
            'Client ID/secret и права управляющего приложения; данные сервера не изменены.'
        ) from None
    emit('external_mas_connection', passed=True, health_endpoint_required=False)


def verify_external_mas_state():
    import configuration

    metadata = state()
    initial_admin = __import__('service_identity').administrator(ROOT, metadata['domain'])
    if initial_admin.mas_user_id is not None and initial_admin.mas_user_id != metadata.get(
        'external_mas_admin_id'
    ):
        raise RuntimeError('Saved external MAS administrator identity changed')
    users = [
        {
            'username': initial_admin.username,
            'user_id': metadata.get('external_mas_admin_id'),
            'admin': True,
        }
    ]
    if not users[0]['user_id']:
        raise RuntimeError(
            'Сохранённая identity администратора внешнего MAS отсутствует; запуск заблокирован.'
        )
    for role in ('referral', 'federation', 'welcome'):
        if not metadata.get(role + '_provisioned'):
            continue
        session = configuration.read_private_json(ROOT / 'secrets' / (role + '-session.json'))
        if (
            not session['user_id'].startswith('@')
            or session['user_id'].split(':', 1)[-1] != metadata['domain']
        ):
            raise RuntimeError(
                'Saved external MAS bot belongs to another homeserver; activation blocked'
            )
        users.append(
            {
                'username': session['user_id'].split(':', 1)[0].removeprefix('@'),
                'user_id': session['mas_user_id'],
                'admin': False,
                'session_id': session['personal_session_id'],
                'scope': session['scope'],
            }
        )
    client = configuration.read_private_json(ROOT / 'secrets/referral-client.json')
    try:
        result = json.loads(
            run(
                ['python3', str(ROOT / 'mas_admin.py')],
                stdin=json.dumps({'action': 'verify-state', 'client': client, 'users': users}),
            )
        )
        if result != {'passed': True, 'accounts_and_sessions': len(users)}:
            raise ValueError()
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError(
            'Сохранённые пользователи и устройства внешнего MAS не подтверждены. '
            'Восстановите его прежнее состояние отдельно; новые аккаунты и ключи не создавались.'
        ) from None
    emit('external_mas_saved_identities', passed=True, snapshot_verified=False)


def verify_external_bot_tokens():
    import configuration
    import federation_runtime
    import stat

    metadata = state()
    tokens = []
    journals = {
        'referral': 'session.json',
        'federation': 'federation-session.json',
        'welcome': 'welcome-session.json',
    }
    for role, journal_name in journals.items():
        if not metadata.get(role + '_provisioned'):
            continue
        runtime_path = ROOT / 'config' / (role + '-bot.json')
        journal_path = ROOT / 'data/referral-control' / journal_name
        session_path = ROOT / 'secrets' / (role + '-session.json')
        if any(
            path.resolve() != path.absolute() for path in (runtime_path, journal_path, session_path)
        ):
            raise RuntimeError('Saved external MAS bot files cannot contain symbolic links')
        info = runtime_path.lstat()
        owner = 10002 if role == 'referral' else 10003
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in (os.geteuid(), owner)
            or info.st_mode & 0o077
        ):
            raise RuntimeError(
                'Saved bot configuration must retain its private managed owner and permissions'
            )
        # Controller journals belong to the operator; runtime configs belong to
        # the bot's container UID and cannot use the operator-only private loader.
        config = configuration.read_private_json(journal_path)['config']
        session = configuration.read_private_json(session_path)
        if any(config.get(key) != session.get(key) for key in ('user_id', 'device_id')):
            raise RuntimeError('Saved external MAS bot identity differs; restore its checkpoint')
        tokens.append({key: config[key] for key in ('access_token', 'user_id', 'device_id')})
    if not tokens:
        return
    referral.validate_saved(ROOT, metadata)
    federation_runtime.validate(ROOT, metadata)
    script = """import json, sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
for saved in json.load(sys.stdin):
    req = urllib.request.Request('http://synapse:8008/_matrix/client/v3/account/whoami',
        headers={'Authorization': 'Bearer ' + saved['access_token']})
    with opener.open(req, timeout=20) as response:
        actual = json.loads(response.read(1048577))
    if actual.get('user_id') != saved['user_id'] or actual.get('device_id') != saved['device_id']:
        raise RuntimeError('Saved external MAS bot identity differs')
print('{"passed": true}')
"""
    try:
        result = json.loads(
            dc('exec', '-T', 'synapse', 'python', '-c', script, stdin=json.dumps(tokens))
        )
        if result != {'passed': True}:
            raise ValueError()
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError(
            'Synapse не подтвердил сохранённые устройства внешнего MAS; не заменяйте аккаунты или ключи.'
        ) from None
    emit('external_mas_bot_tokens', passed=True)


def postgres_command(tool, *arguments):
    if external_database():
        return COMPOSE + ['run', '--rm', '--no-deps', '-T', 'postgres-client', tool, *arguments]
    return COMPOSE + ['exec', '-T', 'postgres', tool, '-U', 'postgres', *arguments]


def postgres(tool, *arguments, stdin=None):
    return run(postgres_command(tool, *arguments), stdin=stdin)


def ownership_check():
    identifiers = run(
        ['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=matrix-stack']
    ).split()
    if identifiers:
        # Docker selects only ownership metadata; do not fetch Config.Env.
        selected = run(
            [
                'docker',
                'inspect',
                '--format',
                '{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}',
                *identifiers,
            ]
        )
        for line in selected.splitlines():
            if json.loads(line) != str(ROOT):
                raise RuntimeError(
                    'Another directory owns the matrix-stack Compose project; refusing to modify it'
                )


def down():
    """Release the owned Compose slot while preserving bind data and journals."""
    ownership_check()
    networks = run(
        [
            'docker',
            'network',
            'ls',
            '--no-trunc',
            '--filter',
            'name=^matrix-stack_default$',
            '--format',
            '{{.ID}}',
        ]
    ).split()
    if len(networks) > 1:
        raise RuntimeError(
            'Cannot identify the owned network; keep it and inspect Docker metadata privately'
        )
    for network in networks:
        projection = '[{{json (index .Labels "com.docker.compose.project")}},{{json (index .Labels "com.docker.compose.network")}},{{json (index .Labels "org.misbot.matrix.installation.directory")}}]'
        labels = json.loads(run(['docker', 'network', 'inspect', '--format', projection, network]))
        if (
            not isinstance(labels, list)
            or len(labels) != 3
            or labels[:2] != ['matrix-stack', 'default']
            or labels[2] not in (str(ROOT), None, '')
        ):
            raise RuntimeError(
                'Another installation owns matrix-stack_default; no containers removed. Keep data and inspect install.sh status'
            )
        endpoints = run(
            [
                'docker',
                'network',
                'inspect',
                '--format',
                '{{range $id,$endpoint := .Containers}}{{$id}} {{end}}',
                network,
            ]
        ).split()
        if endpoints:
            projection = '[{{json (index .Config.Labels "com.docker.compose.project")}},{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}]'
            observed = run(['docker', 'inspect', '--format', projection, *endpoints]).splitlines()
            if len(observed) != len(endpoints) or any(
                json.loads(row) != ['matrix-stack', str(ROOT)] for row in observed
            ):
                raise RuntimeError(
                    'The network has foreign containers; no containers removed. Ask their operator to disconnect them, then retry install.sh down'
                )
        elif labels[2] != str(ROOT):
            raise RuntimeError(
                'Empty legacy network has no ownership evidence; no resources removed'
            )
    if state().get('federation_seeded'):
        __import__('federation_runtime').controller(ROOT, run, False, remove=True)
    if telegram_runtime.enabled(state()):
        telegram_runtime.controller(ROOT, run, False, remove=True)
    dc('down')  # No volumes, orphan removal, images, files or checkpoint deletion.
    emit(
        'installation_down',
        passed=True,
        data_retained=True,
        checkpoints_retained=True,
        next_action='Use install.sh status; to resume use install.sh up in this same directory. If status names a pending operation, follow its recovery command first',
    )


def image(role):
    return json.loads((ROOT / 'images.lock.json').read_text())['digests'][role]


def wait(check, attempts=60, service='service'):
    emit('waiting_for_service', service=service, timeout_seconds=attempts * 2)
    expires = time.monotonic() + attempts * 2
    for _ in range(attempts):
        remaining = expires - time.monotonic()
        if remaining <= 0:
            break
        try:
            with startup_process.deadline(remaining):
                if check():
                    return
        except state_store.OperationInterrupted:
            raise
        except RuntimeError:
            # Failed readiness probes are expected during service boot. Programming
            # errors must propagate immediately instead of becoming apparent hangs.
            pass
        remaining = expires - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))
    diagnostic = (
        'sudo docker compose --project-directory '
        + str(ROOT)
        + ' -f '
        + str(ROOT / 'compose.yaml')
        + ' logs --tail=100 '
        + service
    )
    raise RuntimeError(
        service
        + ' did not become ready within '
        + str(attempts * 2)
        + ' seconds. Inspect logs privately: '
        + diagnostic
        + '. Correct the cause, then retry: sudo bash '
        + str(ROOT / 'install.sh')
        + ' up. '
        + 'Keep existing data and secrets; do not run init in a new directory.'
    )


def dns_check():
    metadata = state()
    referral.validate_saved(ROOT, metadata)
    # Loading configuration can fail because it is busy or invalid, before DNS is tried.
    values = preflight.settings(ROOT)
    mas_mode = values.get('MAS_MODE', 'local')
    mas_domain = values.get('MAS_DOMAIN', '')
    try:
        preflight.dns_check(
            metadata['domain'],
            metadata['public_ip'],
            mas_domain=mas_domain,
            mas_mode=mas_mode,
            mas_public_ip=values.get('MAS_PUBLIC_IP', ''),
            model=values,
        )
    except (RuntimeError, OSError):
        emit('dns', passed=False, failure_code='dns_validation')
        raise
    hosts = configuration_schema.public_hosts(values)
    if mas_mode == 'external':
        hosts.append(mas_domain)
    emit('dns', hosts=len(set(hosts)), passed=True)


def upgrade_legacy_network():
    """Recreate only an attested legacy Compose network; retain all bind data."""
    networks = run(
        [
            'docker',
            'network',
            'ls',
            '--no-trunc',
            '--filter',
            'name=^matrix-stack_default$',
            '--format',
            '{{.ID}}',
        ]
    ).split()
    if not networks:
        return
    if len(networks) != 1:
        raise RuntimeError(
            'Cannot identify the installer network safely; keep it and inspect Docker network metadata privately'
        )
    network = networks[0]
    projection = '[{{json (index .Labels "com.docker.compose.project")}},{{json (index .Labels "com.docker.compose.network")}},{{json (index .Labels "org.misbot.matrix.installation.directory")}}]'
    labels = json.loads(run(['docker', 'network', 'inspect', '--format', projection, network]))
    if (
        not isinstance(labels, list)
        or len(labels) != 3
        or labels[:2] != ['matrix-stack', 'default']
    ):
        raise RuntimeError(
            'Another resource owns matrix-stack_default; keep its containers/network and resolve the conflict before retrying install.sh up'
        )
    if labels[2] == str(ROOT):
        return
    # Docker's Go template indexes an absent string-valued label as "".
    # Accept both representations; endpoint ownership remains mandatory.
    if labels[2] not in (None, ''):
        raise RuntimeError(
            'Another installation owns this Docker network; keep it and use install.sh status before resolving the conflict'
        )
    endpoints = run(
        [
            'docker',
            'network',
            'inspect',
            '--format',
            '{{range $id,$endpoint := .Containers}}{{$id}} {{end}}',
            network,
        ]
    ).split()
    if not endpoints:
        raise RuntimeError(
            'Legacy Docker network has no container ownership evidence. Keep it and inspect its metadata privately before retrying up; do not remove another installation network'
        )
    if any(not re.fullmatch(r'[a-f0-9]{64}', value) for value in endpoints):
        raise RuntimeError('Invalid legacy network endpoint metadata; no containers stopped')
    projection = '[{{json (index .Config.Labels "com.docker.compose.project")}},{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}]'
    observed = run(['docker', 'inspect', '--format', projection, *endpoints]).splitlines()
    if len(observed) != len(endpoints) or any(
        json.loads(row) != ['matrix-stack', str(ROOT)] for row in observed
    ):
        raise RuntimeError(
            'Legacy network has an unmanaged or foreign container. Keep services/data/network; ask its operator to disconnect that container, then retry install.sh up'
        )
    ownership_check()
    emit(
        'legacy_network_upgrade',
        phase='stopping_owned_stack',
        data_retained=True,
        next_action='The old network metadata requires a temporary stop. Keep data/keys; if interrupted use update-recover for a pending update, otherwise retry install.sh up',
    )
    dc('down', '--remove-orphans')  # Attested owned project only; no --volumes.
    emit('legacy_network_upgrade', phase='old_network_removed', data_retained=True)


@startup_process.bounded
def generate():
    ownership_check()
    metadata = state()
    referral.validate_saved(ROOT, metadata)
    __import__('federation_runtime').validate(ROOT, metadata)
    telegram_runtime.validate(ROOT, metadata)
    if (
        metadata.get('synapse_signing_key_created')
        and not (ROOT / 'data/synapse' / (metadata['domain'] + '.signing.key')).is_file()
    ):
        raise RuntimeError('Synapse signing key is missing; restore the original key from backup')
    if (
        metadata.get('databases_initialized')
        and not external_database()
        and not list((ROOT / 'data/postgres').glob('**/PG_VERSION'))
    ):
        raise RuntimeError('PostgreSQL data is missing; restore the installation from backup')
    # Pull exact immutable manifests only when missing locally.
    lock = json.loads((ROOT / 'versions.json').read_text())
    for role, digest in lock['digests'].items():
        if role == 'mas' and external_mas():
            continue
        present = startup_process.run(['docker', 'image', 'inspect', digest], emit=emit)
        if present.returncode != 0:
            run(['docker', 'pull', digest])
        emit('image_ready', role=role, version=lock['tags'][role])
    run(
        ['python3', str(ROOT / 'generate.py'), '--directory', str(ROOT)],
        configuration_errors=GENERATION_ERRORS,
    )
    if yaml_media_s3():
        dockerfile = ROOT / 'synapse-s3/Dockerfile'
        if not dockerfile.read_text().splitlines()[0].endswith(lock['digests']['synapse']):
            raise RuntimeError('S3 Synapse base differs from the pinned image lock')
        tag = 'matrix-stack/synapse-s3:1.7.0'
        source_hash = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
        image_id = run(
            ['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], allowed=(0, 1)
        ).strip()
        image_lock = ROOT / 'synapse-s3-image.lock.json'
        saved = json.loads(image_lock.read_text()) if image_lock.exists() else {}
        if (
            not image_id
            or saved.get('source_sha256') != source_hash
            or saved.get('image_id') != image_id
        ):
            run(['docker', 'build', '-t', tag, str(dockerfile.parent)])
            image_id = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()
        image_lock.write_text(
            json.dumps({'image_id': image_id, 'source_sha256': source_hash}) + '\n'
        )
        import yaml

        compose = ROOT / 'compose.yaml'
        value = yaml.safe_load(compose.read_text())
        value['services']['synapse']['image'] = image_id
        compose.write_text(yaml.safe_dump(value, sort_keys=False))
    referral.prepare_images(ROOT, run, emit)
    __import__('federation_runtime').prepare_image(ROOT, run)
    telegram_runtime.prepare_image(ROOT, run)
    dc('config', '--quiet')
    isolated = ['docker', 'run', '--rm', '--network', 'none', '--memory', '144m', '--cpus', '0.5']

    def mount(source, target):
        return ['--mount', f'type=bind,source={ROOT / source},target={target},readonly']

    if not external_mas():
        run(
            isolated
            + mount('config/mas.yaml', '/config/mas.yaml')
            + mount('config/mas-signing.pem', '/config/mas-signing.pem')
            + [image('mas'), 'config', 'check', '--config', '/config/mas.yaml']
        )
    run(
        isolated
        + mount('config/Caddyfile', '/etc/caddy/Caddyfile')
        + mount('public', '/srv/discovery')
        + [
            image('caddy'),
            'caddy',
            'validate',
            '--config',
            '/etc/caddy/Caddyfile',
            '--adapter',
            'caddyfile',
        ]
    )
    run(
        isolated
        + mount('config/haproxy.cfg', '/usr/local/etc/haproxy/haproxy.cfg')
        + [image('tls-router'), 'haproxy', '-c', '-f', '/usr/local/etc/haproxy/haproxy.cfg']
    )
    emit('generated_configuration', passed=True, server_name=state()['domain'])


def yaml_media_s3():
    import yaml

    return bool(
        yaml.safe_load((ROOT / 'config/homeserver.yaml').read_text()).get('media_storage_providers')
    )


def prepare_host():
    memory = preflight.check_ram()
    if shutil.disk_usage(ROOT).free < 3 * 1024**3:
        raise RuntimeError('At least 3 GiB free after pulling images is required')
    swap = int(
        next(
            line.split()[1]
            for line in Path('/proc/meminfo').read_text().splitlines()
            if line.startswith('SwapTotal:')
        )
    )
    if memory < 2000000 and swap < 1000000:
        path = ROOT / 'swapfile'
        if not path.exists():
            run(['dd', 'if=/dev/zero', 'of=' + str(path), 'bs=1M', 'count=1024', 'status=none'])
            path.chmod(0o600)
            run(['mkswap', str(path)])
        active = run(['swapon', '--show', '--noheadings', '--output', 'NAME']).splitlines()
        if str(path) not in active:
            run(['swapon', str(path)])
        fstab = Path('/etc/fstab')
        if str(path) + ' ' not in fstab.read_text():
            with fstab.open('a') as handle:
                handle.write(str(path) + ' none swap sw 0 0\n')
        emit('swap_enabled', bytes=1024**3)
    Path('/etc/sysctl.d/90-matrix-stack-rtc.conf').write_text(
        'net.core.rmem_max=2500000\nnet.core.wmem_max=2500000\n'
    )
    run(['sysctl', '--load', '/etc/sysctl.d/90-matrix-stack-rtc.conf'])


def create_admin():
    credentials = json.loads((ROOT / 'secrets/admin-access.json').read_text())
    __import__('service_identity').administrator(
        ROOT, domain=state()['domain'] if credentials.get('matrix_id') is not None else None
    )
    if credentials.get('password_managed') is False:
        payload = {
            'action': 'inspect',
            'credentials': credentials,
            'client': json.loads((ROOT / 'secrets/referral-client.json').read_text()),
        }
        try:
            result = json.loads(
                dc(
                    'exec',
                    '-T',
                    'synapse',
                    'python',
                    '-c',
                    (ROOT / 'mas_admin.py').read_text(),
                    stdin=json.dumps(payload),
                )
            )
            if result.get('user_id') != credentials['mas_user_id']:
                raise ValueError()
        except (OSError, RuntimeError, ValueError, TypeError):
            raise RuntimeError(
                'Saved MAS administrator was not confirmed; preserve its account and credentials'
            ) from None
        emit(
            'administrator_ready',
            credential_file=str(ROOT / 'secrets/admin-access.json'),
            password_omitted=True,
            password_managed=False,
        )
        return
    if external_mas():
        metadata = state()
        expected = metadata.get('external_mas_admin_id')
        payload = {
            'action': 'inspect' if expected else 'bootstrap',
            'credentials': credentials,
            'client': json.loads((ROOT / 'secrets/referral-client.json').read_text()),
        }
        try:
            result = json.loads(
                dc(
                    'exec',
                    '-T',
                    'synapse',
                    'python',
                    '-c',
                    (ROOT / 'mas_admin.py').read_text(),
                    stdin=json.dumps(payload),
                )
            )
            if not isinstance(result.get('user_id'), str) or not result['user_id']:
                raise ValueError()
            if expected and result['user_id'] != expected:
                raise ValueError()
        except (OSError, RuntimeError, ValueError, TypeError):
            raise RuntimeError(
                'Администратор внешнего MAS не подтверждён. Сохраните его пользователей '
                'и ключи; проверьте подключение и повторите запуск.'
            ) from None
        if not expected and not result.get('password_managed'):
            credentials.pop('password', None)
            credentials['password_managed'] = False
            state_store.write_json(ROOT / 'secrets/admin-access.json', credentials)
        if credentials.get('mas_user_id') != result['user_id']:
            credentials['mas_user_id'] = result['user_id']
            state_store.write_json(ROOT / 'secrets/admin-access.json', credentials)
        metadata['external_mas_admin_id'] = result['user_id']
        state_store.write_json(ROOT / 'installation.json', metadata)
        if not expected and result.get('password_managed'):
            (ROOT / 'data/synapse/bootstrap-admin.json').unlink(missing_ok=True)
        emit(
            'administrator_ready',
            credential_file=str(ROOT / 'secrets/admin-access.json'),
            password_omitted=True,
            external=True,
            password_managed='password' in credentials,
        )
        return
    # Fixed localpart, independent of user input. Do not interpolate a password into SQL/shell.
    sql = "SELECT json_build_object('admin',can_request_admin,'deleted',deactivated_at IS NOT NULL,'locked',locked_at IS NOT NULL) FROM users WHERE username='matrix-admin';"
    rows = postgres(
        'psql', '-d', 'mas', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', sql
    ).strip()
    needs_setup = not rows
    if rows:
        account = json.loads(rows)
        if account['deleted'] or account['locked']:
            raise RuntimeError(
                'Bootstrap administrator exists but is inactive; refusing to overwrite it'
            )
        needs_setup = not account['admin']
    if needs_setup:
        payload = {
            'credentials': credentials,
            'client': json.loads((ROOT / 'secrets/referral-client.json').read_text()),
        }
        try:
            dc(
                'exec',
                '-T',
                'synapse',
                'python',
                '-c',
                (ROOT / 'mas_admin.py').read_text(),
                stdin=json.dumps(payload),
            )
        except RuntimeError:
            raise RuntimeError(
                'MAS administrator setup was not confirmed. Keep existing data and secrets. '
                'Check MAS connectivity and whether matrix-admin is an existing non-admin account. '
                'Then retry: sudo bash ' + str(ROOT / 'install.sh') + ' up'
            ) from None
    emit(
        'administrator_ready',
        credential_file=str(ROOT / 'secrets/admin-access.json'),
        password_omitted=True,
    )


def installation_endpoints():
    model = preflight.settings(ROOT)
    configuration.assert_auth_identity(ROOT, model, state())
    return configuration_schema.public_endpoints(model)


def certificate_paths(host=None):
    host = host or installation_endpoints()['turn']
    path = ROOT / 'data/caddy/caddy/certificates/acme-v02.api.letsencrypt.org-directory' / host
    return path / (host + '.crt'), path / (host + '.key')


def cert_renew():
    if not any(
        row.get('Service') == 'livekit' and row.get('State') == 'running' for row in service_rows()
    ):
        emit(
            'turn_certificate_reload_deferred',
            passed=True,
            service='livekit',
            next_action='LiveKit is stopped or absent; no service started. When ready, run: sudo bash '
            + str(ROOT / 'install.sh')
            + ' up, then check',
        )
        return
    import fcntl

    with (ROOT / 'data/turn-certificate.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        time.sleep(2)
        cert, key = certificate_paths()
        run(['openssl', 'x509', '-in', str(cert), '-checkend', '86400', '-noout'])
        public_cert = run(['openssl', 'x509', '-in', str(cert), '-pubkey', '-noout'])
        public_key = run(['openssl', 'pkey', '-in', str(key), '-pubout'])
        if public_cert != public_key:
            raise RuntimeError('TURN certificate and key mismatch; retry after both files renew')
        digest = hashlib.sha256(cert.read_bytes()).hexdigest()
        marker = ROOT / 'data/turn-certificate.sha256'
        if marker.exists() and marker.read_text().strip() == digest:
            return
        dc('restart', '-t', '10', 'livekit')
        marker.write_text(digest + '\n')
        emit('turn_certificate_reloaded', passed=True)


def certificate_watch():
    cert, key = certificate_paths()
    system = Path('/etc/systemd/system')
    (system / 'matrix-stack-turn-cert.service').write_text(f'''[Unit]
Description=Reload Matrix TURN certificate after Caddy renewal
After=docker.service
[Service]
Type=oneshot
Environment=DOCKER_HOST=unix:///var/run/docker.sock
ExecStart=/usr/bin/python3 "{ROOT}/lifecycle.py" --directory "{ROOT}" cert-renew
''')
    (system / 'matrix-stack-turn-cert.path').write_text(f"""[Unit]
Description=Watch renewed Matrix TURN certificate
[Path]
PathChanged={cert}
PathChanged={key}
Unit=matrix-stack-turn-cert.service
[Install]
WantedBy=multi-user.target
""")
    (system / 'matrix-stack-turn-cert.timer').write_text("""[Unit]
Description=Retry Matrix TURN certificate reload for paired-file updates
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Unit=matrix-stack-turn-cert.service
[Install]
WantedBy=timers.target
""")
    marker = ROOT / 'data/turn-certificate.sha256'
    marker.write_text(hashlib.sha256(cert.read_bytes()).hexdigest() + '\n')
    run(['systemctl', 'daemon-reload'])
    run(
        [
            'systemctl',
            'enable',
            '--now',
            'matrix-stack-turn-cert.path',
            'matrix-stack-turn-cert.timer',
        ]
    )
    emit('turn_certificate_watch', passed=True)


def up_admission_check():
    import cli_commands

    state_store.assert_database_allowed(ROOT)
    if state().get('credential_transition_pending'):
        raise RuntimeError(
            'Administrator password change was interrupted. Keep private checkpoints; run: '
            + cli_commands.recovery_command('admin-password-recover', ROOT)
            + ' (technical entry: sudo bash '
            + str(ROOT / 'install.sh')
            + ' admin-password-recover)'
        )
    if state().get('migration_frozen'):
        raise RuntimeError(
            'Source is frozen for migration; do not start a second writer. To abandon the move, stop the target and use '
            + cli_commands.recovery_command('move-abort', ROOT)
            + ' --confirm target-stopped (technical entry: install.sh move-abort target-stopped)'
        )
    if state().get('restore_pending'):
        raise RuntimeError(
            'Restored target is staged with writers disabled. Stop the source, switch DNS, then run '
            + cli_commands.recovery_command('restore-activate', ROOT)
            + ' --confirm source-stopped (technical entry: install.sh restore-activate source-stopped)'
        )
    if state().get('media_transition_pending'):
        raise RuntimeError(
            'Media transfer was interrupted; run '
            + cli_commands.recovery_command('media-recover', ROOT)
            + ' (technical entry: install.sh media-recover), then repeat storage s3 connect with the same private settings file'
        )


@startup_process.bounded
def up():
    up_admission_check()
    emit('startup_progress', completed=0, total=10, stage='admission')
    values = preflight.settings(ROOT)
    if values.get('MAS_MODE') == 'external':
        external_mas_probe(values)
    if values.get('MEDIA_MODE') == 's3':
        try:
            media_transfer.client(values).head_bucket(Bucket=values['S3_BUCKET'])
        except state_store.OperationInterrupted:
            raise
        except Exception:
            retry = (
                'sudo bash '
                + str(ROOT / 'install.sh')
                + (' database-recover' if (ROOT / 'database-transition.json').exists() else ' up')
            )
            next_action = (
                'Correct S3 credentials/connectivity; keep all data and configuration, then retry: '
                + retry
                + '.'
                if state().get('storage_committed')
                else 'To continue locally: sudo bash '
                + str(ROOT / 'install.sh')
                + ' media-local; then repeat up. '
                'Later attach S3 and copy media: sudo bash '
                + str(ROOT / 'install.sh')
                + ' media-s3 PRIVATE_S3_ENV.'
            )
            raise RuntimeError('S3 check failed before startup. ' + next_action) from None
        emit('s3_connection', passed=True)
    emit('startup_progress', completed=1, total=10, stage='images')
    generate()
    emit('startup_progress', completed=2, total=10, stage='host')
    dns_check()
    upgrade_legacy_network()
    if not state().get('databases_initialized'):
        prepare_host()
    # Refuse occupied ports for first start; reconfiguration uses the already owned stack.
    if not dc('ps', '-q', '--all').strip():
        for port in [80, 443, 7881]:
            with socket.socket() as sock:
                try:
                    sock.bind(('0.0.0.0', port))
                except OSError:
                    raise RuntimeError('Required TCP port occupied: ' + str(port))
        for port in [3478, 7882]:
            with socket.socket(type=socket.SOCK_DGRAM) as sock:
                try:
                    sock.bind(('0.0.0.0', port))
                except OSError:
                    raise RuntimeError('Required UDP port occupied: ' + str(port))
    emit('startup_progress', completed=3, total=10, stage='database')
    if external_database():
        if not state().get('databases_initialized'):
            import postgres_setup

            try:
                result = postgres_setup.initialize(ROOT, postgres)
            except state_store.OperationInterrupted:
                raise
            except Exception as error:
                if isinstance(error, RuntimeError) and 'Command failed:' not in str(error):
                    raise
                raise RuntimeError(
                    'External PostgreSQL setup was not confirmed. Keep existing roles/data and config/external-postgres-owner.json; check private credentials, TLS and connectivity, then retry install.sh up in the same directory'
                ) from None
            emit('external_postgres_initialized', passed=True, **result)
    else:
        dc('up', '-d', 'postgres')
    metadata = state()
    metadata['network_committed'] = True
    state_store.write_json(ROOT / 'installation.json', metadata)
    wait(
        lambda: postgres(
            'psql',
            '-d',
            'synapse' if external_mas() else 'mas',
            '-X',
            '-A',
            '-t',
            '-v',
            'ON_ERROR_STOP=1',
            '-c',
            'SELECT 1',
        ).strip()
        == '1',
        service='postgres-client' if external_database() else 'postgres',
    )
    metadata = state()
    metadata['databases_initialized'] = True
    metadata['database_mode'] = 'external' if external_database() else 'local'
    metadata['media_mode'] = 's3' if yaml_media_s3() else 'local'
    if metadata['media_mode'] == 's3':
        import yaml

        metadata['s3_prefix'] = yaml.safe_load((ROOT / 'config/homeserver.yaml').read_text())[
            'media_storage_providers'
        ][0]['config']['prefix']
        metadata['s3_identity'] = media_transfer.identity(preflight.settings(ROOT))
    metadata['storage_committed'] = True
    state_store.write_json(ROOT / 'installation.json', metadata)
    emit('startup_progress', completed=4, total=10, stage='applications')
    dc('up', '-d', *owned_databases())
    probe = "import urllib.request; urllib.request.urlopen('http://synapse:8008/_matrix/client/versions',timeout=3).read()"
    if not external_mas():
        probe += "; urllib.request.urlopen('http://mas:8080/health',timeout=3).read()"
    wait(
        lambda: dc('exec', '-T', 'synapse', 'python', '-c', probe) is not None,
        service=' '.join(owned_databases()),
    )
    if external_mas():
        # MAS must call the public homeserver while creating users/devices.
        # Publish HTTPS before provisioning; otherwise both sides wait on each other.
        dc('up', '-d', 'caddy', 'tls-router')
        cert, key = certificate_paths(installation_endpoints()['matrix'])
        wait(lambda: cert.is_file() and key.is_file(), attempts=150, service='caddy tls-router')
        run(['openssl', 'x509', '-in', str(cert), '-checkend', '86400', '-noout'])
    metadata = state()
    if not (ROOT / 'data/synapse' / (metadata['domain'] + '.signing.key')).is_file():
        raise RuntimeError('Synapse has not generated its signing key')
    metadata['synapse_signing_key_created'] = True
    state_store.write_json(ROOT / 'installation.json', metadata)
    emit('startup_progress', completed=5, total=10, stage='administrator')
    create_admin()
    if external_mas() and state().get('referral_provisioned'):
        verify_external_mas_state()
        verify_external_bot_tokens()
    emit('startup_progress', completed=6, total=10, stage='referral')
    dc('up', '-d', 'referral-control')
    wait(
        lambda: dc(
            'exec',
            '-T',
            'referral-control',
            'python',
            '-c',
            "from common import bridge_call; bridge_call({'action':'servers'},path='/config/bot-run/control.sock',timeout=2)",
        )
        is not None,
        service='referral-control',
    )
    metadata = state()
    if not metadata.get('referral_provisioned'):
        referral.provision(ROOT, dc, metadata)
        state_store.write_json(ROOT / 'installation.json', metadata)
    __import__('runtime_config').refresh(ROOT, ('referral-bot',))
    dc('up', '-d', 'referral-bot')
    wait(
        lambda: dc(
            'exec',
            '-T',
            'referral-bot',
            'node',
            '-e',
            "fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
        )
        is not None,
        service='referral-bot',
    )
    metadata = state()
    referral.note_ready(ROOT, metadata)
    state_store.write_json(ROOT / 'installation.json', metadata)
    emit(
        'referral_bot_ready',
        matrix_id='@referral-bot:' + metadata['domain'],
        encryption=True,
        cross_signed=True,
        admin_scopes=False,
    )
    emit('startup_progress', completed=7, total=10, stage='federation')
    import federation_runtime

    federation_runtime.provision(ROOT, dc, metadata)
    state_store.write_json(ROOT / 'installation.json', metadata)
    federation_runtime.controller(ROOT, run)
    __import__('runtime_config').refresh(ROOT, ('federation-bot',))
    dc('up', '-d', 'federation-bot')
    wait(
        lambda: dc(
            'exec',
            '-T',
            'federation-bot',
            'node',
            '-e',
            "fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
        )
        is not None,
        service='federation-bot',
    )
    metadata = state()
    metadata['federation_crypto_initialized'] = True
    metadata['welcome_crypto_initialized'] = True
    federation_runtime.validate(ROOT, metadata)
    state_store.write_json(ROOT / 'installation.json', metadata)
    emit('startup_progress', completed=8, total=10, stage='https')
    dc('up', '-d', 'jwt', 'web', 'admin', 'caddy', 'tls-router')
    cert, key = certificate_paths()
    wait(lambda: cert.is_file() and key.is_file(), attempts=150, service='caddy')
    run(['openssl', 'x509', '-in', str(cert), '-checkend', '86400', '-noout'])
    dc('up', '-d', 'livekit')
    if telegram_runtime.enabled(state()):
        telegram_runtime.controller(ROOT, run)
        wait(
            lambda: telegram_runtime.rpc(ROOT, {'action': 'servers'}) is not None,
            service='telegram-controller',
        )
        dc('up', '-d', 'telegram-worker')
    certificate_watch()
    wait(
        lambda: all(
            row['State'] == 'running' and row.get('Health', '') in ('', 'healthy')
            for row in service_rows()
        )
        and {row['Service'] for row in service_rows()} == expected_services(),
        service=' '.join(sorted(expected_services())),
    )
    emit('startup_progress', completed=9, total=10, stage='verification')
    check()
    release = json.loads((ROOT / 'release.json').read_text())
    metadata = state()
    metadata['applied_release_version'] = release['version']
    metadata['applied_release_revision'] = hashlib.sha256(
        (ROOT / 'SHA256SUMS').read_bytes()
    ).hexdigest()
    state_store.write_json(ROOT / 'installation.json', metadata)
    vault_cache.sync_optional(ROOT, emit)
    emit('startup_progress', completed=10, total=10, stage='complete')
    emit(
        'installation_running',
        admin='https://' + installation_endpoints()['admin'],
        server_name=state()['domain'],
    )


def service_rows():
    return [
        json.loads(line)
        for line in dc('ps', '--all', '--format', 'json').splitlines()
        if line.strip()
    ]


def stop():
    if state().get('federation_seeded'):
        __import__('federation_runtime').controller(ROOT, run, False)
    dc('stop')


def status():
    from server_management import host_statistics

    metadata = state()
    pending = {
        name: bool(metadata.get(name))
        for name in (
            'migration_frozen',
            'restore_pending',
            'credential_transition_pending',
            'media_transition_pending',
        )
    }
    for name, relative in (
        ('backup_recovery_pending', 'backup-writers.json'),
        ('database_recovery_pending', 'database-transition.json'),
        ('database_password_recovery_pending', 'database-password-transition.json'),
        ('s3_credential_recovery_pending', 's3-credentials-transition.json'),
        ('update_recovery_pending', 'update-journal.json'),
        ('configuration_recovery_pending', 'configuration-transition.json'),
        ('federation_recovery_pending', 'federation-transition.json'),
        ('homepage_recovery_pending', 'homepage-transition.json'),
        ('user_password_recovery_pending', 'secrets/user-password-pending.json'),
        ('admin_access_recovery_pending', 'secrets/admin-access-pending.json'),
    ):
        marker = ROOT / relative
        pending[name] = marker.exists() or marker.is_symlink()
    command = 'sudo bash ' + str(ROOT / 'install.sh') + ' '
    actions = []
    if pending['configuration_recovery_pending']:
        actions.append('sudo misbot --directory ' + shlex.quote(str(ROOT)) + ' config recover')
    if pending['federation_recovery_pending']:
        actions.append('sudo misbot --directory ' + shlex.quote(str(ROOT)) + ' federation recover')
    if pending['homepage_recovery_pending']:
        actions.append('sudo misbot --directory ' + shlex.quote(str(ROOT)) + ' homepage recover')
    for flag, operation in (
        ('backup_recovery_pending', 'backup-recover'),
        ('database_recovery_pending', 'database-recover'),
        ('database_password_recovery_pending', 'database-password-recover'),
        ('s3_credential_recovery_pending', 's3-credentials-recover'),
        ('credential_transition_pending', 'admin-password-recover'),
        ('user_password_recovery_pending', 'user-password-recover'),
        ('media_transition_pending', 'media-recover'),
    ):
        if pending[flag]:
            import cli_commands

            actions.extend([cli_commands.recovery_command(operation, ROOT), command + operation])
    if pending['admin_access_recovery_pending']:
        actions.append(
            'sudo misbot --directory ' + shlex.quote(str(ROOT)) + ' admin access recover'
        )
    if pending['update_recovery_pending']:
        import cli_commands

        actions.append(
            cli_commands.recovery_command('update-recover', ROOT)
            + ' --source /root/matrix-installer'
        )
        actions.append(
            'From a trusted extracted installer directory run: sudo bash install.sh update-recover '
            + str(ROOT)
        )
    if pending['migration_frozen']:
        import cli_commands

        actions.extend(
            [
                cli_commands.recovery_command('move-export', ROOT),
                'Only after target writers are stopped and DNS returned: '
                + cli_commands.recovery_command('move-abort', ROOT)
                + ' --confirm target-stopped',
            ]
        )
        actions.extend(
            [
                command + 'move-export',
                'To cancel only after target writers are stopped and DNS returned: '
                + command
                + 'move-abort target-stopped',
            ]
        )
    if pending['restore_pending']:
        import cli_commands

        actions.append(
            'After stopping original source and switching DNS: '
            + cli_commands.recovery_command('restore-activate', ROOT)
            + ' --confirm source-stopped'
        )
        actions.append(
            'After the original source is stopped and target DNS is ready: '
            + command
            + 'restore-activate source-stopped'
        )
    emit(
        'installation_status',
        resources=host_statistics(ROOT),
        applied_release=metadata.get('applied_release_version'),
        operation_running=state_store.operation_running(ROOT),
        installation_complete=bool(metadata.get('applied_release_version')),
        decommissioned=bool(metadata.get('decommissioned')),
        **pending,
        next_actions=actions,
        message='Status does not resume operations or start writers. Wait for any active installer operation before using recovery commands; do not delete checkpoints. Stopped containers are included below',
    )
    if os.environ.get('MISBOT_TERMINAL_UI') == '1':
        rows = service_rows()
        configured = json.loads(dc('config', '--format', 'json'))
        expected = {
            name for name, entry in configured['services'].items() if not entry.get('profiles')
        }
        rows.extend(
            {'Service': name, 'State': 'missing'}
            for name in sorted(expected - {row.get('Service') for row in rows})
        )
        for row in rows:
            emit(
                'service',
                name=row.get('Service'),
                state=row.get('State'),
                health=row.get('Health', ''),
                exit_code=row.get('ExitCode'),
            )
    else:
        print(dc('ps', '--all'))


def wait_writers(writers):
    def ready():
        rows = {row['Service']: row for row in service_rows()}
        return all(
            name in rows
            and rows[name]['State'] == 'running'
            and rows[name].get('Health', '') in ('', 'healthy')
            for name in writers
        )

    if writers:
        wait(ready, service=' '.join(writers))


_backup_writers = None


def writer_runtime():
    return backup_writers.WriterRuntime(ROOT, state, service_rows, dc, wait_writers, emit)


@contextmanager
def paused_writers(leave_stopped=False):
    """Compatibility boundary for callers that still configure lifecycle globally."""
    global _backup_writers
    if _backup_writers is not None:
        if _backup_writers.root != ROOT:
            raise RuntimeError('Nested backup belongs to another installation')
        with _backup_writers.pause(leave_stopped):
            yield
        return
    coordinator = backup_writers.BackupWriters(writer_runtime())
    _backup_writers = coordinator
    try:
        with coordinator.pause(leave_stopped):
            yield
    finally:
        _backup_writers = None


def backup_recover():
    try:
        result = backup_writers.BackupWriters(writer_runtime()).recover()
    except OperationError as error:
        # The lifecycle error adapter already exposes only deliberate RuntimeError
        # guidance. Preserve that public contract for existing CLI/UI callers.
        raise RuntimeError(error.result.message) from None
    emit('backup_recovery', **result.legacy_fields())


def check():
    dns_check()
    expires = time.monotonic() + 120
    while True:
        try:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Public checks timed out; retry after services finish starting')
            with startup_process.deadline(remaining):
                check_services(expires)
            break
        except ReadinessPending:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Public checks failed; see the check records above') from None
            emit('waiting_for_service', service='HTTPS', timeout_seconds=round(remaining))
            time.sleep(min(2, remaining))
    referral.validate_saved(ROOT, state())
    __import__('federation_runtime').validate(ROOT, state())
    telegram_runtime.check(ROOT, state())
    emit('public_checks', passed=True)


class ReadinessPending(RuntimeError):
    """Only classified boot-time availability failures may be polled again."""


def check_services(expires):
    if time.monotonic() >= expires:
        raise RuntimeError('Public checks timed out; retry after services finish starting')
    result = startup_process.run(
        ['python3', str(ROOT / 'verify.py'), '--directory', str(ROOT)],
        emit=emit,
        timeout=max(1, expires - time.monotonic()),
    )
    records = [json.loads(line) for line in result.stdout.splitlines()]
    for record in records:
        emit(
            record['check'],
            **{
                key: value
                for key, value in record.items()
                if key
                in ('status', 'expected', 'passed', 'error_type', 'failure_code', 'tls', 'failures')
            },
        )
    if time.monotonic() >= expires:
        raise RuntimeError('Public checks timed out; retry after services finish starting')
    rows = service_rows()
    if time.monotonic() >= expires:
        raise RuntimeError('Public checks timed out; retry after services finish starting')
    for row in rows:
        emit('service', name=row['Service'], state=row['State'], health=row.get('Health', ''))
    required = expected_services()
    services_ready = {row['Service'] for row in rows} == required and all(
        row['State'] == 'running' and row.get('Health', '') in ('', 'healthy') for row in rows
    )
    missing = sorted(required - {row['Service'] for row in rows})
    emit('service_inventory', passed=services_ready and not missing, missing=missing)
    failed = [record for record in records if record.get('passed') is False]
    transient_public = (
        not result.returncode
        or bool(failed)
        and all(
            record.get('failure_code') in ('connection_refused', 'timeout', 'tls_connection')
            or record.get('failure_code') == 'http_status'
            and record.get('status') in (502, 503, 504)
            for record in failed
        )
    )
    starting = {row['Service'] for row in rows} == required and all(
        row['State'] in ('running', 'restarting')
        and row.get('Health', '') in ('', 'starting', 'healthy')
        for row in rows
    )
    if (
        (result.returncode or not services_ready)
        and transient_public
        and starting
        and time.monotonic() < expires
    ):
        raise ReadinessPending()
    if result.returncode:
        raise RuntimeError('Public checks failed; see the check records above')
    if missing or not services_ready:
        raise RuntimeError('Not all expected containers are ready')


def database_inventory(database, application=None):
    application = application or database
    tables = {
        'synapse': ('users', 'rooms', 'events', 'local_media_repository', 'remote_media_cache'),
        'mas': ('users', 'user_registration_tokens'),
    }
    fields = []
    for table in tables[application]:
        fields.extend(["'" + table + "'", '(SELECT count(*) FROM ' + table + ')'])
    return json.loads(
        postgres(
            'psql',
            '-d',
            database,
            '-X',
            '-At',
            '-v',
            'ON_ERROR_STOP=1',
            '-c',
            'SELECT json_build_object(' + ','.join(fields) + ')',
        )
    )


def backup_dump_arguments(database):
    if database not in ('synapse', 'mas'):
        raise RuntimeError('Unexpected application database for backup')
    arguments = ['-Fc']
    if database == 'synapse':
        arguments.append('--exclude-table-data=public.e2e_one_time_keys_json')
    return arguments + [database]


def clear_restored_one_time_keys(database, sql=None):
    """Only callers holding a staged/import/temporary-restore guard may use this."""
    if database != 'synapse' and not re.fullmatch(r'restorecheck_synapse_[a-f0-9]{8}', database):
        raise RuntimeError('Unexpected Synapse restore database')
    query = (
        "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='15s'; "
        'TRUNCATE TABLE public.e2e_one_time_keys_json; '
        'SELECT count(*) FROM public.e2e_one_time_keys_json; COMMIT;'
    )
    try:
        result = (
            sql(database, query)
            if sql
            else postgres(
                'psql',
                '-d',
                database,
                '-X',
                '-qAt',
                '-v',
                'ON_ERROR_STOP=1',
                '-f',
                '-',
                stdin=query,
            )
        )
        if result.strip() != '0':
            raise RuntimeError('Restored one-time keys were not cleared')
    except (OSError, RuntimeError):
        raise RuntimeError(
            'Restored one-time-key check failed; writers remain disabled. '
            'Check PostgreSQL connectivity and locks, then repeat the original restore/restore-local '
            'or database-recover command; for a ready staged target repeat restore-activate. '
            'Keep the source and archive; do not start Synapse manually'
        ) from None
    emit('restored_one_time_keys', database=database, passed=True, remaining=0)


def backup(leave_stopped=False):
    if state().get('credential_transition_pending'):
        raise RuntimeError(
            'Recover the interrupted password change with install.sh admin-password-recover before making a full backup'
        )
    if state().get('media_transition_pending'):
        raise RuntimeError(
            'Recover the interrupted media transition with install.sh media-recover before making a full backup'
        )
    state_store.assert_database_password_allowed(ROOT)
    directory = ROOT / 'backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6]
    target = directory / stamp
    target.mkdir(mode=0o700)
    referral.validate_saved(ROOT, state())
    with paused_writers(leave_stopped):
        for database in owned_databases():
            with (target / (database + '.dump')).open('wb') as output:
                result = subprocess.run(
                    postgres_command('pg_dump', *backup_dump_arguments(database)),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    **state_store.child_process_options(),
                )
                if result.returncode:
                    raise RuntimeError('Database dump failed')
        (target / 'roles.sql').write_text(
            '-- Dedicated roles are recreated from config/init-db.sql.\n'
            if external_database() or external_mas()
            else postgres('pg_dumpall', '--globals-only')
        )
        with tarfile.open(target / 'files.tar.gz', 'w:gz') as tar:
            # Include the whole installed source release, not a stale hand-written subset.
            source_manifest = ROOT / 'SHA256SUMS'
            sources = {'SHA256SUMS'}
            for line in source_manifest.read_text().splitlines():
                name = line.split('  ', 1)[1]
                relative = Path(name)
                if relative.is_absolute() or '..' in relative.parts or (ROOT / name).is_symlink():
                    raise RuntimeError('Unsafe source path in backup manifest')
                if not (ROOT / name).is_file():
                    raise RuntimeError(
                        'Installed source file missing; restore the trusted installer release'
                    )
                sources.add(name)
            for name in sorted(sources):
                backup_archive.add_tree(tar, ROOT / name, name, recursive=False)
            for name in [
                'config',
                'public',
                'secrets',
                'settings',
                'settings.env',
                'installation.json',
                'compose.yaml',
                'images.lock.json',
                'referral-images.lock.json',
                'federation-images.lock.json',
                'telegram-images.lock.json',
                'data/federation',
                'data/federation-bot',
                'federation-transition.json',
                'stages/federation',
                'homepage-transition.json',
                'stages/homepage',
                'data/referral-bot',
                'data/referral-control',
                'data/admin-panel',
                'data/synapse',
                'data/caddy',
                'data/caddy-config',
                'mas-proxy.env',
                'synapse-s3-image.lock.json',
            ]:
                if (ROOT / name).exists() or (ROOT / name).is_symlink():
                    backup_archive.add_tree(tar, ROOT / name, name)
            telegram_runtime.add_backup(tar, ROOT, state(), run)
        (target / 'mode.txt').write_text(
            'consistent-synapse-only\n' if external_mas() else 'consistent\n'
        )
        metadata = state()
        inventory = {database: database_inventory(database) for database in owned_databases()}
        state_store.write_json(
            target / 'backup-format.json',
            {
                'format': 3 if external_mas() else 2,
                'family': 'misbot-matrix-portable',
                'mas_mode': metadata.get('mas_mode', 'local'),
                **(
                    {
                        'external_mas': {
                            'issuer': metadata['mas_issuer'],
                            'server_name': metadata['domain'],
                            'snapshot': 'not-included',
                            'admin_user_id': metadata.get('external_mas_admin_id'),
                        }
                    }
                    if external_mas()
                    else {}
                ),
                'server_name': metadata['domain'],
                'database_mode': metadata.get('database_mode', 'local'),
                'media_mode': metadata.get('media_mode', 'local'),
                'database_inventory': inventory,
                'synapse_one_time_keys': 'excluded',
            },
        )
        digests = {}
        for path in target.iterdir():
            if path.is_file():
                with path.open('rb') as source:
                    digests[path.name] = hashlib.file_digest(source, 'sha256').hexdigest()
        (target / 'SHA256.json').write_text(json.dumps(digests, indent=2) + '\n')
        archive = directory / ('matrix-' + stamp + '.tar.gz')
        temporary = archive.with_name(archive.name + '.partial')
        with tarfile.open(temporary, 'w:gz') as tar:
            tar.add(target, arcname=stamp)
        os.replace(temporary, archive)
        latest = directory / 'latest.tar.gz'
        link = directory / ('latest-' + uuid.uuid4().hex)
        link.symlink_to(archive.name)
        os.replace(link, latest)
        with archive.open('rb') as source:
            archive_digest = hashlib.file_digest(source, 'sha256').hexdigest()
        emit(
            'consistent_backup',
            archive=str(archive),
            sha256=archive_digest,
            **(
                {
                    'authentication': 'external-MAS-not-included',
                    'next_action': 'Сохраните отдельную копию внешнего MAS; эта копия содержит только Synapse и локальные файлы.',
                }
                if external_mas()
                else {}
            ),
        )
        shutil.rmtree(target)
        return archive


def move_export():
    metadata = state()
    if metadata.get('credential_transition_pending'):
        raise RuntimeError('Recover the interrupted password change before preparing a migration')
    if metadata.get('media_transition_pending'):
        raise RuntimeError('Recover the interrupted media transition before preparing a migration')
    if metadata.get('restore_pending'):
        raise RuntimeError('This machine is a staged restore target, not the active source')
    metadata['migration_frozen'] = True
    state_store.write_json(ROOT / 'installation.json', metadata)
    started = time.monotonic()
    # The freeze survives every export error, including a busy backup lock.
    dc(
        'stop',
        '-t',
        '30',
        *(['federation-bot'] if state().get('federation_seeded') else []),
        *(['telegram-worker'] if telegram_runtime.enabled(metadata) else []),
        'referral-bot',
        'referral-control',
        *owned_databases(),
        'jwt',
    )
    if state().get('federation_seeded'):
        __import__('federation_runtime').controller(ROOT, run, False)
    if telegram_runtime.enabled(metadata):
        telegram_runtime.controller(ROOT, run, False)
    if metadata.get('media_mode', 'local') == 's3':
        if any(
            row['Service']
            in (
                'telegram-worker',
                'federation-bot',
                'referral-bot',
                'referral-control',
                'synapse',
                'mas',
                'jwt',
            )
            and row['State'] in ('running', 'restarting')
            for row in service_rows()
        ):
            raise RuntimeError(
                'Migration writers are still running; no final S3 archive created. Check install.sh status and retry move-export; do not activate the target'
            )
        try:
            emit(
                'migration_external_media_started',
                next_action='Wait for every live S3 object to be verified; missing originals are downloaded before the final archive. Source writers are stopped; duration depends on media size and network',
            )
            result = media_snapshot.hydrate(
                ROOT / 'data/synapse/media_store',
                preflight.settings(ROOT),
                ROOT / 'data/synapse/external-media-inventory.json',
            )
        except (InterruptedError, state_store.OperationInterrupted):
            raise
        except Exception:
            raise RuntimeError(
                'Final S3 media snapshot failed; source remains marked frozen and no final archive was created. Check private S3 credentials/permissions, network/TLS and free space; retry install.sh move-export [DIRECTORY]. To cancel, first stop the target and return DNS, then run install.sh move-abort target-stopped [DIRECTORY]'
            ) from None
        emit('migration_external_media_snapshot', passed=True, **result)
    archive = backup(leave_stopped=True)
    emit(
        'migration_source_frozen',
        archive=str(archive),
        writers_disabled=True,
        export_seconds=round(time.monotonic() - started, 2),
        next_action='Restore this final archive on the target; keep source writers stopped',
    )


def move_abort():
    metadata = state()
    if not metadata.get('migration_frozen'):
        raise RuntimeError('This source has no active migration freeze')
    preflight.check(ROOT)
    metadata.pop('migration_frozen', None)
    state_store.write_json(ROOT / 'installation.json', metadata)
    try:
        up()
    except BaseException:
        metadata = state()
        metadata['migration_frozen'] = True
        state_store.write_json(ROOT / 'installation.json', metadata)
        try:
            telegram_runtime.controller(ROOT, run, start=False)
        finally:
            dc('stop')
        raise


def restore_check():
    # Verify the latest full archive, then restore only into random temporary DBs.
    archive = (ROOT / 'backups/latest.tar.gz').resolve(strict=True)
    temp = ROOT / 'backups' / ('verify-' + uuid.uuid4().hex)
    temp.mkdir(mode=0o700)
    try:
        backup_archive.extract(archive, temp)
        snapshot = backup_archive.verify_snapshot(temp)
        referral.verify_archive(snapshot / 'files.tar.gz')
        __import__('federation_runtime').verify_archive(snapshot / 'files.tar.gz')
        with tarfile.open(snapshot / 'files.tar.gz', 'r:gz') as tar:
            saved_metadata = json.load(tar.extractfile('installation.json'))
        if telegram_runtime.enabled(saved_metadata):
            telegram_runtime.verify_archive(snapshot / 'files.tar.gz')
        databases = backup_archive.validate_authentication_boundary(snapshot, saved_metadata)
        emit('referral_backup_crypto_restore', passed=True)
        for database in databases:
            target = 'restorecheck_' + database + '_' + uuid.uuid4().hex[:8]
            postgres('createdb', '-T', 'template0', target)
            try:
                with (snapshot / (database + '.dump')).open('rb') as data:
                    result = subprocess.run(
                        postgres_command('pg_restore', '--exit-on-error', '-d', target),
                        stdin=data,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        **state_store.child_process_options(),
                    )
                if result.returncode:
                    raise RuntimeError('Restore into temporary database failed')
                metadata_path = snapshot / 'backup-format.json'
                expected = (
                    json.loads(metadata_path.read_text())
                    .get('database_inventory', {})
                    .get(database)
                    if metadata_path.exists()
                    else None
                )
                if expected is not None and database_inventory(target, database) != expected:
                    raise RuntimeError(
                        'Restored application row counts differ from the backup inventory'
                    )
                if database == 'synapse':
                    clear_restored_one_time_keys(target)
                emit(
                    'temporary_database_restore',
                    database=database,
                    passed=True,
                    inventory_compared=expected is not None,
                )
            finally:
                postgres('dropdb', '--if-exists', target)
    finally:
        shutil.rmtree(temp)


def re_safe_basename(value):
    return isinstance(value, str) and Path(value).name == value and value not in ('', '..', '.')


def restore_activate(external_mas_confirm=None):
    marker = ROOT / 'restore-journal.json'
    if not marker.is_file() or json.loads(marker.read_text()).get('stage') != 'ready':
        raise RuntimeError('Restore has not completed; repeat the original restore command')
    metadata = state()
    if not metadata.get('restore_pending'):
        raise RuntimeError(
            'This restore target was already activated; use install.sh status/check. '
            'To restore another archive use an empty directory; existing data was not changed'
        )
    if metadata.get('mas_mode') == 'external':
        if external_mas_confirm != 'preserved-state':
            raise RuntimeError(
                'В копии нет базы и ключей внешнего MAS. Сохраните или восстановите '
                'его прежних пользователей и устройства отдельно, затем подтвердите: '
                'sudo misbot backup activate --confirm source-stopped '
                '--external-mas-confirm preserved-state. Сервер остаётся выключенным.'
            )
        verify_external_mas_state()
    elif external_mas_confirm is not None:
        raise RuntimeError('Подтверждение внешнего MAS применяется только к внешнему MAS.')
    if any(
        row['Service']
        in (
            'synapse',
            'mas',
            'jwt',
            'referral-bot',
            'referral-control',
            'federation-bot',
            'telegram-worker',
        )
        and row['State'] in ('running', 'restarting', 'paused')
        for row in service_rows()
    ):
        raise RuntimeError(
            'Staged target writers are still active (running, restarting or paused); stop them with install.sh down, '
            'then repeat restore-activate after stopping the source; no keys were cleared'
        )
    telegram_runtime.assert_stopped(ROOT, run, metadata)
    preflight.check(ROOT)
    # Also covers ready journals produced by older installers. Repeat after a
    # failed activation is safe because target writers are stopped below.
    clear_restored_one_time_keys('synapse')
    metadata.pop('restore_pending', None)
    state_store.write_json(ROOT / 'installation.json', metadata)
    try:
        up()
    except BaseException:
        metadata = state()
        metadata['restore_pending'] = True
        state_store.write_json(ROOT / 'installation.json', metadata)
        try:
            telegram_runtime.controller(ROOT, run, start=False)
        finally:
            dc('stop')
        raise


def configure(directory, action):
    global ROOT, COMPOSE, STAGES, log
    ROOT = Path(directory).resolve()
    COMPOSE = [
        'docker',
        'compose',
        '--project-directory',
        str(ROOT),
        '-f',
        str(ROOT / 'compose.yaml'),
    ]
    STAGES = ROOT / 'stages'
    os.umask(0o077)
    STAGES.mkdir(exist_ok=True)
    log = STAGES / (
        'certificate-renewal.jsonl'
        if action == 'cert-renew'
        else time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + action + '.jsonl'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    parser.add_argument(
        'action',
        choices=[
            'up',
            'generate',
            'check',
            'backup',
            'backup-recover',
            'restore-check',
            'restore-activate',
            'move-export',
            'move-abort',
            'status',
            'stop',
            'down',
            'cert-renew',
        ],
    )
    parser.add_argument('--external-mas-confirm', choices=('preserved-state',))
    parser.add_argument('--check-up-only', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--register-command', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--remove-command', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_up_only and args.action != 'up':
        parser.error('--check-up-only requires up')
    if args.register_command and args.action not in ('up', 'restore-activate'):
        parser.error('--register-command requires up or restore-activate')
    if args.remove_command and args.action != 'down':
        parser.error('--remove-command requires down')
    configure(args.directory, args.action)

    def interrupted(signum, frame):
        raise state_store.OperationInterrupted('Installer operation interrupted')

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Root is required')
        guard = (
            nullcontext()
            if args.action in ('check', 'status')
            else state_store.operation_guard(ROOT)
        )
        with guard:
            if args.action not in ('check', 'status', 'stop', 'down', 'backup-recover'):
                state_store.assert_update_allowed(ROOT)
            if args.check_up_only:
                up_admission_check()
                emit('up_admission', passed=True)
                return
            if args.action not in ('generate', 'up'):
                ownership_check()
            {
                'up': up,
                'generate': generate,
                'check': check,
                'backup': backup,
                'backup-recover': backup_recover,
                'restore-check': restore_check,
                'restore-activate': lambda: restore_activate(args.external_mas_confirm),
                'move-export': move_export,
                'move-abort': move_abort,
                'cert-renew': cert_renew,
                'status': status,
                'stop': stop,
                'down': down,
            }[args.action]()
            if args.register_command or args.remove_command:
                result = startup_process.run(
                    [
                        '/usr/bin/python3',
                        str(ROOT / 'command_wrapper.py'),
                        'install' if args.register_command else 'remove',
                        '--directory',
                        str(ROOT),
                        '--keep-default',
                    ],
                    emit=emit,
                    timeout=30,
                )
                if result.returncode:
                    raise RuntimeError(
                        'System command registration failed; retry the same operation'
                    )
    except Exception as error:
        recovery = (
            {
                'next_action': 'Check install.sh status; retry install.sh move-export [DIRECTORY]. Before any source restart, stop the target and return DNS, then use install.sh move-abort target-stopped [DIRECTORY]; do not transfer an incomplete archive'
            }
            if args.action == 'move-export'
            else {}
        )
        emit(
            'failed',
            action=args.action,
            error_type=type(error).__name__,
            message=(
                'Operation interrupted; existing data retained. Check install.sh status, then follow the recovery command or retry the same operation'
                if isinstance(error, (InterruptedError, state_store.OperationInterrupted))
                else str(error)
                if type(error) in (RuntimeError, state_store.S3CredentialRecoveryRequired)
                or isinstance(error, __import__('configuration_schema').InvalidConfiguration)
                else 'See the relevant service logs privately'
            ),
            **recovery,
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
