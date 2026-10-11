import json
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telegram_backup as backup


class TelegramBackupTests(unittest.TestCase):
    def fixture(self, base):
        root, worker, destination = base / 'root', base / 'worker', base / 'backup'
        control = root / '.misbot/telegram'
        (control / 'data').mkdir(parents=True)
        worker.mkdir(); destination.mkdir()
        for name in backup.CONFIG_FILES:
            path = control / name
            path.write_bytes(b'FICTIONAL_CONFIGURATION')
            path.chmod(0o600)
        for path in (control / 'data/bot-control.sqlite3', worker / 'worker.sqlite3'):
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE state (value TEXT)')
                db.execute("INSERT INTO state VALUES ('committed')")
            path.chmod(0o600)
        return root, worker, destination, {'telegram_control': {
            'directory': '.misbot/telegram', 'worker_directory': str(worker)}}

    def test_sqlite_snapshot_includes_committed_state_and_private_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root, worker, destination, model = self.fixture(Path(folder))
            # Fictional fixtures are owned by the current test runner, not production UIDs.
            original_regular = backup.regular
            with patch.object(backup, 'regular', side_effect=lambda path, owner: original_regular(path, os.getuid())):
                metadata = backup.snapshot(root, destination, model)
            self.assertEqual(set(metadata['sha256']), backup.MEMBERS)
            self.assertEqual((destination / 'telegram/config/worker.json').stat().st_mode & 0o777, 0o600)
            live = root / '.misbot/telegram/data/bot-control.sqlite3'
            with sqlite3.connect(live) as db:
                db.execute("UPDATE state SET value='newer'")
            with sqlite3.connect(destination / 'telegram/control.sqlite3') as db:
                self.assertEqual(db.execute('SELECT value FROM state').fetchone()[0], 'committed')
            backup.verify(destination, {'telegram': metadata}, set(backup.MEMBERS))
            (destination / 'telegram/config/worker.json').write_bytes(b'CHANGED')
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                backup.verify(destination, {'telegram': metadata}, set(backup.MEMBERS))

    def test_sources_with_symlinks_or_public_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, worker, destination, model = self.fixture(Path(folder))
            config = root / '.misbot/telegram/worker.json'
            config.chmod(0o644)
            with self.assertRaises(RuntimeError):
                backup.regular(config, os.getuid())
            config.chmod(0o600)
            other = config.with_name('alias.json')
            other.symlink_to(config)
            with self.assertRaises(RuntimeError):
                backup.regular(other, os.getuid())

    def test_container_backup_uses_actual_worker_config_and_preserves_sqlite_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root, worker, destination, model = self.fixture(Path(folder))
            runtime = root / 'runtime/telegram'
            runtime.mkdir(parents=True)
            worker.rename(runtime / 'data')
            config = runtime / 'worker.json'
            config.write_bytes(b'FICTIONAL_ACTIVE_CONTAINER_CONFIG')
            config.chmod(0o600)
            model['telegram_control'].update(worker_container='misbot-telegram-worker',
                worker_config='runtime/telegram/worker.json', worker_directory=str(runtime/'data'),
                socket_directory='/run/misbot-telegram')
            self.assertEqual(backup.raw_data_exclusion(root, model), str(runtime/'data').lstrip('/'))
            regular = backup.regular
            with patch.object(backup, 'regular', side_effect=lambda path, owner: regular(path, os.getuid())):
                metadata = backup.snapshot(root, destination, model)
            self.assertEqual((destination/'telegram/config/worker.json').read_bytes(), config.read_bytes())
            with sqlite3.connect(destination/'telegram/worker.sqlite3') as db:
                self.assertEqual(db.execute('SELECT value FROM state').fetchone()[0], 'committed')
            backup.verify(destination, {'telegram': metadata}, set(backup.MEMBERS))

    def test_raw_exclusion_retains_unrelated_data_and_legacy_external_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root, worker, _, model = self.fixture(Path(folder))
            self.assertIsNone(backup.raw_data_exclusion(root, model))
            self.assertIsNone(backup.raw_data_exclusion(root, {}))
            model['telegram_control']['worker_directory'] = str(root)
            with self.assertRaises(RuntimeError): backup.raw_data_exclusion(root, model)

    def test_legacy_archive_and_incomplete_telegram_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(backup.snapshot(Path(folder), Path(folder), {}))
            backup.verify(Path(folder), {}, {'database/synapse.dump'})
            with self.assertRaises(RuntimeError):
                backup.verify(Path(folder), {}, {'telegram/control.sqlite3'})
            with self.assertRaises(RuntimeError):
                backup.verify(Path(folder), {'telegram': {'format': 1, 'sha256': {}}}, set())


if __name__ == '__main__':
    unittest.main()
