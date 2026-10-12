#!/usr/bin/env python3
"""Read-only checks before changing host settings or starting Matrix writers."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import threading
from urllib.parse import unquote, urlsplit

_STRICT_RESOLVER_SLOTS = threading.BoundedSemaphore(14)


def settings(root):
    import configuration

    return configuration.load(root)


def settings_path(root, area='server'):
    root = Path(root)
    return (
        root / 'settings' / (area + '.env')
        if (root / 'settings').exists()
        else root / 'settings.env'
    )


def private_settings_file(path):
    path = Path(path)
    info = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or info.st_mode & 0o077
        or info.st_uid != os.geteuid()
    ):
        raise RuntimeError(
            'Use an owned regular private env file, mode 600, without symlinks; correct its permissions and retry'
        )
    return settings_file(path)


def settings_file(path):
    return settings_text(Path(path).read_text(encoding='utf-8-sig'))


def settings_text(text):
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise RuntimeError(
                f'Invalid settings.env syntax on line {number}; edit the file privately'
            )
        key, value = (part.strip() for part in line.split('=', 1))
        if key in result:
            raise RuntimeError(f'Duplicate settings.env key on line {number}; keep one value')
        if value[:1] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                raise RuntimeError(f'Unclosed quotation in settings.env on line {number}')
            value = value[1:-1]
        result[key] = value
    return result


def resolve(host, family, timeout=10, strict=False):
    # getaddrinfo has no timeout argument. A daemon avoids blocking shutdown on a stuck resolver.
    done = threading.Event()
    result = []
    gate = _STRICT_RESOLVER_SLOTS if strict else None
    if gate is not None and not gate.acquire(blocking=False):
        raise RuntimeError('DNS resolver is busy; retry later')

    def query():
        try:
            result.append({row[4][0] for row in socket.getaddrinfo(host, 443, family)})
        except socket.gaierror as error:
            result.append(
                RuntimeError('DNS resolver is temporarily unavailable')
                if strict
                and error.errno
                not in (socket.EAI_NONAME, getattr(socket, 'EAI_NODATA', socket.EAI_NONAME))
                else set()
            )
        except OSError:
            result.append(RuntimeError('DNS resolver is unavailable') if strict else set())
        finally:
            if gate is not None:
                gate.release()
            done.set()

    try:
        threading.Thread(target=query, daemon=True).start()
    except Exception:
        if gate is not None:
            gate.release()
        raise
    if not done.wait(timeout):
        raise RuntimeError(
            'DNS query timed out for '
            + host
            + '; check the VPS resolver and retry install.sh preflight'
        )
    if isinstance(result[0], RuntimeError):
        raise result[0]
    return result[0]


def dns_check(domain, public_ip, mas_domain=''):
    from configuration_schema import public_hosts

    for host in public_hosts({'DOMAIN': domain, 'MAS_DOMAIN': mas_domain}):
        actual = resolve(host, socket.AF_INET)
        if actual != {public_ip}:
            raise RuntimeError(
                'DNS for '
                + host
                + ': expected only A='
                + public_ip
                + '; observed '
                + (', '.join(sorted(actual)) or 'no A record')
                + '. Correct the DNS A record, disable CDN proxy, allow caches to expire, '
                'then retry install.sh preflight and install.sh up in the same directory.'
            )
        if resolve(host, socket.AF_INET6):
            raise RuntimeError(
                'DNS for ' + host + ': unexpected AAAA. This deployment uses IPv4; '
                'remove AAAA for managed hosts, wait for DNS caches, then retry install.sh preflight.'
            )


def caa_query(host):
    if not shutil.which('dig'):
        raise RuntimeError(
            'CAA checking requires dig. Run sudo apt-get install dnsutils, then retry install.sh preflight'
        )
    try:
        result = subprocess.run(
            ['dig', '+time=3', '+tries=1', '+noall', '+answer', '+comments', host + '.', 'CAA'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(
            'CAA DNS query failed for '
            + host
            + '; check the VPS resolver and retry install.sh preflight'
        ) from None
    if result.returncode or not re.search(r'status: (NOERROR|NXDOMAIN),', result.stdout):
        raise RuntimeError(
            'CAA DNS answer is unavailable for '
            + host
            + '; correct the resolver or DNS service and retry install.sh preflight'
        )
    records = []
    try:
        for line in result.stdout.splitlines():
            if not line.strip() or line.startswith(';'):
                continue
            fields = shlex.split(line)
            # A recursive CAA answer may also contain chased CNAME records.
            if len(fields) >= 4 and fields[3].upper() == 'CAA':
                if len(fields) != 7 or not 0 <= int(fields[4]) <= 255:
                    raise ValueError()
                records.append((int(fields[4]), fields[5].lower(), fields[6]))
    except (ValueError, IndexError):
        raise RuntimeError(
            'CAA DNS answer cannot be parsed for '
            + host
            + '; inspect DNS privately and retry install.sh preflight'
        ) from None
    return records


def caa_check(domain, mas_domain=''):
    # RFC 8659: chase aliases in each query, then climb the original name tree
    # until the first nonempty CAA RRset, excluding the DNS root.
    cache = {}
    from configuration_schema import public_hosts

    for host in public_hosts({'DOMAIN': domain, 'MAS_DOMAIN': mas_domain}):
        current = host
        records = []
        while current:
            if current not in cache:
                cache[current] = caa_query(current)
            records = cache[current]
            if records:
                break
            current = current.partition('.')[2]
        if any(
            flags & 128 and tag not in ('issue', 'issuewild', 'iodef') for flags, tag, _ in records
        ):
            raise RuntimeError(
                'CAA for '
                + host
                + ' has an unsupported critical property at '
                + current
                + '; correct that DNS policy, wait for caches and retry install.sh preflight'
            )
        issuers = [value for _, tag, value in records if tag == 'issue']
        # Certificates cover named hosts, not wildcards. Restrictive issuer
        # parameters need an explicit supported policy rather than a false pass.
        if issuers and not any(
            value.strip().lower() in ('letsencrypt.org', 'letsencrypt.org;') for value in issuers
        ):
            raise RuntimeError(
                'CAA for '
                + host
                + ' does not provide an unrestricted letsencrypt.org issue policy at '
                + current
                + '; add CAA 0 issue "letsencrypt.org" in the DNS panel, wait for caches and retry install.sh preflight'
            )


def system_check(root):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError(
            'This installer requires a Linux amd64 VPS; use a compatible test machine'
        )
    memory = int(
        next(
            line.split()[1]
            for line in Path('/proc/meminfo').read_text().splitlines()
            if line.startswith('MemTotal:')
        )
    )
    if memory < 1800000:
        raise RuntimeError('Less than about 2 GiB RAM; choose a larger VPS before continuing')
    disk_target = Path(root)
    while not disk_target.exists():
        disk_target = disk_target.parent
    free = shutil.disk_usage(disk_target).free
    if free < 3 * 1024**3:
        raise RuntimeError(
            'Less than 3 GiB free; free disk space without deleting Matrix data/keys, '
            'then retry install.sh preflight'
        )
    state = (
        json.loads((root / 'installation.json').read_text())
        if (root / 'installation.json').exists()
        else {}
    )
    if not state.get('databases_initialized') and free < 10 * 1024**3:
        lock = json.loads(
            (
                (root / 'versions.json')
                if (root / 'versions.json').exists()
                else Path(__file__).parent / 'versions.json'
            ).read_text()
        )
        cached = shutil.which('docker') and all(
            subprocess.run(
                ['docker', 'image', 'inspect', digest],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
            for digest in lock['digests'].values()
        )
        if not cached:
            raise RuntimeError(
                'First image download requires at least 10 GiB free; expand the disk '
                'or free unrelated files, then retry install.sh preflight'
            )
    if not Path('/run/systemd/system').is_dir():
        raise RuntimeError('A VPS with systemd is required for TURN certificate renewal')


def docker_unavailable_message(root):
    return (
        'Docker daemon is unavailable; installation data and checkpoints were not deleted. '
        'On this VPS run sudo systemctl status docker, then sudo systemctl start docker. '
        'After Docker returns run sudo bash ' + str(Path(root) / 'install.sh') + ' status; '
        'follow any pending recovery command before install.sh up. See administration.md'
    )


def owned_ports(root):
    if not shutil.which('docker'):
        return set()

    def command(*args):
        result = subprocess.run(
            ['docker', *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20
        )
        if result.returncode:
            raise RuntimeError(docker_unavailable_message(root))
        return result.stdout

    ids = command('ps', '-aq', '--filter', 'label=com.docker.compose.project=matrix-stack').split()
    ports = set()
    if ids:
        projection = (
            r'{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}'
            r'{{printf "\t"}}{{json .State.Running}}{{printf "\t"}}{{json .NetworkSettings.Ports}}'
        )
        for line in command('inspect', '--format', projection, *ids).splitlines():
            owner, running, bindings = map(json.loads, line.split('\t'))
            if owner != str(root):
                raise RuntimeError(
                    'Another directory owns Compose project matrix-stack; '
                    'use the original installation or a separate machine'
                )
            if running:
                for name, rows in (bindings or {}).items():
                    for binding in rows or []:
                        ports.add((int(binding['HostPort']), name.rsplit('/', 1)[1]))
    return ports


def owns_route(root, subnet, device, allow_legacy=False):
    if not shutil.which('docker'):
        return False
    result = subprocess.run(
        ['docker', 'network', 'inspect', 'matrix-stack_default'],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        return False
    networks = json.loads(result.stdout)
    if len(networks) != 1:
        return False
    network = networks[0]
    labels = network.get('Labels') or {}
    bridge = (network.get('Options') or {}).get(
        'com.docker.network.bridge.name', 'br-' + network['Id'][:12]
    )
    owner = labels.get('org.misbot.matrix.installation.directory')
    return (
        labels.get('com.docker.compose.project') == 'matrix-stack'
        and (owner == str(root) or allow_legacy and owner is None)
        and any(row.get('Subnet') == subnet for row in network['IPAM']['Config'])
        and device == bridge
    )


def network_check(root, subnet):
    allowed = owned_ports(root)
    for protocol, kind, numbers in [
        ('tcp', socket.SOCK_STREAM, (80, 443, 7881)),
        ('udp', socket.SOCK_DGRAM, (3478, 7882)),
    ]:
        for port in numbers:
            if (port, protocol) in allowed:
                continue
            with socket.socket(type=kind) as sock:
                try:
                    sock.bind(('0.0.0.0', port))
                except OSError:
                    raise RuntimeError(
                        f'Required {protocol.upper()} port {port} is occupied by another service; '
                        'inspect ss -lntup privately and resolve the conflict before retrying install.sh preflight'
                    )
    candidate = ipaddress.ip_network(subnet, strict=True)
    if candidate.version != 4 or candidate.prefixlen != 24 or not candidate.is_private:
        raise RuntimeError(
            'DOCKER_SUBNET must be a free private IPv4 /24; correct settings.env before starting'
        )
    state = (
        json.loads((root / 'installation.json').read_text())
        if (root / 'installation.json').exists()
        else {}
    )
    result = subprocess.run(
        ['ip', '-j', 'route'], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True
    )
    for route in json.loads(result.stdout):
        destination = route.get('dst', 'default')
        if destination == 'default':
            continue
        existing = ipaddress.ip_network(destination, strict=False)
        legacy = state.get('network_committed') and str(existing) == state.get('subnet') == str(
            candidate
        )
        if str(existing) == str(candidate) and owns_route(
            root, str(candidate), route.get('dev'), allow_legacy=bool(legacy)
        ):
            continue
        if existing.version == candidate.version and existing.overlaps(candidate):
            raise RuntimeError(
                'DOCKER_SUBNET overlaps an existing LAN/VPN/Docker route; '
                'select a free private /24 before the first start. Do not change an installed subnet.'
            )


def receive(sock, count):
    value = b''
    while len(value) < count:
        part = sock.recv(count - len(value))
        if not part:
            raise RuntimeError(
                'Proxy closed the SOCKS5 handshake; check the proxy settings privately'
            )
        value += part
    return value


def proxy_check(values):
    if not values.get('TELEGRAM_CLIENT_ID') or not values.get('SOCKS5_CONFIG'):
        return
    try:
        parsed = urlsplit(values['SOCKS5_CONFIG'])
        if parsed.scheme not in ('socks5', 'socks5h') or not parsed.hostname or not parsed.port:
            raise ValueError()
        with socket.create_connection((parsed.hostname, parsed.port), timeout=8) as sock:
            authenticate = parsed.username is not None
            sock.sendall(b'\x05\x01' + (b'\x02' if authenticate else b'\x00'))
            if receive(sock, 2) != (b'\x05\x02' if authenticate else b'\x05\x00'):
                raise RuntimeError('Proxy refused the SOCKS5 authentication method')
            if authenticate:
                username = unquote(parsed.username).encode()
                password = unquote(parsed.password or '').encode()
                if not 1 <= len(username) <= 255 or len(password) > 255:
                    raise ValueError()
                sock.sendall(
                    b'\x01' + bytes([len(username)]) + username + bytes([len(password)]) + password
                )
                if receive(sock, 2) != b'\x01\x00':
                    raise RuntimeError('Proxy credentials were rejected')
            host = b'oauth.telegram.org'
            sock.sendall(b'\x05\x01\x00\x03' + bytes([len(host)]) + host + (443).to_bytes(2, 'big'))
            reply = receive(sock, 4)
            if reply[:2] != b'\x05\x00':
                raise RuntimeError('Proxy could not connect to Telegram')
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError(
            'Telegram SOCKS5 preflight failed. Correct SOCKS5_CONFIG/credentials privately, '
            'then retry install.sh preflight. To continue without optional Telegram, clear both '
            'TELEGRAM_CLIENT_ID and TELEGRAM_CLIENT_SECRET; do not change Matrix data or identity.'
        ) from None


def check(root):
    root = Path(root).resolve()
    values = settings(root)
    for provider in ('GOOGLE', 'TELEGRAM'):
        if bool(values.get(provider + '_CLIENT_ID')) != bool(
            values.get(provider + '_CLIENT_SECRET')
        ):
            raise RuntimeError(
                provider
                + ' requires both '
                + provider
                + '_CLIENT_ID and '
                + provider
                + '_CLIENT_SECRET. Edit '
                + str(settings_path(root, 'auth'))
                + ' privately; supply both or clear both to disable this optional provider. Then run: sudo bash '
                + shlex.quote(str(root / 'install.sh'))
                + ' preflight. Existing data and keys were not changed'
            )
    metadata = json.loads((root / 'installation.json').read_text())
    values = __import__('configuration').load(root, True, metadata.get('preview', False))
    if values['DOMAIN'] != metadata['domain']:
        raise RuntimeError(
            'DOMAIN differs from immutable Matrix identity; restore the original field'
        )
    __import__('configuration').assert_auth_identity(root, values, metadata)
    domain = metadata['domain']
    public_ip = values.get('PUBLIC_IP')
    if not public_ip:
        addresses = resolve(domain, socket.AF_INET)
        if len(addresses) != 1:
            raise RuntimeError(
                'Set PUBLIC_IP privately in '
                + str(settings_path(root))
                + ' or create a single DOMAIN A record; retry misbot config preflight'
            )
        public_ip = next(iter(addresses))
    try:
        address = ipaddress.ip_address(public_ip)
    except ValueError:
        raise RuntimeError(
            'PUBLIC_IP must be a public IPv4 address; edit '
            + str(settings_path(root))
            + ' privately, '
            'then retry install.sh preflight in the same directory'
        ) from None
    if not address.is_global or address.version != 4:
        raise RuntimeError('PUBLIC_IP must be a public IPv4 address')
    system_check(root)
    dns_check(domain, public_ip, values.get('MAS_DOMAIN', ''))
    caa_check(domain, values.get('MAS_DOMAIN', ''))
    network_check(root, values.get('DOCKER_SUBNET', '172.30.50.0/24'))
    proxy_check(values)
    return {
        'check': 'preflight',
        'passed': True,
        'hosts': len(__import__('configuration_schema').public_hosts(values)),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.directory)))
    except Exception as error:
        print(
            json.dumps(
                {
                    'check': 'preflight',
                    'passed': False,
                    'message': str(error)
                    if isinstance(error, RuntimeError)
                    else 'Invalid settings or unavailable resource; edit settings.env privately and retry install.sh preflight',
                }
            )
        )
        raise SystemExit(1)
