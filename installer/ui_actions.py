"""User tasks mapped to the shared CLI; no separate administrative backend."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    path: str
    title: str
    hint: str = ''
    read_only: bool = False


def action(path, title, hint='', read_only=False):
    return Action(path, title, hint, read_only)


MENUS = {
    'server': (
        'Состояние и работа сервера',
        (
            action(
                'status',
                'Показать состояние',
                'Службы, незавершённые операции и следующие действия',
                True,
            ),
            action('check', 'Проверить доступность', 'DNS, HTTPS, клиент и службы', True),
            action('config validate', 'Проверить настройки', 'Без применения изменений', True),
            action('start', 'Запустить сервер', 'Также продолжить запуск после исправления ошибки'),
            action('stop', 'Остановить сервер', 'Общение станет недоступно; данные сохраняются'),
            action(
                'remove', 'Удалить сервер', 'Выбор данных и отдельное окончательное подтверждение'
            ),
        ),
    ),
    'users': (
        'Пользователи и приглашения',
        (
            action(
                'users invitation list',
                'Показать приглашения',
                'Автор, срок и статус; коды не отображаются',
                True,
            ),
            action(
                'users invitation show',
                'Показать код приглашения',
                'Код будет виден в этом терминале',
            ),
            action(
                'users invitation ensure',
                'Подготовить первое приглашение',
                'Одна регистрация; сохранённый код не заменяется',
            ),
            action(
                'users invitation new', 'Заменить приглашение', 'Старый код перестанет действовать'
            ),
            action(
                'users invitation revoke',
                'Отозвать приглашение',
                'Аккаунты зарегистрированных пользователей сохраняются',
            ),
            action(
                'users password set',
                'Изменить пароль пользователя',
                'Нужен приватный JSON с новым паролем',
            ),
            action(
                'credentials admin show',
                'Показать пароль администратора',
                'Пароль будет виден в этом терминале',
            ),
            action(
                'credentials admin set',
                'Изменить пароль администратора',
                'Нужен приватный JSON с новым паролем',
            ),
        ),
    ),
    'backup': (
        'Резервные копии',
        (
            action('backup create', 'Создать копию', 'Используется сохранённое место хранения'),
            action(
                'backup verify',
                'Проверить последнюю копию',
                'Проверка восстановления во временных базах',
            ),
            action(
                'backup external create',
                'Создать зашифрованную копию в S3',
                'Приватный профиль S3 и отдельный ключ',
            ),
            action(
                '@schedule',
                'Настроить расписание',
                'Автоматические копии и время следующего запуска',
            ),
            action('@restore', 'Восстановление и перенос', 'Отдельный путь для нового сервера'),
        ),
    ),
    'schedule': (
        'Расписание резервных копий',
        (
            action(
                'backup schedule status',
                'Показать расписание',
                'Последняя копия и следующий запуск',
                True,
            ),
            action('backup schedule local', 'Включить локальные копии по расписанию'),
            action('backup schedule external', 'Включить копии в S3 по расписанию'),
            action(
                'backup schedule disable', 'Отключить расписание', 'Созданные копии сохраняются'
            ),
        ),
    ),
    'restore': (
        'Восстановление и перенос',
        (
            action('backup fetch', 'Скачать и проверить копию из S3'),
            action(
                'backup restore',
                'Восстановить в пустой каталог',
                'Службы останутся выключены до активации',
            ),
            action('backup restore-local', 'Восстановить с локальной PostgreSQL'),
            action(
                'backup activate',
                'Активировать восстановленный сервер',
                'Источник должен быть остановлен, DNS переключён',
            ),
            action(
                'backup move export',
                'Подготовить перенос с этого сервера',
                'Источник будет остановлен; создаётся окончательная копия',
            ),
            action(
                'backup move abort',
                'Вернуть исходный сервер после отмены переноса',
                'Назначение остановлено, DNS возвращён',
            ),
        ),
    ),
    'storage': (
        'Медиа и базы данных',
        (
            action('storage status', 'Показать состояние хранилища', read_only=True),
            action(
                'storage s3 check',
                'Проверить подключение к S3',
                'Создаётся и удаляется только собственный проверочный объект',
            ),
            action('storage s3 connect', 'Перенести медиа в S3', 'Локальные файлы сохраняются'),
            action('storage database connect', 'Перенести базы в PostgreSQL провайдера'),
            action(
                '@credentials', 'Ключи доступа и Vault', 'Смена реквизитов действующего хранилища'
            ),
        ),
    ),
    'credentials': (
        'Ключи доступа и Vault',
        (
            action('credentials database rotate', 'Сменить пароли баз данных'),
            action(
                'credentials s3 rotate',
                'Сменить ключи доступа S3',
                'Место хранения остаётся прежним',
            ),
            action(
                'credentials vault connect', 'Подключить Vault', 'Снимки секретов в Vault KV v2'
            ),
            action('credentials vault sync', 'Обновить снимок Vault'),
            action('credentials vault auth', 'Сменить доступ к Vault'),
            action(
                'credentials vault disconnect', 'Отключить Vault', 'Локальные файлы сохраняются'
            ),
        ),
    ),
    'federation': (
        'Связь с другими серверами',
        (
            action('federation peers', 'Показать подключённые серверы', read_only=True),
            action(
                'federation invite',
                'Предложить соединение с сервером',
                'Отправить заявку на взаимное подключение',
            ),
            action(
                'federation check',
                'Проверить другой сервер',
                'Адрес и TLS; проверка не создаёт соединение',
                True,
            ),
            action('@requests', 'Заявки на подключение', 'Проверить, одобрить или отклонить'),
            action('@allowlist', 'Локальный список разрешённых серверов'),
        ),
    ),
    'requests': (
        'Заявки на подключение',
        (
            action('federation requests', 'Показать заявки', read_only=True),
            action('federation requests status', 'Проверить выбранную заявку', read_only=True),
            action('federation requests approve', 'Одобрить входящую заявку'),
            action('federation requests reject', 'Отклонить входящую заявку'),
            action('federation requests retry', 'Продолжить незавершённое согласование'),
            action('federation requests cancel', 'Отменить согласование'),
            action('federation operations', 'Показать операции бота', read_only=True),
        ),
    ),
    'allowlist': (
        'Локальный список разрешённых серверов',
        (
            action('federation servers', 'Показать разрешённые серверы', read_only=True),
            action(
                'federation add',
                'Добавить сервер в локальный список',
                'Не означает взаимного подключения',
            ),
            action('federation remove', 'Удалить сервер из локального списка'),
        ),
    ),
    'update': (
        'Обновление Misbot',
        (
            action('update check', 'Проверить наличие новой версии', read_only=True),
            action(
                'update latest',
                'Установить последний стабильный релиз',
                'Копия, проверка и временная остановка сервера',
            ),
            action(
                'update apply',
                'Установить подготовленный комплект',
                'Выберите доверенный распакованный каталог',
            ),
        ),
    ),
    'admin': (
        'Административный бот',
        (
            action('admin bot show', 'Показать настройки панели', read_only=True),
            action('admin access list', 'Показать администраторов', read_only=True),
            action(
                'admin access add',
                'Добавить администратора',
                'Сначала зарегистрируйте личный аккаунт',
            ),
            action(
                'admin access remove',
                'Отозвать права администратора',
                'Основной аккаунт администратора сохраняется',
            ),
            action(
                'admin bot set',
                'Выбрать имя бота до его создания',
                'Имя созданного бота изменить нельзя',
            ),
        ),
    ),
    'features': (
        'Дополнительные компоненты',
        (
            action('features status', 'Показать подключённые компоненты', read_only=True),
            action(
                'features enable',
                'Подключить подготовленные компоненты',
                'Нужен проверенный приватный план',
            ),
        ),
    ),
    'recovery': (
        'Продолжить прерванную операцию',
        (
            action(
                'status',
                'Определить незавершённую операцию',
                'Сначала проверьте состояние и указанное продолжение',
                True,
            ),
            action('@recover-backup', 'Резервные копии после прерывания'),
            action('@recover-storage', 'Хранилище после прерывания'),
            action('@recover-config', 'Настройки после прерывания'),
            action('@recover-homepage', 'Сайт после прерывания'),
            action('@recover-federation', 'Федерация после прерывания'),
            action('@recover-update', 'Обновление после прерывания'),
            action('@recover-credentials', 'Пароли и ключи после прерывания'),
            action('@recover-features', 'Компоненты после прерывания'),
        ),
    ),
    'recover-backup': (
        'Резервные копии после прерывания',
        (
            action('backup recover', 'Вернуть службы после прерванной копии'),
            action('backup external cleanup', 'Очистить собственную неудачную загрузку копии'),
        ),
    ),
    'recover-storage': (
        'Хранилище после прерывания',
        (
            action('storage recover', 'Вернуть настройки после переноса медиа'),
            action('storage database recover', 'Продолжить перенос баз данных'),
            action('storage database abort', 'Отменить перенос баз до активации'),
        ),
    ),
    'recover-config': (
        'Настройки после прерывания',
        (
            action('config recover', 'Продолжить публикацию настроек'),
            action('config rollback', 'Вернуть настройки из сохранённой копии'),
        ),
    ),
    'recover-homepage': (
        'Сайт после прерывания',
        (
            action('homepage recover', 'Продолжить изменение сайта'),
            action('homepage rollback', 'Вернуть прежний сайт'),
        ),
    ),
    'recover-federation': (
        'Федерация после прерывания',
        (
            action('federation recover', 'Продолжить применение федерации'),
            action('federation rollback', 'Вернуть прежние настройки федерации'),
        ),
    ),
    'recover-update': (
        'Обновление после прерывания',
        (action('update recover', 'Восстановить исходники после обновления'),),
    ),
    'recover-credentials': (
        'Пароли и ключи после прерывания',
        (
            action('users password recover', 'Продолжить смену пароля пользователя'),
            action('admin access recover', 'Продолжить изменение доступа администраторов'),
            action('credentials admin recover', 'Восстановить пароль администратора'),
            action('credentials database recover', 'Продолжить смену паролей баз'),
            action('credentials s3 recover', 'Продолжить смену ключей S3'),
            action('credentials s3 cleanup', 'Удалить собственные проверочные объекты ключей S3'),
            action('credentials vault recover', 'Вернуть отсутствующие секреты из Vault'),
        ),
    ),
    'recover-features': (
        'Компоненты после прерывания',
        (action('features recover', 'Вернуть маршруты после подключения компонентов'),),
    ),
}

ARGUMENTS = {
    'config': ('Приватный файл настроек, абсолютный путь', ''),
    'input': ('Приватный JSON с новым паролем, абсолютный путь', ''),
    'key': ('Файл ключа шифрования вне каталога установки', '/root/misbot-backup.key'),
    'calendar': ('Расписание systemd', '*-*-* 03:00:00 UTC'),
    'archive': ('Архив для восстановления, абсолютный путь', ''),
    'sha256': ('SHA256 архива с исходного сервера', ''),
    'public-ip': ('Публичный IPv4 нового сервера', ''),
    'confirm': ('Подтверждение остановки другого сервера', ''),
    'object': ('Ключ объекта копии в S3', ''),
    'output': ('Новый файл для скачанной копии, абсолютный путь', ''),
    'catalog': ('Сохранённый каталог собственной операции, абсолютный путь', ''),
    'server': ('Домен другого Matrix-сервера без https://', ''),
    'request': ('Идентификатор заявки из списка', ''),
    'tag': ('Имя бота без @ и домена', 'admin-panel'),
    'mxid': ('Зарегистрированный аккаунт: @имя:домен', ''),
    'purpose': ('Назначение S3: media или backup', 'media'),
}

MAIN = (
    ('1', 'Состояние и работа сервера', 'Проверить, запустить или остановить'),
    ('2', 'Пользователи и приглашения', 'Приглашение и доступ администратора'),
    ('3', 'Резервные копии', 'Создание, проверка и расписание'),
    ('4', 'Медиа и базы данных', 'Хранилище и подключение S3'),
    ('5', 'Сайт сервера', 'Название, описание и способ открытия чата'),
    ('6', 'Связь с другими серверами', 'Подключения и заявки'),
    ('7', 'Настройки сервера', 'Регистрация, приглашения и сертификаты'),
    ('8', 'Обновление Misbot', 'Проверить и установить релиз'),
    ('9', 'Административный бот', 'Просмотр панели и имени бота'),
    ('10', 'Продолжить прерванную операцию', 'Восстановление по результату проверки состояния'),
    ('11', 'О программе и лицензиях', 'Misbot MIT · Rich MIT'),
)

MAIN_TARGETS = {
    '1': 'server',
    '2': 'users',
    '3': 'backup',
    '4': 'storage',
    '6': 'federation',
    '8': 'update',
    '9': 'admin',
    '10': 'recovery',
}
