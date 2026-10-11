"""Two real isolated HTTPS listeners; test CA and loopback routing are fixture-only.

Uses real signatures, TLS hostname verification, HTTP and persistent state. Policy
application is simulated: this test does not assert Matrix federation works.
"""

import http.client
import http.server
import json
import multiprocessing
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import federation
import federation_protocol as protocol
import state_store


def transport_for(ports, certificate):
    def transport(server, path, body=None):
        context = ssl.create_default_context(cafile=str(certificate))
        connection = http.client.HTTPSConnection(server, ports[server], context=context, timeout=10)
        import socket

        raw = socket.create_connection(('127.0.0.1', ports[server]), timeout=10)
        connection.sock = context.wrap_socket(raw, server_hostname=server)
        try:
            connection.request(
                'POST' if body else 'GET',
                path,
                body=protocol.canonical(body) if body else None,
                headers={'Content-Type': 'application/json'},
            )
            response = connection.getresponse()
            value = json.loads(response.read(protocol.LIMIT + 1))
            if response.status != 200:
                raise protocol.ProtocolError(value['code'])
            return value
        finally:
            connection.close()

    return transport


def backend_for(root, ports, cert):
    def apply(servers):
        values = configuration.load(root)
        values['FEDERATION_SERVERS'] = ','.join(servers)
        configuration.write_model(root, values)

    return federation.Backend(
        root,
        transport=transport_for(ports, cert),
        apply=apply,
        admin=lambda actor: actor == 'root',
        admission=False,
    )


def listener(root, ports, cert, key, ready):
    import signal

    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    backend = backend_for(root, ports, cert)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            raw = protocol.canonical(value)
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.reply(200, backend.doc())

        def do_POST(self):
            try:
                self.reply(
                    200,
                    backend.receive(
                        json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    ),
                )
            except protocol.ProtocolError as error:
                self.reply(400, {'code': error.code})

    server = http.server.HTTPServer(('127.0.0.1', ports[backend.load()['server_name']]), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    ready.set()
    server.serve_forever()


class RealHTTPS(unittest.TestCase):
    def test_two_instances_signed_approval_over_actual_verified_tls(self):
        import socket

        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            cert = folder / 'certificate.pem'
            key = folder / 'tls-private.pem'
            subprocess.run(
                [
                    'openssl',
                    'req',
                    '-x509',
                    '-newkey',
                    'rsa:2048',
                    '-nodes',
                    '-keyout',
                    str(key),
                    '-out',
                    str(cert),
                    '-days',
                    '1',
                    '-subj',
                    '/CN=a.example',
                    '-addext',
                    'subjectAltName=DNS:a.example,DNS:b.example',
                ],
                check=True,
                capture_output=True,
            )
            ports = {}
            roots = {}
            processes = []
            for server in ('a.example', 'b.example'):
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                ports[server] = sock.getsockname()[1]
                sock.close()
                root = folder / server
                root.mkdir(mode=0o700)
                roots[server] = root
                state_store.write_json(root / 'installation.json', {'domain': server})
                state_store.write_text(
                    root / 'settings.env',
                    'DOMAIN='
                    + server
                    + '\nPUBLIC_IP=8.8.8.8\nACME_EMAIL=operator@example.org\nFEDERATION_SERVERS='
                    + server
                    + '\n',
                )
                federation.setup(root, {'domain': server})
            try:
                for server in roots:
                    ready = multiprocessing.Event()
                    process = multiprocessing.Process(
                        target=listener, args=(roots[server], ports, cert, key, ready)
                    )
                    process.start()
                    processes.append(process)
                    self.assertTrue(ready.wait(10))
                a = backend_for(roots['a.example'], ports, cert)
                b = backend_for(roots['b.example'], ports, cert)
                rid = a.operate('invite', 'b.example')['id']
                self.assertNotIn('a.example', b.servers())
                self.assertEqual(b.operate('approve', rid)['state'], 'connected')
                self.assertEqual(a.operate('retry', rid)['state'], 'connected')
                self.assertEqual(a.operate('request-status', rid)['state'], 'connected')
                self.assertIn('b.example', a.servers())
                self.assertIn('a.example', b.servers())
                # Test CA is required: a normal trust store rejects the fixture certificate.
                with self.assertRaises(ssl.SSLError):
                    with socket.create_connection(('127.0.0.1', ports['a.example'])) as raw:
                        ssl.create_default_context().wrap_socket(raw, server_hostname='a.example')
            finally:
                for process in processes:
                    process.terminate()
                    process.join(10)


if __name__ == '__main__':
    unittest.main()
