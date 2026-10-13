"""Bounded, read-only Telegram OIDC connectivity checks through the selected proxy."""

import json
import subprocess


DISCOVERY_URL = 'https://oauth.telegram.org/.well-known/openid-configuration'
ATTEMPTS = 3
REQUEST_TIMEOUT = 10


class LoginCheckError(RuntimeError):
    """Safe diagnostic, without credentials, URLs supplied by users or remote bodies."""


def check(values):
    """Check repeated TLS/HTTP access; OIDC credentials require a browser code flow."""
    proxy = values.get('SOCKS5_CONFIG', '')
    if proxy:
        if not proxy.startswith('socks5://') or any(ord(c) < 32 for c in proxy):
            raise LoginCheckError('Некорректный адрес SOCKS5. Исправьте настройки прокси.')
        # Telegram DNS resolution must happen on the proxy, as in generated MAS settings.
        proxy = 'socks5h://' + proxy.split('://', 1)[1]
    escaped = proxy.replace('\\', '\\\\').replace('"', '\\"')
    # stdin keeps proxy credentials out of process arguments and exceptions. Disable
    # curlrc and environment proxy defaults so the test matches the selected route.
    config = f'proxy = "{escaped}"\nnoproxy = ""\n'
    for attempt in range(ATTEMPTS):
        try:
            result = subprocess.run(
                [
                    'curl',
                    '--disable',
                    '--config',
                    '-',
                    '--silent',
                    '--fail',
                    '--proto',
                    '=https',
                    '--connect-timeout',
                    '5',
                    '--max-time',
                    str(REQUEST_TIMEOUT),
                    '--max-filesize',
                    '65536',
                    DISCOVERY_URL,
                ],
                input=config,
                capture_output=True,
                text=True,
                timeout=REQUEST_TIMEOUT + 2,
                check=False,
            )
        except FileNotFoundError:
            raise LoginCheckError(
                'Для проверки Telegram требуется curl. Установите curl и повторите проверку.'
            ) from None
        except (OSError, subprocess.TimeoutExpired):
            raise LoginCheckError(
                'Telegram не ответил вовремя. Проверьте сеть и SOCKS5, затем повторите проверку.'
            ) from None
        if result.returncode:
            raise LoginCheckError(
                'Проверка Telegram прервана: запрос '
                + str(attempt + 1)
                + ' из '
                + str(ATTEMPTS)
                + ' не выполнен. Проверьте доступ, адрес и пароль SOCKS5, затем повторите.'
            )
        try:
            metadata = json.loads(result.stdout)
            if (
                not isinstance(metadata, dict)
                or metadata.get('issuer') != 'https://oauth.telegram.org'
                or metadata.get('token_endpoint') != 'https://oauth.telegram.org/token'
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise LoginCheckError(
                'Telegram вернул неожиданный ответ OIDC. Проверьте подключение и повторите.'
            ) from None
