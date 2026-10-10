"""Owned, public system entry point; no secrets in the installed help runtime."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

FAMILY='misbot-matrix-portable'
PUBLIC_FILES=('misbot.py','cli_commands.py','command_wrapper.py','release.json')


def paths(system=Path('/')):
    return system/'usr/local/bin/misbot',system/'usr/local/lib/misbot'


def regular(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise RuntimeError('Managed command files must be regular files without links')
    return info


def trusted_installation(root):
    root=Path(root)
    if (not root.is_absolute() or root==Path('/') or root.resolve()!=root
            or any(ord(c)<32 for c in str(root))):
        raise RuntimeError('Specify an absolute installation directory without symlinks or control characters')
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError('Installation directory must be owned and protected from other writers')
    required={'installation.json','release.json','install.sh','generate.py','lifecycle.py'}
    required.update(path.name for path in root.glob('*.py'))
    for name in required:
        path=root/name; info=regular(path)
        if info.st_uid!=os.geteuid() or info.st_mode & 0o022:
            raise RuntimeError('Installation sources must belong to the administrator and must not be writable by other users')
    try:
        release=json.loads((root/'release.json').read_text())
        state=json.loads((root/'installation.json').read_text())
        if (release.get('family')!=FAMILY or not state.get('domain')
                or state.get('config_format_version',1) not in (1,2)
                or state.get('backend','portable') not in ('portable','existing-compose')):
            raise ValueError()
        if state.get('backend')=='existing-compose':
            backend=regular(root/'existing_compose.py')
            if backend.st_uid!=os.geteuid() or backend.st_mode & 0o022:
                raise ValueError()
    except (ValueError,TypeError):
        raise RuntimeError('This directory is not a compatible Misbot installation') from None
    return root


def record(system=Path('/')):
    launcher,lib=paths(system)
    for directory in (launcher.parent,lib,*lib.parents):
        if not directory.exists():continue
        info=directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink() or
                info.st_uid not in (0,os.geteuid()) or
                (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise RuntimeError('Unsafe command support directory; nothing overwritten')
    path=lib/'command.json'
    if not path.exists() and not path.is_symlink():
        if launcher.exists() or launcher.is_symlink() or lib.exists():
            raise RuntimeError('A foreign misbot command or support directory exists; nothing overwritten')
        return None
    info=regular(path)
    if info.st_uid!=os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError('The system command ownership record is unsafe')
    value=json.loads(path.read_text())
    if value.get('family')!=FAMILY or value.get('format')!=1:
        raise RuntimeError('The system command belongs to another product')
    for name,digest in value['hashes'].items():
        if name not in (*PUBLIC_FILES,'launcher'):
            raise RuntimeError('Unknown system command file; no cleanup performed')
        file=launcher if name=='launcher' else lib/name
        info=regular(file)
        if info.st_uid!=os.geteuid() or info.st_mode & 0o022:
            raise RuntimeError('Unsafe system command file ownership or permissions')
        if hashlib.sha256(file.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('System command was modified outside Misbot; no overwrite or removal performed')
    if set(value['hashes'])!=set(PUBLIC_FILES)|{'launcher'}:
        raise RuntimeError('Incomplete system command ownership record')
    return value


def atomic(path,data,mode):
    fd,name=tempfile.mkstemp(prefix='.misbot-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as output:
            output.write(data);output.flush();os.fsync(output.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def public_directory(path):
    missing=[];current=path
    while not current.exists():
        missing.append(current);current=current.parent
    for current in reversed(missing):
        current.mkdir(mode=0o755)
        current.chmod(0o755) # installer intentionally runs with umask 077


def install(root,source=None,system=Path('/')):
    root=trusted_installation(root)
    source=Path(source or root)
    launcher,lib=paths(system)
    previous=record(system)
    if previous and previous['directory']!=str(root):
        raise RuntimeError('Another installation owns the default command. Use misbot --directory PATH; remove its command explicitly before choosing a new default')
    payload={name:(source/name).read_bytes() for name in PUBLIC_FILES}
    if json.loads(payload['release.json'])['family']!=FAMILY:
        raise RuntimeError('Not a Misbot command source')
    if previous and set(p.name for p in lib.iterdir())!=set(PUBLIC_FILES)|{'command.json'}:
        raise RuntimeError('Unexpected support files exist; no overwrite performed')
    public_directory(launcher.parent)
    public_directory(lib)
    lib.chmod(0o755)
    # The launcher contains only a fixed interpreter and a public runtime path.
    payload['launcher']=('#!/usr/bin/python3\n# misbot-owned-command-v1\nimport os,sys\nos.execv("/usr/bin/python3", ["/usr/bin/python3", "-B", '+repr(str(lib/'misbot.py'))+', *sys.argv[1:]])\n').encode()
    value={'format':1,'family':FAMILY,'directory':str(root),
           'hashes':{name:hashlib.sha256(data).hexdigest() for name,data in payload.items()}}
    if json.loads((root/'installation.json').read_text()).get('backend')=='existing-compose':
        value['backend']='existing-compose'
    payload['command.json']=(json.dumps(value,indent=2)+'\n').encode()
    originals={}
    for name in payload:
        path=launcher if name=='launcher' else lib/name
        originals[path]=(path.read_bytes(),stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
    try:
        for name,data in payload.items():
            atomic(launcher if name=='launcher' else lib/name,data,0o755 if name=='launcher' else 0o644)
    except BaseException:
        for path,saved in originals.items():
            if saved is None:path.unlink(missing_ok=True)
            else:atomic(path,*saved)
        if not previous:lib.rmdir()
        raise
    return value


def remove(root,system=Path('/')):
    launcher,lib=paths(system)
    value=record(system)
    if value is None:return False
    if value['directory']!=str(Path(root)):
        raise RuntimeError('This installation does not own the default command; nothing removed')
    # Inspect every file before deleting any exact owned path.
    if set(p.name for p in lib.iterdir())!=set(PUBLIC_FILES)|{'command.json'}:
        raise RuntimeError('Unexpected support files exist; no removal performed')
    launcher.unlink()
    for name in (*PUBLIC_FILES,'command.json'):(lib/name).unlink()
    lib.rmdir()
    return True


def default_directory():
    # Help/version never call this function or read installation files.
    lib=Path(__file__).resolve().parent
    path=lib/'command.json'
    if path.is_file() and not path.is_symlink():
        info=path.stat()
        if info.st_uid!=0 or info.st_mode & 0o022:
            raise RuntimeError('Unsafe default command record; specify --directory')
        value=json.loads(path.read_text())
        if value.get('format')!=1 or value.get('family')!=FAMILY:
            raise RuntimeError('Invalid default command record; specify --directory')
        return Path(value['directory'])
    return None


def default_backend():
    """Read only the public command record; help must not read private settings."""
    path=Path(__file__).resolve().parent/'command.json'
    if not path.is_file() or path.is_symlink():return 'portable'
    info=path.stat()
    if info.st_uid!=0 or info.st_mode & 0o022:raise RuntimeError('Unsafe public command record')
    value=json.loads(path.read_text())
    if value.get('format')!=1 or value.get('family')!=FAMILY:
        raise RuntimeError('Invalid public command record')
    return value.get('backend','portable')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('install','remove','check'))
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--keep-default',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Use sudo for system command installation')
    # Serialize different installers competing for the same system command.
    with open('/run/misbot-command.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='check':
            previous=record()
            if previous and previous['directory']!=str(args.directory) and not args.keep_default:
                raise RuntimeError('Another installation owns misbot; select it explicitly')
        elif args.action in ('install','remove') and args.keep_default and record() and record()['directory']!=str(args.directory):
            print('Default command retained for its original installation; select this installation with --directory')
            return
        else:result=install(args.directory) if args.action=='install' else remove(args.directory)
    print(json.dumps({'check':'misbot_command','action':args.action,'passed':True}))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,KeyError):
        print('Command registration failed: check ownership, conflicting misbot command and installation path; existing data retained')
        raise SystemExit(1)
