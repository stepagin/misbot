"""Private operation forms; backends retain validation, locking and recovery."""

from contextlib import contextmanager
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

import cli_commands
import configuration
import configuration_schema as schema
import s3_config
import state_store

HANDLED_PATHS = frozenset(
    (
        'backup external create',
        'backup schedule external',
        'backup fetch',
        'storage database connect',
        'credentials s3 rotate',
        'credentials vault connect',
        'credentials vault auth',
        'features enable',
    )
)
DIGEST = re.compile(r'[0-9a-f]{64}')


def supplied(ui):
    return getattr(ui, 'supplied_arguments', {})


def private_parent(ui):
    return configuration.directory(ui.draft.parent)


@contextmanager
def profile_file(ui, values, json_file=False):
    """No secret values enter argv; cancelled/completed form inputs disappear."""
    with tempfile.TemporaryDirectory(prefix='.operation-', dir=private_parent(ui)) as folder:
        path = Path(folder) / ('input.json' if json_file else 'profile.env')
        if json_file:
            state_store.write_json(path, values)
        else:
            state_store.write_text(path, configuration.serialize(values))
        yield path


def confirm(ui, item, rows):
    command = cli_commands.BY_PATH[item.path]
    ui.view.details([('Установка', str(ui.root)), *rows, ('Перерыв в работе', command.stops)])
    if not ui.confirm():
        ui.view.message('Действие отменено.')
        return False
    return True


def finish(ui, item, code):
    if code == 0:
        remember_backup_results(ui, item.path)
    ui.show_operation_result(item, code)
    return code


def backup_values(ui, allow_create=True):
    """Return saved profile/key or collect a new profile without writing it yet."""
    import operation_inputs

    explicit = supplied(ui)
    if allow_create and not explicit.get('config') and not ui.adopted():
        missing = missing_backup_key(ui)
        if missing is not None:
            return missing
    saved = (
        operation_inputs.saved_backup(ui.root, key_override=explicit.get('key'))
        if not explicit.get('config')
        else None
    )
    profile = Path(explicit['config']) if explicit.get('config') else saved[0] if saved else None
    if profile:
        values = s3_config.read_profile(profile, 'backup')
    else:
        values = ui.s3_profile(getattr(ui, 'backup', None) or {})
    key = explicit.get('key') or (saved[1] if saved else None)
    if key:
        key = Path(ui.backup_key(str(key)))
    else:
        initial = ui.installation().get('existing', {}).get('backup_key')
        if not initial:
            initial = str(private_parent(ui) / (ui.draft.stem + '-backup.key'))
        key = Path(
            ui.input_value(
                'Ключ копий; существующий ключ сохраняется, отсутствующий будет создан'
                if allow_create
                else 'Сохранённый ключ расшифрования копии',
                initial,
                ui.backup_key,
            )
        )
    if (not allow_create or ui.adopted()) and not key.is_file():
        raise ValueError('Нужен прежний ключ копии. Новый ключ не расшифрует существующие данные.')
    return values, Path(profile) if profile else None, key, profile is None


def missing_backup_key(ui):
    """A lost configured key needs a deliberate choice, never silent replacement."""
    from terminal_ui import Back

    profile = ui.root / 'settings/backup.env'
    if not profile.exists():
        return None
    values = schema.validate_backup(
        configuration.parse(configuration.read_private(profile), profile, schema.BACKUP_FIELDS)
    )
    old_key = Path(values['BACKUP_KEY_FILE'])
    if old_key.exists() or old_key.is_symlink():
        return None
    if supplied(ui).get('key'):
        key = Path(ui.backup_key(str(supplied(ui)['key'])))
    else:
        ui.view.message(
            'Сохранённый ключ копий отсутствует. Для старых копий нужен прежний ключ. '
            'Новый ключ будет применяться только к будущим копиям и не расшифрует старые.',
            'yellow',
        )
        choice = ui.choose(
            'Ключ резервных копий',
            [
                ('1', 'Использовать сохранённый прежний ключ', 'Выберите файл на этом сервере'),
                (
                    '2',
                    'Создать новый ключ для будущих копий',
                    'Прежний ключ и старые копии сохраняются отдельно',
                ),
            ],
        )
        if choice == '0':
            raise Back()
        if choice == '1':

            def existing(value):
                checked = ui.backup_key(value)
                if not Path(checked).is_file():
                    raise ValueError('Выберите существующий прежний ключ.')
                return checked

            key = Path(ui.input_value('Прежний ключ копий', '', existing))
        else:
            key = private_parent(ui) / ('backup-' + secrets.token_hex(8) + '.key')
    if key == old_key and not key.exists():
        raise ValueError(
            'Для нового ключа нужен отдельный путь; прежний потерянный ключ не заменяется.'
        )
    return {key: values[key] for key in s3_config.FIELDS}, None, key, True


