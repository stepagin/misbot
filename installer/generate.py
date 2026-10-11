#!/usr/bin/env python3
"""Generate the portable Matrix stack; never rotate existing server credentials."""

import argparse
import atexit
import json
import os
from pathlib import Path
import secrets
import subprocess
import ipaddress
import re
import socket
from urllib.parse import urlsplit
import yaml
import referral
import state_store
import media_transfer
import vault_cache
import postgres_setup
import configuration
import configuration_schema
import preflight
import homepage


def merge(base, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


parser = argparse.ArgumentParser()
parser.add_argument('--directory', required=True)
parser.add_argument('--domain')
parser.add_argument('--initialize-env', action='store_true')
parser.add_argument('--preview', action='store_true')
parser.add_argument('--database-mode', choices=('local', 'external'), default='local')
parser.add_argument('--media-mode', choices=('local', 's3'), default='local')
args = parser.parse_args()


def fail(message, code=None):
    if code is not None:
        print(json.dumps({'check': 'configuration_input', 'code': code}), flush=True)
    parser.error(message)


def domain_name(value):
    try:
        value = value.strip().lower().encode('idna').decode('ascii')
    except (ValueError, UnicodeError):
        fail('Invalid domain')
    if (
        len(value) > 253
        or '.' not in value
        or not all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in value.split('.')
        )
    ):
        fail(
            'Domain must be a hostname without scheme, path or port. Use example.org, then repeat init with the same directory'
        )
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    fail('Use a domain, not an IP address. Correct it and repeat init with the same directory')


# Reject an initial typo before creating an otherwise unowned nonempty directory.
input_domain = domain_name(args.domain) if args.domain else None
root = Path(args.directory).absolute()
if root.is_symlink() or root == Path('/'):
    parser.error('Choose a dedicated installation directory, not a symlink or filesystem root')
state_path = root / 'installation.json'
if not input_domain and not state_path.exists():
    parser.error('--domain is required for the first initialization; use init example.org')
if root.exists() and any(root.iterdir()) and not state_path.exists():
    try:
        __import__('command_wrapper').trusted_runtime(root)
        import update

        source_entries, _ = update.validate_manifest(root)
        allowed = {Path(name).parts[0] for name in source_entries} | {
            'SHA256SUMS',
            '.misbot-empty.json',
            '__pycache__',
        }
        if any(path.name not in allowed for path in root.iterdir()):
            raise RuntimeError('Unexpected retained data')
    except (OSError, ValueError, RuntimeError):
        parser.error('Refusing to overwrite an unrelated nonempty directory')
os.umask(0o077)
root.mkdir(parents=True, exist_ok=True)
operation_guard = state_store.operation_guard(root)
try:
    operation_guard.__enter__()
except RuntimeError as error:
    parser.error(str(error))
atexit.register(operation_guard.__exit__, None, None, None)
try:
    state_store.assert_update_allowed(root, allow_restore_generation=True)
except RuntimeError as error:
    parser.error(str(error))
root.chmod(0o700)
config = root / 'config'
config.mkdir(exist_ok=True)
config.chmod(0o755)
public = root / 'public'
public.mkdir(exist_ok=True)
public.chmod(0o755)
secret_dir = root / 'secrets'
secret_dir.mkdir(mode=0o700, exist_ok=True)
secret_dir.chmod(0o700)
state = json.loads(state_path.read_text()) if state_path.exists() else {}
if state.get('config_format_version', 1) not in (1, 2):
    parser.error('Unsupported configuration format; use a compatible installer')


def save_state():
    state_store.write_json(state_path, state)
    (root / '.misbot-empty.json').unlink(missing_ok=True)


selected_domain = input_domain or state.get('domain')
if not selected_domain:
    fail('--domain is required for the first initialization')
if state and (
    selected_domain != state.get('domain') or args.preview != state.get('preview', False)
):
    fail('Domain and preview/production mode are immutable; use a new installation directory')
domain = selected_domain
if not (root / 'settings.env').exists() and not (root / 'settings').exists():
    if state.get('seeded'):
        fail('User configuration is missing; restore it from backup')
    configuration.write_model(
        root,
        {'DOMAIN': domain, 'MEDIA_MODE': 'local', 'POSTGRES_MODE': 'local'},
        preview=args.preview,
    )
state['config_format_version'] = 2 if (root / 'settings').exists() else 1
try:
    settings = configuration.load(root, complete=not args.initialize_env, preview=args.preview)
except configuration_schema.InvalidConfiguration as error:
    fail(str(error), error.code)
if domain_name(settings.get('DOMAIN', '')) != domain:
    fail('Changing DOMAIN is not supported; create a new installation')
mas_domain = configuration_schema.mas_host(settings)
try:
    configuration.assert_auth_identity(root, settings, state)
except RuntimeError as error:
    fail(str(error), 'mas_identity_changed')
database_mode = settings.get('POSTGRES_MODE', 'local')
media_mode = settings.get('MEDIA_MODE', 'local')
if database_mode not in ('local', 'external') or media_mode not in ('local', 's3'):
    fail('POSTGRES_MODE must be local/external and MEDIA_MODE must be local/s3')
if (
    state.get('databases_initialized')
    and not state.get('storage_committed')
    and (database_mode != 'local' or media_mode != 'local')
):
    fail('Existing local installation requires a separate storage migration')
if state.get('storage_committed') and (
    database_mode != state.get('database_mode', 'local')
    or media_mode != state.get('media_mode', 'local')
    or (media_mode == 's3' and settings.get('S3_PREFIX') != state.get('s3_prefix'))
):
    fail('Changing an installed storage mode or S3 prefix requires a separate migration')
