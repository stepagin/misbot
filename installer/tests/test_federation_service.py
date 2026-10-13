"""Real narrow Unix HTTP/RPC listeners, isolated fake installation and peers."""

import http.client
import hashlib
import contextlib
import io
from email.message import Message
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import federation
import federation_service
import federation_runtime
import state_store


class ServiceTests(unittest.TestCase):
    def test_running_job_recovery_is_idempotent_and_preserves_queued_work_and_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'fixture.example'})
            federation.setup(root, {'domain': 'fixture.example'})
            backend = federation.Backend(root, admission=False)
            saved = backend.load()
            saved['jobs'] = {
                'FO-' + 'a' * 32: {'state': 'running'},
                'FO-' + 'b' * 32: {'state': 'queued', 'action': 'api-enable'},
            }
            backend.save(saved)
            key = backend.key.read_bytes()
            with patch.object(backend, 'save', wraps=backend.save) as save:
                federation_service.recover_running_jobs(backend)
                first = backend.path.read_bytes()
                federation_service.recover_running_jobs(backend)
                self.assertEqual(save.call_count, 1)
            actual = backend.load()
            self.assertEqual(actual['jobs']['FO-' + 'a' * 32]['state'], 'interrupted')
            self.assertEqual(actual['jobs']['FO-' + 'b' * 32], saved['jobs']['FO-' + 'b' * 32])
            self.assertEqual(actual['instance_id'], saved['instance_id'])
            self.assertEqual(backend.key.read_bytes(), key)
            self.assertEqual(backend.path.read_bytes(), first)

    def test_startup_readiness_failure_does_not_bind_transport_or_modify_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'fixture.example'})
            federation.setup(root, {'domain': 'fixture.example'})
            backend = federation.Backend(root, admission=False)
            before = backend.path.read_bytes()
            with (
                patch.object(federation, 'backend', return_value=backend),
                patch.object(federation_service.lifecycle, 'configure'),
                patch.object(
                    backend,
                    'ready',
                    side_effect=federation.protocol.ProtocolError('installation_pending'),
                ),
                self.assertRaisesRegex(federation.protocol.ProtocolError, 'installation_pending'),
            ):
                federation_service.serve(root)
            self.assertFalse((root / 'run/federation/control.sock').exists())
            self.assertEqual(backend.path.read_bytes(), before)

    def test_invalid_startup_jobs_never_execute_and_diagnostics_exclude_state_values(self):
        class Listener:
            def __init__(self, path, handler):
                pass

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'fixture.example'})
            federation.setup(root, {'domain': 'fixture.example'})
            backend = federation.Backend(root, admission=False)
            saved = backend.load()
            saved['jobs'] = {'FO-' + 'a' * 32: {'state': 'PRIVATE_FIXTURE_STATE'}}
            backend.save(saved)
            before = backend.path.read_bytes()
            output = io.StringIO()
            with (
                patch.object(federation, 'backend', return_value=backend),
                patch.object(federation_runtime, 'validate'),
                patch.object(federation_service.lifecycle, 'configure'),
                patch.object(federation_service.socketserver, 'UnixStreamServer', Listener),
                patch.object(federation_service.threading, 'Thread') as threads,
                patch.object(federation_service.os, 'chown'),
                patch.object(federation_service.os, 'chmod'),
                patch.object(federation_service, 'job_step') as execute,
                contextlib.redirect_stdout(output),
            ):
                federation_service.serve(root)
                jobs = threads.call_args_list[0].kwargs['target']
                with (
                    patch.object(
                        federation_service.time, 'sleep', side_effect=[None, None, StopIteration]
                    ),
                    self.assertRaises(StopIteration),
                ):
                    jobs()
                execute.assert_not_called()
            self.assertEqual(backend.path.read_bytes(), before)
            self.assertNotIn('PRIVATE_FIXTURE_STATE', output.getvalue())
            self.assertEqual(output.getvalue().count('startup recovery pending'), 1)

    def test_real_controller_binds_under_writer_lock_then_recovers_before_running_queue(self):
        if not hasattr(socket, 'SO_PEERCRED'):
            self.skipTest('Linux Unix peer credentials required')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            actor = '@matrix-admin:fixture.example'
            state_store.write_json(root / 'installation.json', {'domain': 'fixture.example'})
            federation.setup(root, {'domain': 'fixture.example'})
            backend = federation.Backend(root, admin=lambda value: value == actor, admission=False)
            saved = backend.load()
            saved['jobs'] = {
                'FO-' + 'a' * 32: {
                    'state': 'running',
                    'action': 'api-disable',
                    'argument': None,
                    'actor': actor,
                    'event_id': '$old-running',
                },
                'FO-' + 'b' * 32: {
                    'state': 'queued',
                    'action': 'api-enable',
                    'argument': None,
                    'actor': actor,
                    'event_id': '$queued',
                },
            }
            backend.save(saved)
            before = backend.path.read_bytes()
            key = backend.key.read_bytes()
            context = multiprocessing.get_context('fork')
            ready, locked = context.Pipe()

            def hold_writer():
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                with state_store.operation_guard(root):
                    locked.send('locked')
                    locked.recv()

            def controller():
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                real_call = federation_service.bot_call

                def bot_uid(backend, request, uid):
                    # Only OS credential assignment is simulated for a normal
                    # non-root test user. Actual dispatcher/actor authorization,
                    # operation guards and both Unix listeners remain real.
                    if uid != os.geteuid():
                        raise AssertionError('Unexpected fixture peer')
                    return real_call(backend, request, 10003)

                with (
                    patch.object(federation, 'backend', return_value=backend),
                    patch.object(federation_runtime, 'validate'),
                    patch.object(federation_service.lifecycle, 'configure'),
                    patch.object(federation_service.os, 'chown'),
                    patch.object(federation_service, 'bot_call', side_effect=bot_uid),
                    patch.object(backend, 'maintain'),
                ):
                    federation_service.serve(root)

            owner = context.Process(target=hold_writer)
            server = context.Process(target=controller)

            def rpc(action, sender=actor):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(str(root / 'run/federation/control.sock'))
                    client.sendall(json.dumps({'action': action, 'actor': sender}).encode() + b'\n')
                    return json.loads(client.recv(32768))

            def request(method, route, body=None):
                client = http.client.HTTPConnection('fixture', timeout=2)
                client.sock = socket.socket(socket.AF_UNIX)
                client.sock.settimeout(2)
                client.sock.connect(str(root / 'run/federation/http.sock'))
                try:
                    client.request(method, route, body=body)
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()

            try:
                owner.start()
                self.assertTrue(ready.poll(5), 'fixture writer did not acquire lock')
                self.assertEqual(ready.recv(), 'locked')
                server.start()
                for _ in range(250):
                    try:
                        response = rpc('authorize')
                        break
                    except (ConnectionRefusedError, FileNotFoundError):
                        pass
                    if not server.is_alive():
                        self.fail('controller exited while the installer owns its lock')
                    time.sleep(0.02)
                else:
                    self.fail('controller did not bind while the installer owns its lock')
                self.assertEqual(
                    response, {'ok': False, 'code': 'busy_pending_or_operation_failed'}
                )
                status, document = request('GET', '/.well-known/misbot')
                self.assertEqual(status, 200)
                self.assertEqual(document['instance_id'], saved['instance_id'])
                self.assertEqual(document['server_name'], saved['server_name'])
                self.assertNotIn('jobs', document)
                self.assertNotIn('private_key', document)
                # Malformed input is rejected offline, without the writer guard.
                self.assertEqual(
                    request('POST', '/_misbot/v1/exchange', b'{}'),
                    (400, {'code': 'invalid_request'}),
                )
                time.sleep(1.1)  # At least one automatic recovery attempt fails busy.
                self.assertEqual(backend.path.read_bytes(), before)
                self.assertTrue(owner.is_alive())
                self.assertTrue(server.is_alive())

                ready.send('release')
                owner.join(5)
                self.assertFalse(owner.is_alive())
                for _ in range(250):
                    actual = backend.load()
                    if actual['jobs']['FO-' + 'b' * 32]['state'] == 'complete':
                        break
                    time.sleep(0.02)
                else:
                    self.fail('queued job did not execute after recovery acquired the guard')
                self.assertEqual(actual['jobs']['FO-' + 'a' * 32]['state'], 'interrupted')
                queued_event = hashlib.sha256(b'$queued').hexdigest()
                self.assertEqual(set(actual['events']), {queued_event})
                self.assertTrue(actual['api_enabled'])
                self.assertEqual(actual['instance_id'], saved['instance_id'])
                self.assertEqual(backend.key.read_bytes(), key)
                for sender, expected in (
                    (actor, {'ok': True, 'result': {'authorized': True}}),
                    ('@other:fixture.example', {'ok': False, 'code': 'admin_unconfirmed'}),
                ):
                    for _ in range(100):
                        response = rpc('authorize', sender)
                        if response.get('code') != 'busy_pending_or_operation_failed':
                            break
                        time.sleep(0.02)
                    self.assertEqual(response, expected)
                time.sleep(1.1)
                self.assertEqual(set(backend.load()['events']), {queued_event})
            finally:
                for process in (server, owner):
                    if process.pid is not None:
                        process.terminate()
                        process.join(5)
                        if process.is_alive():
                            process.kill()
                            process.join(5)
                ready.close()
                locked.close()

    def test_rpc_operations_snapshot_stays_authorized_while_controller_mutex_is_busy(self):
        handlers = []

        class Listener:
            def __init__(self, path, handler):
                handlers.append(handler)

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'fixture.example'})
            federation.setup(root, {'domain': 'fixture.example'})
            backend = federation.Backend(
                root,
                admin=lambda actor: actor == '@matrix-admin:fixture.example',
                admission=False,
            )
            real_lock = __import__('threading').Lock
            busy = real_lock()
            busy.acquire()
            created_locks = 0

            def controller_lock():
                nonlocal created_locks
                created_locks += 1
                # Rate owns the first lock; work owns the second. Unrelated
                # locks (including subprocess waitpid) must remain ordinary.
                return busy if created_locks == 2 else real_lock()

            with (
                patch.object(federation, 'backend', return_value=backend),
                patch.object(federation_runtime, 'validate'),
                patch.object(federation_service.lifecycle, 'configure'),
                patch.object(federation_service.socketserver, 'UnixStreamServer', Listener),
                patch.object(federation_service.threading, 'Thread'),
                patch.object(federation_service.threading, 'Lock', side_effect=controller_lock),
                patch.object(federation_service.os, 'chown'),
                patch.object(federation_service.os, 'chmod'),
            ):
                federation_service.serve(root)
            for action, actor, uid, expected in (
                (
                    'operations',
                    '@matrix-admin:fixture.example',
                    10003,
                    {'ok': True, 'result': {'operations': []}},
                ),
                (
                    'operations',
                    '@other:fixture.example',
                    10003,
                    {'ok': False, 'code': 'admin_unconfirmed'},
                ),
                (
                    'operations',
                    '@matrix-admin:fixture.example',
                    10001,
                    {'ok': False, 'code': 'local_authorization_required'},
                ),
                (
                    'requests',
                    '@matrix-admin:fixture.example',
                    10003,
                    {'ok': False, 'code': 'busy_pending_or_operation_failed'},
                ),
            ):
                with self.subTest(action=action, actor=actor, uid=uid):
                    rpc = handlers[1].__new__(handlers[1])
                    rpc.connection = Mock()
                    rpc.connection.getsockopt.return_value = struct.pack('3i', 1, uid, 1)
                    rpc.rfile = io.BytesIO(
                        json.dumps(
                            {'action': action, 'actor': actor, 'event_id': '$fixture'}
                        ).encode()
                        + b'\n'
                    )
                    rpc.wfile = io.BytesIO()
                    rpc.handle()
                    self.assertEqual(json.loads(rpc.wfile.getvalue()), expected)
            busy.release()

    def test_panel_failure_cannot_block_automatic_invitation_expiry(self):
        class Listener:
            def __init__(self, path, handler):
                pass

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(
                root / 'installation.json',
                {
                    'domain': 'fixture.example',
                    'federation_provisioned': True,
                },
            )
            federation.setup(root, {'domain': 'fixture.example'})
            backend = federation.Backend(root, admin=lambda actor: True, admission=False)
            saved = backend.load()
            rid = 'MB-' + 'a' * 32
            saved['api_enabled'] = False
            saved['requests'][rid] = {
                'id': rid,
                'server': 'peer.example',
                'direction': 'incoming',
                'state': 'pending',
                'expires_at': int(time.time()) - 1,
                'local_approval': False,
                'remote_approval': True,
                'local_applied': False,
                'remote_applied': False,
            }
            backend.save(saved)
            with (
                patch.object(federation, 'backend', return_value=backend),
                patch.object(federation_runtime, 'validate'),
                patch.object(federation_service.lifecycle, 'configure'),
                patch.object(federation_service.socketserver, 'UnixStreamServer', Listener),
                patch.object(federation_service.threading, 'Thread') as threads,
                patch.object(federation_service.os, 'chown'),
                patch.object(federation_service.os, 'chmod'),
                patch.object(federation_service, 'job_step'),
                patch.object(federation_service.admin_panel, 'Panel') as panel,
                patch('release_update.recover_job'),
                patch('release_update.result_notice'),
            ):
                panel.return_value.maintenance.side_effect = RuntimeError('Statistics unavailable')
                federation_service.serve(root)
                jobs = threads.call_args_list[0].kwargs['target']
                with (
                    patch.object(
                        federation_service.time, 'sleep', side_effect=[None, StopIteration]
                    ),
                    patch.object(federation_service.time, 'monotonic', return_value=61),
                    self.assertRaises(StopIteration),
                ):
                    jobs()
                panel.return_value.maintenance.assert_called_once()
            self.assertEqual(backend.load()['requests'][rid]['state'], 'expired')
            self.assertTrue(backend.load()['requests'][rid]['delivery_pending'])

    def test_disabled_public_routes_use_persistent_setting_without_network(self):
        handlers = []

        class Listener:
            def __init__(self, path, handler):
                handlers.append(handler)

            def serve_forever(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'fixture.example'})
            federation.setup(root, {'domain': 'fixture.example'})
            backend = federation.Backend(root, admin=lambda actor: True, admission=False)
            with (
                patch.object(federation, 'backend', return_value=backend),
                patch.object(federation_runtime, 'validate'),
                patch.object(federation_service.lifecycle, 'configure'),
                patch.object(federation_service.socketserver, 'UnixStreamServer', Listener),
                patch.object(federation_service.threading, 'Thread'),
                patch.object(federation_service.os, 'chown'),
                patch.object(federation_service.os, 'chmod'),
            ):
                federation_service.serve(root)
            http = handlers[0].__new__(handlers[0])
            results = []
            http.reply = lambda status, body: results.append((status, body))
            http.path = '/.well-known/misbot'
            http.do_GET()
            self.assertEqual(results[-1][0], 200)
            backend.operate('api-disable')
            http.do_GET()
            self.assertEqual(results[-1], (503, {'code': 'api_disabled'}))
            http.path = '/_misbot/v1/exchange'
            http.headers = Message()
            http.headers['Content-Length'] = '2'
            http.rfile = io.BytesIO(b'{}')
            http.do_POST()
            self.assertEqual(results[-1], (503, {'code': 'api_disabled'}))
            backend.operate('api-enable')
            http.path = '/.well-known/misbot'
            http.do_GET()
            self.assertEqual(results[-1][0], 200)

    def test_proxy_public_mount_survives_controller_restarts(self):
        if not hasattr(socket, 'SO_PEERCRED') or os.geteuid() != 0:
            self.skipTest('isolated Linux root controller required')
        domain = 'fixture-' + uuid.uuid4().hex + '.example'
        public = Path('/run/misbot-federation-' + hashlib.sha256(domain.encode()).hexdigest()[:16])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            metadata = {
                'domain': domain,
                'backend': 'existing-compose',
                'existing': {'proxy': {'format': 1}},
            }
            state_store.write_json(root / 'installation.json', metadata)
            federation.setup(root, {'domain': domain})
            original = federation.Backend

            class IsolatedBackend(original):
                def guard(self):
                    return contextlib.nullcontext()

            def worker():
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                with (
                    patch.object(
                        federation,
                        'backend',
                        side_effect=lambda p: IsolatedBackend(p, admission=False),
                    ),
                    patch.object(federation_runtime, 'validate'),
                    patch.object(federation_service.lifecycle, 'configure'),
                ):
                    federation_service.serve(root)

            inode = None
            try:
                for _ in range(2):
                    process = multiprocessing.get_context('fork').Process(target=worker)
                    process.start()
                    try:
                        for _ in range(100):
                            if (public / 'http.sock').exists() and (
                                root / 'run/federation/control.sock'
                            ).exists():
                                client = socket.socket(socket.AF_UNIX)
                                client.settimeout(0.1)
                                try:
                                    client.connect(str(public / 'http.sock'))
                                    break
                                except (ConnectionRefusedError, FileNotFoundError):
                                    pass
                                finally:
                                    client.close()
                            if not process.is_alive():
                                self.fail('isolated service exited')
                            time.sleep(0.02)
                        else:
                            self.fail('isolated sockets missing')
                        info = public.stat()
                        self.assertEqual(
                            (info.st_uid, info.st_gid, info.st_mode & 0o777), (0, 10001, 0o750)
                        )
                        if inode is None:
                            inode = info.st_ino
                        self.assertEqual(info.st_ino, inode)
                    finally:
                        process.terminate()
                        process.join(5)
                        if process.is_alive():
                            process.kill()
                            process.join(5)
            finally:
                if public.exists():
                    (public / 'http.sock').unlink(missing_ok=True)
                    public.rmdir()

    def test_public_limits_and_no_local_administrative_dispatch(self):
        if not hasattr(socket, 'SO_PEERCRED') or os.geteuid() != 0:
            self.skipTest('isolated Linux root controller required')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'fixture.example'})
            federation.setup(root, {'domain': 'fixture.example'})
            original = federation.Backend

            def worker():
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                with (
                    patch.object(
                        federation, 'Backend', side_effect=lambda p: original(p, admission=False)
                    ),
                    patch.object(federation_runtime, 'validate'),
                    patch.object(federation_service.lifecycle, 'configure'),
                ):
                    federation_service.serve(root)

            process = multiprocessing.get_context('fork').Process(target=worker)
            process.start()
            try:
                path = root / 'run/federation/http.sock'
                for _ in range(100):
                    if path.exists() and (root / 'run/federation/control.sock').exists():
                        break
                    if not process.is_alive():
                        self.fail('isolated service exited')
                    time.sleep(0.02)
                else:
                    self.fail('isolated sockets missing')

                def request(method, route, body=None, headers=None):
                    client = http.client.HTTPConnection('fixture', timeout=2)
                    client.sock = socket.socket(socket.AF_UNIX)
                    client.sock.settimeout(2)
                    client.sock.connect(str(path))
                    try:
                        client.request(method, route, body=body, headers=headers or {})
                        response = client.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        client.close()

                status, doc = request('GET', '/.well-known/misbot')
                self.assertEqual(status, 200)
                self.assertEqual(doc['server_name'], 'fixture.example')
                self.assertNotIn('requests', doc)
                self.assertNotIn('private_key', doc)
                self.assertEqual(request('POST', '/_misbot/v1/admin', b'{}')[0], 404)
                status, result = request(
                    'POST', '/_misbot/v1/exchange', b'{}', {'Content-Length': '32769'}
                )
                self.assertEqual((status, result['code']), (400, 'request_too_large'))
                self.assertEqual(
                    request('POST', '/_misbot/v1/exchange', b'not-json')[1]['code'],
                    'invalid_request',
                )
                rpc = socket.socket(socket.AF_UNIX)
                rpc.settimeout(2)
                rpc.connect(str(root / 'run/federation/control.sock'))
                try:
                    rpc.sendall(
                        b'{"action":"add","actor":"root","event_id":"$fictional","argument":"other.example"}\n'
                    )
                    reply = json.loads(rpc.recv(32768))
                    self.assertFalse(reply['ok'])
                    self.assertEqual(reply['code'], 'local_authorization_required')
                finally:
                    rpc.close()
                statuses = [request('GET', '/.well-known/misbot')[0] for _ in range(31)]
                self.assertIn(429, statuses)
            finally:
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)


if __name__ == '__main__':
    unittest.main()
