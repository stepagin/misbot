"""Small, line-oriented SSH interface. No ANSI, shell evaluation or secret logs."""
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


class Back(Exception):pass
class Cancel(Exception):pass
class SaveExit(Exception):pass


def text(message):print(message,flush=True)


def question(label,default='',secret=False,reader=None):
    shown='настроен' if secret and default else default
    prompt=label+(' ['+shown+']' if shown else '')+': '
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
    def __init__(self,directory=None,source=None,reader=None,runner=None,draft=None):
        self.source=Path(source or Path(__file__).resolve().parent)
        self.root=Path(directory or '/opt/matrix-stack')
        self.reader=reader;self.runner=runner
        self.draft=Path(draft or Path.home()/'.local/state/misbot/wizard'/
            (hashlib.sha256(str(self.root).encode()).hexdigest()+'.json'))
        self.model={};self.backup={};self.step=0
        if self.draft.exists():
            import s3_config
            from configuration import read_private_json
            saved=read_private_json(self.draft)
            self.model=saved['values'];self.backup=saved.get('backup',{});self.step=saved.get('step',0)
            self.root=Path(saved['directory'])
        elif (self.root/'installation.json').exists() and not (self.root/'configuration-transition.json').exists():
            from configuration import load
            self.model=load(self.root,complete=False)

    def ask(self,label,default='',secret=False):return question(label,default,secret,self.reader)
    def save(self):
        from configuration import directory
        directory(self.draft.parent)
        state_store.write_json(self.draft,{'format':1,'directory':str(self.root),'step':self.step,'values':self.model,'backup':self.backup})

    def field(self,key):
        if key not in schema.FIELDS:
            text('Неизвестное поле; выберите имя из показанного списка.');return
        item=schema.FIELDS[key]
        while True:
            text(key+' — '+item.label+'. '+item.required+'; '+item.impact)
            value=self.ask(key,self.model.get(key,item.default),item.secret)
            try:self.model[key]=schema.field_value(key,value)
            except schema.InvalidConfiguration as error:text(str(error));continue
            self.save();return

    def run(self,path,**values):
        text('Выполняется: '+path+'; установка '+str(self.root))
        args=[sys.executable,str(self.source/'misbot.py'),'--directory',str(self.root),*path.split()]
        for key,value in values.items():args+=['--'+key,str(value)]
        if self.runner:return self.runner(args)
        # Run in the same foreground process group. Ctrl+C reaches backend;
        # wait for it to finish before displaying recovery or accepting changes.
        child=subprocess.Popen(args,stdin=None if path in ('users invitation show','credentials admin show') else subprocess.DEVNULL)
        try:return child.wait()
        except (KeyboardInterrupt,state_store.OperationInterrupted):
            if child.poll() is None:child.send_signal(signal.SIGTERM)
            text('Операция прервана. Ожидается завершение дочернего процесса; checkpoints сохраняются.')
            while child.poll() is None:
                try:child.wait(timeout=1)
                except subprocess.TimeoutExpired:pass
                except KeyboardInterrupt:pass
            text('Проверьте sudo misbot --directory '+shlex.quote(str(self.root))+' status; не удаляйте lock/checkpoints.')
            return 130

    def s3_form(self,purpose):
        import s3_diagnostics as probe
        import s3_config
        values=self.model if purpose=='media' else self.backup
        for key in sorted(s3_config.FIELDS):
            item=schema.FIELDS[key]
            values[key]=self.ask(item.label+' ('+key+')',values.get(key,''),item.secret)
            self.save()
        path=self.draft.parent/(self.draft.stem+'-'+purpose+'.env')
        while True:
            from configuration import serialize
            state_store.write_text(path,serialize({key:values.get(key,'') for key in sorted(s3_config.FIELDS)}))
            result=probe.diagnose(path,purpose,directory=self.root if (self.root/'installation.json').exists() else None,
                progress=lambda item:text(item['stage']+': '+item['status']+' — '+item['explanation']))
            text('Итог: '+result['conclusion'])
            for action in result['next_actions']:text(action)
            if result['passed']:return
            choice=self.ask('1 исправить поле, 2 повторить, 3 локальные медиа/отложить, сохранить и выйти','1')
            if choice=='3':
                if purpose=='media':self.model['MEDIA_MODE']='local'
                else:self.backup={}
                text('Позднее: sudo misbot storage s3 connect --config /root/misbot-s3.env; существующие файлы переносятся и сохраняются.' if purpose=='media' else 'Позднее: misbot backup external create --help; настройки можно сохранить в settings/backup.env.')
                self.save();return
            if choice=='1':
                key=self.ask('Имя поля ('+', '.join(sorted(s3_config.FIELDS))+')')
                if key not in s3_config.FIELDS:text('Неизвестное поле.');continue
                item=schema.FIELDS[key];values[key]=self.ask(item.label,values.get(key,''),item.secret);self.save()

    def setup_step(self,index):
        import preflight
        if index==0:
            self.field('DOMAIN')
            while True:
                path=Path(self.ask('Пустой каталог установки, абсолютный путь; допускаются пробелы',str(self.root)))
                if path.is_absolute() and path!=Path('/') and path.resolve()==path:
                    if path.exists() and any(path.iterdir()) and not (path/'installation.json').exists():text('Каталог чужой и непустой. Выберите другой.');continue
                    self.root=path;self.save();break
                text('Нужен отдельный абсолютный каталог без symlink/..')
        elif index==1:
            import shutil
            missing=[tool for tool in ('dig','ip') if not shutil.which(tool)]
            if missing:raise RuntimeError('Отсутствуют локальные зависимости: '+', '.join(missing)+'. Выполните sudo apt-get update; sudo apt-get install -y dnsutils iproute2 python3-yaml; затем повторите ui. Это не ошибка DNS или S3.')
            text('Проверка Linux amd64, systemd, памяти, диска и портов. Ошибку исправьте и повторите; шаг не пропускается.')
            preflight.system_check(self.root)
        elif index==2:
            self.field('PUBLIC_IP');self.field('ACME_EMAIL')
            domain=self.model['DOMAIN'];ip=self.model.get('PUBLIC_IP')
            text('Создайте A на VPS для: '+', '.join(p+domain for p in ('','matrix.','mas.','admin.','web.','rtc.','turn.'))+'. Удалите AAAA; отключите CDN proxy. Порты описаны в README.')
            if not ip:
                addresses=preflight.resolve(domain,__import__('socket').AF_INET)
                if len(addresses)!=1:raise RuntimeError('DOMAIN не имеет одного A. Исправьте DNS или PUBLIC_IP; повторите этот шаг.')
                ip=next(iter(addresses))
            preflight.dns_check(domain,ip);preflight.caa_check(domain)
            preflight.network_check(self.root,self.model.get('DOCKER_SUBNET','172.30.50.0/24'))
            text('DNS/CAA/система проверены автоматически. До запуска свободные порты проверяются backend.')
        elif index==3:
            self.field('MEDIA_MODE')
            if self.model['MEDIA_MODE']=='s3':self.s3_form('media')
        elif index==4:
            text('Парольный вход доступен без Google/Telegram. OAuth можно пропустить и настроить до запуска позднее.')
            for provider in ('GOOGLE','TELEGRAM'):
                enabled=self.ask('Подключить '+provider+'? да/нет','нет')
                if enabled=='да':
                    self.field(provider+'_CLIENT_ID');self.field(provider+'_CLIENT_SECRET')
                    if provider=='TELEGRAM':self.field('SOCKS5_CONFIG')
            self.field('INVITATION_LIFETIME_SECONDS')
        elif index==5:
            text('Внешние копии требуют отдельного private bucket/prefix и recovery key вне установки. Можно явно отложить; локальная копия не защищает от потери VPS.')
            if self.ask('Настроить внешние копии сейчас? да/отложить','отложить')=='да':
                self.s3_form('backup')
                if self.backup:
                    self.backup['BACKUP_KEY_FILE']=self.ask('Путь recovery key вне установки; отсутствующий ключ будет создан','/root/misbot-backup.key')
                    self.backup['BACKUP_CALENDAR']=self.ask('Systemd calendar (пусто — ручные копии)','')
                    schema.validate_backup(self.backup)
            self.save()
        elif index==6:
            self.model=schema.validate(self.model)
            text('Итог. Внутренние пароли/ключи создаёт backend и сохраняет при повторе.')
            for key,value in self.model.items():
                if value and (value!=schema.FIELDS[key].default or key in ('DOMAIN','MEDIA_MODE','POSTGRES_MODE')):
                    text(key+': '+('настроен' if schema.FIELDS[key].secret else value))
            text('Внешние копии: '+('настроены' if self.backup else 'явно отложены'))
            change=self.ask('Введите имя поля для исправления или применить. Назад/отмена/сохранить и выйти','применить')
            if change!='применить':self.field(change);raise Back()
        elif index==7:
            from configuration import write_model
            if not (self.root/'installation.json').exists():
                code=self.run('config init',domain=self.model['DOMAIN'])
                if code:raise RuntimeError('Init не завершён. Настройки draft сохранены; повторите мастер, не удаляйте secrets.')
            metadata=json.loads((self.root/'installation.json').read_text())
            if metadata.get('databases_initialized'):
                raise RuntimeError('Сервер уже инициализирован. Используйте меню; форму первоначального init применять нельзя.')
            write_model(self.root,self.model,self.backup)
            code=self.run('start')
            if code:raise RuntimeError('Запуск не подтверждён. sudo misbot status; исправьте причину и повторите start. Draft сохранён.')
        elif index==8:
            if self.run('check'):raise RuntimeError('Сервер запущен, но check не прошёл; исправьте конкретную причину и повторите misbot check.')
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
        text('Администратор: @matrix-admin:'+self.model.get('DOMAIN',json.loads((self.root/'installation.json').read_text())['domain']))
        text('Вход: https://web.'+json.loads((self.root/'installation.json').read_text())['domain'])
        text('Приватные реквизиты: '+str(self.root/'secrets/admin-access.json'))
        text('Просмотр пароля: sudo misbot credentials admin show. Смена: misbot credentials admin set --help')
        try:
            result=manage(self.root,'ensure')
            text('Первый токен: состояние '+result['state']+'; срок до '+result['expires_at']+'; ровно одна обычная регистрация.')
            if result['state']=='valid':
                from configuration import read_private_json
                token=read_private_json(self.root/'secrets/first-registration.json')['token']
                text('Регистрационный токен (только этот терминал): '+token)
            text('Не включайте токен в URL. Повторный просмотр: sudo misbot users invitation show')
        except Exception:
            text('Сервер запущен; выдача токена не подтверждена. Сохранённый intent будет продолжен: sudo misbot users invitation ensure. Переустановка не требуется.')

    def wizard(self):
        names=['Домен и каталог','Система','DNS и порты','Медиа','Авторизация','Внешние копии','Итог','Применение и запуск','Проверка','Завершение']
        text('Misbot — мастер SSH. Команды ввода: назад, отмена, сохранить и выйти. Enter сохраняет default/существующий секрет.')
        while self.step<len(names):
            text('\nЭтап '+str(self.step+1)+'/10: '+names[self.step]+'; установка '+str(self.root))
            try:self.setup_step(self.step)
            except Back:self.step=max(0,self.step-1);continue
            except (Cancel,KeyboardInterrupt):text('Ввод отменён; действующая конфигурация не изменена. Draft сохранён.');self.save();return 130
            except SaveExit:self.save();text('Сохранено приватно. Продолжение: sudo python3 '+shlex.quote(str(self.source/'misbot.py'))+' ui --directory '+shlex.quote(str(self.root)));return 0
            except (RuntimeError,OSError):
                text('Этап не завершён. Проверьте misbot config validate/status; данные и ключи сохраняются.')
                # Show only schema/preflight static safe messages, never SDK bodies.
                error=sys.exc_info()[1]
                if type(error) is RuntimeError or isinstance(error,schema.InvalidConfiguration):text(str(error))
                try:self.ask('Повторить шаг / назад / сохранить и выйти','повторить')
                except Back:self.step=max(0,self.step-1)
                except (SaveExit,Cancel,KeyboardInterrupt):self.save();text('Draft сохранён; повторите ui для продолжения.');return 0
                continue
            self.step+=1;self.save()
        text('Мастер завершён. Управление: sudo misbot ui');return 0

    def menu(self):
        text('Misbot — '+str(self.root)+'; Enter не изменяет секреты. Отмена возвращает меню.')
        self.run('status')
        while True:
            text('\n1 статус; 2 проверка; 3 запуск; 4 остановка; 5 настройки; 6 медиа/S3; 7 копии; 8 пользователи/credentials; 9 обновление; 0 выход')
            try:
                choice=self.ask('Выбор','0')
                if choice=='0':return 0
                if choice in ('1','2','3','4'):
                    command={'1':'status','2':'check','3':'start','4':'stop'}[choice]
                    if command=='stop' and self.ask('Остановка служб; данные сохраняются. Выполнить? да/нет','нет')!='да':continue
                    code=self.run(command);text('Код завершения '+str(code)+'. Состояние/продолжение: misbot status.');continue
                if choice=='5':self.settings_menu();continue
                group={'6':'storage','7':'backup','8':'users','9':'update'}.get(choice)
                if not group:continue
                commands=[c for c in cli_commands.COMMANDS if c.path.startswith(group+' ')]
                if choice=='8':commands+=[c for c in cli_commands.COMMANDS if c.path.startswith('credentials ')]
                for i,c in enumerate(commands,1):text(str(i)+' '+c.path+' — '+c.purpose)
                number=self.ask('Номер операции (назад — меню)')
                if not number.isdigit() or not 1<=int(number)<=len(commands):continue
                command=commands[int(number)-1];args={}
                text('Остановка: '+command.stops+' Данные: '+command.data)
                for key in command.arguments:args[key]=self.ask('--'+key)
                if command.backend in ('update','update-recover'):args['source']=self.ask('Доверенный распакованный комплект, абсолютный путь')
                if self.ask('Выполнить '+command.path+'? да/нет','нет')=='да':text('Код завершения '+str(self.run(command.path,**args)))
            except (Back,Cancel,KeyboardInterrupt):text('Отмена ввода; действующие настройки сохранены.');continue
            except SaveExit:text('Выход; действующие настройки сохранены.');return 0
            except RuntimeError as error:
                text('Операция не завершена; данные и checkpoints сохранены.')
                if type(error) is RuntimeError or isinstance(error,schema.InvalidConfiguration):text(str(error))
                text('Проверьте sudo misbot --directory '+shlex.quote(str(self.root))+' status; продолжение указано в результате проверки.')

    def settings_menu(self):
        from configuration import load,apply_settings
        self.model=load(self.root,complete=True)
        text('Immutable DOMAIN/server_name, хранилище и credentials меняются отдельными backend operations.')
        keys=('PUBLIC_IP','ACME_EMAIL','FEDERATION_SERVERS','INVITATION_LIFETIME_SECONDS','INVITATION_USAGE_LIMIT','OPEN_REGISTRATION','REGISTRATION_TOKEN_REQUIRED')
        for key in keys:text(key+' — '+schema.FIELDS[key].label)
        key=self.ask('Имя поля для изменения')
        if key not in keys:text('Поле недоступно обычной форме.');return
        previous=self.model[key];self.field(key)
        text('Изменение '+key+': '+previous+' -> '+self.model[key]+'. Нужна согласованная копия; возможен перезапуск.')
        if self.ask('Применить? да/нет','нет')!='да':return
        apply_settings(self.root,{key:self.model[key]})


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
        if (ui.root/'configuration-transition.json').exists():
            text('Незавершённая публикация: sudo misbot --directory '+shlex.quote(str(ui.root))+' config recover');return 1
        if state.get('databases_initialized'):
            if (ui.root/'configuration-transition.json').exists():
                text('Конфигурация не опубликована полностью. Продолжение: sudo misbot --directory '+shlex.quote(str(ui.root))+' config recover');return 1
            if ui.draft.exists() and 7<=ui.step<=9:
                ui.step=max(8,ui.step);return ui.wizard()
            return ui.menu()
    return ui.wizard()


if __name__=='__main__':
    try:
        def interrupted(signum,frame):raise state_store.OperationInterrupted('SSH/UI interrupted')
        for number in (signal.SIGTERM,signal.SIGHUP):signal.signal(number,interrupted)
        raise SystemExit(main())
    except (KeyboardInterrupt,EOFError,state_store.OperationInterrupted):
        text('Ввод прерван; сохранённый draft и checkpoints оставлены. Повторите sudo misbot ui; проверьте misbot status.');raise SystemExit(130)
    except (OSError,ValueError,RuntimeError,KeyError):
        text('Не удалось безопасно открыть мастер. Сохраните файлы; проверьте misbot status и misbot config validate.');raise SystemExit(1)
