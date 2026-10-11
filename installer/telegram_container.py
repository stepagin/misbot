"""A narrowly mounted Telegram worker; privileged control remains on the host."""
from pathlib import Path
import copy
import json
import re
import sqlite3
import stat

from existing_compose import Error, digest, private_directory, save


def paths(root, model):
    root = Path(root)
    worker = model.get('telegram_control', {})
    name = worker.get('worker_container')
    if not name or not re.fullmatch(r'misbot-[a-z0-9_-]+', name):
        raise Error('Telegram worker container не зарегистрирован.')
    relative = Path(worker.get('worker_config', ''))
    config = root / relative
    data = Path(worker['worker_directory'])
    if (not relative.parts or relative.is_absolute() or '..' in relative.parts or
            not data.is_absolute() or not data.is_relative_to(root) or data == root or
            config.resolve() != config or data.resolve() != data):
        raise Error('Worker config/data должны находиться внутри установки без symlink.')
    socket = Path(worker.get('socket_directory', ''))
    if (socket.parent != Path('/run') or not re.fullmatch(r'misbot-[a-z0-9_-]+', socket.name) or
            socket.resolve() != socket):
        raise Error('Нужен отдельный зарегистрированный control socket в /run.')
    return name, config, data, socket


def service(root, model, image):
    if not re.fullmatch(r'(?:[a-zA-Z0-9._:/-]+@)?sha256:[0-9a-f]{64}', image):
        raise Error('Telegram worker требует неизменяемый image digest.')
    name, config, data, socket = paths(root, model)
    return {
        'container_name': name, 'image': image, 'pull_policy': 'never',
        'restart': 'unless-stopped', 'user': '10001:10001', 'read_only': True,
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'pids_limit': 64, 'mem_limit': '96m', 'memswap_limit': '192m',
        'tmpfs': ['/tmp:rw,size=8m'],
        'environment': {'MISBOT_CONTROL_SOCKET': '/run/control/control.sock'},
        'volumes': [str(config)+':/config/worker.json:ro', str(data)+':/data',
                    str(socket)+':/run/control:ro'],
        'ports': ['127.0.0.1:8088:8080'],
        'logging': {'driver': 'json-file', 'options': {'max-size': '3m', 'max-file': '2'}},
        'healthcheck': {'test': ['CMD', 'python', '-c',
            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"],
            'interval': '20s', 'timeout': '5s', 'retries': 3},
    }


def accepted_mount(installation, name, volume):
    worker = installation.model.get('telegram_control', {})
    if name != worker.get('worker_container') or volume.get('read_only') is not True:
        return False
    _, _, _, socket = paths(installation.root, installation.model)
    if volume.get('source') != str(socket) or volume.get('target') != '/run/control':
        return False
    accepted = {'container': name, 'source': str(socket), 'target': '/run/control'}
    if accepted not in installation.model.get('external_runtime_mounts', []):
        return False
    info = socket.lstat()
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_gid == 10001 and
            stat.S_IMODE(info.st_mode) == 0o750)


def validate_service(installation, name, entry):
    worker = installation.model.get('telegram_control', {})
    if name != worker.get('worker_container'):
        return
    _, config, data, socket = paths(installation.root, installation.model)
    expected = {'/config/worker.json': (config, True), '/data': (data, False), '/run/control': (socket, True)}
    volumes = entry.get('volumes', [])
    if len(volumes) != len(expected):
        raise Error('Telegram worker получает доступ к лишним путям.')
    targets = set()
    for volume in volumes:
        target = volume.get('target')
        source = Path(volume.get('source', ''))
        if (target not in expected or target in targets or volume.get('type') != 'bind' or
                (source, volume.get('read_only', False)) != expected[target]):
            raise Error('Telegram worker mounts отличаются от принятого runtime.')
        targets.add(target)
    if (entry.get('user') != '10001:10001' or entry.get('read_only') is not True or
            set(entry.get('cap_drop', [])) != {'ALL'} or entry.get('cap_add') or entry.get('privileged') or
            entry.get('pid') == 'host' or entry.get('ipc') == 'host' or entry.get('devices') or
            set(entry.get('security_opt', [])) != {'no-new-privileges:true'} or
            entry.get('secrets') or entry.get('configs') or entry.get('volumes_from') or
            entry.get('use_api_socket') or set(entry.get('environment', {})) != {'MISBOT_CONTROL_SOCKET'} or
            entry.get('environment', {}).get('MISBOT_CONTROL_SOCKET') != '/run/control/control.sock'):
        raise Error('Telegram worker требует ограниченные права и readonly root filesystem.')
    info = config.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 10001 or info.st_gid != 10001 or
            stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Error('Изолированный worker config должен принадлежать UID/GID 10001 с mode 0600.')
    info = data.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 10001 or info.st_gid != 10001 or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise Error('Worker data должны принадлежать UID/GID 10001 с mode 0700.')


def restart(installation, allowed=(0,)):
    name, _, _, _ = paths(installation.root, installation.model)
    current = installation.container(name)
    if not current.get('running') or current.get('image_id') != installation.model['images'][name]['id']:
        raise Error('Принятый Telegram worker container не работает.')
    installation.run(['docker', 'restart', name], allowed=allowed)


WORKER_SOURCES = ('Dockerfile', 'requirements.txt', 'worker.py', 'common.py')


def update_needed(installation, entries):
    if not installation.model.get('telegram_control', {}).get('worker_container'):
        return False
    if 'telegram_container.py' not in entries:
        raise Error('Source комплект не поддерживает зарегистрированный Telegram container.')
    for name in WORKER_SOURCES:
        target = 'telegram-bot/' + name
        if target not in entries or not (installation.root / target).is_file():
            raise Error('Source комплект не содержит полный Telegram worker runtime.')
    changed = any(digest(entries['telegram-bot/' + name]) != digest(installation.root / 'telegram-bot' / name)
                  for name in WORKER_SOURCES)
    if changed:
        check_data(installation)
    return changed


