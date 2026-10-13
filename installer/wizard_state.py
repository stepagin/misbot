"""Inspect or explicitly discard one private wizard draft, never installation data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import configuration
import configuration_schema
import state_store


def draft_path(root):
    return (
        Path.home()
        / '.local/state/misbot/wizard'
        / (hashlib.sha256(str(root).encode()).hexdigest() + '.json')
    )


def validate_directory(root):
    root = configuration_schema.installation_directory(str(root))
    if (
        not root.is_absolute()
        or root == Path('/')
        or root.resolve() != root
        or (root.exists() and not root.is_dir())
    ):
        raise ValueError('Нужен отдельный абсолютный каталог без symlink и переходов ..')
    return root


def read_draft(root):
    root = validate_directory(root)
    path = draft_path(root)
    if not path.parent.exists() and not path.parent.is_symlink():
        return path, None
    info = path.parent.lstat()
    if (
        path.parent.resolve() != path.parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise RuntimeError('Черновик не прочитан: проверьте владельца и права его каталога.')
    if not path.exists() and not path.is_symlink():
        return path, None
    saved = configuration.read_private_json(path)
    if (
        not isinstance(saved, dict)
        or saved.get('format') != 1
        or saved.get('directory') != str(root)
        or not isinstance(saved.get('values'), dict)
        or not isinstance(saved.get('backup', {}), dict)
        or type(saved.get('step')) is not int
        or not 0 <= saved['step'] <= 10
    ):
        raise RuntimeError('Черновик не прошёл проверку; файл сохранён без изменений.')
    return path, saved


def manage(root, action, confirm=None):
    root = validate_directory(root)
    if action not in ('show', 'reset'):
        raise ValueError('Неизвестная операция с черновиком.')
    if action == 'reset' and confirm != 'discard-draft':
        raise ValueError('Удаление черновика требует --confirm discard-draft.')
    path, saved = read_draft(root)
    result = {
        'stage': 'draft',
        'state': 'absent' if saved is None else 'saved',
        'directory': str(root),
        'draft_file': str(path),
    }
    if saved is None:
        return result
    if action == 'show':
        # Values can contain OAuth, S3 and database credentials.
        return dict(result, step=saved['step'] + 1)
    if action != 'reset':
        raise ValueError('Неизвестная операция с черновиком.')
    # Serialize discards independently of the installation backend. The exact
    # draft is the only removed path; previous drafts and checkpoints remain.
    with state_store.operation_guard(path.parent / (path.stem + '-draft')):
        path, saved = read_draft(root)
        if saved is not None:
            path.unlink()
    return dict(result, state='discarded')


def purge_installation_drafts(root):
    """Remove verified drafts for this installation when its secrets are deleted."""
    root = validate_directory(root)
    current, saved = read_draft(root)
    if not current.parent.exists():
        return
    candidates = [current] if saved is not None else []
    previous = current.parent / 'previous'
    if previous.exists() or previous.is_symlink():
        info = previous.lstat()
        if (
            previous.resolve() != previous
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise RuntimeError('Previous draft directory is not private; drafts retained')
        for path in previous.glob('*.json'):
            try:
                record = configuration.read_private_json(path)
            except (OSError, ValueError, RuntimeError):
                continue
            if (
                isinstance(record, dict)
                and record.get('format') == 1
                and record.get('directory') == str(root)
                and isinstance(record.get('values'), dict)
            ):
                candidates.append(path)
    # Legacy wizard probes wrote profiles beside the current draft.
    profiles = [
        current.with_name(current.stem + '-' + purpose + '.env') for purpose in ('media', 'backup')
    ]
    profiles.extend(current.parent.glob(current.stem + '-s3-probe-recovery-*.env'))
    for path in profiles:
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise RuntimeError('Wizard S3 profile is not private; profile retained')
        candidates.append(path)
    for path in candidates:
        path.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('action', choices=('show', 'reset'))
    parser.add_argument('--confirm')
    args = parser.parse_args()
    try:
        state_store.require_root()
        print(json.dumps(manage(args.directory, args.action, args.confirm), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print(
            'Черновик не изменён. Проверьте абсолютный --directory и приватные права файла. '
            'Удаление требует --confirm discard-draft; настройки сервера и checkpoints сохраняются.',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
