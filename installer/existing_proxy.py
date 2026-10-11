"""Use the registered Caddy runtime without exposing the installation to it."""

import hashlib
from pathlib import Path
import stat

import command_wrapper
from existing_compose import Error


class Proxy:
    def __init__(self, installation):
        self.installation = installation
        self.model = installation.model.get('proxy')
        if self.model is None:
            return
        required = {'format', 'container', 'config_directory', 'data_directory', 'state_directory'}
        if (
            not isinstance(self.model, dict)
            or set(self.model) != required
            or self.model['format'] != 1
            or self.model['container'] not in installation.services
        ):
            raise Error('Неизвестный или незарегистрированный Caddy runtime.')
        directories = []
        for key in ('config_directory', 'data_directory', 'state_directory'):
            relative = Path(self.model[key])
            path = installation.root / relative
            if (
                relative.is_absolute()
                or '..' in relative.parts
                or not relative.parts
                or relative.parts[0] != 'runtime'
                or path == installation.root
                or path.resolve() != path
            ):
                raise Error('Caddy runtime должен находиться внутри установки без symlink.')
            if any(
                path.is_relative_to(other) or other.is_relative_to(path) for other in directories
            ):
                raise Error('Caddy config/data/state должны находиться в отдельных каталогах.')
            directories.append(path)
        config = installation.root / self.model['config_directory'] / 'Caddyfile'
        if installation.model.get('features', {}).get('caddyfile') != str(config):
            raise Error('Путь Caddyfile не соответствует принятому runtime.')

    def validate(self, path):
        path = Path(path)
        info = command_wrapper.regular(path)
        if path.resolve() != path or info.st_uid != 0 or info.st_mode & 0o022:
            raise Error('Caddyfile должен принадлежать root без чужой записи.')
        if self.model is None:
            return self.installation.run(
                ['caddy', 'validate', '--config', str(path), '--adapter', 'caddyfile']
            )
        name = self.model['container']
        image = self.installation.model['images'][name]['digest']
        # Validate in an isolated disposable container, including while the
        # actual proxy is stopped. No Docker socket or installation-wide mount.
        arguments = [
            'docker',
            'run',
            '--rm',
            '--pull',
            'never',
            '--network',
            'none',
            '--read-only',
            '--cap-drop',
            'ALL',
            '--cap-add',
            'NET_BIND_SERVICE',
            '--security-opt',
            'no-new-privileges:true',
            '--pids-limit',
            '64',
            '--memory',
            '128m',
            '--tmpfs',
            '/tmp:rw,size=8m',
            '--tmpfs',
            '/config:rw,size=8m',
            '--tmpfs',
            '/data:rw,size=64m',
        ]
        config = self.installation.root / self.model['config_directory']
        data = self.installation.root / self.model['data_directory']
        for source, target in (
            (config, '/etc/caddy'),
            (data, '/validation-data'),
            (path, '/etc/caddy/Caddyfile'),
        ):
            if ',' in str(source):
                raise Error('Caddy mount path содержит неподдерживаемый разделитель.')
            arguments += [
                '--mount',
                'type=bind,source=' + str(source) + ',target=' + target + ',readonly',
            ]
        # Provisioning can write storage locks/cache even during validation.
        # Keep the live TLS tree readonly and let it write only to its RAM copy.
        arguments += [
            '--entrypoint',
            'sh',
            image,
            '-c',
            'cp -R /validation-data/. /data/ && exec caddy validate '
            '--config /etc/caddy/Caddyfile --adapter caddyfile',
        ]
        return self.installation.run(arguments)

    def reload(self):
        if self.model is None:
            return self.installation.run(['systemctl', 'reload', 'caddy'])
        name = self.model['container']
        current = self.installation.container(name)
        if (
            not current.get('running')
            or current.get('image_id') != self.installation.model['images'][name]['id']
        ):
            raise Error('Принятый Caddy container не работает; reload отклонён.')
        return self.installation.run(
            [
                'docker',
                'exec',
                name,
                'caddy',
                'reload',
                '--config',
                '/etc/caddy/Caddyfile',
                '--adapter',
                'caddyfile',
            ]
        )


def accepted_mount(installation, name, volume):
    """Only the published site and the instance's public API socket are external."""
    proxy = installation.model.get('proxy', {})
    if (
        proxy.get('format') != 1
        or name != proxy.get('container')
        or volume.get('read_only') is not True
    ):
        return False
    source = Path(volume.get('source', ''))
    if volume.get('target') != str(source) or source.resolve() != source:
        return False
    accepted = {'container': name, 'source': str(source), 'target': str(source)}
    if accepted not in installation.model.get('external_runtime_mounts', []):
        return False
    site = installation.model.get('features', {}).get('site_directory')
    suffix = hashlib.sha256(installation.state['domain'].encode()).hexdigest()[:16]
    public_socket = Path('/run/misbot-federation-' + suffix)
    if source == public_socket:
        info = source.lstat()
        mode = stat.S_IMODE(info.st_mode)
        return (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and (mode == 0o755 or (mode == 0o750 and info.st_gid == 10001))
        )
    if str(source) == site and source.is_relative_to(Path('/srv')) and source != Path('/srv'):
        info = source.lstat()
        return stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
    return False


def validate_service(installation, name, entry):
    proxy = installation.model.get('proxy')
    if proxy is None or name != proxy.get('container'):
        return
    Proxy(installation)
    expected = {
        '/etc/caddy': installation.root / proxy['config_directory'],
        '/data': installation.root / proxy['data_directory'],
        '/config': installation.root / proxy['state_directory'],
    }
    observed = {}
    for volume in entry.get('volumes', []):
        target = volume.get('target')
        if target in observed:
            raise Error('Повторный Caddy mount.')
        observed[target] = volume
        source = Path(volume.get('source', ''))
        if target in expected:
            if source != expected[target] or (
                target == '/etc/caddy' and volume.get('read_only') is not True
            ):
                raise Error('Caddy config/data mounts отличаются от принятого runtime.')
        elif not accepted_mount(installation, name, volume):
            raise Error('Caddy получает доступ к лишнему пути установки.')
    if not set(expected).issubset(observed):
        raise Error('Caddy config/data mounts отсутствуют.')
    if (
        entry.get('privileged')
        or entry.get('pid') == 'host'
        or entry.get('ipc') == 'host'
        or entry.get('devices')
        or entry.get('secrets')
        or entry.get('configs')
        or entry.get('volumes_from')
        or entry.get('use_api_socket')
        or set(entry.get('cap_add', [])) - {'NET_BIND_SERVICE'}
        or entry.get('read_only') is not True
        or set(entry.get('cap_drop', [])) != {'ALL'}
        or set(entry.get('security_opt', [])) != {'no-new-privileges:true'}
        or entry.get('user', '0:0') not in ('0', '0:0', '0:10001')
    ):
        raise Error('Неизвестные права Caddy runtime.')
