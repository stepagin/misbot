"""Lifecycle DNS dispatch uses the selected private MAS configuration."""

import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import state_store


class LifecycleDNSTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.address = '8.8.8.8'
        self.managed = {
            'chat.example.org',
            'matrix.chat.example.org',
            'admin.chat.example.org',
            'web.chat.example.org',
            'rtc.chat.example.org',
            'turn.chat.example.org',
        }
        self.queries = []
        self.records = {}
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'chat.example.org', 'public_ip': self.address},
        )
        self.settings()
        root_patch = patch.object(lifecycle, 'ROOT', self.root, create=True)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        resolver_patch = patch.object(lifecycle.preflight, 'resolve', side_effect=self.resolve)
        resolver_patch.start()
        self.addCleanup(resolver_patch.stop)
        emit_patch = patch.object(lifecycle, 'emit')
        self.emit = emit_patch.start()
        self.addCleanup(emit_patch.stop)

    def settings(self, **values):
        model = {'DOMAIN': 'chat.example.org', 'PUBLIC_IP': self.address, **values}
        state_store.write_text(
            self.root / 'settings.env',
            ''.join(key + '=' + value + '\n' for key, value in model.items()),
        )

    def resolve(self, host, family, strict=False):
        self.queries.append((host, family, strict))
        return self.records.get(
            (host, family), {self.address} if family == socket.AF_INET else set()
        )

    def passed(self, hosts):
        lifecycle.dns_check()
        self.emit.assert_called_once_with('dns', hosts=len(hosts), passed=True)
        self.assertEqual({host for host, _, _ in self.queries}, hosts)
        for host in hosts:
            self.assertEqual(
                {family for queried, family, _ in self.queries if queried == host},
                {socket.AF_INET, socket.AF_INET6},
            )

    def failed(self, message):
        with self.assertRaisesRegex(RuntimeError, message):
            lifecycle.dns_check()
        self.emit.assert_called_once_with('dns', passed=False, failure_code='dns_validation')

    def test_default_local_mas_retains_legacy_seven_host_check(self):
        self.passed(self.managed | {'mas.chat.example.org'})
        self.assertTrue(all(not strict for _, _, strict in self.queries))

    def test_custom_local_mas_checks_configured_name_instead_of_default(self):
        self.settings(MAS_DOMAIN='auth.example.net')
        self.passed(self.managed | {'auth.example.net'})

    def test_local_mas_sharing_matrix_name_counts_distinct_hosts(self):
        self.settings(MAS_DOMAIN='matrix.chat.example.org')
        self.passed(self.managed)

    def test_external_mas_checks_own_address_separately_from_vps(self):
        self.settings(MAS_MODE='external', MAS_DOMAIN='auth.example.net', MAS_PUBLIC_IP='1.1.1.1')
        self.records[('auth.example.net', socket.AF_INET)] = {'1.1.1.1'}
        self.passed(self.managed | {'auth.example.net'})
        self.assertTrue(
            all(strict == (host == 'auth.example.net') for host, _, strict in self.queries)
        )

    def test_external_mas_without_optional_ip_accepts_own_ipv6_only_dns(self):
        self.settings(MAS_MODE='external', MAS_DOMAIN='auth.example.net')
        self.records[('auth.example.net', socket.AF_INET)] = set()
        self.records[('auth.example.net', socket.AF_INET6)] = {'2001:db8::1'}
        self.passed(self.managed | {'auth.example.net'})

    def test_external_mas_wrong_explicit_address_cannot_pass(self):
        self.settings(MAS_MODE='external', MAS_DOMAIN='auth.example.net', MAS_PUBLIC_IP='1.1.1.1')
        self.failed('Внешний MAS: A-запись auth.example.net')

    def test_external_mas_without_records_cannot_pass_even_without_optional_ip(self):
        self.settings(MAS_MODE='external', MAS_DOMAIN='auth.example.net')
        self.records[('auth.example.net', socket.AF_INET)] = set()
        self.records[('auth.example.net', socket.AF_INET6)] = set()
        self.failed('Внешний MAS: настройте A или AAAA')

    def test_managed_vps_names_still_reject_ipv6_with_external_mas(self):
        self.settings(MAS_MODE='external', MAS_DOMAIN='auth.example.net')
        self.records[('chat.example.org', socket.AF_INET6)] = {'2001:db8::1'}
        self.failed('unexpected AAAA')
        self.assertNotIn('auth.example.net', {host for host, _, _ in self.queries})

    def test_selected_private_settings_are_not_replaced_with_metadata_defaults(self):
        path = self.root / 'settings.env'
        path.chmod(0o644)
        before = json.loads((self.root / 'installation.json').read_text())
        with self.assertRaisesRegex(RuntimeError, 'mode 0600'):
            lifecycle.dns_check()
        self.emit.assert_not_called()
        self.assertEqual(self.queries, [])
        self.assertEqual(json.loads((self.root / 'installation.json').read_text()), before)


if __name__ == '__main__':
    unittest.main()
