"""Local SOCKS5 + TLS server exercises the real curl transport without Telegram traffic."""

import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telegram_login_check as check

METADATA = {
    'issuer': 'https://oauth.telegram.org',
    'token_endpoint': 'https://oauth.telegram.org/token',
}


class LoginCheckTests(unittest.TestCase):
    def test_failures_never_expose_proxy_credentials_or_remote_response(self):
        secret = 'FICTIONAL_PRIVATE_PASSWORD'
        for result in (
            subprocess.CompletedProcess([], 97, secret, secret),
            subprocess.CompletedProcess([], 0, secret, secret),
        ):
            with self.subTest(code=result.returncode):
                with patch.object(check.subprocess, 'run', return_value=result) as run:
                    with self.assertRaises(check.LoginCheckError) as error:
                        check.check(
                            {'SOCKS5_CONFIG': f'socks5://user:{secret}@proxy.example.org:1080'}
                        )
                self.assertNotIn(secret, str(error.exception))
                self.assertNotIn(secret, str(run.call_args.args))
                self.assertIn(secret, run.call_args.kwargs['input'])
                self.assertEqual(run.call_args.kwargs['timeout'], 12)

    def test_direct_connection_does_not_use_environment_proxy_or_send_oidc_secrets(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(METADATA), '')
        with patch.object(check.subprocess, 'run', return_value=result) as run:
            check.check({'TELEGRAM_CLIENT_SECRET': 'FICTIONAL_PRIVATE'})
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs['input'], 'proxy = ""\nnoproxy = ""\n')
        self.assertNotIn('FICTIONAL_PRIVATE', str(run.call_args_list))

    @unittest.skipUnless(
        shutil.which('curl') and shutil.which('openssl'), 'curl and openssl required'
    )
    def test_real_socks_authentication_tls_http_repeats_and_mid_check_disconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            cert, key = (Path(folder) / name for name in ('cert.pem', 'key.pem'))
            subprocess.run(
                [
                    'openssl',
                    'req',
                    '-x509',
                    '-newkey',
                    'rsa:2048',
                    '-nodes',
                    '-days',
                    '1',
                    '-subj',
                    '/CN=oauth.telegram.org',
                    '-addext',
                    'subjectAltName=DNS:oauth.telegram.org',
                    '-keyout',
                    str(key),
                    '-out',
                    str(cert),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=10,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            real_run = subprocess.run
            for mode in ('success', 'disconnect', 'bad-password', 'bad-json'):
                with self.subTest(mode=mode), socket.socket() as server:
                    server.bind(('127.0.0.1', 0))
                    server.listen()
                    server.settimeout(5)
                    port = server.getsockname()[1]
                    exchanges, failures = [], []

                    def receive(sock, count):
                        data = b''
                        while len(data) < count:
                            block = sock.recv(count - len(data))
                            if not block:
                                raise RuntimeError('unexpected EOF')
                            data += block
                        return data

                    def serve():
                        try:
                            for index in range(3):
                                raw, _ = server.accept()
                                with raw:
                                    raw.settimeout(5)
                                    version, count = receive(raw, 2)
                                    methods = receive(raw, count)
                                    self.assertEqual(version, 5)
                                    self.assertIn(2, methods)
                                    raw.sendall(b'\x05\x02')
                                    version, count = receive(raw, 2)
                                    username = receive(raw, count)
                                    password = receive(raw, receive(raw, 1)[0])
                                    self.assertEqual(
                                        (version, username, password), (1, b'user', b'fictional')
                                    )
                                    if mode == 'bad-password':
                                        raw.sendall(b'\x01\x01')
                                        return
                                    raw.sendall(b'\x01\x00')
                                    self.assertEqual(receive(raw, 4), b'\x05\x01\x00\x03')
                                    hostname = receive(raw, receive(raw, 1)[0])
                                    self.assertEqual(hostname, b'oauth.telegram.org')
                                    self.assertEqual(receive(raw, 2), b'\x01\xbb')
                                    raw.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb')
                                    if mode == 'disconnect' and index == 1:
                                        return
                                    with context.wrap_socket(raw, server_side=True) as tls:
                                        request = b''
                                        while b'\r\n\r\n' not in request:
                                            request += tls.recv(4096)
                                        exchanges.append(request)
                                        body = (
                                            b'{}'
                                            if mode == 'bad-json'
                                            else json.dumps(METADATA).encode()
                                        )
                                        tls.sendall(
                                            b'HTTP/1.1 200 OK\r\nContent-Length: '
                                            + str(len(body)).encode()
                                            + b'\r\nConnection: close\r\n\r\n'
                                            + body
                                        )
                                    if mode == 'bad-json':
                                        return
                        except Exception as error:
                            failures.append(error)

                    thread = threading.Thread(target=serve, daemon=True)
                    thread.start()

                    def run(args, **kwargs):
                        return real_run([*args, '--cacert', str(cert)], **kwargs)

                    try:
                        with patch.object(check.subprocess, 'run', side_effect=run):
                            values = {'SOCKS5_CONFIG': f'socks5://user:fictional@127.0.0.1:{port}'}
                            if mode == 'success':
                                check.check(values)
                                self.assertEqual(len(exchanges), 3)
                            else:
                                with self.assertRaises(check.LoginCheckError):
                                    check.check(values)
                    finally:
                        thread.join(timeout=6)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(failures, [])
                    for request in exchanges:
                        self.assertIn(b'GET /.well-known/openid-configuration HTTP/', request)
                        self.assertNotIn(b'fictional', request)
