"""Local choice of admin bot identity before its ordinary MAS account is created."""

import argparse
import json
import os
from pathlib import Path
import re

import configuration
import federation_runtime
import lifecycle
import state_store


def choose(root, tag, administrator=None):
    root = Path(root)
    metadata = configuration.read_private_json(root / 'installation.json')
    saved = federation_runtime.identity(root, metadata)
    mxid = tag if ':' in tag else '@' + tag.removeprefix('@') + ':' + metadata['domain']
    pattern = r'@[a-z0-9][a-z0-9._=-]{0,63}:' + re.escape(metadata['domain'])
    if not re.fullmatch(pattern, mxid) or (
        administrator and not re.fullmatch(pattern, administrator)
    ):
        raise ValueError('Use a local Matrix tag')
    created = metadata.get('federation_provisioned') or any(
        (root / 'data/referral-control' / name).exists()
        for name in ('federation-session.json', 'managed-federation-user.json')
    )
    if created and mxid != saved['user_id']:
        raise RuntimeError('Тег созданного бота изменить нельзя. Укажите его сохранённый тег.')
    if mxid in (
        '@referral-bot:' + metadata['domain'],
        '@server-guide:' + metadata['domain'],
    ) or mxid in [administrator, *saved['administrator_mxids']]:
        raise ValueError('Bot identity must differ from other bots and the administrator')
    saved['user_id'] = mxid
    if administrator:
        saved['administrator_mxid'] = administrator
        saved['administrator_mxids'] = list(
            dict.fromkeys([administrator, *saved['administrator_mxids']])
        )
    if len(saved['administrator_mxids']) > 20:
        raise ValueError('Доступен максимум 20 администраторов.')
    state_store.write_json(root / 'config/admin-panel-identity.json', saved)
    path = root / 'config/federation-bot.json'
    if path.exists():
        config = json.loads(path.read_text())
        if not created:
            config['user_id'] = mxid
        config['administrator_mxids'] = saved['administrator_mxids']
        state_store.write_json(path, config)
    return saved


def api(root, action, mxid, user_id=None, admin=None):
    client = configuration.read_private_json(root / 'secrets/referral-client.json')
    payload = {'client': client, 'username': mxid[1:].split(':')[0], 'action': action}
    if user_id is not None:
        payload.update(user_id=user_id, admin=admin)
    try:
        raw = lifecycle.dc(
            'exec',
            '-T',
            'synapse',
            'python',
            '-c',
            (root / 'federation_admin_api.py').read_text(),
            stdin=json.dumps(payload),
        )
        result = json.loads(raw)
    except (RuntimeError, OSError, ValueError):
        raise RuntimeError(
            'Аккаунт и права не подтверждены. Проверьте имя, состояние аккаунта и MAS.'
        ) from None
    if (
        not isinstance(result, dict)
        or set(result) != {'username', 'user_id', 'admin'}
        or result['username'] != payload['username']
        or not isinstance(result['user_id'], str)
        or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', result['user_id'])
        or type(result['admin']) is not bool
        or (user_id is not None and (result['user_id'] != user_id or result['admin'] != admin))
    ):
        raise RuntimeError('Действующий аккаунт и его права не подтверждены. Проверьте MAS.')
    return result


def publish(root, saved):
    state_store.write_json(root / 'config/admin-panel-identity.json', saved)
    path = root / 'config/federation-bot.json'
    if path.exists():
        config = json.loads(path.read_text())
        config['administrator_mxids'] = saved['administrator_mxids']
        state_store.write_json(path, config)
        if os.geteuid() == 0:
            os.chown(path, 10003, 10003)
    # An atomic rename changes a Docker single-file bind mount's inode. Recreate
    # only this bot, preserving /data, account, token, device and crypto stores.
    try:
        lifecycle.dc('up', '-d', '--force-recreate', '--no-deps', 'federation-bot')
    except (OSError, RuntimeError):
        raise RuntimeError(
            'Настройки доступа сохранены; перезапуск панели не подтверждён. '
            'Выполните sudo misbot admin access recover'
        ) from None


