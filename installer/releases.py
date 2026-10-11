"""Public GitHub releases and verified, bounded downloads. No GitHub token needed."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request

REPOSITORY = 'stepagin/misbot'
API = 'https://api.github.com/repos/' + REPOSITORY + '/releases'
INTERVAL = 3 * 24 * 3600
ARCHIVE = 'matrix-installer.tar.gz'
MAX_ARCHIVE = 64 * 1024 * 1024


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('Expected a stable major.minor.patch version')
    return tuple(map(int, value.removeprefix('v').split('.')))


def https(url, limit):
    def valid(target):
        parsed = urllib.parse.urlsplit(target)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and parsed.hostname in
                ('api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                 'objects.githubusercontent.com'))
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not valid(newurl):
                raise RuntimeError('Untrusted release redirect')
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    if not valid(url):
        raise RuntimeError('Untrusted release URL')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
        'User-Agent': 'Misbot-release-check', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise RuntimeError('Release download exceeds size limit')
        return raw
    except (OSError, ValueError):
        raise RuntimeError('GitHub Releases недоступен. Повторите позже; установленная версия сохранена.') from None


def latest(fetch=https):
    releases = []
    # GitHub orders publication dates, not semantic versions. Inspect all pages.
    for page in range(1, 101):
        items = json.loads(fetch(API + '?per_page=100&page=' + str(page), 4 * 1024 * 1024))
        if not isinstance(items, list):
            raise RuntimeError('Invalid GitHub release response')
        for item in items:
            if item.get('draft') or item.get('prerelease'):
                continue
            try:
                value = version(item.get('tag_name'))
            except ValueError:
                continue
            releases.append((value, item))
        if len(items) < 100:
            break
    else:
        raise RuntimeError('Release pagination limit reached; latest version unconfirmed')
    if not releases:
        return None
    value, item = max(releases, key=lambda pair: pair[0])
    number = '.'.join(map(str, value))
    return {'version': number, 'tag': item['tag_name'],
            'url': 'https://github.com/' + REPOSITORY + '/releases/tag/' + item['tag_name'],
            'notes': ''.join(c for c in str(item.get('body') or '') if c in '\n\t' or ord(c) >= 32)[:3500],
            'assets': item.get('assets', [])}


def minor_update(installed, candidate):
    old, new = version(installed), version(candidate)
    return new[0] == old[0] and new[1] > old[1]


def installed(root):
    root = Path(root)
    metadata = json.loads((root / 'installation.json').read_text())
    return metadata.get('applied_release_version') or json.loads((root / 'release.json').read_text())['version']


def comparison(root, release):
    current = installed(root)
    portable=json.loads((Path(root)/'installation.json').read_text()).get('backend','portable')=='portable'
    return {'installed': current, 'latest': release['version'] if release else None,
            'update_available': bool(release and version(release['version']) > version(current)),
            'url': release['url'] if release else None, 'notes': release['notes'] if release else '',
            'update_supported':portable, 'command': 'sudo misbot update latest' if portable else 'sudo misbot update apply --source /root/matrix-installer'}


def download(root, release, fetch=https):
    """Check outer digest, archive paths/size, inner manifest and product version."""
    import update
    parent = Path(root) / 'stages/release-downloads'
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    assets = {item['name']: item for item in release['assets'] if item.get('state') == 'uploaded'}
    if not {ARCHIVE, ARCHIVE + '.sha256'} <= assets.keys():
        raise RuntimeError('Релиз не содержит готового установщика и checksum; обновление не начато.')
    def asset_url(name):
        expected = 'https://github.com/' + REPOSITORY + '/releases/download/' + release['tag'] + '/' + name
        if assets[name].get('browser_download_url') != expected:
            raise RuntimeError('Unexpected release asset URL')
        return expected
    size = assets[ARCHIVE].get('size')
    if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
        raise RuntimeError('Unsupported release archive size')
    if shutil.disk_usage(parent).free < size + 256 * 1024 * 1024 + 1024 ** 3:
        raise RuntimeError('Недостаточно свободного диска для загрузки и распаковки релиза: нужен резерв 1 ГиБ.')
    checksum = fetch(asset_url(ARCHIVE + '.sha256'), 4096).decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{64}  ' + re.escape(ARCHIVE), checksum):
        raise RuntimeError('Invalid release archive checksum')
    raw = fetch(asset_url(ARCHIVE), MAX_ARCHIVE)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != checksum.split()[0] or (assets[ARCHIVE].get('digest') and assets[ARCHIVE]['digest'] != 'sha256:' + digest):
        raise RuntimeError('Release archive checksum mismatch; installed files unchanged')
    folder = Path(tempfile.mkdtemp(prefix='release-', dir=parent))
    folder.chmod(0o700)
    archive = folder / ARCHIVE
    archive.write_bytes(raw)
    archive.chmod(0o600)
    with tarfile.open(archive, 'r:gz') as tar:
        total, names = 0, set()
        for member in tar.getmembers():
            relative = Path(member.name)
            if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                    or relative.parts[0] != 'matrix-installer' or member.name in names
                    or not member.isfile() or member.size < 0):
                raise RuntimeError('Unsafe release archive member')
            names.add(member.name)
            total += member.size
            if total > 256 * 1024 * 1024 or len(names) > 1000:
                raise RuntimeError('Expanded release archive exceeds supported size')
        for member in tar.getmembers():
            target = folder / member.name
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with tar.extractfile(member) as incoming, target.open('wb') as output:
                shutil.copyfileobj(incoming, output)
            target.chmod(0o700 if target.name == 'install.sh' else 0o600)
    source = folder / 'matrix-installer'
    update.validate_manifest(source)
    if update.release(source)['version'] != release['version']:
        raise RuntimeError('Release tag and payload version differ')
    return source


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(comparison(args.directory,latest()),ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError):
        print('Последняя версия не подтверждена. Повторите проверку GitHub Releases позже.')
        raise SystemExit(1)
