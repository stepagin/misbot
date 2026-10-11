"""One private loader and recoverable publication for legacy/split configuration."""

import json
import os
from pathlib import Path
import stat
import secrets
import shlex
import configuration_schema as schema
import state_store


def read_private(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8-sig') as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size > 131072
            ):
                raise schema.InvalidConfiguration(
                    path,
                    'file',
                    'private_permissions',
                    'обычный файл владельца root, mode 0600, без ссылок',
                )
            return stream.read(131073)
    except schema.InvalidConfiguration:
        raise
    except (OSError, UnicodeError):
        raise schema.InvalidConfiguration(
            path, 'file', 'file_unreadable', 'файл недоступен; проверьте путь, права и UTF-8'
        ) from None


def read_private_json(path):
    return json.loads(read_private(path))


def parse(text, path, allowed):
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise schema.InvalidConfiguration(
                path, 'line ' + str(number), 'syntax', 'ожидается KEY=value; shell-код запрещён'
            )
        key, value = (p.strip() for p in line.split('=', 1))
        if key not in allowed:
            raise schema.InvalidConfiguration(
                path,
                'line ' + str(number),
                'unknown_field',
                'неизвестное поле; исходный файл сохраните приватно',
            )
        if key in result:
            raise schema.InvalidConfiguration(
                path, key, 'duplicate_field', 'оставьте одно определение'
            )
        if value[:1] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                raise schema.InvalidConfiguration(path, key, 'syntax', 'закройте кавычки')
            value = value[1:-1]
        result[key] = value
    return result


AREAS = ('server', 'policy', 'media-s3', 'auth', 'database', 'site')
AREA = {}


def pending(root):
    path = Path(root) / 'configuration-transition.json'
    if path.exists() or path.is_symlink():
        raise RuntimeError(
            'Configuration publication is incomplete. Keep checkpoints; run: sudo misbot --directory '
            + shlex.quote(str(root))
            + ' config recover'
        )


def directory(path):
    path = Path(path)
    if path.is_symlink() or path.absolute() != path.resolve():
        raise RuntimeError('Configuration directory cannot contain symbolic links or ..')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError(
            'Configuration directory must be private, mode 0700, owned by the operator'
        )
    return path


def load_bundle(folder, complete=True, preview=False):
    folder = directory(folder)
    values = {}
    origins = {}
    backup = None
    allowed_names = {area + '.env' for area in AREAS} | {'backup.env'}
    if any(p.name not in allowed_names for p in folder.iterdir()):
        raise schema.InvalidConfiguration(
            folder,
            'file',
            'unknown_file',
            'удалите неизвестный файл из активного набора, сохранив его приватно',
        )
    for area in AREAS:
        path = folder / (area + '.env')
        if not path.exists() and not path.is_symlink():
            if area == 'server':
                raise schema.InvalidConfiguration(
                    path, 'file', 'required', 'восстановите основной файл из приватной копии'
                )
            continue
        fields = {k: v for k, v in schema.FIELDS.items() if v.area == AREA.get(area, area)}
        part = parse(read_private(path), path, fields)
        values.update(part)
        origins.update({k: str(path) for k in part})
    path = folder / 'backup.env'
    if path.exists() or path.is_symlink():
        backup = schema.validate_backup(
            parse(read_private(path), path, schema.BACKUP_FIELDS), str(path)
        )
    return schema.validate(values, origins, complete, preview), backup


def _load(root, complete=False, preview=False):
    root = Path(root)
    pending(root)
    if (root / 'settings').exists() or (root / 'settings').is_symlink():
        if (root / 'settings.env').exists():
            raise RuntimeError(
                'Both configuration formats exist. Run config migrate; do not edit competing sources'
            )
        model, backup = load_bundle(root / 'settings', complete, preview)
        if backup and Path(backup['BACKUP_KEY_FILE']).is_relative_to(root):
            raise schema.InvalidConfiguration(
                root / 'settings/backup.env',
                'BACKUP_KEY_FILE',
                'key_location',
                'recovery key должен быть вне установки',
            )
        return model
    path = root / 'settings.env'
    values = parse(read_private(path), path, schema.FIELDS)
    return schema.validate(values, {k: str(path) for k in values}, complete, preview)


