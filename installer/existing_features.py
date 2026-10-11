"""Adapters for shared Misbot features on an explicitly adopted installation."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
import argparse
import hashlib
import importlib.util
import shutil
import subprocess
from urllib.parse import urlsplit
from urllib.request import urlopen

import configuration_schema as schema
import command_wrapper
import federation
from existing_compose import Installation, Error, private_directory, save, emit, digest


def mas_api(installation):
    folder = installation.root / installation.model['telegram_control']['directory']
    path = folder / 'bot-config.json'
    info = command_wrapper.regular(path)
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise Error('MAS control config должен быть root-only.')
    config = json.loads(path.read_text())['servers'][installation.state['domain']]
    sys.path.insert(0, str(installation.root / 'referral-control'))
    from mas import MAS
    return MAS(config)


def caddy_block(text, host):
    """Locate one literal site; quoted braces/comments cannot delimit a block."""
    match = list(re.finditer(r'^' + re.escape(host) + r'\s*\{\s*$', text, re.M))
    if len(match) != 1:
        raise Error('Нужен один явный Caddy site для выбранного домена.')
    start = match[0].end(); depth = 1; quote = None; escape = False; comment = False
    for pos in range(start, len(text)):
        char = text[pos]
        if comment:
            if char == '\n': comment = False
            continue
        if quote:
            if escape: escape = False
            elif char == '\\' and quote != '`': escape = True
            elif char == quote: quote = None
            continue
        if char in ('"', '`'): quote = char
        elif char == '#': comment = True
        elif char == '{': depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0: return start, pos
    raise Error('Не удалось определить границы Caddy site.')


def write_in_place(path, data):
    info = command_wrapper.regular(path)
    if path.resolve() != path or info.st_uid != 0 or info.st_mode & 0o022:
        raise Error('Настройки должны принадлежать root без чужой записи.')
    with path.open('wb') as output:
        output.write(data); output.flush(); os.fsync(output.fileno())


def split_site_options(body):
    """Keep non-handler Caddy options outside route/handle wrappers."""
    masked=list(body);quote=None;comment=False;escaped=False
    for index,char in enumerate(body):
        if comment:
            if char=='\n':comment=False
            else:masked[index]=' '
            continue
        if quote:
            if char!='\n':masked[index]=' '
            if escaped:escaped=False
            elif char=='\\' and quote!='`':escaped=True
            elif char==quote:quote=None
        elif char=='#':comment=True;masked[index]=' '
        elif char in ('"',"'",'`'):quote=char;masked[index]=' '
    if quote:raise Error('Незавершённая строка Caddy.')
    masked=''.join(masked);depth=0;offset=0;start=None;spans=[]
    for line in masked.splitlines(keepends=True):
        word=re.match(r'\s*([a-z_]+)\b',line)
        if depth==0 and word:
            if word[1]=='import':raise Error('Caddy import требует отдельного обследования перед добавлением routes.')
            if word[1] in ('tls','log','bind','protocols'):start=offset
        depth+=line.count('{')-line.count('}')
        if depth<0:raise Error('Несогласованные Caddy braces.')
        offset+=len(line)
        if start is not None and depth==0:
            spans.append((start,offset));start=None
    if depth or start is not None:raise Error('Незавершённые Caddy options.')
    options=''.join(body[a:b] for a,b in spans)
    handlers=body
    for a,b in reversed(spans):handlers=handlers[:a]+handlers[b:]
    return options,handlers


def site_model(installation, values=None):
    import yaml
    account=yaml.safe_load((installation.root/installation.model['identity']['mas_config']).read_text()).get('account',{})
    model={'DOMAIN':installation.state['domain'],
           'OPEN_REGISTRATION':'true' if account.get('password_registration_enabled',True) else 'false',
           'REGISTRATION_TOKEN_REQUIRED':'true' if account.get('password_registration_token_required',False) else 'false'}
    model.update(values or {})
    return schema.validate(model)


class Homepage:
    begin='        # Misbot adopted homepage begin\n'
    end='        # Misbot adopted homepage end\n'

    def __init__(self, root):
        self.features=Features(root);self.installation=self.features.installation
        self.folder=self.features.folder
        self.plan=self.installation.model.get('features',{})
        if not self.plan.get('enabled'):raise Error('Сначала включите дополнительные функции через misbot features enable.')

    def links(self):
        web=self.plan['web_url']
        return {'client':web,'login':web+'#/login','register':web+'#/register',
                'homeserver':'https://'+self.installation.state['domain']}

    def current(self):
        import homepage
        values=json.loads((self.folder/'site.json').read_text())
        return {k:values[k] for k in homepage.KEYS}|{'links':self.links()}

    def proposed(self, changes):
        import homepage
        if set(changes)-set(homepage.KEYS):raise Error('Допускаются только поля сайта.')
        model=site_model(self.installation,json.loads((self.folder/'site.json').read_text())|changes)
        if model['HOMEPAGE_MODE']=='external' and urlsplit(model['HOMEPAGE_URL']).hostname in (
                self.plan['primary_domain'],self.plan['admin_domain'],self.installation.state['domain'],urlsplit(self.plan['web_url']).hostname):
            raise Error('Выберите внешний URL, не управляемый hostname.')
        return model

    def candidate(self, model):
        import homepage
        text=Path(self.plan['caddyfile']).read_text()
        if text.count(self.begin)!=1 or text.count(self.end)!=1:raise Error('Homepage markers отсутствуют или неоднозначны; сохраните Caddyfile.')
        if model['HOMEPAGE_MODE']=='landing':
            action='            root * '+self.plan['site_directory']+'\n            file_server\n'
        else:
            target=self.plan['web_url'] if model['HOMEPAGE_MODE']=='element' else model['HOMEPAGE_URL']
            if '#' not in target:target+='#'
            action='            redir '+json.dumps(target)+' 302\n'
        block=self.begin+'        handle / {\n            header Referrer-Policy no-referrer\n            header Cache-Control no-store\n'+action+'        }\n'+self.end
        a=text.index(self.begin);b=text.index(self.end)+len(self.end)
        return text[:a]+block+text[b:]

    def check(self, changes):
        model=self.proposed(changes)
        staged=self.folder/'homepage.proposed'
        command_wrapper.atomic(staged,self.candidate(model).encode(),0o600)
        self.installation.run(['caddy','validate','--config',str(staged),'--adapter','caddyfile'])
        emit('existing_homepage_check',passed=True,identity_preserved=True)
        return model

    def apply(self, changes):
        import homepage
        self.installation.require_no_pending()
        model=self.check(changes)
        stage=private_directory(self.folder/'homepage-checkpoints'/uuid.uuid4().hex)
        site=Path(self.plan['site_directory'])
        paths={'Caddyfile':Path(self.plan['caddyfile']),'site.json':self.folder/'site.json',
               'index.html':site/'index.html','site.css':site/'site.css'}
        for name,path in paths.items():command_wrapper.atomic(stage/name,path.read_bytes(),0o600)
        save(self.folder/'homepage.json',{'format':1,'checkpoint':str(stage)})
        try:
            command_wrapper.atomic(site/'index.html',homepage.render(model,self.links()).encode(),0o644)
            write_in_place(Path(self.plan['caddyfile']),self.candidate(model).encode())
            self.installation.run(['systemctl','reload','caddy'])
            with urlopen('https://'+self.installation.state['domain']+'/_matrix/client/versions',timeout=15) as response:
                if response.status!=200:raise Error('Matrix API после reload не подтверждён.')
            save(self.folder/'site.json',{k:model[k] for k in homepage.KEYS})
            save(self.folder/'homepage-last.json',{'format':1,'checkpoint':str(stage)})
            (self.folder/'homepage.json').unlink()
        except BaseException:
            self.recover();raise
        emit('existing_homepage',passed=True,mode=model['HOMEPAGE_MODE'],core_restarted=False)

    def recover(self, rollback=False):
        path=self.folder/('homepage-last.json' if rollback else 'homepage.json')
        if not path.exists():emit('existing_homepage_recovery',pending=False);return
        stage=Path(json.loads(path.read_text())['checkpoint'])
        if stage.parent!=self.folder/'homepage-checkpoints' or stage.resolve()!=stage:raise Error('Неверный homepage checkpoint.')
        site=Path(self.plan['site_directory'])
        for name,target in {'Caddyfile':Path(self.plan['caddyfile']),'site.json':self.folder/'site.json',
                            'index.html':site/'index.html','site.css':site/'site.css'}.items():
            if name=='Caddyfile':write_in_place(target,(stage/name).read_bytes())
            else:command_wrapper.atomic(target,(stage/name).read_bytes(),0o600 if name=='site.json' else 0o644)
        self.installation.run(['systemctl','reload','caddy'])
        (self.folder/'homepage.json').unlink(missing_ok=True)
        emit('existing_homepage_recovery',passed=True,core_restarted=False)


class Features:
    def __init__(self, root):
        self.installation = Installation(root)
        self.root = self.installation.root
        self.folder = private_directory(self.installation.control / 'features')

    def status(self):
        value = self.installation.model.get('features', {})
        emit('existing_features', enabled=value.get('enabled', False),
             postgres='local', vault=False,
             pending=(self.folder / 'enable.json').exists(),
             homepage=value.get('primary_domain'), admin=value.get('admin_domain'),
             referral_bot=self.installation.state.get('referral_provisioned', False),
             federation_bot=self.installation.state.get('federation_provisioned', False))

    def plan(self, path):
        info = command_wrapper.regular(path)
        if path.resolve() != path or info.st_uid != 0 or info.st_mode & 0o077:
            raise Error('Feature plan должен быть root-only JSON без symlink.')
        value = json.loads(path.read_text())
        required={'format', 'primary_domain', 'admin_domain', 'web_url', 'caddyfile', 'site_directory', 'image_archive', 'image_manifest'}
        if not required.issubset(value) or set(value)-required-{'telegram_runtime'} or value['format'] != 1:
            raise Error('Неполный или неизвестный feature plan.')
        for name in ('primary_domain', 'admin_domain'):value[name] = schema.domain(value[name])
        web = urlsplit(value['web_url'])
        if web.scheme != 'https' or not web.hostname or web.username or web.password or web.query or web.fragment or web.path != '/':
            raise Error('Нужен HTTPS origin существующего Element Web с / в конце.')
        if value['admin_domain'] in (value['primary_domain'], web.hostname, self.installation.state['domain']):
            raise Error('Admin hostname занят действующей службой.')
        if value['caddyfile'] not in self.installation.model.get('external_backup_paths', []):
            raise Error('Caddyfile должен входить в принятую резервную копию.')
        for name in ('caddyfile', 'site_directory', 'image_archive', 'image_manifest'):
            target = Path(value[name])
            if not target.is_absolute() or target == Path('/') or target.resolve() != target or not re.fullmatch(r'/[A-Za-z0-9._/-]+',str(target)):
                raise Error('Feature пути должны быть абсолютными без symlink.')
        if not Path(value['site_directory']).is_relative_to(Path('/srv')):
            raise Error('Публичные assets размещаются в отдельном каталоге /srv.')
        for name in ('image_archive', 'image_manifest'):
            if not Path(value[name]).is_relative_to(self.folder):
                raise Error('Публичные image артефакты должны быть в features каталоге.')
        return value

    def enable(self, path):
        import yaml
        installation = self.installation
        installation.require_no_pending()
        installation.validate()
        value = self.plan(path)
        if installation.model.get('features', {}).get('enabled') and not (self.folder/'enable.json').exists():
            self.status(); return
        pending = self.folder / 'enable.json'
        previous = json.loads(pending.read_text()) if pending.exists() else None
        if previous and previous.get('plan') != value:
            raise Error('Для продолжения верните прежний feature plan; checkpoint сохранён.')
        selected = json.loads((installation.control / 'last-backup.json').read_text())
        if not selected.get('remote', {}).get('verified'):
            raise Error('Перед новыми компонентами нужна проверенная внешняя копия Misbot.')
        memory = dict((line.split(':')[0], int(line.split()[1])*1024) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:', 'SwapTotal:')))
        if memory['MemTotal'] < 2*1024**3 and memory['SwapTotal'] < 1024**3:
            self.prepare_swap()
        if shutil.disk_usage(self.root).free < 1536*1024**2:
            raise Error('Для дополнительных компонентов нужен резерв диска 1.5 GiB.')
        manifest_path, archive = Path(value['image_manifest']), Path(value['image_archive'])
        for p in (manifest_path, archive):
            info = command_wrapper.regular(p)
            if info.st_uid != 0 or info.st_mode & 0o077:raise Error('Image артефакты должны быть root-only.')
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('format') != 1 or digest(archive) != manifest['archive_sha256']:
            raise Error('Image archive не соответствует manifest.')
        images = manifest['images']
        if set(images) != {'referral-bot', 'federation-bot', 'admin'}:
            raise Error('Неизвестный набор дополнительных images.')
        for role in ('referral-bot', 'federation-bot'):
            actual = {p.name: digest(p) for p in (self.root / role).iterdir() if p.is_file()}
            if actual != images[role]['source_sha256']:
                raise Error('Bot image собран из других исходников.')
        stage = Path(previous['checkpoint']) if previous else private_directory(self.folder / 'checkpoints' / uuid.uuid4().hex)
        if previous:
            if stage.parent != self.folder/'checkpoints' or stage.resolve()!=stage:raise Error('Неверный feature checkpoint.')
            self.recover()
        else:
            command_wrapper.atomic(stage / 'installation.before', (self.root / 'installation.json').read_bytes(), 0o600)
            command_wrapper.atomic(stage / 'Caddyfile.before', Path(value['caddyfile']).read_bytes(), 0o600)
            save(self.folder / 'enable.json', {'format': 1, 'checkpoint': str(stage), 'plan': value, 'phase': 'prepared'})
        installation.run(['docker', 'image', 'load', '-i', str(archive)], timeout=300)
        for role, image in images.items():
            if not re.fullmatch('sha256:[0-9a-f]{64}', image['id']):raise Error('Неверный image ID.')
            _, actual = installation.run(['docker', 'image', 'inspect', image['id'], '--format', '{{.Id}}'])
            if actual.decode().strip() != image['id']:raise Error('Принятый image не найден после load.')
        emit('existing_features_stage',stage='images_verified')
        self.provision(value)
        emit('existing_features_stage',stage='ordinary_bot_sessions_verified')
        import referral, federation_runtime
        metadata = installation.state
        federation.setup(self.root, metadata)
        services = {'referral-bot': referral.services()['referral-bot'], 'federation-bot': federation_runtime.service(),
            'admin': {'restart': 'unless-stopped', 'mem_limit': '48m', 'memswap_limit': '96m',
                      'environment': {'SERVER_NAME': metadata['domain']}, 'ports': ['127.0.0.1:8092:8080'],
                      'pids_limit': 64, 'logging': {'driver':'json-file', 'options':{'max-size':'3m','max-file':'2'}}}}
        services['referral-bot'].pop('depends_on', None)
        control = Path(value.get('telegram_runtime',{}).get('socket_directory',
            str(self.root / installation.model['telegram_control']['directory'] / 'bot-run')))
        if not control.is_relative_to(self.root):
            installation.model['external_runtime_mounts']=[{'container':'misbot-referral-bot',
                'source':str(control),'target':'/run/control'}]
        for role in services:
            entry = services[role]; name = 'misbot-' + role
            entry.update(image=images[role]['id'], container_name=name, pull_policy='never')
            if role != 'admin':
                entry['volumes'] = [str(self.root / 'config' / (role+'.json'))+':/config/'+('referral' if role=='referral-bot' else 'federation')+'.json:ro',
                    str(control if role=='referral-bot' else self.root/'run/federation')+':/run/control:ro',
                    str(self.root/'data'/role)+':/data']
            if role=='federation-bot':entry['volumes'].append(str(self.root/'config/welcome-bot.json')+':/config/welcome.json:ro')
            installation.model['images'][name] = {'id': images[role]['id'], 'digest': images[role]['id']}
        project = {'name': 'misbot-addons', 'directory': 'addons', 'services': {role:'misbot-'+role for role in services}}
        directory = private_directory(self.root / 'addons')
        command_wrapper.atomic(directory / 'docker-compose.yml', yaml.safe_dump({'services': services},sort_keys=False).encode(), 0o600)
        save(installation.control / 'pins/misbot-addons.json', {'services':{role:{'image':entry['image'],'pull_policy':'never'} for role,entry in services.items()}})
        if not any(p['name']=='misbot-addons' for p in installation.model['projects']):
            installation.model['projects'].append(project)
        installation.model['writers'] = list(dict.fromkeys(['misbot-referral-bot', 'misbot-federation-bot', *installation.model['writers']]))
        installation.model['features'] = dict(value, enabled=False)
        save(self.root / 'installation.json', metadata)
        installation = Installation(self.root); self.installation = installation
        installation.validate()
        federation_runtime.controller(self.root, lambda cmd: installation.run(cmd)[1].decode())
        metadata['existing']['external_backup_paths'] = list(dict.fromkeys([
            *installation.model.get('external_backup_paths', []),
            str(Path(value['site_directory'])),
            str(Path('/etc/systemd/system')/federation_runtime.unit_name(self.root))]))
        save(self.root / 'installation.json', metadata)
        installation.run(installation.compose(project, 'up', '-d', '--pull', 'never', '--no-build', '--wait', '--wait-timeout', '180'), timeout=210)
        for role, uid in (('referral-bot',10002),('federation-bot',10003)):
            if not (self.root/'data'/role/'cross-signing.json').is_file():raise Error('Bot crypto ещё не подтверждено; сохраните checkpoint.')
            metadata[role.replace('-','_').replace('_bot','')+'_crypto_initialized'] = True
        if not (self.root/'data/federation-bot/welcome/cross-signing.json').is_file():raise Error('Welcome crypto ещё не подтверждено.')
        metadata['welcome_crypto_initialized']=True
        save(self.root / 'installation.json', metadata)
        self.installation = Installation(self.root)
        emit('existing_features_stage',stage='bots_crypto_verified')
        self.install_proxy(value)
        emit('existing_features_stage',stage='public_routes_verified')
        metadata['existing']['features']['enabled'] = True
        save(self.root / 'installation.json', metadata)
        self.installation = Installation(self.root)
        self.refresh_telegram(value,stage)
        if value.get('telegram_runtime'):
            metadata['existing']['external_backup_paths']=list(dict.fromkeys([
                *metadata['existing']['external_backup_paths'],
                *[str(Path('/etc/systemd/system')/value['telegram_runtime'][k]) for k in ('control_unit','worker_unit')]]))
            save(self.root/'installation.json',metadata)
        (self.folder / 'enable.json').unlink()
        self.status()

    def refresh_telegram(self, plan, stage):
        value=plan.get('telegram_runtime')
        if value is None:return False
        if (not isinstance(value,dict) or not {'directory','control_unit','worker_unit'}.issubset(value) or
                set(value)-{'directory','control_unit','worker_unit','socket_directory'}):
            raise Error('Неверный Telegram runtime plan.')
        runtime=Path(value['directory'])
        if not runtime.is_absolute() or runtime.resolve()!=runtime or runtime.parent!=Path('/usr/local/lib'):
            raise Error('Telegram runtime должен быть отдельным каталогом /usr/local/lib.')
        info=runtime.stat()
        if info.st_uid!=0 or info.st_mode&0o022:raise Error('Небезопасный Telegram runtime.')
        config=self.root/self.installation.model['telegram_control']['directory']/'bot-config.json'
        for key in ('control_unit','worker_unit'):
            if not re.fullmatch('misbot-[a-z0-9_-]+\\.service',value[key]):raise Error('Неверное имя Telegram unit.')
            path=Path('/etc/systemd/system')/value[key];info=command_wrapper.regular(path)
            if info.st_uid!=0 or info.st_mode&0o022 or str(runtime) not in path.read_text():
                raise Error('Telegram unit не соответствует принятому runtime.')
        mapping={name:self.root/('telegram-bot' if name in ('worker.py','common.py','README.md') else 'referral-control')/name
                 for name in ('bridge.py','mas.py','control.py','worker.py','common.py','README.md')}
        previous={}
        for name in mapping:
            target=runtime/name;info=command_wrapper.regular(target)
            if info.st_uid!=0 or info.st_mode&0o022:raise Error('Небезопасный Telegram public file.')
            previous[name]=target.read_bytes()
        info=command_wrapper.regular(config)
        if info.st_uid!=0 or info.st_mode&0o077:raise Error('Telegram config должен быть root-only.')
        before=config.read_bytes();settings=json.loads(before)
        server=settings['servers'][self.installation.state['domain']]
        server.update(federation_enabled=True,federation_backend='misbot')
        server['management_actions']=list(dict.fromkeys([*server.get('management_actions',[]),'components']))
        saved=private_directory(stage/'telegram-public-before')
        for name,data in previous.items():command_wrapper.atomic(saved/name,data,0o600)
        command_wrapper.atomic(saved/'bot-config.before',before,0o600)
        control_unit=Path('/etc/systemd/system')/value['control_unit']
        unit_before=control_unit.read_bytes()
        command_wrapper.atomic(saved/'control-unit.before',unit_before,0o600)
        try:
            unit_text=unit_before.decode()
            if value.get('socket_directory') and 'RuntimeDirectoryPreserve=' not in unit_text:
                if unit_text.count('[Service]')!=1:raise Error('Неверная структура Telegram unit.')
                unit_text=unit_text.replace('[Service]','[Service]\nRuntimeDirectoryPreserve=restart',1)
                command_wrapper.atomic(control_unit,unit_text.encode(),0o644)
                self.installation.run(['systemctl','daemon-reload'])
            for name,source in mapping.items():command_wrapper.atomic(runtime/name,source.read_bytes(),0o644)
            save(config,settings)
            self.installation.run(['systemctl','restart',value['control_unit'],value['worker_unit']])
            self.installation.run(['systemctl','is-active','--quiet',value['control_unit'],value['worker_unit']])
        except BaseException:
            for name,data in previous.items():command_wrapper.atomic(runtime/name,data,0o644)
            command_wrapper.atomic(config,before,0o600)
            command_wrapper.atomic(control_unit,unit_before,0o644)
            self.installation.run(['systemctl','daemon-reload'])
            self.installation.run(['systemctl','restart',value['control_unit'],value['worker_unit']],allowed=(0,1))
            raise
        emit('existing_telegram_runtime',passed=True,core_restarted=False,shared_federation_backend=True)
        return True

    def prepare_swap(self):
        # Keep swap outside the backup payload; root-only and independently owned.
        if shutil.disk_usage(self.root).free < 2560*1024**2:
            raise Error('Для 1 GiB swap и компонентов нужен резерв диска 2.5 GiB.')
        folder=private_directory(Path('/var/lib/misbot-swap'))
        swap=folder/(self.installation.state['domain']+'.swap')
        marker=folder/(self.installation.state['domain']+'.json')
        name='misbot-swap-'+hashlib.sha256(self.installation.state['domain'].encode()).hexdigest()[:16]+'.service'
        unit=Path('/etc/systemd/system')/name
        header='# Misbot adopted swap '+str(self.root)+'\n'
        if swap.exists() and not marker.exists():raise Error('Swap path занят другим файлом; ничего не удалено.')
        if unit.exists() and not unit.read_text().startswith(header):raise Error('Swap unit принадлежит другой задаче.')
        if not swap.exists():
            save(marker,{'format':1,'root':str(self.root),'path':str(swap),'phase':'creating'})
            with swap.open('xb') as output:
                os.fchmod(output.fileno(),0o600)
                block=b'\0'*(1024**2)
                for _ in range(1024):output.write(block)
                output.flush();os.fsync(output.fileno())
            self.installation.run(['mkswap',str(swap)])
            save(marker,{'format':1,'root':str(self.root),'path':str(swap),'phase':'ready'})
        saved=json.loads(marker.read_text())
        info=command_wrapper.regular(swap)
        if saved.get('root')!=str(self.root) or saved.get('phase')!='ready' or info.st_uid!=0 or info.st_mode & 0o077 or info.st_size!=1024**3:
            raise Error('Swap preparation прервана; сохраните её собственный marker и файл.')
        text=header+'[Unit]\nDescription=Misbot production swap\nAfter=local-fs.target\nBefore=docker.service\n[Service]\nType=oneshot\nRemainAfterExit=true\nExecStart=/sbin/swapon '+str(swap)+'\n[Install]\nWantedBy=multi-user.target\n'
        command_wrapper.atomic(unit,text.encode(),0o644)
        self.installation.run(['systemctl','daemon-reload'])
        self.installation.run(['systemctl','enable','--now',name])
        emit('existing_feature_resources',swap_bytes=1024**3,core_restarted=False)

    def provision(self, plan):
        installation = self.installation
        api = mas_api(installation)
        control = private_directory(self.root/'data/referral-control')
        cfg = dict(api.config)
        save(control/'bot-config.json', {'servers':{installation.state['domain']:cfg}})
        policy = json.loads((self.root/installation.model['telegram_control']['directory']/'bot-policy.json').read_text())['servers'][installation.state['domain']]
        save(control/'bot-policy.json', {'servers':{installation.state['domain']:policy}})
        spec = importlib.util.spec_from_file_location('misbot_provision', self.root/'referral-control/provision.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        admins = ['@'+user['attributes']['username']+':'+installation.state['domain'] for user in api.items('users')
            if user['attributes'].get('admin', user['attributes'].get('can_request_admin',False)) and not user['attributes'].get('locked_at') and not user['attributes'].get('deactivated_at')]
        if not admins:raise Error('Не подтверждён действующий местный MAS admin.')
        for role, uid, gid in (('referral-bot',10002,10001),('federation-bot',10003,10003),('welcome-bot',10003,10003)):
            username=None
            if role=='federation-bot' and (self.root/'config/admin-panel-identity.json').exists():
                import federation_runtime
                username=federation_runtime.identity(self.root,installation.state)['user_id'][1:].split(':')[0]
            result = module.provision(control, api, federation=role=='federation-bot',welcome=role=='welcome-bot',username=username)
            config = result['config']
            # Provisioning is performed on the host; the bot uses the public TLS URL.
            config['homeserver_url'] = 'https://'+installation.state['domain']
            import requests
            response=requests.get(config['homeserver_url']+'/_matrix/client/v3/account/whoami',
                headers={'Authorization':'Bearer '+config['access_token']},timeout=15,allow_redirects=False)
            if response.status_code!=200 or response.json().get('user_id')!=config['user_id'] or response.json().get('device_id')!=config['device_id']:
                raise Error('Сохранённая bot session не подтверждена; не заменяйте устройство или ключи.')
            if role=='federation-bot':
                command_wrapper.public_directory(self.root/'config')
                import federation_runtime
                identity_path=self.root/'config/admin-panel-identity.json'
                if not identity_path.exists():
                    prior=self.root/'config/federation-bot.json'
                    previous=json.loads(prior.read_text()) if prior.exists() else {}
                    save(identity_path,{'user_id':config['user_id'],'administrator_mxid':(previous.get('administrator_mxids') or admins)[0]})
                identity=federation_runtime.identity(self.root,installation.state)
                if identity['user_id']!=config['user_id']:raise Error('Сохранённый тег бота не совпадает с session.')
                config['administrator_mxids']=[identity['administrator_mxid']]
            command_wrapper.public_directory(self.root/'config')
            command_wrapper.public_directory(self.root/'secrets')
            (self.root/'secrets').chmod(0o700)
            target=self.root/'config'/(role+'.json')
            if target.exists():
                info=command_wrapper.regular(target)
                if target.is_symlink() or info.st_uid not in (0,uid) or info.st_mode & 0o077:raise Error('Небезопасный bot config.')
            command_wrapper.atomic(target,(json.dumps(config,indent=2)+'\n').encode(),0o600)
            os.chown(self.root/'config'/(role+'.json'),uid,gid)
            save(self.root/'secrets'/('welcome-session.json' if role=='welcome-bot' else 'federation-session.json' if role=='federation-bot' else 'referral-session.json'),result['metadata'])
            folder = self.root/'data'/('federation-bot/welcome' if role=='welcome-bot' else role);folder.mkdir(mode=0o700,parents=True,exist_ok=True);folder.chmod(0o700);os.chown(folder,uid,gid)
            installation.state[role.replace('-bot','')+'_provisioned'] = True

    def install_proxy(self, plan):
        # Narrow added routes; the complete original site remains the fallback.
        path = Path(plan['caddyfile']);text = path.read_text()
        start,end = caddy_block(text,plan['primary_domain'])
        options, original = split_site_options(text[start:end])
        if '# Misbot adopted features begin' in text:raise Error('Feature proxy уже опубликован; используйте recovery.')
        site = Path(plan['site_directory']);command_wrapper.public_directory(site)
        import homepage
        model = site_model(self.installation, {'SITE_NAME':plan['primary_domain']})
        links = {'client':plan['web_url'],'login':plan['web_url']+'#/login','register':plan['web_url']+'#/register','homeserver':'https://'+self.installation.state['domain']}
        command_wrapper.atomic(site/'index.html',homepage.render(model,links).encode(),0o644)
        command_wrapper.atomic(site/'site.css',(self.root/'site/site.css').read_bytes(),0o644)
        save(self.folder/'site.json',{k:model[k] for k in homepage.KEYS})
        block = '\n    # Misbot adopted features begin\n    route {\n'
        block += '        # Misbot adopted homepage begin\n        handle / {\n            header Referrer-Policy no-referrer\n            header Cache-Control no-store\n            root * '+str(site)+'\n            file_server\n        }\n        # Misbot adopted homepage end\n'
        block += '        handle /site.css {\n            root * '+str(site)+'\n            file_server\n        }\n'
        public_socket='/run/misbot-federation-'+hashlib.sha256(self.installation.state['domain'].encode()).hexdigest()[:16]+'/http.sock'
        block += '        handle /.well-known/misbot {\n            reverse_proxy unix/'+public_socket+'\n        }\n'
        block += '        handle /_misbot/v1/exchange {\n            request_body {\n                max_size 32KB\n            }\n            reverse_proxy unix/'+public_socket+'\n        }\n'
        block += '        handle {\n'+original+'\n        }\n    }\n    # Misbot adopted features end\n'
        candidate = text[:start]+options+block+text[end:]
        # Identity domain needs Misbot discovery too, even if the short site differs.
        if plan['primary_domain'] != self.installation.state['domain']:
            a,b = caddy_block(candidate,self.installation.state['domain'])
            identity_options,identity_handlers=split_site_options(candidate[a:b])
            prefix = '\n    route {\n        handle /.well-known/misbot {\n            reverse_proxy unix/'+public_socket+'\n        }\n        handle /_misbot/v1/exchange {\n            request_body {\n                max_size 32KB\n            }\n            reverse_proxy unix/'+public_socket+'\n        }\n        handle {\n'
            candidate = candidate[:a]+identity_options+prefix+identity_handlers+'\n        }\n    }\n'+candidate[b:]
        if re.search(r'^'+re.escape(plan['admin_domain'])+r'\s*\{',candidate,re.M):raise Error('Admin domain уже имеет Caddy site; сохраняйте действующую службу.')
        candidate += '\n'+plan['admin_domain']+' {\n    reverse_proxy 127.0.0.1:8092\n}\n'
        staged = self.folder/'Caddyfile.proposed'
        command_wrapper.atomic(staged,candidate.encode(),0o600)
        self.installation.run(['caddy','validate','--config',str(staged),'--adapter','caddyfile'])
        write_in_place(path,candidate.encode())
        self.installation.run(['systemctl','reload','caddy'])
        for origin,path_suffix in ((plan['primary_domain'],'/'),(plan['admin_domain'],'/'),(self.installation.state['domain'],'/.well-known/misbot'),(self.installation.state['domain'],'/_matrix/client/versions')):
            with urlopen('https://'+origin+path_suffix,timeout=15) as response:
                if response.status != 200:raise Error('Новый публичный маршрут не подтверждён.')

    def recover(self):
        path = self.folder/'enable.json'
        if not path.exists():self.status();return
        value = json.loads(path.read_text());stage = Path(value['checkpoint'])
        if stage.parent != self.folder/'checkpoints' or stage.resolve()!=stage:raise Error('Неверный feature checkpoint.')
        # Revert only public routes. Preserve newly issued bot sessions and crypto.
        write_in_place(Path(value['plan']['caddyfile']),(stage/'Caddyfile.before').read_bytes())
        self.installation.run(['systemctl','reload','caddy'])
        emit('existing_features_recovery', public_routes_restored=True, bot_identity_retained=True,
                continuation='Repair the failed phase, then repeat misbot features enable --config PATH with the same plan.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--config',type=Path)
    parser.add_argument('action',choices=('features-enable','features-status','features-recover','homepage-show','homepage-check','homepage-apply','homepage-recover','homepage-rollback'))
    for name in ('mode','url','name','description','support-url'):parser.add_argument('--'+name)
    args=parser.parse_args()
    if os.geteuid()!=0:return 77
    os.umask(0o077)
    try:
        feature=Features(args.directory)
        if args.action.startswith('homepage-'):
            page=Homepage(args.directory)
            if args.action=='homepage-show':print(json.dumps(page.current(),ensure_ascii=False));return 0
            with feature.installation.lock():
                if args.action in ('homepage-recover','homepage-rollback'):page.recover(args.action=='homepage-rollback')
                else:
                    changes={key:getattr(args,name) for name,key in (('mode','HOMEPAGE_MODE'),('url','HOMEPAGE_URL'),('name','SITE_NAME'),('description','SITE_DESCRIPTION'),('support_url','SITE_SUPPORT_URL')) if getattr(args,name) is not None}
                    (page.apply if args.action=='homepage-apply' else page.check)(changes)
            return 0
        if args.action=='features-status':feature.status();return 0
        with feature.installation.lock():
            if args.action=='features-enable':
                if not args.config:raise Error('Нужен --config с feature plan.')
                feature.enable(args.config)
            else:feature.recover()
        return 0
    except Exception as error:
        emit('existing_features',passed=False,explanation=str(error) if isinstance(error,Error) else 'Feature operation не подтверждена. Сохраните checkpoint и выполните misbot features recover.')
        return 1




class Federation(federation.Backend):
    def __init__(self, root, **kwargs):
        super().__init__(root, **kwargs)
        self.installation = Installation(root)

    def guard(self):
        return self.installation.lock()

    def restart_scope(self):return 'synapse'

    def ready(self, recovery=False):
        if not self.admission:
            return
        self.installation.require_no_pending()
        self.installation.validate()
        if not recovery and not self.installation.model.get('features', {}).get('enabled'):
            raise Error('Сначала завершите misbot features enable.')
        if (self.root / 'federation-transition.json').exists() and not recovery:
            raise Error('Есть незавершённая политика; выполните misbot federation recover.')

    def check_admin(self, actor):
        if actor == 'root':
            return os.geteuid() == 0
        try:
            api = mas_api(self.installation)
            user = api.matrix_user(actor, self.installation.state['domain'])
            api.administrator_user(user['id'])
            return True
        except Exception:
            return False

    def servers(self):
        import yaml
        target = self.root / self.installation.model['identity']['synapse_config']
        values = yaml.safe_load(target.read_text()).get('federation_domain_whitelist')
        if not isinstance(values, list) or not values:
            raise Error('Открытая федерация не преобразуется в allowlist автоматически.')
        return [schema.domain(v) for v in values]

    def auto_accept(self):
        path = self.installation.control / 'features/policy.json'
        return path.exists() and json.loads(path.read_text()).get('auto_accept') is True

    def set_auto_accept(self, value):
        folder = private_directory(self.installation.control / 'features')
        save(folder / 'policy.json', {'auto_accept': value == 'true'})

    def matrix_operation(self, action, argument):
        # Execute the existing fixed membership helper inside the accepted Synapse.
        config = json.loads((self.root / 'config/federation-bot.json').read_text())
        payload = json.dumps({'session': config, 'action': action, 'argument': argument}).encode()
        with tempfile.TemporaryFile() as incoming:
            incoming.write(payload); incoming.seek(0)
            _, output = self.installation.run(['docker', 'exec', '-i',
                self.installation.media_service()[2], 'python', '-c',
                (self.root / 'federation_matrix_api.py').read_text()], incoming=incoming)
        value = json.loads(output)
        if not value.get('ok'):
            raise federation.protocol.ProtocolError(value.get('code', 'matrix_unavailable'))
        return value['result']

    def apply_policy(self, servers, actor='root'):
        installation = self.installation
        installation.run(['/usr/local/bin/misbot', '--directory', str(self.root), 'backup', 'create'], timeout=1800)
        selected = json.loads((installation.control / 'last-backup.json').read_text())
        if not selected.get('remote', {}).get('verified'):
            installation.run(['/usr/local/bin/misbot', '--directory', str(self.root), 'backup', 'verify'], timeout=1800)
        self.authorize(actor)
        stage = private_directory(self.root / 'stages/federation' / uuid.uuid4().hex)
        target = self.root / installation.model['identity']['synapse_config']
        command_wrapper.atomic(stage / 'homeserver.before', target.read_bytes(), 0o600)
        save(self.root / 'federation-transition.json', {'format': 1,
            'stage': str(stage.relative_to(self.root)), 'servers': servers,
            'before_sha256': digest(stage / 'homeserver.before')})
        try:
            return self.recover_policy()
        except BaseException:
            self.recover_policy(True)
            raise

    def recover_policy(self, rollback=False):
        import yaml
        path = self.root / 'federation-transition.json'
        if not path.exists():
            return {'status': 'unchanged'}
        value = json.loads(path.read_text())
        if value.get('format') != 1 or not re.fullmatch('stages/federation/[0-9a-f]{32}', value.get('stage', '')):
            raise Error('Неверный checkpoint федерации.')
        saved = self.root / value['stage'] / 'homeserver.before'
        if saved.resolve() != saved or digest(saved) != value['before_sha256']:
            raise Error('Checkpoint федерации повреждён.')
        original = saved.read_bytes()
        config = yaml.safe_load(original)
        if config.get('server_name') != self.installation.state['domain']:
            raise Error('Identity в checkpoint отличается.')
        servers = [schema.domain(v) for v in value['servers']]
        if not servers:
            raise Error('Пустой allowlist не допускается.')
        config['federation_domain_whitelist'] = servers
        data = original if rollback else yaml.safe_dump(config, sort_keys=False).encode()
        target = self.root / self.installation.model['identity']['synapse_config']
        with target.open('wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        project, service, name, _ = self.installation.media_service()
        self.installation.restart_media(project, service)
        self.installation.validate()
        path.unlink()
        return {'status': 'rolled_back' if rollback else 'applied', 'servers': self.servers(),
                'identity_preserved': True, 'federation_exchange': 'not_verified'}


if __name__=='__main__':raise SystemExit(main())
