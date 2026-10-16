"""Content changes refresh only their consumers, including post-provisioned bots."""

from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE))
import runtime_config
import state_store


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='misbot-runtime-config-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'stack'
        self.generate('--domain', 'refresh.example.org')

    def generate(self, *options, bundle=BUNDLE):
        result = subprocess.run(
            [
                sys.executable,
                str(bundle / 'generate.py'),
                '--directory',
                str(self.root),
                '--preview',
                *options,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def compose(self):
        return yaml.safe_load((self.root / 'compose.yaml').read_text())

    def labels(self, compose=None):
        return {
            role: service.get('labels', {}).get(runtime_config.LABEL)
            for role, service in (compose or self.compose())['services'].items()
        }

    def test_generated_labels_are_stable_and_ignore_live_data(self):
        before = self.labels()
        self.generate()
        self.assertEqual(self.labels(), before)
        for name in (
            'data/postgres/WAL',
            'data/caddy/certificate.pem',
            'data/referral-bot/state.json',
            'data/referral-control/data/queue.db',
            'data/federation-bot/store.sqlite',
            'public/site/index.html',
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('FICTIONAL_MUTABLE_STATE')
        compose = self.compose()
        self.assertFalse(runtime_config.apply(self.root, compose, preview=True))
        self.assertEqual(self.labels(compose), before)
        self.assertIsNone(before['admin'])

    def test_atomic_replacement_changes_only_consumers_and_rollback_restores_digest(self):
        path = self.root / 'config/rtc-appservice.yaml'
        previous = path.read_bytes()
        before = self.labels()
        with path.open('rb') as old_mount:
            state_store.write_text(path, previous.decode() + '\n# FICTIONAL_NEW_RTC_CONFIG\n')
            self.assertEqual(old_mount.read(), previous)
            compose = self.compose()
            runtime_config.apply(self.root, compose, preview=True)
            self.assertEqual(
                {role for role, value in self.labels(compose).items() if value != before[role]},
                {'synapse', 'jwt'},
            )
            state_store.write_text(path, previous.decode())
            runtime_config.apply(self.root, compose, preview=True)
            self.assertEqual(self.labels(compose), before)
        # Replacing an inode with identical bytes must not recreate services.
        runtime_config.apply(self.root, compose, preview=True)
        self.assertEqual(self.labels(compose), before)

    def test_regeneration_adopts_old_rtc_and_livekit_configuration(self):
        registration = self.root / 'config/rtc-appservice.yaml'
        saved = yaml.safe_load(registration.read_text())
        saved['scopes'] = saved.pop('io.element.msc4502.scopes')
        registration.write_text(yaml.safe_dump(saved))
        livekit = self.root / 'config/livekit.yaml'
        saved = yaml.safe_load(livekit.read_text())
        saved['room']['auto_create'] = True
        livekit.write_text(yaml.safe_dump(saved))
        compose = self.compose()
        runtime_config.apply(self.root, compose, preview=True)
        old = self.labels(compose)
        self.generate()
        self.assertEqual(
            {role for role, value in self.labels().items() if value != old[role]},
            {'synapse', 'jwt', 'livekit'},
        )
        fixed = self.labels()
        self.generate()
        self.assertEqual(self.labels(), fixed)

    def test_each_owned_file_changes_only_its_consumers(self):
        before = self.labels()
        cases = {
            'config/homeserver.yaml': {'synapse'},
            'config/synapse-log.yaml': {'synapse'},
            'config/mas.yaml': {'mas'},
            'config/mas-signing.pem': {'mas'},
            'config/livekit.yaml': {'livekit'},
            'config/Caddyfile': {'caddy'},
            'config/haproxy.cfg': {'tls-router'},
            'public/web-config.json': {'web'},
            'config/referral-bot.json': {'referral-bot'},
            'config/federation-bot.json': {'federation-bot'},
            'config/welcome-bot.json': {'federation-bot'},
            'data/referral-control/bot-config.json': {'referral-control'},
            'data/referral-control/bot-policy.json': {'referral-control'},
            'secrets/livekit_secret': {'jwt'},
            'secrets/db_admin': {'postgres'},
        }
        for name, consumers in cases.items():
            with self.subTest(name=name):
                path = self.root / name
                existed = path.exists()
                content = path.read_bytes() if existed else b''
                path.write_bytes(content + b'FICTIONAL_DIFFERENT_BYTES')
                compose = self.compose()
                runtime_config.apply(self.root, compose, preview=True)
                self.assertEqual(
                    {role for role, value in self.labels(compose).items() if value != before[role]},
                    consumers,
                )
                path.write_bytes(content) if existed else path.unlink()

    def test_s3_adapter_source_update_changes_synapse_without_image_change(self):
        # A copied source bundle models an update while the pinned image is unchanged.
        bundle = Path(self.temp.name) / 'bundle'
        shutil.copytree(BUNDLE, bundle, ignore=shutil.ignore_patterns('__pycache__'))
        import configuration

        configuration.patch(
            self.root,
            {
                'MEDIA_MODE': 's3',
                'S3_BUCKET': 'fictional-bucket',
                'S3_REGION': 'eu-central-1',
                'S3_ENDPOINT_URL': 'https://objects.example.org',
                'S3_ACCESS_KEY_ID': 'fictional-access',
                'S3_SECRET_ACCESS_KEY': 'fictional-secret',
                'S3_PREFIX': 'refresh/',
            },
        )
        self.generate(bundle=bundle)
        before = self.labels()
        image = self.compose()['services']['synapse']['image']
        source = bundle / 'misbot_s3_provider.py'
        source.write_text(source.read_text() + '\n# FICTIONAL_NEW_ADAPTER_REVISION\n')
        self.generate(bundle=bundle)
        self.assertEqual(
            {role for role, value in self.labels().items() if value != before[role]}, {'synapse'}
        )
        self.assertEqual(self.compose()['services']['synapse']['image'], image)
        self.assertEqual(
            (self.root / 'config/misbot_s3_provider.py').read_bytes(), source.read_bytes()
        )
        self.assertEqual((self.root / 'config/misbot_s3_provider.py').stat().st_mode & 0o777, 0o644)

    def test_missing_or_symlink_input_is_rejected_without_compose_write(self):
        path = self.root / 'config/referral-bot.json'
        before = (self.root / 'compose.yaml').read_bytes()
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            runtime_config.refresh(self.root, ('referral-bot',))
        path.symlink_to(self.root / 'config/federation-bot.json')
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            runtime_config.refresh(self.root, ('referral-bot',))
        self.assertEqual((self.root / 'compose.yaml').read_bytes(), before)

    def test_startup_fingerprints_provisioned_bot_files_before_compose_up(self):
        import lifecycle
        import referral
        import federation_runtime

        metadata = {'domain': 'refresh.example.org', 'databases_initialized': True}
        (self.root / 'data/synapse').mkdir(parents=True, exist_ok=True)
        (self.root / 'data/synapse/refresh.example.org.signing.key').write_text('FICTIONAL')
        (self.root / 'release.json').write_text('{"version":"1.2.0"}')
        (self.root / 'SHA256SUMS').write_text('FICTIONAL_SOURCE_MANIFEST')
        started = []

        def provision_referral(root, dc, state):
            state_store.write_json(root / 'config/referral-bot.json', {'provisioned': 'FICTIONAL'})
            state['referral_provisioned'] = True

        def provision_federation(root, dc, state):
            for name in ('federation', 'welcome'):
                state_store.write_json(
                    root / 'config' / (name + '-bot.json'), {'provisioned': name}
                )

        def dc(*args, **kwargs):
            if args[:2] == ('up', '-d'):
                for role in args[2:]:
                    if role in ('referral-bot', 'federation-bot'):
                        compose = self.compose()
                        self.assertEqual(
                            self.labels()[role],
                            runtime_config.fingerprint(self.root, compose, role),
                        )
                        started.append(role)
            return 'existing'

        with ExitStack() as stack:
            stack.enter_context(patch.object(lifecycle, 'ROOT', self.root, create=True))
            for name in (
                'up_admission_check',
                'generate',
                'dns_check',
                'upgrade_legacy_network',
                'create_admin',
                'wait',
                'check',
                'run',
                'certificate_watch',
                'emit',
            ):
                stack.enter_context(patch.object(lifecycle, name))
            stack.enter_context(patch.object(lifecycle, 'state', return_value=metadata))
            stack.enter_context(patch.object(lifecycle, 'external_database', return_value=False))
            stack.enter_context(patch.object(lifecycle, 'external_mas', return_value=False))
            stack.enter_context(patch.object(lifecycle, 'yaml_media_s3', return_value=False))
            stack.enter_context(
                patch.object(lifecycle, 'certificate_paths', return_value=(self.root, self.root))
            )
            stack.enter_context(patch.object(lifecycle, 'dc', side_effect=dc))
            stack.enter_context(patch.object(referral, 'provision', side_effect=provision_referral))
            stack.enter_context(patch.object(referral, 'note_ready'))
            stack.enter_context(
                patch.object(federation_runtime, 'provision', side_effect=provision_federation)
            )
            stack.enter_context(patch.object(federation_runtime, 'controller'))
            stack.enter_context(patch.object(federation_runtime, 'validate'))
            stack.enter_context(patch.object(lifecycle.vault_cache, 'sync_optional'))
            lifecycle.up()
        self.assertEqual(started, ['referral-bot', 'federation-bot'])
        before = (self.root / 'compose.yaml').read_bytes()
        runtime_config.refresh(self.root, ('referral-bot', 'federation-bot'))
        self.assertEqual((self.root / 'compose.yaml').read_bytes(), before)
