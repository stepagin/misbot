"""Storage and transport boundaries without external services or socket binding."""

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tarfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import federation
import federation_protocol as protocol
import federation_runtime
import federation_store
import state_store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data/federation').mkdir(parents=True, mode=0o700)
        state_store.write_json(self.root / 'installation.json', {'domain': 'a.example'})
        self.backend = federation.Backend(self.root, admin=lambda actor: True, admission=False)
        self.initial = {
            'format': 1,
            'instance_id': 'a' * 32,
            'server_name': 'a.example',
            'requests': {},
            'peers': {},
            'events': {},
            'nonces': {},
        }
        self.backend.save(self.initial)

    def test_large_legacy_history_survives_setup_runtime_and_replayed_mutation(self):
        data = copy.deepcopy(self.initial)
        data['events'] = {
            hashlib.sha256(str(index).encode()).hexdigest(): {'action': 'api-status', 'time': 1}
            for index in range(1100)
        }
        # Legacy pretty-printed format-1 files require no migration or pruning.
        state_store.write_json(self.backend.path, data)
        self.assertGreater(self.backend.path.stat().st_size, 131072)
        state_store.write_text(self.backend.key, 'FICTIONAL KEY; setup only checks presence')
        self.assertEqual(federation.setup(self.root, {'domain': 'a.example'}), data)
        federation_runtime.validate(self.root, {'domain': 'a.example', 'federation_seeded': True})
        self.assertEqual(self.backend.load(), data)
        state_store.write_json(
            self.root / 'installation.json',
            {
                'domain': 'a.example',
                'federation_seeded': True,
            },
        )
        archive = self.root / 'backup.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for relative in (
                'installation.json',
                'data/federation/state.json',
                'data/federation/identity.pem',
            ):
                tar.add(self.root / relative, arcname=relative)
        federation_runtime.verify_archive(archive)
        with patch.object(federation_store, 'MAX_BYTES', 131072):
            with self.assertRaisesRegex(protocol.ProtocolError, 'federation_state_capacity'):
                federation_runtime.verify_archive(archive)
        self.assertEqual(
            self.backend.operate('api-disable', event_id='0'), {'status': 'already_processed'}
        )
        self.assertTrue(self.backend.operate('api-status')['enabled'])
        for index in range(1000):
            self.backend.operate('api-status', event_id='new-' + str(index))
        self.assertEqual(self.backend.load(), data)

    def test_capacity_retains_readability_and_local_recovery_without_evicting_events(self):
        data = copy.deepcopy(self.initial)
        data['events'] = {str(index): {} for index in range(100000)}
        self.backend.save(data)
        with self.assertRaisesRegex(protocol.ProtocolError, 'federation_state_capacity'):
            self.backend.save({**data, 'events': {**data['events'], 'overflow': {}}})
        self.assertEqual(self.backend.load(), data)
        with self.assertRaisesRegex(protocol.ProtocolError, 'event_capacity'):
            self.backend.operate('api-disable', event_id='new')
        self.assertEqual(self.backend.operate('api-status', event_id='read'), {'enabled': True})
        self.assertEqual(
            self.backend.operate('operations'), {'recovery_available': False, 'operations': []}
        )
        self.backend.operate('api-disable')
        with patch.object(self.backend, 'recover_policy', return_value={'status': 'recovered'}):
            self.assertEqual(self.backend.operate('recover'), {'status': 'recovered'})
        self.assertEqual(self.backend.load()['events'], data['events'])

    def test_byte_count_boundary_and_invalid_publication_keep_previous_checkpoint(self):
        data = {**self.initial, 'note': 'Пример'}
        limit = len((json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())
        with patch.object(federation_store, 'MAX_BYTES', limit):
            self.backend.save(data)
            before = self.backend.path.read_bytes()
            self.assertEqual(self.backend.load(), data)
            for invalid, code in (
                ({**data, 'note': 'Пример!'}, 'federation_state_capacity'),
                ({**data, 'events': []}, 'invalid_federation_state'),
            ):
                with self.assertRaisesRegex(protocol.ProtocolError, code):
                    self.backend.save(invalid)
                self.assertEqual(self.backend.path.read_bytes(), before)
                self.assertEqual(self.backend.load(), data)
        with patch.object(federation_store, 'MAX_BYTES', limit - 1):
            with self.assertRaisesRegex(protocol.ProtocolError, 'federation_state_capacity'):
                self.backend.load()
        self.assertEqual(self.backend.path.read_bytes(), before)

    def test_read_keeps_open_snapshot_when_writer_replaces_path(self):
        path = self.backend.path
        updated = {**self.initial, 'events': {'published': {'action': 'api-disable', 'time': 1}}}
        replacement = path.with_name('replacement.json')
        state_store.write_json(replacement, updated)
        original_open = os.open

        def open_then_replace(opened_path, flags):
            fd = original_open(opened_path, flags)
            os.replace(replacement, path)
            self.assertEqual(os.fstat(fd).st_nlink, 0)
            return fd

        with patch.object(federation_store.os, 'open', side_effect=open_then_replace):
            self.assertEqual(federation_store.read(path), self.initial)
        self.assertEqual(federation_store.read(path), updated)

    def test_private_file_guards_preserved(self):
        path = self.backend.path
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaisesRegex(protocol.ProtocolError, 'unsafe_federation_state'):
            self.backend.save(self.initial)
        path.chmod(0o600)
        target = path.with_name('saved.json')
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(protocol.ProtocolError, 'unreadable_federation_state'):
            self.backend.load()
        path.unlink()
        os.link(target, path)
        with self.assertRaisesRegex(protocol.ProtocolError, 'unsafe_federation_state'):
            self.backend.load()
        self.assertEqual(target.read_bytes(), original)


class DeadlineTests(unittest.TestCase):
    def test_dns_total_deadline_and_bounded_timed_out_resolvers(self):
        release = threading.Event()
        finished = threading.Event()
        slots = threading.BoundedSemaphore(1)

        def lookup(*args, **kwargs):
            release.wait(2)
            finished.set()
            return [(2, 1, 6, '', ('8.8.8.8', 443))]

        with (
            patch.object(protocol, 'NETWORK_TIMEOUT', 0.05),
            patch.object(protocol, '_RESOLVERS', slots),
            patch.object(protocol.socket, 'getaddrinfo', side_effect=lookup),
        ):
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(protocol.ProtocolError, '^timeout$'):
                    protocol.https('a.example', protocol.DISCOVERY)
                self.assertLess(time.monotonic() - started, 1)
                with self.assertRaisesRegex(protocol.ProtocolError, '^dns_busy$'):
                    protocol.https('a.example', protocol.DISCOVERY)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
        self.assertTrue(slots.acquire(timeout=1))
        slots.release()

    def test_absolute_deadline_interrupts_headers_and_dripping_body_with_pinned_dns(self):
        for phase in ('headers', 'body'):
            interrupted = threading.Event()
            raw_socket = Mock()
            tls = Mock()
            tls.shutdown.side_effect = lambda how: interrupted.set()
            context = Mock()
            context.wrap_socket.return_value = tls
            connection = Mock()
            response = Mock(status=200)
            response.read.return_value = b'{}'
            connection.getresponse.return_value = response

            def slow_read(*args, **kwargs):
                # Simulate bytes arriving within each per-read timeout, until
                # the overall deadline shuts down the verified TLS socket.
                if not interrupted.wait(1):
                    self.fail('total deadline did not interrupt the socket')
                return response if phase == 'headers' else b'{}'

            if phase == 'headers':
                connection.getresponse.side_effect = slow_read
            else:
                response.read.side_effect = slow_read
            with (
                self.subTest(phase=phase),
                patch.object(protocol, 'NETWORK_TIMEOUT', 0.05),
                patch.object(
                    protocol.socket,
                    'getaddrinfo',
                    return_value=[(2, 1, 6, '', ('8.8.8.8', 443)), (2, 1, 6, '', ('1.1.1.1', 443))],
                ) as dns,
                patch.object(protocol.socket, 'socket', return_value=raw_socket),
                patch.object(protocol.ssl, 'create_default_context', return_value=context),
                patch.object(protocol.http.client, 'HTTPSConnection', return_value=connection),
            ):
                started = time.monotonic()
                with self.assertRaisesRegex(protocol.ProtocolError, '^timeout$'):
                    protocol.https('a.example', protocol.DISCOVERY)
                self.assertLess(time.monotonic() - started, 1)
                dns.assert_called_once()
                raw_socket.connect.assert_called_once_with(('8.8.8.8', 443))
                context.wrap_socket.assert_called_once_with(raw_socket, server_hostname='a.example')
                tls.close.assert_called()


if __name__ == '__main__':
    unittest.main()
