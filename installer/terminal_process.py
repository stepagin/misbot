"""Drain a child without blocking animation or retaining unbounded output."""

import codecs
import json
import os
import selectors
import time

MAX_LINE = 256 * 1024


def present_output(path, line):
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
    if record.get('status') == 'error' and record.get('explanation'):
        return [str(record['explanation'])]
    if path == 'admin bot show':
        return [
            'Бот панели: ' + str(record.get('user_id', 'не выбран')),
            'Администратор: ' + str(record.get('administrator_mxid', 'не выбран')),
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
    return [line]


def read_output(child, emit):
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    pending = ''
    dropping = False
    exited_at = None
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while selector.get_map():
            # A detached grandchild may retain the pipe after the command exits.
            # Drain queued bytes briefly; the command's exit code is authoritative.
            if child.poll() is not None:
                exited_at = time.monotonic() if exited_at is None else exited_at
                if time.monotonic() - exited_at >= 1:
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
