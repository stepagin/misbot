#!/usr/bin/env python3
"""Resume Matrix federation backfill for rooms shared by two accounts.

Only Client API reads are performed. Synapse itself imports the original events
through federation when the destination account paginates room history.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import ssl
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


PAGE_SIZE = 100
PAGES_PER_ROOM = 5
MEDIA_PER_CYCLE = 3
STOP = False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


HTTPS_OPENER = build_opener(NoRedirect, HTTPSHandler(context=ssl.create_default_context()))


class MirrorError(Exception):
    def __init__(self, side, kind, status=None):
        self.side, self.kind, self.status = side, kind, status
        super().__init__(f'{side}: {kind}' + (f' ({status})' if status else ''))


def private_file(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'File must belong to this user and have mode 0600: {path}')


def write_private(path, data):
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != 'https'
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ('', '/')
    ):
        raise ValueError(
            'Matrix base URLs must be https://host[:port] without credentials or a path'
        )
    return url.rstrip('/')


def validate_config(config):
    for side in ('source', 'target'):
        config[f'{side}_url'] = validate_url(config[f'{side}_url'])
        if not config[f'{side}_user'].startswith('@') or ':' not in config[f'{side}_user']:
            raise ValueError(f'Invalid {side} Matrix user ID')
        token_file = config.get(f'{side}_token_file')
        helper = config.get(f'{side}_token_command')
        if bool(token_file) == bool(helper):
            raise ValueError(f'Set exactly one token file or token command for {side}')
        if token_file:
            private_file(Path(token_file))
        if helper and (not Path(helper).is_file() or not os.access(helper, os.X_OK)):
            raise ValueError(f'Token command is not executable: {helper}')
    if config['source_user'] == config['target_user']:
        raise ValueError('Source and target users must differ')
    if config['interval'] < 5 or config['reconcile_interval'] < 60:
        raise ValueError('Intervals are too short')
    manifest, origin = config.get('media_manifest'), config.get('media_origin')
    if bool(manifest) != bool(origin):
        raise ValueError('Set both media manifest and Matrix media origin')
    if manifest:
        private_file(Path(manifest))
        if not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?', origin):
            raise ValueError('Invalid Matrix media origin')
        if config['source_user'].split(':', 1)[1] != origin:
            raise ValueError('Media origin must match the source Matrix user ID server name')
    return config


class MatrixClient:
    def __init__(self, side, config):
        self.side = side
        self.base = config[f'{side}_url']
        self.token_file = config.get(f'{side}_token_file')
        self.helper = config.get(f'{side}_token_command')
        self.cached_token = None

    def token(self, refresh=False):
        if self.token_file:
            try:
                private_file(Path(self.token_file))
                token = Path(self.token_file).read_text(encoding='utf-8').strip()
            except (OSError, ValueError) as error:
                raise MirrorError(self.side, 'AUTH_REQUIRED') from error
        else:
            if refresh or self.cached_token is None:
                try:
                    result = subprocess.run(
                        [self.helper], capture_output=True, text=True, timeout=20, check=True
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    raise MirrorError(self.side, 'AUTH_REQUIRED') from error
                self.cached_token = result.stdout.strip()
            token = self.cached_token
        if not token or '\n' in token or '\r' in token:
            raise MirrorError(self.side, 'AUTH_REQUIRED')
        return token

    def get(self, path, params=None):
        url = self.base + '/_matrix/client/v3/' + path
        if params:
            url += '?' + urlencode(params)
        for attempt in range(2):
            request = Request(
                url,
                headers={
                    'Authorization': 'Bearer ' + self.token(refresh=bool(attempt)),
                    'Accept': 'application/json',
                },
            )
            try:
                with HTTPS_OPENER.open(request, timeout=25) as response:
                    payload = json.load(response)
                    if not isinstance(payload, dict):
                        raise MirrorError(self.side, 'INVALID_RESPONSE')
                    return payload
            except HTTPError as error:
                if error.code == 401 and attempt == 0 and self.helper:
                    continue
                if error.code in (401, 403):
                    raise MirrorError(self.side, 'AUTH_REQUIRED', error.code) from error
                raise MirrorError(self.side, 'HTTP', error.code) from error
            except (URLError, TimeoutError, OSError) as error:
                raise MirrorError(self.side, 'UNAVAILABLE') from error
            except (ValueError, UnicodeError) as error:
                raise MirrorError(self.side, 'INVALID_RESPONSE') from error
        raise MirrorError(self.side, 'AUTH_REQUIRED')

    def sync(self, since=None):
        params = {
            'timeout': 0,
            'filter': json.dumps({'room': {'timeline': {'limit': 100}}}, separators=(',', ':')),
        }
        if since:
            params['since'] = since
        return self.get('sync', params)

    def messages(self, room_id, cursor):
        return self.get(
            'rooms/' + quote(room_id, safe='') + '/messages',
            {'from': cursor, 'dir': 'b', 'limit': PAGE_SIZE},
        )


def connect(state_dir):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_dir.stat().st_mode & 0o077:
        raise ValueError('State directory must have mode 0700')
    db_path = state_dir / 'state.sqlite3'
    db = sqlite3.connect(db_path, timeout=20)
    os.chmod(db_path, 0o600)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms (
            room_id TEXT PRIMARY KEY, source_cursor TEXT, target_cursor TEXT,
            source_done INTEGER NOT NULL DEFAULT 0, target_done INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'BACKFILLING', last_success INTEGER,
            last_reconcile INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            room_id TEXT NOT NULL, side TEXT NOT NULL, event_id TEXT NOT NULL,
            PRIMARY KEY (room_id, side, event_id)
        );
        CREATE INDEX IF NOT EXISTS events_side_room ON events(side, room_id);
        CREATE TABLE IF NOT EXISTS media (
            media_id TEXT PRIMARY KEY, expected_size INTEGER, expected_sha256 TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
            next_retry INTEGER NOT NULL DEFAULT 0, last_error TEXT
        );
    """)
    return db


