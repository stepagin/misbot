"""Lazy adapter to the allowlisted backend shared by administrator interfaces."""

import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys

from common import BotError

_BACKENDS = {}


def _trust_runtime(root):
    if not root.is_absolute() or root.resolve() != root or root == Path('/'):
        raise RuntimeError('Unsafe installation directory')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError('Unsafe installation ownership')
    # Validate executable sources before importing the installation's trust checker.
    for path in [root / 'command_wrapper.py', root / 'server_management.py', *root.glob('*.py')]:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise RuntimeError('Unsafe management runtime')


def load_backend(config=None):
    config = config or {}
    configured = Path(config['stack_root']) if config.get('stack_root') else None
    here = Path(__file__).resolve()
    candidates = []
    if configured is not None and (configured / 'server_management.py').exists():
        _trust_runtime(configured)
        candidates.append(configured)
    candidates += [here.parents[2] / 'installer', here.parents[1], here.parents[2]]
    folder = next(
        (value for value in candidates if (value / 'server_management.py').is_file()), None
    )
    if folder is None:
        raise RuntimeError('The management runtime is not available')
    source = folder / 'server_management.py'
    key = str(source.resolve())
    if key not in _BACKENDS:
        existing = sys.modules.get('server_management')
        if existing is not None and Path(existing.__file__).resolve() == source.resolve():
            module = existing
        else:
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            name = '_misbot_server_management_' + hashlib.sha256(key.encode()).hexdigest()[:16]
            spec = importlib.util.spec_from_file_location(name, source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        _BACKENDS[key] = module
    module = _BACKENDS[key]
    if configured is not None and folder == configured:
        module.command_wrapper.trusted_installation(configured)
    return module


def __getattr__(name):
    # Compatibility for source callers; optional management does not affect bridge startup.
    if name in ('COMMANDS', 'WRITES', 'server_management'):
        module = load_backend()
        return module if name == 'server_management' else getattr(module, name)
    raise AttributeError(name)


def run_control(config, operation, *, period='day'):
    try:
        backend = load_backend(config)
        return backend.run_control(config, operation, period=period, runner=subprocess.run)
    except (RuntimeError, OSError, ValueError):
        raise BotError(
            'Управляющая операция недоступна. Проверьте режим установки, разрешённые действия и состояние через SSH.'
        ) from None
