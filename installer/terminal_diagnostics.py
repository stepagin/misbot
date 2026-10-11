"""Safe presentation of availability records; the CLI JSON remains unchanged."""

from collections.abc import Sequence
from dataclasses import dataclass
import json

CHECKS = {
    'dns': 'DNS',
    'homepage_routes': 'Главная страница и маршруты',
    'discovery': 'Настройки подключения Matrix',
    'federation': 'Настройки федерации',
    'synapse': 'API Matrix',
    'oauth_metadata': 'Настройки входа Matrix',
    'mas_discovery': 'Настройки службы входа',
    'mas_health': 'Служба входа',
    'web': 'Веб-чат',
    'admin': 'Веб-панель администратора',
    'rtc_jwt': 'Авторизация звонков',
    'legacy_login_disabled': 'Отключение устаревшего входа',
    'synapse_admin_protected': 'Защита API администратора Matrix',
    'mas_admin_protected': 'Защита API службы входа',
    'turn_tls': 'TURN для звонков, TCP 443',
    'service_inventory': 'Состав служб',
    'existing_checks': 'Проверки подключённого сервера',
}
SERVICES = {
    'postgres': 'PostgreSQL',
    'synapse': 'Matrix',
    'mas': 'Служба входа',
    'jwt': 'Авторизация звонков',
    'livekit': 'Звонки',
    'web': 'Веб-чат',
    'admin': 'Веб-панель',
    'caddy': 'Caddy',
    'tls-router': 'HTTPS/TURN (HAProxy)',
    'referral-control': 'Управление приглашениями',
    'referral-bot': 'Реферальный бот',
    'federation-bot': 'Панель и серверный помощник',
}
FAILURES = {
    'connection_refused': 'Соединение с TCP 443 отклонено. Подключение к этому адресу недоступно.',
    'timeout': 'Ответ не получен за отведённое время.',
    'dns_resolution': 'Не удалось определить адрес через DNS.',
    'dns_validation': 'Проверка DNS не пройдена. Доступность адресов не подтверждена.',
    'tls_certificate': 'Не удалось проверить HTTPS-сертификат.',
    'tls_connection': 'Защищённое соединение не установлено.',
    'network_unreachable': 'Нет сетевого маршрута до сервера.',
    'unexpected_response': 'Ответ получен, но его содержимое не соответствует настройкам.',
    'check_failed': 'Проверка не подтвердила доступность. Причина не уточнена.',
}
LEGACY_ERRORS = {
    'ConnectionRefusedError': 'connection_refused',
    'TimeoutError': 'timeout',
    'gaierror': 'dns_resolution',
    'SSLCertVerificationError': 'tls_certificate',
    'SSLError': 'tls_connection',
    'JSONDecodeError': 'unexpected_response',
}


@dataclass(frozen=True)
class CheckResult:
    key: str
    label: str
    message: str
    passed: bool
    kind: str = 'check'


def parse_checks(output: str) -> list[CheckResult]:
    """Only known labels, fixed messages and bounded numbers reach the UI."""
    results = []
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        name = record.get('check')
        if not isinstance(name, str):
            continue
        service = record.get('name')
        if name == 'service' and isinstance(service, str) and service in SERVICES:
            observed_state = record.get('state')
            running = observed_state == 'running'
            healthy = record.get('health') in ('', 'healthy')
            state = {
                'running': 'Работает' if healthy else 'Работает, готовность не подтверждена',
                'restarting': 'Перезапускается',
                'exited': 'Остановлена',
                'created': 'Создана, но не запущена',
                'paused': 'Приостановлена',
            }.get(
                observed_state if isinstance(observed_state, str) else '',
                'Состояние не определено',
            )
            results.append(
                CheckResult(
                    'service:' + service, SERVICES[service], state, running and healthy, 'service'
                )
            )
            continue
        if name not in CHECKS or type(record.get('passed')) is not bool:
            continue
        passed = record['passed']
        message = 'Проверка пройдена.' if passed else FAILURES['check_failed']
        if not passed:
            code = record.get('failure_code')
            if not isinstance(code, str):
                error_type = record.get('error_type')
                code = (
                    LEGACY_ERRORS.get(error_type, 'check_failed')
                    if isinstance(error_type, str)
                    else 'check_failed'
                )
            message = FAILURES.get(code, FAILURES['check_failed'])
            if code == 'http_status' or 'status' in record:
                status, expected = record.get('status'), record.get('expected')
                if all(type(n) is int and 100 <= n <= 599 for n in (status, expected)):
                    message = 'HTTP ' + str(status) + '; ожидается ' + str(expected) + '.'
                    if status == expected:
                        message += ' Содержимое ответа не соответствует настройкам.'
        if name == 'service_inventory' and not passed:
            missing = record.get('missing')
            if isinstance(missing, list):
                labels = [
                    SERVICES[item] for item in missing if isinstance(item, str) and item in SERVICES
                ]
                if labels:
                    message = 'Не созданы службы: ' + ', '.join(labels) + '.'
        kind = 'summary' if name in ('service_inventory', 'existing_checks') else 'check'
        results.append(CheckResult(name, CHECKS[name], message, passed, kind))
        if name == 'existing_checks' and isinstance(record.get('checks'), list):
            for index, item in enumerate(record['checks'], 1):
                if isinstance(item, dict) and type(item.get('passed')) is bool:
                    results.append(
                        CheckResult(
                            'existing_checks:' + str(index),
                            'Проверка подключённого сервера №' + str(index),
                            'Проверка пройдена.' if item['passed'] else FAILURES['check_failed'],
                            item['passed'],
                        )
                    )
    return results


def check_summary(results: Sequence[CheckResult], code: int) -> str:
    checks = [result for result in results if result.kind == 'check']
    passed = sum(result.passed for result in checks)
    failed = len(checks) - passed
    if code == 130:
        return 'Проверка прервана. Завершение не подтверждено.'
    if not checks:
        return 'Подробности проверки не получены. Откройте состояние сервера.'
    return 'Успешно: ' + str(passed) + '. Не пройдено: ' + str(failed) + '.'
