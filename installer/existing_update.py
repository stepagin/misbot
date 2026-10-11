"""Verified public source updates for an adopted stack; core pins are preserved."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import uuid

import command_wrapper
from existing_compose import Installation, Error, digest, emit, private_directory, save, interrupted
import update


def sources(folder):
    if not folder.is_absolute() or folder.resolve() != folder:
        raise Error('Нужен доверенный абсолютный source каталог без symlink.')
    info = folder.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise Error('Source каталог должен принадлежать root без чужой записи.')
    entries, revision = update.validate_manifest(folder)
    update.release(folder)
    protected = {'installation.json', 'docker-compose.yml', 'settings', '.misbot',
                 'synapse', 'mas', 'postgres', 'live', 'config', 'data', 'secrets', 'run'}
    for name, path in entries.items():
        parts=Path(name).parts
        public_directories={'site','configuration-examples','synapse-s3','referral-bot','referral-control','federation-bot','telegram-bot'}
        public_file=(len(parts)==1 and (Path(name).suffix in ('.py','.md') or
                     name in ('install.sh','versions.json','release.json','misbot-api.openapi.json')))
        if parts[0] in protected or not (public_file or (len(parts)>1 and parts[0] in public_directories)):
            raise Error('Source комплект содержит приватный путь установки.')
        info = command_wrapper.regular(path)
        if path.resolve() != path or info.st_uid != 0 or info.st_mode & 0o022:
            raise Error('Source файлы должны принадлежать root без чужой записи.')
    if not {'existing_compose.py', 'existing_s3.py', 'existing_update.py'}.issubset(entries):
        raise Error('Source комплект не поддерживает adopted backend.')
    return entries, revision


def restore(installation):
    path = installation.control / 'source-update.json'
    if not path.exists():
        emit('existing_source_recovery', pending=False)
        return
    record = json.loads(path.read_text())
    folder = Path(record['checkpoint'])
    if folder.parent != installation.control / 'source-updates' or folder.resolve() != folder:
        raise Error('Некорректный source checkpoint; файлы сохранены.')
    previous = json.loads((folder / 'files.json').read_text())
    for name in record['files']:
        target = installation.root / name
        if target.resolve() != target or not target.is_relative_to(installation.root):
            raise Error('Некорректный путь source recovery.')
        if name in previous:
            saved = folder / 'before' / name
            if digest(saved) != previous[name]['sha256']:
                raise Error('Source checkpoint повреждён.')
            command_wrapper.atomic(target, saved.read_bytes(), previous[name]['mode'])
        elif target.exists():
            # Remove only a newly published public file with the accepted hash.
            if digest(target) != record['files'][name]:
                raise Error('Новый source файл изменён; сохраните checkpoint.')
            target.unlink()
    command_wrapper.install(installation.root)
    installation.validate()
    path.unlink()
    emit('existing_source_recovery', passed=True, core_services_restarted=False)


def apply(installation, source):
    installation.require_no_pending()
    journal = installation.control / 'source-update.json'
    if journal.exists():
        raise Error('Есть незавершённый source update; выполните misbot update recover --source PATH.')
    entries, revision = sources(source)
    candidate = update.release(source)
    update.run_data_migrations(source, installation.root, candidate, 'check')
    previous, _ = update.validate_manifest(installation.root)
    installation.validate()
    # The candidate backend can take a backup before its public source is applied.
    last = installation.control / 'last-backup.json'
    selected = json.loads(last.read_text()) if last.exists() else {}
    reused = False
    if selected.get('remote', {}).get('verified'):
        directory = Path(selected.get('directory', ''))
        if directory.resolve()!=directory or not directory.is_relative_to(Path(installation.model['backup_directory'])):
            raise Error('Проверенная копия находится вне принятого каталога.')
        manifest = json.loads((directory/'manifest.json').read_text())
        images_match=manifest.get('images')==installation.model['images']
        pending=installation.control/'features/enable.json'
        if not images_match and pending.exists():
            stage=Path(json.loads(pending.read_text())['checkpoint'])
            if stage.parent!=installation.control/'features/checkpoints' or stage.resolve()!=stage:
                raise Error('Неверный feature checkpoint для source update.')
            before=json.loads((stage/'installation.before').read_text())
            original=before['existing']['images'];current=installation.model['images']
            images_match=(manifest.get('images')==original and
                all(current.get(name)==value for name,value in original.items()) and
                set(current)-set(original)<= {'misbot-referral-bot','misbot-federation-bot','misbot-admin'} and
                before['domain']==installation.state['domain'] and
                before['existing']['identity']==installation.model['identity'])
        reused = (manifest.get('server_name')==installation.state['domain'] and
                  images_match and
                  manifest.get('encrypted_archive_sha256')==selected.get('sha256'))
    if not reused:
        configured = installation.control / 's3/backup.env'
        installation.backup(configured if configured.exists() else None)
        selected = json.loads(last.read_text())
        if not selected.get('remote', {}).get('verified'):
            installation.verify()
    emit('existing_source_backup', verified=True, reused=reused,
         scope='public sources only; configuration, images and database data preserved')
    entries = {**entries, 'SHA256SUMS': source / 'SHA256SUMS'}
    changes = {name: p for name, p in entries.items()
               if not (installation.root / name).exists() or digest(installation.root / name) != digest(p)}
    if not changes:
        if candidate.get('data_schema_version'):
            checkpoint = private_directory(installation.control / 'source-updates' / uuid.uuid4().hex)
            update.run_data_migrations(source, installation.root, candidate, 'apply', checkpoint)
        command_wrapper.install(installation.root)
        emit('existing_source_update', passed=True, already_current=True)
        return
    folder = private_directory(installation.control / 'source-updates' / uuid.uuid4().hex)
    before = {}
    for name in changes:
        target = installation.root / name
        if target.exists():
            info = command_wrapper.regular(target)
            if target.resolve() != target or info.st_uid != 0 or info.st_mode & 0o022:
                raise Error('Небезопасный source target; ничего не опубликовано.')
            saved = folder / 'before' / name
            saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(target, saved)
            saved.chmod(0o600)
            before[name] = {'sha256': digest(saved), 'mode': stat.S_IMODE(info.st_mode)}
    save(folder / 'files.json', before)
    save(journal, {'format': 1, 'checkpoint': str(folder), 'revision': revision,
                   'files': {name: digest(p) for name, p in changes.items()}})
    try:
        update.run_data_migrations(source, installation.root, candidate, 'apply', folder)
        for name, source_file in changes.items():
            target = installation.root / name
            command_wrapper.public_directory(target.parent)
            command_wrapper.atomic(target, source_file.read_bytes(), 0o755 if name == 'install.sh' else 0o644)
        update.validate_manifest(installation.root)
        installation.validate()
        command_wrapper.install(installation.root)
        if installation.model.get('features',{}).get('enabled') and installation.model['features'].get('telegram_runtime'):
            from existing_features import Features
            Features(installation.root).refresh_telegram(installation.model['features'],folder)
        journal.unlink()
    except BaseException:
        update.run_data_migrations(source, installation.root, candidate, 'rollback-check')
        restore(installation)
        raise
    emit('existing_source_update', passed=True, files_changed=len(changes),
         checkpoint=str(folder), core_images_changed=False, core_services_restarted=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('action', choices=('update', 'update-recover'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        return 77
    os.umask(0o077)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        sources(args.source)
        installation = Installation(args.directory)
        with installation.lock():
            if args.action == 'update':
                apply(installation, args.source)
            else:
                update.run_data_migrations(args.source, installation.root, update.release(args.source), 'rollback-check')
                restore(installation)
        return 0
    except Exception as error:
        emit('existing_source_update', passed=False,
             explanation=str(error) if isinstance(error, Error) else 'Source update не подтверждён. Сохраните checkpoint и выполните misbot status.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