def save_backup(ui, values, key, calendar=''):
    """Persist the selected profile before a timer can reference it."""
    if ui.adopted():
        folder = configuration.directory(ui.root / '.misbot/s3')
        profile = folder / 'backup.env'
        with state_store.operation_guard(ui.root):
            state_store.write_text(profile, configuration.serialize(values))
        return profile
    model = configuration.load(ui.root, complete=False)
    if (ui.root / 'settings.env').exists():
        configuration.migrate(ui.root)
        model = configuration.load(ui.root, complete=False)
    backup = {**values, 'BACKUP_KEY_FILE': str(key), 'BACKUP_CALENDAR': calendar}
    configuration.write_model(ui.root, model, backup)
    ui.backup = backup
    return ui.root / 'settings/backup.env'


def execute_backup(ui, item):
    import operation_inputs

    values, profile, key, fresh = backup_values(ui)
    calendar = ''
    if item.path == 'backup schedule external':
        calendar = supplied(ui).get('calendar') or ui.schedule_calendar(
            operation_inputs.saved_calendar(ui.root)
        )
    rows = [('Хранилище копий', values['S3_BUCKET']), ('Ключ копий', str(key))]
    if calendar:
        rows.append(('Расписание', calendar))
    if fresh and (ui.root / 'settings.env').exists():
        ui.view.message('Настройки будут перенесены в новый формат с сохранением приватной копии.')
    if not confirm(ui, item, rows):
        return 0
    if not key.exists():
        code = ui.run('backup key create', key=key)
        if code:
            return finish(ui, item, code)
        ui.view.message(
            'Ключ копий создан. Сохраните его отдельную копию вне этого VPS: ' + str(key)
        )
    if fresh:
        profile = save_backup(ui, values, key, calendar)
    args = {'config': profile, 'key': key}
    if calendar:
        args['calendar'] = calendar
    code = ui.run(item.path, **args)
    return finish(ui, item, code)


def field(ui, key, initial='', required=False):
    definition = schema.FIELDS[key]

    def validate(value):
        result = schema.field_value(key, value)
        if required and not result:
            raise ValueError('Заполните это поле.')
        return result

    return ui.input_value(
        definition.label, initial or definition.default, validate, secret=definition.secret
    )


def execute_database(ui, item):
    import database

    values = {'POSTGRES_SSL_MODE': 'verify-full'}
    ui.view.message(
        'Выделенная PostgreSQL должна быть подготовлена у провайдера. TLS проверяет имя сервера и CA.'
    )
    for key in (
        'POSTGRES_HOST',
        'POSTGRES_PORT',
        'POSTGRES_ADMIN_USER',
        'POSTGRES_ADMIN_PASSWORD',
        'POSTGRES_SSL_ROOT_CERT',
    ):
        values[key] = field(
            ui,
            key,
            ui.model.get(key, ''),
            required=key in ('POSTGRES_HOST', 'POSTGRES_ADMIN_PASSWORD'),
        )
    with profile_file(ui, values) as profile:
        database.settings(profile)
        if not confirm(ui, item, [('PostgreSQL', values['POSTGRES_HOST']), ('TLS', 'verify-full')]):
            return 0
        code = ui.run(item.path, config=profile)
    return finish(ui, item, code)


def execute_s3_rotation(ui, item):
    model = configuration.load(ui.root, complete=False)
    values = s3_config.validate({key: model.get(key, '') for key in s3_config.FIELDS})
    # Preserve all destination fields. Only replacement credentials are editable.
    for key in ('S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY'):
        values[key] = ui.input_value(
            schema.FIELDS[key].label,
            '',
            lambda value, key=key: s3_config.validate_field(key, value),
            secret=True,
        )
    values = s3_config.validate(values)
    if not confirm(
        ui, item, [('Хранилище', values['S3_BUCKET']), ('Изменение', 'Только ключи доступа')]
    ):
        return 0
    with profile_file(ui, values) as profile:
        code = ui.run(item.path, config=profile)
    return finish(ui, item, code)


def vault_address(value):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == 'https'
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in ('', '/')
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Нужен HTTPS-адрес Vault без пути и реквизитов доступа.')
    return value


