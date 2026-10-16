"""Safe static homepage and narrow, checkpointed proxy-only administration."""

import argparse
import html
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import stat
import sys
import time
import urllib.error
import urllib.request

import configuration as config
import configuration_schema as schema
import state_store

KEYS = ('HOMEPAGE_MODE', 'HOMEPAGE_URL', 'SITE_NAME', 'SITE_DESCRIPTION', 'SITE_SUPPORT_URL')
BEGIN = '    # misbot homepage begin\n'
END = '    # misbot homepage end\n'


def links(model):
    # Element's own login/register routes start native OAuth and retain its callback.
    endpoints = schema.public_endpoints(model)
    web = 'https://' + endpoints['web'] + '/'
    return {
        'client': web,
        'login': web + '#/login',
        'register': web + '#/register',
        'homeserver': 'https://' + endpoints['matrix'],
    }


def render(model, public_links=None, template=None, source=None):
    source = Path(source) if source is not None else Path(__file__).parent
    model = schema.validate(model, preview=True)
    urls = public_links or links(model)
    escape = lambda value: html.escape(value, quote=True)
    if public_links is not None:
        if set(public_links) != set(links(model)):
            raise RuntimeError('Публичные ссылки сайта неполны.')
        for value in public_links.values():
            schema.field_value('HOMEPAGE_URL', value)
    registration = ''
    if model['OPEN_REGISTRATION'] == 'true':
        registration = (
            '<a class="button secondary" href="'
            + escape(urls['register'])
            + '">Зарегистрироваться</a>'
        )
    note = 'Регистрация закрыта. Обратитесь к владельцу сервера.'
    if model['OPEN_REGISTRATION'] == 'true':
        note = (
            'Для регистрации нужен код приглашения. Получите его у участника сервера и введите в поле регистрационного кода при создании аккаунта.'
            if model['REGISTRATION_TOKEN_REQUIRED'] == 'true'
            else 'Создайте учётную запись через кнопку «Зарегистрироваться».'
        )
    support = (
        ('<a href="' + escape(model['SITE_SUPPORT_URL']) + '">Связаться с поддержкой</a>')
        if model['SITE_SUPPORT_URL']
        else ''
    )
    if template is None:
        template = (source / 'site/index.html').read_text(encoding='utf-8')
    values = {
        'NAME': escape(model['SITE_NAME'] or model['DOMAIN']),
        'DESCRIPTION': escape(model['SITE_DESCRIPTION']),
        'LOGIN': escape(urls['login']),
        'REGISTER': registration,
        'CLIENT': escape(urls['client']),
        'HOMESERVER': escape(model['DOMAIN']),
        'NOTE': escape(note),
        'SUPPORT': support,
    }
    if '@@SETUPURL@@' in template:
        url = json.loads((source / 'release.json').read_text())['homepage_setup_url']
        values['SETUPURL'] = escape(schema.field_value('HOMEPAGE_URL', url))
        if not url:
            raise RuntimeError('Не задана ссылка подключения своего сервера в release.json.')
    return re.sub(r'@@([A-Z]+)@@', lambda match: values[match[1]], template)


def redirect_target(model):
    target = (
        links(model)['client'] if model['HOMEPAGE_MODE'] == 'element' else model['HOMEPAGE_URL']
    )
    # Browsers inherit the original fragment when Location omits one. Explicit
    # empty fragment prevents forwarding OAuth tokens hidden after '#'.
    return target if '#' in target else target + '#'


def route(model):
    model = schema.validate(model, preview=True)
    headers = (
        '        header Cache-Control "no-store"\n        header Referrer-Policy "no-referrer"\n'
    )
    if model['HOMEPAGE_MODE'] == 'landing':
        action = (
            "        header Content-Security-Policy \"default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\"\n"
            '        header X-Content-Type-Options nosniff\n'
            '        root * /srv/discovery/site\n        file_server\n'
        )
    else:
        target = redirect_target(model)
        action = '        redir ' + json.dumps(target, ensure_ascii=True) + ' 302\n'
    # Only root is the homepage. Never forward source URI/query to another site.
    fallback = (
        '        reverse_proxy mas:8080\n'
        if model.get('MAS_MODE', 'local') == 'local'
        and schema.mas_host(model) == schema.public_endpoints(model)['site']
        else '        respond "Not found" 404\n'
    )
    return (
        BEGIN
        + '    handle / {\n'
        + headers
        + action
        + '    }\n    handle /site.css {\n        root * /srv/discovery/site\n        file_server\n    }\n    handle {\n'
        + fallback
        + '    }\n'
        + END
    )


