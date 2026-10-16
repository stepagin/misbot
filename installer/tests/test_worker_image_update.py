"""Image transitions preserve worker data and can resume an interrupted rollback."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telegram_container as worker
from existing_compose import Error
import test_container_runtime as runtime_tests

IMAGE = runtime_tests.IMAGE

NEW_IMAGE = 'sha256:' + 'b' * 64


class WorkerImageTests(unittest.TestCase):
    def fixture(self, folder):
        root = Path(folder) / 'installation'
        root.mkdir()
        installation = runtime_tests.RuntimeTests().fixture(root)
        installation.state['existing'] = installation.model
        installation.control = root / '.misbot'
        (installation.control / 'pins').mkdir(parents=True)
        installation.projects = [
            {
                'name': 'fictional-runtime',
                'directory': 'runtime',
                'services': {'telegram-worker': 'misbot-telegram-worker'},
            }
        ]
        installation.compose = lambda project, *args: ['docker', 'compose', *args]
        installation.state['existing']['images']['misbot-telegram-worker'] = {
            'id': IMAGE,
            'digest': IMAGE,
        }
        (root / 'installation.json').write_text(json.dumps(installation.state))
        pin = installation.control / 'pins/fictional-runtime.json'
        pin.write_text(
            json.dumps({'services': {'telegram-worker': {'image': IMAGE, 'pull_policy': 'never'}}})
        )
        data = root / 'runtime/telegram/data/worker.sqlite3'
        with sqlite3.connect(data) as db:
            db.execute('CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('CREATE TABLE users (uid INTEGER PRIMARY KEY, server TEXT, waiting TEXT)')
            db.execute("INSERT INTO state VALUES ('offset','12345')")
        data.chmod(0o600)
        source = Path(folder) / 'source'
        source.mkdir()
        entries = {'telegram_container.py': source / 'telegram_container.py'}
        (root / 'telegram-bot').mkdir()
        for name in worker.WORKER_SOURCES:
            old = root / 'telegram-bot' / name
            text = (
                'FROM python:fictional@sha256:' + 'c' * 64 + '\n'
                if name == 'Dockerfile'
                else '# old\n'
            )
            old.write_text(text)
            target = source / name
            target.write_text(text)
            entries['telegram-bot/' + name] = target
        stage = installation.control / 'source-updates/test'
        stage.mkdir(parents=True)
        return installation, entries, stage, data

    def prepare(self, installation, entries, stage):
        def run(args, **kwargs):
            if args[:2] == ['docker', 'build']:
                Path(args[args.index('--iidfile') + 1]).write_text(NEW_IMAGE)
            return 0, b''

        installation.run.side_effect = run

        def private(path):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            return path

        with (
            patch.object(worker, 'private_directory', side_effect=private),
            patch.object(worker, 'save', side_effect=lambda p, v: p.write_text(json.dumps(v))),
        ):
            return worker.prepare_update(installation, entries, stage)

    def test_unchanged_sources_do_not_rebuild_and_missing_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, entries, stage, data = self.fixture(folder)
            self.assertFalse(worker.update_needed(installation, entries))
            entries['telegram-bot/worker.py'].write_text('# new\n')
            import telegram_backup

            regular = telegram_backup.regular
            with patch.object(
                telegram_backup, 'regular', side_effect=lambda p, uid: regular(p, os.getuid())
            ):
                self.assertTrue(worker.update_needed(installation, entries))
            del entries['telegram-bot/common.py']
            with self.assertRaises(Error):
                worker.update_needed(installation, entries)
            installation.run.assert_not_called()

    def test_build_contains_only_public_files_and_probe_cannot_poll_or_access_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, entries, stage, data = self.fixture(folder)
            record = self.prepare(installation, entries, stage)
            self.assertEqual(record['image'], NEW_IMAGE)
            self.assertEqual(
                {p.name for p in (stage / 'worker-build').iterdir()}, set(worker.WORKER_SOURCES)
            )
            probe = installation.run.call_args.args[0]
            self.assertIn('none', probe)
            self.assertIn('--read-only', probe)
            self.assertEqual(probe[-2:], ['worker.py', '--help'])
            self.assertNotIn('--mount', probe)

    def test_mutable_build_base_is_refused_before_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, entries, stage, data = self.fixture(folder)
            entries['telegram-bot/Dockerfile'].write_text('FROM python:latest\n')
            with self.assertRaises(Error):
                self.prepare(installation, entries, stage)
            installation.run.assert_not_called()

    def test_image_pin_transition_and_repeat_recovery_retain_latest_sqlite_records(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, entries, stage, data = self.fixture(folder)
            record = self.prepare(installation, entries, stage)
            with patch.object(worker, 'save', side_effect=lambda p, v: p.write_text(json.dumps(v))):
                worker.publish_update(installation, stage, record)
            self.assertEqual(
                installation.model['images']['misbot-telegram-worker']['id'], NEW_IMAGE
            )
            with sqlite3.connect(data) as db:
                db.execute("UPDATE state SET value='12346' WHERE key='offset'")
            worker.recover_update(installation, stage, record)
            worker.recover_update(installation, stage, record)
            self.assertEqual(installation.model['images']['misbot-telegram-worker']['id'], IMAGE)
            with sqlite3.connect(data) as db:
                self.assertEqual(db.execute('SELECT value FROM state').fetchone()[0], '12346')
            self.assertIn('--wait', installation.run.call_args.args[0])

    def test_changed_checkpoint_or_foreign_configuration_is_preserved(self):
        for target in ('worker-state.before', 'installation.json'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                installation, entries, stage, data = self.fixture(folder)
                record = self.prepare(installation, entries, stage)
                path = stage / target if target.endswith('.before') else installation.root / target
                path.write_bytes(b'foreign change')
                installation.run.reset_mock()
                with self.assertRaises(Error):
                    worker.recover_update(installation, stage, record)
                installation.run.assert_not_called()
                self.assertEqual(path.read_bytes(), b'foreign change')

    def test_failed_image_start_can_restore_original_pin(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, entries, stage, data = self.fixture(folder)
            record = self.prepare(installation, entries, stage)

            def failing(args, **kwargs):
                if args[:2] == ['docker', 'compose']:
                    raise Error('fictional unhealthy worker')
                return 0, b''

            installation.run.side_effect = failing
            with self.assertRaises(Error):
                worker.publish_update(installation, stage, record)
            installation.run.side_effect = lambda *args, **kwargs: (0, b'')
            worker.recover_update(installation, stage, record)
            self.assertEqual(installation.model['images']['misbot-telegram-worker']['id'], IMAGE)
            with sqlite3.connect(data) as db:
                self.assertEqual(db.execute('SELECT value FROM state').fetchone()[0], '12345')

    def test_unknown_sqlite_schema_is_rejected_without_build_or_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, entries, stage, data = self.fixture(folder)
            entries['telegram-bot/worker.py'].write_text('# changed\n')
            with sqlite3.connect(data) as db:
                db.execute('PRAGMA user_version=99')
            import telegram_backup

            regular = telegram_backup.regular
            with patch.object(
                telegram_backup, 'regular', side_effect=lambda p, uid: regular(p, os.getuid())
            ):
                with self.assertRaises(Error):
                    worker.update_needed(installation, entries)
            installation.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
