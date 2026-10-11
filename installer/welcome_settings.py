"""Read existing installation settings without changing bot identity or secrets."""

import json
from pathlib import Path
from typing import cast

import configuration
import configuration_schema
from welcome_message import DEFAULT_WELCOME, WelcomeContext


def owner(root: Path) -> str:
    root = Path(root)
    identity = root / 'config/admin-panel-identity.json'
    if identity.exists():
        return cast(str, configuration.read_private_json(identity)['administrator_mxid'])
    config = root / 'config/federation-bot.json'
    if config.exists():
        admins = json.loads(config.read_text()).get('administrator_mxids')
        if admins:
            return cast(str, admins[0])
    domain = cast(str, configuration.read_private_json(root / 'installation.json')['domain'])
    return '@matrix-admin:' + domain


def welcome_context(root: Path, domain: str, template: str = DEFAULT_WELCOME) -> WelcomeContext:
    root = Path(root)
    server_name = domain
    if '{server_name}' in template:
        metadata = configuration.read_private_json(root / 'installation.json')
        if metadata.get('backend') == 'existing-compose':
            site = root / '.misbot/features/site.json'
            values = (
                configuration.read_private_json(site) if site.exists() or site.is_symlink() else {}
            )
            fallback = metadata.get('existing', {}).get('features', {}).get('primary_domain')
            server_name = values.get('SITE_NAME') or fallback or domain
        elif any(
            path.exists() or path.is_symlink()
            for path in (root / 'settings', root / 'settings.env')
        ):
            server_name = configuration.load(root).get('SITE_NAME') or domain
        server_name = configuration_schema.field_value('SITE_NAME', server_name)
    referral_mxid = '@referral-bot:' + domain
    if '{referral_url}' in template:
        config = root / 'config/referral-bot.json'
        if config.exists() or config.is_symlink():
            referral_mxid = json.loads(config.read_text())['user_id']
    administrator_mxid = owner(root) if '{administrator_url}' in template else ''
    return WelcomeContext(domain, server_name, referral_mxid, administrator_mxid)
