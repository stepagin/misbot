"""One field model for files, CLI, wizard and runtime generation."""

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit, quote
import s3_config


class InvalidConfiguration(RuntimeError):
    def __init__(self, file, field, code, message):
        self.file = str(file)
        self.field = field
        self.code = code
        super().__init__(f'{self.file}: {field}: {message}. Повтор: sudo misbot config validate')

    def result(self):
        return {
            'stage': 'configuration',
            'status': 'failed',
            'code': self.code,
            'file': self.file,
            'field': self.field,
            'explanation': str(self),
            'next_actions': ['sudo misbot config validate'],
        }


@dataclass(frozen=True)
class Field:
    area: str
    label: str
    default: str = ''
    kind: str = 'text'
    secret: bool = False
    required: str = 'необязательно'
    example: str = ''
    impact: str = 'Применяется при generate/start; работающий сервер требует копии.'


FIELDS = {
    'HOMEPAGE_MODE': Field(
        'site',
        'Главная: landing, element или external',
        'landing',
        'homepage',
        example='landing',
        impact='Только главная страница и Caddy; identity и службы Matrix сохраняются.',
    ),
    'HOMEPAGE_URL': Field(
        'site',
        'Цель external: полный HTTPS URL без credentials',
        kind='https',
        example='https://community.example.net/',
        required='external',
    ),
    'SITE_NAME': Field(
        'site', 'Название сервера; пусто — домен', kind='short_text', example='Наш сервер'
    ),
    'SITE_DESCRIPTION': Field(
        'site',
        'Краткое описание сервера',
        'Общайтесь в своём пространстве Matrix.',
        'description',
        example='Общение нашей команды',
    ),
    'SITE_SUPPORT_URL': Field(
        'site',
        'Необязательная HTTPS ссылка поддержки',
        kind='https',
        example='https://support.example.net/',
    ),
    'DOMAIN': Field(
        'server',
        'Основной домен, без https:// и пути',
        kind='domain',
        required='всегда',
        example='example.org',
        impact='Matrix identity: после init изменять нельзя.',
    ),
    'MAS_DOMAIN': Field(
        'server',
        'Домен MAS; пусто — mas.<DOMAIN>',
        kind='domain',
        example='auth.example.net',
        impact='OAuth issuer: после запуска баз изменять нельзя.',
    ),
    'PUBLIC_IP': Field(
        'server',
        'Публичный IPv4 из панели VPS; пусто — единственный A домена',
        kind='ip',
        example='203.0.113.20',
    ),
    'ACME_EMAIL': Field(
        'server', 'Email оператора для сертификатов', kind='email', example='operator@example.org'
    ),
    'MEDIA_MODE': Field(
        'server',
        'Медиа: local или s3',
        'local',
        'media',
        example='local',
        impact='Действующее хранилище меняется только storage operation.',
    ),
    'POSTGRES_MODE': Field(
        'server',
        'PostgreSQL: local или external',
        'local',
        'database',
        example='local',
        impact='Действующее хранилище меняется только storage operation.',
    ),
    'DOCKER_SUBNET': Field(
        'policy',
        'Приватная свободная IPv4 сеть /24',
        '172.30.50.0/24',
        'subnet',
        example='172.30.50.0/24',
        impact='После первого запуска не изменять.',
    ),
    'FEDERATION_SERVERS': Field(
        'policy',
        'Разрешённые Matrix server_name через запятую',
        'misbot.ru',
        'domains',
        example='matrix.org,example.net',
    ),
    'FEDERATION_AUTO_ACCEPT': Field(
        'policy',
        'Принимать комнаты только от согласованного bot MXID одобренного peer',
        'false',
        'bool',
        example='false',
        impact='Не изменяет allowlist; по умолчанию требуется местный администратор.',
    ),
    'OPEN_REGISTRATION': Field(
        'policy', 'Разрешить новые парольные регистрации', 'true', 'bool', example='true'
    ),
    'REGISTRATION_TOKEN_REQUIRED': Field(
        'policy', 'Требовать приглашение при парольной регистрации', 'true', 'bool', example='true'
    ),
    'INVITATION_LIFETIME_SECONDS': Field(
        'policy',
        'Срок приглашений и первого токена, секунды (7 дней)',
        '604800',
        'lifetime',
        example='604800',
        impact='Новые приглашения; сохранённый токен не продлевается.',
    ),
    'INVITATION_USAGE_LIMIT': Field(
        'policy', 'Лимит приглашений бота; первый токен всегда на одного', '1', 'usage', example='1'
    ),
    'GOOGLE_CLIENT_ID': Field(
        'auth',
        'Client ID из Google Cloud',
        example='FICTIONAL_GOOGLE_ID',
        required='вместе с secret',
    ),
    'GOOGLE_CLIENT_SECRET': Field(
        'auth',
        'Client secret из Google Cloud',
        secret=True,
        example='FICTIONAL_GOOGLE_SECRET',
        required='вместе с ID',
        impact='В активной установке форма не ротирует OAuth credentials.',
    ),
    'TELEGRAM_CLIENT_ID': Field(
        'auth',
        'Client ID приложения Telegram OIDC',
        example='FICTIONAL_TELEGRAM_ID',
        required='вместе с secret',
    ),
    'TELEGRAM_CLIENT_SECRET': Field(
        'auth',
        'Client secret Telegram OIDC',
        secret=True,
        example='FICTIONAL_TELEGRAM_SECRET',
        required='вместе с ID',
        impact='В активной установке форма не ротирует OAuth credentials.',
    ),
    'SOCKS5_CONFIG': Field(
        'auth',
        'Необязательный HTTPS-egress Telegram через SOCKS5',
        kind='proxy',
        secret=True,
        example='socks5://fictional:password@proxy.example.org:1080',
    ),
    'POSTGRES_HOST': Field(
        'database',
        'Хост выделенной PostgreSQL',
        kind='host',
        required='external',
        example='db.example.org',
    ),
    'POSTGRES_PORT': Field('database', 'Порт PostgreSQL', '5432', 'port', example='5432'),
    'POSTGRES_SSL_MODE': Field(
        'database', 'TLS PostgreSQL', 'require', 'ssl', example='verify-full'
    ),
    'POSTGRES_SSL_ROOT_CERT': Field(
        'database', 'Путь CA PEM; пусто — системный CA', kind='path', example='/root/db-ca.pem'
    ),
    'POSTGRES_ADMIN_USER': Field(
        'database',
        'Административная роль выделенного кластера',
        'postgres',
        'role',
        example='postgres',
    ),
    'POSTGRES_ADMIN_PASSWORD': Field(
        'database',
        'Пароль роли провайдера PostgreSQL',
        secret=True,
        required='external',
        example='FICTIONAL_DATABASE_PASSWORD',
        impact='Через поддерживаемый backend; не менять обычной формой.',
    ),
}
for key, label, secret, example in (
    ('S3_BUCKET', 'Имя существующего bucket', False, 'fictional-private-bucket'),
    ('S3_REGION', 'Region подписи SigV4 из документации провайдера', False, 'us-east-1'),
    ('S3_ENDPOINT_URL', 'HTTPS origin провайдера; пусто — AWS', False, 'https://s3.example.org'),
    ('S3_PREFIX', 'Отдельный prefix с / в конце', False, 'matrix/example/media/'),
    ('S3_ACCESS_KEY_ID', 'Access key ID провайдера', True, 'FICTIONAL_ACCESS'),
    ('S3_SECRET_ACCESS_KEY', 'Secret access key провайдера', True, 'FICTIONAL_SECRET'),
):
    FIELDS[key] = Field(
        'media-s3',
        label,
        secret=secret,
        required='s3, кроме необязательного endpoint',
        example=example,
        impact='Назначение/credentials меняются отдельными storage/credentials операциями.',
    )
