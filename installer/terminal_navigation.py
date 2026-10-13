"""Contextual selections for the terminal interface; operations stay in their backends."""

import os
from pathlib import Path
import re
import stat

import ui_actions


def pending_actions(ui):
    root = ui.root
    markers = {
        'configuration-transition.json': ('config recover', 'config rollback'),
        'homepage-transition.json': ('homepage recover', 'homepage rollback'),
        'federation-transition.json': ('federation recover', 'federation rollback'),
        'backup-writers.json': ('backup recover',),
        'database-transition.json': ('storage database recover', 'storage database abort'),
        'database-password-transition.json': ('credentials database recover',),
        's3-credentials-transition.json': ('credentials s3 recover', 'credentials s3 cleanup'),
        'update-journal.json': ('update recover',),
        'secrets/user-password-pending.json': ('users password recover',),
        'secrets/admin-access-pending.json': ('admin access recover',),
        '.misbot/backup-writers.json': ('backup recover',),
        '.misbot/s3/media-journal.json': ('storage recover',),
        '.misbot/source-update.json': ('update recover',),
        '.misbot/features/enable.json': ('features recover',),
        '.misbot/features/homepage.json': ('homepage recover',),
    }
    paths = {
        action
        for marker, actions in markers.items()
        if (root / marker).exists() or (root / marker).is_symlink()
        for action in actions
    }
    for flag, action in (
        ('credential_transition_pending', 'credentials admin recover'),
        ('media_transition_pending', 'storage recover'),
        ('restore_pending', 'backup activate'),
        ('migration_frozen', 'backup move abort'),
    ):
        if ui.installation().get(flag):
            paths.add(action)
    if cleanup_catalogs(ui.root):
        paths.add('backup external cleanup')
    return [
        item
        for _, items in ui_actions.MENUS.values()
        for item in items
        if item.path in paths and ui.supported(item.path)
    ]


def cleanup_catalogs(root):
    import terminal_forms

    catalogs = []
    for path in sorted((root / 'stages/external-backups').glob('*/catalog.json'), reverse=True):
        if not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{32}', path.parent.name):
            continue
        try:
            record = terminal_forms.read_record(path)
        except (OSError, RuntimeError, ValueError):
            continue
        if (
            record.get('format') == 1
            and record.get('upload_verified') is False
            and record.get('cleanup_delete_requested') is not True
        ):
            catalogs.append((path, record))
    return catalogs


def cleanup_arguments(ui, supplied):
    """Select only this installation's unverified uploads with the matching saved profile."""
    import s3_config
    import external_backup

    args = dict(supplied)
    profile = Path(args.get('config') or ui.root / 'settings/backup.env')
    if not profile.exists():
        return args
    values = s3_config.read_profile(profile, 'backup')
    args['config'] = str(profile)
    if args.get('catalog'):
        return args
    rows = []
    identity = external_backup.storage_identity(values)
    for path, record in cleanup_catalogs(ui.root):
        expected = values['S3_PREFIX'] + path.parent.name + '.matrix.enc'
        if record.get('storage') == identity and record.get('object_key') == expected:
            rows.append((str(path), path.parent.name, 'Неподтверждённая загрузка своей копии'))
    if rows:
        args['catalog'] = choose_value(ui, 'Какую незавершённую загрузку очистить?', rows)
    return args


def choose_value(ui, title, rows, *, manual=False):
    """Keep opaque identifiers out of user input; manual entries remain an explicit option."""
    from terminal_ui import Back

    choices = [(str(i), label, hint) for i, (_, label, hint) in enumerate(rows, 1)]
    if manual:
        choices.append(('m', 'Ввести другой адрес или имя', ''))
    if not choices:
        ui.view.message('Подходящих записей нет.')
        raise Back()
    selected = ui.choose(title, choices, keep_hints=True)
    if selected == '0':
        raise Back()
    return None if selected == 'm' else rows[int(selected) - 1][0]