def load(root, complete=False, preview=False):
    # A reader shares the writer lock: checking a marker alone has a TOCTOU race.
    with state_store.operation_guard(root):
        return _load(root, complete, preview)


def backup_profile(root):
    with state_store.operation_guard(root):
        pending(root)
        if not (Path(root) / 'settings').exists():
            return None
        return load_bundle(Path(root) / 'settings', False)[1]


def serialize(values):
    # Literal parser strips matching outer quotes only; preserve arbitrary secret characters.
    def literal(value):
        if value[:1] in ('"', "'") or value != value.strip():
            return '"' + value + '"'
        return value

    return ''.join(k + '=' + literal(v) + '\n' for k, v in values.items())


def stage_bundle(folder, model, backup):
    directory(folder)
    for area in AREAS:
        fields = {
            k: model[k]
            for k, v in schema.FIELDS.items()
            if k in model and v.area == AREA.get(area, area)
        }
        if area != 'server' and not any(v != schema.FIELDS[k].default for k, v in fields.items()):
            continue
        state_store.write_text(folder / (area + '.env'), serialize(fields))
    if backup:
        state_store.write_text(folder / 'backup.env', serialize(schema.validate_backup(backup)))


def snapshot(root, folder):
    root = Path(root)
    folder = directory(folder)
    pending(root)
    if (root / 'settings').exists():
        target = folder / 'settings'
        directory(target)
        for path in (root / 'settings').iterdir():
            state_store.write_text(target / path.name, read_private(path))
    else:
        state_store.write_text(folder / 'settings.env', read_private(root / 'settings.env'))


def export_text(root):
    return serialize(load(root))


def assert_auth_identity(root, model, metadata):
    if not any(
        metadata.get(key)
        for key in ('databases_initialized', 'storage_committed', 'restore_pending')
    ):
        return
    expected = metadata.get('mas_issuer')
    path = Path(root) / 'config/mas.yaml'
    if path.exists() or path.is_symlink():
        try:
            import yaml

            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError()
            issuer = yaml.safe_load(path.read_text()).get('http', {}).get('issuer')
            if not isinstance(issuer, str) or not issuer or (expected and expected != issuer):
                raise ValueError()
            expected = issuer
        except ImportError:
            raise RuntimeError(
                'Missing local dependency: install python3-yaml, then retry config validate'
            ) from None
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            raise RuntimeError(
                'Runtime MAS issuer does not match its saved identity; retain original files and backups'
            ) from None
    if not expected or expected != 'https://' + schema.mas_host(model) + '/':
        raise RuntimeError(
            'MAS issuer is immutable after database initialization; restore the original MAS_DOMAIN before applying'
        )


def assert_identity(root, model, metadata):
    if metadata.get('domain') != model['DOMAIN']:
        raise RuntimeError('Configuration domain differs from immutable Matrix identity')
    assert_auth_identity(root, model, metadata)
    path = Path(root) / 'config/homeserver.yaml'
    if path.exists() or path.is_symlink():
        try:
            import yaml

            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError()
            saved = yaml.safe_load(path.read_text())
            if saved.get('server_name') != metadata['domain']:
                raise ValueError()
        except ImportError:
            raise RuntimeError(
                'Missing local dependency: sudo apt-get install -y python3-yaml; retry config migrate'
            ) from None
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            raise RuntimeError(
                'Runtime Matrix identity does not match configuration; retain original files and backups'
            ) from None