def vault_segment(value):
    if not value or not all(re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in value.split('/')):
        raise ValueError('Нужен относительный путь из букв, цифр, _ и -, без пустых частей.')
    return value


def vault_secret(value):
    if not value or len(value.encode()) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError('Введите выданное значение AppRole без управляющих символов.')
    return value


def vault_auth_referenced(root, folder):
    """Do not remove credentials after connect saved a descriptor but sync failed."""
    import vault_cache

    path = root / 'config/vault-connection.env'
    if not path.exists():
        return False
    try:
        current = vault_cache.connection(path)
        return any(
            Path(current[name]).is_relative_to(folder)
            for name in ('VAULT_ROLE_ID_FILE', 'VAULT_SECRET_ID_FILE')
        )
    except (OSError, ValueError, RuntimeError):
        # Ambiguous saved state is a recovery case, not permission to delete keys.
        return True


def execute_vault(ui, item):
    import vault_cache

    existing = ui.root / 'config/vault-connection.env'
    values = vault_cache.connection(existing) if existing.exists() else {}
    if values and item.path == 'credentials vault connect':
        if not confirm(
            ui, item, [('Vault', values['VAULT_ADDR']), ('Путь', values['VAULT_SECRET_PATH'])]
        ):
            return 0
        return finish(ui, item, ui.run(item.path, config=existing))
    if item.path.endswith(' auth') and not values:
        raise ValueError('Сначала подключите Vault в этом разделе.')
    if not values:
        for key, label, default, validator in (
            ('VAULT_ADDR', 'HTTPS-адрес Vault', '', vault_address),
            ('VAULT_KV_MOUNT', 'KV v2 mount', 'matrix', vault_segment),
            ('VAULT_SECRET_PATH', 'Выделенный путь снимков', 'misbot', vault_segment),
        ):
            values[key] = ui.input_value(label, default, validator)
    else:
        ui.view.message('Назначение Vault сохранено; изменяются только реквизиты доступа.')
    values['VAULT_CA_FILE'] = ui.input_value(
        'Путь частного CA PEM; Enter — системные сертификаты',
        values.get('VAULT_CA_FILE', ''),
        lambda value: schema.field_value('POSTGRES_SSL_ROOT_CERT', value),
    )
    role = ui.input_value('AppRole RoleID', '', vault_secret, secret=True)
    secret = ui.input_value('AppRole SecretID', '', vault_secret, secret=True)
    if not confirm(
        ui, item, [('Vault', values['VAULT_ADDR']), ('Путь', values['VAULT_SECRET_PATH'])]
    ):
        return 0
    parent = configuration.directory(ui.root / 'config/vault-auth')
    folder = Path(tempfile.mkdtemp(prefix='access-', dir=parent))
    try:
        role_file, secret_file = folder / 'role-id', folder / 'secret-id'
        state_store.write_text(role_file, role)
        state_store.write_text(secret_file, secret)
        values.update(VAULT_ROLE_ID_FILE=str(role_file), VAULT_SECRET_ID_FILE=str(secret_file))
        with profile_file(ui, values) as profile:
            vault_cache.connection(profile)
            code = ui.run(item.path, config=profile)
    finally:
        if not vault_auth_referenced(ui.root, folder):
            shutil.rmtree(folder)
    return finish(ui, item, code)


def read_record(path):
    """Read only an owned private receipt, including trusted ancestor paths."""
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Receipt path cannot contain links')
    record = configuration.read_private_json(path)
    if not isinstance(record, dict):
        raise ValueError('Invalid receipt')
    return record


def valid_digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def receipts(ui):
    """Trusted emitted receipts and verified export catalogs; never hash an import."""
    records = []
    paths = list((ui.root / 'stages/external-backups').glob('*/catalog.json'))
    paths += list((ui.draft.parent / 'backup-receipts').glob('*.json'))
    for path in sorted(paths, reverse=True):
        try:
            value = read_record(path)
            if value.get('format') != 1:
                continue
            if path.name == 'catalog.json':
                if value.get('upload_verified') is not True:
                    continue
                for name in ('source_archive', 'downloaded_archive'):
                    if value.get(name) and valid_digest(value.get('archive_sha256')):
                        records.append(
                            {
                                'archive': value[name],
                                'sha256': value['archive_sha256'],
                                'receipt': str(path),
                            }
                        )
                if valid_digest(value.get('ciphertext_sha256')) and isinstance(
                    value.get('object_key'), str
                ):
                    records.append(
                        {
                            'object': value['object_key'],
                            'sha256': value['ciphertext_sha256'],
                            'key': value.get('key_file'),
                            'receipt': str(path),
                        }
                    )
            elif value.get('directory') == str(ui.root) and valid_digest(value.get('sha256')):
                if isinstance(value.get('archive'), str):
                    records.append(value)
        except (OSError, ValueError, RuntimeError):
            continue
    return records