def set_meta(db, key, value):
    db.execute(
        'INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(value)),
    )
    db.commit()


def get_meta(db, key):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def record_events(db, side, room_id, events):
    db.executemany(
        'INSERT OR IGNORE INTO events VALUES (?, ?, ?)',
        (
            (room_id, side, e['event_id'])
            for e in events
            if isinstance(e, dict) and isinstance(e.get('event_id'), str)
        ),
    )


def apply_sync(db, side, response, joined):
    if not isinstance(response.get('next_batch'), str):
        raise MirrorError(side, 'INVALID_RESPONSE')
    field = side + '_cursor'
    done = side + '_done'
    for room_id, content in response.get('rooms', {}).get('join', {}).items():
        if room_id not in joined:
            continue
        db.execute('INSERT OR IGNORE INTO rooms(room_id) VALUES (?)', (room_id,))
        timeline = content.get('timeline', {})
        record_events(db, side, room_id, timeline.get('events', []))
        row = db.execute(
            f'SELECT {field}, {done} FROM rooms WHERE room_id=?', (room_id,)
        ).fetchone()
        if timeline.get('limited'):
            db.execute(
                f'UPDATE rooms SET {field}=?, {done}=0 WHERE room_id=?',
                (timeline.get('prev_batch'), room_id),
            )
        elif not row[0] and not row[1]:
            cursor = timeline.get('prev_batch')
            db.execute(
                f'UPDATE rooms SET {field}=?, {done}=? WHERE room_id=?',
                (cursor, int(not cursor), room_id),
            )
    set_meta(db, side + '_since', response['next_batch'])


