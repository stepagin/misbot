"""Local invariants; no Docker daemon, external credentials, DNS or server required."""

import hashlib
import html
import importlib.util
import json
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import yaml

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE))


class GeneratorTests(unittest.TestCase):
    def test_product_version_is_available_without_root_or_installation_state(self):
        for action in ('version', '--version'):
            result = subprocess.run(
                ['bash', str(BUNDLE / 'install.sh'), action], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                'Misbot ' + json.loads((BUNDLE / 'release.json').read_text())['version'],
            )

    def test_known_provider_pair_code_translates_to_safe_full_path_continuation(self):
        import lifecycle

        for provider in ('google', 'telegram'):
            result = subprocess.CompletedProcess(
                [],
                2,
                stdout=json.dumps(
                    {
                        'check': 'configuration_input',
                        'code': 'oauth_' + provider + '_pair_incomplete',
                    }
                ),
                stderr='FICTIONAL_PRIVATE_DRIVER_SECRET',
            )
            with (
                self.subTest(provider=provider),
                patch.object(lifecycle, 'ROOT', Path('/opt/fictional'), create=True),
                patch.object(lifecycle.subprocess, 'run', return_value=result),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, provider.upper() + '_CLIENT_SECRET'
                ) as error:
                    lifecycle.run(
                        ['python3', 'generate.py'], configuration_errors=lifecycle.GENERATION_ERRORS
                    )
                self.assertIn('sudo bash /opt/fictional/install.sh generate', str(error.exception))
                self.assertNotIn('FICTIONAL_PRIVATE_DRIVER_SECRET', str(error.exception))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='matrix-installer-test-')
        self.root = Path(self.temp.name) / 'stack'

    def tearDown(self):
        self.temp.cleanup()

    def test_build_keeps_all_shipped_document_links(self):
        package = Path(self.temp.name) / 'public.tar.gz'
        subprocess.run(
            [sys.executable, str(BUNDLE / 'build.py'), '--release', '--output', str(package)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with tarfile.open(package) as archive:
            names = set(archive.getnames())
            missing = []
            self.assertFalse(
                any(
                    '.server-access' in p
                    or p.endswith('.env')
                    or '/tests/' in p
                    or '/verification/' in p
                    or '/evidence/' in p
                    or '/ci/' in p
                    or '/.github/' in p
                    or '/node_modules/' in p
                    or 'SYNAPSE_INSTALLER_' in p
                    or p.endswith('live-test.js')
                    for p in names
                )
            )
            self.assertFalse(
                names.intersection(
                    {
                        'matrix-installer/pyproject.toml',
                        'matrix-installer/eslint.config.cjs',
                        'matrix-installer/.prettierrc.json',
                        'matrix-installer/package.json',
                        'matrix-installer/package-lock.json',
                        'matrix-installer/CONTRIBUTING.md',
                        'matrix-installer/development-licenses.md',
                    }
                )
            )
            self.assertEqual(
                json.loads(archive.extractfile('matrix-installer/release.json').read())['version'],
                json.loads((BUNDLE / 'release.json').read_text())['version'],
            )
            self.assertEqual(
                archive.extractfile('matrix-installer/LICENSE').read(),
                (BUNDLE.parent / 'LICENSE').read_bytes(),
            )
            licenses = (
                archive.extractfile('matrix-installer/third-party-licenses.md').read().decode()
            )
            self.assertIn('Copyright (c) 2020 Will McGugan', licenses)
            self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', licenses)
            self.assertIn('matrix-installer/terminal_views.py', names)
            self.assertIn('matrix-installer/ui_actions.py', names)
            self.assertIn('matrix-installer/welcome_message.py', names)
            self.assertIn('matrix-installer/welcome_settings.py', names)
            manifest = archive.extractfile('matrix-installer/SHA256SUMS').read().decode()
            self.assertEqual(
                {'matrix-installer/' + line.split('  ', 1)[1] for line in manifest.splitlines()},
                names - {'matrix-installer/SHA256SUMS'},
            )
            for line in manifest.splitlines():
                digest, name = line.split('  ', 1)
                self.assertEqual(
                    hashlib.sha256(
                        archive.extractfile('matrix-installer/' + name).read()
                    ).hexdigest(),
                    digest,
                )
            for member in archive.getmembers():
                if not member.name.endswith('.md'):
                    continue
                content = archive.extractfile(member).read().decode()
                for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', content):
                    if target.startswith(('http:', 'https:', '#', 'mailto:')):
                        continue
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(member.name), target.split('#')[0])
                    )
                    if resolved not in names:
                        missing.append((member.name, target))
            self.assertEqual(
                missing, [], f'Public installer archive has broken document links: {missing}'
            )

    def invoke(self, *args, ok=True):
        result = subprocess.run(
            ['python3', str(BUNDLE / 'generate.py'), '--directory', str(self.root), *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        # These historical runtime invariants exercise legacy-format compatibility.
        # New split-format generation is covered independently in test_configuration.
        if result.returncode == 0 and (self.root / 'settings').exists():
            import configuration, configuration_schema, shutil

            values = {key: field.default for key, field in configuration_schema.FIELDS.items()}
            values.update(configuration.load(self.root))
            if '--database-mode' in args:
                values['POSTGRES_MODE'] = args[args.index('--database-mode') + 1]
            if '--media-mode' in args:
                values['MEDIA_MODE'] = args[args.index('--media-mode') + 1]
            shutil.rmtree(self.root / 'settings')
            import state_store

            state_store.write_text(self.root / 'settings.env', configuration.serialize(values))
            metadata = json.loads((self.root / 'installation.json').read_text())
            metadata['config_format_version'] = 1
            state_store.write_json(self.root / 'installation.json', metadata)
        return result

    def initialize(self):
        self.invoke('--domain', 'chat.example.org', '--initialize-env')

    def test_full_removal_preserves_bundle_and_can_initialize_a_new_domain(self):
        import remove_server
        import state_store

        self.initialize()
        package = Path(self.temp.name) / 'installer.tar.gz'
        subprocess.run(
            [sys.executable, str(BUNDLE / 'build.py'), '--release', '--output', str(package)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with tarfile.open(package) as archive:
            for member in archive.getmembers():
                relative = Path(member.name).relative_to('matrix-installer')
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(0o600)
        record = {
            'format': 1,
            'domain': 'chat.example.org',
            'directory': str(self.root),
            'local': dict.fromkeys(remove_server.LOCAL, True),
            's3': [],
            'external_database': False,
            'vault': False,
            'completed': [],
        }
        with (
            patch.object(remove_server, 'remove_containers'),
            patch.object(remove_server, 'host_cleanup'),
        ):
            remove_server.execute(self.root, record, [], stop=lambda: None)
        self.invoke('--domain', 'new.example.org', '--initialize-env')
        self.assertEqual(
            json.loads((self.root / 'installation.json').read_text())['domain'], 'new.example.org'
        )
        self.assertFalse((self.root / '.misbot-empty.json').exists())
        self.assertTrue((self.root / 'remove_server.py').exists())

    def test_initial_domain_typo_or_missing_domain_does_not_poison_retry_directory(self):
        for domain in ('https://fictional.invalid', '127.0.0.1', 'mi\u200bsbot.example.org', None):
            with self.subTest(domain=domain):
                args = ['--initialize-env']
                if domain is not None:
                    args += ['--domain', domain]
                self.invoke(*args, ok=False)
                self.assertFalse(self.root.exists())
        self.initialize()
        before = self.fingerprints()
        self.invoke('--domain', 'chat.example.org', '--initialize-env')
        self.assertEqual(before, self.fingerprints())

    def fingerprints(self):
        files = list((self.root / 'secrets').iterdir()) + [self.root / 'config/mas-signing.pem']
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    def test_credentials_are_unique_and_stable(self):
        self.initialize()
        before = self.fingerprints()
        secrets = [
            p.read_text().strip() for p in (self.root / 'secrets').iterdir() if p.suffix != '.json'
        ]
        self.assertEqual(len(set(secrets)), 10)
        self.assertTrue(all(len(secret) == 64 for secret in secrets))
        self.invoke('--domain', 'chat.example.org', '--initialize-env')
        self.assertEqual(before, self.fingerprints())
        admin = json.loads((self.root / 'secrets/admin-access.json').read_text())
        self.assertEqual(admin['matrix_id'], '@matrix-admin:chat.example.org')
        self.assertGreaterEqual(len(admin['password']), 40)
        self.assertEqual((self.root / 'settings.env').stat().st_mode & 0o777, 0o600)

    def test_local_bootstrap_hides_password_SQL_before_any_role_statement(self):
        self.invoke('--domain', 'chat.example.org', '--initialize-env', '--preview')
        env = self.root / 'settings.env'
        env.write_text(env.read_text().replace('PUBLIC_IP=', 'PUBLIC_IP=1.2.3.4'))
        self.invoke('--preview')
        sql = (self.root / 'config/init-db.sql').read_text()
        password = sql.index('PASSWORD')
        for control in (
            "SET log_statement='none';",
            "SET log_duration='off';",
            "SET log_error_verbosity='terse';",
            "SET log_min_error_statement='panic';",
        ):
            self.assertLess(sql.index(control), password)
        self.assertEqual((self.root / 'secrets').stat().st_mode & 0o777, 0o700)

    def test_custom_mas_domain_keeps_credentials_and_routes_shared_matrix_host(self):
        import configuration, state_store

        self.invoke('--domain', 'chat.example.org', '--initialize-env', '--preview')
        state_store.write_text(self.root / 'config/mas-signing.pem', 'FICTIONAL_SIGNING_KEY')
        before = self.fingerprints()
        for host in ('auth.example.net', 'matrix.chat.example.org', 'chat.example.org'):
            with self.subTest(host=host):
                configuration.patch(self.root, {'MAS_DOMAIN': host})
                self.invoke('--preview')
                mas = yaml.safe_load((self.root / 'config/mas.yaml').read_text())
                discovery = json.loads((self.root / 'public/well-known-client.json').read_text())
                self.assertEqual(mas['http']['issuer'], 'https://' + host + '/')
                self.assertEqual(
                    discovery['org.matrix.msc2965.authentication']['issuer'], mas['http']['issuer']
                )
                caddy = (self.root / 'config/Caddyfile').read_text()
                self.assertEqual(caddy.count('\n' + host + ' {'), 1)
                self.assertNotIn('\nmas.chat.example.org {', caddy)
                self.assertIn('reverse_proxy mas:8080', caddy)
                self.assertIn('reverse_proxy synapse:8008', caddy)
                if host == 'matrix.chat.example.org':
                    self.assertIn('@matrix_api path /_matrix/* /_synapse/*', caddy)
                self.assertEqual(self.fingerprints(), before)

    def test_initialized_mas_issuer_change_fails_before_runtime_or_secrets_written(self):
        import configuration, state_store

        self.invoke('--domain', 'chat.example.org', '--initialize-env', '--preview')
        state_store.write_text(self.root / 'config/mas-signing.pem', 'FICTIONAL_SIGNING_KEY')
        configuration.patch(self.root, {'MAS_DOMAIN': 'auth.example.net'})
        self.invoke('--preview')
        metadata = json.loads((self.root / 'installation.json').read_text())
        metadata['databases_initialized'] = True
        state_store.write_json(self.root / 'installation.json', metadata)
        paths = ('config/mas.yaml', 'config/homeserver.yaml', 'compose.yaml', 'installation.json')
        saved = {name: (self.root / name).read_bytes() for name in paths}
        keys = self.fingerprints()
        env = self.root / 'settings.env'
        state_store.write_text(
            env,
            env.read_text().replace('MAS_DOMAIN=auth.example.net', 'MAS_DOMAIN=other.example.net'),
        )
        result = self.invoke('--preview', ok=False)
        self.assertIn('MAS issuer is immutable', result.stderr)
        self.assertEqual({name: (self.root / name).read_bytes() for name in paths}, saved)
        self.assertEqual(self.fingerprints(), keys)

    def test_first_source_seed_preserves_initialized_secrets(self):
        self.initialize()
        before = self.fingerprints()
        subprocess.run(
            [sys.executable, str(BUNDLE / 'build.py'), '--check'],
            check=True,
            capture_output=True,
            text=True,
        )
        archive_path = Path(self.temp.name) / 'matrix-installer.tar.gz'
        subprocess.run(
            [sys.executable, str(BUNDLE / 'build.py'), '--release', '--output', str(archive_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        with tarfile.open(archive_path) as archive:
            for member in archive:
                self.assertTrue(member.isfile())
                self.assertEqual(Path(member.name).parts[0], 'matrix-installer')
                self.assertNotIn('..', Path(member.name).parts)
                target = Path(self.temp.name) / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        source = Path(self.temp.name) / 'matrix-installer'
        spec = importlib.util.spec_from_file_location('installer_update', BUNDLE / 'update.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertFalse((self.root / 'install.sh').exists())
        with (
            patch.object(module, 'require_root'),
            patch.object(
                sys,
                'argv',
                ['update.py', 'seed', '--source', str(source), '--directory', str(self.root)],
            ),
        ):
            module.main()
        self.assertTrue((self.root / 'install.sh').is_file())
        self.assertTrue((self.root / 'lifecycle.py').is_file())
        self.assertTrue((self.root / 'referral-bot/main.js').is_file())
        self.assertTrue((self.root / 'referral-control/bridge.py').is_file())
        self.assertEqual(before, self.fingerprints())

    def test_missing_existing_key_fails_instead_of_rotating(self):
        self.initialize()
        (self.root / 'secrets/mas_encryption').unlink()
        self.invoke('--initialize-env', ok=False)
        self.assertFalse((self.root / 'secrets/mas_encryption').exists())

    def test_disappearing_signing_key_is_not_regenerated_later_in_same_run(self):
        self.initialize()
        env = self.root / 'settings.env'
        env.write_text(env.read_text().replace('PUBLIC_IP=', 'PUBLIC_IP=8.8.8.8'))
        worker = Path(self.temp.name) / 'example-race.py'
        worker.write_text(
            'import sys,runpy; from pathlib import Path; '
            'sys.path.insert(0,sys.argv[1]); import state_store,os\n'
            # Keep the real UID for lock/tempfile ownership; this worker only
            # exercises rendering and never starts privileged services.
            'state_store.require_root=lambda *args:None\n'
            'os.chown=lambda *args:None\n'
            'original=state_store.write_json\n'
            'saves=0\n'
            'def save(path,value):\n'
            ' global saves\n'
            ' original(path,value)\n'
            ' if Path(path).name=="installation.json":\n'
            '  saves+=1\n'
            '  if saves==2:\n'
            '   (Path(path).parent/"config/mas-signing.pem").unlink(missing_ok=True)\n'
            'state_store.write_json=save\n'
            'bundle=sys.argv[1]; root=sys.argv[2]\n'
            'sys.argv=["generate.py","--directory",root,"--domain","chat.example.org"]\n'
            'runpy.run_path(str(Path(bundle)/"generate.py"),run_name="__main__")\n'
        )
        result = subprocess.run(
            [sys.executable, str(worker), str(BUNDLE), str(self.root)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('signing key disappeared', result.stderr)
        self.assertFalse((self.root / 'config/mas-signing.pem').exists())
        self.invoke('--initialize-env', ok=False)
        self.assertFalse((self.root / 'config/mas-signing.pem').exists())

    def test_domain_cannot_change(self):
        self.initialize()
        before = self.fingerprints()
        self.invoke('--domain', 'other.example.org', '--initialize-env', ok=False)
        self.assertEqual(before, self.fingerprints())

    def test_invalid_private_defaults_error_omits_yaml_contents(self):
        self.initialize()
        env = self.root / 'settings.env'
        env.write_text(env.read_text().replace('PUBLIC_IP=', 'PUBLIC_IP=8.8.8.8'))
        marker = 'EXAMPLE_PRIVATE_MARKER_NOT_A_REAL_SECRET'
        defaults = self.root / 'config/mas-defaults.yaml'
        defaults.write_text('example: [' + marker)
        defaults.chmod(0o600)
        worker = Path(self.temp.name) / 'example-defaults.py'
        worker.write_text(
            'import sys,os,runpy; from pathlib import Path; '
            'sys.path.insert(0,sys.argv[1]); import state_store\n'
            'state_store.require_root=lambda *args:None\n'
            'os.chown=lambda *args:None\n'
            'bundle=sys.argv[1];root=sys.argv[2]\n'
            'sys.argv=["generate.py","--directory",root]\n'
            'runpy.run_path(str(Path(bundle)/"generate.py"),run_name="__main__")\n'
        )
        result = subprocess.run(
            [sys.executable, str(worker), str(BUNDLE), str(self.root)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MAS defaults YAML is invalid', result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(defaults.read_text(), 'example: [' + marker)

    def test_hostname_validation(self):
        for domain in [
            'https://example.org',
            'example.org:443',
            'example.org/path',
            'a;touch.example.org',
            '127.0.0.1',
            '* .example.org',
        ]:
            with self.subTest(domain=domain):
                self.invoke('--domain', domain, '--initialize-env', ok=False)

    def test_partial_provider_and_unknown_env_fail(self):
        self.initialize()
        env = self.root / 'settings.env'
        original = env.read_text()
        env.write_text(original.replace('GOOGLE_CLIENT_ID=', 'GOOGLE_CLIENT_ID=value'))
        self.invoke('--initialize-env', ok=False)
        env.write_text(original + 'TYPO_SECRET=value\n')
        self.invoke('--initialize-env', ok=False)

    def test_env_does_not_execute_shell(self):
        self.initialize()
        env = self.root / 'settings.env'
        original = env.read_text()
        marker = Path(self.temp.name) / 'should-not-exist'
        value = '$(touch ' + str(marker) + ')'
        env.write_text(
            original.replace('GOOGLE_CLIENT_ID=', 'GOOGLE_CLIENT_ID=dummy').replace(
                'GOOGLE_CLIENT_SECRET=', 'GOOGLE_CLIENT_SECRET=' + value
            )
        )
        self.invoke('--initialize-env')
        self.assertFalse(marker.exists())

    def test_render_uses_seven_domains_and_private_network(self):
        self.invoke('--domain', 'chat.example.org', '--preview')
        import admin_panel

        panel = admin_panel.Panel(self.root)
        self.assertEqual(panel.settings()['welcome'], admin_panel.DEFAULT_WELCOME)
        self.assertIn(
            'Здравствуйте. Добро пожаловать в chat.example.org.',
            panel.welcome_text(panel.settings()['welcome']),
        )
        compose = yaml.safe_load((self.root / 'compose.yaml').read_text())
        synapse = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
        livekit = yaml.safe_load((self.root / 'config/livekit.yaml').read_text())
        caddy = (self.root / 'config/Caddyfile').read_text()
        landing = (self.root / 'public/site/index.html').read_text()
        setup_url = json.loads((BUNDLE / 'release.json').read_text())['homepage_setup_url']
        self.assertIn('href="' + html.escape(setup_url, quote=True) + '"', landing)
        self.assertIn('Подключить свой сервер', landing)
        self.assertNotIn('@@SETUPURL@@', landing)
        self.assertEqual(len(compose['services']), 12)
        self.assertEqual(synapse['server_name'], 'chat.example.org')
        self.assertEqual(synapse['federation_domain_whitelist'], ['misbot.ru'])
        self.assertEqual(synapse['encryption_enabled_by_default_for_room_type'], 'all')
        self.assertEqual(livekit['turn']['proxy_protocol_trusted_cidrs'], ['172.30.50.0/24'])
        self.assertIn('allow 172.30.50.0/24', caddy)
        self.assertNotIn('element.chat.example.org', caddy)
        self.assertNotIn('live.chat.example.org', caddy)
        self.assertNotIn('/srv/discovery/mas.yaml', caddy)
        self.assertNotIn('ports', compose['services']['postgres'])
        self.assertIn('./public:/srv/discovery:ro', compose['services']['caddy']['volumes'])
        self.assertTrue(
            all(
                '@sha256:' in service['image']
                for name, service in compose['services'].items()
                if not name.startswith(('referral-', 'federation-'))
            )
        )
        self.assertEqual(compose['services']['federation-bot']['user'], '10003:10003')
        self.assertIn('handle /.well-known/misbot', caddy)
        self.assertIn('handle /_misbot/v1/*', caddy)
        self.assertIn('unix//run/misbot/http.sock', caddy)
        self.assertEqual(
            compose['networks']['default']['ipam']['config'], [{'subnet': '172.30.50.0/24'}]
        )

    def test_referral_client_is_private_and_all_registration_requires_invites(self):
        self.invoke('--domain', 'chat.example.org', '--preview')
        mas = yaml.safe_load((self.root / 'config/mas.yaml').read_text())
        client = json.loads((self.root / 'secrets/referral-client.json').read_text())
        self.assertEqual(mas['policy']['data']['admin_clients'], [client['client_id']])
        self.assertTrue(mas['account']['password_registration_token_required'])
        self.assertEqual((self.root / 'secrets/referral-client.json').stat().st_mode & 0o777, 0o600)
        cfg = json.loads((self.root / 'config/referral-bot.json').read_text())
        self.assertEqual(cfg['user_id'], '@referral-bot:chat.example.org')
        self.assertNotIn('client_secret', cfg)
        env = self.root / 'settings.env'
        env.write_text(
            env.read_text()
            .replace('GOOGLE_CLIENT_ID=', 'GOOGLE_CLIENT_ID=example')
            .replace('GOOGLE_CLIENT_SECRET=', 'GOOGLE_CLIENT_SECRET=example')
            .replace('TELEGRAM_CLIENT_ID=', 'TELEGRAM_CLIENT_ID=example')
            .replace('TELEGRAM_CLIENT_SECRET=', 'TELEGRAM_CLIENT_SECRET=example')
        )
        self.invoke('--preview')
        providers = yaml.safe_load((self.root / 'config/mas.yaml').read_text())['upstream_oauth2'][
            'providers'
        ]
        self.assertTrue(all(p['registration_token_required'] for p in providers))

    def test_invitation_policy_is_configurable_and_invalid_values_preserve_config(self):
        self.invoke('--domain', 'chat.example.org', '--preview')
        env = self.root / 'settings.env'
        original = env.read_text()
        env.write_text(
            original.replace(
                'INVITATION_LIFETIME_SECONDS=604800', 'INVITATION_LIFETIME_SECONDS=3600'
            )
            .replace('INVITATION_USAGE_LIMIT=1', 'INVITATION_USAGE_LIMIT=3')
            .replace('REGISTRATION_TOKEN_REQUIRED=true', 'REGISTRATION_TOKEN_REQUIRED=false')
        )
        self.invoke('--preview')
        policy = self.root / 'data/referral-control/bot-policy.json'
        value = json.loads(policy.read_text())['servers']['chat.example.org']
        self.assertEqual(value['invitation_lifetime_seconds'], 3600)
        self.assertEqual(value['invitation_usage_limit'], 3)
        self.assertFalse(value['registration_required'])
        before = policy.read_bytes()
        for key, value in [
            ('INVITATION_LIFETIME_SECONDS', '0'),
            ('INVITATION_USAGE_LIMIT', '-1'),
            ('REGISTRATION_TOKEN_REQUIRED', 'yes'),
        ]:
            broken = original.replace(
                key
                + '='
                + {
                    'INVITATION_LIFETIME_SECONDS': '604800',
                    'INVITATION_USAGE_LIMIT': '1',
                    'REGISTRATION_TOKEN_REQUIRED': 'true',
                }[key],
                key + '=' + value,
            )
            env.write_text(broken)
            self.invoke('--preview', ok=False)
            self.assertEqual(before, policy.read_bytes())

    def test_federation_list_normalizes_and_updates_both_configs(self):
        self.invoke('--domain', 'chat.example.org', '--preview')
        env = self.root / 'settings.env'
        env.write_text(
            env.read_text().replace(
                'FEDERATION_SERVERS=misbot.ru',
                'FEDERATION_SERVERS= MISBOT.RU , matrix.org,misbot.ru, chat.example.org',
            )
        )
        self.invoke('--preview')
        synapse = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
        web = json.loads((self.root / 'public/web-config.json').read_text())
        self.assertEqual(
            synapse['federation_domain_whitelist'], ['misbot.ru', 'matrix.org', 'chat.example.org']
        )
        self.assertEqual(
            web['room_directory']['servers'], ['chat.example.org', 'misbot.ru', 'matrix.org']
        )
        env.write_text(
            env.read_text().replace(
                'FEDERATION_SERVERS= MISBOT.RU , matrix.org,misbot.ru, chat.example.org',
                'FEDERATION_SERVERS=other.example.net',
            )
        )
        self.invoke('--preview')
        self.assertEqual(
            yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())[
                'federation_domain_whitelist'
            ],
            ['other.example.net'],
        )

    def test_invalid_federation_list_preserves_previous_config(self):
        self.invoke('--domain', 'chat.example.org', '--preview')
        env = self.root / 'settings.env'
        original = env.read_text()
        before = (self.root / 'config/homeserver.yaml').read_bytes()
        for value in [
            '',
            ',misbot.ru',
            'misbot.ru,',
            'misbot.ru,,matrix.org',
            'https://misbot.ru',
            'misbot.ru:443',
            '*.example.org',
            '127.0.0.1',
            'misbot.ru/path',
        ]:
            with self.subTest(value=value):
                env.write_text(
                    original.replace('FEDERATION_SERVERS=misbot.ru', 'FEDERATION_SERVERS=' + value)
                )
                self.invoke('--preview', ok=False)
                self.assertEqual(before, (self.root / 'config/homeserver.yaml').read_bytes())

    def test_old_env_uses_default_federation_server(self):
        self.invoke('--domain', 'chat.example.org', '--preview')
        env = self.root / 'settings.env'
        env.write_text(env.read_text().replace('FEDERATION_SERVERS=misbot.ru\n', ''))
        self.invoke('--preview')
        self.assertEqual(
            yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())[
                'federation_domain_whitelist'
            ],
            ['misbot.ru'],
        )

    def test_external_postgres_and_s3_generate_complete_stack(self):
        self.invoke(
            '--domain',
            'chat.example.org',
            '--database-mode',
            'external',
            '--media-mode',
            's3',
            '--initialize-env',
            '--preview',
        )
        env = self.root / 'settings.env'
        self.assertIn('POSTGRES_MODE=external', env.read_text())
        self.assertIn('MEDIA_MODE=s3', env.read_text())
        value = env.read_text().replace('POSTGRES_HOST=', 'POSTGRES_HOST=db.example.net')
        value = value.replace('POSTGRES_ADMIN_PASSWORD=', 'POSTGRES_ADMIN_PASSWORD=example:secret')
        value = value.replace('S3_BUCKET=', 'S3_BUCKET=example-private-bucket')
        value = value.replace('S3_REGION=', 'S3_REGION=eu-central-1')
        value = value.replace('S3_ENDPOINT_URL=', 'S3_ENDPOINT_URL=https://objects.example.net')
        value = value.replace('S3_ACCESS_KEY_ID=', 'S3_ACCESS_KEY_ID=example-access')
        value = value.replace('S3_SECRET_ACCESS_KEY=', 'S3_SECRET_ACCESS_KEY=example-secret')
        value = value.replace('S3_PREFIX=', 'S3_PREFIX=chat-example-org/')
        env.write_text(value)
        self.invoke('--preview')
        compose = yaml.safe_load((self.root / 'compose.yaml').read_text())
        synapse = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
        mas = yaml.safe_load((self.root / 'config/mas.yaml').read_text())
        self.assertNotIn('postgres', compose['services'])
        self.assertEqual(compose['services']['postgres-client']['profiles'], ['tools'])
        self.assertNotIn('depends_on', compose['services']['synapse'])
        self.assertEqual(synapse['database']['args']['host'], 'db.example.net')
        self.assertEqual(synapse['database']['args']['sslmode'], 'require')
        self.assertEqual(mas['database']['host'], 'db.example.net')
        self.assertTrue(synapse['enable_local_media_storage'])
        provider = synapse['media_storage_providers'][0]
        self.assertTrue(provider['store_synchronous'])
        self.assertEqual(provider['config']['prefix'], 'chat-example-org/')
        self.assertEqual(provider['config']['endpoint_url'], 'https://objects.example.net')
        self.assertEqual(provider['config']['secret_access_key'], 'REDACTED_SECRET')
        self.assertIn('REDACTED_PASSWORD', (self.root / 'config/postgres-admin.pgpass').read_text())
        # Every runtime which names the CA must receive it, including Synapse/MAS.
        env.write_text(
            env.read_text().replace('POSTGRES_SSL_MODE=require', 'POSTGRES_SSL_MODE=verify-full')
        )
        self.invoke('--preview')
        compose = yaml.safe_load((self.root / 'compose.yaml').read_text())
        for role in ('synapse', 'mas', 'postgres-client'):
            self.assertIn(
                './config/postgres-ca.pem:/config/postgres-ca.pem:ro',
                compose['services'][role]['volumes'],
            )

    def test_external_storage_requires_credentials_and_modes_are_immutable_after_start(self):
        self.invoke(
            '--domain',
            'chat.example.org',
            '--database-mode',
            'external',
            '--media-mode',
            's3',
            '--initialize-env',
            '--preview',
        )
        self.invoke('--preview', ok=False)
        env = self.root / 'settings.env'
        text = env.read_text()
        for key, value in [
            ('POSTGRES_HOST', 'db.example.net'),
            ('POSTGRES_ADMIN_PASSWORD', 'example'),
            ('S3_BUCKET', 'bucket'),
            ('S3_REGION', 'eu-central-1'),
            ('S3_ACCESS_KEY_ID', 'key'),
            ('S3_SECRET_ACCESS_KEY', 'secret'),
            ('S3_PREFIX', 'chat/'),
        ]:
            text = text.replace(key + '=', key + '=' + value)
        env.write_text(text)
        self.invoke('--preview')
        state = self.root / 'installation.json'
        metadata = json.loads(state.read_text())
        metadata.update(
            storage_committed=True, database_mode='external', media_mode='s3', s3_prefix='chat/'
        )
        state.write_text(json.dumps(metadata))
        env.write_text(text.replace('S3_PREFIX=chat/', 'S3_PREFIX=other/'))
        self.invoke('--preview', ok=False)

    def test_committed_external_host_and_port_changes_refused_before_config_replacement(self):
        self.invoke(
            '--domain',
            'chat.example.org',
            '--database-mode',
            'external',
            '--initialize-env',
            '--preview',
        )
        env = self.root / 'settings.env'
        original = (
            env.read_text()
            .replace('POSTGRES_HOST=', 'POSTGRES_HOST=db.example.net')
            .replace('POSTGRES_ADMIN_PASSWORD=', 'POSTGRES_ADMIN_PASSWORD=EXAMPLE_NOT_REAL')
        )
        env.write_text(original)
        self.invoke('--preview')
        metadata = json.loads((self.root / 'installation.json').read_text())
        metadata.update(storage_committed=True, database_mode='external', media_mode='local')
        (self.root / 'installation.json').write_text(json.dumps(metadata))
        checkpoint = self.root / 'config/external-postgres-owner.json'
        checkpoint.write_text(
            json.dumps(
                {
                    'format': 1,
                    'owner': 'EXAMPLE_OWNER',
                    'identity': {
                        'POSTGRES_HOST': 'db.example.net',
                        'POSTGRES_PORT': '5432',
                        'server_name': 'chat.example.org',
                    },
                }
            )
        )
        self.invoke('--preview')
        (self.root / 'config/vault-state.json').write_text(
            json.dumps({'dirty': False, 'version': 1})
        )
        observed = {
            name: (self.root / name).read_bytes()
            for name in (
                'compose.yaml',
                'config/homeserver.yaml',
                'config/mas.yaml',
                'config/vault-state.json',
            )
        }
        for old, new in [
            ('POSTGRES_HOST=db.example.net', 'POSTGRES_HOST=other.example.net'),
            ('POSTGRES_PORT=5432', 'POSTGRES_PORT=55432'),
        ]:
            env.write_text(original.replace(old, new))
            self.invoke('--preview', ok=False)
            self.assertEqual({name: (self.root / name).read_bytes() for name in observed}, observed)
        # Legacy installs without the checkpoint still retain their last rendered
        # public connection identity; credentials are never compared by this guard.
        checkpoint.unlink()
        env.write_text(original)
        self.invoke('--preview')
        env.write_text(
            original.replace('POSTGRES_HOST=db.example.net', 'POSTGRES_HOST=other.example.net')
        )
        self.invoke('--preview', ok=False)

    def test_subnet_can_change_only_before_network_creation(self):
        self.initialize()
        env = self.root / 'settings.env'
        env.write_text(env.read_text().replace('172.30.50.0/24', '172.30.60.0/24'))
        self.invoke('--initialize-env')
        path = self.root / 'installation.json'
        metadata = json.loads(path.read_text())
        self.assertEqual(metadata['subnet'], '172.30.60.0/24')
        metadata['network_committed'] = True
        path.write_text(json.dumps(metadata))
        env.write_text(env.read_text().replace('172.30.60.0/24', '172.30.70.0/24'))
        self.invoke('--initialize-env', ok=False)


if __name__ == '__main__':
    unittest.main()
