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
    parent = Path(tempfile.mkdtemp(prefix='misbot-stage4-core-'))
    parent.chmod(0o700)
    root = parent / 'stack'
    project = 'misbotstage4' + uuid.uuid4().hex[:10]
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
        federation_image = 'misbot-stage4-federation:' + project
        run(
            [
                'env',
                'DOCKER_BUILDKIT=0',
                'docker',
                'build',
                '--memory',
                '256m',
                '--memory-swap',
                '384m',
                '-t',
                federation_image,
                str(root / 'federation-bot'),
            ]
        )
        config = yaml.safe_load((root / 'compose.yaml').read_text())
        config['services'] = {
            k: config['services'][k]
            for k in (
                'postgres',
                'synapse',
                'mas',
                'referral-control',
                'referral-bot',
                'federation-bot',
                'web',
            )
        }
        config['secrets'] = {'db_admin': config['secrets']['db_admin']}
        for service in config['services'].values():
            service['restart'] = 'no'
            service['cpus'] = 0.4
            service['mem_limit'] = '128m'
            service['memswap_limit'] = '256m'
        config['services']['synapse']['mem_limit'] = '192m'
        # Match production: bots may start while MAS/Synapse are still recovering.
        # Their saved session is retried by Docker; never provision another device.
        for role in ('referral-bot', 'federation-bot'):
            config['services'][role]['restart'] = 'unless-stopped'
        for role, folder, cfg in [
            ('referral-bot', 'referral-bot', 'referral'),
            ('federation-bot', 'federation-bot', 'federation'),
        ]:
            config['services'][role]['image'] = run(
                [
                    'docker',
                    'image',
                    'inspect',
                    'matrix-stack/referral-bot:1.0.0',
                    '--format',
                    '{{.Id}}',
                ]
            ).strip()
            for name in ('main.js', 'logic.js', 'signing.js', 'package.json', 'package-lock.json'):
                config['services'][role]['volumes'].append(
                    str(root / folder / name) + ':/app/' + name + ':ro'
                )
        config['services']['referral-control']['volumes'].append(
            str(root / 'referral-control/provision.py') + ':/app/provision.py:ro'
        )
        config['services']['referral-control']['image'] = run(
            [
                'docker',
                'image',
                'inspect',
                'matrix-stack/referral-control:1.0.0',
                '--format',
                '{{.Id}}',
            ]
        ).strip()
        config['services']['federation-bot']['image'] = run(
            ['docker', 'image', 'inspect', federation_image, '--format', '{{.Id}}']
        ).strip()
        state_store.write_text(root / 'integration.yaml', yaml.safe_dump(config, sort_keys=False))
        started = True
        dc('up', '-d', 'postgres', 'synapse', 'mas', 'referral-control', 'web')
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
        import referral, federation_runtime, federation

        lifecycle.configure(root, 'stage4-core')
        lifecycle.COMPOSE = compose

        def configure_own(directory, action):
            assert Path(directory) == root
            lifecycle.ROOT = root
            lifecycle.COMPOSE = compose

        with (
            patch.object(lifecycle, 'configure', side_effect=configure_own),
            patch.object(lifecycle, 'ownership_check', side_effect=ownership),
            state_store.operation_guard(root),
        ):
            referral.provision(root, dc, metadata)
            federation_runtime.provision(root, dc, metadata)
            state_store.write_json(root / 'installation.json', metadata)
        script = parent / 'controller.py'
        script.write_text(
            'import sys,json\nfrom pathlib import Path\nsys.path.insert(0,'
            + repr(str(root))
            + ')\nimport lifecycle,federation_service\nroot=Path('
            + repr(str(root))
            + ')\ncompose='
            + repr(compose)
            + "\nlifecycle.configure(root,'stage4-controller')\ndef configure(directory,action):\n lifecycle.ROOT=root;lifecycle.COMPOSE=compose\ndef ownership():\n ids=lifecycle.run(['docker','ps','-aq','--filter','label=com.docker.compose.project="
            + project
            + "']).split()\n rows=lifecycle.run(['docker','inspect','--format','{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}',*ids]).splitlines()\n assert ids and all(row==str(root) for row in rows)\nlifecycle.configure=configure;lifecycle.ownership_check=ownership\nfederation_service.serve(root)\n"
        )
        controller_log = (parent / 'private-controller.log').open('w')
        controller = subprocess.Popen(
            ['python3', str(script)], stdout=controller_log, stderr=controller_log
        )
        for _ in range(50):
            if (root / 'run/federation/control.sock').exists():
                break
            if controller.poll() is not None:
                raise RuntimeError('Controller startup failed')
            time.sleep(0.2)
        else:
            raise RuntimeError('Controller socket missing')
        dc('up', '-d', 'referral-bot', 'federation-bot')
        for role in ('referral-bot', 'federation-bot'):
            for _ in range(120):
                try:
                    dc(
                        'exec',
                        '-T',
                        role,
                        'node',
                        '-e',
                        "fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                    )
                    break
                except RuntimeError:
                    time.sleep(1)
            else:
                raise RuntimeError('Bot readiness failed: ' + role)
        metadata = configuration.read_private_json(root / 'installation.json')
        referral.note_ready(root, metadata)
        metadata['federation_crypto_initialized'] = True
        federation_runtime.validate(root, metadata)
        state_store.write_json(root / 'installation.json', metadata)
        test = parent / 'client'
        test.mkdir(mode=0o700)
        raw = dc('exec', '-T', 'synapse', 'cat', '/data/installer-test-client.json')
        state_store.write_json(test / 'config.json', json.loads(raw))
        image = run(
            ['docker', 'image', 'inspect', 'matrix-stack/referral-bot:1.0.0', '--format', '{{.Id}}']
        ).strip()
        network = project + '_default'

        def client(restored=False):
            return run(
                [
                    'docker',
                    'run',
                    '--rm',
                    '--network',
                    network,
                    '--memory',
                    '192m',
                    '--memory-swap',
                    '320m',
                    '--cpus',
                    '0.5',
                    '--user',
                    '0:0',
                    '-e',
                    'NODE_OPTIONS=--max-old-space-size=128',
                    '-v',
                    str(test) + ':/test',
                    '-v',
                    str(tests / 'stage4_matrix.js') + ':/app/stage4-test.js:ro',
                    image,
                    'node',
                    '/app/stage4-test.js',
                    *(['restored'] if restored else []),
                ]
            )

        print(client(), flush=True)
        with (
            patch.object(lifecycle, 'configure', side_effect=configure_own),
            patch.object(lifecycle, 'ownership_check', side_effect=ownership),
        ):
            backend = federation.Backend(root)
            assert backend.operate('servers', actor='@matrix-admin:bootstrap-test.invalid')[
                'servers'
            ]
            assert not backend.check_admin('@referral-bot:bootstrap-test.invalid')
            assert not backend.check_admin('@matrix-admin:foreign.invalid')
            result = backend.operate(
                'add', 'peer-test.invalid', actor='@matrix-admin:bootstrap-test.invalid'
            )
            assert result['status'] == 'applied'
            emit('real_federation_policy_checked_backup_apply_restart', passed=True)
            keys = {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    'data/federation/identity.pem',
                    'secrets/federation-session.json',
                    'data/federation-bot/cross-signing.json',
                )
            }
            with state_store.operation_guard(root):
                archive = lifecycle.backup()
                lifecycle.restore_check()
            from tempfile import TemporaryDirectory
            import tarfile

            with TemporaryDirectory(prefix='misbot-stage4-restored-') as destination:
                with tarfile.open(archive) as tar:
                    tar.extractall(destination, filter='data')
                snapshot = next(Path(destination).iterdir())
                federation_runtime.verify_archive(snapshot / 'files.tar.gz')
                with tarfile.open(snapshot / 'files.tar.gz') as tar:
                    for name, digest in keys.items():
                        assert hashlib.sha256(tar.extractfile(name).read()).hexdigest() == digest
                import backup_archive

                restored_files = Path(destination) / 'files'
                restored_files.mkdir(mode=0o700)
                backup_archive.extract(snapshot / 'files.tar.gz', restored_files, owners=True)
                dc('stop', '-t', '30', 'federation-bot')
                assert all(
                    row['State'] not in ('running', 'restarting')
                    for row in lifecycle.service_rows()
                    if row['Service'] == 'federation-bot'
                )
                replacement = yaml.safe_load((root / 'integration.yaml').read_text())
                volumes = replacement['services']['federation-bot']['volumes']
                replacement['services']['federation-bot']['volumes'] = [
                    str(restored_files / 'data/federation-bot') + ':/data'
                    if v.endswith(':/data')
                    else v
                    for v in volumes
                ]
                state_store.write_text(
                    root / 'restored-integration.yaml', yaml.safe_dump(replacement, sort_keys=False)
                )
                run(
                    [
                        'docker',
                        'compose',
                        '-p',
                        project,
                        '--project-directory',
                        str(root),
                        '-f',
                        str(root / 'restored-integration.yaml'),
                        'up',
                        '-d',
                        '--no-deps',
                        'federation-bot',
                    ]
                )
                for _ in range(120):
                    try:
                        dc(
                            'exec',
                            '-T',
                            'federation-bot',
                            'node',
                            '-e',
                            "fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                        )
                        break
                    except RuntimeError:
                        time.sleep(1)
                else:
                    raise RuntimeError('Restored bot not ready')
                print(client(True), flush=True)
                dc('stop', '-t', '30', 'federation-bot')
                dc('up', '-d', '--no-deps', 'federation-bot')
            emit('real_backup_both_db_restore_federation_session_crypto_identity', passed=True)
            backend.operate(
                'remove', 'peer-test.invalid', actor='@matrix-admin:bootstrap-test.invalid'
            )
            for name, digest in keys.items():
                assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
        succeeded = True
        emit('stage4_isolated_matrix', passed=True, public_ports=False, public_federation=False)
    except Exception as error:
        emit(
            'stage4_isolated_matrix',
            passed=False,
            private_fixture=str(parent),
            error_type=type(error).__name__,
        )
        import traceback

        for frame in traceback.extract_tb(error.__traceback__)[-4:]:
            emit(
                'fixture_failure_frame',
                file=Path(frame.filename).name,
                function=frame.name,
                line=frame.lineno,
            )
        if started:
            logs = subprocess.run(
                compose + ['logs', '--no-color', '--tail', '100'], capture_output=True, text=True
            )
            state_store.write_text(parent / 'private-container-logs.txt', logs.stdout + logs.stderr)
        raise
    finally:
        if 'controller' in locals():
            controller.terminate()
            controller.wait(20)
            controller_log.close()
        if started:
            try:
                dc('down')
            except Exception:
                succeeded = False
                emit('fixture_cleanup_pending', project=project)
        if succeeded:
            run(['docker', 'image', 'rm', federation_image])
            shutil.rmtree(parent)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Isolated Matrix verification failed; protected fixture retained', file=sys.stderr)
        sys.exit(1)
