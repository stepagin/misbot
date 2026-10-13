"""Drain a child without blocking animation or retaining unbounded output."""

import codecs
import json
import os
from pathlib import Path
import re
import selectors
import signal
import time

from startup_process import process_group_running

MAX_LINE = 256 * 1024


def present_output(path, line):
    messages = primary_output(path, line)
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return messages
    if not isinstance(record, dict):
        return messages
    authentication = record.get('authentication')
    if record.get('check') == 'consistent_backup':
        archive = record.get('archive')
        digest = record.get('sha256')
        if isinstance(archive, str) and Path(archive).is_absolute():
            messages.append('Архив копии: ' + archive)
        if isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            messages.append('SHA256: ' + digest)
    if authentication in ('external-MAS-not-included', 'external-MAS-not-restored'):
        messages.append(
            'Внимание: пользователи, устройства, ключи и данные внешнего MAS '
            + (
                'не входят в эту копию.'
                if authentication == 'external-MAS-not-included'
                else 'не восстановлены из этой копии.'
            )
        )
        messages.append(
            'Сохраните или восстановите соответствующее состояние внешнего MAS отдельно. '
            'Перед активацией Matrix подтвердите его сохранность.'
        )
    if record.get('check') != 'installation_status' and any(
        isinstance(key, str) and (key == 'pending' or key.endswith('_pending')) and value is True
        for key, value in record.items()
    ):
        messages.append('Операция требует продолжения. Сохраните данные и служебные файлы.')
    actions = []
    if isinstance(record.get('next_action'), str):
        actions.append(record['next_action'])
    if isinstance(record.get('next_actions'), list):
        actions.extend(item for item in record['next_actions'] if isinstance(item, str))
    messages.extend('Следующий шаг: ' + action for action in dict.fromkeys(actions))
    return messages


