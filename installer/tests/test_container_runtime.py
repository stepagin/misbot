import copy
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import command_wrapper
from existing_compose import Error
from existing_features import Features
import existing_proxy
import telegram_container
import existing_update


IMAGE = 'sha256:' + 'a' * 64


class RuntimeTests(unittest.TestCase):
    def fixture(self, root):
        proxy = {
            'format': 1,
            'container': 'misbot-caddy',
            'config_directory': 'runtime/caddy/config',
            'data_directory': 'runtime/caddy/data',
            'state_directory': 'runtime/caddy/state',
        }
        worker = {
            'worker_container': 'misbot-telegram-worker',
            'worker_config': 'runtime/telegram/worker.json',
            'worker_directory': str(root / 'runtime/telegram/data'),
            'socket_directory': '/run/misbot-telegram',
        }
        installation = SimpleNamespace(
            root=root,
            state={'domain': 'matrix.fictional.example.org'},
            model={
                'proxy': proxy,
                'telegram_control': worker,
                'features': {
                    'caddyfile': str(root / 'runtime/caddy/config/Caddyfile'),
                    'site_directory': '/srv/misbot-fictional/site',
                },
                'images': {
                    'misbot-caddy': {'id': IMAGE, 'digest': 'caddy@' + IMAGE},
                    'misbot-telegram-worker': {'id': IMAGE, 'digest': IMAGE},
                },
            },
            services={
                'misbot-caddy': ('fictional-runtime', 'caddy'),
                'misbot-telegram-worker': ('fictional-runtime', 'telegram-worker'),
            },
            run=Mock(return_value=(0, b'')),
            container=Mock(return_value={'running': True, 'image_id': IMAGE}),
        )
        for path in (
            'runtime/caddy/config',
            'runtime/caddy/data',
            'runtime/caddy/state',
            'runtime/telegram/data',
        ):
            (root / path).mkdir(parents=True, exist_ok=True)
        (root / worker['worker_config']).write_text('{}')
        return installation

    def test_host_caddy_remains_compatible(self):
        installation = SimpleNamespace(model={}, run=Mock())
        with patch.object(
            command_wrapper, 'regular', return_value=SimpleNamespace(st_uid=0, st_mode=0o600)
        ):
            existing_proxy.Proxy(installation).validate(Path('/tmp/fictional-Caddyfile'))
        installation.run.assert_called_once_with(
            ['caddy', 'validate', '--config', '/tmp/fictional-Caddyfile', '--adapter', 'caddyfile']
        )
        existing_proxy.Proxy(installation).reload()
        self.assertEqual(installation.run.call_args.args[0], ['systemctl', 'reload', 'caddy'])

    def test_proxy_validator_is_offline_and_cannot_mount_installation_or_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation = self.fixture(root)
            candidate = root / 'candidate'
            candidate.write_text('fictional.example.org { respond 200 }')
            with patch.object(
                command_wrapper, 'regular', return_value=SimpleNamespace(st_uid=0, st_mode=0o600)
            ):
                existing_proxy.Proxy(installation).validate(candidate)
            args = installation.run.call_args.args[0]
            self.assertIn('none', args)
            self.assertIn('caddy@' + IMAGE, args)
            self.assertIn('--read-only', args)
            self.assertEqual(args[args.index('--cap-add') + 1], 'NET_BIND_SERVICE')
            self.assertNotIn('docker.sock', ' '.join(args))
            self.assertNotIn('source=' + str(root) + ',', ' '.join(args))
            self.assertIn('/data:rw,size=64m', args)
            self.assertIn('target=/validation-data,readonly', ' '.join(args))
            existing_proxy.Proxy(installation).reload()
            self.assertEqual(
                installation.run.call_args.args[0][:3], ['docker', 'exec', 'misbot-caddy']
            )
            installation.container.return_value['image_id'] = 'sha256:' + 'b' * 64
            with self.assertRaises(Error):
                existing_proxy.Proxy(installation).reload()

    def test_proxy_rejects_unknown_format_overlapping_storage_and_wrong_config(self):
        with tempfile.TemporaryDirectory() as folder:
            installation = self.fixture(Path(folder))
            before = copy.deepcopy(installation.model)
            for change in (
                {'format': 2},
                {'config_directory': '../private'},
                {'data_directory': 'runtime/caddy/config'},
                {'state_directory': 'runtime/caddy'},
                {'container': 'foreign-caddy'},
            ):
                installation.model = copy.deepcopy(before)
                installation.model['proxy'].update(change)
                with self.assertRaises(Error):
                    existing_proxy.Proxy(installation)

    def test_only_instance_public_socket_and_published_site_are_proxy_mounts(self):
        with tempfile.TemporaryDirectory() as folder:
            installation = self.fixture(Path(folder))
            suffix = hashlib.sha256(installation.state['domain'].encode()).hexdigest()[:16]
            source = '/run/misbot-federation-' + suffix
            installation.model['external_runtime_mounts'] = [
                {'container': 'misbot-caddy', 'source': source, 'target': source}
            ]
            volume = {'source': source, 'target': source, 'read_only': True}
            info = SimpleNamespace(st_uid=0, st_gid=10001, st_mode=stat.S_IFDIR | 0o750)
            with (
                patch.object(Path, 'resolve', lambda path: path),
                patch.object(Path, 'lstat', return_value=info),
            ):
                self.assertTrue(existing_proxy.accepted_mount(installation, 'misbot-caddy', volume))
                self.assertFalse(
                    existing_proxy.accepted_mount(installation, 'matrix-synapse', volume)
                )
                self.assertFalse(
                    existing_proxy.accepted_mount(
                        installation, 'misbot-caddy', dict(volume, read_only=False)
                    )
                )
                self.assertFalse(
                    existing_proxy.accepted_mount(
                        installation, 'misbot-caddy', dict(volume, source='/run/docker.sock')
                    )
                )
                info.st_gid = 0
                info.st_mode = stat.S_IFDIR | 0o755
                self.assertTrue(existing_proxy.accepted_mount(installation, 'misbot-caddy', volume))
                info.st_mode = stat.S_IFDIR | 0o777
                self.assertFalse(
                    existing_proxy.accepted_mount(installation, 'misbot-caddy', volume)
                )
                info.st_mode = stat.S_IFDIR | 0o755
                volume = {
                    'source': '/srv/misbot-fictional/site',
                    'target': '/srv/misbot-fictional/site',
                    'read_only': True,
                }
                installation.model['external_runtime_mounts'].append(
                    {'container': 'misbot-caddy', **{k: volume[k] for k in ('source', 'target')}}
                )
                self.assertTrue(existing_proxy.accepted_mount(installation, 'misbot-caddy', volume))
                info.st_mode = stat.S_IFDIR | 0o777
                self.assertFalse(
                    existing_proxy.accepted_mount(installation, 'misbot-caddy', volume)
                )

    def test_proxy_cannot_mount_private_installation_data_or_gain_extra_privileges(self):
        with tempfile.TemporaryDirectory() as folder:
            installation = self.fixture(Path(folder))
            proxy = installation.model['proxy']
            entry = {
                'read_only': True,
                'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'],
                'volumes': [
                    {
                        'source': str(installation.root / proxy[key]),
                        'target': target,
                        'type': 'bind',
                        'read_only': key == 'config_directory',
                    }
                    for key, target in (
                        ('config_directory', '/etc/caddy'),
                        ('data_directory', '/data'),
                        ('state_directory', '/config'),
                    )
                ],
            }
            existing_proxy.validate_service(installation, 'misbot-caddy', entry)
            for change in (
                {'privileged': True},
                {'cap_add': ['SYS_ADMIN']},
                {'secrets': ['mas-admin']},
                {'pid': 'host'},
                {'read_only': False},
            ):
                with self.assertRaises(Error):
                    existing_proxy.validate_service(installation, 'misbot-caddy', entry | change)
            entry['volumes'].append(
                {
                    'source': str(installation.root),
                    'target': '/installation',
                    'type': 'bind',
                    'read_only': True,
                }
            )
            with self.assertRaises(Error):
                existing_proxy.validate_service(installation, 'misbot-caddy', entry)

    def test_worker_mounts_and_privileges_exclude_admin_config_and_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            installation = self.fixture(Path(folder))
            entry = telegram_container.service(installation.root, installation.model, IMAGE)
            volumes = []
            for spec in entry['volumes']:
                parts = spec.split(':')
                volumes.append(
                    {
                        'type': 'bind',
                        'source': parts[0],
                        'target': parts[1],
                        'read_only': len(parts) == 3,
                    }
                )
            entry['volumes'] = volumes

            def info(path):
                directory = path.name == 'data'
                return SimpleNamespace(
                    st_uid=10001,
                    st_gid=10001,
                    st_nlink=1,
                    st_mode=(stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o600),
                )

            with patch.object(Path, 'lstat', side_effect=info, autospec=True):
                telegram_container.validate_service(installation, 'misbot-telegram-worker', entry)
                for change in (
                    {'user': '0:0'},
                    {'privileged': True},
                    {'cap_add': ['SYS_ADMIN']},
                    {'secrets': ['mas-admin']},
                    {
                        'environment': {
                            'MISBOT_CONTROL_SOCKET': '/run/control/control.sock',
                            'SECRET': 'fictional',
                        }
                    },
                ):
                    with self.assertRaises(Error):
                        telegram_container.validate_service(
                            installation, 'misbot-telegram-worker', entry | change
                        )
                bad = copy.deepcopy(entry)
                bad['volumes'].append(
                    {'type': 'bind', 'source': '/run/docker.sock', 'target': '/var/run/docker.sock'}
                )
                with self.assertRaises(Error):
                    telegram_container.validate_service(installation, 'misbot-telegram-worker', bad)

    def test_worker_socket_is_exact_readonly_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            installation = self.fixture(Path(folder))
            source = '/run/misbot-telegram'
            installation.model['external_runtime_mounts'] = [
                {'container': 'misbot-telegram-worker', 'source': source, 'target': '/run/control'}
            ]
            volume = {'source': source, 'target': '/run/control', 'read_only': True}
            info = SimpleNamespace(st_uid=0, st_gid=10001, st_mode=stat.S_IFDIR | 0o750)
            with (
                patch.object(Path, 'resolve', lambda path: path),
                patch.object(Path, 'lstat', return_value=info),
            ):
                self.assertTrue(
                    telegram_container.accepted_mount(
                        installation, 'misbot-telegram-worker', volume
                    )
                )
                self.assertFalse(
                    telegram_container.accepted_mount(
                        installation, 'misbot-telegram-worker', volume | {'read_only': False}
                    )
                )
                self.assertFalse(
                    telegram_container.accepted_mount(installation, 'matrix-synapse', volume)
                )

    def test_refresh_does_not_restart_disabled_host_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            installation = self.fixture(Path(folder))
            feature = object.__new__(Features)
            feature.installation = installation
            feature.restart_telegram(
                {
                    'control_unit': 'misbot-control.service',
                    'worker_unit': 'misbot-old-worker.service',
                }
            )
            commands = [call.args[0] for call in installation.run.call_args_list]
            self.assertIn(['docker', 'restart', 'misbot-telegram-worker'], commands)
            self.assertIn(['systemctl', 'restart', 'misbot-control.service'], commands)
            self.assertNotIn('misbot-old-worker.service', str(commands))

    def test_incompatible_source_update_is_rejected_before_backup_or_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            installation = self.fixture(Path(folder))
            installation.control = installation.root / '.misbot'
            installation.require_no_pending = Mock()
            installation.backup = Mock()
            source = Path(folder) / 'candidate'
            source.mkdir()
            (source / 'release.json').write_text(
                json.dumps(
                    {
                        'family': existing_update.update.FAMILY,
                        'version': '1.2.0',
                        'update_scope': 'source-and-configuration',
                    }
                )
            )
            for entries in (
                {},
                {'existing_proxy.py': Path('/fictional/source.py')},
                {
                    'existing_proxy.py': Path('/fictional/source.py'),
                    'telegram_container.py': Path('/fictional/worker.py'),
                },
            ):
                with patch.object(existing_update, 'sources', return_value=(entries, 'fixture')):
                    with self.assertRaises(Error):
                        existing_update.apply(installation, source)
            installation.backup.assert_not_called()
            self.assertFalse(installation.control.exists())


if __name__ == '__main__':
    unittest.main()