def access(root, action, mxid=None):
    root = Path(root)
    if action not in ('list', 'add', 'remove', 'recover'):
        raise ValueError('Неизвестное действие с доступом администратора.')
    metadata = configuration.read_private_json(root / 'installation.json')
    if metadata.get('backend') == 'existing-compose':
        raise RuntimeError(
            'Для подключённого стека настройте права через его MAS и штатные средства.'
        )
    saved = federation_runtime.identity(root, metadata)
    if action == 'list':
        return {
            'administrators': saved['administrator_mxids'],
            'primary': saved['administrator_mxid'],
        }
    state_store.assert_update_allowed(root)
    if any(
        metadata.get(name)
        for name in ('migration_frozen', 'restore_pending', 'credential_transition_pending')
    ):
        raise RuntimeError('Сначала завершите восстановление или перенос сервера.')
    pending = root / 'secrets/admin-access-pending.json'
    if action == 'recover':
        if not pending.exists() and not pending.is_symlink():
            return {'status': 'unchanged', 'message': 'Незавершённого изменения доступа нет.'}
        operation = configuration.read_private_json(pending)
        if (
            set(operation) != {'domain', 'mxid', 'user_id', 'admin'}
            or operation['domain'] != metadata['domain']
            or type(operation['admin']) is not bool
            or not isinstance(operation['user_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation['user_id'])
        ):
            raise RuntimeError(
                'Некорректное сохранённое изменение доступа. Сохраните его для проверки.'
            )
        mxid = operation['mxid']
        action = 'add' if operation['admin'] else 'remove'
    elif pending.exists() or pending.is_symlink():
        raise RuntimeError('Завершите предыдущее изменение: sudo misbot admin access recover')
    else:
        operation = None
    if not isinstance(mxid, str) or not re.fullmatch(
        r'@[a-z0-9][a-z0-9._=-]{0,254}:' + re.escape(metadata['domain']), mxid
    ):
        raise ValueError(
            'Введите полный адрес существующего местного аккаунта: @имя:' + metadata['domain']
        )
    protected = {'@matrix-admin:' + metadata['domain'], saved['administrator_mxid']}
    bots = {
        saved['user_id'],
        '@server-guide:' + metadata['domain'],
        '@referral-bot:' + metadata['domain'],
    }
    for name in ('referral-bot.json', 'welcome-bot.json'):
        path = root / 'config' / name
        if path.exists():
            bots.add(json.loads(path.read_text())['user_id'])
    if mxid in bots or (action == 'remove' and mxid in protected):
        raise ValueError('Служебные аккаунты и первоначального администратора изменять нельзя.')
    if (
        action == 'add'
        and len(saved['administrator_mxids']) >= 20
        and mxid not in saved['administrator_mxids']
    ):
        raise ValueError('Доступен максимум 20 администраторов.')
    if action == 'remove' and mxid not in saved['administrator_mxids'] and operation is None:
        return {'status': 'unchanged', 'administrators': saved['administrator_mxids']}
    if operation is None:
        user = api(root, 'inspect', mxid)
        operation = {
            'domain': metadata['domain'],
            'mxid': mxid,
            'user_id': user['user_id'],
            'admin': action == 'add',
        }
        state_store.write_json(pending, operation)
    api(root, 'set-admin', mxid, operation['user_id'], operation['admin'])
    admins = saved['administrator_mxids']
    saved['administrator_mxids'] = (
        list(dict.fromkeys([*admins, mxid]))
        if action == 'add'
        else [item for item in admins if item != mxid]
    )
    publish(root, saved)
    pending.unlink()
    return {'status': 'applied', 'administrators': saved['administrator_mxids'], 'mxid': mxid}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('show', 'set', 'list', 'add', 'remove', 'recover'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--tag')
    parser.add_argument('--mxid')
    parser.add_argument('--administrator')
    args = parser.parse_args()
    state_store.require_root()
    __import__('command_wrapper').trusted_installation(args.directory)
    with state_store.operation_guard(args.directory):
        if args.action == 'set':
            if not args.tag:
                raise ValueError('Specify --tag')
            result = choose(args.directory, args.tag, args.administrator)
        elif args.action in ('list', 'add', 'remove', 'recover'):
            lifecycle.configure(args.directory, 'admin-access-' + args.action)
            lifecycle.ownership_check()
            result = access(args.directory, args.action, args.mxid)
        else:
            result = federation_runtime.identity(
                args.directory,
                configuration.read_private_json(args.directory / 'installation.json'),
            )
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error))
        print(
            'Операция не завершена. Незавершённое изменение доступа: '
            'sudo misbot admin access recover. Сохранённые теги: sudo misbot admin bot show.'
        )
        raise SystemExit(1)