def remember_backup_results(ui, path):
    """Persist checksums returned by the trusted backend, not calculated from imports."""
    if path not in (
        'backup create',
        'backup move export',
        'backup external create',
        'backup fetch',
    ):
        return
    for record in getattr(ui, 'operation_records', []):
        if (
            record.get('check')
            not in ('consistent_backup', 'encrypted_external_backup', 'external_backup_download')
            or record.get('passed') is False
        ):
            continue
        archive = record.get('archive') or record.get('downloaded_archive')
        digest = record.get('archive_sha256') or record.get('sha256')
        if not isinstance(archive, str) or not valid_digest(digest):
            continue
        target = Path(archive)
        if not target.is_absolute() or target.resolve() != target:
            continue
        folder = configuration.directory(private_parent(ui) / 'backup-receipts')
        state_store.write_json(
            folder / (secrets.token_hex(12) + '.json'),
            {'format': 1, 'directory': str(ui.root), 'archive': archive, 'sha256': digest},
        )


def select_receipt(ui, kind):
    from terminal_ui import Back

    candidates = []
    seen = set()
    for record in receipts(ui):
        value = record.get(kind)
        if not isinstance(value, str) or value in seen:
            continue
        if kind == 'archive' and not Path(value).is_file():
            continue
        seen.add(value)
        candidates.append(record)
    if not candidates:
        return None
    rows = [
        (str(n), value[kind], 'Контрольная сумма сохранена при создании или проверке копии')
        for n, value in enumerate(candidates, 1)
    ]
    rows.append(('manual', 'Другая копия', 'Понадобится контрольная сумма с исходного сервера'))
    selected = ui.choose('Выберите копию', rows)
    if selected == '0':
        raise Back()
    return None if selected == 'manual' else candidates[int(selected) - 1]