if state.get('storage_committed') and media_mode == 's3':
    previous = state.get('s3_identity')
    if previous is None and (config / 'homeserver.yaml').is_file():
        providers = yaml.safe_load((config / 'homeserver.yaml').read_text()).get(
            'media_storage_providers', []
        )
        if providers:
            saved_provider = providers[0]['config']
            previous = {
                'S3_BUCKET': saved_provider['bucket'],
                'S3_REGION': saved_provider['region_name'],
                'S3_ENDPOINT_URL': saved_provider.get('endpoint_url', ''),
                'S3_PREFIX': saved_provider['prefix'],
            }
    if previous is not None and previous != media_transfer.identity(settings):
        fail(
            'Changing an installed S3 bucket, endpoint, region or prefix requires a separate data migration'
        )
database_host = settings.get('POSTGRES_HOST', '')
database_port = settings.get('POSTGRES_PORT', '5432')
database_ssl = settings.get('POSTGRES_SSL_MODE', 'require')
if database_mode == 'external' and not args.initialize_env:
    if (
        not database_host
        or not re.fullmatch(r'[A-Za-z0-9.-]+', database_host)
        or not re.fullmatch(r'[0-9]{1,5}', database_port)
        or not 1 <= int(database_port) <= 65535
    ):
        fail('External PostgreSQL requires POSTGRES_HOST and a valid POSTGRES_PORT')
    if database_ssl not in ('require', 'verify-full'):
        fail('POSTGRES_SSL_MODE must be require or verify-full')
    if not re.fullmatch(
        r'[a-zA-Z_][a-zA-Z0-9_]{0,62}', settings.get('POSTGRES_ADMIN_USER', 'postgres')
    ) or not settings.get('POSTGRES_ADMIN_PASSWORD'):
        fail('External PostgreSQL requires POSTGRES_ADMIN_USER and POSTGRES_ADMIN_PASSWORD')
    if state.get('storage_committed'):
        checkpoint = config / 'external-postgres-owner.json'
        previous = None
        if checkpoint.is_file() and not checkpoint.is_symlink():
            saved = json.loads(checkpoint.read_text())
            if saved.get('format') != 1 or saved.get('identity', {}).get('server_name') != domain:
                fail(
                    'External PostgreSQL identity checkpoint does not match this server; restore the original checkpoint from backup',
                    'postgres_identity_mismatch',
                )
            previous = saved['identity']
        elif (config / 'homeserver.yaml').is_file():
            # Legacy releases retained the rendered connection identity, even
            # before the ownership checkpoint existed. Do not inspect passwords.
            saved = (
                yaml.safe_load((config / 'homeserver.yaml').read_text())
                .get('database', {})
                .get('args', {})
            )
            previous = {'POSTGRES_HOST': saved.get('host'), 'POSTGRES_PORT': str(saved.get('port'))}
        if previous is None:
            fail(
                'Cannot confirm the installed external PostgreSQL destination; restore its checkpoint/config from backup before retrying generate/up',
                'postgres_identity_missing',
            )
        if (
            previous.get('POSTGRES_HOST') != database_host
            or str(previous.get('POSTGRES_PORT')) != database_port
        ):
            fail(
                'Changing installed external PostgreSQL host or port requires a separate data migration. Restore the original POSTGRES_HOST/POSTGRES_PORT from your verified backup, then retry install.sh up; keep both datasets and the ownership checkpoint',
                'postgres_destination_changed',
            )
if media_mode == 's3' and not args.initialize_env:
    import s3_config

    try:
        s3_config.validate({k: v for k, v in settings.items() if k in s3_config.FIELDS})
    except s3_config.ConfigError as error:
        fail(str(error), error.code)
    if not all(
        settings.get(k)
        for k in ('S3_BUCKET', 'S3_REGION', 'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY', 'S3_PREFIX')
    ):
        fail('S3 media requires bucket, region, credentials and an installation-specific prefix')
    endpoint = settings.get('S3_ENDPOINT_URL', '')
    if endpoint and (
        urlsplit(endpoint).scheme != 'https'
        or not urlsplit(endpoint).hostname
        or urlsplit(endpoint).username
        or urlsplit(endpoint).password
        or urlsplit(endpoint).path not in ('', '/')
    ):
        fail('S3_ENDPOINT_URL must be an HTTPS origin without credentials or path')
    if not re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}/', settings['S3_PREFIX']
    ) or '..' in settings['S3_PREFIX'].split('/'):
        fail('S3_PREFIX must be a private, fixed path ending in /')
federation_entries = settings.get('FEDERATION_SERVERS', 'misbot.ru').split(',')
if any(not value.strip() for value in federation_entries):
    fail('FEDERATION_SERVERS requires a nonempty comma-separated list of domains')
federation_servers = list(dict.fromkeys(domain_name(value) for value in federation_entries))
for provider in ['GOOGLE', 'TELEGRAM']:
    if bool(settings.get(provider + '_CLIENT_ID')) != bool(
        settings.get(provider + '_CLIENT_SECRET')
    ):
        fail(
            provider + ' requires both CLIENT_ID and CLIENT_SECRET',
            'oauth_' + provider.lower() + '_pair_incomplete',
        )
registration = settings.get('OPEN_REGISTRATION', 'true')
if registration not in ('true', 'false'):
    fail('OPEN_REGISTRATION must be true or false')
if settings.get('REGISTRATION_TOKEN_REQUIRED', 'true') not in ('true', 'false'):
    fail('REGISTRATION_TOKEN_REQUIRED must be true or false')
