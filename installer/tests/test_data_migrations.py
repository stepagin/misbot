"""Real old databases, interrupted transactions and verified release updates."""

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import data_migrations as migrations
import state_store
import update
from welcome_message import DEFAULT_WELCOME, WelcomeContext

# Frozen source text from the old runtime, independent of the new matcher.
STOCK_WELCOME = (
    'Добро пожаловать на сервер {domain}! Здесь можно общаться в личных '
    'и групповых чатах, делиться файлами и звонить через Matrix. '
    'Личные чаты поддерживают сквозное шифрование. Приглашения: @referral-bot:{domain}. '
    'Этот бот присылает новости сервера и не отвечает на сообщения.'
)


def upgraded_text(domain='matrix.example.org'):
    return WelcomeContext(
        domain, domain, '@referral-bot:' + domain, '@matrix-admin:' + domain
    ).render(DEFAULT_WELCOME)


class DataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'data/admin-panel/panel.sqlite'
        state_store.write_json(self.root / 'installation.json', {'domain': 'matrix.example.org'})

    def legacy(self, welcome='MiXeD Привет'):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            # Frozen layout of the old runtime, independent of migration SQL.
            db.executescript("""
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE users (mxid TEXT PRIMARY KEY, created INTEGER, active INTEGER);
                CREATE TABLE outbox (id TEXT PRIMARY KEY, recipient TEXT NOT NULL,
                    via TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0);
                CREATE INDEX outbox_pending ON outbox(state,next_at);
            """)
            db.execute('INSERT INTO settings VALUES (?,?)', ('welcome', json.dumps(welcome)))
            db.execute('INSERT INTO settings VALUES (?,?)', ('notified_version', '"1.1.0"'))
            db.execute('INSERT INTO users VALUES (?,?,?)', ('@alice:fictional.example', 100, 1))
            db.execute(
                'INSERT INTO outbox VALUES (?,?,?,?,?,?,?)',
                ('delivery-id', '@alice:fictional.example', 'welcome', 'MiXeD', 'sent', 1, 0),
            )

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return {
                table: db.execute('SELECT * FROM ' + table).fetchall()
                for table in ('settings', 'users', 'outbox')
            }

    def test_legacy_rows_and_delivery_markers_survive_repeat(self):
        self.legacy()
        before = self.rows()
        self.assertEqual(migrations.check(self.root), 0)
        self.assertEqual(migrations.migrate(self.root), (0, 1))
        self.assertEqual(migrations.migrate(self.root), (1, 1))
        self.assertEqual(migrations.check(self.root), 1)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        # A schema-0 runtime can still write without losing its newer state.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE settings SET value='\"edited after migration\"' WHERE key='welcome'")
        migrations.migrate(self.root)
        self.assertIn(('welcome', '"edited after migration"'), self.rows()['settings'])

    def test_preflight_does_not_create_data(self):
        self.assertEqual(migrations.check(self.root), 0)
        self.assertFalse((self.root / 'data').exists())

    def test_stock_welcome_variants_upgrade_once_with_original_checkpoint_and_no_deliveries(self):
        state_store.write_json(self.root / 'installation.json', {'domain': 'matrix.example.org'})
        variants = (
            STOCK_WELCOME,
            STOCK_WELCOME.replace('@referral-bot:{domain}. ', '@referral-bot:{domain} '),
            STOCK_WELCOME.replace('@referral-bot:{domain}. ', '@referral-bot:{domain}\n'),
        )
        for index, old in enumerate(
            variants + tuple(body.replace('{domain}', 'matrix.example.org') for body in variants)
        ):
            with self.subTest(variant=index):
                self.path.unlink(missing_ok=True)
                self.legacy(old)
                before = self.rows()
                checkpoint = self.root / ('checkpoint-' + str(index))
                self.assertEqual(migrations.migrate(self.root, checkpoint), (0, 1))
                after = self.rows()
                expected = dict(before)
                expected['settings'] = [
                    (key, json.dumps(upgraded_text()) if key == 'welcome' else value)
                    for key, value in before['settings']
                ]
                self.assertEqual(after, expected)
                with sqlite3.connect(checkpoint / 'data-migrations/panel.sqlite') as db:
                    self.assertEqual(
                        db.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()[0],
                        json.dumps(old),
                    )
                saved = self.path.read_bytes()
                self.assertEqual(migrations.migrate(self.root), (1, 1))
                self.assertEqual(migrations.migrate(self.root), (1, 1))
                self.assertEqual(self.path.read_bytes(), saved)
                self.assertEqual(self.rows(), after)

    def test_custom_welcome_and_close_variants_are_never_replaced(self):
        state_store.write_json(self.root / 'installation.json', {'domain': 'matrix.example.org'})
        for custom in (
            'Мой MiXeD текст для {domain}.\nhttps://example.org/',
            STOCK_WELCOME + '\nДополнительная инструкция.',
            STOCK_WELCOME.replace('Приглашения:', 'Наши приглашения:'),
            STOCK_WELCOME.replace('{domain}', 'foreign.example'),
            DEFAULT_WELCOME,
            42,
            {'message': 'Retain invalid values too'},
        ):
            with self.subTest(custom=custom):
                self.path.unlink(missing_ok=True)
                self.legacy(custom)
                before = self.rows()
                migrations.migrate(self.root)
                migrations.migrate(self.root)
                self.assertEqual(self.rows(), before)

    def test_welcome_failure_rolls_back_setting_schema_and_history_then_retry_succeeds(self):
        self.legacy(STOCK_WELCOME)
        before = self.rows()
        inspect = migrations.inspect_database

        def fail_after_replacement(db):
            version = inspect(db)
            saved = db.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()
            if saved and json.loads(saved[0]) == upgraded_text():
                raise RuntimeError('fictional interruption before commit')
            return version

        checkpoint = self.root / 'checkpoint'
        with patch.object(migrations, 'inspect_database', side_effect=fail_after_replacement):
            with self.assertRaisesRegex(RuntimeError, 'before commit'):
                migrations.migrate(self.root, checkpoint)
        self.assertEqual(self.rows(), before)
        self.assertEqual(migrations.check(self.root), 0)
        with sqlite3.connect(checkpoint / 'data-migrations/panel.sqlite') as db:
            self.assertEqual(
                json.loads(
                    db.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()[0]
                ),
                STOCK_WELCOME,
            )
        self.assertEqual(migrations.migrate(self.root), (0, 1))
        self.assertEqual(self.rows()['outbox'], before['outbox'])
        self.assertIn(('welcome', json.dumps(upgraded_text())), self.rows()['settings'])

    def test_invalid_configured_identity_retains_old_text_and_checkpoint_until_corrected(self):
        self.legacy(STOCK_WELCOME)
        before = self.rows()
        config = self.root / 'config/referral-bot.json'
        config.parent.mkdir(mode=0o700)
        state_store.write_json(config, {'user_id': 'FICTIONAL_SECRET'})
        checkpoint = self.root / 'checkpoint'
        with self.assertRaisesRegex(RuntimeError, '^Invalid local welcome identity$'):
            migrations.migrate(self.root, checkpoint)
        self.assertEqual(self.rows(), before)
        self.assertEqual(migrations.check(self.root), 0)
        self.assertTrue((checkpoint / 'data-migrations/panel.sqlite').is_file())
        state_store.write_json(config, {'user_id': '@referral-bot:matrix.example.org'})
        migrations.migrate(self.root)
        self.assertIn(('welcome', json.dumps(upgraded_text())), self.rows()['settings'])
        self.assertEqual(self.rows()['outbox'], before['outbox'])

    def test_exit_during_welcome_upgrade_rolls_back_and_recovers_without_duplicate_delivery(self):
        self.legacy(STOCK_WELCOME)
        before = self.rows()
        script = """import json, os, sys
import data_migrations as migrations
from welcome_message import DEFAULT_WELCOME
inspect = migrations.inspect_database
def exit_after_replacement(db):
    version = inspect(db)
    saved = db.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()
    if saved and json.loads(saved[0]).startswith('Здравствуйте. Добро пожаловать в '):
        os._exit(23)
    return version
migrations.inspect_database = exit_after_replacement
migrations.migrate(sys.argv[1])
"""
        result = subprocess.run(
            [sys.executable, '-c', script, str(self.root)],
            cwd=Path(migrations.__file__).parent,
        )
        self.assertEqual(result.returncode, 23)
        self.assertEqual(migrations.check(self.root), 0)
        self.assertEqual(self.rows(), before)
        migrations.migrate(self.root)
        migrations.migrate(self.root)
        self.assertEqual(self.rows()['outbox'], before['outbox'])
        self.assertIn(('welcome', json.dumps(upgraded_text())), self.rows()['settings'])

    def test_online_checkpoint_contains_previous_schema_and_is_not_overwritten(self):
        self.legacy()
        checkpoint = self.root / 'checkpoint'
        migrations.migrate(self.root, checkpoint)
        saved = checkpoint / 'data-migrations/panel.sqlite'
        with sqlite3.connect(saved) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM outbox').fetchone()[0], 'sent')
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        before = saved.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            migrations.migrate(self.root, checkpoint)
        self.assertEqual(saved.read_bytes(), before)

    def test_unknown_schema_or_layout_leaves_database_unchanged(self):
        for case in ('version', 'columns'):
            with self.subTest(case=case):
                self.path.unlink(missing_ok=True)
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with sqlite3.connect(self.path) as db:
                    db.execute(
                        'PRAGMA user_version=99'
                        if case == 'version'
                        else 'CREATE TABLE settings (unexpected TEXT)'
                    )
                before = self.path.read_bytes()
                with self.assertRaises(RuntimeError):
                    migrations.check(self.root)
                with self.assertRaises(RuntimeError):
                    migrations.migrate(self.root)
                self.assertEqual(self.path.read_bytes(), before)

    def test_failure_rolls_back_ddl_history_and_version_together(self):
        self.legacy()
        before = self.rows()
        broken = (
            (
                1,
                'admin-panel-v1',
                (
                    *migrations.MIGRATIONS[0][2],
                    'CREATE TABLE interrupted (id INTEGER)',
                    'FICTIONAL INVALID SQL',
                ),
            ),
        )
        with patch.object(migrations, 'MIGRATIONS', broken):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate(self.root)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertNotIn('interrupted', tables)
            self.assertNotIn('misbot_migrations', tables)
        self.assertEqual(self.rows(), before)
        self.assertEqual(migrations.migrate(self.root), (0, 1))

    def test_abrupt_process_exit_recovers_hot_journal_before_schema_check(self):
        self.legacy()
        before = self.rows()
        script = """import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=1')
db.execute('BEGIN IMMEDIATE')
db.execute('CREATE TABLE crashed_transaction (body BLOB)')
db.execute('INSERT INTO crashed_transaction VALUES (zeroblob(1048576))')
db.execute('PRAGMA user_version=99')
os._exit(23)
"""
        result = subprocess.run([sys.executable, '-c', script, str(self.path)], timeout=10)
        self.assertEqual(result.returncode, 23)
        self.assertTrue(Path(str(self.path) + '-journal').exists())
        self.assertEqual(migrations.check(self.root), 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(migrations.migrate(self.root), (0, 1))

    def test_skipping_versions_applies_all_steps_once(self):
        self.legacy()
        steps = (*migrations.MIGRATIONS, (2, 'fixture-v2', ('CREATE TABLE fixture (id INTEGER)',)))
        with (
            patch.object(migrations, 'MIGRATIONS', steps),
            patch.object(migrations, 'SCHEMA_VERSION', 2),
        ):
            self.assertEqual(migrations.migrate(self.root), (0, 2))
            self.assertEqual(migrations.migrate(self.root), (2, 2))
            with sqlite3.connect(self.path) as db:
                self.assertEqual(
                    db.execute('SELECT version FROM misbot_migrations ORDER BY version').fetchall(),
                    [(1,), (2,)],
                )

    def test_published_history_cannot_be_rewritten(self):
        migrations.migrate(self.root)
        changed = ((1, 'changed-id', migrations.MIGRATIONS[0][2]),)
        with patch.object(migrations, 'MIGRATIONS', changed):
            with self.assertRaisesRegex(RuntimeError, 'history differs'):
                migrations.check(self.root)

    def test_missing_intermediate_migration_is_refused_before_data_creation(self):
        with patch.object(migrations, 'SCHEMA_VERSION', 2):
            with self.assertRaisesRegex(RuntimeError, 'sequential'):
                migrations.migrate(self.root)
        self.assertFalse((self.root / 'data').exists())

    def test_database_and_parent_symlinks_are_rejected(self):
        other = self.root / 'other'
        other.mkdir()
        (self.root / 'data').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            migrations.migrate(self.root)
        self.assertEqual(list(other.iterdir()), [])


class ReleaseDataMigrationTests(unittest.TestCase):
    def test_ui_release_updates_previous_versions_without_changing_private_data(self):
        added = {
            'terminal_views.py',
            'ui_actions.py',
            'third-party-licenses.md',
            'welcome_message.py',
            'welcome_settings.py',
        }
        entries = update.validate_manifest(self.source)[0]
        (self.root / 'secrets').mkdir(mode=0o700)
        state_store.write_text(self.root / 'secrets/fictional.key', 'FICTIONAL_KEEP_KEY')
        private_before = {
            name: (self.root / name).read_bytes()
            for name in ('settings.env', 'secrets/fictional.key')
        }
        for previous in ('1.0.0', '1.0.1', '1.1.0', '1.1.1'):
            with self.subTest(previous=previous):
                info = update.release(self.root)
                info['version'] = previous
                (self.root / 'release.json').write_text(json.dumps(info))
                for name in added:
                    (self.root / name).unlink(missing_ok=True)
                (self.root / 'SHA256SUMS').write_text(
                    ''.join(
                        hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                        + '  '
                        + name
                        + '\n'
                        for name in entries
                        if name not in added
                    )
                )
                with (
                    patch.object(update, 'update_resource_check'),
                    patch.object(update, 'verified_backup'),
                    patch.object(update, 'run_up', side_effect=self.start),
                    patch.object(update, 'refresh_command'),
                ):
                    update.perform_update(self.source, self.root, 'apply')
                    update.perform_update(self.source, self.root, 'apply')
                self.assertEqual(
                    update.release(self.root)['version'], update.release(self.source)['version']
                )
                self.assertEqual(migrations.check(self.root), 1)
                for name in added:
                    self.assertEqual((self.root / name).read_bytes(), entries[name].read_bytes())
                self.assertEqual(
                    private_before,
                    {name: (self.root / name).read_bytes() for name in private_before},
                )
                self.assertEqual(
                    json.loads((self.root / 'installation.json').read_text())['domain'],
                    'fictional.example',
                )

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base = Path(cls.temp.name).resolve()
        archive = cls.base / 'release.tar.gz'
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / 'build.py'),
                '--release',
                '--output',
                str(archive),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with tarfile.open(archive) as package:
            package.extractall(cls.base / 'source', filter='data')
        cls.source = cls.base / 'source/matrix-installer'

    def setUp(self):
        self.target = tempfile.TemporaryDirectory()
        self.addCleanup(self.target.cleanup)
        self.root = Path(self.target.name).resolve()
        update.copy_sources(update.validate_manifest(self.source)[0], self.root)
        info = update.release(self.root)
        info.pop('data_schema_version')
        info['version'] = '1.0.0'
        (self.root / 'release.json').write_text(json.dumps(info))
        entries = update.validate_manifest(self.source)[0]
        (self.root / 'SHA256SUMS').write_text(
            ''.join(
                hashlib.sha256((self.root / name).read_bytes()).hexdigest() + '  ' + name + '\n'
                for name in entries
            )
        )
        state_store.write_json(
            self.root / 'installation.json',
            {
                'domain': 'fictional.example',
                'config_format_version': 1,
                'databases_initialized': True,
                'applied_release_version': '1.0.0',
            },
        )
        state_store.write_text(self.root / 'settings.env', 'FICTIONAL_PRIVATE_SETTINGS')

    def start(self, root, log):
        self.assertEqual(migrations.check(root), 1)
        info = json.loads((root / 'installation.json').read_text())
        info.update(
            applied_release_version=update.release(root)['version'],
            applied_release_revision=update.validate_manifest(root)[1],
        )
        state_store.write_json(root / 'installation.json', info)
        return 0

    def test_source_update_upgrades_stock_before_start_and_retains_edit_on_repeat(self):
        state_store.write_text(self.root / 'settings.env', 'DOMAIN=fictional.example\n')
        migrations.migrate(self.root)
        database = self.root / 'data/admin-panel/panel.sqlite'
        old = STOCK_WELCOME.replace('@referral-bot:{domain}. ', '@referral-bot:{domain}\n')
        with sqlite3.connect(database) as db:
            db.execute('INSERT INTO settings VALUES (?,?)', ('welcome', json.dumps(old)))
            db.execute('INSERT INTO settings VALUES (?,?)', ('enabled_at', '100'))
            db.execute('INSERT INTO users VALUES (?,?,?)', ('@alice:fictional.example', 150, 1))
            db.execute(
                'INSERT INTO outbox(id,recipient,via,body,state) VALUES (?,?,?,?,?)',
                ('delivered-before-update', '@alice:fictional.example', 'welcome', 'old', 'sent'),
            )
        expected = upgraded_text('fictional.example')

        def start(root, log):
            with sqlite3.connect(database) as db:
                self.assertEqual(
                    json.loads(
                        db.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()[0]
                    ),
                    expected,
                )
                self.assertEqual(
                    db.execute('SELECT * FROM users').fetchall(),
                    [('@alice:fictional.example', 150, 1)],
                )
                self.assertEqual(
                    db.execute('SELECT id,state FROM outbox').fetchall(),
                    [('delivered-before-update', 'sent')],
                )
            return self.start(root, log)

        with (
            patch.object(update, 'update_resource_check'),
            patch.object(update, 'verified_backup'),
            patch.object(update, 'run_up', side_effect=start),
            patch.object(update, 'refresh_command'),
        ):
            update.perform_update(self.source, self.root, 'apply')
            checkpoints = list(
                (self.root / 'stages/updates').glob('*/data-migrations/panel.sqlite')
            )
            self.assertEqual(len(checkpoints), 1)
            with sqlite3.connect(checkpoints[0]) as db:
                self.assertEqual(
                    json.loads(
                        db.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()[0]
                    ),
                    old,
                )
            expected = 'Изменено владельцем после обновления. {domain}'
            with sqlite3.connect(database) as db:
                db.execute(
                    "UPDATE settings SET value=? WHERE key='welcome'", (json.dumps(expected),)
                )
            saved = database.read_bytes()
            update.perform_update(self.source, self.root, 'apply')
            self.assertEqual(database.read_bytes(), saved)

    def test_adopted_source_migration_is_usable_by_running_domain_only_reader(self):
        metadata = {
            'domain': 'matrix.example.org',
            'backend': 'existing-compose',
            'existing': {'features': {'primary_domain': 'example.org'}},
        }
        state_store.write_json(self.root / 'installation.json', metadata)
        config = self.root / 'config'
        config.mkdir(mode=0o700)
        state_store.write_json(
            config / 'referral-bot.json', {'user_id': '@referral-bot:matrix.example.org'}
        )
        state_store.write_json(
            config / 'admin-panel-identity.json',
            {'administrator_mxid': '@owner:matrix.example.org'},
        )
        site = self.root / '.misbot/features/site.json'
        site.parent.mkdir(parents=True, mode=0o700)
        state_store.write_json(site, {'SITE_NAME': 'example.org'})
        migrations.migrate(self.root)
        database = self.root / 'data/admin-panel/panel.sqlite'
        old = STOCK_WELCOME.replace('@referral-bot:{domain}. ', '@referral-bot:{domain}\n')
        with sqlite3.connect(database) as db:
            db.execute('INSERT INTO settings VALUES (?,?)', ('welcome', json.dumps(old)))
            db.execute(
                'INSERT INTO outbox(id,recipient,via,body,state) VALUES (?,?,?,?,?)',
                ('existing-delivery', '@alice:matrix.example.org', 'welcome', 'retain', 'sent'),
            )
        update.run_data_migrations(
            self.source, self.root, update.release(self.source), 'apply', self.root / 'checkpoint'
        )
        with sqlite3.connect(database) as db:
            setting = json.loads(
                db.execute("SELECT value FROM settings WHERE key='welcome'").fetchone()[0]
            )
        # Frozen rendering contract of the already-running old host controller.
        delivered = setting.replace('{domain}', metadata['domain'])
        expected = (
            upgraded_text()
            .replace('в matrix.example.org.', 'в example.org.')
            .replace('@matrix-admin:', '@owner:')
        )
        self.assertEqual(delivered, expected)
        saved = database.read_bytes()
        update.run_data_migrations(self.source, self.root, update.release(self.source), 'apply')
        self.assertEqual(database.read_bytes(), saved)
        with sqlite3.connect(database) as db:
            self.assertEqual(
                db.execute('SELECT id,state FROM outbox').fetchall(),
                [('existing-delivery', 'sent')],
            )

    def test_verified_backup_precedes_migration_and_migration_precedes_start(self):
        events = []

        def backup(*args):
            self.assertEqual(migrations.check(self.root), 0)
            self.assertFalse((self.root / 'data').exists())
            events.append('backup')

        def start(*args):
            events.append('start')
            return self.start(*args)

        with (
            patch.object(update, 'update_resource_check'),
            patch.object(update, 'verified_backup', side_effect=backup),
            patch.object(update, 'run_up', side_effect=start),
            patch.object(update, 'refresh_command'),
        ):
            update.perform_update(self.source, self.root, 'apply')
        self.assertEqual(events, ['backup', 'start'])
        self.assertFalse((self.root / 'update-journal.json').exists())

    def test_unknown_data_schema_blocks_backup_and_file_changes(self):
        path = self.root / 'data/admin-panel/panel.sqlite'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA user_version=99')
        before = update.validate_manifest(self.root)[1]
        with (
            patch.object(update, 'verified_backup') as backup,
            patch.object(update, 'snapshot') as snapshot,
        ):
            with self.assertRaisesRegex(RuntimeError, 'Data schema downgrade refused'):
                update.perform_update(self.source, self.root, 'apply')
            backup.assert_not_called()
            snapshot.assert_not_called()
        self.assertEqual(update.validate_manifest(self.root)[1], before)

    def test_failed_start_restores_sources_without_erasing_new_data(self):
        calls = []

        def start(root, log):
            calls.append(update.release(root)['version'])
            if len(calls) == 1:
                with sqlite3.connect(root / 'data/admin-panel/panel.sqlite') as db:
                    db.execute('INSERT INTO settings VALUES (?,?)', ('welcome', '"KEEP_NEW_VALUE"'))
                return 1
            self.assertEqual(migrations.check(root), 1)
            return 0

        with (
            patch.object(update, 'update_resource_check'),
            patch.object(update, 'verified_backup'),
            patch.object(update, 'run_up', side_effect=start),
            patch.object(update, 'refresh_command'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'rollback healthy=True'):
                update.perform_update(self.source, self.root, 'apply')
        self.assertEqual(calls, [update.release(self.source)['version'], '1.0.0'])
        with sqlite3.connect(self.root / 'data/admin-panel/panel.sqlite') as db:
            self.assertEqual(
                db.execute('SELECT value FROM settings WHERE key="welcome"').fetchone()[0],
                '"KEEP_NEW_VALUE"',
            )

    def test_crash_after_migration_recovers_previous_sources_and_allows_retry(self):
        entries = update.validate_manifest(self.source)[0]
        checkpoint = update.snapshot(self.root, entries, 'interrupted-fixture')
        update.record_owner(
            self.root,
            {
                'format': 1,
                'stage': str(checkpoint.relative_to(self.root)),
                'candidate_files': sorted(entries),
                'component_transition': None,
            },
        )
        migrations.migrate(self.root)
        with sqlite3.connect(self.root / 'data/admin-panel/panel.sqlite') as db:
            db.execute('INSERT INTO settings VALUES (?,?)', ('notified_version', '"1.1.0"'))
        update.copy_sources(entries, self.root)
        with (
            patch.object(update, 'run_up', return_value=0),
            patch.object(update, 'refresh_command'),
        ):
            update.recover_update(self.root, self.source)
        self.assertEqual(update.release(self.root)['version'], '1.0.0')
        self.assertEqual(migrations.check(self.root), 1)
        with (
            patch.object(update, 'update_resource_check'),
            patch.object(update, 'verified_backup'),
            patch.object(update, 'run_up', side_effect=self.start),
            patch.object(update, 'refresh_command'),
        ):
            update.perform_update(self.source, self.root, 'apply')
        with sqlite3.connect(self.root / 'data/admin-panel/panel.sqlite') as db:
            self.assertEqual(
                db.execute('SELECT value FROM settings WHERE key="notified_version"').fetchone()[0],
                '"1.1.0"',
            )

    def test_recovery_does_not_start_old_runtime_on_unknown_data(self):
        entries = update.validate_manifest(self.source)[0]
        checkpoint = update.snapshot(self.root, entries, 'unknown-fixture')
        update.record_owner(
            self.root,
            {
                'format': 1,
                'stage': str(checkpoint.relative_to(self.root)),
                'candidate_files': sorted(entries),
                'component_transition': None,
            },
        )
        migrations.migrate(self.root)
        with sqlite3.connect(self.root / 'data/admin-panel/panel.sqlite') as db:
            db.execute('PRAGMA user_version=99')
        with patch.object(update, 'run_up') as start, patch.object(update, 'restore') as restore:
            with self.assertRaisesRegex(RuntimeError, 'rollback-check failed'):
                update.recover_update(self.root, self.source)
            start.assert_not_called()
            restore.assert_not_called()
        self.assertTrue((self.root / 'update-journal.json').exists())

    def test_release_110_updates_and_repeats_without_changing_existing_data(self):
        info = update.release(self.root)
        info.update(version='1.1.0', data_schema_version=1)
        (self.root / 'release.json').write_text(json.dumps(info))
        entries = update.validate_manifest(self.source)[0]
        (self.root / 'SHA256SUMS').write_text(
            ''.join(
                hashlib.sha256((self.root / name).read_bytes()).hexdigest() + '  ' + name + '\n'
                for name in entries
            )
        )
        metadata = json.loads((self.root / 'installation.json').read_text())
        metadata['applied_release_version'] = '1.1.0'
        state_store.write_json(self.root / 'installation.json', metadata)
        migrations.migrate(self.root)
        with sqlite3.connect(self.root / 'data/admin-panel/panel.sqlite') as db:
            db.execute('INSERT INTO settings VALUES (?,?)', ('welcome', '"RETAIN_110_VALUE"'))
        with (
            patch.object(update, 'update_resource_check'),
            patch.object(update, 'verified_backup'),
            patch.object(update, 'run_up', side_effect=self.start),
            patch.object(update, 'refresh_command'),
        ):
            update.perform_update(self.source, self.root, 'apply')
            update.perform_update(self.source, self.root, 'apply')
        self.assertEqual(
            update.release(self.root)['version'], update.release(self.source)['version']
        )
        self.assertEqual(migrations.check(self.root), 1)
        with sqlite3.connect(self.root / 'data/admin-panel/panel.sqlite') as db:
            self.assertEqual(
                db.execute('SELECT value FROM settings WHERE key="welcome"').fetchone()[0],
                '"RETAIN_110_VALUE"',
            )
