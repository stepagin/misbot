#!/usr/bin/env python3
"""Deploy an explicitly authorized disposable PostgreSQL/Vault/Dex test fixture.

Never use this fixture as production infrastructure. All credentials are random,
stored in private files, and only Caddy HTTPS and authenticated PostgreSQL TLS
are published. Persistent Vault uses one test-only unseal share.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import yaml


def run(*command):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Fixture command failed: ' + Path(command[0]).name +
                           '; inspect private fixture diagnostics')
    return result.stdout


def private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(value)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def request(url, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Vault-Token'] = token
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def wait(check):
    for _ in range(90):
        try:
            if check():
                return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(2)
    raise RuntimeError('Fixture was not ready after 180 seconds; inspect logs privately')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('deploy', 'status', 'destroy'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--client-ip', required=True)
    parser.add_argument('--versions', type=Path, required=True)
    parser.add_argument('--public-port',type=int,default=443,
                        help='Use 8443 to leave Matrix public ports free after initial certificate issuance')
    parser.add_argument('--extra-client-ip',action='append',default=[])
    args = parser.parse_args()
    if not 1<=args.public_port<=65535:raise RuntimeError('Invalid fixture HTTPS port')
    import ipaddress
    ipaddress.IPv4Address(args.client_ip)
    for address in args.extra_client_ip:ipaddress.IPv4Address(address)
    authority=args.domain+(':'+str(args.public_port) if args.public_port!=443 else '')
    if os.geteuid() != 0:
        raise RuntimeError('Run this explicitly authorized fixture as root')
    os.umask(0o077)
    root = args.directory.absolute()
    if root.is_symlink() or root.resolve()!=root or root.parent!=Path('/opt') or not root.name.startswith('matrix-installer-test-'):
        raise RuntimeError('Choose a dedicated /opt/matrix-installer-test-* directory')
    if root.exists() and any(root.iterdir()) and not (root / 'fixture.json').is_file():
        raise RuntimeError('Fixture directory is not empty and not owned by this fixture')
    dc = ['docker', 'compose', '-p', 'installerresources', '--project-directory', str(root),
          '-f', str(root / 'compose.yaml')]
    if args.action != 'deploy':
        marker = json.loads((root / 'fixture.json').read_text())
        if marker.get('domain') != args.domain:
            raise RuntimeError('Fixture domain mismatch')
        if args.action == 'status':
            print(run(*dc, 'ps'))
        else:
            run(*dc, 'down', '--remove-orphans')
            shutil.rmtree(root)
            print(json.dumps({'check': 'external_fixture_removed', 'passed': True}))
        return
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    marker = root / 'fixture.json'
    if marker.exists() and json.loads(marker.read_text()).get('domain') != args.domain:
        raise RuntimeError('Cannot change the fixture identity')
    private(marker, json.dumps({'domain': args.domain, 'client_ip': args.client_ip,'public_port':args.public_port}))
    credential_path = root / 'credentials.json'
    if credential_path.exists():
        credentials = json.loads(credential_path.read_text())
    else:
        credentials = {name: secrets.token_urlsafe(36) for name in
                       ('postgres_password', 'oidc_client_secret', 'oidc_password')}
        private(credential_path, json.dumps(credentials))
    lock = json.loads(args.versions.read_text())
    refs = {'postgres': lock['digests']['postgres'], 'caddy': lock['digests']['caddy'],
            'vault': 'hashicorp/vault:1.20.4', 'dex': 'ghcr.io/dexidp/dex:v2.44.0'}
    images = {}
    for name, reference in refs.items():
        run('docker', 'pull', reference)
        inspected = json.loads(run('docker', 'image', 'inspect', reference))[0]
        images[name] = inspected['Id']
        print(json.dumps({'check': 'fixture_image', 'service': name, 'image_id': images[name]}), flush=True)
    private(root / 'images.json', json.dumps(images))
    private(root / 'postgres-password', credentials['postgres_password'])
    private(root / 'vault.hcl', '''ui = false
disable_mlock = true
storage "file" { path = "/vault/data" }
listener "tcp" { address = "0.0.0.0:8200" tls_disable = true }
api_addr = "http://vault:8200"
''')
    import bcrypt
    dex = {'issuer': 'https://' + authority + '/dex',
        'storage': {'type': 'sqlite3', 'config': {'file': '/data/dex.db'}},
        'web': {'http': '0.0.0.0:5556'}, 'enablePasswordDB': True,
        'staticPasswords': [{'email': 'fixture-user@example.invalid', 'username': 'fixture-user',
            'userID': '00000000-0000-4000-8000-000000000001',
            'hash': bcrypt.hashpw(credentials['oidc_password'].encode(), bcrypt.gensalt()).decode()}],
        'staticClients': [{'id': 'synapse-installer-fixture',
            'secret': credentials['oidc_client_secret'], 'name': 'Disposable Matrix installer test',
            'redirectURIs': ['https://mas.misbot.ru/upstream/callback/01K5WZ00000000000000000003']}],
        'oauth2': {'skipApprovalScreen': True}}
    private(root / 'dex.yaml', yaml.safe_dump(dex))
    private(root / 'Caddyfile', args.domain + ''' {
    handle /v1/* {
        reverse_proxy vault:8200
    }
    handle /dex/* {
        reverse_proxy dex:5556
    }
    handle {
        respond "Disposable Synapse installer resources" 200
    }
}
''')
    run('docker', 'run', '--rm', '--network', 'none', '--memory', '96m',
        '-v', str(root / 'Caddyfile') + ':/config/Caddyfile:ro', images['caddy'],
        'caddy', 'validate', '--config', '/config/Caddyfile', '--adapter', 'caddyfile')
    for name in ('postgres', 'vault', 'dex', 'caddy', 'caddy-config', 'postgres-tls'):
        (root / 'data' / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(root / 'data/postgres', 999, 999)
    os.chown(root / 'postgres-password', 999, 999)
    common = {'restart': 'no', 'cpus': 0.25, 'pids_limit': 128,
              'security_opt': ['no-new-privileges:true'],
              'logging': {'driver': 'json-file', 'options': {'max-size': '1m', 'max-file': '2'}}}
    def service(name, memory, **values):
        return {**common, 'image': images[name], 'mem_limit': f'{memory}m',
                'memswap_limit': f'{memory * 2}m', **values}
    config = {'services': {
        'vault': service('vault', 128, user='0:0', entrypoint=['vault'], command=['server', '-config=/config/vault.hcl'],
            cap_drop=['ALL'], volumes=['./vault.hcl:/config/vault.hcl:ro', './data/vault:/vault/data']),
        'dex': service('dex', 80, user='0:0', command=['dex', 'serve', '/config/dex.yaml'],
            cap_drop=['ALL'], volumes=['./dex.yaml:/config/dex.yaml:ro', './data/dex:/data']),
        'caddy': service('caddy', 48, ports=['80:80', '443:443'] if args.public_port==443 else [str(args.public_port)+':443'],
            volumes=['./Caddyfile:/etc/caddy/Caddyfile:ro', './data/caddy:/data', './data/caddy-config:/config'])},
        'networks': {'default': {'ipam': {'config': [{'subnet': '172.29.180.0/24'}]}}}}
    private(root / 'compose.yaml', yaml.safe_dump(config))
    run(*dc, 'up', '-d','--force-recreate')
    wait(lambda: bool(request('https://' + authority + '/dex/.well-known/openid-configuration')))
    vault_url = 'https://' + authority
    wait(lambda: 'initialized' in request(vault_url + '/v1/sys/init'))
    initialized = request(vault_url + '/v1/sys/init')['initialized']
    if not initialized:
        recovery = request(vault_url + '/v1/sys/init', {'secret_shares': 1, 'secret_threshold': 1})
        private(root / 'vault-recovery.json', json.dumps(recovery))
    else:
        recovery = json.loads((root / 'vault-recovery.json').read_text())
    request(vault_url + '/v1/sys/unseal', {'key': recovery['keys_base64'][0]})
    token = recovery['root_token']
    if not (root / 'vault-configured').exists():
        request(vault_url + '/v1/sys/mounts/matrix', {'type': 'kv', 'options': {'version': '2'}}, token)
        private(root / 'vault-configured', 'yes\n')
    certdir = root / 'data/caddy/caddy/certificates/acme-v02.api.letsencrypt.org-directory' / args.domain
    wait(lambda: (certdir / (args.domain + '.crt')).is_file())
    tls = root / 'data/postgres-tls'
    for suffix, target in (('.crt', 'server.crt'), ('.key', 'server.key')):
        shutil.copyfile(certdir / (args.domain + suffix), tls / target)
        os.chown(tls / target, 999, 999)
        (tls / target).chmod(0o600)
    tls.chmod(0o755)
    private(root / 'pg_hba.conf', 'local all all trust\n' +
        f'hostssl all all {args.client_ip}/32 scram-sha-256\n' +
        ''.join(f'hostssl all all {address}/32 scram-sha-256\n' for address in args.extra_client_ip)+
        'host all all 0.0.0.0/0 reject\nhost all all ::/0 reject\n')
    config['services']['postgres'] = service('postgres', 96,
        environment={'POSTGRES_PASSWORD_FILE': '/run/secrets/password',
                     'POSTGRES_INITDB_ARGS': '--encoding=UTF8 --locale=C'},
        ports=['55432:5432'],
        command=['postgres', '-c', 'ssl=on', '-c', 'ssl_cert_file=/tls/server.crt',
                 '-c', 'ssl_key_file=/tls/server.key', '-c', 'hba_file=/config/pg_hba.conf',
                 '-c', 'shared_buffers=16MB', '-c', 'max_connections=20'],
        volumes=['./data/postgres:/var/lib/postgresql', './postgres-password:/run/secrets/password:ro',
                 './data/postgres-tls:/tls:ro', './pg_hba.conf:/config/pg_hba.conf:ro'])
    (root / 'pg_hba.conf').chmod(0o644)
    private(root / 'compose.yaml', yaml.safe_dump(config))
    run(*dc, 'up', '-d','--force-recreate', 'postgres')
    wait(lambda: '1' in run(*dc, 'exec', '-T', 'postgres', 'psql', '-U', 'postgres', '-Atc', 'SELECT 1'))
    # Derived client files are private handoffs, never output as environment variables or URLs.
    private(root / 'postgres.env', f'POSTGRES_HOST={args.domain}\nPOSTGRES_PORT=55432\n'
        'POSTGRES_SSL_MODE=verify-full\nPOSTGRES_ADMIN_USER=postgres\n' +
        'POSTGRES_ADMIN_PASSWORD=' + credentials['postgres_password'] + '\n')
    private(root / 'oidc.env', 'OIDC_ISSUER=https://' + authority + '/dex\n'
        'OIDC_CLIENT_ID=synapse-installer-fixture\nOIDC_CLIENT_SECRET=' + credentials['oidc_client_secret'] + '\n')
    print(json.dumps({'check': 'external_resources_ready', 'postgres_tls': True,
        'vault_persistent_unsealed': True, 'oidc_discovery': True,
        'credentials_directory': str(root)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'check': 'external_resources_failed', 'error_type': type(error).__name__,
                          'message': str(error) if isinstance(error, RuntimeError) else
                          'Inspect the private fixture logs; no credentials are printed'}))
        raise SystemExit(1)
