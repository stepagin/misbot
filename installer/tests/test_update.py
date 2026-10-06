"""Update gates and rollback paths must protect the installed release."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update


class UpdateTests(unittest.TestCase):
    def test_command_refresh_uses_verified_backend_and_safe_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'command_wrapper.py').write_text('# fictional owned helper')
            (root/'identity').write_text('FICTIONAL_KEEP')
            for returncode in (0,23):
                with patch.object(update.subprocess,'run',return_value=subprocess.CompletedProcess([],returncode,stdout=b'FICTIONAL_SECRET')) as execute:
                    if returncode:
                        with self.assertRaisesRegex(RuntimeError,'Command refresh failed') as caught:update.refresh_command(root)
                        self.assertNotIn('FICTIONAL_SECRET',str(caught.exception))
                    else:update.refresh_command(root)
                    self.assertEqual(execute.call_args.args[0],['/usr/bin/python3',str(root/'command_wrapper.py'),'install','--directory',str(root),'--keep-default'])
            self.assertEqual((root/'identity').read_text(),'FICTIONAL_KEEP')

    def test_pending_s3_credentials_refuses_before_resource_query_or_source_mutation(self):
        import state_store
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            state_store.write_json(root/'s3-credentials-transition.json',{
                'format':1,'stage':'stages/s3-credentials/'+'a'*32,
                'pid':99999999,'started':'FICTIONAL_INTERRUPTED_PROCESS'})
            state_store.write_text(root/'settings.env','FICTIONAL_EXISTING_SETTINGS')
            before={p.name:p.read_bytes() for p in root.iterdir()}
            with patch.object(update,'validate_manifest') as manifest, \
                    patch.object(update,'update_resource_check') as resources, \
                    patch.object(update,'snapshot') as snapshot, \
                    patch.object(update,'copy_sources') as copy:
                with self.assertRaisesRegex(state_store.S3CredentialRecoveryRequired,
                        str(root/'install.sh')+' s3-credentials-recover'):
                    update.perform_update(root/'missing_source',root,'apply')
                for call in (manifest,resources,snapshot,copy):call.assert_not_called()
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})

    def component_folders(self, base):
        after = base / 'new'; before = base / 'old'; after.mkdir(); before.mkdir()
        newer = json.loads((Path(__file__).resolve().parents[1] / 'versions.json').read_text())
        older = json.loads(json.dumps(newer))
        older['tags']['synapse'] = 'oci.element.io/synapse:v1.161.0'
        older['digests']['synapse'] = 'oci.element.io/synapse@sha256:6b95dd129e35e97e8032cb759e5db4a516a5f7ef6c98d34d716fea1ae239b62a'
        (before / 'versions.json').write_text(json.dumps(older))
        (after / 'versions.json').write_text(json.dumps(newer))
        return before, after

    def test_only_complete_approved_component_pair_is_admitted(self):
        with tempfile.TemporaryDirectory() as temp:
            before, after = self.component_folders(Path(temp))
            plan = update.component_plan(before, after)
            self.assertEqual(plan['from'], '1.161.0'); self.assertEqual(plan['to'], '1.162.0')
            self.assertIsNone(update.component_plan(after, after))
            with self.assertRaisesRegex(RuntimeError, 'not an approved component path'):
                update.component_plan(after, before)
            changed = json.loads((after / 'versions.json').read_text())
            changed['tags']['postgres'] = 'postgres:99.0'
            (after / 'versions.json').write_text(json.dumps(changed))
            with self.assertRaisesRegex(RuntimeError, 'not an approved component path'):
                update.component_plan(before, after)

    def test_component_recovery_accepts_atomic_inventory_not_yet_replaced(self):
        import lifecycle
        with tempfile.TemporaryDirectory() as temp:
            before, after = self.component_folders(Path(temp)); plan = update.component_plan(before, after)
            (after / 'versions.json').write_bytes((before / 'versions.json').read_bytes())
            with patch.object(lifecycle, 'configure'), patch.object(lifecycle, 'dc') as dc, \
                    patch.object(lifecycle, 'service_rows', return_value=[]), \
                    patch.object(update, 'component_schema') as schema:
                update.component_rollback_guard(after, before, plan)
            self.assertEqual(dc.call_args.args[:3], ('stop', '-t', '30'))
            schema.assert_called_once_with(after)

    def test_component_schema_refusal_never_restores_or_starts_older_files(self):
        with tempfile.TemporaryDirectory() as temp:
            before, after = self.component_folders(Path(temp)); plan = update.component_plan(before, after)
            with patch.object(update, 'copy_sources', return_value=[]), \
                    patch.object(update, 'run_up', return_value=1) as up, \
                    patch.object(update, 'component_rollback_guard', side_effect=RuntimeError('Unknown schema; retained journal')), \
                    patch.object(update, 'restore') as restore:
                with self.assertRaisesRegex(RuntimeError, 'Unknown schema'):
                    update.apply_changes(after, before, {}, {'version':'1.2.50'}, 'revision', plan)
                restore.assert_not_called(); self.assertEqual(up.call_count, 1)

    def test_actual_schema_and_background_gate_uses_no_database_payload(self):
        import lifecycle
        with patch.object(lifecycle, 'configure'), patch.object(lifecycle, 'postgres',
                return_value='{"version":94,"compat":84,"pending":0}') as query:
            update.component_schema(Path('/tmp/fictional'))
            self.assertNotIn('key_json', repr(query.call_args))
        for schema, message in (
            ({'version':95,'compat':95,'pending':0}, 'Cannot verify'),
            ({'version':94,'compat':84,'pending':1}, 'still pending')):
            with self.subTest(schema=schema), patch.object(lifecycle, 'configure'), \
                    patch.object(lifecycle, 'postgres', return_value=json.dumps(schema)):
                with self.assertRaisesRegex(RuntimeError, message):
                    update.component_schema(Path('/tmp/fictional'))

    def test_low_disk_refused_before_backup_snapshot_or_source_replacement(self):
        import preflight
        import lifecycle
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / 'payload'; payload.write_bytes(b'FICTIONAL_SOURCE')
            before = payload.read_bytes()
            with patch.object(preflight, 'system_check'), \
                    patch.object(lifecycle, 'configure'), patch.object(lifecycle, 'postgres', return_value='1024'), \
                    patch.object(update.shutil, 'disk_usage', return_value=Mock(free=3 * 1024 ** 3)), \
                    patch.object(update, 'snapshot') as snapshot, patch.object(update, 'verified_backup') as backup:
                with self.assertRaisesRegex(RuntimeError, 'Installed files were not changed'):
                    update.update_resource_check(root, {'payload':payload})
                snapshot.assert_not_called(); backup.assert_not_called()
            self.assertEqual(payload.read_bytes(), before)

    def test_background_wait_executes_actual_retry_path_and_preserves_recovery_instruction(self):
        import lifecycle
        pending = '{"version":94,"compat":84,"pending":1}'
        ready = '{"version":94,"compat":84,"pending":0}'
        with patch.object(lifecycle, 'configure'), patch.object(lifecycle, 'emit'), \
                patch.object(lifecycle, 'postgres', side_effect=[pending, ready]) as sql, \
                patch.object(update.time, 'sleep') as sleep:
            update.component_schema(Path('/tmp/fictional'), wait_background=True)
            self.assertEqual(sql.call_count, 2); sleep.assert_called_once_with(2)
        with patch.object(lifecycle, 'configure'), patch.object(lifecycle, 'emit'), \
                patch.object(lifecycle, 'postgres', return_value=pending), patch.object(update.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'update-recover from the trusted bundle'):
                update.component_schema(Path('/tmp/fictional'), wait_background=True)

    def test_incompatible_installations_refuse_before_backup_or_source_mutation(self):
        bundle=Path(__file__).resolve().parents[1]
        package_folder=tempfile.TemporaryDirectory()
        self.addCleanup(package_folder.cleanup)
        archive=Path(package_folder.name)/'source.tar.gz'
        subprocess.run([sys.executable,str(bundle/'build.py'),'--output',str(archive)],
                       check=True,stdout=subprocess.DEVNULL)
        for case,message in (('format','Unsupported configuration format'),
                             ('downgrade','Downgrade refused'),
                             ('uninitialized','Run install.sh up once'),
                             ('components','Image digest changes')):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                base=Path(folder)
                with tarfile.open(archive) as package:package.extractall(base/'source',filter='data')
                source=base/'source/matrix-installer';root=base/'installation';root.mkdir()
                for name in ('install.sh','generate.py','lifecycle.py'):
                    (root/name).write_text('FICTIONAL_PRIOR_SOURCE')
                (root/'data').mkdir();(root/'data/preserved').write_text('FICTIONAL_EXISTING_DATA')
                (root/'versions.json').write_bytes((source/'versions.json').read_bytes())
                metadata={'domain':'fictional.invalid','config_format_version':1,
                          'applied_release_version':json.loads((source/'release.json').read_text())['version'],
                          'databases_initialized':True}
                if case=='format':metadata['config_format_version']=99
                elif case=='downgrade':metadata['applied_release_version']='999.0.0'
                elif case=='uninitialized':metadata['databases_initialized']=False
                else:(root/'versions.json').write_text('{"fictional_different_digest":true}')
                (root/'installation.json').write_text(json.dumps(metadata))
                before={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in root.rglob('*') if p.is_file()}
                with patch.object(update,'verified_backup') as backup,patch.object(update,'snapshot') as snapshot:
                    with self.assertRaisesRegex(RuntimeError,message):update.perform_update(source,root,'apply')
                    backup.assert_not_called();snapshot.assert_not_called()
                after={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in root.rglob('*') if p.is_file()}
                self.assertEqual(before,after)

    def test_backup_restore_failure_prevents_source_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            result = subprocess.CompletedProcess([], 1)
            with patch.object(update, 'run_child', return_value=result) as run:
                with self.assertRaisesRegex(RuntimeError, 'installed source unchanged'):
                    update.verified_backup(path / 'source', path / 'target', path)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], 'backup')

    def test_keyboard_interrupt_during_source_copy_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with patch.object(update, 'copy_sources', side_effect=KeyboardInterrupt), \
                    patch.object(update, 'restore') as restore, patch.object(update, 'run_up', return_value=0):
                with self.assertRaisesRegex(RuntimeError, 'rollback healthy=True'):
                    update.apply_changes(path, path / 'saved', {}, {'version': '1.2.15'}, 'revision')
            restore.assert_called_once()

    def test_signal_flag_after_copy_rolls_back_and_clears_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with patch.object(update, 'copy_sources', return_value=['example']), \
                    patch.object(update, 'interruption_requested', True), patch.object(update, 'restore') as restore, \
                    patch.object(update, 'run_up', return_value=0):
                with self.assertRaisesRegex(RuntimeError, 'rollback healthy=True'):
                    update.apply_changes(path, path / 'saved', {}, {'version': '1.2.15'}, 'revision')
                self.assertFalse(update.interruption_requested)
            restore.assert_called_once()

    def test_pending_update_blocks_another_apply_and_escaping_recovery(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'update-journal.json').write_text(json.dumps({'stage': '../../outside'}))
            (root / 'update-journal.json').chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'Recover the interrupted update first'):
                update.perform_update(root / 'source', root, 'apply')
            with self.assertRaisesRegex(RuntimeError, 'Unsafe update recovery checkpoint'):
                update.recover_update(root)

    def test_partial_source_copy_failure_restores_prior_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with patch.object(update, 'copy_sources', side_effect=OSError('disk full')), \
                    patch.object(update, 'restore') as restore, patch.object(update, 'run_up', return_value=0):
                with self.assertRaisesRegex(RuntimeError, 'rollback healthy=True'):
                    update.apply_changes(path, path / 'saved', {}, {'version': '1.2.4'}, 'revision')
            restore.assert_called_once()

    def test_wrong_post_start_revision_also_triggers_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with patch.object(update, 'copy_sources', return_value=['generate.py']), \
                    patch.object(update, 'state', return_value={'applied_release_version': '1.2.4', 'applied_release_revision': 'wrong'}), \
                    patch.object(update, 'restore') as restore, patch.object(update, 'run_up', return_value=0) as run_up:
                with self.assertRaisesRegex(RuntimeError, 'rollback healthy=True'):
                    update.apply_changes(path, path / 'saved', {}, {'version': '1.2.4'}, 'revision')
            restore.assert_called_once()
            self.assertEqual(run_up.call_count, 2)
