from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_management as management
import state_store


class ServerManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        state_store.write_json(
            self.root / 'installation.json',
            {'backend': 'existing-compose', 'domain': 'example.org'},
        )
        state_store.write_json(self.root / 'release.json', {'version': '1.2.0'})
        self.actor = '@owner:example.org'
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.trust = patch(
            'server_management.command_wrapper.trusted_installation', return_value=self.root
        )
        self.trust.start()

    def tearDown(self):
        self.trust.stop()
        self.tmp.cleanup()

    def queue(self, operation='backup', event='$one', argument=None):
        return management.queue_operation(
            self.root, operation, self.actor, event, argument, runner=self.runner
        )

    def data(self):
        return json.loads((self.root / 'data/admin-panel/server-jobs.json').read_text())

    def test_queue_is_durable_and_duplicate_confirmation_launches_once(self):
        first = self.queue()
        second = self.queue()
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'queued')
        self.runner.assert_called_once()
        command = self.runner.call_args.args[0]
        self.assertEqual(command[0], 'systemd-run')
        self.assertIn(str(self.root / 'server_management.py'), command)
        self.assertEqual(command[-1], first['operation_id'])
        path = self.root / 'data/admin-panel/server-jobs.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.data()['format'], 1)

    def test_concurrent_duplicate_only_launches_one_worker(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: self.queue(), range(2)))
        self.assertEqual(jobs[0]['operation_id'], jobs[1]['operation_id'])
        self.runner.assert_called_once()

    def test_same_event_changed_operation_rejected_and_one_active_job(self):
        self.queue()
        for operation, event in [('start', '$one'), ('start', '$two')]:
            with self.subTest(operation=operation, event=event), self.assertRaises(RuntimeError):
                self.queue(operation, event)
        self.runner.assert_called_once()
        self.assertEqual(len(self.data()['jobs']), 1)

    def test_arguments_and_operations_are_allowlisted_before_launch(self):
        for operation, argument in [
            ('start;id', None),
            ('stop', 'arbitrary'),
            ('update', 'not-a-version'),
        ]:
            with self.subTest(operation=operation), self.assertRaises((RuntimeError, ValueError)):
                self.queue(operation, argument=argument)
        self.runner.assert_not_called()

    def test_unknown_old_queue_format_is_preserved(self):
        folder = self.root / 'data/admin-panel'
        folder.mkdir(mode=0o700, parents=True)
        path = folder / 'server-jobs.json'
        state_store.write_json(path, {'format': 9, 'jobs': {}})
        original = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Неизвестный формат'):
            self.queue()
        self.assertEqual(path.read_bytes(), original)
        self.runner.assert_not_called()

    def history(self, count):
        jobs = {}
        for index in range(count):
            identifier = 'SO-' + f'{index:032x}'
            jobs[identifier] = {
                'operation_id': identifier,
                'operation': 'backup',
                'status': 'complete',
                'actor': self.actor,
                'event_id': '$saved-history-' + str(index),
                'created_at': 0,
                'argument': None,
                'result': {'ok': True},
            }
        folder = self.root / 'data/admin-panel'
        folder.mkdir(mode=0o700, parents=True)
        state_store.write_json(folder / 'server-jobs.json', {'format': 1, 'jobs': jobs})
        return next(iter(jobs))

    def test_valid_history_above_configuration_limit_still_has_readable_results(self):
        identifier = self.history(600)
        path = self.root / 'data/admin-panel/server-jobs.json'
        self.assertGreater(path.stat().st_size, 131072)
        result = management.operation_status(self.root, identifier, self.actor, runner=self.runner)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['result']['ok'])
        self.runner.assert_not_called()

    def test_history_limit_rejects_new_job_and_preserves_existing_results(self):
        identifier = self.history(management.MAX_JOBS)
        path = self.root / 'data/admin-panel/server-jobs.json'
        original = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'предел истории'):
            self.queue()
        self.assertEqual(path.read_bytes(), original)
        self.runner.assert_not_called()
        self.assertEqual(
            management.operation_status(self.root, identifier, self.actor, runner=self.runner)[
                'status'
            ],
            'complete',
        )

    def test_symlink_queue_cannot_replace_outside_file(self):
        folder = self.root / 'data/admin-panel'
        folder.mkdir(mode=0o700, parents=True)
        outside = self.root / 'outside.json'
        state_store.write_json(outside, {'format': 1, 'jobs': {}})
        (folder / 'server-jobs.json').symlink_to(outside)
        original = outside.read_bytes()
        with self.assertRaises(Exception):
            self.queue()
        self.assertEqual(outside.read_bytes(), original)
        self.runner.assert_not_called()

    def test_launch_failure_is_saved_without_subprocess_secret(self):
        self.runner.side_effect = subprocess.CalledProcessError(1, 'FICTIONAL_SECRET')
        result = self.queue()
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))
        self.assertEqual(self.queue(), result)
        self.assertEqual(self.runner.call_count, 1)

    def test_inactive_old_worker_is_interrupted_and_never_replayed(self):
        job = self.queue()
        with management.database(self.root) as data:
            data['jobs'][job['operation_id']]['created_at'] = 0
        self.runner.return_value = subprocess.CompletedProcess([], 3)
        result = management.operation_status(
            self.root, job['operation_id'], self.actor, runner=self.runner
        )
        self.assertEqual(result['status'], 'interrupted')
        self.assertFalse(result['result']['ok'])
        self.assertEqual(self.queue()['status'], 'interrupted')
        self.assertEqual(
            sum(call.args[0][0] == 'systemd-run' for call in self.runner.call_args_list), 1
        )
        with self.assertRaises(RuntimeError):
            management.run_job(self.root, job['operation_id'])

    def test_unknown_unit_probe_preserves_pending_operation(self):
        job = self.queue()
        with management.database(self.root) as data:
            data['jobs'][job['operation_id']]['created_at'] = 0
        self.runner.side_effect = OSError('FICTIONAL_SECRET')
        result = management.operation_status(
            self.root, job['operation_id'], self.actor, runner=self.runner
        )
        self.assertEqual(result['status'], 'queued')
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))

    def test_owner_cannot_read_another_actors_job(self):
        job = self.queue()
        with self.assertRaises(RuntimeError):
            management.operation_status(
                self.root, job['operation_id'], '@other:example.org', runner=self.runner
            )

    def test_worker_rechecks_live_role_before_mutating(self):
        job = self.queue()
        denied = Mock(side_effect=RuntimeError('FICTIONAL_SECRET'))
        with (
            patch('server_management.authorize_current', denied),
            patch('server_management.run_control') as mutation,
        ):
            management.run_job(self.root, job['operation_id'])
        mutation.assert_not_called()
        denied.assert_called_once_with(self.root, self.actor)
        result = management.operation_status(
            self.root, job['operation_id'], self.actor, runner=self.runner
        )
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))

    def test_worker_finishes_once_and_notices_use_durable_outbox_key(self):
        job = self.queue()
        with (
            patch('server_management.authorize_current'),
            patch(
                'server_management.run_control', return_value={'ok': True, 'operation': 'backup'}
            ),
        ):
            management.run_job(self.root, job['operation_id'])
        with self.assertRaises(RuntimeError):
            management.run_job(self.root, job['operation_id'])
        panel = SimpleNamespace(root=self.root, notice=Mock())
        management.operation_notices(panel, runner=self.runner)
        key, body = panel.notice.call_args.args
        self.assertEqual(key, 'server-operation:' + job['operation_id'])
        self.assertIn('Завершена', body)

    def test_update_tracks_detached_release_result_without_private_detail(self):
        state_store.write_json(
            self.root / 'installation.json', {'backend': 'portable', 'domain': 'example.org'}
        )
        job = self.queue('update', argument='1.2.1')
        with (
            patch('server_management.authorize_current'),
            patch('release_update.queue', return_value={'status': 'queued'}) as updater,
        ):
            management.run_job(self.root, job['operation_id'])
        updater.assert_called_once_with(self.root, '1.2.1', '$one')
        state_store.write_json(
            self.root / 'data/admin-panel/update.json',
            {
                'id': hashlib.sha256(b'$one').hexdigest(),
                'state': 'complete',
                'version': '1.2.1',
                'result': {'success': True, 'installed': '1.2.1', 'detail': 'FICTIONAL_SECRET'},
            },
        )
        result = management.operation_status(
            self.root, job['operation_id'], self.actor, runner=self.runner
        )
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['result']['ok'])
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))

    def test_host_resources_measure_cpu_and_preserve_unavailable_independently(self):
        files = {
            '/proc/meminfo': 'MemTotal: 2048 kB\nMemAvailable: 512 kB\nSwapTotal: 1024 kB\nSwapFree: 256 kB\n',
            '/proc/uptime': '90000.1 0',
        }
        with (
            patch.object(Path, 'read_text', lambda path: files[str(path)]),
            patch.object(management, 'cpu_sample', side_effect=[(100, 50), (200, 75)]),
            patch.object(management.time, 'sleep') as sleep,
            patch.object(management.os, 'getloadavg', return_value=(0.3, 0.2, 0.1)),
            patch.object(management.os, 'cpu_count', return_value=2),
            patch.object(
                management.shutil,
                'disk_usage',
                return_value=SimpleNamespace(total=100, used=70, free=30),
            ),
        ):
            result = management.host_statistics(self.root)
        self.assertEqual(result['cpu_used_percent'], 75)
        self.assertEqual(result['memory_used_bytes'], 1536 * 1024)
        self.assertEqual(result['swap_used_bytes'], 768 * 1024)
        self.assertEqual(result['disk_free_bytes'], 30)
        self.assertEqual(result['uptime_seconds'], 90000)
        sleep.assert_called_once_with(0.1)
        with (
            patch.object(Path, 'read_text', side_effect=OSError),
            patch.object(management.os, 'getloadavg', return_value=(0, 0, 0)),
            patch.object(management.os, 'cpu_count', return_value=None),
            patch.object(management.shutil, 'disk_usage', side_effect=OSError),
        ):
            self.assertEqual(management.host_statistics(self.root), {'cpu_load': 0})

    def test_cpu_counters_do_not_double_count_guest_and_reject_reset(self):
        with patch.object(Path, 'read_text', return_value='cpu 10 20 30 40 50 60 70 80 90 100\n'):
            self.assertEqual(management.cpu_sample(), (360, 90))
        for samples in ([(100, 50), (100, 50)], [(100, 50), (90, 40)]):
            with (
                patch.object(management, 'cpu_sample', side_effect=samples),
                patch.object(management.time, 'sleep'),
            ):
                self.assertNotIn('cpu_used_percent', management.host_statistics(self.root))

    def test_status_includes_resources_for_portable_and_adopted_installations(self):
        for kind in ('portable', 'existing-compose'):
            with self.subTest(kind=kind):
                state_store.write_json(self.root / 'installation.json', {'backend': kind})
                response = subprocess.CompletedProcess(
                    [],
                    0,
                    b'{"check":"server_status","containers":[{"name":"synapse","running":true}]}',
                )
                with patch.object(
                    management, 'host_statistics', return_value={'cpu_used_percent': 25}
                ):
                    result = management.run_control(
                        {'stack_root': str(self.root), 'management_actions': ['status']},
                        'status',
                        runner=Mock(return_value=response),
                    )
                self.assertTrue(result['ok'])
                self.assertEqual(result['resources'], {'cpu_used_percent': 25})

    def test_status_reuses_safe_metrics_from_child_without_second_sample(self):
        response = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    'check': 'existing_status',
                    'containers': [{'name': 'synapse', 'running': True}],
                    'resources': {
                        'cpu_used_percent': 25,
                        'secret': 'PRIVATE_MARKER',
                        'cpu_load': 'unsafe',
                    },
                }
            ),
        )
        with patch.object(management, 'host_statistics') as sample:
            result = management.run_control(
                {'stack_root': str(self.root), 'management_actions': ['status']},
                'status',
                runner=Mock(return_value=response),
            )
        sample.assert_not_called()
        self.assertEqual(result['resources'], {'cpu_used_percent': 25})
        self.assertNotIn('PRIVATE_MARKER', json.dumps(result))

    def test_statistics_failure_keeps_counters_unknown(self):
        with (
            patch('admin_panel.Panel', side_effect=RuntimeError('FICTIONAL_SECRET')),
            patch('server_management.host_statistics', return_value={'disk_free_bytes': 17}),
        ):
            result = management.statistics(self.root, 'week')
        self.assertFalse(result['ok'])
        self.assertEqual(result['statistics']['disk_free_bytes'], 17)
        self.assertNotIn('messages', result['statistics'])
        self.assertNotIn('registrations', result['statistics'])
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))

    def test_restart_preserves_stopped_services_and_checks_result(self):
        fake = SimpleNamespace(
            lock=lambda: nullcontext(),
            require_no_pending=Mock(),
            validate=Mock(),
            services={'running-one': (), 'stopped-two': ()},
            projects=[{'name': 'owned', 'services': {'one': 'running-one', 'two': 'stopped-two'}}],
            container=lambda name: {'running': name == 'running-one', 'health': 'healthy'},
            compose=lambda project, *args: ['docker', 'compose', '-p', project['name'], *args],
            run=Mock(),
            check=Mock(return_value=0),
        )
        with patch('existing_compose.Installation', return_value=fake):
            result = management.restart(self.root)
        self.assertTrue(result['ok'])
        command = fake.run.call_args.args[0]
        self.assertEqual(command[-1], 'one')
        self.assertNotIn('two', command)
        fake.validate.assert_called_once()
        fake.require_no_pending.assert_called_once()
        fake.check.assert_called_once()

    def test_restart_waits_for_warmup_and_has_bounded_failure(self):
        check = Mock(side_effect=[False, False, True])
        sleeper = Mock()
        management.wait_ready(check, seconds=10, sleeper=sleeper)
        self.assertEqual(sleeper.call_count, 2)
        with self.assertRaises(RuntimeError):
            management.wait_ready(lambda: False, seconds=4, sleeper=sleeper)

    def test_native_authorization_uses_live_mas_without_federation_state(self):
        state_store.write_json(
            self.root / 'installation.json', {'backend': 'portable', 'domain': 'example.org'}
        )
        (self.root / 'secrets').mkdir(mode=0o700)
        state_store.write_json(
            self.root / 'secrets/referral-client.json',
            {'client_id': 'fictional', 'client_secret': 'FICTIONAL_SECRET'},
        )
        (self.root / 'federation_admin_api.py').write_text('trusted script')
        with (
            patch('lifecycle.configure') as configure,
            patch('lifecycle.ownership_check') as ownership,
            patch('lifecycle.dc', return_value='{"authorized":true}') as execute,
        ):
            management.authorize_current(self.root, self.actor)
        configure.assert_called_once_with(self.root, 'server-authorize')
        ownership.assert_called_once()
        payload = json.loads(execute.call_args.kwargs['stdin'])
        self.assertEqual(payload['username'], 'owner')
        self.assertEqual(execute.call_args.args[:4], ('exec', '-T', 'synapse', 'python'))
        self.assertFalse((self.root / 'data/federation').exists())
        with (
            patch('lifecycle.configure'),
            patch('lifecycle.ownership_check'),
            patch('lifecycle.dc', return_value='{"authorized":false}'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'Права администратора'):
                management.authorize_current(self.root, self.actor)

    def test_adopted_authorization_queries_active_user_and_current_role(self):
        api = SimpleNamespace(
            matrix_user=Mock(return_value={'id': 'fictional-user'}), administrator_user=Mock()
        )
        with (
            patch('existing_compose.Installation'),
            patch('existing_features.mas_api', return_value=api),
        ):
            management.authorize_current(self.root, self.actor)
        api.matrix_user.assert_called_once_with(self.actor, 'example.org')
        api.administrator_user.assert_called_once_with('fictional-user')


if __name__ == '__main__':
    unittest.main()