def scan_room(db, client, side, room_id):
    field = side + '_cursor'
    done = side + '_done'
    row = db.execute(f'SELECT {field}, {done} FROM rooms WHERE room_id=?', (room_id,)).fetchone()
    if row[1] or not row[0]:
        return
    cursor = row[0]
    for _ in range(PAGES_PER_ROOM):
        if STOP or get_meta(db, 'stop_requested') == '1':
            return
        page = client.messages(room_id, cursor)
        if not isinstance(page.get('chunk'), list):
            raise MirrorError(side, 'INVALID_RESPONSE')
        next_cursor = page.get('end')
        if next_cursor == cursor:
            raise MirrorError(side, 'STALLED_PAGINATION')
        with db:
            record_events(db, side, room_id, page['chunk'])
            db.execute(
                f'UPDATE rooms SET {field}=?, {done}=?, last_success=? WHERE room_id=?',
                (next_cursor, int(not next_cursor), int(time.time()), room_id),
            )
        if not next_cursor:
            break
        cursor = next_cursor


def reset_side(db, side, room_id=None):
    field = side + '_cursor'
    done = side + '_done'
    with db:
        if room_id:
            db.execute('DELETE FROM events WHERE side=? AND room_id=?', (side, room_id))
            db.execute(
                f"UPDATE rooms SET {field}=NULL, {done}=0, status='BACKFILLING' WHERE room_id=?",
                (room_id,),
            )
        else:
            db.execute('DELETE FROM events WHERE side=?', (side,))
            db.execute(f"UPDATE rooms SET {field}=NULL, {done}=0, status='BACKFILLING'")
        db.execute('DELETE FROM meta WHERE key=?', (side + '_since',))


def refresh_rooms(db, joined):
    now = int(time.time())
    for room_id, last_reconcile in db.execute(
        'SELECT room_id, last_reconcile FROM rooms'
    ).fetchall():
        if room_id not in joined:
            db.execute("UPDATE rooms SET status='WAIT_JOIN' WHERE room_id=?", (room_id,))
        elif last_reconcile == 0:
            db.execute('UPDATE rooms SET last_reconcile=? WHERE room_id=?', (now, room_id))
    db.commit()


def reconcile(db):
    now = int(time.time())
    for room_id, source_done, target_done in db.execute(
        'SELECT room_id, source_done, target_done FROM rooms'
    ).fetchall():
        if not source_done or not target_done:
            status = 'BACKFILLING'
        else:
            missing = db.execute(
                """SELECT count(*) FROM events s WHERE s.room_id=? AND s.side='source'
                AND NOT EXISTS (SELECT 1 FROM events t WHERE t.room_id=s.room_id AND t.side='target' AND t.event_id=s.event_id)""",
                (room_id,),
            ).fetchone()[0]
            status = 'GAPS' if missing else 'LIVE'
        db.execute(
            'UPDATE rooms SET status=?, last_success=? WHERE room_id=?', (status, now, room_id)
        )
    db.commit()


def reset_for_full_scan(db, joined):
    with db:
        for room_id in joined:
            db.execute('DELETE FROM events WHERE room_id=?', (room_id,))
            db.execute(
                "UPDATE rooms SET source_cursor=NULL, target_cursor=NULL, source_done=0, target_done=0, status='BACKFILLING', last_reconcile=? WHERE room_id=?",
                (int(time.time()), room_id),
            )
        db.execute("DELETE FROM meta WHERE key IN ('source_since', 'target_since')")


def check_identity(client, expected):
    actual = client.get('account/whoami').get('user_id')
    if actual != expected:
        raise MirrorError(client.side, 'WRONG_ACCOUNT')


def load_media_manifest(db, config):
    manifest = config.get('media_manifest')
    if not manifest:
        return
    private_file(Path(manifest))
    with Path(manifest).open(encoding='utf-8') as stream, db:
        for number, line in enumerate(stream, 1):
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) not in (2, 3) or not re.fullmatch(r'[A-Za-z0-9_-]+', fields[0]):
                raise ValueError(f'Invalid media manifest line {number}')
            if not fields[1].isdigit():
                raise ValueError(f'Invalid media size at line {number}')
            digest = fields[2].lower() if len(fields) == 3 and fields[2] else None
            if digest and not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError(f'Invalid media digest at line {number}')
            old = db.execute(
                'SELECT expected_size, expected_sha256 FROM media WHERE media_id=?', (fields[0],)
            ).fetchone()
            expected_size = int(fields[1])
            if old and old != (expected_size, digest):
                raise ValueError(
                    f'Manifest changed for media ID at line {number}; use a new state directory'
                )
            db.execute(
                'INSERT OR IGNORE INTO media(media_id, expected_size, expected_sha256) VALUES (?, ?, ?)',
                (fields[0], expected_size, digest),
            )


