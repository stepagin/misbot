"""A database move must never overwrite active/foreign data or stale source writes."""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database
import lifecycle
import state_store


class DatabaseTests(unittest.TestCase):
    def test_legacy_import_cleanup_refuses_live_destination_and_keeps_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); record = self.fixture(root)
            record['imported'] = ['synapse', 'mas']
            snap = root / 'snapshot'; snap.mkdir()
            state_store.write_json(snap / 'backup-format.json',
                {'database_inventory':{'synapse':{}, 'mas':{}}})
            client = Mock(); client.sql.return_value = '1\n'
            with patch.object(database.postgres_setup, 'initialize'), \
                    patch.object(database, 'inventory', return_value={}), \
                    patch.object(lifecycle, 'clear_restored_one_time_keys') as clear:
                with self.assertRaisesRegex(RuntimeError, 'no keys were cleared'):
                    database.import_databases(root, root, client, snap, record)
                clear.assert_not_called(); client.assert_not_called()
            self.assertEqual(record['imported'], ['synapse', 'mas'])

    def test_failed_new_import_cleanup_never_marks_database_imported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); record = self.fixture(root)
            snap = root / 'snapshot'; snap.mkdir(); (snap / 'synapse.dump').write_bytes(b'FICTIONAL_DUMP')
            state_store.write_json(snap / 'backup-format.json',
                {'database_inventory':{'synapse':{}, 'mas':{}}})
            client = Mock(); client.sql.return_value = '0\n'
            with patch.object(database.postgres_setup, 'initialize'), \
                    patch.object(database, 'inventory', return_value={}), \
                    patch.object(lifecycle, 'clear_restored_one_time_keys', side_effect=RuntimeError('blocked')):
                with self.assertRaisesRegex(RuntimeError, 'blocked'):
                    database.import_databases(root, root, client, snap, record)
            self.assertEqual(record['imported'], [])
            self.assertEqual(json.loads((root / 'database-transition.json').read_text())['imported'], [])

    def test_client_verifies_tls_using_explicit_or_system_CA(self):
        for custom in (False, True):
            with self.subTest(custom=custom), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); client_root = root / 'client'; client_root.mkdir()
                (client_root / 'config').mkdir(); (client_root / 'secrets').mkdir()
                state_store.write_json(root / 'versions.json', {'digests': {'postgres': 'postgres@sha256:fictional'}})
                ca = root / 'fictional-ca.pem'; ca.write_text('FICTIONAL_CA_FOR_ARGUMENT_TEST')
                state_store.write_text(client_root / 'settings.env', 'POSTGRES_HOST=db.example.invalid\nPOSTGRES_PORT=5432\nPOSTGRES_ADMIN_USER=postgres\nPOSTGRES_ADMIN_PASSWORD=FICTIONAL_PRIVATE_PASSWORD\nPOSTGRES_SSL_ROOT_CERT=' + (str(ca) if custom else '') + '\n')
                client = database.Client(root, client_root)
                command = client.command('psql', ['-d', 'postgres', '-c', 'SELECT 1;'])
                expected = str(ca) if custom else '/etc/ssl/certs/ca-certificates.crt'
                self.assertIn('type=bind,source=' + expected + ',target=/config/ca.pem,readonly', command)
                self.assertIn('PGSSLMODE=verify-full', command)
                self.assertIn('PGSSLROOTCERT=/config/ca.pem', command)
                self.assertNotIn('FICTIONAL_PRIVATE_PASSWORD', repr(command))

    def test_unverifiable_orphan_container_status_is_not_treated_as_absent(self):
        record={'active_client':'matrix-dbcopy-'+'a'*32}
        for code,out,err,expected in [(0,'false\n','',False),(1,'','Error: No such object: owned-test',False),
                (1,'','EXAMPLE_PRIVATE_DAEMON_ERROR',True),(0,'unknown\n','',True)]:
            with self.subTest(code=code,out=out),patch.object(database.subprocess,'run',return_value=Mock(returncode=code,stdout=out,stderr=err)):
                if expected:
                    with self.assertRaisesRegex(RuntimeError,'could not be verified') as caught:database.no_active_client(record)
                    self.assertNotIn('EXAMPLE_PRIVATE',str(caught.exception))
                else:database.no_active_client(record)

    def test_SIGTERM_interrupts_captured_child_instead_of_being_swallowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);marker=root/'child-ready';driver=root/'driver.py'
            driver.write_text('import sys\nfrom pathlib import Path\n'
                +'sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+')\n'
                +'import database,lifecycle\n'
                +'lifecycle.ownership_check=lambda:None\n'
                +'database.external=lambda *args:lifecycle.run([sys.executable,"-c",'
                +repr('from pathlib import Path;import time;Path('+repr(str(marker))+').write_text("ready");time.sleep(30)')+'])\n'
                +'sys.argv=["database.py","external","--directory",'+repr(str(root))+',"--settings",'+repr(str(root/'fictional.env'))+']\n'
                +'database.state_store.require_root=lambda:None\n'
                +'try:database.main()\n'
                +'except BaseException as error:print(type(error).__name__,flush=True);sys.exit(1)\n')
            parent=subprocess.Popen([sys.executable,str(driver)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
            try:
                deadline=time.monotonic()+5
                while not marker.exists() and parent.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(marker.exists(),'disposable child did not reach its wait')
                parent.send_signal(signal.SIGTERM)
                out,err=parent.communicate(timeout=2)
                self.assertEqual(parent.returncode,1,err)
                self.assertIn('DatabaseInterrupted',out)
            finally:
                try:os.killpg(parent.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                parent.communicate(timeout=5)

    def fixture(self, root):
        state_store.write_json(root / 'installation.json', {'domain': 'example.invalid'})
        (root / 'stages/database-transfers/test').mkdir(parents=True)
        (root / 'stages/database-targets/test').mkdir(parents=True)
        record = {'format': 1, 'server_name': 'example.invalid', 'stage': 'stages/database-transfers/test',
                  'target': 'stages/database-targets/test', 'writers': ['mas', 'synapse'],
                  'phase': 'prepared', 'activated': False, 'imported': [],
                  'pid': os.getpid(), 'started': state_store.process_identity(os.getpid())[1]}
        database.save(root, record)
        return record

    def test_private_parameters_and_tls_refusal_do_not_echo_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'pg.env'
            state_store.write_text(path, 'POSTGRES_HOST=db.example.invalid\nPOSTGRES_ADMIN_PASSWORD=FICTIONAL_DO_NOT_ECHO\nPOSTGRES_SSL_MODE=require\n')
            with self.assertRaisesRegex(RuntimeError, 'verify-full') as error:
                database.settings(path)
            self.assertNotIn('FICTIONAL_DO_NOT_ECHO', str(error.exception))
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'private env'):
                database.settings(path)
            path.chmod(0o600)
            path.write_text('POSTGRES_HOST=db.example.invalid\nPOSTGRES_ADMIN_PASSWORD=FICTIONAL_DO_NOT_ECHO\nDOMAIN=other.invalid\n')
            with self.assertRaisesRegex(RuntimeError, 'six PostgreSQL'):
                database.settings(path)

    def test_pending_journal_admits_only_current_driver_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); record = self.fixture(root)
            with self.assertRaisesRegex(RuntimeError, 'database-recover'):
                state_store.assert_update_allowed(root)
            with patch.dict(os.environ, {'MATRIX_INSTALLER_DATABASE_STAGE': record['stage']}):
                state_store.assert_update_allowed(root)
                with patch.object(state_store, 'related_owner', return_value=False):
                    with self.assertRaisesRegex(RuntimeError, 'independent writer'):
                        state_store.assert_update_allowed(root)
            (root / 'database-transition.json').chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'private owned'):
                state_store.database_transition_record(root)

    def test_active_orphan_and_activated_abort_refused_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); record = self.fixture(root)
            record['active_client'] = 'matrix-dbcopy-' + 'a' * 32
            with patch.object(database.subprocess, 'run', return_value=Mock(returncode=0, stdout='true\n')):
                with self.assertRaisesRegex(RuntimeError, 'still running'):
                    database.no_active_client(record)
            record.pop('active_client'); record['activated'] = True; record['phase'] = 'activating'
            database.save(root, record)
            with patch.object(database.lifecycle, 'dc') as dc:
                with self.assertRaisesRegex(RuntimeError, 'lose them'):
                    database.abort(root)
                dc.assert_not_called()

    def test_nonempty_target_and_incompatible_major_refused(self):
        client = Mock()
        client.sql.return_value = '1\n'
        with self.assertRaisesRegex(RuntimeError, 'never adopts'):
            database.empty_target(client)
        client.sql.return_value = '170009\n'
        with self.assertRaisesRegex(RuntimeError, 'major 18'):
            database.version_check(client)

    def test_application_roles_authenticate_with_preserved_credentials_and_static_failure(self):
        client = Mock(return_value='1\n')
        database.role_access(client)
        self.assertEqual([call.kwargs['user'] for call in client.call_args_list], ['synapse', 'mas'])
        client = Mock(side_effect=RuntimeError('EXAMPLE_SECRET_ERROR_MUST_NOT_ECHO'))
        with self.assertRaisesRegex(RuntimeError, 'preserved installation credentials') as error:
            database.role_access(client)
        self.assertNotIn('EXAMPLE_SECRET_ERROR_MUST_NOT_ECHO', str(error.exception))

    def test_client_uses_private_passfile_stdin_and_static_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); folder = root / 'target'; (folder / 'config').mkdir(parents=True)
            state_store.write_json(root / 'versions.json', {'digests': {'postgres': 'postgres@sha256:example'}})
            state_store.write_text(folder / 'settings.env', 'POSTGRES_HOST=db.example.invalid\nPOSTGRES_PORT=5432\nPOSTGRES_ADMIN_USER=postgres\nPOSTGRES_ADMIN_PASSWORD=EXAMPLE:PASS\\word\nPOSTGRES_SSL_ROOT_CERT=\n')
            client = database.Client(root, folder)
            with patch.object(database.subprocess, 'run', return_value=Mock(returncode=1, stdout=b'')) as run:
                with self.assertRaisesRegex(RuntimeError, 'TLS') as error:
                    client.sql('postgres', 'SELECT 1;')
                command = run.call_args.args[0]
                self.assertNotIn('EXAMPLE', repr(command)); self.assertNotIn('PASS', repr(command).replace('PGPASSFILE', ''))
                self.assertEqual(run.call_args.kwargs['input'], b'SELECT 1;')
                self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
                self.assertNotIn('EXAMPLE', str(error.exception))
            self.assertEqual((folder / 'config/admin.pgpass').stat().st_mode & 0o777, 0o600)

    def test_activated_recovery_never_imports_or_restores_local_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); record = self.fixture(root)
            record.update(activated=True, phase='activating')
            (root / 'config').mkdir()
            with patch.object(database, 'Client'), patch.object(database, 'version_check'), patch.object(database, 'role_access'), \
                    patch.object(database, 'snapshot') as snap, \
                    patch.object(database, 'import_databases') as import_db, \
                    patch.object(lifecycle, 'up') as up, patch.object(lifecycle, 'dc'), \
                    patch.object(lifecycle, 'wait_writers'), patch.object(lifecycle, 'emit'):
                database.resume(root, record)
                snap.assert_not_called(); import_db.assert_not_called(); up.assert_called_once()
            self.assertFalse((root / 'database-transition.json').exists())
            self.assertTrue(json.loads((root / 'config/database-last-transition.json').read_text())['activated'])

    def test_live_target_connections_refuse_database_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); record = self.fixture(root); snap = root / 'snapshot'; snap.mkdir()
            state_store.write_json(snap / 'backup-format.json', {'database_inventory': {'synapse': {}, 'mas': {}}})
            client = Mock(); client.sql.return_value = '1\n'
            with patch.object(database.postgres_setup, 'initialize'):
                with self.assertRaisesRegex(RuntimeError, 'no database reset'):
                    database.import_databases(root, root, client, snap, record)
            client.assert_not_called()
            self.assertEqual(record['imported'], [])


if __name__ == '__main__':
    unittest.main()
