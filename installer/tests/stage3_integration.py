"""Isolated Linux Docker core: local DBs, attachment, migration and single-use token.

No public ports, system wrapper, host tuning or existing installation changes.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch
import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', required=True, type=Path)
    args = parser.parse_args()
    assert os.geteuid() == 0
    source = args.bundle.resolve(strict=True)
    tests = Path(__file__).parent
    parent = Path(tempfile.mkdtemp(prefix='misbot-stage3-core-'))
    parent.chmod(0o700)
    root = parent / 'stack'
    project = 'misbotstage3' + uuid.uuid4().hex[:10]
    sys.path.insert(0, str(source))
    import configuration, lifecycle, state_store, registration

    compose = [
        'docker',
        'compose',
        '-p',
        project,
        '--project-directory',
        str(root),
        '-f',
        str(root / 'integration.yaml'),
    ]

    def emit(check, **values):
        print(json.dumps({'check': check, **values}), flush=True)

    def run(command, stdin=None):
        result = subprocess.run(command, input=stdin, capture_output=True, text=True)
        if result.returncode:
            state_store.write_text(
                parent / 'private-command-error.txt', result.stdout + '\n' + result.stderr
            )
            raise RuntimeError('Fixture command failed: ' + Path(command[0]).name)
        return result.stdout

    def dc(*parts, **kwargs):
        return run(compose + list(parts), **kwargs)

    def wait():
        script = "import urllib.request; urllib.request.urlopen('http://mas:8080/health',timeout=3).read(); urllib.request.urlopen('http://synapse:8008/_matrix/client/versions',timeout=3).read()"
        for _ in range(90):
            try:
                dc('exec', '-T', 'synapse', 'python', '-c', script)
                return
            except RuntimeError:
                time.sleep(2)
        raise RuntimeError('Fixture core not ready')

    def ownership():
        ids = run(
            ['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]
        ).split()
        if not ids:
            raise RuntimeError('Fixture ownership missing')
        rows = run(
            [
                'docker',
                'inspect',
                '--format',
                '{{index .Config.Labels "com.docker.compose.project.working_dir"}}',
                *ids,
            ]
        ).splitlines()
        if any(row != str(root) for row in rows):
            raise RuntimeError('Foreign fixture directory')

    succeeded = False
    started = False
    try:
        run(
            [
                'python3',
                str(source / 'generate.py'),
                '--directory',
                str(root),
                '--domain',
                'bootstrap-test.invalid',
                '--initialize-env',
            ]
        )
        configuration.patch(root, {'PUBLIC_IP': '1.1.1.1', 'DOCKER_SUBNET': '172.30.251.0/24'})
        for line in (source / 'SHA256SUMS').read_text().splitlines():
            name = line.split('  ', 1)[1]
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        shutil.copy2(source / 'SHA256SUMS', root / 'SHA256SUMS')
        run(['python3', str(root / 'generate.py'), '--directory', str(root)])
        config = yaml.safe_load((root / 'compose.yaml').read_text())
        config['services'] = {k: config['services'][k] for k in ('postgres', 'synapse', 'mas')}
        config['secrets'] = {'db_admin': config['secrets']['db_admin']}
        for service in config['services'].values():
            service['restart'] = 'no'
            service['cpus'] = 0.4
            service['mem_limit'] = '256m'
            service['memswap_limit'] = '512m'
        state_store.write_text(root / 'integration.yaml', yaml.safe_dump(config, sort_keys=False))
        started = True
        dc('up', '-d')
        wait()
        credentials = configuration.read_private_json(root / 'secrets/admin-access.json')
        client = configuration.read_private_json(root / 'secrets/referral-client.json')
        dc(
            'exec',
            '-T',
            'synapse',
            'python',
            '-c',
            (root / 'mas_admin.py').read_text(),
            stdin=json.dumps({'credentials': credentials, 'client': client}),
        )
        fixture = 'CREDS=' + repr(credentials) + '\n' + (tests / 'oauth_internal.py').read_text()
        run(compose + ['exec', '-T', 'synapse', 'python', '-'], stdin=fixture)
        metadata = configuration.read_private_json(root / 'installation.json')
        metadata.update(
            databases_initialized=True,
            storage_committed=True,
            database_mode='local',
            media_mode='local',
            network_committed=True,
        )
        state_store.write_json(root / 'installation.json', metadata)
        lifecycle.configure(root, 'stage3-core')
        lifecycle.COMPOSE = compose
        with (
            patch.object(lifecycle, 'configure', lambda *args: None),
            patch.object(lifecycle, 'ownership_check', ownership),
        ):
            first = registration.manage(root, 'ensure')
            token = configuration.read_private_json(root / 'secrets/first-registration.json')[
                'token'
            ]
            assert first['state'] == 'valid' and first['usage_limit'] == 1
            payload = {'client': client, 'token': token}
            output = dc(
                'exec',
                '-T',
                'synapse',
                'python',
                '-c',
                (tests / 'registration_internal.py').read_text(),
                stdin=json.dumps(payload),
            )
            for line in output.splitlines():
                if line.startswith('{'):
                    print(line, flush=True)
            assert registration.manage(root, 'status')['state'] == 'used'
            saved = configuration.read_private_json(root / 'secrets/first-registration.json')[
                'token'
            ]
            registration.manage(root, 'ensure')
            assert (
                configuration.read_private_json(root / 'secrets/first-registration.json')['token']
                == saved
            )
            assert registration.manage(root, 'revoke')['state'] == 'revoked'
            assert registration.manage(root, 'new')['state'] == 'valid'
            emit('first_token_repeat_revoke_replace', passed=True)
            # Upload random synthetic media; validate the same old MXC after migration.
            media_script = """import json,urllib.request,hashlib,secrets