def fetch_media(client, origin, media_id, cached_only):
    path = (
        '/_matrix/client/v1/media/download/'
        + quote(origin, safe='')
        + '/'
        + quote(media_id, safe='')
    )
    url = (
        client.base
        + path
        + (
            '?allow_remote=false&allow_redirect=false'
            if cached_only
            else '?allow_remote=true&allow_redirect=false'
        )
    )
    request = Request(
        url,
        headers={'Authorization': 'Bearer ' + client.token(), 'Accept': 'application/octet-stream'},
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with HTTPS_OPENER.open(request, timeout=60) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except HTTPError as error:
        raise MirrorError('target', 'MEDIA_HTTP', error.code) from error
    except (URLError, TimeoutError, OSError) as error:
        raise MirrorError('target', 'MEDIA_UNAVAILABLE') from error
    return size, digest.hexdigest()


def prefetch_media(db, config, client):
    if not config.get('media_manifest'):
        return
    now = int(time.time())
    rows = db.execute(
        """SELECT media_id, expected_size, expected_sha256, attempts FROM media
        WHERE status != 'VERIFIED' AND next_retry <= ? ORDER BY attempts, media_id LIMIT ?""",
        (now, MEDIA_PER_CYCLE),
    ).fetchall()
    for media_id, expected_size, expected_hash, attempts in rows:
        if STOP or get_meta(db, 'stop_requested') == '1':
            break
        try:
            fetched = fetch_media(client, config['media_origin'], media_id, False)
            cached = fetch_media(client, config['media_origin'], media_id, True)
            if (
                fetched != cached
                or fetched[0] != expected_size
                or (expected_hash and fetched[1] != expected_hash)
            ):
                raise MirrorError('target', 'MEDIA_MISMATCH')
            db.execute(
                "UPDATE media SET status='VERIFIED', last_error=NULL WHERE media_id=?", (media_id,)
            )
        except MirrorError as error:
            delay = min(3600, 30 * (2 ** min(attempts, 7)))
            db.execute(
                "UPDATE media SET status='ERROR', attempts=attempts+1, next_retry=?, last_error=? WHERE media_id=?",
                (now + delay, str(error), media_id),
            )
        db.commit()


def sleep_until_next(db, seconds):
    until = time.monotonic() + seconds
    while not STOP and time.monotonic() < until and get_meta(db, 'stop_requested') != '1':
        time.sleep(min(1, until - time.monotonic()))


def run(state_dir):
    global STOP
    lock_path = state_dir / 'worker.lock'
    with lock_path.open('a+') as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Mirror is already running') from error
        private_file(state_dir / 'config.json')
        config = validate_config(
            json.loads((state_dir / 'config.json').read_text(encoding='utf-8'))
        )
        db = connect(state_dir)
        clients = {side: MatrixClient(side, config) for side in ('source', 'target')}
        signal.signal(signal.SIGTERM, lambda *_: stop_signal())
        signal.signal(signal.SIGINT, lambda *_: stop_signal())
        set_meta(db, 'pid', os.getpid())
        set_meta(db, 'stop_requested', 0)
        set_meta(db, 'state', 'STARTING')
        try:
            while not STOP and get_meta(db, 'stop_requested') != '1':
                try:
                    for side, client in clients.items():
                        check_identity(client, config[f'{side}_user'])
                    source_rooms = set(
                        clients['source'].get('joined_rooms').get('joined_rooms', [])
                    )
                    target_rooms = set(
                        clients['target'].get('joined_rooms').get('joined_rooms', [])
                    )
                    joined = source_rooms & target_rooms
                    for room_id in source_rooms:
                        db.execute(
                            "INSERT OR IGNORE INTO rooms(room_id, status) VALUES (?, 'WAIT_JOIN')",
                            (room_id,),
                        )
                    db.commit()
                    refresh_rooms(db, joined)
                    load_media_manifest(db, config)
                    last = int(get_meta(db, 'last_full_scan') or 0)
                    if last and time.time() - last >= config['reconcile_interval']:
                        reset_for_full_scan(db, joined)
                        set_meta(db, 'last_full_scan', int(time.time()))
                    for side, client in clients.items():
                        since = get_meta(db, side + '_since')
                        try:
                            response = client.sync(since)
                        except MirrorError as error:
                            if error.status == 400 and since:
                                reset_side(db, side)
                                response = client.sync()
                            else:
                                raise
                        apply_sync(db, side, response, joined)
                    for room_id in sorted(joined):
                        for side, client in clients.items():
                            try:
                                scan_room(db, client, side, room_id)
                            except MirrorError as error:
                                if error.status == 400:
                                    reset_side(db, side, room_id)
                                else:
                                    raise
                    prefetch_media(db, config, clients['target'])
                    reconcile(db)
                    db.execute(
                        "UPDATE rooms SET status='WAIT_JOIN' WHERE room_id NOT IN ({})".format(
                            ','.join('?' for _ in joined) or 'NULL'
                        ),
                        tuple(joined),
                    )
                    db.commit()
                    if not get_meta(db, 'last_full_scan'):
                        set_meta(db, 'last_full_scan', int(time.time()))
                    set_meta(db, 'state', 'RUNNING')
                    pending = db.execute(
                        "SELECT count(*) FROM rooms WHERE status='BACKFILLING'"
                    ).fetchone()[0]
                    pending += db.execute(
                        "SELECT count(*) FROM media WHERE status!='VERIFIED' AND next_retry<=?",
                        (int(time.time()),),
                    ).fetchone()[0]
                    sleep_until_next(db, 2 if pending else config['interval'])
                except MirrorError as error:
                    state = (
                        'AUTH_REQUIRED'
                        if error.kind in ('AUTH_REQUIRED', 'WRONG_ACCOUNT')
                        else ('WAIT_SOURCE' if error.side == 'source' else 'WAIT_TARGET')
                    )
                    set_meta(db, 'state', state)
                    set_meta(db, 'last_error', str(error))
                    print(
                        f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} {error}',
                        file=sys.stderr,
                        flush=True,
                    )
                    sleep_until_next(db, config['interval'])
        finally:
            set_meta(db, 'state', 'STOPPED')
            set_meta(db, 'pid', '')
            db.close()


