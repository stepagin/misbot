"""Task-oriented SSH interface using the existing CLI and private wizard drafts."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import configuration_schema as schema
import cli_commands
import state_store
from terminal_views import TerminalView, literal
import ui_actions


class Back(Exception):pass
class Cancel(Exception):pass
class SaveExit(Exception):pass


def text(message):print(message,flush=True)


def question(label,default='',secret=False,reader=None):
    shown='настроен' if secret and default else default
    prompt=literal(label)+(' ['+literal(shown)+']' if shown else '')+': '
    if reader:value=reader(prompt,secret)
    elif secret:
        # getpass fallback would echo on a non-terminal: explicitly forbid it.
        if not sys.stdin.isatty():raise Cancel()
        value=getpass.getpass(prompt)
    else:value=input(prompt)
    commands={'назад':Back,'отмена':Cancel,'сохранить и выйти':SaveExit}
    if value.lower().strip() in commands:raise commands[value.lower().strip()]()
    return value if value else default


class UI:
    def __init__(self,directory=None,source=None,reader=None,runner=None,draft=None,view=None):
        self.source=Path(source or Path(__file__).resolve().parent)
        self.root=Path(directory or '/opt/matrix-stack')
        self.reader=reader;self.runner=runner
        self.view=view or TerminalView()
        self.draft=Path(draft or Path.home()/'.local/state/misbot/wizard'/
            (hashlib.sha256(str(self.root).encode()).hexdigest()+'.json'))
        self.model={};self.backup={};self.step=0
        if self.draft.exists():
            import s3_config
            from configuration import read_private_json
            saved=read_private_json(self.draft)
            self.model=saved['values'];self.backup=saved.get('backup',{});self.step=saved.get('step',0)
            self.root=Path(saved['directory'])
        elif (self.root/'installation.json').exists() and not self.adopted() and not (self.root/'configuration-transition.json').exists():
            from configuration import load
            self.model=load(self.root,complete=False)

    def ask(self,label,default='',secret=False):return question(label,default,secret,self.reader)

    def installation(self):
        path=self.root/'installation.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def adopted(self):return self.installation().get('backend')=='existing-compose'

    def supported(self,path):
        command=cli_commands.BY_PATH.get(path)
        if command is None:return False
        if command.installed and not self.installation():return False
        if self.adopted():return command.backend in cli_commands.EXISTING_BACKENDS
        return not command.backend.startswith('features-')

    def choose(self,title,choices,exit_label='Назад',aliases=None):
        choices=list(choices)+[('0',exit_label,'')]
        self.view.options(title,choices)
        self.view.footer(exit_label)
        valid={item[0] for item in choices}
        while True:
            value=self.ask('Номер','0').strip()
            value=(aliases or {}).get(value,value)
            if value in valid:return value
            self.view.message('Выберите номер из списка. Enter — '+exit_label.lower()+'.','yellow')

    def confirm(self,label='Выполнить операцию?'):
        while True:
            answer=self.ask(label+' да/нет','нет').strip().casefold()
            if answer in ('да','нет'):return answer=='да'
            self.view.message('Введите «да» или «нет». Enter отменяет действие.','yellow')
    def save(self):
        from configuration import directory
        directory(self.draft.parent)
        state_store.write_json(self.draft,{'format':1,'directory':str(self.root),'step':self.step,'values':self.model,'backup':self.backup})

    def initialized(self):
        from configuration import read_private_json
        path=self.root/'installation.json'
        return path.exists() and bool(read_private_json(path).get('databases_initialized'))

    def use_active_configuration(self):
        from configuration import load,backup_profile
        self.model=load(self.root,complete=False)
        self.backup=backup_profile(self.root)
        self.view.message('Базы уже инициализированы. Первоначальные настройки повторно не применяются; продолжаем проверку существующей установки.')

    def field(self,key):
        if key not in schema.FIELDS:
            self.view.message('Неизвестное поле; выберите имя из показанного списка.');return
        item=schema.FIELDS[key]
        while True:
            self.view.message(key+' — '+item.label+'. '+item.required+'; '+item.impact)
            current=self.model.get(key,item.default)
            choices={
                'media':(('local','Хранить медиа на этом сервере'),('s3','Хранить медиа в S3')),
                'homepage':(('landing','Страница с описанием и кнопками входа'),('element','Сразу открыть клиент чата'),('external','Перейти на внешний сайт')),
                'bool':(('true','Включено'),('false','Выключено')),
            }.get(item.kind)
            if choices and self.view.console:
                self.view.options('Выберите вариант',[(str(i),label,'Текущее значение' if value==current else '')
                                                       for i,(value,label) in enumerate(choices,1)])
                default=next((str(i) for i,(value,label) in enumerate(choices,1) if value==current),'')
                value=self.ask('Вариант',default)
                value=dict((str(i),value) for i,(value,label) in enumerate(choices,1)).get(value,value)
            else:
                if not item.secret and item.kind in ('text','https','short_text','description','email','domain','ip'):
                    self.view.message('Enter — сохранить значение. /очистить — убрать значение, если поле необязательное.','dim')
                value=self.ask(item.label if self.view.console else key,current,item.secret)
                if value=='/очистить' and not item.secret:value=''
            try:self.model[key]=schema.field_value(key,value)
            except schema.InvalidConfiguration as error:self.view.message(str(error));continue
            self.save();return

    def run(self,path,**values):
        self.view.message('Выполняется: '+path+'; установка '+str(self.root))
        args=[sys.executable,str(self.source/'misbot.py'),'--directory',str(self.root),*path.split()]
        for key,value in values.items():args+=['--'+key,str(value)]
        if self.runner:return self.runner(args)
        # Run in the same foreground process group. Ctrl+C reaches backend;
        # wait for it to finish before displaying recovery or accepting changes.
        child=subprocess.Popen(args,stdin=None if path in ('users invitation show','credentials admin show','update latest') else subprocess.DEVNULL)
        try:return child.wait()
        except (KeyboardInterrupt,state_store.OperationInterrupted):
            if child.poll() is None:child.send_signal(signal.SIGTERM)
            self.view.message('Операция прервана. Ожидается завершение дочернего процесса; checkpoints сохраняются.')
            while child.poll() is None:
                try:child.wait(timeout=1)
                except subprocess.TimeoutExpired:pass
                except KeyboardInterrupt:pass
            self.view.message('Проверьте sudo misbot --directory '+shlex.quote(str(self.root))+' status; не удаляйте lock/checkpoints.')
            return 130

    def s3_form(self,purpose):
        import s3_diagnostics as probe
        import s3_config
        values=self.model if purpose=='media' else self.backup
        keys=('S3_BUCKET','S3_ENDPOINT_URL','S3_REGION','S3_PREFIX','S3_ACCESS_KEY_ID','S3_SECRET_ACCESS_KEY')
        for key in keys:
            item=schema.FIELDS[key]
            values[key]=self.ask(item.label+' ('+key+')',values.get(key,''),item.secret)
            self.save()
        path=self.draft.parent/(self.draft.stem+'-'+purpose+'.env')
        while True:
            from configuration import serialize
            state_store.write_text(path,serialize({key:values.get(key,'') for key in sorted(s3_config.FIELDS)}))
            result=probe.diagnose(path,purpose,directory=self.root if (self.root/'installation.json').exists() else None,
                progress=lambda item:self.view.message(item['stage']+': '+item['status']+' — '+item['explanation']))
            self.view.message('Итог: '+result['conclusion'])
            for action in result['next_actions']:self.view.message(action)
            if result['passed']:return
            choice=self.ask('1 исправить поле, 2 повторить, 3 локальные медиа/отложить, сохранить и выйти','1')
            if choice=='3':
                if purpose=='media':self.model['MEDIA_MODE']='local'
                else:self.backup={}
                self.view.message('Позднее: sudo misbot storage s3 connect --config /root/misbot-s3.env; существующие файлы переносятся и сохраняются.' if purpose=='media' else 'Позднее: misbot backup external create --help; настройки можно сохранить в settings/backup.env.')
                self.save();return
            if choice=='1':
                selection=self.choose('Какое поле исправить?',[(str(i),schema.FIELDS[key].label,'') for i,key in enumerate(keys,1)],
                                      aliases={key:str(i) for i,key in enumerate(keys,1)})
                if selection=='0':raise Back()
                key=keys[int(selection)-1]
                item=schema.FIELDS[key];values[key]=self.ask(item.label,values.get(key,''),item.secret);self.save()

    def setup_step(self,index):
        import preflight
        if index==0:
            self.field('DOMAIN')
            while True:
                path=Path(self.ask('Пустой каталог установки, абсолютный путь; допускаются пробелы',str(self.root)))
                if path.is_absolute() and path!=Path('/') and path.resolve()==path:
                    if path.exists() and any(path.iterdir()) and not (path/'installation.json').exists():self.view.message('Каталог чужой и непустой. Выберите другой.');continue
                    self.root=path;self.save();break
                self.view.message('Нужен отдельный абсолютный каталог без symlink/..')
        elif index==1:
            import shutil
            missing=[tool for tool in ('dig','ip') if not shutil.which(tool)]
            if missing:raise RuntimeError('Отсутствуют локальные зависимости: '+', '.join(missing)+'. Выполните sudo apt-get update; sudo apt-get install -y dnsutils iproute2 python3-yaml; затем повторите ui. Это не ошибка DNS или S3.')
            self.view.message('Проверка Linux amd64, systemd, памяти, диска и портов. Ошибку исправьте и повторите; шаг не пропускается.')
            preflight.system_check(self.root)
        elif index==2:
            self.field('MAS_DOMAIN');self.field('PUBLIC_IP');self.field('ACME_EMAIL')
            domain=self.model['DOMAIN'];ip=self.model.get('PUBLIC_IP')
            self.view.message('Создайте A на VPS для: '+', '.join(schema.public_hosts(self.model))+'. Удалите AAAA; отключите CDN proxy. Порты описаны в README.')
            if not ip:
                addresses=preflight.resolve(domain,__import__('socket').AF_INET)
                if len(addresses)!=1:raise RuntimeError('DOMAIN не имеет одного A. Исправьте DNS или PUBLIC_IP; повторите этот шаг.')
                ip=next(iter(addresses))
            preflight.dns_check(domain,ip,self.model.get('MAS_DOMAIN',''));preflight.caa_check(domain,self.model.get('MAS_DOMAIN',''))
            preflight.network_check(self.root,self.model.get('DOCKER_SUBNET','172.30.50.0/24'))
            self.view.message('DNS/CAA/система проверены автоматически. До запуска свободные порты проверяются backend.')
        elif index==3:
            self.field('MEDIA_MODE')
            if self.model['MEDIA_MODE']=='s3':self.s3_form('media')
        elif index==4:
            self.view.message('Парольный вход доступен без Google/Telegram. OAuth можно пропустить и настроить до запуска позднее.')
            for provider in ('GOOGLE','TELEGRAM'):
                enabled=self.ask('Подключить '+provider+'? да/нет','нет')
                if enabled=='да':
                    self.field(provider+'_CLIENT_ID');self.field(provider+'_CLIENT_SECRET')
                    if provider=='TELEGRAM':self.field('SOCKS5_CONFIG')
            self.field('INVITATION_LIFETIME_SECONDS')
        elif index==5:
            self.view.message('Внешние копии требуют отдельного private bucket/prefix и recovery key вне установки. Можно явно отложить; локальная копия не защищает от потери VPS.')
            if self.ask('Настроить внешние копии сейчас? да/отложить','отложить')=='да':
                self.s3_form('backup')
                if self.backup:
                    self.backup['BACKUP_KEY_FILE']=self.ask('Путь recovery key вне установки; отсутствующий ключ будет создан','/root/misbot-backup.key')
                    self.backup['BACKUP_CALENDAR']=self.ask('Systemd calendar (пусто — ручные копии)','')
                    schema.validate_backup(self.backup)
            self.save()
        elif index==6:
            self.model=schema.validate(self.model)
            self.view.message('Итог. Внутренние пароли/ключи создаёт backend и сохраняет при повторе.')
            rows=[]
            for key,value in self.model.items():
                if value and (value!=schema.FIELDS[key].default or key in ('DOMAIN','MEDIA_MODE','POSTGRES_MODE')):
                    rows.append((schema.FIELDS[key].label,'настроен' if schema.FIELDS[key].secret else value))
            self.view.details(rows)
            self.view.message('Внешние копии: '+('настроены' if self.backup else 'явно отложены'))
            while True:
                change=self.ask('Введите имя поля для исправления или применить. Назад/отмена/сохранить и выйти')
                if change:break
                self.view.message('Чтобы запустить установку, введите «применить». «сохранить и выйти» оставит только черновик.')
            if change!='применить':self.field(change);raise Back()
        elif index==7:
            from configuration import write_model
            if not (self.root/'installation.json').exists():
                code=self.run('config init',domain=self.model['DOMAIN'])
                if code:raise RuntimeError('Init не завершён. Настройки draft сохранены; повторите мастер, не удаляйте secrets.')
            metadata=json.loads((self.root/'installation.json').read_text())
            if metadata.get('databases_initialized'):
                self.use_active_configuration()
                return
            write_model(self.root,self.model,self.backup)
            code=self.run('start')
            if code:raise RuntimeError('Запуск не подтверждён. sudo misbot status; исправьте причину и повторите start. Draft сохранён.')
        elif index==8:
            if self.run('check'):raise RuntimeError('Готовность сервера не подтверждена. Выполните sudo misbot status; если запуск не завершён, sudo misbot start, затем sudo misbot check. Повторный init не нужен.')
            if self.backup:
                key=self.backup['BACKUP_KEY_FILE']
                if not Path(key).exists() and self.run('backup key create',key=key):raise RuntimeError('Не удалось создать recovery key; сохраните настройки и повторите backup key create.')
                from configuration import backup_profile
                profile=self.root/'settings/backup.env'
                command='backup schedule external' if self.backup.get('BACKUP_CALENDAR') else 'backup external create'
                args={'config':profile,'key':key}
                if self.backup.get('BACKUP_CALENDAR'):args['calendar']=self.backup['BACKUP_CALENDAR']
                if self.run(command,**args):raise RuntimeError('Сервер работает; внешняя копия не подтверждена. Настройки сохранены; повторите backup external create.')
        elif index==9:self.finish()

    def finish(self):
        from registration import manage
        domain=self.installation()['domain']
        self.view.heading('Настройка завершена','Откройте клиент и войдите в аккаунт администратора.')
        self.view.details([('Клиент','https://web.'+domain),
                           ('Администратор','@matrix-admin:'+domain)])
        self.view.message('Пароль: sudo misbot credentials admin show — только по отдельному запросу.')
        try:
            result=manage(self.root,'ensure')
            self.view.details([('Первое приглашение',result['state']),('Срок до',result['expires_at'])])
            self.view.message('Пригласить пользователя: sudo misbot ui → Пользователи и приглашения → Показать код приглашения.')
            self.view.message('Пользователь вводит код в форме регистрации клиента. Не добавляйте код в URL.')
        except Exception:
            self.view.message('Сервер запущен; выдача токена не подтверждена. Сохранённый intent будет продолжен: sudo misbot users invitation ensure. Переустановка не требуется.')

    def wizard(self):
        names=['Домен и каталог','Система','DNS и порты','Медиа','Авторизация','Внешние копии','Итог','Применение и запуск','Проверка','Завершение']
        if 8<=self.step<=9 and self.initialized():self.use_active_configuration()
        self.view.message('Misbot — мастер SSH. Команды ввода: назад, отмена, сохранить и выйти. Enter сохраняет default/существующий секрет.')
        while self.step<len(names):
            if self.step<8 and self.initialized():
                self.use_active_configuration()
                self.step=8;self.save()
            self.view.wizard_step(self.step,names)
            self.view.message('Установка: '+str(self.root),'dim')
            try:self.setup_step(self.step)
            except Back:
                if self.initialized():
                    self.view.message('Первоначальное применение уже выполнено. Возвращаемся в меню управления существующей установкой.')
                    return self.menu()
                self.step=max(0,self.step-1);continue
            except (Cancel,KeyboardInterrupt):self.view.message('Ввод отменён; действующая конфигурация не изменена. Draft сохранён.');self.save();return 130
            except SaveExit:self.save();self.view.message('Сохранено приватно. Продолжение: sudo python3 '+shlex.quote(str(self.source/'misbot.py'))+' ui --directory '+shlex.quote(str(self.root)));return 0
            except (RuntimeError,OSError):
                self.view.message('Этап не завершён. Выполните sudo misbot config validate и sudo misbot status; данные и ключи сохраняются.')
                # Show only schema/preflight static safe messages, never SDK bodies.
                error=sys.exc_info()[1]
                if type(error) is RuntimeError or isinstance(error,schema.InvalidConfiguration):self.view.message(str(error))
                try:self.ask('Повторить шаг / назад / сохранить и выйти','повторить')
                except Back:
                    if self.initialized():return self.menu()
                    self.step=max(0,self.step-1)
                except (SaveExit,Cancel,KeyboardInterrupt):self.save();self.view.message('Draft сохранён; повторите ui для продолжения.');return 0
                continue
            self.step+=1;self.save()
        self.view.message('Мастер завершён. Управление: sudo misbot ui');return 0

    def continuation(self):
        return 'Далее: sudo misbot --directory '+shlex.quote(str(self.root))+' status. Продолжение указано в результате проверки.'

    def available_actions(self,group):
        return [item for item in ui_actions.MENUS[group][1]
                if (bool(self.available_actions(item.path[1:])) if item.path.startswith('@')
                    else self.supported(item.path))]

    def welcome(self):
        while True:
            try:
                self.view.heading('Misbot','Создайте сервер или восстановите его из копии.')
                choice=self.choose('С чего начать?',[
                    ('1','Установить новый сервер','Мастер проверит настройки перед запуском'),
                    ('2','Восстановить сервер из копии','Сначала восстановление, затем отдельная активация'),
                    ('3','О программе и лицензиях','Misbot MIT · Rich MIT')],exit_label='Выйти из Misbot')
                if choice=='0':return 0
                if choice=='1':return self.wizard()
                if choice=='3':self.about();continue
                self.task_menu('restore')
                if self.installation():return self.menu()
            except (Back,Cancel,KeyboardInterrupt):continue
            except SaveExit:return 0

    def execute(self,item):
        if not self.supported(item.path):
            self.view.message('Операция недоступна для выбранной установки.','yellow');return 2
        command=cli_commands.BY_PATH[item.path]
        self.view.heading(item.title,command.purpose)
        args={}
        execution_root=self.root
        if command.backend in ('restore','restore-local'):
            while True:
                target=Path(self.ask('Пустой каталог для восстановления, абсолютный путь',str(self.root)))
                if target.is_absolute() and target!=Path('/') and target.resolve()==target:
                    if not target.exists() or (target.is_dir() and not any(target.iterdir())):
                        execution_root=target;break
                self.view.message('Выберите пустой абсолютный каталог без symlink и переходов ..','yellow')
        if 'request' in command.arguments:self.run('federation requests')
        for key in command.arguments:
            label,default=ui_actions.ARGUMENTS[key]
            if key=='config':
                if command.path.startswith('backup '):label='Приватный профиль S3 для копий, абсолютный путь'
                elif command.backend.startswith('vault-'):label='Приватный профиль Vault, абсолютный путь'
                elif command.backend=='database-external':label='Приватный профиль PostgreSQL, абсолютный путь'
                elif command.backend=='features-enable':label='Проверенный приватный план компонентов, абсолютный путь'
                if self.adopted() and command.backend in ('s3-check','media-s3','backup-external','backup-schedule'):
                    purpose='backup' if command.path.startswith('backup ') else 'media'
                    default=str(self.root/'.misbot/s3'/(purpose+'.env'))
            if key=='key' and self.adopted():
                default=self.installation().get('existing',{}).get('backup_key',default)
            if key=='confirm':
                default=''
                self.view.message('Введите source-stopped после остановки источника и переключения DNS.'
                                  if command.backend=='restore-activate' else
                                  'Введите target-stopped после остановки назначения и возврата DNS.')
            while True:
                value=self.ask(label,default).strip()
                if value:args[key]=value;break
                self.view.message('Это поле обязательно. «назад» — отменить ввод.','yellow')
        if command.backend in ('update','update-recover'):
            while True:
                source=self.ask('Доверенный распакованный комплект, абсолютный путь').strip()
                if source:args['source']=source;break
                self.view.message('Укажите каталог комплекта или введите «назад».','yellow')
        if not item.read_only:
            rows=[('Операция',item.title),('Установка',str(execution_root)),
                  ('Перерыв в работе',command.stops),('Данные',command.data)]
            rows.extend((ui_actions.ARGUMENTS[key][0],value) for key,value in args.items() if key!='source')
            if 'source' in args:rows.append(('Комплект',args['source']))
            self.view.details(rows)
            if not self.confirm():
                self.view.message('Действие отменено.');return 0
        previous=self.root
        try:
            self.root=execution_root
            code=self.run(command.path,**args)
        finally:self.root=previous
        self.view.result(item.title,code,self.continuation())
        if command.backend in ('update','release-update') and code==0:
            self.view.message('Обновление применено. Откройте sudo misbot ui заново, чтобы использовать новый интерфейс.')
            raise SaveExit()
        if command.backend in ('restore','restore-local') and code==0:
            self.view.message('Копия восстановлена. Перед активацией остановите источник и переключите DNS.')
            self.view.message('Продолжение: sudo misbot --directory '+shlex.quote(str(execution_root))+' backup activate --confirm source-stopped')
        return code

    def task_menu(self,group):
        title=ui_actions.MENUS[group][0]
        while True:
            actions=self.available_actions(group)
            if not actions:
                self.view.message('Этот раздел недоступен для выбранной установки.');return
            try:
                self.view.heading(title)
                if group=='recovery':
                    self.view.message('Сначала проверьте состояние. Выберите продолжение, указанное сервером; не удаляйте сохранённые этапы операции.')
                choices=[(str(i),item.title,item.hint) for i,item in enumerate(actions,1)]
                choice=self.choose('Что сделать?',choices)
                if choice=='0':return
                item=actions[int(choice)-1]
                if item.path.startswith('@'):self.task_menu(item.path[1:])
                else:self.execute(item)
            except (Back,Cancel,KeyboardInterrupt):
                self.view.message('Ввод отменён.');return

    def menu(self):
        state=self.installation()
        version=json.loads((self.source/'release.json').read_text())['version']
        self.view.heading('Misbot '+version,'Управление сервером '+state.get('domain',''))
        self.view.details([('Установка',str(self.root)),
                           ('Режим','Подключённая Compose-установка' if self.adopted() else 'Установка Misbot')])
        self.run('status')
        while True:
            try:
                choices=[item for item in ui_actions.MAIN if not (self.adopted() and item[0] in ('2','7'))]
                if self.adopted():choices.append(('12','Дополнительные компоненты','Подключить подготовленный план'))
                choice=self.choose('Главное меню',choices,exit_label='Выйти из Misbot')
                if choice=='0':return 0
                if choice=='5':self.homepage_menu()
                elif choice=='7':self.settings_menu()
                elif choice=='11':self.about()
                elif choice=='12' and self.adopted():self.task_menu('features')
                else:self.task_menu(ui_actions.MAIN_TARGETS[choice])
            except (Back,Cancel,KeyboardInterrupt):self.view.message('Ввод отменён; возвращаемся в главное меню.')
            except SaveExit:self.view.message('Выход из Misbot.');return 0
            except (RuntimeError,OSError,ValueError):
                self.view.message('Операция не завершена.','yellow')
                error=sys.exc_info()[1]
                if type(error) is RuntimeError or isinstance(error,schema.InvalidConfiguration):self.view.message(str(error))
                self.view.message(self.continuation())

    def about(self):
        self.view.heading('О программе и лицензиях')
        self.view.details([('Misbot','MIT · Copyright (c) 2026 Stepan Ponamarev'),
                           ('Оформление','Rich · MIT · Copyright (c) 2020 Will McGugan'),
                           ('Лицензия Misbot',str(self.source/'LICENSE')),
                           ('Сторонние компоненты',str(self.source/'third-party-licenses.md'))])
        if self.view.console is None:
            self.view.message('Сейчас используется простой вывод. Rich: sudo apt-get install python3-rich. Для оформления нужен терминал с TERM, отличным от dumb.')
        self.view.message('Системные пакеты и их зависимости сохраняют собственные лицензии.')

    def edit_fields(self,title,keys):
        before={key:self.model[key] for key in keys}
        aliases={key:str(i) for i,key in enumerate(keys,1)}
        aliases['применить']='a'
        while True:
            choices=[(str(i),schema.FIELDS[key].label,
                      'настроен' if schema.FIELDS[key].secret and self.model[key] else self.model[key] or 'не задано')
                     for i,key in enumerate(keys,1)]
            choices.append(('a','Применить изменения','Сначала покажем итог и запросим подтверждение'))
            choice=self.choose(title,choices,aliases=aliases)
            if choice=='0':return None
            if choice=='a':
                return {key:self.model[key] for key in keys if self.model[key]!=before[key]}
            self.field(keys[int(choice)-1])

    def homepage_menu(self):
        import homepage
        if self.adopted():
            import existing_features
            current=existing_features.Homepage(self.root).current()
            self.model={key:current[key] for key in homepage.KEYS}
        else:
            from configuration import load
            self.model=load(self.root,True)
        before=dict(self.model)
        self.view.heading('Сайт сервера','Выберите поле, проверьте итог и примените изменения.')
        changes=self.edit_fields('Что изменить?',homepage.KEYS)
        if not changes:
            self.view.message('Изменения не применялись.');return
        for key,value in changes.items():schema.field_value(key,value)
        self.view.details([(schema.FIELDS[key].label,before[key]+' → '+value) for key,value in changes.items()])
        self.view.message('Для применения обновится Caddy; возможен короткий перерыв HTTPS.')
        if not self.confirm('Проверить и применить эти изменения?'):return
        if self.adopted():
            names={'HOMEPAGE_MODE':'mode','HOMEPAGE_URL':'url','SITE_NAME':'name',
                   'SITE_DESCRIPTION':'description','SITE_SUPPORT_URL':'support-url'}
            code=self.run('homepage apply',**{names[key]:value for key,value in changes.items()})
            self.view.result('Сайт сервера',code,self.continuation())
        else:
            with state_store.operation_guard(self.root):
                result=homepage.Backend(self.root).apply(changes)
            self.view.message('Результат: '+result['status']+'. Просмотр: sudo misbot homepage show')

    def settings_menu(self):
        from configuration import load,apply_settings
        if self.adopted():
            self.view.message('Настройки подключённого стека меняются его собственными средствами.');return
        self.model=load(self.root,complete=True)
        self.view.heading('Настройки сервера','Домен сервера закреплён. Хранилище и ключи доступа — в отдельных разделах.')
        keys=('PUBLIC_IP','ACME_EMAIL','FEDERATION_SERVERS','INVITATION_LIFETIME_SECONDS',
              'INVITATION_USAGE_LIMIT','OPEN_REGISTRATION','REGISTRATION_TOKEN_REQUIRED')
        before=dict(self.model)
        changes=self.edit_fields('Что изменить?',keys)
        if not changes:
            self.view.message('Изменения не применялись.');return
        schema.validate(self.model)
        self.view.details([(schema.FIELDS[key].label,before[key]+' → '+value) for key,value in changes.items()])
        self.view.message('Будет создана согласованная копия; возможен перезапуск служб.')
        if self.confirm('Применить изменения?'):
            apply_settings(self.root,changes)
            self.view.message('Настройки применены.','green')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',type=Path)
    args=parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('UI требует SSH TTY: ssh -t USER@HOST; затем sudo misbot ui. Для скрипта: misbot help',file=sys.stderr);return 2
    if os.geteuid()!=0:print('Выполните sudo misbot ui; автоматически пароль не запрашивается.',file=sys.stderr);return 77
    ui=UI(args.directory)
    if (ui.root/'installation.json').exists():
        import command_wrapper
        command_wrapper.trusted_installation(ui.root)
        state=json.loads((ui.root/'installation.json').read_text())
        if state.get('backend')=='existing-compose':return ui.menu()
        if (ui.root/'configuration-transition.json').exists():
            text('Незавершённая публикация: sudo misbot --directory '+shlex.quote(str(ui.root))+' config recover');return 1
        if state.get('restore_pending'):return ui.menu()
        if state.get('databases_initialized'):
            if (ui.root/'configuration-transition.json').exists():
                text('Конфигурация не опубликована полностью. Продолжение: sudo misbot --directory '+shlex.quote(str(ui.root))+' config recover');return 1
            if ui.draft.exists() and 7<=ui.step<=9:
                return ui.wizard()
            return ui.menu()
    return ui.wizard() if ui.draft.exists() or ui.installation() else ui.welcome()


if __name__=='__main__':
    try:
        def interrupted(signum,frame):raise state_store.OperationInterrupted('SSH/UI interrupted')
        for number in (signal.SIGTERM,signal.SIGHUP):signal.signal(number,interrupted)
        raise SystemExit(main())
    except (KeyboardInterrupt,EOFError,state_store.OperationInterrupted):
        text('Ввод прерван; сохранённый draft и checkpoints оставлены. Повторите sudo misbot ui; проверьте misbot status.');raise SystemExit(130)
    except (OSError,ValueError,RuntimeError,KeyError):
        text('Не удалось безопасно открыть мастер. Сохраните файлы; проверьте misbot status и misbot config validate.');raise SystemExit(1)
