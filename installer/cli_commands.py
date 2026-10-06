"""One command vocabulary shared by CLI, help, documentation and future UI."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    path: str
    backend: str
    arguments: tuple = ()
    purpose: str = ''
    stops: str = 'Нет.'
    data: str = 'Данные и ключи сохраняются.'
    installed: bool = True


COMMANDS = [
    Command('config init','init',('domain',),'Создать установку и приватный settings.env.',installed=False),
    Command('config generate','generate',purpose='Сгенерировать конфигурацию без перезапуска.'),
    Command('config preflight','preflight',purpose='Проверить систему, DNS и порты.'),
    Command('config command install','wrapper-install',purpose='Установить или обновить системную команду.'),
    Command('config command remove','wrapper-remove',purpose='Удалить только принадлежащую установке системную команду.'),
    Command('status','status',purpose='Показать службы, pending состояния и дальнейшие действия.'),
    Command('check','check',purpose='Проверить DNS, HTTPS, discovery, RTC и состояние служб.'),
    Command('start','up',purpose='Проверить конфигурацию и запустить службы.',stops='Возможен перезапуск изменённых служб.'),
    Command('stop','stop',purpose='Остановить службы, сохранив контейнеры и данные.',stops='Да.'),
    Command('remove','remove',purpose='Удалить свои контейнеры, сеть и системную команду; сохранить каталог данных.',stops='Да.'),
    Command('users password set','user-password',('input',),'Изменить пароль существующего обычного пользователя.',stops='Краткая остановка для копии.'),
    Command('users password recover','user-password-recover',purpose='Продолжить прерванную смену пароля пользователя.'),
    Command('storage local','media-local',purpose='Выбрать локальные медиа до первого успешного подключения S3.'),
    Command('storage s3 connect','media-s3',('config',),'Перенести локальные медиа в S3, сохранив локальные файлы.',stops='Копия и остановка Synapse для переноса.'),
    Command('storage recover','media-recover',purpose='Восстановить локальные настройки прерванного media transfer.',stops='Запуск прежнего стека.'),
    Command('storage database connect','database-external',('config',),'Перенести локальные БД в отдельную внешнюю PostgreSQL.',stops='Да, для согласованного переноса.'),
    Command('storage database recover','database-recover',purpose='Продолжить перенос БД из checkpoint.'),
    Command('storage database abort','database-abort',purpose='Отменить перенос БД до активации назначения.'),
    Command('backup create','backup',purpose='Создать согласованную локальную копию.',stops='Краткая остановка писателей.'),
    Command('backup recover','backup-recover',purpose='Восстановить состояние служб после прерванной копии.'),
    Command('backup verify','restore-check',purpose='Проверить копию восстановлением двух временных БД.'),
    Command('backup key create','backup-key',('key',),'Создать отдельный приватный recovery key.',installed=False),
    Command('backup external create','backup-external',('config','key'),'Создать, зашифровать и проверить внешнюю копию.',stops='Краткая остановка писателей.'),
    Command('backup external cleanup','backup-cleanup',('config','catalog'),'Повторить очистку только собственной неудачной загрузки.'),
    Command('backup fetch','backup-fetch',('config','key','object','sha256','output'),'Скачать и проверить зашифрованную копию в новый файл.',installed=False),
    Command('backup restore','restore',('archive','sha256','public-ip'),'Восстановить копию в пустое staged назначение.',stops='Писатели остаются выключены.',installed=False),
    Command('backup restore-local','restore-local',('archive','sha256','public-ip'),'Восстановить обе БД в новую локальную PostgreSQL.',stops='Писатели остаются выключены.',installed=False),
    Command('backup activate','restore-activate',('confirm',),'Активировать восстановленную установку после остановки источника и DNS.'),
    Command('backup move export','move-export',purpose='Остановить источник и создать окончательную копию.',stops='Да; источник остаётся остановлен.'),
    Command('backup move abort','move-abort',('confirm',),'Вернуть источник после остановки назначения и возврата DNS.'),
    Command('backup schedule local','backup-schedule-local',('calendar',),'Проверить локальную копию и включить systemd timer.',stops='Краткая остановка для проверочной копии.'),
    Command('backup schedule external','backup-schedule',('config','key','calendar'),'Проверить внешнюю копию и включить systemd timer.',stops='Краткая остановка для проверочной копии.'),
    Command('backup schedule status','backup-schedule-status',purpose='Показать timer и последний подтверждённый backup.'),
    Command('backup schedule disable','backup-schedule-disable',purpose='Отключить свой timer, сохранив копии.'),
    Command('update apply','update',purpose='Применить доверенный комплект после backup и проверки.',stops='Копия и применение конфигурации.'),
    Command('update recover','update-recover',purpose='Вернуть исходники из private checkpoint доверенным комплектом.',stops='Возможен перезапуск прежних служб.'),
    Command('credentials admin set','admin-password',('input',),'Изменить пароль первоначального администратора.',stops='Краткая остановка для копии.'),
    Command('credentials admin recover','admin-password-recover',purpose='Восстановить прерванную смену пароля администратора.'),
    Command('credentials database rotate','database-password',purpose='Сменить application passwords Synapse/MAS.',stops='Да, после проверенной копии.'),
    Command('credentials database recover','database-password-recover',purpose='Продолжить смену application passwords.'),
    Command('credentials s3 rotate','s3-credentials',('config',),'Сменить media S3 credentials, сохраняя назначение.',stops='Копия и согласование работающих служб.'),
    Command('credentials s3 recover','s3-credentials-recover',purpose='Продолжить смену media S3 credentials.'),
    Command('credentials s3 cleanup','s3-credentials-cleanup',purpose='Очистить только собственные credential probes.'),
    Command('credentials vault connect','vault-connect',('config',),'Подключить static snapshots к Vault KV v2.'),
    Command('credentials vault sync','vault-sync',purpose='Сверить и обновить snapshot Vault.'),
    Command('credentials vault recover','vault-recover',purpose='Восстановить отсутствующие секретные файлы из verified snapshot.',stops='Службы должны быть остановлены.'),
    Command('credentials vault auth','vault-auth',('config',),'Сменить AppRole для прежнего Vault назначения.'),
    Command('credentials vault disconnect','vault-disconnect',purpose='Сохранить local files и отключить Vault; remote versions остаются.'),
    Command('storage s3 check','s3-check',('config','purpose'),'Проверить S3 без изменения настроек установки.',installed=False),
    Command('storage s3 cleanup','s3-cleanup',('config','catalog'),'Удалить только собственный probe по private catalog.',installed=False),
]
BY_PATH={c.path:c for c in COMMANDS}
BY_BACKEND={c.backend:c.path for c in COMMANDS}
TOPICS={
    'config':'Установка и конфигурация', 'users':'Пользователи и регистрация',
    'storage':'Медиа, S3 и БД', 'backup':'Копии, восстановление и перенос',
    'update':'Обновление', 'credentials':'Credentials и Vault',
    'federation':'Федерация: FEDERATION_SERVERS в settings.env; config generate и start. Отдельной команды изменения нет.',
    'homepage':'Главная страница перенаправляет на Element Web. Команды выбора другого URL нет.',
}


def backend_argv(command,values,root):
    action=command.backend
    args=[values[name] for name in command.arguments]
    if action=='init':return [action,*args,str(root)]
    if action in ('backup-key','backup-fetch'):return [action,*args]
    if action in ('restore','restore-local'):
        return [action,args[0],str(root),*args[1:]]
    return [action,*args,str(root)]


def recovery_command(action,root):
    import shlex
    return 'sudo misbot --directory '+shlex.quote(str(root))+' '+BY_BACKEND.get(action,action)
