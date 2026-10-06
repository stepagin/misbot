"""Failure recovery checks that need no Docker daemon or public credentials."""
import importlib.util
import io
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE))
spec = importlib.util.spec_from_file_location('installer_lifecycle', BUNDLE / 'lifecycle.py')
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


class LifecycleTests(unittest.TestCase):
    def test_unavailable_docker_explains_recovery_without_replaying_private_stderr(self):
        result=subprocess.CompletedProcess(['docker','ps'],1,'',
            'Cannot connect to the Docker daemon at unix:///fictional.sock. '
            'FICTIONAL_PASSWORD_DO_NOT_ECHO')
        with patch.object(lifecycle,'ROOT',Path('/opt/fictional-stack'),create=True), \
                patch.object(lifecycle.subprocess,'run',return_value=result):
            with self.assertRaisesRegex(RuntimeError,'Docker daemon is unavailable') as caught:
                lifecycle.run(['/usr/bin/docker','ps'])
        message=str(caught.exception)
        for command in ('sudo systemctl status docker','sudo systemctl start docker',
                        'sudo bash /opt/fictional-stack/install.sh status','pending recovery'):
            self.assertIn(command,message)
        self.assertNotIn('FICTIONAL_PASSWORD',message)
        self.assertNotIn('fictional.sock',message)

    def test_other_docker_failure_keeps_private_stderr_without_inventing_daemon_outage(self):
        result=subprocess.CompletedProcess(['docker','pull'],1,'',
            'registry authorization failed: FICTIONAL_TOKEN_DO_NOT_ECHO')
        with patch.object(lifecycle.subprocess,'run',return_value=result):
            with self.assertRaisesRegex(RuntimeError,'inspect the relevant service logs privately') as caught:
                lifecycle.run(['docker','pull','fictional.invalid/image'])
        self.assertNotIn('FICTIONAL_TOKEN',str(caught.exception))
        self.assertNotIn('systemctl',str(caught.exception))

    def test_down_refuses_foreign_project_or_network_before_removal(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(lifecycle,'ROOT',Path(folder),create=True):
            cases=[
                ['id',json.dumps('/opt/foreign')],
                ['', 'network',json.dumps(['matrix-stack','default','/opt/foreign'])],
                ['', 'network',json.dumps(['matrix-stack','default',folder]),'endpoint',json.dumps(['foreign',folder])],
                ['', 'network',json.dumps(['matrix-stack','default','']),''],
            ]
            for outputs in cases:
                with self.subTest(outputs=outputs),patch.object(lifecycle,'run',side_effect=outputs),patch.object(lifecycle,'dc') as dc:
                    with self.assertRaises(RuntimeError):lifecycle.down()
                    dc.assert_not_called()

    def test_down_retains_private_data_and_checkpoint_with_repeat(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(lifecycle,'ROOT',Path(folder),create=True):
            root=Path(folder)
            paths=[root/'database-password-transition.json',root/'installation.json',root/'fictional-private-data']
            for path in paths:path.write_text('FICTIONAL_RETAINED_CONTENT')
            before={path:path.read_bytes() for path in paths}
            for outputs in (
                    ['', 'network',json.dumps(['matrix-stack','default',folder]),'endpoint',json.dumps(['matrix-stack',folder])],
                    ['', '']):
                with patch.object(lifecycle,'run',side_effect=outputs),patch.object(lifecycle,'dc') as dc,patch.object(lifecycle,'emit') as emit:
                    lifecycle.down()
                    dc.assert_called_once_with('down')
                    self.assertTrue(emit.call_args.kwargs['checkpoints_retained'])
            self.assertEqual(before,{path:path.read_bytes() for path in paths})

    def test_backup_excludes_only_synapse_one_time_key_data(self):
        self.assertEqual(lifecycle.backup_dump_arguments('synapse'),
            ['-Fc', '--exclude-table-data=public.e2e_one_time_keys_json', 'synapse'])
        self.assertEqual(lifecycle.backup_dump_arguments('mas'), ['-Fc', 'mas'])
        with self.assertRaisesRegex(RuntimeError, 'Unexpected application'):
            lifecycle.backup_dump_arguments('foreign')

    def test_old_dump_key_cleanup_is_transactional_bounded_and_protected(self):
        with patch.object(lifecycle, 'postgres', return_value='0\n') as sql, \
                patch.object(lifecycle, 'emit') as emit:
            lifecycle.clear_restored_one_time_keys('restorecheck_synapse_aaaaaaaa')
        args = sql.call_args.args
        self.assertIn('-qAt', args)
        query = sql.call_args.kwargs['stdin']
        self.assertTrue(query.startswith('BEGIN;'))
        self.assertTrue(query.endswith('COMMIT;'))
        self.assertIn("lock_timeout='5s'", query)
        self.assertIn('TRUNCATE TABLE public.e2e_one_time_keys_json', query)
        self.assertNotIn('e2e_device_keys_json', query)
        self.assertEqual(emit.call_args.kwargs['remaining'], 0)
        for response in ('1\n', RuntimeError('FICTIONAL_PRIVATE_KEY_DO_NOT_ECHO')):
            with self.subTest(response=type(response).__name__), \
                    patch.object(lifecycle, 'postgres', side_effect=response if isinstance(response, Exception) else None,
                                 return_value=response), patch.object(lifecycle, 'emit') as emit:
                with self.assertRaisesRegex(RuntimeError, 'writers remain disabled') as error:
                    lifecycle.clear_restored_one_time_keys('synapse')
                self.assertNotIn('FICTIONAL_PRIVATE_KEY', str(error.exception))
                emit.assert_not_called()

    def test_activation_never_clears_active_target_or_live_staged_writers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'restore-journal.json').write_text('{"stage":"ready"}')
            for pending, rows, message in (
                (False, [], 'already activated'),
                *((True, [{'Service':service, 'State':state}], 'writers are still active')
                  for service in ('synapse','mas','jwt','referral-bot','referral-control')
                  for state in ('running','restarting','paused'))):
                with self.subTest(pending=pending), patch.object(lifecycle, 'ROOT', root, create=True), \
                        patch.object(lifecycle, 'state', return_value={'restore_pending':pending}), \
                        patch.object(lifecycle, 'service_rows', return_value=rows), \
                        patch.object(lifecycle, 'clear_restored_one_time_keys') as clear, \
                        patch.object(lifecycle.preflight, 'check') as preflight_check, \
                        patch.object(lifecycle, 'up') as up:
                    with self.assertRaisesRegex(RuntimeError, message):
                        lifecycle.restore_activate()
                    clear.assert_not_called(); up.assert_not_called(); preflight_check.assert_not_called()

    def test_legacy_ready_cleanup_failure_preserves_stage_before_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'restore-journal.json').write_text('{"stage":"ready"}')
            (root / 'installation.json').write_text('{"restore_pending":true}')
            with patch.object(lifecycle, 'ROOT', root, create=True), \
                    patch.object(lifecycle, 'state', return_value={'restore_pending':True}), \
                    patch.object(lifecycle, 'service_rows', return_value=[]), \
                    patch.object(lifecycle.preflight, 'check'), \
                    patch.object(lifecycle, 'clear_restored_one_time_keys', side_effect=RuntimeError('blocked')), \
                    patch.object(lifecycle, 'up') as up:
                with self.assertRaisesRegex(RuntimeError, 'blocked'):
                    lifecycle.restore_activate()
                up.assert_not_called()
            self.assertTrue(json.loads((root / 'installation.json').read_text())['restore_pending'])

    def test_frozen_status_includes_stopped_containers_and_exact_recovery_without_secret_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(lifecycle,'ROOT',root,create=True), \
                    patch.object(lifecycle,'state',return_value={'migration_frozen':True,'applied_release_version':'1.2.45','private_field':'FICTIONAL_PRIVATE_VALUE'}), \
                    patch.object(lifecycle,'dc',return_value='synapse exited\nmas exited') as dc, \
                    patch.object(lifecycle,'emit') as emit,redirect_stdout(io.StringIO()) as output:
                lifecycle.status()
            dc.assert_called_once_with('ps','--all')
            self.assertIn('synapse exited',output.getvalue());self.assertIn('mas exited',output.getvalue())
            fields=emit.call_args.kwargs;self.assertTrue(fields['migration_frozen'])
            self.assertIn('sudo bash '+str(root/'install.sh')+' move-export',fields['next_actions'])
            self.assertIn('sudo misbot --directory '+str(root)+' backup move export',fields['next_actions'])
            self.assertTrue(any('target writers are stopped' in value and 'move-abort target-stopped' in value for value in fields['next_actions']))
            self.assertNotIn('FICTIONAL_PRIVATE_VALUE',json.dumps(fields))
            self.assertEqual(list(root.iterdir()),[])

    def test_status_does_not_read_private_journals_and_reports_pending_before_Docker_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'secrets').mkdir()
            for relative in ('backup-writers.json','database-transition.json','update-journal.json','secrets/user-password-pending.json'):
                (root/relative).write_text('FICTIONAL_PRIVATE_BROKEN_JOURNAL')
            with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'state',return_value={}), \
                    patch.object(Path,'read_text',side_effect=AssertionError('Status must not read journal/secret contents')), \
                    patch.object(lifecycle,'dc',side_effect=RuntimeError('Protected Docker unavailable')) as dc, \
                    patch.object(lifecycle,'emit') as emit:
                with self.assertRaisesRegex(RuntimeError,'Protected Docker unavailable'):lifecycle.status()
            fields=emit.call_args.kwargs
            self.assertTrue(all(fields[name] for name in ('backup_recovery_pending','database_recovery_pending','update_recovery_pending','user_password_recovery_pending')))
            self.assertTrue(any('trusted extracted installer' in value and 'update-recover' in value for value in fields['next_actions']))
            self.assertTrue(any('user-password-recover' in value for value in fields['next_actions']))
            self.assertNotIn('FICTIONAL_PRIVATE_BROKEN_JOURNAL',json.dumps(fields))
            self.assertEqual(len(list(root.rglob('*.json'))),4);dc.assert_called_once_with('ps','--all')

    def test_interrupt_propagates_out_of_readiness_retry(self):
        with patch.object(lifecycle,'ROOT',Path('/tmp/fictional-source'),create=True),patch.object(lifecycle,'emit'),patch.object(lifecycle.time,'sleep'):
            with self.assertRaises(lifecycle.state_store.OperationInterrupted):
                lifecycle.wait(lambda:(_ for _ in ()).throw(lifecycle.state_store.OperationInterrupted()),attempts=1)

    def test_interrupt_is_not_changed_into_optional_S3_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(lifecycle,'ROOT',Path(folder),create=True),patch.object(lifecycle,'state',return_value={}), \
                    patch.object(lifecycle.preflight,'settings',return_value={'MEDIA_MODE':'s3','S3_BUCKET':'fictional-bucket'}), \
                    patch.object(lifecycle.media_transfer,'client') as client:
                client.return_value.head_bucket.side_effect=lifecycle.state_store.OperationInterrupted()
                with self.assertRaises(lifecycle.state_store.OperationInterrupted):lifecycle.up()

    def test_S3_start_failure_has_correct_continuation_for_database_driver(self):
        for pending in (False, True):
            with self.subTest(database_pending=pending), tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                if pending:(root/'database-transition.json').write_text('{}')
                with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'up_admission_check'), \
                        patch.object(lifecycle,'state',return_value={'storage_committed':True}), \
                        patch.object(lifecycle.preflight,'settings',return_value={'MEDIA_MODE':'s3','S3_BUCKET':'fictional-bucket'}), \
                        patch.object(lifecycle.media_transfer,'client') as client,patch.object(lifecycle,'generate') as generate:
                    client.return_value.head_bucket.side_effect=RuntimeError('FICTIONAL_SECRET_DO_NOT_ECHO')
                    with self.assertRaises(RuntimeError) as failure:lifecycle.up()
                    message=str(failure.exception)
                    self.assertIn('S3 check failed before startup',message)
                    self.assertIn('sudo bash '+str(root/'install.sh')+(' database-recover' if pending else ' up'),message)
                    self.assertNotIn('FICTIONAL_SECRET_DO_NOT_ECHO',message)
                    self.assertNotIn('media-local',message)
                    generate.assert_not_called()

    def test_certificate_reload_does_not_start_stopped_or_absent_livekit(self):
        for rows in ([],[{'Service':'livekit','State':'exited'}],[{'Service':'livekit','State':'restarting'}]):
            with self.subTest(rows=rows),tempfile.TemporaryDirectory() as folder:
                with patch.object(lifecycle,'ROOT',Path(folder),create=True), \
                        patch.object(lifecycle,'service_rows',return_value=rows),patch.object(lifecycle,'emit') as emit, \
                        patch.object(lifecycle,'dc') as dc,patch.object(lifecycle,'certificate_paths') as paths:
                    lifecycle.cert_renew()
                    dc.assert_not_called();paths.assert_not_called()
                    self.assertEqual(emit.call_args.args[0],'turn_certificate_reload_deferred')
                    self.assertIn(' up, then check',emit.call_args.kwargs['next_action'])
                    self.assertEqual(list(Path(folder).iterdir()),[])

    def test_running_livekit_reload_records_certificate_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();cert=root/'fictional.crt';cert.write_text('FICTIONAL_PUBLIC_CERTIFICATE')
            with patch.object(lifecycle,'ROOT',root,create=True), \
                    patch.object(lifecycle,'service_rows',return_value=[{'Service':'livekit','State':'running'}]), \
                    patch.object(lifecycle,'certificate_paths',return_value=(cert,root/'fictional.key')), \
                    patch.object(lifecycle,'run',side_effect=['','FICTIONAL_PUBLIC_KEY','FICTIONAL_PUBLIC_KEY']*2), \
                    patch.object(lifecycle.time,'sleep'),patch.object(lifecycle,'emit'),patch.object(lifecycle,'dc') as dc:
                lifecycle.cert_renew();lifecycle.cert_renew()
                dc.assert_called_once_with('restart','-t','10','livekit')
                self.assertTrue((root/'data/turn-certificate.sha256').is_file())

    def test_legacy_network_recreation_requires_all_endpoint_owners_and_never_reads_env(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);endpoint='a'*64
            answers=['b'*64,json.dumps(['matrix-stack','default','']),endpoint,
                     json.dumps(['matrix-stack',str(root)])]
            with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'run',side_effect=answers) as run, \
                    patch.object(lifecycle,'ownership_check'),patch.object(lifecycle,'emit'),patch.object(lifecycle,'dc') as dc:
                lifecycle.upgrade_legacy_network()
                dc.assert_called_once_with('down','--remove-orphans')
                for call in run.call_args_list:self.assertNotIn('.Config.Env',' '.join(call.args[0]))
            answers[-1]=json.dumps(['unmanaged',str(root)])
            with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'run',side_effect=answers), \
                    patch.object(lifecycle,'dc') as dc:
                with self.assertRaisesRegex(RuntimeError,'unmanaged or foreign'):lifecycle.upgrade_legacy_network()
                dc.assert_not_called()

    def test_network_with_current_owner_is_unchanged_and_unknown_empty_legacy_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for labels,endpoints,error in [(['matrix-stack','default',str(root)],None,None),
                     (['matrix-stack','default','/opt/another'],None,'Another installation'),
                     (['matrix-stack','default',None],'','no container ownership'),
                     (['matrix-stack','default',''],'','no container ownership'),
                     (['unmanaged','default',None],None,'Another resource')]:
                answers=['b'*64,json.dumps(labels)]+([endpoints] if endpoints is not None else [])
                with self.subTest(labels=labels),patch.object(lifecycle,'ROOT',root,create=True), \
                        patch.object(lifecycle,'run',side_effect=answers),patch.object(lifecycle,'dc') as dc:
                    if error:
                        with self.assertRaisesRegex(RuntimeError,error):lifecycle.upgrade_legacy_network()
                    else:lifecycle.upgrade_legacy_network()
                    dc.assert_not_called()

    def test_ownership_projection_never_fetches_environment_and_refuses_foreign_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def run(command, **kwargs):
                if command[1] == 'ps':return 'example-container\n'
                self.assertIn('--format', command)
                self.assertNotIn('.Config.Env', command[command.index('--format') + 1])
                return json.dumps(str(root)) + '\n'
            with patch.object(lifecycle, 'ROOT', root, create=True), patch.object(lifecycle, 'run', side_effect=run):
                lifecycle.ownership_check()
            with patch.object(lifecycle, 'ROOT', root, create=True), \
                    patch.object(lifecycle, 'run', side_effect=['example-container\n','"/opt/foreign"\n']):
                with self.assertRaisesRegex(RuntimeError, 'Another directory'):
                    lifecycle.ownership_check()
    def test_orphaned_dump_keeps_operation_lock_until_child_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'stack';root.mkdir()
            (root / 'installation.json').write_text(json.dumps({'domain':'example.invalid'}))
            marker = root / 'example-dump-pid'
            child_code = ('import os,sys,time; from pathlib import Path; '
                'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)')
            worker_code = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                'import lifecycle,state_store; root=Path(sys.argv[2]); '
                'lifecycle.configure(root,"example-dump-lock"); '
                'lifecycle.referral.validate_saved=lambda *args:None; '
                'lifecycle.service_rows=lambda:[{"Service":"synapse","State":"running"}]; '
                'lifecycle.dc=lambda *args:None; lifecycle.wait_writers=lambda *args:None; '
                'lifecycle.postgres_command=lambda *args:[sys.executable,"-c",sys.argv[3],sys.argv[4]]; '
                'guard=state_store.operation_guard(root); guard.__enter__(); lifecycle.backup()')
            worker = subprocess.Popen([sys.executable,'-c',worker_code,str(BUNDLE),str(root),
                child_code,str(marker)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and worker.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), 'Dump child did not start')
                child_pid = int(marker.read_text())
                worker.kill();worker.communicate(timeout=5)
                with self.assertRaisesRegex(RuntimeError, 'Another installer operation'):
                    with lifecycle.state_store.operation_guard(root):
                        pass
                os.kill(child_pid, signal.SIGTERM);child_pid = None
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with lifecycle.state_store.operation_guard(root):
                            pass
                        break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            self.fail('Dump child exit did not release the operation lock')
                        time.sleep(0.02)
            finally:
                if worker.poll() is None:
                    worker.kill();worker.communicate(timeout=5)
                if child_pid is not None:
                    try: os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError: pass

    def test_killed_backup_retains_restart_intent_and_recovery_preserves_prior_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'installation.json').write_text(json.dumps({'domain':'example.invalid'}))
            code=('import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                'import lifecycle; lifecycle.ROOT=Path(sys.argv[2]); '
                'lifecycle.service_rows=lambda:[{"Service":"synapse","State":"running"},'
                '{"Service":"mas","State":"exited"}]; lifecycle.dc=lambda *args:None; '
                'pause=lifecycle.paused_writers(); pause.__enter__(); '
                'print("paused",flush=True); time.sleep(60)')
            worker=subprocess.Popen([sys.executable,'-c',code,str(BUNDLE),str(root)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(worker.stdout.readline().strip(),'paused')
                worker.send_signal(signal.SIGKILL)
                worker.communicate(timeout=5)
            finally:
                if worker.poll() is None:
                    worker.kill();worker.communicate(timeout=5)
            self.assertTrue((root/'backup-writers.json').is_file())
            with self.assertRaisesRegex(RuntimeError,'backup-recover'):
                lifecycle.state_store.assert_update_allowed(root)
            with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'dc') as dc, \
                    patch.object(lifecycle,'wait_writers') as ready,patch.object(lifecycle,'emit'):
                lifecycle.backup_recover()
                self.assertEqual(dc.call_args.args,('up','-d','synapse'))
                ready.assert_called_once_with(['synapse'])
                self.assertFalse((root/'backup-writers.json').exists())
                dc.reset_mock();lifecycle.backup_recover();dc.assert_not_called()

    def test_nested_snapshot_failure_restarts_once_and_failed_restart_keeps_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(lifecycle,'ROOT',root,create=True), \
                    patch.object(lifecycle,'state',return_value={'domain':'example.invalid'}), \
                    patch.object(lifecycle,'service_rows',return_value=[{'Service':'synapse','State':'running'}]), \
                    patch.object(lifecycle,'dc') as dc,patch.object(lifecycle,'wait_writers'),patch.object(lifecycle,'emit'):
                with self.assertRaisesRegex(RuntimeError,'snapshot failed'):
                    with lifecycle.paused_writers():
                        with lifecycle.paused_writers(leave_stopped=True):
                            raise RuntimeError('snapshot failed')
                self.assertEqual([call.args for call in dc.call_args_list],
                    [('stop','-t','30','synapse'),('up','-d','synapse')])
                self.assertFalse((root/'backup-writers.json').exists())
                dc.reset_mock();dc.side_effect=[None,RuntimeError('restart failed')]
                with self.assertRaisesRegex(RuntimeError,'restart failed'):
                    with lifecycle.paused_writers():pass
                self.assertTrue((root/'backup-writers.json').is_file())
                self.assertIsNone(lifecycle._writer_pause_root)

    def test_recovery_never_unfreezes_migration_and_refuses_live_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            record={'format':1,'server_name':'example.invalid','writers':['synapse'],
                'keep_stopped':False,'pid':123456,'started':'unused'}
            lifecycle.state_store.write_json(root/'backup-writers.json',record)
            with patch.object(lifecycle,'ROOT',root,create=True), \
                    patch.object(lifecycle,'state',return_value={'domain':'example.invalid','migration_frozen':True}), \
                    patch.object(lifecycle,'dc') as dc,patch.object(lifecycle,'emit'), \
                    patch.object(lifecycle.state_store,'process_identity',return_value=(1,'unused')):
                with self.assertRaisesRegex(RuntimeError,'still exists'):lifecycle.backup_recover()
                dc.assert_not_called()
                with patch.object(lifecycle.state_store,'process_identity',side_effect=FileNotFoundError):
                    lifecycle.backup_recover()
                self.assertEqual(dc.call_args.args,('stop','-t','30','synapse'))
                self.assertFalse((root/'backup-writers.json').exists())

    def test_frozen_source_and_staged_target_cannot_start_writers(self):
        with tempfile.TemporaryDirectory() as folder:
            for metadata in ({'migration_frozen': True}, {'restore_pending': True}):
                with self.subTest(metadata=metadata), patch.object(lifecycle, 'ROOT', Path(folder), create=True), \
                        patch.object(lifecycle, 'state', return_value=metadata), \
                        patch.object(lifecycle, 'generate') as generate:
                    with self.assertRaises(RuntimeError):
                        lifecycle.up()
                    generate.assert_not_called()

    def test_failed_final_export_keeps_source_frozen_and_stops_writers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(lifecycle, 'ROOT', root, create=True), \
                    patch.object(lifecycle, 'state', return_value={}), \
                    patch.object(lifecycle, 'dc') as dc, \
                    patch.object(lifecycle, 'backup', side_effect=RuntimeError('busy backup lock')):
                with self.assertRaises(RuntimeError):
                    lifecycle.move_export()
            import json
            self.assertTrue(json.loads((root / 'installation.json').read_text())['migration_frozen'])
            self.assertEqual(dc.call_args.args[0], 'stop')

    def test_final_S3_export_hydrates_after_stop_before_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);metadata={'domain':'example.invalid','media_mode':'s3'};events=[]
            def stop(*args):events.append('stop')
            def hydrate(*args):
                self.assertTrue(json.loads((root/'installation.json').read_text())['migration_frozen'])
                events.append('hydrate');return {'s3_objects_verified':1,'missing_local_objects_restored':1}
            def archive(**kwargs):
                self.assertTrue(kwargs['leave_stopped']);events.append('archive');return root/'final.tar.gz'
            with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'state',return_value=metadata), \
                    patch.object(lifecycle,'dc',side_effect=stop),patch.object(lifecycle,'service_rows',return_value=[{'Service':'synapse','State':'exited'}]), \
                    patch.object(lifecycle.preflight,'settings',return_value={'S3_BUCKET':'fictional-bucket'}), \
                    patch.object(lifecycle.media_snapshot,'hydrate',side_effect=hydrate),patch.object(lifecycle,'backup',side_effect=archive),patch.object(lifecycle,'emit'):
                lifecycle.move_export()
            self.assertEqual(events,['stop','hydrate','archive'])

    def test_final_S3_fault_stays_frozen_and_retry_finishes_before_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);metadata={'domain':'example.invalid','media_mode':'s3'}
            with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'state',return_value=metadata), \
                    patch.object(lifecycle,'dc') as dc,patch.object(lifecycle,'service_rows',return_value=[]), \
                    patch.object(lifecycle.preflight,'settings',return_value={}), \
                    patch.object(lifecycle.media_snapshot,'hydrate',side_effect=[OSError('FICTIONAL_PRIVATE_SDK_RESPONSE'),{'s3_objects_verified':1,'missing_local_objects_restored':1}]), \
                    patch.object(lifecycle,'backup',return_value=root/'final.tar.gz') as archive,patch.object(lifecycle,'emit'):
                with self.assertRaises(RuntimeError) as caught:lifecycle.move_export()
                self.assertNotIn('FICTIONAL_PRIVATE_SDK_RESPONSE',str(caught.exception));self.assertIn('move-abort target-stopped',str(caught.exception))
                self.assertTrue(json.loads((root/'installation.json').read_text())['migration_frozen']);archive.assert_not_called()
                lifecycle.move_export();archive.assert_called_once_with(leave_stopped=True)
                self.assertTrue(all(call.args[0]=='stop' for call in dc.call_args_list))

    def test_final_S3_signal_keeps_freeze_without_publishing_archive(self):
        for error in (InterruptedError('controlled signal'),lifecycle.state_store.OperationInterrupted('controlled signal')):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'state',return_value={'media_mode':'s3'}), \
                        patch.object(lifecycle,'dc'),patch.object(lifecycle,'service_rows',return_value=[]), \
                        patch.object(lifecycle.preflight,'settings',return_value={}), \
                        patch.object(lifecycle.media_snapshot,'hydrate',side_effect=error),patch.object(lifecycle,'backup') as archive,patch.object(lifecycle,'emit'):
                    with self.assertRaises(type(error)) as caught:lifecycle.move_export()
                self.assertIs(caught.exception,error);archive.assert_not_called()
                self.assertTrue(json.loads((root/'installation.json').read_text())['migration_frozen'])

    def test_final_S3_requires_stopped_writers_before_resource_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(lifecycle,'ROOT',root,create=True),patch.object(lifecycle,'state',return_value={'media_mode':'s3'}), \
                    patch.object(lifecycle,'dc'),patch.object(lifecycle,'service_rows',return_value=[{'Service':'synapse','State':'restarting'}]), \
                    patch.object(lifecycle.media_snapshot,'hydrate') as hydrate,patch.object(lifecycle,'backup') as archive:
                with self.assertRaisesRegex(RuntimeError,'still running'):lifecycle.move_export()
            hydrate.assert_not_called();archive.assert_not_called()

    def test_child_cannot_consume_commands_after_installer_in_ssh_stream(self):
        # A shared stream carries the next shell command. Child stdin must be EOF.
        script = ('import sys\n'
                  'sys.path.insert(0, ' + repr(str(BUNDLE)) + ')\n'
                  'import lifecycle\n'
                  'print(lifecycle.run([sys.executable,"-c",'
                  '"import sys; print(repr(sys.stdin.read()))"]).strip())\n'
                  'print(sys.stdin.read().strip())\n')
        with tempfile.TemporaryDirectory() as folder:
            runner = Path(folder) / 'runner.py'
            runner.write_text(script)
            result = subprocess.run([sys.executable, str(runner)], input='NEXT_SHELL_COMMAND\n',
                                    capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ["''", 'NEXT_SHELL_COMMAND'])

    def test_failed_command_never_repeats_credentials_from_stderr(self):
        with self.assertRaises(RuntimeError) as caught:
            lifecycle.run([sys.executable, '-c',
                           'import sys; print("EXAMPLE_PRIVATE_SECRET",file=sys.stderr); sys.exit(1)'])
        self.assertNotIn('EXAMPLE_PRIVATE_SECRET', str(caught.exception))

    def test_generation_error_translates_only_known_codes_without_child_text(self):
        for code in ('postgres_destination_changed','unrecognized'):
            output=json.dumps({'check':'configuration_input','code':code,'message':'EXAMPLE_PRIVATE_SECRET'})
            script='import sys; print('+repr(output)+'); print("EXAMPLE_PRIVATE_SECRET",file=sys.stderr); sys.exit(2)'
            with self.assertRaises(RuntimeError) as caught:
                lifecycle.run([sys.executable,'-c',script],configuration_errors=lifecycle.GENERATION_ERRORS)
            message=str(caught.exception)
            self.assertNotIn('EXAMPLE_PRIVATE_SECRET',message)
            if code=='postgres_destination_changed':
                self.assertIn('requires a separate data migration',message)
                self.assertIn('install.sh up',message)
            else:
                self.assertIn('Command failed',message)

    def test_timeout_identifies_service_and_preserving_resume_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(lifecycle, 'ROOT', root, create=True), \
                 patch.object(lifecycle, 'emit'), patch.object(lifecycle.time, 'sleep'):
                with self.assertRaises(RuntimeError) as caught:
                    lifecycle.wait(lambda: False, attempts=2, service='referral-bot')
        message = str(caught.exception)
        self.assertIn('referral-bot did not become ready within 4 seconds', message)
        self.assertIn('logs --tail=100 referral-bot', message)
        self.assertIn('install.sh up', message)
        self.assertIn('Keep existing data and secrets', message)

    def test_writer_readiness_waits_for_restart_and_accepts_previously_stopped_nonwriters(self):
        rows=[{'Service':'synapse','State':'running','Health':'starting'},
            {'Service':'mas','State':'running','Health':''},
            {'Service':'web','State':'exited','Health':''}]
        def wait(probe,**kwargs):
            self.assertFalse(probe())
            rows[0]['Health']='healthy'
            self.assertTrue(probe())
            self.assertEqual(kwargs['service'],'synapse mas')
        with patch.object(lifecycle,'service_rows',return_value=rows),patch.object(lifecycle,'wait',side_effect=wait):
            lifecycle.wait_writers(['synapse','mas'])


if __name__ == '__main__':
    unittest.main()