def recover(root, rollback=False):
    root = Path(root)
    with state_store.operation_guard(root):
        path = root / 'configuration-transition.json'
        if not path.exists():
            return {
                'stage': 'configuration',
                'status': 'ready',
                'format': 2 if (root / 'settings').exists() else 1,
            }
        record = read_private_json(path)
        if (
            record.get('format') != 1
            or not isinstance(record.get('id'), str)
            or not __import__('re').fullmatch('[0-9a-f]{32}', record['id'])
        ):
            raise RuntimeError('Invalid configuration journal; keep private checkpoints')
        allowed_names = {area + '.env' for area in AREAS} | {'backup.env'}
        if (
            not isinstance(record.get('files'), list)
            or not isinstance(record.get('old_files', []), list)
            or any(
                name not in allowed_names for name in record['files'] + record.get('old_files', [])
            )
        ):
            raise RuntimeError('Unsafe configuration file list; keep checkpoints')
        stage = root / 'stages/configuration' / record['id']
        metadata = (
            read_private_json(root / 'installation.json')
            if (root / 'installation.json').exists()
            else {}
        )
        if rollback:
            old = directory(stage / 'previous')
            if (old / 'settings.env').exists():
                model = schema.validate(
                    parse(read_private(old / 'settings.env'), old / 'settings.env', schema.FIELDS),
                    preview=record.get('preview', False),
                )
            elif (old / 'settings').exists():
                model, _ = load_bundle(old / 'settings', True, record.get('preview', False))
            else:
                raise RuntimeError(
                    'No previous configuration exists; retain the initial journal and use config recover'
                )
            if metadata.get('domain') and metadata['domain'] != model['DOMAIN']:
                raise RuntimeError('Previous configuration belongs to another Matrix identity')
            assert_auth_identity(root, model, metadata)
            if (root / 'settings').exists():
                directory(root / 'settings')
                if any(p.name not in allowed_names for p in (root / 'settings').iterdir()):
                    raise RuntimeError(
                        'Unknown active configuration files; retain them privately before rollback'
                    )
            if (old / 'settings.env').exists():
                # Remove only known, owned configuration files from this publication.
                if (root / 'settings').exists():
                    directory(root / 'settings')
                    for name in record['files']:
                        (root / 'settings' / name).unlink(missing_ok=True)
                    for name in record.get('old_files', []):
                        (root / 'settings' / name).unlink(missing_ok=True)
                    (root / 'settings').rmdir()
                state_store.write_text(root / 'settings.env', read_private(old / 'settings.env'))
                metadata['config_format_version'] = 1
            else:
                target = directory(root / 'settings')
                for name in set(record['files']) | set(record.get('old_files', [])):
                    (target / name).unlink(missing_ok=True)
                for src in (old / 'settings').iterdir():
                    state_store.write_text(target / src.name, read_private(src))
                metadata['config_format_version'] = 2
        else:
            model, backup = load_bundle(stage / 'next', True, record.get('preview', False))
            if metadata.get('domain') and metadata['domain'] != model['DOMAIN']:
                raise RuntimeError(
                    'Configuration identity differs from the installation; keep checkpoints'
                )
            assert_auth_identity(root, model, metadata)
            target = directory(root / 'settings')
            for name in record.get('old_files', []):
                if name not in record['files']:
                    (target / name).unlink(missing_ok=True)
            for name in record['files']:
                state_store.write_text(target / name, read_private(stage / 'next' / name))
            loaded, checked = load_bundle(target, True, record.get('preview', False))
            if loaded != model or checked != backup:
                raise RuntimeError('Configuration verification failed; run config recover')
            (root / 'settings.env').unlink(missing_ok=True)
            metadata['config_format_version'] = 2
        if metadata:
            state_store.write_json(root / 'installation.json', metadata)
        __import__('vault_cache').mark_dirty(root)
        path.unlink()
        return {
            'stage': 'configuration',
            'status': 'ready',
            'format': metadata.get('config_format_version', 2),
            'checkpoint': str(stage / 'previous'),
        }