def primary_output(path, line):
    """Present selected safe backend records; external JSON contracts stay intact."""
    if line.startswith('Another installer operation is running for this directory.'):
        return [
            'Для этой установки уже выполняется другая операция. Дождитесь её завершения.',
            'Меню: «Состояние и работа сервера» → «Показать состояние».',
        ]
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return [line]
    if not isinstance(record, dict):
        return [line]
    if str(record.get('message', '')).startswith(
        'Another installer operation is running for this directory.'
    ):
        return present_output(path, record['message'])
    if record.get('check') == 'startup_progress':
        stages = {
            'admission': 'Проверка условий запуска',
            'images': 'Загрузка образов и подготовка настроек',
            'host': 'Проверка DNS и подготовка сервера',
            'database': 'Запуск базы данных',
            'applications': 'Запуск Matrix и MAS',
            'administrator': 'Подготовка администратора',
            'referral': 'Запуск бота приглашений',
            'federation': 'Запуск административного бота',
            'https': 'HTTPS и звонки',
            'verification': 'Итоговая проверка',
            'complete': 'Сервер запущен',
        }
        completed, total = record.get('completed'), record.get('total')
        if type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0:
            return [
                f'Запуск: {completed * 100 // total}% · этапы {completed}/{total} · '
                + stages.get(record.get('stage'), 'Подготовка')
            ]
        return []
    if record.get('check') == 'command_progress':
        command = record.get('command')
        labels = {
            'docker pull': 'Загрузка образа Docker',
            'docker build': 'Сборка образа Docker',
            'docker': 'Docker',
            'systemctl': 'Системная служба',
            'apt-get': 'Установка системных пакетов',
            'curl': 'Загрузка файла',
            'python3': 'Подготовка и проверка настроек',
            'dd': 'Подготовка swap',
        }
        seconds = record.get('elapsed_seconds')
        limit = record.get('timeout_seconds')
        if type(seconds) is int and type(limit) is int:
            return [f'{labels.get(command, "Команда")}: {seconds} с · предел {limit} с']
        return []
    if record.get('check') in ('download_progress', 'download_layer'):
        layer = record.get('layer', '')
        if not isinstance(layer, str) or not re.fullmatch(r'[a-f0-9]{12,64}', layer):
            return []
        if record['check'] == 'download_layer':
            actions = {
                'Pulling fs layer': 'ожидает загрузки',
                'Waiting': 'ожидает загрузки',
                'Download complete': 'загружен',
                'Pull complete': 'готов',
                'Already exists': 'уже есть',
            }
            action = actions.get(record.get('action'))
            return ['Слой ' + layer[:12] + ': ' + action] if action else []
        current, total = record.get('current'), record.get('total')
        if not (
            type(current) is int and type(total) is int and 0 <= current <= total and total > 0
        ):
            return []
        action = 'Загрузка' if record.get('action') == 'Downloading' else 'Распаковка'
        message = f'{action} слоя {layer[:12]}: {current * 100 // total}% · {current / 1e6:.1f}/{total / 1e6:.1f} МБ'
        speed = record.get('bytes_per_second')
        if type(speed) is int and speed >= 0:
            message += f' · {speed / 1e6:.1f} МБ/с'
        return [message]
    if record.get('check') == 'image_ready':
        role = record.get('role')
        return ['Образ готов: ' + role] if isinstance(role, str) else []
    if record.get('check') == 'waiting_for_service':
        return [
            'Ожидаем готовности: '
            + str(record.get('service', 'служба'))
            + ' · предел '
            + str(record.get('timeout_seconds', '?'))
            + ' с'
        ]
    if record.get('check') == 'installation_status':
        from terminal_diagnostics import status_messages

        return status_messages(record)
    if record.get('check') == 'service':
        from terminal_diagnostics import parse_checks

        return [row.label + ': ' + row.message for row in parse_checks(line)]
    if record.get('check') in ('existing_status', 'server_status'):
        from terminal_diagnostics import parse_checks

        messages = []
        for container in record.get('containers', []):
            if not isinstance(container, dict):
                continue
            encoded = json.dumps(
                {
                    'check': 'service',
                    'name': container.get('name'),
                    'state': 'running' if container.get('running') is True else 'exited',
                    'health': ''
                    if container.get('health') == 'none'
                    else container.get('health', ''),
                }
            )
            messages.extend(row.label + ': ' + row.message for row in parse_checks(encoded))
        if record.get('backup_pending'):
            messages.append(
                'Резервная копия прервана. Откройте продолжение резервного копирования.'
            )
        return messages or ['Сведения о службах не получены.']
    if record.get('check') in ('existing_storage_status', 'storage_status'):
        return [
            'Медиа: ' + ('S3' if record.get('media_s3') else 'на этом сервере') + '.',
            'Копии: ' + ('S3' if record.get('backup_s3') else 'локальные') + '.',
            'Перенос медиа: '
            + ('требует продолжения' if record.get('media_pending') else 'не выполняется')
            + '.',
            'Внешняя копия: '
            + ('проверена' if record.get('last_backup_verified') else 'не подтверждена')
            + '.',
        ]
    if record.get('check') == 'backup_schedule_status':
        if 'configured' in record:
            return [
                'Расписание копий: '
                + ('настроено' if record.get('configured') else 'не настроено')
                + '.',
                'Автоматические копии: '
                + ('включены' if record.get('enabled') else 'выключены')
                + '.',
            ]
        if record.get('resource') == 'timer':
            return [
                'Следующая копия: '
                + str(record.get('NextElapseUSecRealtime') or 'не запланирована')
                + '.'
            ]
        return [
            'Последний запуск копирования: '
            + ('успешно' if record.get('Result') == 'success' else 'не подтверждён')
            + '.'
        ]
    if record.get('schema') == 1 and isinstance(record.get('stages'), list):
        stages = {
            'configuration': 'Настройки',
            'connection': 'Соединение',
            'authentication': 'Ключи доступа',
            'bucket': 'Бакет',
            'write': 'Запись',
            'read': 'Чтение',
            'cleanup': 'Удаление проверочного объекта',
        }
        return [
            ('✓ ' if item.get('status') == 'passed' else '× ')
            + stages.get(item.get('stage'), 'Проверка')
            + ': '
            + str(item.get('explanation', 'Результат не получен.'))
            for item in record['stages']
            if isinstance(item, dict)
        ] + ['Итог: ' + str(record.get('conclusion', 'Проверка не завершена.'))]
    if record.get('status') == 'error' and record.get('explanation'):
        return [str(record['explanation'])]
    if path == 'admin bot show':
        return [
            'Бот панели: ' + str(record.get('user_id', 'не выбран')),
            'Администратор: ' + str(record.get('administrator_mxid', 'не выбран')),
        ]
    if path.startswith('admin access ') and isinstance(record.get('administrators'), list):
        return ['Администраторы сервера:'] + [
            str(index) + '. ' + str(mxid) for index, mxid in enumerate(record['administrators'], 1)
        ]
    if path == 'federation servers':
        servers = record.get('servers', [])
        return ['Разрешённые серверы:'] + ([str(server) for server in servers] or ['Список пуст.'])
    if path == 'federation operations':
        operations = record.get('operations', [])
        return [
            str(item.get('id', '')) + ': ' + str(item.get('state', 'неизвестно'))
            for item in operations
        ] or ['Сохранённых операций бота нет.']
    if path == 'users invitation list':
        invitations = record.get('invitations', [])
        states = {
            'valid': 'действует',
            'used': 'использовано',
            'expired': 'истекло',
            'revoked': 'отозвано',
            'pending': 'ожидает подтверждения',
        }
        return [
            str(item.get('id', ''))
            + ' · '
            + str(item.get('issuer', 'администратор'))
            + ' · '
            + states.get(item.get('state'), str(item.get('state', 'неизвестно')))
            + ' · до '
            + str(item.get('expires_at') or 'без ограничения')
            for item in invitations
        ] or ['Приглашений пока нет.']
    if record.get('check') == 'failed':
        # RuntimeError messages are deliberate backend guidance; provider errors
        # are never repeated verbatim.
        if record.get('error_type') == 'RuntimeError' and record.get('message'):
            return [str(record['message'])]
        return ['Операция не завершена. Проверьте состояние и повторите после исправления причины.']
    states = {
        'applied': 'применено',
        'unchanged': 'изменения не требуются',
        'healthy': 'работает',
        'stopped': 'остановлено',
        'rolled_back': 'восстановлены прежние настройки',
        'valid': 'проверено',
        'failed': 'не завершено',
        'complete': 'завершено',
        'pending': 'ожидает продолжения',
    }
    state = record.get('status') or record.get('state')
    if isinstance(state, str):
        messages = ['Результат: ' + states.get(state, state.replace('_', ' ')) + '.']
        if record.get('explanation'):
            messages.append(str(record['explanation']))
        return messages
    if record.get('check'):
        from ui_actions import MENUS

        name = str(record['check']).replace('_', ' ')
        title = next(
            (item.title for _, actions in MENUS.values() for item in actions if item.path == path),
            'Операция',
        )
        labels = {
            'installation running': 'Сервер запущен',
            'referral bot ready': 'Бот приглашений готов',
            'configuration valid': 'Настройки проверены',
            'release applied': 'Обновление применено',
            'consistent backup': 'Создана согласованная копия Matrix',
            'restore staged': 'Копия восстановлена; сервер ещё не активирован',
        }
        if record.get('passed') is False:
            return [
                'Не завершено: ' + title + '.',
                'Проверьте состояние сервера; данные и настройки сохранены.',
            ]
        if name in labels:
            return [labels[name] + '.']
        if record.get('passed') is True:
            return ['Подтверждено: ' + title + '.']
        return ['Сведения об операции: ' + title + '.']
    return ['Результат получен.']


