"""Literal private S3 profiles and validation shared by all S3 consumers."""

from collections.abc import Mapping
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

FIELDS = {
    'S3_BUCKET',
    'S3_REGION',
    'S3_ENDPOINT_URL',
    'S3_ACCESS_KEY_ID',
    'S3_SECRET_ACCESS_KEY',
    'S3_PREFIX',
}


class ConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate(values: Mapping[str, str]) -> dict[str, str]:
    if set(values) - FIELDS:
        raise ConfigError(
            'unknown_parameters',
            'Файл должен содержать только шесть S3-полей; удалите посторонние настройки приватно.',
        )
    if not all(isinstance(v, str) and not any(ord(c) < 32 for c in v) for v in values.values()):
        raise ConfigError(
            'invalid_parameters', 'S3-параметры должны быть строками без управляющих символов.'
        )
    if not values.get('S3_BUCKET'):
        raise ConfigError(
            'bucket_required', 'Укажите S3_BUCKET — имя существующего bucket, не URL.'
        )
    bucket = values['S3_BUCKET']
    if (
        not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket)
        or '..' in bucket
        or '.-' in bucket
        or '-.' in bucket
        or re.fullmatch(r'\d+\.\d+\.\d+\.\d+', bucket)
    ):
        raise ConfigError(
            'bucket_format',
            'Проверьте имя S3_BUCKET: 3–63 строчные буквы, цифры, точки или дефисы.',
        )
    if not values.get('S3_REGION') or not re.fullmatch(
        r'[a-z0-9][a-z0-9-]{0,62}', values['S3_REGION']
    ):
        raise ConfigError(
            'region_required',
            'Укажите S3_REGION из документации провайдера. Это параметр подписи SigV4; он не определяется по endpoint автоматически.',
        )
    if not values.get('S3_ACCESS_KEY_ID') or not values.get('S3_SECRET_ACCESS_KEY'):
        raise ConfigError(
            'credentials_pair_required',
            'Заполните оба S3_ACCESS_KEY_ID и S3_SECRET_ACCESS_KEY приватно.',
        )
    endpoint = values.get('S3_ENDPOINT_URL', '')
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            valid = (
                parsed.scheme == 'https'
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and parsed.path in ('', '/')
                and not parsed.query
                and not parsed.fragment
                and not any(c.isspace() for c in endpoint)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError(
                'endpoint_format',
                'S3_ENDPOINT_URL должен быть HTTPS origin без credentials, query и пути. Пустое значение выбирает AWS endpoint для заданного region.',
            )
    prefix = values.get('S3_PREFIX', '')
    if (
        not prefix
        or not prefix.endswith('/')
        or prefix.startswith('/')
        or any(part in ('.', '..', '') for part in prefix[:-1].split('/'))
        or '\\' in prefix
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}/', prefix)
        or '.misbot-probes' in prefix.split('/')
    ):
        raise ConfigError(
            'prefix_format',
            'Укажите отдельный относительный S3_PREFIX с завершающим /, без пустых частей, . и ..; namespace .misbot-probes зарезервирован.',
        )
    return dict(values, S3_ENDPOINT_URL=endpoint)


def read_profile(path: str | Path, purpose: str = 'media') -> dict[str, str]:
    path = Path(path)
    if path.name == 'backup.env' and path.parent.name == 'settings':
        if purpose != 'backup':
            raise ConfigError(
                'profile_purpose',
                'Этот файл относится к внешним копиям; используйте --purpose backup, а не media.',
            )
        import configuration
        import configuration_schema

        try:
            model = configuration_schema.validate_backup(
                configuration.parse(
                    configuration.read_private(path), path, configuration_schema.BACKUP_FIELDS
                ),
                str(path),
            )
            if Path(model['BACKUP_KEY_FILE']).is_relative_to(path.parent.parent):
                raise ConfigError(
                    'key_location', 'Recovery key должен быть вне каталога установки.'
                )
        except configuration_schema.InvalidConfiguration as error:
            raise ConfigError(error.code, str(error)) from None
        return validate({key: model[key] for key in FIELDS})
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8-sig') as incoming:
            info = os.fstat(incoming.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > 65536
            ):
                raise ConfigError(
                    'private_file_permissions',
                    'Нужен обычный файл владельца root, mode 0600, без ссылок; каталог держите 0700.',
                )
            text = incoming.read(65537)
    except ConfigError:
        raise
    except (OSError, UnicodeError):
        raise ConfigError(
            'private_file_unreadable',
            'Приватный S3-файл отсутствует, недоступен или имеет неверную кодировку; исправьте файл, не выполняя его как shell-код.',
        ) from None
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ConfigError(
                'file_syntax',
                'Используйте буквальные строки KEY=value; shell-код не поддерживается.',
            )
        key, value = (p.strip() for p in line.split('=', 1))
        if key not in FIELDS:
            raise ConfigError(
                'unknown_parameters',
                'Допустимы только шесть S3-полей; неизвестные ключи не выводятся.',
            )
        if key in values:
            raise ConfigError(
                'duplicate_parameters', 'Удалите дубли S3-полей; для каждого оставьте одну строку.'
            )
        if value[:1] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError('file_syntax', 'Закройте кавычки значений приватного файла.')
            value = value[1:-1]
        values[key] = value
    return validate(values)