def write_model(root, values, backup=None, preview=False):
    root = Path(root)
    model = schema.validate(values, preview=preview)
    if backup:
        schema.validate_backup(backup)
    if backup and Path(backup['BACKUP_KEY_FILE']).is_relative_to(root):
        raise schema.InvalidConfiguration(
            root / 'settings/backup.env',
            'BACKUP_KEY_FILE',
            'key_location',
            'recovery key должен быть вне установки',
        )
    with state_store.operation_guard(root):
        pending(root)
        if (root / 'installation.json').exists():
            metadata = read_private_json(root / 'installation.json')
            if metadata.get('domain') and metadata['domain'] != model['DOMAIN']:
                raise RuntimeError(
                    'DOMAIN/server_name are immutable; restore the original field before applying'
                )
            assert_auth_identity(root, model, metadata)
        if (root / 'settings.env').exists():
            if (root / 'settings').exists():
                raise RuntimeError(
                    'Both configuration formats exist; retain files and resolve the configuration checkpoint before applying'
                )
            # Existing format remains authoritative until explicit migration.
            if backup:
                raise RuntimeError('Run config migrate before configuring external backups')
            __import__('vault_cache').mark_dirty(root)
            state_store.write_text(root / 'settings.env', serialize(model))
            return
        directory(root)
        previous_backup = backup_profile(root) if (root / 'settings').exists() else None
        identifier = secrets.token_hex(16)
        stage = directory(root / 'stages/configuration' / identifier)
        old = directory(stage / 'previous')
        if (root / 'settings').exists():
            snapshot(root, old)
        stage_bundle(stage / 'next', model, backup if backup is not None else previous_backup)
        state_store.write_json(
            root / 'configuration-transition.json',
            {
                'format': 1,
                'id': identifier,
                'files': [p.name for p in (stage / 'next').iterdir()],
                'old_files': [p.name for p in (root / 'settings').iterdir()]
                if (root / 'settings').exists()
                else [],
                'preview': preview,
            },
        )
        recover(root)


def migrate(root):
    root = Path(root)
    with state_store.operation_guard(root):
        if (root / 'configuration-transition.json').exists():
            return recover(root)
        state_store.assert_update_allowed(root)
        if (root / 'settings').exists():
            load(root, True)
            return {'stage': 'configuration', 'status': 'ready', 'format': 2}
        metadata = read_private_json(root / 'installation.json')
        model = load(root, True, metadata.get('preview', False))
        if any(
            metadata.get(k)
            for k in (
                'restore_pending',
                'migration_frozen',
                'media_transition_pending',
                'credential_transition_pending',
            )
        ):
            raise RuntimeError(
                'Finish pending restoration/storage/credentials operation before config migrate'
            )
        assert_identity(root, model, metadata)
        identifier = secrets.token_hex(16)
        stage = directory(root / 'stages/configuration' / identifier)
        snapshot(root, stage / 'previous')
        stage_bundle(stage / 'next', model, None)
        state_store.write_json(
            root / 'configuration-transition.json',
            {
                'format': 1,
                'id': identifier,
                'files': [p.name for p in (stage / 'next').iterdir()],
                'old_files': [],
                'preview': metadata.get('preview', False),
            },
        )
        return recover(root)


def patch(root, changes):
    with state_store.operation_guard(root):
        model = load(root)
        model.update(changes)
        preview = (
            read_private_json(Path(root) / 'installation.json').get('preview', False)
            if (Path(root) / 'installation.json').exists()
            else False
        )
        write_model(root, model, preview=preview)


def restore_snapshot(root, folder):
    folder = Path(folder)
    if (folder / 'settings').exists():
        model, backup = load_bundle(folder / 'settings')
        write_model(root, model, backup)
    else:
        path = folder / 'settings.env'
        model = parse(read_private(path), path, schema.FIELDS)
        write_model(root, model)