from pathlib import Path
saved=json.loads(Path('/data/installer-test-client.json').read_text());auth={'Authorization':'Bearer '+saved['access_token']}
body=secrets.token_bytes(4096)
request=urllib.request.Request('http://synapse:8008/_matrix/media/v3/upload',data=body,headers={**auth,'Content-Type':'application/octet-stream'})
with urllib.request.urlopen(request,timeout=20) as response:mxc=json.load(response)['content_uri']
Path('/data/stage3-attachment.json').write_text(json.dumps({'mxc':mxc,'sha256':hashlib.sha256(body).hexdigest()}))
"""
            dc('exec', '-T', 'synapse', 'python', '-c', media_script)
            inventory = {name: lifecycle.database_inventory(name) for name in ('synapse', 'mas')}
            keys = {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root / 'secrets').iterdir()
                if p.is_file()
            }
            keys['config/mas-signing.pem'] = hashlib.sha256(
                (root / 'config/mas-signing.pem').read_bytes()
            ).hexdigest()
            # Prepare an existing legacy-format fixture, preserving every value.
            model = configuration.load(root, True)
            shutil.rmtree(root / 'settings')
            state_store.write_text(root / 'settings.env', configuration.serialize(model))
            metadata = configuration.read_private_json(root / 'installation.json')
            metadata['config_format_version'] = 1
            state_store.write_json(root / 'installation.json', metadata)
            lifecycle.backup()
            lifecycle.restore_check()
            emit('legacy_backup_restore_both_databases', passed=True)
            original_write = state_store.write_text
            publications = []

            def interrupted_publication(path, text, *args, **kwargs):
                if Path(path).parent == root / 'settings':
                    publications.append(path)
                    if len(publications) == 2:
                        raise OSError('Fictional configuration publication interruption')
                return original_write(path, text, *args, **kwargs)

            with patch.object(state_store, 'write_text', side_effect=interrupted_publication):
                try:
                    configuration.migrate(root)
                except OSError:
                    pass
                else:
                    raise AssertionError('Fault injection did not interrupt migration')
            try:
                configuration.load(root, True)
            except RuntimeError:
                pass
            else:
                raise AssertionError('Partial configuration was readable')
            configuration.recover(root)
            configuration.migrate(root)
            assert configuration.load(root, True) == model
            emit('configuration_partial_publication_forward_recovery_repeat', passed=True)
            assert {
                name: lifecycle.database_inventory(name) for name in ('synapse', 'mas')
            } == inventory
            for name, digest in keys.items():
                assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
            check_media = """import json,urllib.request,hashlib
from pathlib import Path
saved=json.loads(Path('/data/installer-test-client.json').read_text());item=json.loads(Path('/data/stage3-attachment.json').read_text())
url='http://synapse:8008/_matrix/client/v1/media/download/'+item['mxc'][6:]
with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+saved['access_token']}),timeout=20) as response:body=response.read()
assert hashlib.sha256(body).hexdigest()==item['sha256']
"""
            dc('exec', '-T', 'synapse', 'python', '-c', check_media)
            emit('configuration_migration_identity_databases_old_attachment', passed=True)
            lifecycle.backup()
            lifecycle.restore_check()
            emit('split_backup_restore_both_databases', passed=True)
        succeeded = True
        emit('stage3_isolated_core', passed=True, public_ports=False)
    except Exception as error:
        emit('stage3_isolated_core', passed=False, private_fixture=str(parent))
        emit('fixture_failure_kind', error_type=type(error).__name__)
        if started:
            logs = subprocess.run(
                compose + ['logs', '--no-color', '--tail', '80'], capture_output=True, text=True
            )
            state_store.write_text(parent / 'private-container-logs.txt', logs.stdout + logs.stderr)
        raise
    finally:
        if started:
            try:
                dc('down')
            except Exception:
                succeeded = False
                emit('fixture_cleanup_pending', project=project)
        if succeeded:
            shutil.rmtree(parent)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Isolated core verification failed; protected fixture retained', file=sys.stderr)
        sys.exit(1)