def public_ip_suggestion():
    """Only propose a unique global address assigned to this host, never old DNS."""
    try:
        result = subprocess.run(
            ['ip', '-j', '-4', 'address', 'show', 'scope', 'global'],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        addresses = {
            item['local']
            for interface in json.loads(result.stdout)
            for item in interface.get('addr_info', [])
            if ipaddress.ip_address(item['local']).is_global
        }
        return next(iter(addresses)) if len(addresses) == 1 else ''
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return ''


def collect_restore(ui, backend):
    explicit = supplied(ui)
    archive, digest = explicit.get('archive'), explicit.get('sha256')
    selected = None
    if archive:
        selected = next((value for value in receipts(ui) if value.get('archive') == archive), None)
    elif not digest:
        selected = select_receipt(ui, 'archive')
    if not archive:
        archive = (
            selected['archive']
            if selected
            else ui.input_value(
                'Архив для восстановления',
                '',
                lambda value: ui.operation_argument('archive', value, backend),
            )
        )
    if not digest:
        if selected:
            digest = selected['sha256']
        else:
            ui.view.message(
                'Нужна контрольная сумма, сохранённая на исходном сервере. Сумма самого полученного файла не подтверждает источник.'
            )
            digest = ui.input_value(
                'SHA256 с исходного сервера',
                '',
                lambda value: ui.operation_argument('sha256', value, backend),
            )
    address = explicit.get('public-ip') or ui.input_value(
        'Публичный IPv4 нового сервера',
        public_ip_suggestion(),
        lambda value: ui.operation_argument('public-ip', value, backend),
    )
    return {'archive': archive, 'sha256': digest, 'public-ip': address}


def execute_fetch(ui, item):
    explicit = supplied(ui)
    selected = (
        None if explicit.get('object') or explicit.get('sha256') else select_receipt(ui, 'object')
    )
    # The selected catalog can name the original key. Explicit CLI input wins.
    previous = getattr(ui, 'supplied_arguments', {})
    if selected and selected.get('key') and not explicit.get('key'):
        ui.supplied_arguments = {**explicit, 'key': selected['key']}
    try:
        values, profile, key, _ = backup_values(ui, allow_create=False)
    finally:
        ui.supplied_arguments = previous
    object_key = explicit.get('object') or (selected['object'] if selected else None)
    digest = explicit.get('sha256') or (selected['sha256'] if selected else None)
    if not object_key:
        object_key = ui.input_value(
            'Объект копии в S3',
            '',
            lambda value: ui.operation_argument('object', value, 'backup-fetch'),
        )
    if not digest:
        digest = ui.input_value(
            'SHA256 зашифрованной копии с исходного сервера',
            '',
            lambda value: ui.operation_argument('sha256', value, 'backup-fetch'),
        )
    automatic_output = not explicit.get('output')
    output = (
        Path(explicit['output'])
        if not automatic_output
        else private_parent(ui) / 'downloads' / ('backup-' + secrets.token_hex(8) + '.tar.gz')
    )
    if not confirm(ui, item, [('Копия', object_key), ('Сохранить', str(output))]):
        return 0
    if automatic_output:
        configuration.directory(output.parent)
    with profile_file(ui, values) as input_profile:
        code = ui.run(
            item.path,
            config=profile or input_profile,
            key=key,
            object=object_key,
            sha256=digest,
            output=output,
        )
    return finish(ui, item, code)


def feature_path(ui, key, label, default):
    def validate(value):
        path = Path(value)
        if (
            not path.is_absolute()
            or path == Path('/')
            or path.resolve() != path
            or not re.fullmatch(r'/[A-Za-z0-9._/-]+', value)
        ):
            raise ValueError('Нужен абсолютный путь без ссылок, пробелов и переходов ..')
        if key in ('image_archive', 'image_manifest') and not path.is_relative_to(
            ui.root / '.misbot/features'
        ):
            raise ValueError('Выберите принятый файл из каталога .misbot/features этой установки.')
        if key == 'site_directory' and not path.is_relative_to('/srv'):
            raise ValueError('Для сайта нужен отдельный каталог внутри /srv.')
        return value

    return ui.input_value(label, default, validate)


def execute_features(ui, item):
    folder = ui.root / '.misbot/features'
    pending = folder / 'enable.json'
    if pending.exists():
        values = read_record(pending)['plan']
        ui.view.message('Продолжение использует прежний сохранённый план компонентов.')
    else:
        existing = ui.installation().get('existing', {})
        saved = existing.get('features', {})
        values = {'format': 1}
        for key, label, default in (
            (
                'primary_domain',
                'Домен сайта',
                saved.get('primary_domain', ui.installation().get('domain', '')),
            ),
            (
                'admin_domain',
                'Отдельный домен панели администратора',
                saved.get('admin_domain', ''),
            ),
        ):
            values[key] = ui.input_value(label, default, schema.domain)
        values['web_url'] = ui.input_value(
            'HTTPS-адрес существующего Element Web',
            saved.get('web_url', ''),
            lambda value: vault_address(value).rstrip('/') + '/',
        )
        proxy = existing.get('proxy', {})
        caddy = saved.get(
            'caddyfile',
            str(ui.root / proxy['config_directory'] / 'Caddyfile')
            if proxy.get('config_directory')
            else '',
        )
        for key, label, default in (
            ('caddyfile', 'Принятый Caddyfile', caddy),
            ('site_directory', 'Каталог сайта', saved.get('site_directory', '/srv/misbot-site')),
            ('image_archive', 'Принятый архив образов', str(folder / 'images.tar')),
            ('image_manifest', 'Проверенный manifest образов', str(folder / 'images.json')),
        ):
            values[key] = feature_path(ui, key, label, default)
        if saved.get('telegram_runtime'):
            values['telegram_runtime'] = saved['telegram_runtime']
        ui.view.message(
            'Архив и manifest должны быть подготовлены для этой установки. Их соответствие проверит backend перед загрузкой образов.'
        )
    if not confirm(
        ui, item, [('Сайт', values['primary_domain']), ('Панель', values['admin_domain'])]
    ):
        return 0
    with profile_file(ui, values, json_file=True) as profile:
        code = ui.run(item.path, config=profile)
    return finish(ui, item, code)


def execute(ui, item):
    ui.view.heading(item.title, item.hint)
    if supplied(ui).get('config') and item.path not in (
        'backup external create',
        'backup schedule external',
        'backup fetch',
    ):
        profile = Path(supplied(ui)['config'])
        if not confirm(ui, item, [('Сохранённый профиль', str(profile))]):
            return 0
        return finish(ui, item, ui.run(item.path, config=profile))
    handlers = {
        'backup external create': execute_backup,
        'backup schedule external': execute_backup,
        'backup fetch': execute_fetch,
        'storage database connect': execute_database,
        'credentials s3 rotate': execute_s3_rotation,
        'credentials vault connect': execute_vault,
        'credentials vault auth': execute_vault,
        'features enable': execute_features,
    }
    return handlers[item.path](ui, item)
