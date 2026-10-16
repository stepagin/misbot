"""Make Compose adopt changed configuration without hashing mutable application data."""

import hashlib
from pathlib import Path

import state_store
import yaml

LABEL = 'org.misbot.configuration.sha256'
# The controller mounts a directory containing both configuration and live data.
# Include only its startup configuration, never sessions, queues, sockets or databases.
DIRECTORY_INPUTS = {
    'referral-control': (
        'data/referral-control/bot-config.json',
        'data/referral-control/bot-policy.json',
    ),
}


def fingerprint(root, compose, role, *, preview=False):
    root = Path(root)
    service = compose['services'][role]
    paths = set(DIRECTORY_INPUTS.get(role, ()))
    for volume in service.get('volumes', []):
        source = volume.split(':', 1)[0] if isinstance(volume, str) else volume.get('source', '')
        relative = source.removeprefix('./')
        if relative.startswith(('config/', 'public/', 'secrets/')):
            paths.add(relative)
    for secret in service.get('secrets', []):
        name = secret if isinstance(secret, str) else secret['source']
        source = compose.get('secrets', {}).get(name, {}).get('file')
        if source:
            paths.add(source.removeprefix('./'))
    digest = hashlib.sha256()
    included = False
    for relative in sorted(paths):
        path = root / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink():
            raise RuntimeError('Unsafe runtime configuration input')
        # Directory mounts expose atomically replaced children directly. Their
        # mutable contents must not trigger restarts (public assets, TLS, data).
        if path.is_dir():
            continue
        if not path.is_file():
            if not preview:
                raise RuntimeError('Runtime configuration input missing: ' + relative)
            content = b''
        else:
            content = path.read_bytes()
        included = True
        digest.update(relative.encode() + b'\0' + hashlib.sha256(content).digest())
    return digest.hexdigest() if included else None


def apply(root, compose, roles=None, *, preview=False):
    """Set stable labels from current bytes; do not expose credentials in Compose."""
    changed = False
    for role in compose['services'] if roles is None else roles:
        value = fingerprint(root, compose, role, preview=preview)
        service = compose['services'][role]
        if value is not None and service.get('labels', {}).get(LABEL) != value:
            service.setdefault('labels', {})[LABEL] = value
            changed = True
    return changed


def refresh(root, roles):
    """Refresh post-provisioning inputs before starting their consumers."""
    path = Path(root) / 'compose.yaml'
    compose = yaml.safe_load(path.read_text())
    if apply(root, compose, roles):
        state_store.write_text(path, yaml.safe_dump(compose, sort_keys=False))
