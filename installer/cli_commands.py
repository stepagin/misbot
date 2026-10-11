"""One command vocabulary shared by CLI, help, documentation and future UI."""

from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path


@dataclass(frozen=True)
class Command:
    path: str
    backend: str
    arguments: tuple[str, ...] = ()
    purpose: str = ''
    stops: str = 'Нет.'
    data: str = 'Данные и ключи сохраняются.'
    installed: bool = True


COMMANDS = [
    Command(
        'admin bot show',
        'admin-bot-show',
        purpose='Показать сохранённые теги панели и её единственного администратора.',
    ),
    Command(
        'admin bot set',
        'admin-bot-set',
        ('tag',),
        'Выбрать тег до создания бота; созданную identity переименовать нельзя.',
    ),
    Command(
        'update check',
        'release-check',
        purpose='Сравнить установленную версию сервера с последним стабильным GitHub Release.',
    ),
    Command(
        'update latest',
        'release-update',
        purpose='Загрузить готовый проверенный GitHub Release и применить с проверкой диска.',
        stops='Сервер временно остановится.',
    ),
    Command(
        'features status',
        'features-status',
        purpose='Показать дополнительные компоненты подключённой существующей установки.',
    ),
    Command(
        'features enable',
        'features-enable',
        ('config',),
        'Подключить применимые компоненты Misbot по проверенному feature plan; только adopted backend.',
    ),
    Command(
        'features recover',
        'features-recover',
        purpose='Вернуть публичные маршруты, сохраняя новые bot accounts, sessions и crypto.',
    ),
    Command(
        'homepage show', 'homepage-show', purpose='Показать режим, тексты и публичные ссылки сайта.'
    ),
    Command(
        'homepage check',
        'homepage-check',
        purpose='Проверить предложенные поля и Caddy без применения; внешний сайт не проверяется.',
    ),
    Command(
        'homepage apply',
        'homepage-apply',
        purpose='Применить только настройки сайта после проверки, с checkpoint и rollback.',
        stops='Пересоздаётся только Caddy; короткий перерыв HTTPS.',
    ),
    Command(
        'homepage recover',
        'homepage-recover',
        purpose='Завершить прерванное применение и проверить служебные маршруты.',
        stops='Только Caddy.',
    ),
    Command(
        'homepage rollback',
        'homepage-rollback',
        purpose='Вернуть прежнюю конфигурацию сайта из checkpoint.',
        stops='Только Caddy.',
    ),
    Command(
        'federation servers',
        'federation-servers',
        purpose='Показать действующий локальный allowlist.',
    ),
    Command(
        'federation check',
        'federation-check',
        ('server',),
        'Проверить Misbot discovery/TLS; факт федеративных комнат отдельно.',
    ),
    Command(
        'federation add',
        'federation-add',
        ('server',),
        'Добавить Matrix server_name только в локальный allowlist.',
        stops='Копия, Synapse/Web перезапускаются.',
    ),
    Command(
        'federation remove',
        'federation-remove',
        ('server',),
        'Удалить одну локальную запись, сохраняя identity и restrictive policy.',
        stops='Копия, Synapse/Web перезапускаются.',
    ),
    Command(
        'federation invite',
        'federation-invite',
        ('server',),
        'Отправить подписанную заявку на взаимный allowlist; default 7 дней.',
    ),
    Command(
        'federation peers',
        'federation-peers',
        purpose='Показать согласованные instance/bot identities.',
    ),
    Command(
        'federation requests',
        'federation-requests',
        purpose='Показать сохранённые заявки и частичное применение.',
    ),
    Command(
        'federation operations',
        'federation-operations',
        purpose='Показать сохранённые асинхронные операции административного бота.',
    ),
    Command(
        'federation requests status',
        'federation-request-status',
        ('request',),
        'Показать локальный checkpoint заявки; retry проверяет peer.',
    ),
    Command(
        'federation requests approve',
        'federation-approve',
        ('request',),
        'Одобрить входящую заявку и продолжить обмен.',
        stops='Проверенная копия и перезапуск Synapse/Web при применении.',
    ),
    Command(
        'federation requests reject',
        'federation-reject',
        ('request',),
        'Отклонить входящую заявку; существующий allowlist сохраняется.',
    ),
    Command(
        'federation requests cancel',
        'federation-cancel',
        ('request',),
        'Отменить согласование; уже применённые записи удаляются отдельно.',
    ),
    Command(
        'federation requests retry',
        'federation-retry',
        ('request',),
        'Запросить удалённое состояние и завершить частичное применение.',
    ),
    Command(
        'federation recover',
        'federation-recover',
        purpose='Завершить локальное применение из защищённого checkpoint.',
        stops='Synapse/Web перезапускаются.',
    ),
    Command(
        'federation rollback',
        'federation-rollback',
        purpose='Восстановить прежнюю конфигурацию локальной операции.',
        stops='Synapse/Web перезапускаются.',
    ),
    Command('ui', 'ui', purpose='Открыть русский SSH-мастер или меню; нужен TTY.', installed=False),
    Command(
        'config init',
        'init',
        ('domain',),
        'Создать установку, settings/server.env и приватные внутренние credentials.',
        installed=False,
    ),
    Command('config generate', 'generate', purpose='Сгенерировать конфигурацию без перезапуска.'),
    Command(
        'config validate',
        'configuration-validate',
        purpose='Проверить общую схему приватных настроек без внешних запросов.',
    ),
    Command(
        'config migrate',
        'configuration-migrate',
        purpose='Перенести settings.env в разделённую конфигурацию с приватной копией.',
    ),
    Command(
        'config recover',
        'configuration-recover',
        purpose='Завершить прерванную публикацию конфигурации.',
    ),
    Command(
        'config rollback',
        'configuration-rollback',
        purpose='Вернуть приватную копию прерванной публикации конфигурации.',
    ),
    Command(
        'config apply',
        'configuration-apply',
        purpose='Проверить настройки, создать и проверить копию, сгенерировать и запустить службы.',
        stops='Копия и возможный перезапуск.',
    ),
    Command('config preflight', 'preflight', purpose='Проверить систему, DNS и порты.'),
    Command(
        'config command install',
        'wrapper-install',
        purpose='Установить или обновить системную команду.',
    ),
    Command(
        'config command remove',
        'wrapper-remove',
        purpose='Удалить только принадлежащую установке системную команду.',
    ),
    Command(
        'status', 'status', purpose='Показать службы, pending состояния и дальнейшие действия.'
    ),
    Command(
        'storage status',
        'storage-status',
        purpose='Показать подключение S3 и результаты копирования существующего стека.',
    ),
    Command('check', 'check', purpose='Проверить DNS, HTTPS, discovery, RTC и состояние служб.'),
    Command(
        'start',
        'up',
        purpose='Проверить конфигурацию и запустить службы.',
        stops='Возможен перезапуск изменённых служб.',
    ),
    Command(
        'stop', 'stop', purpose='Остановить службы, сохранив контейнеры и данные.', stops='Да.'
    ),
    Command(
        'remove',
        'remove',
        purpose='Интерактивно выбрать и удалить данные сервера, контейнеры и внешние ресурсы; сохранить утилиту.',
        stops='Да.',
        data='Удаление выбранных данных необратимо; есть несколько подтверждений.',
    ),
    Command(
        'users password set',
        'user-password',
        ('input',),
        'Изменить пароль существующего обычного пользователя.',
        stops='Краткая остановка для копии.',
    ),
    Command(
        'users password recover',
        'user-password-recover',
        purpose='Продолжить прерванную смену пароля пользователя.',
    ),
    Command(
        'users invitation ensure',
        'invitation-ensure',
        purpose='Создать или продолжить выдачу сохранённого первого токена: одна регистрация, default 7 дней.',
    ),
    Command(
        'users invitation status',
        'invitation-status',
        purpose='Проверить действительность первого токена без его вывода.',
    ),
    Command(
        'users invitation show',
        'invitation-show',
        purpose='Показать действительный первый токен только оператору в TTY.',
    ),
    Command(
        'users invitation revoke',
        'invitation-revoke',
        purpose='Отозвать первый токен; аккаунты и данные сохраняются.',
    ),
    Command(
        'users invitation new',
        'invitation-new',
        purpose='Явно отозвать прежний токен и создать замену на одного обычного пользователя.',
    ),
    Command(
        'credentials admin show',
        'invitation-admin-show',
        purpose='Показать сохранённый пароль администратора только оператору в TTY.',
    ),
    Command(
        'storage local',
        'media-local',
        purpose='Выбрать локальные медиа до первого успешного подключения S3.',
    ),
    Command(
        'storage s3 connect',
        'media-s3',
        ('config',),
        'Перенести локальные медиа в S3, сохранив локальные файлы.',
        stops='Копия и остановка Synapse для переноса.',
    ),
    Command(
        'storage recover',
        'media-recover',
        purpose='Восстановить локальные настройки прерванного media transfer.',
        stops='Запуск прежнего стека.',
    ),
    Command(
        'storage database connect',
        'database-external',
        ('config',),
        'Перенести локальные БД в отдельную внешнюю PostgreSQL.',
        stops='Да, для согласованного переноса.',
    ),
    Command(
        'storage database recover',
        'database-recover',
        purpose='Продолжить перенос БД из checkpoint.',
    ),
    Command(
        'storage database abort',
        'database-abort',
        purpose='Отменить перенос БД до активации назначения.',
    ),
    Command(
        'backup create',
        'backup',
        purpose='Создать согласованную локальную копию.',
        stops='Краткая остановка писателей.',
    ),
    Command(
        'backup recover',
        'backup-recover',
        purpose='Восстановить состояние служб после прерванной копии.',
    ),
    Command(
        'backup verify',
        'restore-check',
        purpose='Проверить копию восстановлением двух временных БД.',
    ),
    Command(
        'backup key create',
        'backup-key',
        ('key',),
        'Создать отдельный приватный recovery key.',
        installed=False,
    ),
    Command(
        'backup external create',
        'backup-external',
        ('config', 'key'),
        'Создать, зашифровать и проверить внешнюю копию.',
        stops='Краткая остановка писателей.',
    ),
    Command(
        'backup external cleanup',
        'backup-cleanup',
        ('config', 'catalog'),
        'Повторить очистку только собственной неудачной загрузки.',
    ),
    Command(
        'backup fetch',
        'backup-fetch',
        ('config', 'key', 'object', 'sha256', 'output'),
        'Скачать и проверить зашифрованную копию в новый файл.',
        installed=False,
    ),
    Command(
        'backup restore',
        'restore',
        ('archive', 'sha256', 'public-ip'),
        'Восстановить копию в пустое staged назначение.',
        stops='Писатели остаются выключены.',
        installed=False,
    ),
    Command(
        'backup restore-local',
        'restore-local',
        ('archive', 'sha256', 'public-ip'),
        'Восстановить обе БД в новую локальную PostgreSQL.',
        stops='Писатели остаются выключены.',
        installed=False,
    ),
    Command(
        'backup activate',
        'restore-activate',
        ('confirm',),
        'Активировать восстановленную установку после остановки источника и DNS.',
    ),
    Command(
        'backup move export',
        'move-export',
        purpose='Остановить источник и создать окончательную копию.',
        stops='Да; источник остаётся остановлен.',
    ),
    Command(
        'backup move abort',
        'move-abort',
        ('confirm',),
        'Вернуть источник после остановки назначения и возврата DNS.',
    ),
    Command(
        'backup schedule local',
        'backup-schedule-local',
        ('calendar',),
        'Проверить локальную копию и включить systemd timer.',
        stops='Краткая остановка для проверочной копии.',
    ),
    Command(
        'backup schedule external',
        'backup-schedule',
        ('config', 'key', 'calendar'),
        'Проверить внешнюю копию и включить systemd timer.',
        stops='Краткая остановка для проверочной копии.',
    ),
    Command(
        'backup schedule status',
        'backup-schedule-status',
        purpose='Показать timer и последний подтверждённый backup.',
    ),
    Command(
        'backup schedule disable',
        'backup-schedule-disable',
        purpose='Отключить свой timer, сохранив копии.',
    ),
    Command(
        'update apply',
        'update',
        purpose='Применить доверенный комплект после backup и проверки.',
        stops='Копия и применение конфигурации.',
    ),
    Command(
        'update recover',
        'update-recover',
        purpose='Вернуть исходники из private checkpoint доверенным комплектом.',
        stops='Возможен перезапуск прежних служб.',
    ),
    Command(
        'credentials admin set',
        'admin-password',
        ('input',),
        'Изменить пароль первоначального администратора.',
        stops='Краткая остановка для копии.',
    ),
    Command(
        'credentials admin recover',
        'admin-password-recover',
        purpose='Восстановить прерванную смену пароля администратора.',
    ),
    Command(
        'credentials database rotate',
        'database-password',
        purpose='Сменить application passwords Synapse/MAS.',
        stops='Да, после проверенной копии.',
    ),
    Command(
        'credentials database recover',
        'database-password-recover',
        purpose='Продолжить смену application passwords.',
    ),
    Command(
        'credentials s3 rotate',
        's3-credentials',
        ('config',),
        'Сменить media S3 credentials, сохраняя назначение.',
        stops='Копия и согласование работающих служб.',
    ),
    Command(
        'credentials s3 recover',
        's3-credentials-recover',
        purpose='Продолжить смену media S3 credentials.',
    ),
    Command(
        'credentials s3 cleanup',
        's3-credentials-cleanup',
        purpose='Очистить только собственные credential probes.',
    ),
    Command(
        'credentials vault connect',
        'vault-connect',
        ('config',),
        'Подключить static snapshots к Vault KV v2.',
    ),
    Command('credentials vault sync', 'vault-sync', purpose='Сверить и обновить snapshot Vault.'),
    Command(
        'credentials vault recover',
        'vault-recover',
        purpose='Восстановить отсутствующие секретные файлы из verified snapshot.',
        stops='Службы должны быть остановлены.',
    ),
    Command(
        'credentials vault auth',
        'vault-auth',
        ('config',),
        'Сменить AppRole для прежнего Vault назначения.',
    ),
    Command(
        'credentials vault disconnect',
        'vault-disconnect',
        purpose='Сохранить local files и отключить Vault; remote versions остаются.',
    ),
    Command(
        'storage s3 check',
        's3-check',
        ('config', 'purpose'),
        'Проверить S3 без изменения настроек установки.',
        installed=False,
    ),
    Command(
        'storage s3 cleanup',
        's3-cleanup',
        ('config', 'catalog'),
        'Удалить только собственный probe по private catalog.',
        installed=False,
    ),
]
BY_PATH = {c.path: c for c in COMMANDS}
BY_BACKEND = {c.backend: c.path for c in COMMANDS}
EXISTING_BACKENDS = frozenset(
    (
        'ui',
        'status',
        'check',
        'up',
        'stop',
        'configuration-validate',
        'backup',
        'backup-recover',
        'restore-check',
        'wrapper-install',
        'wrapper-remove',
        'backup-external',
        's3-check',
        's3-cleanup',
        'media-s3',
        'media-recover',
        'storage-status',
        'update',
        'update-recover',
    )
)
EXISTING_BACKENDS = EXISTING_BACKENDS | frozenset(
    c.backend for c in COMMANDS if c.backend.startswith('federation-')
)
EXISTING_BACKENDS = EXISTING_BACKENDS | frozenset(
    ('features-status', 'features-enable', 'features-recover')
)
EXISTING_BACKENDS = EXISTING_BACKENDS | frozenset(
    c.backend for c in COMMANDS if c.backend.startswith('homepage-')
)
EXISTING_BACKENDS = EXISTING_BACKENDS | frozenset(
    (
        'backup-schedule',
        'backup-schedule-local',
        'backup-schedule-status',
        'backup-schedule-disable',
    )
)
EXISTING_BACKENDS = EXISTING_BACKENDS | frozenset(
    ('release-check', 'admin-bot-show', 'admin-bot-set')
)
TOPICS = {
    'admin': 'Административная панель и её сохранённая identity',
    'features': 'Дополнительные компоненты существующей установки',
    'config': 'Установка и конфигурация',
    'users': 'Пользователи и регистрация',
    'storage': 'Медиа, S3 и БД',
    'backup': 'Копии, восстановление и перенос',
    'update': 'Обновление',
    'credentials': 'Credentials и Vault',
    'federation': 'Федерация: локальная политика и подписанные заявки Misbot.',
    'homepage': 'Главная страница: локальный лендинг, Element Web или внешний HTTPS URL.',
}


def backend_argv(command: Command, values: Mapping[str, str], root: Path) -> list[str]:
    action = command.backend
    args = [values[name] for name in command.arguments]
    if action == 'init':
        return [action, *args, str(root)]
    if action in ('backup-key', 'backup-fetch'):
        return [action, *args]
    if action in ('restore', 'restore-local'):
        return [action, args[0], str(root), *args[1:]]
    return [action, *args, str(root)]


def recovery_command(action: str, root: Path) -> str:
    import shlex

    return (
        'sudo misbot --directory ' + shlex.quote(str(root)) + ' ' + BY_BACKEND.get(action, action)
    )
