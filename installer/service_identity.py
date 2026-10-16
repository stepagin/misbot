"""Preserved portable service identities and opaque installation secrets."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import json
import re
import subprocess

import yaml


DEFAULTS = {
    'format': 1,
    'livekit_key': 'matrix-livekit',
    'mas_signing_key': {},
    'synapse_form_secret_enabled': True,
    'synapse_registration_shared_secret_enabled': False,
}
SECRETS = (
    'db_synapse',
    'db_mas',
    'db_admin',
    'mas_shared',
    'rtc_as_token',
    'rtc_hs_token',
    'livekit_secret',
    'synapse_macaroon',
    'synapse_form',
    'mas_encryption',
)


def secret_value(path, name):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Saved server secret is missing or unsafe; restore it from backup')
    try:
        value = path.read_text(encoding='utf-8')
    except (UnicodeError, OSError):
        raise RuntimeError('Invalid saved server secret; restore it from backup') from None
    # Generated files end with one newline. Spaces belong to an imported value.
    if value.endswith('\n'):
        value = value[:-1]
    if not 1 <= len(value) <= 4096 or not value.isprintable():
        raise RuntimeError('Invalid saved server secret; restore it from backup')
    if name in ('livekit_secret', 'rtc_as_token', 'rtc_hs_token') and value != value.strip():
        raise RuntimeError(
            'RTC credentials cannot retain boundary whitespace with the pinned JWT service'
        )
    if name == 'mas_encryption' and not re.fullmatch(r'[a-fA-F0-9]{64}', value):
        raise RuntimeError('MAS encryption key must retain its original 32-byte hexadecimal value')
    return value


def secret_names(identity, mas_mode):
    names = list(SECRETS)
    if mas_mode == 'external':
        names = [name for name in names if name not in ('db_mas', 'mas_shared', 'mas_encryption')]
    if not identity['synapse_form_secret_enabled']:
        names.remove('synapse_form')
    if identity['synapse_registration_shared_secret_enabled']:
        names.append('synapse_registration_shared_secret')
    return names


def load(root, metadata, *, preview=False):
    raw = metadata.get('service_identity', {})
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise RuntimeError('Unsupported saved service identity')
    identity = dict(DEFAULTS, **raw)
    if type(identity['format']) is not int or identity['format'] != 1:
        raise RuntimeError('Unsupported saved service identity format')
    if not isinstance(identity['livekit_key'], str) or not re.fullmatch(
        r'[A-Za-z0-9_-]{1,128}', identity['livekit_key']
    ):
        raise RuntimeError('Invalid saved LiveKit key name')
    for name in ('synapse_form_secret_enabled', 'synapse_registration_shared_secret_enabled'):
        if type(identity[name]) is not bool:
            raise RuntimeError('Invalid saved Synapse secret policy')
    signing = identity['mas_signing_key']
    if not isinstance(signing, dict) or set(signing) - {'kid', 'algorithm'}:
        raise RuntimeError('Unsupported saved MAS signing key metadata')
    if 'kid' in signing and (
        not isinstance(signing['kid'], str)
        or not 1 <= len(signing['kid']) <= 128
        or not signing['kid'].isprintable()
    ):
        raise RuntimeError('Invalid saved MAS signing key ID')
    if 'algorithm' in signing and signing['algorithm'] not in ('RS256', 'RS384', 'RS512'):
        raise RuntimeError('Only a saved RSA MAS signing key is supported')
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    previous = metadata.get('service_identity_sha256')
    if previous is not None and previous != fingerprint:
        raise RuntimeError('Service identity is immutable; restore its original saved metadata')
    # Older installations have no identity fingerprint: compare the identity
    # fields in their rendered configuration before accepting the first snapshot.
    if previous is None:
        paths = {
            'livekit': root / 'config/livekit.yaml',
            'mas': root / 'config/mas.yaml',
            'synapse': root / 'config/homeserver.yaml',
        }
        try:
            if paths['livekit'].is_file():
                saved = yaml.safe_load(paths['livekit'].read_text())
                if set(saved.get('keys', {})) != {identity['livekit_key']}:
                    raise ValueError()
            if paths['mas'].is_file():
                keys = yaml.safe_load(paths['mas'].read_text()).get('secrets', {}).get('keys', [])
                if (
                    len(keys) != 1
                    or {k: keys[0][k] for k in ('kid', 'algorithm') if k in keys[0]} != signing
                ):
                    raise ValueError()
            if paths['synapse'].is_file():
                saved = yaml.safe_load(paths['synapse'].read_text())
                if (saved.get('form_secret') is not None) != identity[
                    'synapse_form_secret_enabled'
                ]:
                    raise ValueError()
                if (
                    bool(saved.get('registration_shared_secret'))
                    != identity['synapse_registration_shared_secret_enabled']
                ):
                    raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            raise RuntimeError(
                'Saved service identity differs from its runtime configuration'
            ) from None
    if not preview:
        for name in secret_names(identity, metadata.get('mas_mode', 'local')):
            path = root / 'secrets' / name
            if metadata.get('seeded') or path.exists() or path.is_symlink():
                secret_value(path, name)
        if metadata.get('seeded') and metadata.get('mas_mode', 'local') == 'local':
            path = root / 'config/mas-signing.pem'
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Saved MAS signing key is missing; restore it from backup')
            if signing:
                try:
                    checked = subprocess.run(
                        [
                            'openssl',
                            'rsa',
                            '-check',
                            '-noout',
                            '-in',
                            str(path),
                            '-passin',
                            'pass:',
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
                    if checked.returncode:
                        raise ValueError()
                except (OSError, ValueError, subprocess.SubprocessError):
                    raise RuntimeError(
                        'Saved MAS signing key must be an unencrypted RSA private key'
                    ) from None
    return identity, fingerprint


@dataclass(frozen=True)
class AdministratorIdentity:
    username: str
    matrix_id: str
    mas_user_id: str | None
    password_managed: bool


def administrator(root: Path, domain: str | None = None) -> AdministratorIdentity:
    """Read the initial administrator, separately from explicitly added admins."""
    import configuration
    import mas_admin

    root = Path(root)
    metadata_path = root / 'installation.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    if domain is None:
        domain = metadata.get('domain', '')
    path = root / 'secrets/admin-access.json'
    if not path.exists() and not path.is_symlink():
        if not metadata.get('preview', False) and (
            metadata.get('administrator_identity') is not None or metadata.get('seeded')
        ):
            raise RuntimeError(
                'Saved initial administrator credentials are missing; restore them from backup'
            )
        return AdministratorIdentity('matrix-admin', '@matrix-admin:' + domain, None, True)
    saved = configuration.read_private_json(path)
    username = mas_admin.administrator_username(saved, domain=domain or None)
    identity = AdministratorIdentity(
        username,
        saved.get('matrix_id', '@' + username + ':' + domain),
        saved.get('mas_user_id'),
        saved.get('password_managed', True),
    )
    previous = metadata.get('administrator_identity')
    if previous is not None and (
        not isinstance(previous, dict)
        or previous.get('username') != identity.username
        or previous.get('matrix_id') != identity.matrix_id
        or (
            previous.get('mas_user_id') is not None
            and previous['mas_user_id'] != identity.mas_user_id
        )
    ):
        raise RuntimeError('Initial administrator identity changed; restore its saved credentials')
    return identity


def validate_installation(root, metadata):
    """Read-only admission for updates and unpacked restores, before publication."""
    import referral

    root = Path(root)
    load(root, metadata, preview=metadata.get('preview', False))
    administrator(root, metadata.get('domain'))
    path = root / 'secrets/referral-client.json'
    if path.is_symlink():
        raise RuntimeError('Unsafe saved referral service credentials')
    if path.exists():
        referral.validate_client(
            json.loads(path.read_text()), external=metadata.get('mas_mode') == 'external'
        )
    elif metadata.get('referral_seeded'):
        raise RuntimeError('Saved referral service credential is missing; restore from backup')