for key, default, maximum in [
    ('INVITATION_LIFETIME_SECONDS', '604800', 315360000),
    ('INVITATION_USAGE_LIMIT', '1', 2147483647),
]:
    value = settings.get(key, default)
    if not re.fullmatch(r'[0-9]{1,10}', value) or not 1 <= int(value) <= maximum:
        fail(key + ' must be a positive integer within its supported range')
try:
    network = ipaddress.ip_network(settings.get('DOCKER_SUBNET', '172.30.50.0/24'), strict=True)
    if network.version != 4 or network.prefixlen != 24 or not network.is_private:
        raise ValueError()
except ValueError:
    fail('DOCKER_SUBNET must be a private IPv4 /24 network')
subnet = str(network)
if state.get('network_committed') and state.get('subnet') and state['subnet'] != subnet:
    fail('Changing the installed Docker subnet is not supported')
vault_cache.mark_dirty(root)
state.update(
    domain=domain, subnet=subnet, preview=args.preview, mas_issuer='https://' + mas_domain + '/'
)
save_state()
lock = json.loads((Path(__file__).parent / 'versions.json').read_text())
images, resolved = lock['tags'], lock['digests']
required_roles = {
    'postgres',
    'synapse',
    'mas',
    'jwt',
    'livekit',
    'web',
    'admin',
    'caddy',
    'tls-router',
}
if (
    set(images) != required_roles
    or set(resolved) != required_roles
    or not all(re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', image) for image in resolved.values())
):
    fail('Invalid versions.json image lock')
(root / 'images.lock.json').write_text(json.dumps(lock, indent=2) + '\n')


def secret(name):
    if args.preview:
        return 'REDACTED_' + name
    path = secret_dir / name
    if path.is_symlink():
        fail('Unexpected symlink in secrets directory')
    if not path.exists():
        if state.get('seeded'):
            fail('Missing server secret; restore all secrets from backup instead of regenerating')
        state_store.write_text(path, secrets.token_hex(32) + '\n')
        path.chmod(0o600)
    value = path.read_text().strip()
    if not re.fullmatch('[a-f0-9]{64}', value):
        fail('Invalid server secret file; restore it from backup')
    return value


def dump(name, value):
    state_store.write_text(
        config / name, yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    )


seed_names = [
    'db_synapse',
    'db_mas',
    'db_admin',
    'mas_shared',
    'rtc_as_token',
    'rtc_hs_token',
    'livekit_secret',
    'synapse_macaroon',
    'synapse_form',
    'mas_encryption',
]
for name in seed_names:
    secret(name)
referral_client, referral_policy = referral.configure(root, state, settings, preview=args.preview)
if not args.preview:
    signing = config / 'mas-signing.pem'
    if not signing.exists():
        if state.get('seeded'):
            fail('Missing MAS signing key; restore it from backup')
        try:
            state_store.create_rsa_key(signing)
        except RuntimeError as error:
            fail(str(error))
        signing.chmod(0o600)
    admin = secret_dir / 'admin-access.json'
    if not admin.exists():
        if state.get('seeded'):
            fail('Missing administrator credentials; restore them from backup')
        state_store.write_json(
            admin,
            {
                'username': 'matrix-admin',
                'matrix_id': f'@matrix-admin:{domain}',
                'password': secrets.token_urlsafe(36),
                'admin_url': f'https://admin.{domain}',
                'web_url': f'https://web.{domain}',
                'homeserver': domain,
            },
        )
        admin.chmod(0o600)
state['seeded'] = True
save_state()
if args.initialize_env:
    print('Initialized user configuration and private server credentials; contents omitted.')
    raise SystemExit(0)
if not args.preview:
    try:
        state_store.require_root('Production generation requires root')
    except RuntimeError as error:
        fail(str(error))
public_ip = settings.get('PUBLIC_IP')
if not public_ip:
    if args.preview:
        public_ip = '203.0.113.10'
    else:
        try:
            addresses = sorted(
                {record[4][0] for record in socket.getaddrinfo(domain, 443, socket.AF_INET)}
            )
            if len(addresses) != 1:
                raise ValueError()
            public_ip = addresses[0]
        except (ValueError, OSError):
            fail(
                'Set PUBLIC_IP in '
                + str(preflight.settings_path(root))
                + ' or create a single DOMAIN A record; retry misbot config generate'
            )
try:
    address = ipaddress.ip_address(public_ip)
    if address.version != 4 or (not args.preview and not address.is_global):
        raise ValueError()
except ValueError:
    fail('PUBLIC_IP must be a public IPv4 address')
acme_email = settings.get('ACME_EMAIL', '')
if acme_email and not re.fullmatch(r'[^\s@{}]+@[^\s@{}]+\.[^\s@{}]+', acme_email):
    fail('Invalid ACME_EMAIL')
db_synapse, db_mas = secret('db_synapse'), secret('db_mas')
secret('db_admin')
mas_shared = secret('mas_shared')
as_token, hs_token = secret('rtc_as_token'), secret('rtc_hs_token')
lk_key, lk_secret = 'matrix-livekit', secret('livekit_secret')

synapse = {
    'server_name': f'{domain}',
    'public_baseurl': f'https://matrix.{domain}/',
    'federation_domain_whitelist': federation_servers,
    'pid_file': '/data/homeserver.pid',
    'report_stats': False,
    'listeners': [
        {
            'port': 8008,
            'type': 'http',
            'tls': False,
            'x_forwarded': True,
            'bind_addresses': ['0.0.0.0'],
            'resources': [{'names': ['client', 'federation'], 'compress': False}],
        }
    ],
    'database': {
        'name': 'psycopg2',
        'args': {
            'user': 'synapse',
            'password': db_synapse,
            'database': 'synapse',
            'host': database_host if database_mode == 'external' else 'postgres',
            'port': int(database_port) if database_mode == 'external' else 5432,
            'cp_min': 1,
            'cp_max': 3,
        },
    },
    'log_config': '/config/synapse-log.yaml',
    'media_store_path': '/data/media_store',
    'signing_key_path': f'/data/{domain}.signing.key',
    'macaroon_secret_key': secret('synapse_macaroon'),
    'form_secret': secret('synapse_form'),
    'matrix_authentication_service': {
        'enabled': True,
        'endpoint': 'http://mas:8080/',
        'secret': mas_shared,
    },
    'encryption_enabled_by_default_for_room_type': 'all',
    'room_list_publication_rules': [
        {'user_id': f'@*:{domain}', 'alias': f'#*:{domain}', 'action': 'allow'}
    ],
    'max_upload_size': '100M',
    'enable_registration': False,
    'url_preview_enabled': False,
    'enable_metrics': False,
    'presence': {'enabled': False},
    'caches': {'global_factor': 0.35},
    'app_service_config_files': ['/config/rtc-appservice.yaml'],
    'max_event_delay_duration': '24h',
    'rc_delayed_event_mgmt': {'per_second': 1, 'burst_count': 20},
    'experimental_features': {
        'msc4143_enabled': True,
        'msc4222_enabled': True,
        'msc4502_enabled': True,
        'msc4512_enabled': True,
        'msc4108_enabled': True,
    },
    'matrix_rtc': {
        'transports': [
            {
                'type': 'livekit',
                'url': f'wss://rtc.{domain}/livekit/sfu',
                'livekit_service_url': f'https://rtc.{domain}/livekit/jwt',
            }
        ]
    },
}
if database_mode == 'external':
    synapse['database']['args']['sslmode'] = database_ssl
    synapse['database']['args']['connect_timeout'] = 10
    if database_ssl == 'verify-full':
        ca_source = Path(
            settings.get('POSTGRES_SSL_ROOT_CERT') or '/etc/ssl/certs/ca-certificates.crt'
        )
        if not ca_source.is_absolute() or not ca_source.is_file():
            fail('POSTGRES_SSL_ROOT_CERT must name an existing CA PEM file on this VPS')
        import shutil

        shutil.copyfile(ca_source, config / 'postgres-ca.pem')
        (config / 'postgres-ca.pem').chmod(0o644)
        synapse['database']['args']['sslrootcert'] = '/config/postgres-ca.pem'
if media_mode == 's3':
    # Keep the complete local copy until a separately verified migration/backup gate.
    synapse['enable_local_media_storage'] = True
    synapse['media_storage_providers'] = [
        {
            'module': 's3_storage_provider.S3StorageProviderBackend',
            'store_local': True,
            'store_remote': True,
            'store_synchronous': True,
            'config': {
                'bucket': settings.get('S3_BUCKET', 'REDACTED_BUCKET'),
                'region_name': settings.get('S3_REGION', 'REDACTED_REGION'),
                'access_key_id': 'REDACTED_ACCESS_KEY'
                if args.preview
                else settings['S3_ACCESS_KEY_ID'],
                'secret_access_key': 'REDACTED_SECRET'
                if args.preview
                else settings['S3_SECRET_ACCESS_KEY'],
                'prefix': settings.get('S3_PREFIX', 'preview/'),
                **(
                    {'endpoint_url': settings['S3_ENDPOINT_URL']}
                    if settings.get('S3_ENDPOINT_URL')
                    else {}
                ),
            },
        }
    ]
dump('homeserver.yaml', synapse)
dump(
    'synapse-log.yaml',
    {
        'version': 1,
        'formatters': {'standard': {'format': '%(asctime)s %(name)s %(levelname)s %(message)s'}},
        'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'standard'}},
        'loggers': {'synapse.storage.SQL': {'level': 'WARNING'}},
        'root': {'level': 'INFO', 'handlers': ['console']},
        'disable_existing_loggers': False,
    },
)
dump(
    'rtc-appservice.yaml',
    {
        'id': 'matrix-livekit',
        'as_token': as_token,
        'hs_token': hs_token,
        'sender_localpart': '_lk_jwt_service',
        'namespaces': {'users': [{'exclusive': False, 'regex': '.*'}]},
        'url': None,
        'scopes': ['urn:matrix:client:rooms:is_joined'],
        'io.element.msc4502.scope': ['urn:matrix:client:io.element.msc4502:rooms:is_joined'],
        'proxy_prefix': 'rtc/livekit',
        'proxy_url': 'http://jwt:8080',
        'rate_limited': False,
    },
)

if not args.preview:
    if not (config / 'mas-signing.pem').exists():
        fail(
            'MAS signing key disappeared during generation; keep existing data and restore its saved key'
        )
    os.chown(config / 'mas-signing.pem', 65532, 65532)

mas = {
    'http': {
        'public_base': f'https://{mas_domain}/',
        'issuer': f'https://{mas_domain}/',
        'listeners': [
            {
                'name': 'web',
                'resources': [
                    {'name': r}
                    for r in [
                        'discovery',
                        'human',
                        'oauth',
                        'graphql',
                        'assets',
                        'adminapi',
                        'health',
                    ]
                ],
                'binds': [{'host': '0.0.0.0', 'port': 8080}],
            }
        ],
    },
    'database': {
        'host': database_host if database_mode == 'external' else 'postgres',
        'port': int(database_port) if database_mode == 'external' else 5432,
        'username': 'mas',
        'password': db_mas,
        'database': 'mas',
        'ssl_mode': database_ssl if database_mode == 'external' else 'disable',
        'min_connections': 1,
        'max_connections': 3,
    },
    'matrix': {
        'kind': 'synapse',
        'homeserver': f'{domain}',
        'endpoint': 'http://synapse:8008',
        'secret': mas_shared,
    },
    'secrets': {
        'encryption': secret('mas_encryption'),
        'keys': [{'key_file': '/config/mas-signing.pem'}],
    },
    'passwords': {'enabled': True},
    'account': {
        'password_registration_enabled': True,
        'password_registration_email_required': False,
        'password_recovery_enabled': False,
    },
    'branding': {'service_name': 'Matrix'},
    'email': {'from': f'Matrix <noreply@{domain}>', 'transport': 'blackhole'},
    'telemetry': {'tracing': {'exporter': 'none'}, 'metrics': {'exporter': 'none'}},
    'policy': {'data': {'client_registration': {'allow_host_mismatch': True}}},
}
if database_mode == 'external':
    mas['database']['connect_timeout'] = 10
    if database_ssl == 'verify-full':
        mas['database']['ssl_ca_file'] = '/config/postgres-ca.pem'
mas['clients'] = [
    {
        'client_id': referral_client['client_id'],
        'client_auth_method': 'client_secret_basic',
        'client_secret': referral_client['client_secret'],
    }
]
mas['policy']['data']['admin_clients'] = [referral_client['client_id']]
google = (
    {'GOOGLE_CLIENT_ID': 'REDACTED_CLIENT_ID', 'GOOGLE_CLIENT_SECRET': 'REDACTED_CLIENT_SECRET'}
    if args.preview and settings.get('GOOGLE_CLIENT_ID')
    else settings
)
providers = []
if google.get('GOOGLE_CLIENT_ID') and google.get('GOOGLE_CLIENT_SECRET'):
    providers.append(
        {
            'id': '01K5WZ00000000000000000001',
            'human_name': 'Google',
            'brand_name': 'google',
            'issuer': 'https://accounts.google.com',
            'token_endpoint_auth_method': 'client_secret_post',
            'client_id': google['GOOGLE_CLIENT_ID'],
            'client_secret': google['GOOGLE_CLIENT_SECRET'],
            'scope': 'openid profile email',
            'claims_imports': {
                'localpart': {'action': 'ignore'},
                'displayname': {'action': 'suggest', 'template': '{{ user.name }}'},
                'email': {'action': 'suggest', 'template': '{{ user.email }}'},
                'account_name': {'template': '{{ user.email }}'},
            },
        }
    )
telegram = (
    {'TELEGRAM_CLIENT_ID': 'REDACTED_CLIENT_ID', 'TELEGRAM_CLIENT_SECRET': 'REDACTED_CLIENT_SECRET'}
    if args.preview and settings.get('TELEGRAM_CLIENT_ID')
    else settings
)
if telegram.get('TELEGRAM_CLIENT_ID') and telegram.get('TELEGRAM_CLIENT_SECRET'):
    providers.append(
        {
            'id': '01K5WZ00000000000000000002',
            'human_name': 'Telegram',
            'enabled': True,
            'issuer': 'https://oauth.telegram.org',
            'token_endpoint_auth_method': 'client_secret_basic',
            # Telegram gzips the bare JWKS URI without content negotiation; MAS lacks gzip decoding.
            # This query returns the same live keys as plain JSON over the official HTTPS endpoint.
            'jwks_uri': 'https://oauth.telegram.org/.well-known/jwks.json?format=json',
            'client_id': telegram['TELEGRAM_CLIENT_ID'],
            'client_secret': telegram['TELEGRAM_CLIENT_SECRET'],
            'pkce_method': 'always',
            'fetch_userinfo': False,
            'scope': 'openid profile',
            'claims_imports': {
                'localpart': {'action': 'ignore', 'on_conflict': 'fail'},
                'displayname': {'action': 'suggest', 'template': '{{ user.name }}'},
            },
        }
    )
if providers:
    mas['upstream_oauth2'] = {'providers': providers}
proxy = (
    {'SOCKS5_CONFIG': 'socks5h://REDACTED_USER:REDACTED_PASSWORD@proxy.invalid:1080'}
    if args.preview and settings.get('SOCKS5_CONFIG')
    else settings
)
has_proxy = bool(proxy.get('SOCKS5_CONFIG'))
if has_proxy:
    proxy_url = proxy['SOCKS5_CONFIG']
    parsed_proxy = urlsplit(proxy_url)
    if (
        parsed_proxy.scheme not in ('socks5', 'socks5h')
        or not parsed_proxy.hostname
        or not parsed_proxy.port
    ):
        raise ValueError('SOCKS5_CONFIG must specify a SOCKS5 host and port')
    proxy_url = 'socks5h://' + proxy_url.split('://', 1)[1]
    if '\n' in proxy_url or '\r' in proxy_url:
        raise ValueError('SOCKS5_CONFIG must be a single line')
    bypass = f'localhost,127.0.0.1,::1,synapse,mas,postgres,jwt,livekit,caddy,{domain},.{domain},{mas_domain},google.com,.google.com,googleapis.com,.googleapis.com,googleusercontent.com,.googleusercontent.com'
    proxy_file = root / 'mas-proxy.env'
    state_store.write_text(proxy_file, f'HTTPS_PROXY={proxy_url}\nNO_PROXY={bypass}\n')
    proxy_file.chmod(0o600)
elif (root / 'mas-proxy.env').exists():
    (root / 'mas-proxy.env').unlink()
if not args.preview:
    defaults = config / 'mas-defaults.yaml'
    if not defaults.exists():
        if state.get('defaults_created'):
            fail('Missing MAS password defaults; restore them from backup')
        try:
            generated_defaults = subprocess.check_output(
                [
                    'docker',
                    'run',
                    '--rm',
                    '--network',
                    'none',
                    resolved['mas'],
                    'config',
                    'generate',
                ],
                text=True,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **state_store.child_process_options(),
            )
        except subprocess.SubprocessError:
            fail(
                'MAS defaults generation failed. Check Docker, the locked MAS image and available resources, keep existing files, then retry generate/up'
            )
        state_store.write_text(defaults, generated_defaults)
        defaults.chmod(0o600)
    state['defaults_created'] = True
    save_state()
    try:
        base = yaml.safe_load(defaults.read_text())
    except yaml.YAMLError:
        fail(
            'MAS defaults YAML is invalid. Restore config/mas-defaults.yaml from this installation backup, then retry generate/up; credentials are not regenerated'
        )
    if not isinstance(base, dict):
        fail(
            'MAS defaults must contain a YAML mapping; restore its saved file, then retry generate/up'
        )
    base.pop('database', None)
    mas = merge(base, mas)
mas['account']['password_registration_enabled'] = registration == 'true'
mas['account']['password_registration_token_required'] = referral_policy['registration_required']
for provider in mas.get('upstream_oauth2', {}).get('providers', []):
    provider['registration_token_required'] = referral_policy['registration_required']
dump('mas.yaml', mas)

dump(
    'livekit.yaml',
    {
        'port': 7880,
        'rtc': {'tcp_port': 7881, 'udp_port': 7882, 'use_external_ip': False, 'node_ip': public_ip},
        'keys': {lk_key: lk_secret},
        'turn': {
            'enabled': True,
            'domain': f'turn.{domain}',
            'udp_port': 3478,
            'tls_port': 443,
            'proxy_protocol': True,
            'proxy_protocol_trusted_cidrs': [subnet],
            'cert_file': f'/certs/caddy/certificates/acme-v02.api.letsencrypt.org-directory/turn.{domain}/turn.{domain}.crt',
            'key_file': f'/certs/caddy/certificates/acme-v02.api.letsencrypt.org-directory/turn.{domain}/turn.{domain}.key',
        },
        'webhook': {'api_key': lk_key, 'urls': ['http://jwt:8080/sfu_webhook']},
        'logging': {'level': 'info'},
    },
)

(config / 'init-db.sql').write_text(
    postgres_setup.PRIVATE_ROLE_LOGGING_SQL
    + f"CREATE ROLE synapse LOGIN PASSWORD '{db_synapse}';\n"
    "CREATE DATABASE synapse OWNER synapse ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;\n"
    f"CREATE ROLE mas LOGIN PASSWORD '{db_mas}';\n"
    "CREATE DATABASE mas OWNER mas ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;\n"
)

discovery = {
    'm.homeserver': {'base_url': f'https://matrix.{domain}'},
    'org.matrix.msc2965.authentication': {
        'issuer': f'https://{mas_domain}/',
        'account': f'https://{mas_domain}/account/',
    },
    'org.matrix.msc4143.rtc_foci': [
        {'type': 'livekit', 'livekit_service_url': f'https://rtc.{domain}/livekit/jwt'}
    ],
}
(public / 'well-known-client.json').write_text(json.dumps(discovery))
(public / 'well-known-server.json').write_text(json.dumps({'m.server': f'matrix.{domain}:443'}))
web = {
    'default_server_config': {
        'm.homeserver': {'base_url': f'https://matrix.{domain}', 'server_name': f'{domain}'}
    },
    'default_server_name': f'{domain}',
    'disable_custom_urls': True,
    'disable_guests': True,
    'brand': 'Matrix',
    'default_country_code': 'RU',
    'default_language': 'ru',
    'integrations_ui_url': '',
    'integrations_rest_url': '',
    'integrations_widgets_urls': [],
    'element_call': {'use_exclusively': True, 'disable': False, 'brand': 'Matrix Call'},
    'setting_defaults': {'breadcrumbs': True},
    'room_directory': {'servers': list(dict.fromkeys([domain, *federation_servers]))},
}
(public / 'web-config.json').write_text(json.dumps(web, indent=2))

mas_site = (
    ''
    if mas_domain in (domain, 'matrix.' + domain)
    else mas_domain + ' {\n    reverse_proxy mas:8080\n}\n'
)
matrix_routes = (
    '    @matrix_api path /_matrix/* /_synapse/*\n    handle @matrix_api {\n        reverse_proxy synapse:8008\n    }\n'
    if mas_domain == 'matrix.' + domain
    else ''
)
(config / 'Caddyfile').write_text(
    """{
    acme_ca https://acme-v02.api.letsencrypt.org/directory
    cert_issuer acme {
        disable_tlsalpn_challenge
    }
    servers :443 {
        listener_wrappers {
            proxy_protocol {
                timeout 5s
                allow @SUBNET@
            }
            tls
        }
    }
}
(discovery) {
    handle /.well-known/matrix/client {
        header Access-Control-Allow-Origin *
        header Content-Type application/json
        header Cache-Control "public, max-age=300"
        root * /srv/discovery
        rewrite * /well-known-client.json
        file_server
    }
    handle /.well-known/matrix/server {
        header Content-Type application/json
        root * /srv/discovery
        rewrite * /well-known-server.json
        file_server
    }
}
@DOMAIN@ {
    import discovery
    handle /.well-known/misbot {
        reverse_proxy unix//run/misbot/http.sock
    }
    handle /_misbot/v1/* {
        request_body {
            max_size 32KB
        }
        reverse_proxy unix//run/misbot/http.sock
    }
    handle {
        redir https://web.@DOMAIN@{uri} 302
    }
}
matrix.@DOMAIN@ {
    import discovery
    @legacy path_regexp legacy ^/_matrix/client/[^/]+/(login|logout|refresh)$
    handle @legacy {
        header Access-Control-Allow-Origin *
        header Content-Type application/json
        respond `{ "errcode": "M_UNRECOGNIZED", "error": "Native OAuth authentication required" }` 404
    }
@MATRIX_ROUTES@    handle {
        reverse_proxy @MATRIX_UPSTREAM@
    }
}
@MAS_SITE@
web.@DOMAIN@ {
    encode zstd gzip
    reverse_proxy web:8080
}
admin.@DOMAIN@ {
    encode zstd gzip
    reverse_proxy admin:8080
}
rtc.@DOMAIN@ {
    handle_path /livekit/jwt/* {
        reverse_proxy jwt:8080
    }
    handle_path /livekit/sfu* {
        reverse_proxy livekit:7880
    }
    handle {
        respond "MatrixRTC" 200
    }
}
turn.@DOMAIN@ {
    respond "TURN endpoint" 200
}
""".replace('@MATRIX_ROUTES@', matrix_routes)
    .replace(
        '@MATRIX_UPSTREAM@', 'mas:8080' if mas_domain == 'matrix.' + domain else 'synapse:8008'
    )
    .replace('@MAS_SITE@', mas_site)
    .replace('@DOMAIN@', domain)
    .replace('@SUBNET@', subnet)
)
homepage.write_assets(root, settings)
state_store.write_text(
    config / 'Caddyfile', homepage.replace_route((config / 'Caddyfile').read_text(), settings)
)
(config / 'haproxy.cfg').write_text(
    """global
    log stdout format raw local0
    maxconn 256
defaults
    log global
    mode tcp
    timeout connect 5s
    timeout client 2h
    timeout server 2h
frontend tls
    bind *:443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req_ssl_hello_type 1 }
    use_backend turn if { req_ssl_sni -i turn.@DOMAIN@ }
    default_backend https
backend https
    server caddy caddy:443 send-proxy-v2 resolvers docker init-addr libc,none
backend turn
    server livekit livekit:443 send-proxy-v2 resolvers docker init-addr libc,none
resolvers docker
    nameserver dns 127.0.0.11:53
    resolve_retries 3
    timeout resolve 1s
    timeout retry 1s
    hold valid 10s
    hold nx 3s
""".replace('@DOMAIN@', domain).replace('@SUBNET@', subnet)
)


def service(role, memory, **kwargs):
    return {
        'image': resolved[role],
        'restart': 'unless-stopped',
        'mem_limit': memory,
        'memswap_limit': str(int(memory[:-1]) * 2) + 'm',
        'pids_limit': 128,
        'logging': {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}},
        **kwargs,
    }