def operation_selection(ui, command, key):
    """Return a known argument, or None when a new user value is appropriate."""
    from terminal_ui import Back

    if key == 'request':
        if ui.run('federation requests', json=True):
            raise Back()
        rows = []
        for record in ui.operation_records:
            for item in record.get('requests', []):
                if not isinstance(item, dict) or not re.fullmatch(
                    r'MB-[0-9a-f]{32}', str(item.get('id', ''))
                ):
                    continue
                action = command.path.rsplit(' ', 1)[-1]
                if action in ('approve', 'reject', 'cancel'):
                    direction = 'outgoing' if action == 'cancel' else 'incoming'
                    if (
                        item.get('direction') != direction
                        or item.get('state') != 'pending'
                        or item.get('local_applied')
                        or item.get('remote_applied')
                    ):
                        continue
                rows.append(
                    (
                        item['id'],
                        item.get('server', 'Сервер'),
                        str(item.get('direction', '')) + ' · ' + str(item.get('state', '')),
                    )
                )
        return choose_value(ui, 'Выберите заявку', rows)
    if key == 'server' and command.path in ('federation remove', 'federation check'):
        if ui.run('federation servers', json=True):
            raise Back()
        servers = [server for r in ui.operation_records for server in r.get('servers', [])]
        return choose_value(
            ui,
            'Выберите сервер',
            [(v, v, '') for v in servers if isinstance(v, str)],
            manual=command.path == 'federation check',
        )
    if key == 'mxid' and command.path == 'admin access remove':
        if ui.run('admin access list'):
            raise Back()
        rows = []
        for record in ui.operation_records:
            protected = {record.get('primary'), '@matrix-admin:' + ui.installation()['domain']}
            rows.extend(
                (mxid, mxid, '')
                for mxid in record.get('administrators', [])
                if isinstance(mxid, str) and mxid not in protected
            )
        return choose_value(ui, 'У кого отозвать права?', rows)
    if key == 'mxid' and command.path == 'admin access add':
        return account_selection(ui, administrators=True)
    return None


def account_selection(ui, administrators=False):
    import operation_inputs

    try:
        with ui.view.busy('Получаем список аккаунтов сервера'):
            accounts = operation_inputs.local_accounts(ui.root)
    except (OSError, RuntimeError, ValueError, KeyError):
        ui.view.message('Список аккаунтов пока недоступен. Можно ввести локальное имя.')
        return None
    protected = {'matrix-admin', 'referral-bot', 'server-guide', ui.saved_admin_tag()}
    rows = [
        (item['mxid'] if administrators else item['username'], item['mxid'], '')
        for item in accounts
        if item['username'] not in protected and (administrators or not item['admin'])
    ]
    if not rows:
        return None
    return choose_value(ui, 'Выберите аккаунт', rows, manual=True)


def schedule_calendar(ui, default=''):
    from terminal_ui import Back
    import operation_inputs

    if not default:
        default = operation_inputs.saved_calendar(ui.root)
    choices = [
        ('1', 'Каждый день', ''),
        ('2', 'Раз в неделю', ''),
        ('3', 'Задать выражение systemd', 'Дополнительные настройки'),
    ]
    if default:
        choices.insert(0, ('s', 'Сохранить текущее расписание', default))
    selected = ui.choose('Когда создавать копии?', choices, keep_hints=True)
    if selected == '0':
        raise Back()
    if selected == 's':
        return ui.calendar_value(default)
    if selected == '3':
        return ui.input_value(
            'Расписание systemd', default or '*-*-* 03:00:00 UTC', ui.calendar_value
        )
    weekday = ''
    if selected == '2':
        days = [
            ('Mon', 'Понедельник'),
            ('Tue', 'Вторник'),
            ('Wed', 'Среда'),
            ('Thu', 'Четверг'),
            ('Fri', 'Пятница'),
            ('Sat', 'Суббота'),
            ('Sun', 'Воскресенье'),
        ]
        weekday = choose_value(ui, 'День недели', [(a, b, '') for a, b in days]) + ' '

    def clock(value):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
            raise ValueError('Введите время ЧЧ:ММ, например 03:00.')
        return value

    hour = ui.input_value('Время, ЧЧ:ММ', '03:00', clock)
    zone = ui.choose(
        'Часовой пояс',
        [
            ('1', 'UTC', ''),
            ('2', 'Часовой пояс сервера', ''),
            ('3', 'Другой часовой пояс', 'Например Europe/Moscow'),
        ],
    )
    if zone == '0':
        raise Back()
    timezone = ' UTC' if zone == '1' else ''
    if zone == '3':
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        def valid_zone(value):
            try:
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError(
                    'Укажите известный часовой пояс, например Europe/Moscow.'
                ) from None
            return value

        timezone = ' ' + ui.input_value('Часовой пояс', '', valid_zone)
    return ui.calendar_value(weekday + '*-*-* ' + hour + ':00' + timezone)


def trusted_bundle(source, root):
    """Suggest only the already selected unpacked runtime, never the installed wrapper."""
    import update

    source = Path(source)
    if source == root or (source / 'command.json').exists():
        return None
    if not source.is_absolute() or source.resolve() != source:
        return None
    try:
        entries, _ = update.validate_manifest(source)
        update.release(source)
        for path in (source, source / 'SHA256SUMS', *entries.values()):
            info = path.lstat()
            if (
                info.st_uid != os.geteuid()
                or info.st_mode & 0o022
                or stat.S_ISLNK(info.st_mode)
                or path.resolve() != path
            ):
                return None
        return source
    except (OSError, RuntimeError, ValueError, KeyError):
        return None