def apply_settings(root, changes):
    import lifecycle

    root = Path(root)
    allowed = {
        'PUBLIC_IP',
        'ACME_EMAIL',
        'FEDERATION_SERVERS',
        'OPEN_REGISTRATION',
        'REGISTRATION_TOKEN_REQUIRED',
        'INVITATION_LIFETIME_SECONDS',
        'INVITATION_USAGE_LIMIT',
    }
    if set(changes) - allowed:
        raise RuntimeError(
            'Immutable identity, storage and credentials require their dedicated backend operations'
        )
    with state_store.operation_guard(root):
        state_store.assert_update_allowed(root)
        before = load(root, True)
        proposed = dict(before)
        proposed.update(changes)
        schema.validate(proposed)
        assert_identity(root, proposed, read_private_json(root / 'installation.json'))
        lifecycle.configure(root, 'configuration-apply')
        lifecycle.ownership_check()
        lifecycle.backup()
        lifecycle.restore_check()
        checkpoint = directory(root / 'stages/configuration' / secrets.token_hex(16))
        snapshot(root, checkpoint)
        try:
            write_model(root, proposed)
            lifecycle.up()
        except (Exception, KeyboardInterrupt):
            if (root / 'configuration-transition.json').exists():
                recover(root, rollback=True)
            restore_snapshot(root, checkpoint)
            raise RuntimeError(
                'Settings restored; retain the checked backup and run: sudo misbot --directory '
                + shlex.quote(str(root))
                + ' start'
            ) from None


def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('action', choices=('validate', 'migrate', 'recover', 'rollback', 'apply'))
    args = parser.parse_args()
    try:
        state_store.require_root()
        import command_wrapper

        command_wrapper.trusted_installation(args.directory)
        with state_store.operation_guard(args.directory):
            if args.action == 'validate':
                metadata = read_private_json(args.directory / 'installation.json')
                model = load(args.directory, True, metadata.get('preview', False))
                assert_identity(args.directory, model, metadata)
                print(
                    json.dumps(
                        {
                            'stage': 'configuration',
                            'status': 'ready',
                            'format': 2 if (args.directory / 'settings').exists() else 1,
                        }
                    )
                )
                return 0
            if args.action == 'migrate':
                result = migrate(args.directory)
            elif args.action in ('recover', 'rollback'):
                result = recover(args.directory, args.action == 'rollback')
            else:
                import lifecycle

                state_store.assert_update_allowed(args.directory)
                metadata = read_private_json(args.directory / 'installation.json')
                model = load(args.directory, True, metadata.get('preview', False))
                assert_identity(args.directory, model, metadata)
                lifecycle.configure(args.directory, 'configuration-apply')
                lifecycle.ownership_check()
                if metadata.get('databases_initialized'):
                    lifecycle.backup()
                    lifecycle.restore_check()
                else:
                    snapshot(
                        args.directory,
                        directory(args.directory / 'stages/configuration' / secrets.token_hex(16)),
                    )
                lifecycle.up()
                result = {'stage': 'configuration', 'status': 'ready'}
            print(json.dumps(result, ensure_ascii=False))
            return 0
    except KeyboardInterrupt:
        print(
            'Configuration operation interrupted; retain checkpoints. Run misbot status, then config recover if publication is pending',
            file=sys.stderr,
        )
        return 130
    except schema.InvalidConfiguration as error:
        print(json.dumps(error.result(), ensure_ascii=False))
        return 2
    except RuntimeError as error:
        print(
            json.dumps(
                {
                    'stage': 'configuration',
                    'status': 'failed',
                    'code': 'pending_or_runtime',
                    'explanation': str(error),
                },
                ensure_ascii=False,
            )
        )
        return 1
    except (OSError, ValueError, KeyError):
        print(
            'Configuration could not be read safely; retain checkpoints and run config validate',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
