"""Writer pause/recovery with real private checkpoints and simulated Compose only."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_writers
import state_store


class BackupWriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.metadata = {'domain': 'fictional.example.org'}
        state_store.write_json(self.root / 'installation.json', self.metadata)
        self.identity = self.root / 'identity.txt'
        self.identity.write_text('FICTIONAL_IDENTITY_DO_NOT_REPLACE')
        self.compose = Mock()
        self.wait_ready = Mock()
        self.emit = Mock()
        self.runtime = backup_writers.WriterRuntime(
            self.root,
            lambda: self.metadata,
            lambda: [
                {'Service': 'synapse', 'State': 'running'},
                {'Service': 'mas', 'State': 'exited'},
                {'Service': 'caddy', 'State': 'running'},
            ],
            self.compose,
            self.wait_ready,
            self.emit,
        )
        self.writers = backup_writers.BackupWriters(self.runtime)

    def record(self, **changes):
        record = {
            'format': 1,
            'server_name': self.metadata['domain'],
            'writers': ['synapse'],
            'keep_stopped': False,
            'pid': 123456,
            'started': 'fictional-dead-process',
        }
        record.update(changes)
        state_store.write_json(self.root / 'backup-writers.json', record)
        return record

    def test_journal_precedes_stop_and_nested_snapshot_failure_restarts_once(self):
        def compose(*args):
            record = state_store.backup_pause_record(self.root)
            self.assertEqual(record['writers'], ['synapse'])
            self.assertFalse(record['keep_stopped'])

        self.compose.side_effect = compose
        with self.assertRaisesRegex(RuntimeError, 'snapshot failed'):
            with self.writers.pause():
                with self.writers.pause(leave_stopped=True):
                    raise RuntimeError('snapshot failed')
        self.assertEqual(
            [call.args for call in self.compose.call_args_list],
            [('stop', '-t', '30', 'synapse'), ('up', '-d', 'synapse')],
        )
        self.assertIsNone(state_store.backup_pause_record(self.root))
        self.assertFalse(self.writers.paused)
        self.assertEqual(json.loads((self.root / 'installation.json').read_text()), self.metadata)
        self.assertEqual(self.identity.read_text(), 'FICTIONAL_IDENTITY_DO_NOT_REPLACE')

    def test_interrupted_readiness_keeps_checkpoint_then_recovery_is_idempotent(self):
        self.wait_ready.side_effect = state_store.OperationInterrupted()
        with self.assertRaises(state_store.OperationInterrupted):
            with self.writers.pause():
                pass
        record = state_store.backup_pause_record(self.root)
        self.assertEqual(record['format'], 1)
        self.assertEqual(record['server_name'], self.metadata['domain'])
        self.assertFalse(self.writers.paused)
        self.emit.assert_called_once_with(
            'backup_recovery_pending',
            command='sudo bash ' + str(self.root / 'install.sh') + ' backup-recover',
        )
        self.wait_ready.side_effect = None
        self.compose.reset_mock()
        with patch.object(state_store, 'process_identity', side_effect=FileNotFoundError):
            result = self.writers.recover()
            repeated = self.writers.recover()
        self.assertEqual(result.operation.code, 'backup_writers_resumed')
        self.assertEqual(
            result.legacy_fields(),
            {
                'pending': False,
                'changed': True,
                'writers': ['synapse'],
                'kept_stopped': False,
                'next_action': 'Run install.sh check, then retry the backup',
            },
        )
        self.assertEqual(repeated.operation.code, 'backup_recovery_not_needed')
        self.assertEqual(repeated.legacy_fields(), {'pending': False, 'changed': False})
        self.compose.assert_called_once_with('up', '-d', 'synapse')
        self.assertIsNone(state_store.backup_pause_record(self.root))

    def test_checkpoint_write_failure_does_not_stop_services(self):
        with patch.object(state_store, 'write_json', side_effect=OSError('fictional disk failure')):
            with self.assertRaises(OSError):
                with self.writers.pause():
                    self.fail('Snapshot must not start')
        self.compose.assert_not_called()
        self.assertFalse(self.writers.paused)

    def test_foreign_identity_and_live_owner_are_refused_before_compose(self):
        for changes, process, message in (
            ({'server_name': 'FICTIONAL_SECRET_FOREIGN_VALUE'}, None, 'another Matrix identity'),
            ({}, (123456, 'fictional-dead-process'), 'still exists'),
        ):
            record = self.record(**changes)
            with patch.object(state_store, 'process_identity', return_value=process):
                with self.assertRaisesRegex(backup_writers.OperationError, message) as error:
                    self.writers.recover()
            self.assertEqual(error.exception.result.stage.value, 'backup_recovery')
            self.assertEqual(error.exception.result.status.value, 'failed')
            self.assertTrue(error.exception.result.next_actions)
            self.assertNotIn('FICTIONAL_SECRET_FOREIGN_VALUE', str(error.exception))
            self.compose.assert_not_called()
            self.assertEqual(state_store.backup_pause_record(self.root), record)

    def test_failed_recovery_keeps_the_exact_checkpoint_for_retry(self):
        original = self.record()
        self.wait_ready.side_effect = state_store.OperationInterrupted()
        with patch.object(state_store, 'process_identity', side_effect=FileNotFoundError):
            with self.assertRaises(state_store.OperationInterrupted):
                self.writers.recover()
            self.assertEqual(state_store.backup_pause_record(self.root), original)
            self.wait_ready.side_effect = None
            result = self.writers.recover()
        self.assertTrue(result.changed)
        self.assertEqual(self.identity.read_text(), 'FICTIONAL_IDENTITY_DO_NOT_REPLACE')
        self.assertIsNone(state_store.backup_pause_record(self.root))

    def test_recovery_keeps_migration_source_and_staged_target_stopped(self):
        for flag in ('migration_frozen', 'restore_pending'):
            self.metadata = {'domain': 'fictional.example.org', flag: True}
            self.record()
            self.compose.reset_mock()
            with patch.object(state_store, 'process_identity', side_effect=FileNotFoundError):
                self.writers.recover()
                self.writers.recover()
            self.compose.assert_called_once_with('stop', '-t', '30', 'synapse')
            self.wait_ready.assert_not_called()
            self.assertTrue(self.metadata[flag])

    def test_two_explicit_installations_share_no_pause_state(self):
        other_root = self.root / 'second'
        other_root.mkdir()
        other_compose = Mock()
        other = backup_writers.BackupWriters(
            backup_writers.WriterRuntime(
                other_root,
                lambda: {'domain': 'second.example.org'},
                self.runtime.services,
                other_compose,
                Mock(),
                Mock(),
            )
        )
        with self.writers.pause():
            with other.pause():
                self.assertEqual(
                    state_store.backup_pause_record(self.root)['server_name'],
                    'fictional.example.org',
                )
                self.assertEqual(
                    state_store.backup_pause_record(other_root)['server_name'], 'second.example.org'
                )
            self.assertIsNotNone(state_store.backup_pause_record(self.root))
        for compose in (self.compose, other_compose):
            self.assertEqual(
                [call.args for call in compose.call_args_list],
                [('stop', '-t', '30', 'synapse'), ('up', '-d', 'synapse')],
            )