def read_output(child, emit):
    return drain_output(child, emit, False)


def read_interrupted_output(child, emit, timeout=None):
    return drain_output(child, emit, True, timeout=timeout)


def drain_output(child, emit, wait_descendants, timeout=None):
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    pending = ''
    dropping = False
    exited_at = None
    expires = None if timeout is None else time.monotonic() + timeout
    killed = False
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while selector.get_map() or (
            wait_descendants and (child.poll() is None or process_group_running(child))
        ):
            if expires is not None and time.monotonic() >= expires and not killed:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                killed = True
                emit(
                    'Прерванный запуск завершён принудительно. Данные и файлы восстановления '
                    'не удалены; проверьте состояние перед повтором.'
                )
            # A detached grandchild may retain the pipe after the command exits.
            # Drain queued bytes briefly; the command's exit code is authoritative.
            if child.poll() is not None:
                exited_at = time.monotonic() if exited_at is None else exited_at
                if time.monotonic() - exited_at >= 1 and (
                    not wait_descendants or not process_group_running(child)
                ):
                    if pending and not dropping:
                        emit(pending)
                    break
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    pending += decoder.decode(b'', final=True)
                    if pending and not dropping:
                        emit(pending)
                    continue
                pending += decoder.decode(chunk).replace('\r', '\n')
                while '\n' in pending:
                    line, pending = pending.split('\n', 1)
                    if not dropping and line:
                        if len(line) <= MAX_LINE:
                            emit(line)
                        else:
                            emit(
                                'Строка результата слишком велика для интерфейса. Повторите просмотр через CLI.'
                            )
                    dropping = False
                # Never split JSON or expose fragments of a structured record.
                if len(pending) > MAX_LINE:
                    if not dropping:
                        emit(
                            'Строка результата слишком велика для интерфейса. Повторите просмотр через CLI.'
                        )
                    pending = ''
                    dropping = True
    return child.wait()
