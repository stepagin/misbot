"""Portable public addresses preserve Matrix identity across generation and restore."""

import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import configuration_schema as schema
import homepage
import lifecycle
import preflight
import restore
import state_store


BUNDLE = Path(__file__).resolve().parents[1]
CUSTOM = {
    'DOMAIN': 'matrix.example.org',
    'PUBLIC_DOMAIN': 'example.org',
    'WEB_DOMAIN': 'chat.example.org',
    'MAS_DOMAIN': 'matrix.example.org',
    'SFU_DOMAIN': 'live.example.org',
}


class PublicEndpointsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'stack'

    def generate(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(BUNDLE / 'generate.py'), '--directory', str(self.root), *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def test_defaults_retain_original_seven_hosts_and_mas_api(self):
        model = schema.validate({'DOMAIN': 'example.org'})
        self.assertEqual(
            set(schema.public_hosts(model)),
            {
                'example.org',
                *[
                    name + '.example.org'
                    for name in ('matrix', 'mas', 'web', 'admin', 'rtc', 'turn')
                ],
            },
        )
        self.assertEqual(schema.mas_host(model), 'mas.example.org')
        self.assertEqual(schema.mas_api_base(model), 'http://mas:8080')
        self.assertEqual(homepage.links(model)['client'], 'https://web.example.org/')

    def test_dns_and_caa_check_resolved_hosts_without_double_matrix_prefix(self):
        model = schema.validate(CUSTOM)
        expected = {
            'example.org',
            'matrix.example.org',
            'chat.example.org',
            'admin.example.org',
            'rtc.example.org',
            'turn.example.org',
            'live.example.org',
        }
        dns, caa = set(), set()

        def resolve(host, family):
            dns.add(host)
            return {'203.0.113.20'} if family == socket.AF_INET else set()

        with (
            patch.object(preflight, 'resolve', side_effect=resolve),
            patch.object(
                preflight,
                'caa_query',
                side_effect=lambda host: caa.add(host) or [(0, 'issue', 'letsencrypt.org')],
            ),
        ):
            preflight.dns_check(model['DOMAIN'], '203.0.113.20', model=model)
            preflight.caa_check(model['DOMAIN'], model=model)
        self.assertEqual(dns, expected)
        self.assertEqual(caa, expected)

    def test_conflicting_origins_rejected_but_local_mas_can_share_matrix_or_site(self):
        for changes in (
            {'WEB_DOMAIN': 'matrix.example.org'},
            {'WEB_DOMAIN': 'example.org'},
            {'SFU_DOMAIN': 'turn.example.org'},
            {'SFU_DOMAIN': 'chat.example.org'},
            {'MAS_DOMAIN': 'chat.example.org'},
            {'DOMAIN': 'admin.example.org'},
        ):
            with self.subTest(changes=changes), self.assertRaises(schema.InvalidConfiguration):
                schema.validate({**CUSTOM, **changes})
        for mas in ('matrix.example.org', 'example.org', 'auth.example.net'):
            with self.subTest(mas=mas):
                schema.validate({**CUSTOM, 'MAS_DOMAIN': mas})

    def test_old_committed_metadata_accepts_legacy_addresses_and_refuses_new_mapping(self):
        metadata = {'domain': 'example.org', 'network_committed': True}
        configuration.assert_public_identity({'DOMAIN': 'example.org'}, metadata)
        configuration.assert_public_identity(
            {'DOMAIN': 'example.org', 'PUBLIC_DOMAIN': 'example.org'}, metadata
        )
        for changes in (
            {'PUBLIC_DOMAIN': 'other.example.org'},
            {'WEB_DOMAIN': 'chat.example.org'},
            {'SFU_DOMAIN': 'live.example.org'},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'immutable'):
                configuration.assert_public_identity({'DOMAIN': 'example.org', **changes}, metadata)

    def test_legacy_map_anchors_matrix_and_mas_to_saved_identity(self):
        metadata = {
            'domain': 'example.org',
            'network_committed': True,
            'mas_issuer': 'https://auth.example.net/',
        }
        original = {'DOMAIN': 'example.org', 'MAS_DOMAIN': 'auth.example.net'}
        configuration.assert_public_identity(original, metadata)
        for changes in ({'DOMAIN': 'changed.example.org'}, {'MAS_DOMAIN': 'changed.example.net'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'immutable'):
                configuration.assert_public_identity({**original, **changes}, metadata)

    def test_config_snapshot_restore_keeps_frozen_addresses_and_accepts_new_public_ip(self):
        self.root.mkdir(mode=0o700)
        metadata = {
            'domain': CUSTOM['DOMAIN'],
            'mas_issuer': 'https://matrix.example.org/',
            'config_format_version': 2,
        }
        state_store.write_json(self.root / 'installation.json', metadata)
        configuration.write_model(self.root, CUSTOM)
        metadata.update(
            public_endpoints=schema.public_endpoints(CUSTOM),
            storage_committed=True,
            restore_pending=True,
        )
        state_store.write_json(self.root / 'installation.json', metadata)
        configuration.patch(self.root, {'PUBLIC_IP': '8.8.8.8'})
        saved = self.root.parent / 'saved'
        configuration.snapshot(self.root, saved)
        configuration.patch(self.root, {'SITE_NAME': 'Changed'})
        configuration.restore_snapshot(self.root, saved)
        model = configuration.load(self.root, True)
        self.assertEqual(model['PUBLIC_IP'], '8.8.8.8')
        self.assertEqual(schema.public_endpoints(model), metadata['public_endpoints'])
        original = (self.root / 'settings/server.env').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'immutable'):
            configuration.patch(self.root, {'WEB_DOMAIN': 'other.example.org'})
        self.assertEqual((self.root / 'settings/server.env').read_bytes(), original)
        self.assertFalse((self.root / 'configuration-transition.json').exists())

    def test_restore_rejects_changed_frozen_host_before_publishing_target(self):
        archive = self.root.parent / 'backup.tar.gz'
        archive.write_bytes(b'fictional archive')
        snapshot = self.root.parent / 'snapshot'
        snapshot.mkdir()
        metadata = {
            'domain': CUSTOM['DOMAIN'],
            'mas_issuer': 'https://matrix.example.org/',
            'databases_initialized': True,
            'public_endpoints': schema.public_endpoints(CUSTOM),
        }

        def extract(path, destination, **kwargs):
            if path == archive:
                return
            state_store.write_json(destination / 'installation.json', metadata)
            state_store.write_text(
                destination / 'settings.env',
                configuration.serialize({**CUSTOM, 'WEB_DOMAIN': 'wrong.example.org'}),
            )

        with (
            patch.object(restore.backup_archive, 'extract', side_effect=extract),
            patch.object(restore.backup_archive, 'verify_snapshot', return_value=snapshot),
            patch.object(
                restore.backup_archive,
                'validate_authentication_boundary',
                return_value=('synapse', 'mas'),
            ),
            patch.object(restore.update, 'copy_sources') as copy,
            self.assertRaisesRegex(RuntimeError, 'immutable'),
        ):
            restore._restore_locked(
                archive, self.root, hashlib.sha256(archive.read_bytes()).hexdigest(), '8.8.8.8'
            )
        copy.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_generation_preserves_identity_sessions_and_routes_shared_mas_once(self):
        self.generate('--domain', CUSTOM['DOMAIN'], '--preview', '--initialize-env')
        configuration.patch(self.root, CUSTOM)
        self.generate('--preview')
        endpoints = schema.public_endpoints(CUSTOM)
        synapse = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
        livekit = yaml.safe_load((self.root / 'config/livekit.yaml').read_text())
        mas = yaml.safe_load((self.root / 'config/mas.yaml').read_text())
        discovery = json.loads((self.root / 'public/well-known-client.json').read_text())
        web = json.loads((self.root / 'public/web-config.json').read_text())
        policy = json.loads((self.root / 'data/referral-control/bot-policy.json').read_text())[
            'servers'
        ][CUSTOM['DOMAIN']]
        self.assertEqual(synapse['server_name'], CUSTOM['DOMAIN'])
        self.assertEqual(synapse['public_baseurl'], 'https://matrix.example.org/')
        self.assertEqual(synapse['signing_key_path'], '/data/matrix.example.org.signing.key')
        self.assertEqual(mas['http']['issuer'], 'https://matrix.example.org/')
        self.assertEqual(discovery['m.homeserver']['base_url'], 'https://matrix.example.org')
        self.assertEqual(
            web['default_server_config']['m.homeserver']['server_name'], CUSTOM['DOMAIN']
        )
        self.assertEqual(policy['site_url'], 'https://example.org/')
        self.assertEqual(policy['web_url'], 'https://chat.example.org')
        self.assertEqual(livekit['turn']['domain'], 'turn.example.org')
        self.assertIn('/turn.example.org/turn.example.org.crt', livekit['turn']['cert_file'])
        caddy = (self.root / 'config/Caddyfile').read_text()
        for host in schema.public_hosts(CUSTOM):
            self.assertEqual(caddy.count('\n' + host + ' {\n'), 1, host)
        self.assertNotIn('matrix.matrix.example.org', caddy)
        matrix_block = caddy.split('\nmatrix.example.org {\n')[1].split('\n}\n', 1)[0]
        self.assertIn('reverse_proxy synapse:8008', matrix_block)
        self.assertIn('reverse_proxy mas:8080', matrix_block)
        self.assertIn('handle /.well-known/misbot', matrix_block)
        self.assertIn('live.example.org {\n    reverse_proxy livekit:7880', caddy)
        self.assertIn(
            'https://chat.example.org/#/login', (self.root / 'public/site/index.html').read_text()
        )
        metadata = configuration.read_private_json(self.root / 'installation.json')
        self.assertEqual(metadata['public_endpoints'], endpoints)
        metadata.update(databases_initialized=True, network_committed=True, restore_pending=True)
        state_store.write_json(self.root / 'installation.json', metadata)
        secret_files = [path for path in (self.root / 'secrets').iterdir() if path.is_file()]
        fingerprints = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in secret_files
        }
        configuration.patch(self.root, {'PUBLIC_IP': '8.8.8.8'})
        self.generate('--preview')
        self.assertEqual(
            fingerprints,
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in secret_files},
        )
        with patch.object(lifecycle, 'ROOT', self.root, create=True):
            self.assertEqual(lifecycle.certificate_paths()[0].name, 'turn.example.org.crt')
        before = {
            name: (self.root / name).read_bytes()
            for name in ('config/Caddyfile', 'config/homeserver.yaml', 'installation.json')
        }
        path = self.root / 'settings/server.env'
        state_store.write_text(
            path,
            path.read_text().replace('WEB_DOMAIN=chat.example.org', 'WEB_DOMAIN=other.example.org'),
        )
        result = self.generate('--preview', success=False)
        self.assertIn('immutable', result.stderr)
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})


if __name__ == '__main__':
    unittest.main()
