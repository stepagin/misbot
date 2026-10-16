"""Safe presentation of availability records; the CLI JSON remains unchanged."""

from collections.abc import Sequence
from dataclasses import dataclass
import json
import math

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
PENDING = {
    'migration_frozen': 'Перенос: исходный сервер остановлен',
    'restore_pending': 'Копия восстановлена; сервер ещё не активирован',
    'credential_transition_pending': 'Смена пароля администратора',
    'media_transition_pending': 'Перенос медиа',
    'backup_recovery_pending': 'Возврат служб после резервной копии',
    'database_recovery_pending': 'Перенос баз данных',
    'database_password_recovery_pending': 'Смена паролей баз данных',
    's3_credential_recovery_pending': 'Смена ключей S3',
    'update_recovery_pending': 'Обновление Misbot',
    'configuration_recovery_pending': 'Применение настроек',
    'federation_recovery_pending': 'Изменение федерации',
    'homepage_recovery_pending': 'Применение настроек сайта',
    'user_password_recovery_pending': 'Смена пароля пользователя',
    'admin_access_recovery_pending': 'Изменение доступа администраторов',
}


def resource_messages(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []

    def number(key: str) -> float | None:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        return float(item) if math.isfinite(item) and item >= 0 else None

    messages = []
    cpu = number('cpu_used_percent')
    messages.append(f'CPU: {cpu:g} %' if cpu is not None and cpu <= 100 else 'CPU: нет данных')
    load, cores = number('cpu_load'), number('cpu_count')
    if load is not None and cores is not None:
        messages.append(f'Нагрузка за минуту: {load:g}; процессоров: {cores:g}')
    for title, prefix in (('RAM', 'memory'), ('Диск установки', 'disk'), ('Swap', 'swap')):
        total, used = number(prefix + '_total_bytes'), number(prefix + '_used_bytes')
        if total is None or used is None or used > total:
            messages.append(title + ': нет данных')
        elif total == 0:
            messages.append(title + ': не используется')
        else:
            messages.append(
                f'{title}: {used / 1024**3:.1f} / {total / 1024**3:.1f} ГиБ '
                f'({100 * used / total:.0f} %)'
            )
    uptime = number('uptime_seconds')
    if uptime is not None:
        messages.append(
            f'Без перезагрузки: {int(uptime // 86400)} д. {int(uptime % 86400 // 3600)} ч.'
        )
    return messages


def status_messages(record: dict[str, object]) -> list[str]:
    messages = resource_messages(record.get('resources'))
    if record.get('operation_running') is True:
        messages.append('Выполняется операция. Дождитесь завершения; повторный запуск пока занят.')
    elif record.get('operation_running') is False:
        messages.append('Активных операций нет.')
    else:
        messages.append('Состояние активных операций не получено.')
    if record.get('decommissioned') is True:
        messages.append('Сервер удалён частично. Сохранённые данные защищены от нового запуска.')
    elif record.get('installation_complete') is False:
        messages.append(
            'Установка не завершена. Выберите «Запустить сервер», затем «Проверить доступность».'
        )
    pending = [label for key, label in PENDING.items() if record.get(key) is True]
    if pending:
        messages.append('Требуют продолжения:')
        messages.extend('• ' + label for label in pending)
        messages.append('Откройте «Продолжить прерванную операцию» и выберите указанный этап.')
    elif record.get('installation_complete') is True:
        messages.append('Незавершённых этапов нет.')
    return messages


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
        if name in ('existing_status', 'server_status'):
            for container in record.get('containers', []):
                if isinstance(container, dict):
                    results.extend(
                        parse_checks(
                            json.dumps(
                                {
                                    'check': 'service',
                                    'name': container.get('name'),
                                    'state': 'running'
                                    if container.get('running') is True
                                    else 'exited',
                                    'health': ''
                                    if container.get('health') == 'none'
                                    else container.get('health', ''),
                                }
                            )
                        )
                    )
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
                'missing': 'Не создана; запуск не завершён',
            }.get(
                observed_state if isinstance(observed_state, str) else '',
                'Состояние не определено',
            )
            exit_code = record.get('exit_code')
            if observed_state == 'exited' and type(exit_code) is int and exit_code != 0:
                state += ' (код ' + str(exit_code) + ')'
                if exit_code == 137:
                    state += '; процесс завершён принудительно'
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
    # Readiness may report several attempts. Keep their full history in the
    # lifecycle log, but show one current result per check or named service.
    # Replacing dict values preserves the original, stable presentation order.
    return list({result.key: result for result in results}.values())


def check_summary(results: Sequence[CheckResult], code: int) -> str:
    checks = [result for result in results if result.kind == 'check']
    passed = sum(result.passed for result in checks)
    failed = len(checks) - passed
    if code == 130:
        return 'Проверка прервана. Завершение не подтверждено.'
    if not checks:
        return 'Подробности проверки не получены. Откройте состояние сервера.'
    return 'Успешно: ' + str(passed) + '. Не пройдено: ' + str(failed) + '.'