BACKUP_FIELDS = {
    key: Field(
        'backup',
        value.label,
        value.default,
        value.kind,
        value.secret,
        value.required,
        value.example,
        value.impact,
    )
    for key, value in FIELDS.items()
    if key in s3_config.FIELDS
}
BACKUP_FIELDS.update(
    {
        'BACKUP_KEY_FILE': Field(
            'backup',
            'Приватный recovery key вне установки',
            kind='path',
            required='backup включён',
            example='/root/misbot-backup.key',
        ),
        'BACKUP_CALENDAR': Field(
            'backup', 'Расписание systemd, необязательно', example='*-*-* 03:00:00 UTC'
        ),
    }
)


def domain(value):
    try:
        value = value.strip().lower().encode('idna').decode('ascii')
    except (UnicodeError, ValueError):
        raise ValueError('hostname без схемы, порта и пути') from None
    if (
        len(value) > 253
        or '.' not in value
        or not all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in value.split('.')
        )
    ):
        raise ValueError('hostname: example.org')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError('домен, а не IP')


def field_value(key, value, file='форма', schema=FIELDS):
    if key not in schema:
        raise InvalidConfiguration(
            file,
            key,
            'unknown_field',
            'неизвестное поле; значение сохраните приватно, не отбрасывайте',
        )
    item = schema[key]
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvalidConfiguration(
            file, key, 'syntax', 'буквальная строка без управляющих символов'
        )
    try:
        if not value:
            if item.required == 'всегда' or item.kind in (
                'media',
                'database',
                'bool',
                'ssl',
                'port',
                'lifetime',
                'usage',
                'subnet',
                'domains',
                'role',
                'homepage',
            ):
                raise ValueError('обязательное поле')
            return value
        if item.kind == 'domain':
            return domain(value)
        if item.kind == 'domains':
            if any(not p.strip() for p in value.split(',')):
                raise ValueError('непустой список доменов через запятую')
            return ','.join(dict.fromkeys(domain(p) for p in value.split(',')))
        enums = {
            'media': ('local', 's3'),
            'database': ('local', 'external'),
            'bool': ('true', 'false'),
            'ssl': ('require', 'verify-full'),
            'homepage': ('landing', 'element', 'external'),
        }
        if item.kind in enums and value not in enums[item.kind]:
            raise ValueError(' / '.join(enums[item.kind]))
        if item.kind in ('short_text', 'description') and len(value) > (
            120 if item.kind == 'short_text' else 1000
        ):
            raise ValueError('слишком длинный текст')
        if item.kind == 'https':
            parsed = urlsplit(value)
            if (
                parsed.scheme != 'https'
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or any(c.isspace() or c in '\\"<>`{}' for c in value)
                or re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f)', value, re.I)
            ):
                raise ValueError(
                    'HTTPS URL без credentials, управляющих символов и нестандартного порта'
                )
            try:
                address = ipaddress.ip_address(parsed.hostname)
                host = str(address)
                if address.version == 6:
                    host = '[' + host + ']'
            except ValueError:
                host = domain(parsed.hostname)
            return urlunsplit(
                (
                    'https',
                    host,
                    quote(parsed.path, safe="/:%@!$&'()*+,;=-._~"),
                    quote(parsed.query, safe="/:?%@!$&'()*+,;=-._~"),
                    quote(parsed.fragment, safe="/:?%@!$&'()*+,;=-._~"),
                )
            )
        if item.kind == 'ip':
            address = ipaddress.ip_address(value)
            if address.version != 4 or not address.is_global:
                raise ValueError('публичный IPv4')
        if item.kind == 'subnet':
            net = ipaddress.ip_network(value, strict=True)
            if net.version != 4 or net.prefixlen != 24 or not net.is_private:
                raise ValueError('приватная IPv4 сеть /24')
        limits = {'port': 65535, 'lifetime': 315360000, 'usage': 2147483647}
        if item.kind in limits and (
            not re.fullmatch(r'[0-9]{1,10}', value) or not 1 <= int(value) <= limits[item.kind]
        ):
            raise ValueError('целое 1–' + str(limits[item.kind]))
        if item.kind == 'email' and not re.fullmatch(r'[^\s@{}]+@[^\s@{}]+\.[^\s@{}]+', value):
            raise ValueError('email: operator@example.org')
        if item.kind == 'host':
            if len(value) > 253 or not all(
                re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                for part in value.split('.')
            ):
                raise ValueError('hostname/IPv4 без схемы и порта')
        if item.kind == 'role' and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', value):
            raise ValueError('имя роли: postgres')
        if item.kind == 'path' and (
            not Path(value).is_absolute()
            or '..' in Path(value).parts
            or Path(value).resolve() != Path(value)
        ):
            raise ValueError('абсолютный путь без symlink/..')
        if item.kind == 'proxy':
            parsed = urlsplit(value)
            if (
                parsed.scheme != 'socks5'
                or not parsed.hostname
                or not parsed.port
                or parsed.path not in ('', '/')
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError('socks5://[user:password@]host:port')
    except (ValueError, UnicodeError):
        raise InvalidConfiguration(
            file,
            key,
            'field_format',
            'допустимый ввод: '
            + item.label
            + '; пример '
            + ('скрыто' if item.secret else item.example),
        ) from None
    return value


def mas_host(values):
    return values.get('MAS_DOMAIN') or 'mas.' + values['DOMAIN']


def public_hosts(values):
    domain = values['DOMAIN']
    return list(
        dict.fromkeys(
            [
                domain,
                'matrix.' + domain,
                mas_host(values),
                *[prefix + '.' + domain for prefix in ('admin', 'web', 'rtc', 'turn')],
            ]
        )
    )


def validate(values, origins=None, complete=True, preview=False):
    origins = origins or {}
    normalized = {}
    for key, value in values.items():
        if preview and key == 'PUBLIC_IP' and value:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise InvalidConfiguration(
                    origins.get(key, 'settings.env'), key, 'field_format', 'IPv4'
                ) from None
            normalized[key] = value
        else:
            normalized[key] = field_value(key, value, origins.get(key, 'settings.env'))
    if not complete:
        return normalized
    model = {key: item.default for key, item in FIELDS.items()}
    model.update(normalized)

    def error(key, message, code='field_dependency'):
        raise InvalidConfiguration(origins.get(key, 'settings.env'), key, code, message)

    if not model['DOMAIN']:
        error('DOMAIN', 'обязательный hostname без схемы')
    if mas_host(model) in [
        prefix + '.' + model['DOMAIN'] for prefix in ('web', 'admin', 'rtc', 'turn')
    ]:
        error(
            'MAS_DOMAIN',
            'домен занят другой службой; используйте отдельный домен, DOMAIN или matrix.<DOMAIN>',
        )
    if model['HOMEPAGE_MODE'] == 'external':
        if not model['HOMEPAGE_URL']:
            error('HOMEPAGE_URL', 'обязательно при external')
        target = urlsplit(model['HOMEPAGE_URL'])
        host = target.hostname.lower().rstrip('.')
        managed = public_hosts(model)
        if model['PUBLIC_IP']:
            managed.append(model['PUBLIC_IP'])
        if host in managed:
            error(
                'HOMEPAGE_URL',
                'выберите внешний сайт; управляемые домены создают цикл или ведут в служебные endpoints',
            )
    for provider in ('GOOGLE', 'TELEGRAM'):
        if bool(model[provider + '_CLIENT_ID']) != bool(model[provider + '_CLIENT_SECRET']):
            error(
                provider + '_CLIENT_SECRET',
                provider
                + ' requires both '
                + provider
                + '_CLIENT_ID and '
                + provider
                + '_CLIENT_SECRET; заполните оба или очистите оба',
                'oauth_' + provider.lower() + '_pair_incomplete',
            )
    if model['MEDIA_MODE'] == 's3':
        try:
            s3_config.validate({key: model[key] for key in s3_config.FIELDS})
        except s3_config.ConfigError as exc:
            mapping = {
                'bucket_required': 'S3_BUCKET',
                'bucket_format': 'S3_BUCKET',
                'region_required': 'S3_REGION',
                'credentials_pair_required': 'S3_SECRET_ACCESS_KEY',
                'endpoint_format': 'S3_ENDPOINT_URL',
                'prefix_format': 'S3_PREFIX',
            }
            error(mapping.get(exc.code, 'S3_BUCKET'), str(exc), exc.code)
    if model['POSTGRES_MODE'] == 'external':
        for key in ('POSTGRES_HOST', 'POSTGRES_ADMIN_PASSWORD'):
            if not model[key]:
                error(key, 'обязательно при external PostgreSQL')
    return model


def validate_backup(values, file='settings/backup.env'):
    model = {key: item.default for key, item in BACKUP_FIELDS.items()}
    model.update(
        {key: field_value(key, value, file, BACKUP_FIELDS) for key, value in values.items()}
    )
    try:
        s3_config.validate({k: model[k] for k in s3_config.FIELDS})
    except s3_config.ConfigError as exc:
        raise InvalidConfiguration(file, 'S3', exc.code, str(exc)) from None
    if not model['BACKUP_KEY_FILE']:
        raise InvalidConfiguration(
            file, 'BACKUP_KEY_FILE', 'required', 'нужен путь recovery key вне установки'
        )
    if any(ord(c) < 32 or c in '%\\' for c in model['BACKUP_CALENDAR']):
        raise InvalidConfiguration(
            file,
            'BACKUP_CALENDAR',
            'field_format',
            'одна строка systemd calendar без % или backslash',
        )
    return model
