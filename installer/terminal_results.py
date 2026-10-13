"""Present known operation results without executing work or exposing raw provider output.

Callers retain progress, pagination, next actions and interactive secret display.
None means a record belongs to another renderer; an empty plain result is not retained.
"""

import re
from datetime import datetime
from pathlib import PurePosixPath


VERSION = re.compile(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?')
DIGEST = re.compile(r'[a-fA-F0-9]{64}')
SENSITIVE = re.compile(
    r'(?i)(?:authorization\s*:|bearer\s+|-----BEGIN|'
    r'(?:password|passwd|access[_ -]?key|secret[_ -]?key|client[_ -]?secret|'
    r'token|credential)\s*[=:]|https?://[^\s/]+@|[?&](?:key|sig|signature)=)'
)
# These wrappers deliberately publish RuntimeError/owned Error guidance while
# replacing provider/driver exceptions. Matching both route and producer avoids
# treating a random JSON message or SDK response as a public error contract.
ERROR_PRODUCERS = {
    'media_storage': ('storage local', 'storage s3 connect', 'storage recover'),
    'media_local_recovery': ('storage s3 connect', 'storage recover'),
    'database_storage': ('storage database ',),
    'administrator_password': ('credentials admin ',),
    'user_password': ('users password ',),
    'database_passwords': ('credentials database ',),
    's3_credentials': ('credentials s3 ',),
    'vault': ('credentials vault ',),
    'external_backup': ('backup key create', 'backup external ', 'backup fetch'),
    'backup_schedule': ('backup schedule ',),
    'restore': ('backup restore', 'backup restore-local'),
    'preflight': ('config preflight', 'start'),
    'existing_features': ('features ', 'homepage '),
    'existing_source_update': ('update apply', 'update recover'),
    'existing_backup_schedule': ('backup schedule ',),
    'existing_operation': (
        'status',
        'check',
        'start',
        'stop',
        'config validate',
        'backup ',
        'storage ',
    ),
}
PASSWORD_RESULTS = {
    'administrator_password_changed': ('credentials admin set', 'Пароль администратора изменён.'),
    'administrator_password_recovered': (
        'credentials admin recover',
        'Восстановлен прежний пароль администратора.',
    ),
    'user_password_changed': ('users password set', 'Новый пароль пользователя установлен.'),
    'user_password_recovered': (
        'users password recover',
        'Установка нового пароля пользователя завершена.',
    ),
    'database_passwords_changed': (
        'credentials database ',
        'Пароли приложений для баз данных изменены.',
    ),
}
STORAGE_RESULTS = {
    'media_local_selected': (
        'storage local',
        'Выбраны локальные медиа. Сервер ещё нужно запустить.',
    ),
    'media_s3_selected': ('storage s3 connect', 'Выбрано S3. Сервер ещё нужно запустить.'),
    'media_s3_connected': (
        'storage s3 connect',
        'Медиа подключены к S3. Локальные файлы сохранены.',
    ),
    'media_local_recovery': ('storage recover', 'Прежние локальные настройки медиа восстановлены.'),
    'database_external_connected': (
        'storage database ',
        'Перенос в PostgreSQL провайдера завершён.',
    ),
    'database_local_restored': (
        'storage database abort',
        'Восстановлена работа с локальной PostgreSQL.',
    ),
}


def matches(path, routes):
    return any(path.startswith(route) if route.endswith(' ') else path == route for route in routes)


def safe_text(value, limit=1600):
    """Extra guard for documented product text, never a sanitizer for arbitrary SDK dumps."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if SENSITIVE.search(value) or any(v in value for v in ('Traceback', 'botocore.', 'urllib.')):
        return None
    if any(char in value for char in '{}') or value.lstrip().startswith(('[', 'File ')):
        return None
    return value.strip() or None


def version(value):
    return value if isinstance(value, str) and VERSION.fullmatch(value) else None


def private_path(value):
    value = safe_text(value, 1024)
    if value and value.startswith('/') and '..' not in PurePosixPath(value).parts:
        return value
    return None


def number(value):
    return str(value) if type(value) is int and value >= 0 else None


def state(record):
    value = record.get('status', record.get('state'))
    return value if isinstance(value, str) else ''


def known_error(path, record):
    check = record.get('check')
    if isinstance(check, str) and matches(path, ERROR_PRODUCERS.get(check, ())):
        return True
    if check == 'failed' and record.get('error_type') in (
        'RuntimeError',
        'S3CredentialRecoveryRequired',
    ):
        return matches(path, ('start', 'stop', 'check', 'status', 'backup ', 'config generate'))
    return record.get('stage') in ('configuration', 'homepage') and matches(
        path, ('config ', 'homepage ')
    )


def failure_result(path, record):
    result = ['Операция не завершена.']
    if record.get('partial') is True or state(record) in ('partial', 'partially_applied'):
        result = ['Изменения применены частично; завершение не подтверждено.']
    if known_error(path, record):
        explanation = safe_text(record.get('explanation')) or safe_text(record.get('message'))
        if explanation:
            result.append(explanation)
    if len(result) == 1:
        result.append('Проверьте состояние сервера и сохранённое продолжение операции.')
    return result


def release_result(path, record):
    current, latest = version(record.get('installed')), version(record.get('latest'))
    if path == 'update check' and 'installed' in record:
        result = ['Установлено: ' + (current or 'версия не подтверждена') + '.']
        if latest:
            result.append('Последний стабильный релиз: ' + latest + '.')
        else:
            result.append('Последний стабильный релиз не подтверждён.')
        if record.get('update_available') is True and latest:
            result.append(
                'Доступно обновление.'
                if record.get('update_supported') is True
                else 'Применение обновления требует подготовленного комплекта через SSH.'
            )
        elif record.get('update_available') is False and latest:
            result.append('Более новой стабильной версии нет.')
        return result
    if path != 'update latest' or 'installed' not in record:
        return None
    if record.get('success') is True:
        return ['Обновление применено.' + (' Установлено: ' + current + '.' if current else '')]
    if record.get('success') is False:
        result = ['Установлено: ' + current + '.'] if current else []
        detail = safe_text(record.get('detail'))
        if detail and detail.startswith('Обновление не подтверждено. Приватный журнал: '):
            return result + [detail]
        return result + [
            'Обновление не подтверждено.',
            'Проверьте состояние сервера перед повтором.',
        ]
    if record.get('detail') == 'Последняя стабильная версия уже установлена.' and current:
        return ['Последняя стабильная версия уже установлена: ' + current + '.']
    return ['Результат обновления не подтверждён.']


def schedule_result(record):
    if record.get('resource') == 'timer':
        raw = record.get('NextElapseUSecRealtime')
        next_run = safe_text(raw, 120)
        if raw == '':
            return ['Следующая копия не запланирована.']
        return (
            ['Следующая копия: ' + next_run + '.']
            if next_run
            else ['Время следующей копии не получено.']
        )
    if record.get('resource') == 'service':
        if record.get('ActiveState') in ('activating', 'active'):
            return ['Копирование выполняется; результат ещё не подтверждён.']
        if record.get('execution_metadata_available') is not True:
            return ['Сведения о выполненном запуске копирования отсутствуют.']
        return [
            'Последний запуск копирования: '
            + ('успешен.' if record.get('Result') == 'success' else 'не подтверждён.')
        ]
    result = []
    if record.get('needs_reconfigure') is True:
        result.append('Расписание нужно настроить для этого сервера.')
    elif record.get('configured') is False:
        result.append('Расписание копий не настроено.')
    if type(record.get('enabled')) is bool:
        result.append(
            'Автоматические копии: ' + ('включены.' if record['enabled'] else 'выключены.')
        )
    calendar = safe_text(record.get('calendar'), 160)
    if calendar:
        result.append('Расписание: ' + calendar + '.')
    if 'last_verified' in record:
        receipt = record.get('last_verified')
        verified = receipt if isinstance(receipt, dict) else {}
        stamp = verified.get('completed_utc')
        try:
            date = (
                datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ') if isinstance(stamp, str) else None
            )
        except ValueError:
            date = None
        if date and verified.get('two_database_restores') is True:
            result.append(
                'Последняя проверенная копия: ' + date.strftime('%d.%m.%Y %H:%M UTC') + '.'
            )
        else:
            result.append('Проверенная копия на этом сервере не подтверждена.')
    return result or ['Состояние расписания не подтверждено.']


def backup_result(path, record):
    check = record.get('check')
    if path == 'backup key create' and check == 'backup_recovery_key':
        if record.get('created') is not True:
            return ['Создание ключа копий не подтверждено.']
        result = ['Ключ резервных копий создан. Сохраните его отдельно от сервера.']
        key = private_path(record.get('key_file'))
        return result + (['Файл ключа: ' + key] if key else [])
    if path == 'backup fetch' and check == 'external_backup_download':
        archive = private_path(record.get('archive'))
        digest = record.get('archive_sha256')
        if record.get('passed') is not True:
            return ['Скачивание и проверка копии не подтверждены.']
        result = ['Копия скачана; целостность подтверждена.']
        if archive:
            result.append('Архив копии: ' + archive)
        if isinstance(digest, str) and DIGEST.fullmatch(digest):
            result.append('SHA256: ' + digest)
        return result
    if path == 'backup external create' and check == 'encrypted_external_backup':
        if record.get('passed') is not True:
            return ['Внешняя копия не подтверждена.']
        result = [
            'Зашифрованная копия загружена в S3; целостность подтверждена.',
            'Проверка целостности не подтверждает восстановление баз данных из этой копии.',
        ]
        object_key = safe_text(record.get('object_key'), 512)
        if object_key:
            result.append('Объект копии: ' + object_key)
        catalog = private_path(record.get('private_catalog'))
        if catalog:
            result.append('Сведения о копии: ' + catalog)
        return result
    if (
        path in ('backup external create', 'backup external cleanup')
        and check == 'external_backup_cleanup'
    ):
        result = [
            'Удаление неподтверждённой загрузки запрошено.'
            if record.get('delete_requested') is True
            else 'Удаление неподтверждённой загрузки не подтверждено.'
        ]
        if record.get('unverified_object_may_remain') is True:
            result.append(
                'Объект может оставаться в S3; эта загрузка не является проверенной копией.'
            )
        catalog = private_path(record.get('private_catalog'))
        return result + (['Сведения об очистке: ' + catalog] if catalog else [])
    return None


def features_result(path, record):
    check = record.get('check')
    if not path.startswith('features '):
        return None
    if check == 'existing_features_recovery':
        return [
            'Публичные маршруты восстановлены. Учётные записи и ключи ботов сохранены.'
            if record.get('public_routes_restored') is True
            and record.get('bot_identity_retained') is True
            else 'Завершение восстановления компонентов не подтверждено.'
        ]
    if check != 'existing_features':
        return None
    result = []
    for key, label in (
        ('enabled', 'Дополнительные компоненты'),
        ('referral_bot', 'Бот приглашений'),
        ('federation_bot', 'Административный бот'),
        ('vault', 'Vault'),
    ):
        value = record.get(key)
        if type(value) is bool:
            result.append(label + ': ' + ('подключён.' if value else 'не подключён.'))
    if record.get('pending') is True:
        result.append('Подключение компонентов требует продолжения.')
    return result or ['Состояние компонентов не подтверждено.']


def credentials_result(path, record):
    check = record.get('check')
    if not isinstance(check, str):
        return None
    if check in PASSWORD_RESULTS:
        route, text = PASSWORD_RESULTS[check]
        if matches(path, (route,)) and record.get('passed') is True:
            return [text]
    if path.startswith('credentials s3 '):
        if check == 's3_credentials_changed' and record.get('passed') is True:
            return [
                'Ключи доступа S3 уже соответствуют запросу; замена не требовалась.'
                if record.get('changed') is False
                else 'Ключи доступа S3 изменены.'
            ]
        if check == 's3_credential_probe_cleanup' and record.get('passed') is True:
            count = number(record.get('deleted'))
            return [
                'Собственные проверочные объекты удалены' + (': ' + count if count else '') + '.'
            ]
    if path.startswith('credentials vault '):
        if check == 'vault_snapshot' and record.get('passed') is True:
            result = ['Снимок секретов в Vault проверен.']
            revision, files = number(record.get('version')), number(record.get('files'))
            if revision:
                result.append('Версия снимка: ' + revision + '.')
            if files:
                result.append('Файлов в снимке: ' + files + '.')
            return result
        if check == 'vault_recovered' and record.get('passed') is True:
            files = number(record.get('files'))
            return [
                'Отсутствующие секретные файлы восстановлены'
                + (': ' + files if files else '')
                + '.'
            ]
        if check == 'vault_disconnected' and record.get('passed') is True:
            result = [
                'Vault отключён.' if record.get('changed') is not False else 'Vault уже отключён.'
            ]
            if record.get('local_files_retained') is True:
                result.append('Локальные файлы сохранены.')
            if record.get('remote_versions_retained') is True:
                result.append('Удалённые версии в Vault сохранены.')
            return result
    if (
        record.get('pending') is False
        and record.get('changed') is False
        and check
        in (
            'database_password_recovery',
            's3_credential_recovery',
        )
        and path in ('credentials database recover', 'credentials s3 recover')
    ):
        return ['Незавершённого изменения ключей или паролей нет.']
    return None


def probe_result(record):
    labels = {
        'configuration': 'Настройки',
        'connection': 'Соединение',
        'authentication': 'Ключи доступа',
        'bucket': 'Бакет',
        'write': 'Запись',
        'read': 'Чтение',
        'cleanup': 'Очистка',
    }
    result = []
    for item in record['stages'][:20]:
        if not isinstance(item, dict):
            continue
        label = labels.get(item.get('stage')) if isinstance(item.get('stage'), str) else None
        status = item.get('status')
        status = status if isinstance(status, str) else ''
        symbol, fallback = {
            'passed': ('✓', 'подтверждено'),
            'failed': ('×', 'не подтверждено'),
            'not_checked': ('○', 'не проверялось'),
            'running': ('…', 'выполняется'),
        }.get(status, ('?', 'результат неизвестен'))
        explanation = safe_text(item.get('explanation'), 512) or fallback
        result.append(symbol + ' ' + (label or 'Проверка') + ': ' + explanation)
    conclusion = safe_text(record.get('conclusion'), 512)
    return result + (['Итог: ' + conclusion] if conclusion else [])


def storage_status_result(record):
    if record.get('passed') is False:
        return ['Сохранённое состояние хранилища не удалось проверить.']

    def choice(value, yes, no):
        return yes if value is True else no if value is False else 'нет данных'

    result = [
        'Медиа: ' + choice(record.get('media_s3'), 'S3', 'на этом сервере') + '.',
        'Хранилище внешних копий S3: '
        + choice(record.get('backup_s3'), 'подключено', 'не подключено')
        + '.',
        'Перенос медиа: '
        + choice(record.get('media_pending'), 'требует продолжения', 'не выполняется')
        + '.',
        'Целостность сохранённой загрузки в S3: '
        + choice(
            record.get('upload_verified', record.get('last_backup_verified')),
            'подтверждена',
            'не подтверждена',
        )
        + '.',
    ]
    restored = record.get('restore_verified')
    scope = record.get('restore_scope')
    stamp = record.get('last_restore_verified_at')
    try:
        date = datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ') if isinstance(stamp, str) else None
    except ValueError:
        date = None
    if scope == 'selected-backup':
        result.append(
            'Восстановление выбранной внешней копии: '
            + choice(restored, 'подтверждено', 'не подтверждено')
            + '.'
        )
    elif restored is True and date and scope == 'scheduled-history':
        result.extend(
            [
                'Последняя успешная проверка восстановления баз данных: '
                + date.strftime('%d.%m.%Y %H:%M UTC')
                + '.',
                'Это результат прошлого запуска по расписанию; он не подтверждает последнюю загрузку.',
            ]
        )
    else:
        result.append(
            'Проверка восстановления баз данных: '
            + ('не подтверждена' if restored is False else 'нет данных')
            + '.'
        )
    if 'database_mode' in record:
        mode = record.get('database_mode')
        result.append(
            'База данных: '
            + (
                {'external': 'внешняя PostgreSQL', 'local': 'на этом сервере'}.get(
                    mode, 'нет данных'
                )
                if isinstance(mode, str)
                else 'нет данных'
            )
            + '.'
        )
    if record.get('database_pending') is True:
        result.append('Перенос базы данных требует продолжения.')
    destination = record.get('manual_backup_destination')
    if destination == 'local':
        result.append('Команда «Создать локальную копию» сохраняет архив на этом сервере.')
    elif destination == 'external':
        result.append('Ручная резервная копия сохраняется в S3.')
    return result


def presentation_outcome(path, record):
    """Final product mutation outcome, separate from child exit/progress diagnostics.

    None leaves the last outcome unchanged. A later recognized final success can
    resolve an earlier failure; transient health/progress events never do either.
    Foreground interactive commands continue to rely on their actual exit status.
    """
    if not isinstance(record, dict) or path in (
        'status',
        'check',
        'config validate',
        'config preflight',
        'features status',
        'backup schedule status',
        'credentials vault status',
        'storage status',
        'storage s3 check',
        'update check',
    ):
        return None
    check = record.get('check')
    recognized = known_error(path, record) or (path == 'update latest' and 'installed' in record)
    if isinstance(check, str):
        for profiles in (STORAGE_RESULTS, PASSWORD_RESULTS):
            if check in profiles and matches(path, (profiles[check][0],)):
                recognized = True
    recognized = recognized or any(
        formatter(path, record) is not None
        for formatter in (backup_result, features_result, credentials_result)
    )
    if not recognized:
        return None
    if (
        record.get('passed') is False
        or record.get('success') is False
        or record.get('partial') is True
        or state(record) in ('failed', 'error', 'partial', 'partially_applied')
    ):
        return False
    if record.get('passed') is True or record.get('success') is True:
        return True
    return None


def format_record(path: str, record: dict) -> list[str] | None:
    """Format only recognized public results; never inspect unknown fields or credentials."""
    if not isinstance(path, str) or not isinstance(record, dict):
        return None
    # Its existing renderer already preserves partial/remote/local state precisely.
    if path.startswith('federation '):
        return None
    if (
        path in ('storage s3 check', 'storage s3 cleanup')
        and record.get('schema') == 1
        and isinstance(record.get('stages'), list)
    ):
        return probe_result(record)
    if record.get('partial') is True or state(record) in ('partial', 'partially_applied'):
        return failure_result(path, record)
    if path == 'storage status' and record.get('check') in (
        'storage_status',
        'existing_storage_status',
    ):
        return storage_status_result(record)
    if record.get('passed') is False or state(record) in ('failed', 'error'):
        return failure_result(path, record)
    result = release_result(path, record)
    if result is not None:
        return result
    if (
        record.get('passed') is False
        or record.get('success') is False
        or state(record)
        in (
            'failed',
            'error',
            'partial',
            'partially_applied',
        )
        or record.get('partial') is True
    ):
        return failure_result(path, record)
    if state(record) in ('pending', 'queued', 'running'):
        return [
            'Операция ожидает продолжения.'
            if state(record) == 'pending'
            else 'Операция ещё не завершена.'
        ]
    check = record.get('check')
    if not isinstance(check, str):
        return None
    if path.startswith('backup schedule ') and check in (
        'backup_schedule',
        'backup_schedule_status',
        'existing_backup_schedule',
    ):
        return schedule_result(record)
    for formatter in (backup_result, features_result, credentials_result):
        result = formatter(path, record)
        if result is not None:
            return result
    if check in STORAGE_RESULTS:
        route, text = STORAGE_RESULTS[check]
        if matches(path, (route,)) and record.get('passed') is True:
            return [text]
    return None


def format_plain_result(path: str, line: str) -> list[str]:
    """Keep known safe summaries, not arbitrary stdout, tracebacks or secret display."""
    if not isinstance(path, str) or not isinstance(line, str):
        return []
    text = safe_text(line)
    if not text or path in ('users invitation show', 'credentials admin show'):
        return []
    if path in ('update apply', 'update recover'):
        match = re.fullmatch(r'release_applied (\S+)', text)
        if match and version(match[1]):
            return ['Обновление применено: ' + match[1] + '.']
        match = re.fullmatch(r'update_recovered (\S+) healthy True', text)
        if match and version(match[1]):
            return ['Восстановлена версия ' + match[1] + '. Проверки служб пройдены.']
        if text.startswith('private_update_log '):
            path_value = private_path(text.removeprefix('private_update_log '))
            return ['Журнал обновления: ' + path_value] if path_value else []
        match = re.fullmatch(r'source_files_changed (\d+)', text)
        if match:
            return ['Изменено файлов программы: ' + match[1] + '.']
        match = re.fullmatch(
            r'update_error Update failed; previous files restored; rollback healthy=(True|False); inspect private logs in (.+)',
            text,
        )
        if match:
            result = ['Обновление не завершено. Прежние файлы программы восстановлены.']
            result.append(
                'Прежние службы работают.'
                if match[1] == 'True'
                else 'Работа прежних служб не подтверждена; требуется восстановление.'
            )
            folder = private_path(match[2])
            if folder:
                result.append('Каталог журналов обновления: ' + folder)
            return result
        if text.startswith('update_error '):
            return ['Обновление не завершено. Проверьте состояние сервера и журнал обновления.']
    if (
        path == 'update check'
        and text == 'Последняя версия не подтверждена. Повторите проверку GitHub Releases позже.'
    ):
        return [text]
    if path.startswith('users invitation ') and text in (
        'Список приглашений не удалось проверить безопасно. Сохраните данные и повторите users invitation list.',
        'Invitation could not be verified safely; retain private checkpoint and retry users invitation ensure',
        'Finish initial start or pending recovery before managing invitations',
    ):
        return [
            'Приглашение не подтверждено. Проверьте состояние сервера и завершите указанную операцию восстановления.'
        ]
    if path == 'backup schedule status' and text.startswith('NextElapseUSecRealtime='):
        value = safe_text(text.removeprefix('NextElapseUSecRealtime='), 120)
        return ['Следующая копия: ' + (value or 'не запланирована') + '.']
    return []