services = {
    'postgres': service(
        'postgres',
        '128m',
        environment={
            'POSTGRES_PASSWORD_FILE': '/run/secrets/db_admin',
            'POSTGRES_INITDB_ARGS': '--encoding=UTF8 --locale=C',
        },
        secrets=['db_admin'],
        command=[
            'postgres',
            '-c',
            'shared_buffers=32MB',
            '-c',
            'max_connections=30',
            '-c',
            'work_mem=2MB',
            '-c',
            'maintenance_work_mem=16MB',
            '-c',
            'effective_cache_size=128MB',
        ],
        volumes=[
            './data/postgres:/var/lib/postgresql',
            './config/init-db.sql:/docker-entrypoint-initdb.d/10-matrix.sql:ro',
        ],
        healthcheck={
            'test': ['CMD-SHELL', 'pg_isready -U postgres'],
            'interval': '10s',
            'timeout': '5s',
            'retries': 12,
        },
    ),
    'synapse': service(
        'synapse',
        '320m',
        user='991:991',
        environment={'SYNAPSE_CONFIG_PATH': '/config/homeserver.yaml', 'MALLOC_ARENA_MAX': '2'},
        volumes=[
            './config/homeserver.yaml:/config/homeserver.yaml:ro',
            './config/synapse-log.yaml:/config/synapse-log.yaml:ro',
            './config/rtc-appservice.yaml:/config/rtc-appservice.yaml:ro',
            './data/synapse:/data',
        ],
        depends_on={'postgres': {'condition': 'service_healthy'}},
    ),
    'mas': service(
        'mas',
        '144m',
        command=['server', '--config', '/config/mas.yaml'],
        volumes=[
            './config/mas.yaml:/config/mas.yaml:ro',
            './config/mas-signing.pem:/config/mas-signing.pem:ro',
        ],
        depends_on={'postgres': {'condition': 'service_healthy'}},
    ),
    'jwt': service(
        'jwt',
        '64m',
        environment={
            'LIVEKIT_URL': f'wss://rtc.{domain}/livekit/sfu',
            'LIVEKIT_KEY': lk_key,
            'LIVEKIT_SECRET_FROM_FILE': '/run/secrets/livekit_secret',
            'LIVEKIT_FULL_ACCESS_HOMESERVERS': f'{domain}',
            'LIVEKIT_HS_SERVER_NAME': f'{domain}',
            'LIVEKIT_CS_API_URL_OVERRIDES': f'{domain}=http://synapse:8008',
            'LIVEKIT_AS_REGISTRATION_FILE': '/config/rtc-appservice.yaml',
        },
        secrets=['livekit_secret'],
        volumes=['./config/rtc-appservice.yaml:/config/rtc-appservice.yaml:ro'],
    ),
    'livekit': service(
        'livekit',
        '160m',
        command=['--config', '/config/livekit.yaml'],
        volumes=['./config/livekit.yaml:/config/livekit.yaml:ro', './data/caddy:/certs:ro'],
        ports=['7881:7881/tcp', '7882:7882/udp', '3478:3478/udp'],
    ),
    'web': service(
        'web',
        '48m',
        environment={'ELEMENT_WEB_PORT': '8080'},
        volumes=['./public/web-config.json:/app/config.json:ro'],
    ),
    'admin': service('admin', '48m', environment={'SERVER_NAME': f'{domain}'}),
    'caddy': service(
        'caddy',
        '80m',
        ports=['80:80'],
        volumes=[
            './config/Caddyfile:/etc/caddy/Caddyfile:ro',
            './public:/srv/discovery:ro',
            './data/caddy:/data',
            './data/caddy-config:/config',
            './run/federation:/run/misbot:ro',
        ],
    ),
    'tls-router': service(
        'tls-router',
        '48m',
        ports=['443:443'],
        volumes=['./config/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro'],
        depends_on=['caddy'],
    ),
}
if database_mode == 'external':
    del services['postgres']
    for role in ('synapse', 'mas'):
        services[role].pop('depends_on', None)
    admin_user = settings.get('POSTGRES_ADMIN_USER', 'postgres')
    admin_password = 'REDACTED_PASSWORD' if args.preview else settings['POSTGRES_ADMIN_PASSWORD']
    pgpass = config / 'postgres-admin.pgpass'
    escaped_password = admin_password.replace('\\', '\\\\').replace(':', '\\:')
    pgpass.write_text(f'{database_host}:{database_port}:*:{admin_user}:{escaped_password}\n')
    pgpass.chmod(0o600)
    services['postgres-client'] = {
        **service('postgres', '64m', profiles=['tools']),
        'environment': {
            'PGHOST': database_host,
            'PGPORT': database_port,
            'PGUSER': admin_user,
            'PGSSLMODE': database_ssl,
            'PGPASSFILE': '/config/postgres-admin.pgpass',
            'PGCONNECT_TIMEOUT': '10',
            **(
                {'PGSSLROOTCERT': '/config/postgres-ca.pem'}
                if database_ssl == 'verify-full'
                else {}
            ),
        },
        'volumes': [
            './config/postgres-admin.pgpass:/config/postgres-admin.pgpass:ro',
            './config/init-db.sql:/config/init-db.sql:ro',
        ],
    }
    if database_ssl == 'verify-full':
        services['postgres-client']['volumes'].append(
            './config/postgres-ca.pem:/config/postgres-ca.pem:ro'
        )
        for role in ('synapse', 'mas'):
            services[role]['volumes'].append('./config/postgres-ca.pem:/config/postgres-ca.pem:ro')
    if not args.preview:
        os.chown(pgpass, 999, 999)
