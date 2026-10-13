"""Prepare the small, fixed set of packages needed before the installation wizard."""

import importlib
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import sys

import startup_process


def missing_packages():
    packages = [
        package
        for tool, package in (('dig', 'dnsutils'), ('ip', 'iproute2'))
        if not shutil.which(tool)
    ]
    if importlib.util.find_spec('yaml') is None:
        packages.append('python3-yaml')
    return packages


def ensure(ui):
    packages = missing_packages()
    if not packages:
        return
    distribution = platform.freedesktop_os_release()
    supported = {'ubuntu': {'24.04', '26.04'}, 'debian': {'12', '13'}}
    if distribution.get('VERSION_ID') not in supported.get(distribution.get('ID'), set()):
        raise RuntimeError('Подготовка пакетов поддерживает Ubuntu 24.04/26.04 и Debian 12/13.')
    if os.geteuid() != 0 or Path(sys.executable).resolve() != Path('/usr/bin/python3').resolve():
        raise RuntimeError('Подготовка требует системного Python и прав администратора.')
    ui.view.message('Для проверки системы нужны пакеты: ' + ', '.join(packages) + '.')
    if not ui.confirm('Установить эти пакеты из репозиториев системы?'):
        from terminal_ui import Back

        raise Back()
    options = [
        '-o',
        'Acquire::http::Timeout=30',
        '-o',
        'Acquire::https::Timeout=30',
        '-o',
        'DPkg::Lock::Timeout=60',
    ]
    try:
        with ui.view.busy('Подготавливаем пакеты системы') as progress:
            for arguments, timeout in (
                (['update', '-qq'], 600),
                (['install', '-y', '--no-install-recommends', *packages], 900),
            ):
                result = startup_process.run(
                    [
                        '/usr/bin/env',
                        'DEBIAN_FRONTEND=noninteractive',
                        'NEEDRESTART_MODE=l',
                        '/usr/bin/apt-get',
                        *options,
                        *arguments,
                    ],
                    timeout=timeout,
                    emit=lambda *_args, **_values: progress.log('Подготовка пакетов продолжается…'),
                )
                if result.returncode:
                    raise RuntimeError('package preparation failed')
        importlib.invalidate_caches()
        if missing_packages():
            raise RuntimeError('packages still unavailable')
    except (OSError, RuntimeError, ImportError):
        raise RuntimeError(
            'Пакеты пока не готовы. Проверьте доступ к репозиториям системы и повторите шаг; '
            'введённые настройки сохранены.'
        ) from None
