"""Interactive decommissioning, including init-only installs; keep product sources."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import configuration
import lifecycle
import s3_config
import state_store

LOCAL = {
    'data': ('Базы PostgreSQL, медиа, пользователи, переписки, ключи Synapse, состояния и теги ботов', ('data',)),
    'secrets': ('Все локальные секреты и настройки (settings, config, secrets, пароли, OAuth/S3/Vault)',
                ('settings', 'settings.env', 'config', 'secrets')),
    'archives': ('Локальные копии, журналы и checkpoints, включая содержащие секреты', ('backups', 'stages')),
}


def yes(question, ask=input):
    while True:
        answer = ask(question + ' [да/нет, по умолчанию нет]: ').strip().lower()
        if answer in ('да', 'д', 'yes'):
            return True
        if answer in ('', 'нет', 'н', 'no'):
            return False


def s3_identity(profile):
    return {key: profile[key] for key in ('S3_ENDPOINT_URL', 'S3_REGION', 'S3_BUCKET', 'S3_PREFIX')}


def s3_label(profile):
    return (profile['S3_ENDPOINT_URL'] or 'AWS:' + profile['S3_REGION']) + '/' + profile['S3_BUCKET'] + '/' + profile['S3_PREFIX']


def discover(root, backup_config=None):
    root = Path(root)
    model = configuration.load(root) if (root/'settings').exists() or (root/'settings.env').exists() else {}
    targets = []
    if model.get('MEDIA_MODE') == 's3':
        profile = s3_config.validate({key: model.get(key, '') for key in s3_config.FIELDS})
        targets.append({'kind': 'media', 'profile': profile, 'identity': s3_identity(profile)})
    paths = []
    if backup_config:
        paths.append(Path(backup_config))
    if (root / 'settings/backup.env').exists():
        paths.append(root / 'settings/backup.env')
    schedule = root / 'config/backup-schedule.json'
    if schedule.exists():
        saved = configuration.read_private_json(schedule)
        if saved.get('mode') == 'external':
            paths.append(Path(saved['settings']))
    for path in paths:
        profile = s3_config.read_profile(path, 'backup')
        if any(item['identity'] == s3_identity(profile) for item in targets):
            continue
        targets.append({'kind': 'backup', 'profile': profile, 'identity': s3_identity(profile)})
    return model, targets


def plan(root, targets, model, ask=input, say=print):
    root = Path(root)
    domain = configuration.read_private_json(root / 'installation.json')['domain']
    say('Удаление сервера ' + domain + ' из ' + str(root) + '. Утилита misbot останется. Данные после удаления восстановить нельзя.')
    if not yes('Начать выбор удаляемых данных?', ask):
        return None
    selected = {key: yes(description + ': удалить?', ask) for key, (description, _) in LOCAL.items()}
    remote = []
    for target in targets:
        label = s3_label(target['profile'])
        say('S3 ' + target['kind'] + ': ' + label + '. Удаляются ВСЕ объекты, версии, delete markers и незавершённые uploads только в этом prefix; bucket сохраняется.')
        if yes('Удалить этот S3 prefix?', ask):
            if ask('Для подтверждения введите: УДАЛИТЬ S3 ' + label + '\n') != 'УДАЛИТЬ S3 ' + label:
                raise RuntimeError('Подтверждение S3 не совпало; удаление отменено.')
            remote.append(target['identity'])
    external_db = False
    if model.get('POSTGRES_MODE') == 'external':
        external_db = yes('Удалить во внешней PostgreSQL только installer-owned базы synapse/mas и их роли?', ask)
        if external_db and ask('Введите: УДАЛИТЬ БАЗЫ synapse mas\n') != 'УДАЛИТЬ БАЗЫ synapse mas':
            raise RuntimeError('Подтверждение БД не совпало; удаление отменено.')
    vault = False
    if (root / 'config/vault-state.json').exists():
        vault = yes('Навсегда удалить все версии принадлежащего этому серверу snapshot в Vault KV?', ask)
        if vault and ask('Введите: УДАЛИТЬ VAULT\n') != 'УДАЛИТЬ VAULT':
            raise RuntimeError('Подтверждение Vault не совпало; удаление отменено.')
    say('Секреты, recovery keys и выгрузки, вручную сохранённые вне установки, могут находиться в других местах. Копии у других Matrix-серверов и на устройствах пользователей удаляются их владельцами.')
    say('Итог: контейнеры и служебные units будут удалены. Локально: ' + ', '.join(k + '=' + ('удалить' if v else 'сохранить') for k, v in selected.items())
        + '; S3 prefixes: ' + str(len(remote)) + '; внешние БД: ' + str(external_db) + '; Vault: ' + str(vault))
    if all(selected.values()):
        say('Также удалятся остальные runtime-файлы в выделенном каталоге установки, кроме исходников утилиты. Тег admin-panel и настройки уведомлений будут забыты.')
    if not yes('Этот точный список верен?', ask):
        return None
    if ask('Финальное подтверждение: введите УДАЛИТЬ СЕРВЕР ' + domain + '\n') != 'УДАЛИТЬ СЕРВЕР ' + domain:
        raise RuntimeError('Финальное подтверждение не совпало; удаление отменено.')
    return {'format': 1, 'domain': domain, 'directory': str(root), 'local': selected,
            's3': remote, 'external_database': external_db, 'vault': vault, 'completed': []}


def client(profile):
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=profile['S3_ENDPOINT_URL'] or None,
        region_name=profile['S3_REGION'], aws_access_key_id=profile['S3_ACCESS_KEY_ID'],
        aws_secret_access_key=profile['S3_SECRET_ACCESS_KEY'],
        config=Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 3}))


def delete_s3(profile, service=None):
    profile = s3_config.validate(profile)
    service = service or client(profile)
    bucket, prefix = profile['S3_BUCKET'], profile['S3_PREFIX']
    # Never delete the bucket, and never enumerate outside the explicitly selected prefix.
    def remove(objects):
        for obj in objects:
            if not obj['Key'].startswith(prefix):
                raise RuntimeError('Provider returned an object outside the selected prefix')
        if objects:
            result = service.delete_objects(Bucket=bucket, Delete={'Objects': objects, 'Quiet': True})
            if result.get('Errors'):
                raise RuntimeError('S3 refused some deletions (retention/permissions); local credentials preserved.')
    for page in service.get_paginator('list_object_versions').paginate(Bucket=bucket, Prefix=prefix):
        remove([{'Key': obj['Key'], 'VersionId': obj['VersionId']}
                for obj in page.get('Versions', []) + page.get('DeleteMarkers', [])])
    for page in service.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        remove([{'Key': obj['Key']} for obj in page.get('Contents', [])])
    for page in service.get_paginator('list_multipart_uploads').paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Uploads', []):
            if not obj['Key'].startswith(prefix):
                raise RuntimeError('Multipart upload outside selected prefix')
            service.abort_multipart_upload(Bucket=bucket, Key=obj['Key'], UploadId=obj['UploadId'])
    # Readback proves empty scope; suspended/versioned buckets must have no versions left.
    for operation, keys in (('list_object_versions', ('Versions', 'DeleteMarkers')),
                            ('list_objects_v2', ('Contents',)), ('list_multipart_uploads', ('Uploads',))):
        for page in service.get_paginator(operation).paginate(Bucket=bucket, Prefix=prefix):
            if any(page.get(key) for key in keys):
                raise RuntimeError('S3 prefix is not empty; another writer or retention prevented removal')


def delete_external_database(root):
    saved = configuration.read_private_json(Path(root) / 'config/external-postgres-owner.json')
    model = configuration.load(root)
    expected = {'POSTGRES_HOST': model['POSTGRES_HOST'], 'POSTGRES_PORT': model['POSTGRES_PORT'],
                'server_name': configuration.read_private_json(Path(root) / 'installation.json')['domain']}
    if saved.get('identity') != expected or not re.fullmatch(r'[0-9a-f]{32}', saved.get('owner', '')):
        raise RuntimeError('External database ownership is unconfirmed; credentials preserved')
    marker = 'matrix-installer:' + saved['owner']
    query = "SELECT json_build_object('roles',(SELECT coalesce(json_agg(json_build_object('name',rolname,'marker',shobj_description(oid,'pg_authid'))),'[]') FROM pg_roles WHERE rolname IN ('synapse','mas')),'databases',(SELECT coalesce(json_agg(json_build_object('name',datname,'owner',pg_get_userbyid(datdba))),'[]') FROM pg_database WHERE datname IN ('synapse','mas')));"
    observed = json.loads(lifecycle.postgres('psql', '-d', 'postgres', '-X', '-At', '-c', query))
    roles = {row['name']: row for row in observed['roles']}
    if any(row['marker'] != marker for row in roles.values()) or any(row['name'] not in roles or row['owner'] != row['name'] for row in observed['databases']):
        raise RuntimeError('Foreign external database or role detected; nothing deleted')
    for row in observed['databases']:
        lifecycle.postgres('psql', '-d', 'postgres', '-X', '-v', 'ON_ERROR_STOP=1', '-c',
                           'DROP DATABASE ' + row['name'] + ' WITH (FORCE)')
    for name in roles:
        lifecycle.postgres('psql', '-d', 'postgres', '-X', '-v', 'ON_ERROR_STOP=1', '-c', 'DROP ROLE ' + name)


def delete_vault(root):
    import urllib.request
    import vault_cache
    root = Path(root)
    saved = configuration.read_private_json(vault_cache.descriptor(root))
    service = vault_cache.Client(vault_cache.connection(root / 'config/vault-connection.env'))
    remote = service.read()
    if remote is None and saved.get('version'):remote=service.read(saved['version'])
    if remote is None:raise RuntimeError('Vault ownership unconfirmed; local credentials preserved')
    if remote and (remote[1].get('owner') != saved['owner'] or remote[1].get('server_name') != saved['server_name']):
        raise RuntimeError('Foreign Vault snapshot; nothing deleted')
    url = service.values['VAULT_ADDR'].rstrip('/') + '/v1/' + service.values['VAULT_KV_MOUNT'] + '/metadata/' + service.values['VAULT_SECRET_PATH']
    request = urllib.request.Request(url, method='DELETE', headers={'X-Vault-Token': service.token})
    with service.opener.open(request, timeout=15) as response:
        if response.status != 204:
            raise RuntimeError('Vault deletion unconfirmed; local credentials preserved')
    if service.read() is not None:
        raise RuntimeError('Vault snapshot still exists')


def remove_path(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    # Refuse mounted filesystems; never recurse into a separate disk accidentally.
    for folder, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            child = Path(folder) / name
            if not child.is_symlink() and os.path.ismount(child):
                raise RuntimeError('Mounted directory in deletion scope; unmount it explicitly first')
    if os.path.ismount(path):
        raise RuntimeError('Deletion scope is mounted; unmount it first')
    shutil.rmtree(path)


def source_names(root):
    import update
    entries, _ = update.validate_manifest(Path(root))
    if any(path.resolve()!=path for path in entries.values()):
        raise RuntimeError('Utility source directories contain symlinks; deletion refused')
    return set(entries) | {'SHA256SUMS'}


def prune_runtime(root, sources):
    retained=set(sources)|{'installation.json','remove-journal.json'}
    directories={str(parent) for name in retained for parent in Path(name).parents if str(parent)!='.'}
    def visit(folder):
        for path in folder.iterdir():
            relative=str(path.relative_to(root))
            if relative in retained:continue
            if relative in directories:visit(path)
            else:remove_path(path)
    visit(root)


def execute(root, record, targets, stop=None):
    root = Path(root)
    journal = root / 'remove-journal.json'
    installed=root/'installation.json'
    if record.get('directory') != str(root) or (installed.exists() and record.get('domain') != configuration.read_private_json(installed)['domain']):
        raise RuntimeError('Foreign removal journal')
    if not installed.exists():
        __import__('command_wrapper').trusted_runtime(root)
        required={'services-stopped','containers-removed','host-cleanup'} | {'local-'+key for key in LOCAL}
        if not all(record['local'].values()) or not required.issubset(record['completed']):
            raise RuntimeError('Removal finalization is unconfirmed')
    sources = source_names(root)
    def step(name, operation):
        if name in record['completed']:
            return
        operation()
        record['completed'].append(name)
        state_store.write_json(journal, record)
        print('Выполнено: ' + name, flush=True)
    state_store.write_json(journal, record)
    step('services-stopped', stop or (lambda: stop_services(root)))
    for index, identity in enumerate(record['s3']):
        name = 's3-' + str(index)
        if name in record['completed']:
            continue
        target = next((item for item in targets if item['identity'] == identity), None)
        if not target:
            raise RuntimeError('Restore original S3 profile before continuing removal')
        step(name, lambda: delete_s3(target['profile']))
    if record['external_database']:
        step('external-database', lambda: delete_external_database(root))
    if record['vault']:
        step('vault', lambda: delete_vault(root))
    step('containers-removed', lambda: remove_containers(root))
    if all(record['local'].values()):step('host-cleanup', lambda: host_cleanup(root))
    for key, (_, names) in LOCAL.items():
        if record['local'][key]:
            step('local-' + key, lambda names=names: [remove_path(root / name) for name in names])
    if all(record['local'].values()):
        # Preserve only the verified allowlisted utility and its public entry point.
        prune_runtime(root,sources)
        state_store.write_json(root / '.misbot-empty.json', {'family': 'misbot-matrix-portable', 'format': 1})
        (root / 'installation.json').unlink(missing_ok=True)
    else:
        metadata = configuration.read_private_json(root / 'installation.json')
        metadata['decommissioned'] = True
        state_store.write_json(root / 'installation.json', metadata)
    journal.unlink()
    print('Удаление выбранных ресурсов завершено. Утилита misbot сохранена.' + (' Для нового сервера: sudo misbot config init --domain example.org' if all(record['local'].values()) else ' Для новой установки сначала повторите remove с полным локальным удалением.'))


def stop_services(root):
    import backup_schedule
    import federation_runtime
    lifecycle.configure(root, 'remove')
    stop_update(root)
    # Check owned units before stopping or deleting any of them.
    base = backup_schedule.unit_name(root)
    paths = [backup_schedule.UNIT_DIRECTORY / (base + '.' + suffix) for suffix in ('timer', 'service')]
    for path in paths:
        backup_schedule.owned_unit(path, root)
    if any(path.exists() for path in paths):
        lifecycle.run(['systemctl', 'disable', '--now', base + '.timer'])
        lifecycle.run(['systemctl', 'stop', base + '.service'])
        for path in paths:
            path.unlink(missing_ok=True)
        lifecycle.run(['systemctl', 'daemon-reload'])
    federation_runtime.controller(root, lifecycle.run, False, remove=True)
    stop_turn(root)
    metadata = configuration.read_private_json(Path(root) / 'installation.json')
    if shutil.which('docker'):
        lifecycle.ownership_check()
        lifecycle.dc('stop')
    elif metadata.get('network_committed'):
        raise RuntimeError('Restore Docker to remove the installed server; no data deleted')


def stop_update(root):
    import release_update
    path=release_update.job_path(root)
    if not path.exists():return
    job=configuration.read_private_json(path)
    if job.get('state') not in ('queued','running'):return
    identifier=job.get('id','')
    expected=release_update.unit(root,identifier)
    if not re.fullmatch(r'[0-9a-f]{64}',identifier) or job.get('unit')!=expected:
        raise RuntimeError('Foreign update job; no deletion performed')
    result=subprocess.run(['systemctl','show',expected+'.service','--property=LoadState','--value'],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=15)
    if result.stdout.strip()=='not-found':return
    if result.returncode or result.stdout.strip()!='loaded':raise RuntimeError('Update worker state unconfirmed')
    lifecycle.run(['systemctl','stop',expected+'.service'])


def host_cleanup(root):
    root=Path(root)
    swap=root/'swapfile'
    if swap.exists():
        if swap.is_symlink():raise RuntimeError('Unexpected swap symlink; retained')
        active=lifecycle.run(['swapon','--show','--noheadings','--output','NAME']).splitlines()
        if str(swap) in active:lifecycle.run(['swapoff',str(swap)])
        fstab=Path('/etc/fstab')
        exact=str(swap)+' none swap sw 0 0'
        original=fstab.read_text()
        updated=''.join(line for line in original.splitlines(keepends=True) if line.strip()!=exact)
        if updated!=original:state_store.write_text(fstab,updated,mode=0o644)
        swap.unlink()
    stop_turn(root)
    sysctl=Path('/etc/sysctl.d/90-matrix-stack-rtc.conf')
    if sysctl.exists() and not sysctl.is_symlink() and sysctl.read_text()=='net.core.rmem_max=2500000\nnet.core.wmem_max=2500000\n':
        sysctl.unlink()


def stop_turn(root):
    root=Path(root)
    system=Path('/etc/systemd/system')
    service=system/'matrix-stack-turn-cert.service'
    marker='# Misbot TURN '+str(root)+'\n'
    if service.exists():
        expected='ExecStart=/usr/bin/python3 "'+str(root/'lifecycle.py')+'" --directory "'+str(root)+'" cert-renew'
        if service.is_symlink() or expected not in service.read_text().splitlines():
            raise RuntimeError('Foreign TURN certificate service; retained')
        paths=[system/('matrix-stack-turn-cert.'+suffix) for suffix in ('path','timer','service')]
        for path in paths:
            if path.exists() and (path.is_symlink() or (not path.read_text().startswith(marker) and path.name!=service.name
                    and 'Unit=matrix-stack-turn-cert.service' not in path.read_text().splitlines())):
                raise RuntimeError('Foreign certificate unit; retained')
        lifecycle.run(['systemctl','disable','--now','matrix-stack-turn-cert.path','matrix-stack-turn-cert.timer'])
        lifecycle.run(['systemctl','stop',service.name])
        for path in paths:path.unlink(missing_ok=True)
        lifecycle.run(['systemctl','daemon-reload'])


def remove_containers(root):
    if shutil.which('docker'):
        lifecycle.down()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--backup-config', type=Path)
    args = parser.parse_args()
    state_store.require_root()
    __import__('command_wrapper').trusted_runtime(args.directory)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('Удаление требует интерактивного терминала и всех подтверждений; флага --yes нет.')
    with state_store.operation_guard(args.directory):
        model, targets = discover(args.directory, args.backup_config)
        path = args.directory / 'remove-journal.json'
        if path.exists():
            record = configuration.read_private_json(path)
            print('Прерванное удаление. Завершены: ' + ', '.join(record['completed']))
            if input('Для продолжения прежнего плана введите УДАЛИТЬ СЕРВЕР ' + record['domain'] + '\n') != 'УДАЛИТЬ СЕРВЕР ' + record['domain']:
                return
        else:
            record = plan(args.directory, targets, model)
        if record:
            execute(args.directory, record, targets)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        print('Удаление отменено или не завершено. Выполненные шаги записаны в remove-journal.json; '
              'повторите sudo misbot remove с прежними профилями. До завершения сохраните оставшиеся credentials.', file=sys.stderr)
        raise SystemExit(1)