elif (config / 'postgres-admin.pgpass').exists():
    (config / 'postgres-admin.pgpass').unlink()
if media_mode == 's3':
    services['synapse']['image'] = 'matrix-stack/synapse-s3:1.7.0'
compose = {
    'name': 'matrix-stack',
    'services': services,
    'networks': {
        'default': {
            'ipam': {'config': [{'subnet': subnet}]},
            'labels': {'org.misbot.matrix.installation.directory': str(root)},
        }
    },
    'secrets': {name: {'file': f'./secrets/{name}'} for name in ['db_admin', 'livekit_secret']},
}
services.update(referral.services())
import federation_runtime

federation_runtime.configure(root, state, args.preview)
services['federation-bot'] = federation_runtime.service()
if has_proxy:
    services['mas']['env_file'] = [{'path': './mas-proxy.env', 'format': 'raw'}]
(root / 'compose.yaml').write_text(yaml.safe_dump(compose, sort_keys=False))
if not args.preview:
    for name in (['postgres'] if database_mode == 'local' else []) + [
        'synapse',
        'caddy',
        'caddy-config',
    ]:
        (root / 'data' / name).mkdir(parents=True, exist_ok=True)
    os.chown(root / 'data/synapse', 991, 991)
    # PostgreSQL 18 creates PGDATA below this mount (18/docker); its parent
    # must remain traversable after the entrypoint drops root privileges.
    if database_mode == 'local':
        os.chown(root / 'data/postgres', 999, 999)
        (root / 'data/postgres').chmod(0o700)
        os.chown(secret_dir / 'db_admin', 999, 999)
        (secret_dir / 'db_admin').chmod(0o600)
    os.chown(config / 'init-db.sql', 999, 999)
    (config / 'init-db.sql').chmod(0o600)
    for name in ['homeserver.yaml', 'synapse-log.yaml', 'rtc-appservice.yaml']:
        os.chown(config / name, 991, 991)
        (config / name).chmod(0o600)
    os.chown(config / 'mas.yaml', 65532, 65532)
    (config / 'mas.yaml').chmod(0o600)
    for name in ['Caddyfile', 'haproxy.cfg']:
        (config / name).chmod(0o644)
    for path in public.iterdir():
        path.chmod(0o755 if path.is_dir() else 0o644)
    (root / 'data/synapse').chmod(0o700)
# The Caddy validator consumes the same Caddyfile path as the service.
if acme_email:
    path = config / 'Caddyfile'
    path.write_text(path.read_text().replace('{\n', '{\n    email ' + acme_email + '\n', 1))
state['public_ip'] = public_ip
save_state()
print(
    'Configuration generated. Secrets omitted from output; OAuth providers:',
    len(providers),
    'SOCKS proxy configured:',
    has_proxy,
)