def replace_route(text, model):
    if text.count(BEGIN) == 1 and text.count(END) == 1:
        start = text.index(BEGIN)
        end = text.index(END) + len(END)
        return text[:start] + route(model) + text[end:]
    legacy = (
        '    handle {\n        redir ' + links(model)['client'].rstrip('/') + '{uri} 302\n    }\n'
    )
    if text.count(legacy) != 1:
        raise RuntimeError(
            'Неизвестная конфигурация прокси; сохраните её и выполните misbot homepage recover'
        )
    return text.replace(legacy, route(model), 1)


def write_assets(root, model):
    folder = Path(root) / 'public/site'
    if folder.resolve() != folder.absolute():
        raise RuntimeError('Небезопасный каталог сайта')
    folder.mkdir(mode=0o755, parents=True, exist_ok=True)
    folder.chmod(0o755)
    state_store.write_text(folder / 'index.html', render(model), 0o644)
    state_store.write_text(
        folder / 'site.css', (Path(__file__).parent / 'site/site.css').read_text(), 0o644
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class ServiceUnavailable(RuntimeError):
    def __init__(self, status):
        super().__init__('Служба временно недоступна; ожидаем готовность сервера.')
        self.status = status


def probe(model, api_enabled=True):
    """Verify actual public origin with normal TLS; never follow the external target."""
    opener = urllib.request.build_opener(NoRedirect)

    def get(url):
        try:
            with opener.open(url, timeout=10) as response:
                return response.status, response.headers, response.read(65537)
        except urllib.error.HTTPError as response:
            if response.code in (502, 503, 504) and not (
                not api_enabled and url.endswith('/.well-known/misbot')
            ):
                response.close()
                raise ServiceUnavailable(response.code) from None
            return response.code, response.headers, response.read(65537)

    endpoints = schema.public_endpoints(model)
    origin = 'https://' + endpoints['site']
    status, headers, body = get(origin + '/?misbot_route_check=1')
    if model['HOMEPAGE_MODE'] == 'landing':
        if status != 200 or b'name="misbot-homepage"' not in body:
            raise RuntimeError('Лендинг не подтверждён')
    elif status != 302 or headers.get('Location') != (
        links(model)['client'] if model.get('_legacy_homepage_redirect') else redirect_target(model)
    ):
        raise RuntimeError('Переадресация не подтверждена')
    for path in ('/.well-known/matrix/client', '/.well-known/matrix/server', '/.well-known/misbot'):
        code, _, payload = get(origin + path)
        if path.endswith('/misbot') and not api_enabled:
            if code != 503 or json.loads(payload) != {'code': 'api_disabled'}:
                raise RuntimeError('Отключённый Misbot API не подтверждён')
            continue
        if code != 200 or not isinstance(json.loads(payload), dict):
            raise RuntimeError('Служебный discovery не подтверждён')
        document = json.loads(payload)
        if (
            path.endswith('/client')
            and document.get('m.homeserver', {}).get('base_url') != 'https://' + endpoints['matrix']
        ):
            raise RuntimeError('Matrix discovery указывает другой сервер')
        if path.endswith('/server') and document.get('m.server') != endpoints['matrix'] + ':443':
            raise RuntimeError('Federation discovery изменён')
        if path.endswith('/misbot') and (
            document.get('server_name') != model['DOMAIN'] or document.get('protocol') != 1
        ):
            raise RuntimeError('Misbot discovery изменён')
    code, _, payload = get('https://' + endpoints['matrix'] + '/_matrix/client/versions')
    if code != 200 or 'versions' not in json.loads(payload):
        raise RuntimeError('Matrix client API не подтверждён')
    return {
        'local_routes': 'verified',
        'external_target': 'not_checked',
        'browser_authorization': 'not_checked',
    }


class Backend:
    def __init__(self, root, validator=None, activate=None, verify=None, attempts=15):
        self.root = Path(root)
        self.validator = validator or self.validate_proxy
        self.activate = activate or self.restart_proxy
        self.verify = verify or self.probe
        self.attempts = attempts

    def probe(self, model):
        import federation

        return probe(model, api_enabled=federation.Backend(self.root).api_enabled())

    def ready(self, recovery=False):
        import lifecycle

        state_store.assert_update_allowed(
            self.root, allow_homepage=recovery, allow_configuration=recovery
        )
        metadata = config.read_private_json(self.root / 'installation.json')
        if any(
            metadata.get(k)
            for k in (
                'restore_pending',
                'migration_frozen',
                'media_transition_pending',
                'credential_transition_pending',
            )
        ):
            raise RuntimeError('Сначала завершите pending операцию; misbot status')
        lifecycle.configure(self.root, 'homepage')
        lifecycle.ownership_check()

    def initial_launch_incomplete(self):
        """Only the native bootstrap before public services permits local rollback."""
        metadata = config.read_private_json(self.root / 'installation.json')
        if (
            metadata.get('backend') == 'existing-compose'
            or metadata.get('applied_release_version')
            or metadata.get('databases_initialized') is not True
            or metadata.get('federation_seeded') is not True
            or metadata.get('welcome_crypto_initialized')
        ):
            return False
        import lifecycle

        return not any(row['Service'] == 'tls-router' for row in lifecycle.service_rows())

    def paths(self):
        for path in (
            self.root / 'config',
            self.root / 'public',
            self.root / 'config/Caddyfile',
            self.root / 'public/site',
            self.root / 'public/site/index.html',
            self.root / 'public/site/site.css',
        ):
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            if (
                path.resolve() != path.absolute()
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o022
                or (
                    not stat.S_ISDIR(info.st_mode)
                    and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
                )
            ):
                raise RuntimeError(
                    'Небезопасный файл или каталог сайта; ссылки и запись другими пользователями запрещены'
                )

    def current(self):
        model = config.load(self.root, True)
        return {key: model[key] for key in KEYS} | {'links': links(model)}

    def proposed(self, changes):
        if set(changes) - set(KEYS):
            raise RuntimeError('Разрешены только поля сайта; misbot homepage help')
        model = config.load(self.root, True)
        model.update(changes)
        return schema.validate(model)

    def validate_proxy(self, path):
        import lifecycle

        lifecycle.run(
            [
                'docker',
                'run',
                '--rm',
                '--network',
                'none',
                '--memory',
                '144m',
                '--cpus',
                '0.5',
                '--mount',
                'type=bind,source=' + str(path) + ',target=/etc/caddy/Caddyfile,readonly',
                lifecycle.image('caddy'),
                'caddy',
                'validate',
                '--config',
                '/etc/caddy/Caddyfile',
                '--adapter',
                'caddyfile',
            ]
        )

    def restart_proxy(self):
        import lifecycle

        # Atomic file writes change its bind-mounted inode; recreate only Caddy.
        lifecycle.dc('up', '-d', '--no-deps', '--force-recreate', 'caddy')

    def apply(self, changes):
        self.ready()
        if self.initial_launch_incomplete():
            raise RuntimeError(
                'Первоначальный запуск ещё не завершён. Сначала продолжите установку '
                'через «Состояние и работа сервера» → «Запустить сервер»; '
                'настройки сайта не изменены.'
            )

        self.paths()
        model = self.proposed(changes)
        before = config.load(self.root, True)
        if all(model[key] == before[key] for key in KEYS):
            return {'status': 'unchanged'}
        stage = config.directory(self.root / 'stages/homepage' / secrets.token_hex(16))
        config.snapshot(self.root, stage / 'previous')
        state_store.write_text(
            stage / 'previous-caddy', (self.root / 'config/Caddyfile').read_text()
        )
        saved = self.root / 'public/site'
        if saved.exists():
            for name in ('index.html', 'site.css'):
                if (saved / name).exists():
                    state_store.write_text(stage / ('previous-' + name), (saved / name).read_text())
        candidate = replace_route((self.root / 'config/Caddyfile').read_text(), model)
        state_store.write_text(stage / 'next-caddy', candidate)
        self.validator(stage / 'next-caddy')
        state_store.write_json(stage / 'next-site', {key: model[key] for key in KEYS})
        state_store.write_json(
            self.root / 'homepage-transition.json',
            {'format': 1, 'stage': str(stage.relative_to(self.root)), 'phase': 'prepared'},
        )
        try:
            return self.recover()
        except state_store.OperationInterrupted:
            raise
        except (Exception, KeyboardInterrupt):
            try:
                self.recover(rollback=True)
            except (Exception, KeyboardInterrupt):
                raise RuntimeError(
                    'Применение и rollback не подтверждены; сохраните checkpoint: sudo misbot --directory '
                    + shlex.quote(str(self.root))
                    + ' homepage rollback'
                ) from None
            raise RuntimeError(
                'Прежняя главная страница восстановлена. Исправьте причину; повтор: sudo misbot homepage apply --help'
            ) from None

    def recover(self, rollback=False):
        self.paths()
        path = self.root / 'homepage-transition.json'
        if not path.exists():
            return {'status': 'unchanged'}
        journal = config.read_private_json(path)
        if journal.get('format') != 1 or not re.fullmatch(
            r'stages/homepage/[0-9a-f]{32}', journal.get('stage', '')
        ):
            raise RuntimeError('Небезопасный checkpoint; сохраните его приватно')
        stage = self.root / journal['stage']
        incomplete = self.initial_launch_incomplete()
        if incomplete and not rollback:
            raise RuntimeError(
                'Первоначальный запуск не завершён. В разделе «Сайт после прерывания» '
                'выберите «Вернуть прежний сайт», затем «Запустить сервер». '
                'Checkpoint и данные сохранены.'
            )
        if (self.root / 'configuration-transition.json').exists():
            config.recover(self.root, rollback=rollback)
        if rollback:
            config.restore_snapshot(self.root, stage / 'previous')
            model = config.load(self.root, True)
            state_store.write_text(
                self.root / 'config/Caddyfile', config.read_private(stage / 'previous-caddy')
            )
            for name in ('index.html', 'site.css'):
                if (stage / ('previous-' + name)).exists():
                    state_store.write_text(
                        self.root / 'public/site' / name,
                        config.read_private(stage / ('previous-' + name)),
                        0o644,
                    )
        else:
            model = self.proposed(config.read_private_json(stage / 'next-site'))
            config.write_model(self.root, model)
            write_assets(self.root, model)
            state_store.write_text(
                self.root / 'config/Caddyfile', config.read_private(stage / 'next-caddy')
            )
        self.validator(self.root / 'config/Caddyfile')
        if incomplete:
            # Restore the saved files, without starting services or claiming HTTPS works.
            path.unlink()
            return {
                'stage': 'homepage',
                'status': 'rolled_back',
                'restart': 'not started',
                'public_checks': 'deferred',
                'next_action': 'up',
            }
        self.activate()
        # Recovery is complete only after real routes are verified.
        observed = dict(model)
        legacy_redirect = 'redir ' + links(model)['client'].rstrip('/') + '{uri} 302'
        if rollback and legacy_redirect in config.read_private(stage / 'previous-caddy'):
            observed['HOMEPAGE_MODE'] = 'element'
            observed['_legacy_homepage_redirect'] = True
        for attempt in range(self.attempts):
            try:
                self.verify(observed)
                break
            except (RuntimeError, OSError, ValueError):
                if attempt == self.attempts - 1:
                    raise
                time.sleep(2)
        path.unlink()
        return {
            'stage': 'homepage',
            'status': 'rolled_back' if rollback else 'applied',
            'restart': 'caddy only',
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('action', choices=('show', 'check', 'apply', 'recover', 'rollback'))
    for arg in ('mode', 'url', 'name', 'description', 'support-url'):
        parser.add_argument('--' + arg)
    args = parser.parse_args()
    changes = {
        key: value
        for key, value in zip(
            KEYS, (args.mode, args.url, args.name, args.description, args.support_url)
        )
        if value is not None
    }
    try:
        state_store.require_root()
        __import__('command_wrapper').trusted_installation(args.directory)
        with state_store.operation_guard(args.directory):
            backend = Backend(args.directory)
            if args.action == 'show':
                result = backend.current()
            elif args.action == 'check':
                backend.ready()
                backend.paths()
                model = backend.proposed(changes)
                stage = config.directory(args.directory / 'stages/homepage' / secrets.token_hex(16))
                state_store.write_text(
                    stage / 'candidate',
                    replace_route((args.directory / 'config/Caddyfile').read_text(), model),
                )
                backend.validator(stage / 'candidate')
                result = {
                    'status': 'valid',
                    'external_target': 'not_checked',
                    'changes': {k: model[k] for k in changes},
                    'next_action': 'sudo misbot homepage apply --help',
                }
            elif args.action == 'apply':
                result = backend.apply(changes)
            else:
                backend.ready(True)
                result = backend.recover(args.action == 'rollback')
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (KeyboardInterrupt, state_store.OperationInterrupted):
        print('Прервано. Checkpoint сохранён; sudo misbot homepage recover', file=sys.stderr)
        return 130
    except schema.InvalidConfiguration as error:
        print(json.dumps(error.result(), ensure_ascii=False))
        return 2
    except (RuntimeError, OSError, ValueError, KeyError):
        print(
            json.dumps(
                {
                    'stage': 'homepage',
                    'status': 'failed',
                    'code': 'configuration_or_routes',
                    'explanation': str(sys.exc_info()[1])
                    if type(sys.exc_info()[1]) is RuntimeError
                    else 'Проверка файлов или HTTPS маршрутов не прошла; секреты и данные сохранены.',
                    'next_action': 'sudo misbot homepage show; sudo misbot homepage recover',
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == '__main__':

    def interrupted(signum, frame):
        raise state_store.OperationInterrupted('Interrupted')

    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, interrupted)
    raise SystemExit(main())
