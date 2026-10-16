"""RTC rendering and optional pinned-image contract; no public ports or real identities."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

BUNDLE = Path(__file__).resolve().parents[1]


class RTCContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='misbot-rtc-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'stack'

    def generate(self, *options):
        result = subprocess.run(
            [sys.executable, str(BUNDLE / 'generate.py'), '--directory', str(self.root), *options],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def registration(self):
        return yaml.safe_load((self.root / 'config/rtc-appservice.yaml').read_text())

    def assert_rtc_contract(self):
        registration = self.registration()
        self.assertEqual(
            registration['io.element.msc4502.scopes'],
            ['urn:matrix:client:io.element.msc4502:rooms:is_joined'],
        )
        self.assertEqual(registration['io.element.msc4512.proxy_prefix'], 'rtc/livekit')
        self.assertEqual(registration['io.element.msc4512.proxy_url'], 'http://jwt:8080')
        livekit = yaml.safe_load((self.root / 'config/livekit.yaml').read_text())
        self.assertIs(livekit['room']['auto_create'], False)

    def test_regeneration_repairs_old_rtc_contract_without_rotating_secrets(self):
        self.generate('--domain', 'rtc-test.example.org', '--initialize-env')
        secrets = self.root / 'secrets'
        before = {path.name: path.read_bytes() for path in secrets.iterdir() if path.is_file()}
        (self.root / 'config/mas-defaults.yaml').write_text('{}\n')
        # Only bypass host ownership changes. Run the production rendering path,
        # reading the persisted credentials and never invoking Docker or DNS.
        worker = """
import os, runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import configuration, state_store
state_store.require_root = lambda *args: None
os.chown = lambda *args: None
bundle, root = sys.argv[1:]
configuration.patch(Path(root), {'PUBLIC_IP': '8.8.8.8'})
sys.argv = ['generate.py', '--directory', root]
runpy.run_path(str(Path(bundle) / 'generate.py'), run_name='__main__')
"""
        registration_path = self.root / 'config/rtc-appservice.yaml'
        registration_path.write_text(
            yaml.safe_dump(
                {
                    'as_token': before['rtc_as_token'].decode().strip(),
                    'hs_token': before['rtc_hs_token'].decode().strip(),
                    'scopes': ['urn:matrix:client:rooms:is_joined'],
                    'io.element.msc4502.scope': [
                        'urn:matrix:client:io.element.msc4502:rooms:is_joined'
                    ],
                    'proxy_prefix': 'rtc/livekit',
                    'proxy_url': 'http://jwt:8080',
                }
            )
        )
        (self.root / 'config/livekit.yaml').write_text('room: {auto_create: true}\n')
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, '-c', worker, str(BUNDLE), str(self.root)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_rtc_contract()
            for name, content in before.items():
                self.assertEqual((secrets / name).read_bytes(), content, name)
            registration = self.registration()
            self.assertEqual(registration['as_token'], before['rtc_as_token'].decode().strip())
            self.assertEqual(registration['hs_token'], before['rtc_hs_token'].decode().strip())
            livekit = yaml.safe_load((self.root / 'config/livekit.yaml').read_text())
            self.assertEqual(
                livekit['keys']['matrix-livekit'], before['livekit_secret'].decode().strip()
            )

    @unittest.skipUnless(
        os.environ.get('MISBOT_TEST_PINNED_RTC') == '1',
        'set MISBOT_TEST_PINNED_RTC=1 on a disposable Docker host with the pinned Synapse image',
    )
    def test_pinned_synapse_loader_registers_proxy_and_membership_scope(self):
        self.generate('--domain', 'rtc-test.example.org', '--preview')
        self.assert_rtc_contract()
        image = json.loads((BUNDLE / 'versions.json').read_text())['digests']['synapse']
        # HomeServerConfig alone never loads the registration. Exercise Synapse's
        # real loader and the scope predicate used by the membership endpoint.
        probe = """
import json, sys
from synapse.appservice import Scopes
from synapse.config.appservice import _load_appservice
service = _load_appservice('rtc-test.example.org', json.load(sys.stdin), 'generated-rtc')
assert service.proxy_prefix == 'rtc/livekit'
assert service.proxy_url == 'http://jwt:8080'
assert service.has_scope(Scopes.QUERY_ROOM_MEMBERSHIP)
"""
        result = subprocess.run(
            [
                'docker',
                'run',
                '--rm',
                '-i',
                '--pull=never',
                '--network=none',
                '--read-only',
                '--cap-drop=ALL',
                '--security-opt=no-new-privileges',
                '--entrypoint=python',
                image,
                '-c',
                probe,
            ],
            input=json.dumps(self.registration()),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
