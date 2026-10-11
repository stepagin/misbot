"""Misbot protocol v1: origin-bound RSA signatures and pinned HTTPS transport.

No caller supplied URLs, redirects, private IPs, or unverified response bodies.
"""

import base64
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import time

import configuration_schema as schema
import state_store

VERSION = 1
DISCOVERY = '/.well-known/misbot'
API = '/_misbot/v1/exchange'
LIMIT = 32768


class ProtocolError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def openssl(args, data=None):
    result = subprocess.run(
        ['openssl', *args],
        input=data,
        capture_output=True,
        timeout=20,
        **state_store.child_process_options(),
    )
    if result.returncode:
        raise ProtocolError('signature_dependency_or_key')
    return result.stdout


def public_key(key):
    return openssl(['pkey', '-in', str(key), '-pubout']).decode()


def fingerprint(pem):
    return hashlib.sha256(pem.encode()).hexdigest()


def sign(key, payload):
    return base64.b64encode(
        openssl(['dgst', '-sha256', '-sign', str(key)], canonical(payload))
    ).decode()


def verify(pem, payload, signature):
    try:
        if (
            not isinstance(pem, str)
            or len(pem) > 2048
            or not pem.startswith('-----BEGIN PUBLIC KEY-----')
        ):
            return False
        raw = base64.b64decode(signature, validate=True)
        if len(raw) != 384:
            return False  # protocol v1 is exactly RSA-3072
        with tempfile.TemporaryDirectory(prefix='misbot-signature-') as folder:
            key = Path(folder) / 'public.pem'
            sig = Path(folder) / 'signature'
            key.write_text(pem)
            sig.write_bytes(raw)
            result = subprocess.run(
                ['openssl', 'dgst', '-sha256', '-verify', str(key), '-signature', str(sig)],
                input=canonical(payload),
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
    except (ValueError, OSError, TypeError, subprocess.SubprocessError):
        return False


def document(server, identity, key, bot_mxid=None):
    pem = public_key(key)
    return {
        'protocol': VERSION,
        'instance_id': identity,
        'server_name': server,
        'bot_mxid': bot_mxid or '@admin-panel:' + server,
        'api_path': API,
        'algorithm': 'RSA-3072-SHA256',
        'public_key': pem,
        'key_id': fingerprint(pem),
    }


def validate_document(value, server):
    import re

    if (
        not isinstance(value, dict)
        or type(value.get('protocol')) is not int
        or value.get('protocol') != VERSION
    ):
        raise ProtocolError('unsupported_version')
    if (
        value.get('server_name') != server
        or not re.fullmatch(
            r'@[a-z0-9][a-z0-9._=-]{0,63}:' + re.escape(server), str(value.get('bot_mxid', ''))
        )
        or value.get('api_path') != API
        or value.get('algorithm') != 'RSA-3072-SHA256'
        or not re.fullmatch('[0-9a-f]{32}', str(value.get('instance_id', '')))
        or value.get('key_id') != fingerprint(value.get('public_key', ''))
    ):
        raise ProtocolError('discovery_identity')
    return value


def https(server, path, body=None):
    server = schema.domain(server)
    if path not in (DISCOVERY, API, '/.well-known/matrix/server', '/_matrix/federation/v1/version'):
        raise ProtocolError('unsafe_path')
    try:
        addresses = socket.getaddrinfo(server, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ProtocolError('non_public_destination')
        # Pin this resolution; TLS still authenticates the original hostname/SNI.
        family, kind, proto, _, address = addresses[0]
        sock = socket.socket(family, kind, proto)
        sock.settimeout(10)
        try:
            sock.connect(address)
            tls = ssl.create_default_context().wrap_socket(sock, server_hostname=server)
        except BaseException:
            sock.close()
            raise
        connection = http.client.HTTPSConnection(server, timeout=10)
        connection.sock = tls
        try:
            connection.request(
                'POST' if body is not None else 'GET',
                path,
                body=canonical(body) if body is not None else None,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            )
            response = connection.getresponse()
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ProtocolError('response_too_large')
            if response.status != 200:
                raise ProtocolError('remote_http_' + str(response.status))
            return json.loads(raw)
        finally:
            connection.close()
    except ProtocolError:
        raise
    except socket.gaierror:
        raise ProtocolError('dns_failed') from None
    except ssl.SSLError:
        raise ProtocolError('tls_failed') from None
    except (TimeoutError, socket.timeout):
        raise ProtocolError('timeout') from None
    except (ValueError, OSError, http.client.HTTPException):
        raise ProtocolError('connection_or_response_failed') from None


def discover(server, transport=https):
    return validate_document(transport(server, DISCOVERY), server)


def envelope(doc, key, target, body):
    value = {
        'protocol': VERSION,
        'sender': doc['server_name'],
        'instance_id': doc['instance_id'],
        'recipient': target['server_name'],
        'recipient_instance': target['instance_id'],
        'timestamp': int(time.time()),
        'nonce': secrets.token_hex(16),
        'method': 'POST',
        'path': API,
        'body': body,
    }
    return {'payload': value, 'signature': sign(key, value)}


def authenticate(request, own, remote, seen, now=None):
    now = int(time.time()) if now is None else now
    import re

    try:
        if not isinstance(request, dict) or set(request) != {'payload', 'signature'}:
            raise ProtocolError('invalid_request')
        value = request['payload']
        nonce = value['nonce']
        if not isinstance(value, dict) or set(value) != {
            'protocol',
            'sender',
            'instance_id',
            'recipient',
            'recipient_instance',
            'timestamp',
            'nonce',
            'method',
            'path',
            'body',
        }:
            raise ProtocolError('invalid_request')
        if type(value['protocol']) is not int or value['protocol'] != VERSION:
            raise ProtocolError('unsupported_version')
        if (
            value['sender'] != remote['server_name']
            or value['instance_id'] != remote['instance_id']
            or value['recipient'] != own['server_name']
            or value['recipient_instance'] != own['instance_id']
            or value['method'] != 'POST'
            or value['path'] != API
        ):
            raise ProtocolError('signature_identity')
        if type(value['timestamp']) is not int or abs(now - value['timestamp']) > 300:
            raise ProtocolError('expired_request')
        if not re.fullmatch('[0-9a-f]{32}', nonce):
            raise ProtocolError('invalid_nonce')
        if nonce in seen:
            raise ProtocolError('replay')
        if not verify(remote['public_key'], value, request['signature']):
            raise ProtocolError('invalid_signature')
        return value
    except (KeyError, TypeError):
        raise ProtocolError('invalid_request') from None