def check_data(installation):
    """This release retains worker schema 0; unknown layouts require a migration."""
    from telegram_backup import regular
    _, _, data, _ = paths(installation.root, installation.model)
    path = regular(data / 'worker.sqlite3', 10001)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != 0 or db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise Error('Неизвестная или повреждённая Telegram SQLite; прежний runtime сохранён.')
        for table, expected in (('state', ('key', 'value')), ('users', ('uid', 'server', 'waiting'))):
            actual = tuple(row[1] for row in db.execute('PRAGMA table_info(' + table + ')'))
            if actual != expected:
                raise Error('Telegram SQLite требует отдельной совместимой миграции; прежний runtime сохранён.')


def prepare_update(installation, entries, stage):
    """Build only verified public files; no runtime configuration enters context."""
    context = private_directory(stage / 'worker-build')
    for name in WORKER_SOURCES:
        target = context / name
        target.write_bytes(entries['telegram-bot/' + name].read_bytes())
        target.chmod(0o600)
    dockerfile = (context / 'Dockerfile').read_text()
    bases = re.findall(r'^FROM\s+(\S+)', dockerfile, re.M | re.I)
    if not bases or any(not re.fullmatch(r'[a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}', value) for value in bases):
        raise Error('Telegram build base должен быть закреплён по digest.')
    iid = stage / 'worker-image-id'
    installation.run(['docker', 'build', '--pull=false', '--iidfile', str(iid), str(context)], timeout=600)
    image = iid.read_text().strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise Error('Сборка Telegram не вернула проверяемый image ID.')
    installation.run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
                     '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                     '--pids-limit', '64', '--memory', '96m', '--tmpfs', '/tmp:rw,size=8m',
                     '--entrypoint', 'python', image, 'worker.py', '--help'])
    name, _, _, _ = paths(installation.root, installation.model)
    project = next(item for item in installation.projects if item['name'] == installation.services[name][0])
    pin = installation.control / 'pins' / (project['name'] + '.json')
    state = installation.root / 'installation.json'
    previous = {'state': state, 'pins': pin}
    hashes = {}
    for key, source in previous.items():
        target = stage / ('worker-' + key + '.before')
        target.write_bytes(source.read_bytes()); target.chmod(0o600)
        hashes[key] = digest(target)
    candidate = copy.deepcopy(installation.state)
    candidate['existing']['images'][name] = {'id': image, 'digest': image}
    pins = json.loads(pin.read_text())
    pins['services'][installation.services[name][1]]['image'] = image
    save(stage / 'worker-state.after', candidate)
    save(stage / 'worker-pins.after', pins)
    return {'format': 1, 'container': name, 'project': project['name'], 'image': image,
            'before': hashes, 'after': {key: digest(stage / ('worker-' + key + '.after')) for key in previous}}


def publish_update(installation, stage, record):
    import command_wrapper
    name = record['container']
    project = next(item for item in installation.projects if item['name'] == record['project'])
    installation.container(name)  # Verify ownership before stopping an existing writer.
    installation.run(['docker', 'stop', '--time', '30', name])
    for key, target in (('state', installation.root / 'installation.json'),
                        ('pins', installation.control / 'pins' / (project['name'] + '.json'))):
        source = stage / ('worker-' + key + '.after')
        if digest(source) != record['after'][key]:
            raise Error('Telegram update checkpoint повреждён.')
        command_wrapper.atomic(target, source.read_bytes(), 0o600)
    installation.state = json.loads((installation.root / 'installation.json').read_text())
    installation.model = installation.state['existing']
    installation.run(installation.compose(project, 'up', '-d', '--no-deps', '--pull', 'never',
                     '--no-build', '--wait', '--wait-timeout', '90', installation.services[name][1]), timeout=120)


def recover_update(installation, stage, record):
    import command_wrapper
    if (record.get('format') != 1 or
            record.get('container') != installation.model.get('telegram_control', {}).get('worker_container') or
            record.get('project') != installation.services[record['container']][0]):
        raise Error('Неизвестный Telegram update checkpoint.')
    name = record['container']
    project = next(item for item in installation.projects if item['name'] == record['project'])
    targets = {'state': installation.root / 'installation.json',
               'pins': installation.control / 'pins' / (project['name'] + '.json')}
    for key, target in targets.items():
        before = stage / ('worker-' + key + '.before')
        if digest(before) != record['before'][key] or digest(target) not in (record['before'][key], record['after'][key]):
            raise Error('Telegram checkpoint или принятые настройки изменены; сохраните журнал.')
    current = installation.container(name)
    old = json.loads((stage / 'worker-state.before').read_text())['existing']['images'][name]['id']
    if current.get('exists', True) and current.get('image_id') not in (old, record['image']):
        raise Error('Telegram использует неизвестный образ; recovery не заменяет чужой runtime.')
    installation.run(['docker', 'stop', '--time', '30', name], allowed=(0, 1))
    for key, target in targets.items():
        command_wrapper.atomic(target, (stage / ('worker-' + key + '.before')).read_bytes(), 0o600)
    installation.state = json.loads((installation.root / 'installation.json').read_text())
    installation.model = installation.state['existing']
    installation.run(installation.compose(project, 'up', '-d', '--no-deps', '--pull', 'never',
                     '--no-build', '--wait', '--wait-timeout', '90', installation.services[name][1]), timeout=120)
