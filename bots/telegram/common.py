"""Small, shared primitives. Never include HTTP bodies or URLs in errors."""

import ipaddress
import json
import os
import re
import socket
from pathlib import Path

MAX_BODY = 32768


class BotError(Exception):
    pass


def domain(value):
    if not isinstance(value, str) or value != value.strip() or len(value) > 253:
        raise BotError('Укажите домен Matrix-сервера, например matrix.org.')
    try:
        result = value.lower().encode('idna').decode('ascii')
    except UnicodeError:
        raise BotError(
            'Не удалось распознать домен. Укажите домен Matrix-сервера, например matrix.org.'
        )
    try:
        ipaddress.ip_address(result)
    except ValueError:
        pass
    else:
        raise BotError(
            'Для этого действия нужен домен Matrix-сервера. Укажите домен вместо IP-адреса.'
        )
    labels = result.split('.')
    if len(labels) < 2 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in labels
    ):
        raise BotError('Укажите только домен сервера, без https://, пути и порта.')
    return result


def read_json(path):
    return json.loads(Path(path).read_text())


def private_json(path, data):
    path = Path(path)
    # Update in place: Docker single-file bind mounts must retain their inode.
    with path.open('w') as handle:
        path.chmod(0o600)
        json.dump(data, handle, ensure_ascii=False, indent=2)


def receive(stream):
    data = stream.readline(MAX_BODY + 1)
    if len(data) > MAX_BODY or not data.endswith(b'\n'):
        raise BotError('Запрос не принят: превышен размер или не завершено сообщение.')
    return json.loads(data)


def bridge_call(request, path=None, timeout=100):
    path = path or os.environ.get('MISBOT_CONTROL_SOCKET', '/run/control/control.sock')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(path)
        client.sendall(json.dumps(request).encode() + b'\n')
        result = receive(client.makefile('rb'))
    if not result.get('ok'):
        raise BotError(result.get('error', 'Не удалось выполнить запрос. Повторите позже.'))
    return result['result']