def stop_signal():
    global STOP
    STOP = True


def is_running(state_dir):
    lock_path = state_dir / 'worker.lock'
    if not lock_path.exists():
        return False
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def status(state_dir):
    db_path = state_dir / 'state.sqlite3'
    if not db_path.exists():
        raise ValueError('No mirror state at this path')
    db = connect(state_dir)
    config_path = state_dir / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    total, verified, errors = db.execute(
        "SELECT count(*), sum(status='VERIFIED'), sum(status='ERROR') FROM media"
    ).fetchone()
    media_state = (
        'NOT_CONFIGURED'
        if not config.get('media_manifest')
        else ('VERIFIED_LOCAL_ORIGIN' if total == verified else 'INCOMPLETE')
    )
    result = {
        'running': is_running(state_dir),
        'state': get_meta(db, 'state'),
        'last_error': get_meta(db, 'last_error'),
        'media': {
            'state': media_state,
            'total': total,
            'verified': verified or 0,
            'errors': errors or 0,
        },
        'rooms': [],
    }
    result['media']['recent_errors'] = [
        {'media_id': media_id, 'error': error}
        for media_id, error in db.execute(
            "SELECT media_id, last_error FROM media WHERE status='ERROR' ORDER BY next_retry DESC LIMIT 10"
        )
    ]
    for room_id, state, success in db.execute(
        'SELECT room_id, status, last_success FROM rooms ORDER BY room_id'
    ):
        source_count, target_count = [
            db.execute(
                'SELECT count(*) FROM events WHERE room_id=? AND side=?', (room_id, side)
            ).fetchone()[0]
            for side in ('source', 'target')
        ]
        missing = db.execute(
            """SELECT count(*) FROM events s WHERE s.room_id=? AND s.side='source'
            AND NOT EXISTS (SELECT 1 FROM events t WHERE t.room_id=s.room_id AND t.side='target' AND t.event_id=s.event_id)""",
            (room_id,),
        ).fetchone()[0]
        result['rooms'].append(
            {
                'room_id': room_id,
                'state': state,
                'source_events': source_count,
                'target_events': target_count,
                'missing_events': missing,
                'last_success': success,
            }
        )
    db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('start', help='Save parameters and launch a background worker')
    start.add_argument('--state-dir', type=Path, required=True)
    for side in ('source', 'target'):
        start.add_argument(f'--{side}-url', required=True)
        start.add_argument(f'--{side}-user', required=True)
        credentials = start.add_mutually_exclusive_group(required=True)
        credentials.add_argument(f'--{side}-token-file', type=Path)
        credentials.add_argument(f'--{side}-token-command', type=Path)
    start.add_argument('--interval', type=int, default=30, help='Seconds between live checks')
    start.add_argument(
        '--reconcile-interval', type=int, default=1800, help='Seconds between full event ID scans'
    )
    start.add_argument(
        '--media-manifest', type=Path, help='Private TSV: media_id, byte size, optional SHA-256'
    )
    start.add_argument('--media-origin', help='Original Matrix server_name for MXC URLs')
    for name in ('run', 'resume', 'status', 'stop'):
        sub.add_parser(name).add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    state_dir = args.state_dir.expanduser().resolve()
    try:
        if args.command == 'start':
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if state_dir.stat().st_mode & 0o077 or is_running(state_dir):
                raise ValueError('State directory is not private or mirror is already running')
            config = {
                key: str(value.resolve()) if isinstance(value, Path) else value
                for key, value in vars(args).items()
                if key not in ('command', 'state_dir') and value is not None
            }
            validate_config(config)
            existing = state_dir / 'config.json'
            if existing.exists():
                private_file(existing)
                old = json.loads(existing.read_text(encoding='utf-8'))
                if any(
                    old[f'{side}_user'] != config[f'{side}_user'] for side in ('source', 'target')
                ):
                    raise ValueError(
                        'Existing state belongs to different accounts; use another state directory'
                    )
                if old.get('media_origin') and old['media_origin'] != config.get('media_origin'):
                    raise ValueError(
                        'Existing media state belongs to another origin; use another state directory'
                    )
            write_private(state_dir / 'config.json', config)
        if args.command in ('start', 'resume'):
            if not (state_dir / 'config.json').exists() or is_running(state_dir):
                raise ValueError('Mirror configuration is missing or worker is already running')
            private_file(state_dir / 'config.json')
            validate_config(json.loads((state_dir / 'config.json').read_text(encoding='utf-8')))
            db = connect(state_dir)
            set_meta(db, 'stop_requested', 0)
            db.close()
            log = os.open(state_dir / 'worker.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        'run',
                        '--state-dir',
                        str(state_dir),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                os.close(log)
            for _ in range(50):
                if is_running(state_dir):
                    break
                if worker.poll() is not None:
                    raise ValueError('Worker exited during startup; see worker.log')
                time.sleep(0.1)
            if not is_running(state_dir):
                raise ValueError('Worker did not start; see worker.log')
            status(state_dir)
        elif args.command == 'run':
            run(state_dir)
        elif args.command == 'status':
            status(state_dir)
        else:
            if not (state_dir / 'state.sqlite3').exists():
                raise ValueError('No mirror state at this path')
            db = connect(state_dir)
            set_meta(db, 'stop_requested', 1)
            db.close()
            for _ in range(150):
                if not is_running(state_dir):
                    print('Mirror stopped.')
                    break
                time.sleep(0.2)
            else:
                print('Stop requested; the worker is still finishing its current request.')
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f'mirror: {error}\n')


if __name__ == '__main__':
    main()
