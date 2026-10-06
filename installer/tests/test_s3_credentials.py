"""Credential replacement must keep destination, data and forward recovery."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import media_transfer
import s3_credentials as operation
import state_store


class S3CredentialTests(unittest.TestCase):
    def test_cli_errors_have_action_specific_commands_and_omit_driver_secrets(self):
        from argparse import Namespace
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);input_path=root/'private input.env'
            for action,pending in (('rotate',False),('recover',True),('cleanup',False),('cleanup',True)):
                with self.subTest(action=action,pending=pending):
                    if pending:state_store.write_text(root/operation.JOURNAL,'FICTIONAL_PENDING')
                    else:(root/operation.JOURNAL).unlink(missing_ok=True)
                    args=Namespace(directory=root,settings=input_path,action=action)
                    output=io.StringIO()
                    with patch.object(operation,'parse_arguments',return_value=args), \
                            patch.object(operation,'main',side_effect=RuntimeError('FICTIONAL_DRIVER_SECRET')), \
                            redirect_stdout(output):
                        self.assertEqual(operation.run_cli(),1)
                    result=json.loads(output.getvalue())
                    command='s3-credentials-recover' if pending else ('s3-credentials-cleanup' if action=='cleanup' else 's3-credentials')
                    self.assertIn(str(root/'install.sh')+' '+command,result['next_action'])
                    if action=='rotate' and not pending:self.assertIn("'"+str(input_path)+"'",result['next_action'])
                    self.assertNotIn('FICTIONAL_DRIVER_SECRET',output.getvalue())

    def fixture(self, root):
        for name in ('stages', 'secrets', 'config'):
            (root / name).mkdir()
        values = {'S3_BUCKET':'fictional-bucket', 'S3_REGION':'fictional-region',
            'S3_ENDPOINT_URL':'https://s3.fictional.invalid', 'S3_PREFIX':'fictional/media/',
            'S3_ACCESS_KEY_ID':'FICTIONAL_OLD_ACCESS', 'S3_SECRET_ACCESS_KEY':'FICTIONAL_OLD_SECRET'}
        state_store.write_text(root / 'settings.env', ''.join(k+'='+v+'\n' for k,v in values.items())+'ACME_EMAIL=fictional@example.org\nDOMAIN=fictional.invalid\nMEDIA_MODE=s3\n')
        metadata = {'domain':'fictional.invalid', 'databases_initialized':True,
            'storage_committed':True, 'network_committed':True, 'media_mode':'s3',
            's3_identity':media_transfer.identity(values)}
        state_store.write_json(root / 'installation.json', metadata)
        state_store.write_text(root / 'secrets/identity', 'FICTIONAL_KEEP_IDENTITY')
        new = dict(values, S3_ACCESS_KEY_ID='FICTIONAL_NEW_ACCESS', S3_SECRET_ACCESS_KEY='FICTIONAL_NEW_SECRET')
        path = root / 'replacement.env'
        state_store.write_text(path, ''.join(k+'='+v+'\n' for k,v in new.items()))
        return values, new, path, metadata

    def context(self, root):
        def dc(*args):
            return 'fictional-container\n' if args[:2] == ('ps','-q') else ''
        return patch.multiple(lifecycle, create=True, ROOT=root,
            COMPOSE=['docker','compose','--project-directory',str(root)],
            state=lambda:json.loads((root/'installation.json').read_text()),
            dc=Mock(side_effect=dc), backup=Mock(), restore_check=Mock(),
            service_rows=Mock(return_value=[]), generate=Mock(), check=Mock(),
            external_database=Mock(return_value=False), certificate_watch=Mock(),
            wait_writers=Mock(), run=Mock(return_value='sha256:'+'a'*64+'\n'), emit=Mock())

    def service(self):
        service = Mock()
        service.list_objects_v2.return_value = {'Contents':[]}
        service.put_object.return_value = {}
        service.get_object.side_effect = lambda **kw: {'Body':io.BytesIO(operation.PROBE)}
        return service

    def test_wrong_credentials_and_changed_destination_do_not_change_files_or_stop_services(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old,new,path,_=self.fixture(root)
            original=(root/'settings.env').read_bytes()
            for case in ('credential_failure','bucket','region','endpoint','prefix'):
                changed=dict(new)
                for field in ('S3_BUCKET','S3_REGION','S3_ENDPOINT_URL','S3_PREFIX'):
                    if field.lower().removeprefix('s3_').removesuffix('_url') == case:
                        changed[field]='FICTIONAL_OTHER_DESTINATION'
                state_store.write_text(path,''.join(k+'='+v+'\n' for k,v in changed.items()))
                service=self.service()
                if case=='credential_failure':service.head_bucket.side_effect=RuntimeError('FICTIONAL_PRIVATE_SDK_ERROR')
                with self.subTest(case=case),self.context(root),patch.object(media_transfer,'client',return_value=service):
                    with self.assertRaises(Exception):operation.rotate(root,path)
                    lifecycle.backup.assert_not_called();lifecycle.dc.assert_not_called()
                self.assertEqual((root/'settings.env').read_bytes(),original)
                self.assertFalse((root/operation.JOURNAL).exists())

    def test_same_pair_is_only_a_validated_noop_and_closes_sample_get_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old,_,path,_=self.fixture(root)
            state_store.write_text(path,''.join(k+'='+v+'\n' for k,v in old.items()))
            service=self.service();body=io.BytesIO(b'FICTIONAL_MEDIA')
            service.list_objects_v2.return_value={'Contents':[{'Key':old['S3_PREFIX']+'sample'}]}
            service.get_object.side_effect=None;service.get_object.return_value={'Body':body}
            with self.context(root),patch.object(media_transfer,'client',return_value=service):
                operation.rotate(root,path)
                lifecycle.backup.assert_not_called();lifecycle.dc.assert_not_called()
                self.assertFalse(lifecycle.emit.call_args.kwargs['changed'])
            self.assertTrue(body.closed)
            self.assertFalse((root/operation.JOURNAL).exists())

    def test_failed_restore_gate_never_creates_checkpoint_or_applies_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old,_,path,_=self.fixture(root)
            with self.context(root),patch.object(media_transfer,'client',return_value=self.service()), \
                    patch.object(lifecycle,'restore_check',side_effect=RuntimeError('bad backup')):
                with self.assertRaises(RuntimeError):operation.rotate(root,path)
                self.assertEqual(lifecycle.dc.call_count,1)  # Only read-only container lookup.
            self.assertFalse((root/operation.JOURNAL).exists())
            self.assertEqual(operation.preflight.settings(root)['S3_SECRET_ACCESS_KEY'],old['S3_SECRET_ACCESS_KEY'])

    def test_failure_after_publication_blocks_other_operations_and_forward_recover_uses_same_pair(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,new,path,metadata=self.fixture(root)
            service=self.service()
            with self.context(root),patch.object(media_transfer,'client',return_value=service), \
                    patch.object(operation.vault_cache,'mark_dirty'),patch.object(operation.vault_cache,'sync_optional'), \
                    patch.object(lifecycle,'generate',side_effect=[RuntimeError('interrupted'),None]):
                with self.assertRaises(RuntimeError):operation.rotate(root,path)
                self.assertTrue((root/operation.JOURNAL).exists())
                with patch.dict(os.environ,{},clear=True):
                    for guard in (state_store.assert_update_allowed,state_store.assert_database_allowed):
                        with self.assertRaisesRegex(RuntimeError,'s3-credentials-recover'):guard(root)
                operation.recover(root)
                self.assertFalse((root/operation.JOURNAL).exists())
                self.assertEqual(service.put_object.call_count,1)
                operation.recover(root)
                self.assertFalse(lifecycle.emit.call_args.kwargs['changed'])
            actual=operation.preflight.settings(root)
            self.assertEqual(actual['S3_SECRET_ACCESS_KEY'],new['S3_SECRET_ACCESS_KEY'])
            self.assertEqual(actual['ACME_EMAIL'],'fictional@example.org')
            self.assertEqual(json.loads((root/'installation.json').read_text()),metadata)
            self.assertEqual((root/'secrets/identity').read_text(),'FICTIONAL_KEEP_IDENTITY')

    def test_failed_put_with_successful_cleanup_is_not_mistaken_for_verified_write_on_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,new,_,_=self.fixture(root)
            stage=root/'stages'/('a'*32);stage.mkdir(mode=0o700)
            service=self.service();service.put_object.side_effect=[RuntimeError('denied'),{}]
            with self.context(root):
                with self.assertRaises(RuntimeError):operation.probe_write(root,stage,service,new)
                saved=operation.private_json(stage/'probe.json')
                self.assertTrue(saved['deleted']);self.assertFalse(saved['verified_write'])
                operation.probe_write(root,stage,service,new)
                saved=operation.private_json(stage/'probe.json')
                self.assertTrue(saved['verified_write']);self.assertTrue(saved['deleted'])
            self.assertEqual(service.put_object.call_count,2)

    def test_paused_synapse_blocks_local_key_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old,_,path,_=self.fixture(root)
            with self.context(root),patch.object(media_transfer,'client',return_value=self.service()), \
                    patch.object(lifecycle,'service_rows',return_value=[{'Service':'synapse','State':'paused'}]):
                with self.assertRaisesRegex(RuntimeError,'did not stop'):operation.rotate(root,path)
                lifecycle.generate.assert_not_called()
            self.assertEqual(operation.preflight.settings(root)['S3_SECRET_ACCESS_KEY'],old['S3_SECRET_ACCESS_KEY'])

    def test_probe_cleanup_never_deletes_unowned_or_unrelated_objects(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,new,_,_=self.fixture(root)
            parent=root/'stages/s3-credentials';parent.mkdir()
            stage=parent/('a'*32);stage.mkdir()
            probe={'format':1,'server_name':'fictional.invalid','destination':media_transfer.identity(new),
                'object_key':new['S3_PREFIX']+'real-user-media','deleted':False,'verified_write':True,'version_id':None}
            state_store.write_json(stage/'probe.json',probe)
            service=self.service()
            with self.context(root),patch.object(media_transfer,'client',return_value=service):
                with self.assertRaisesRegex(RuntimeError,'not owned'):operation.cleanup(root)
            service.delete_object.assert_not_called()

    def test_versioned_probe_cleanup_targets_only_recorded_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,new,_,_=self.fixture(root)
            stage=root/'stages'/('a'*32);stage.mkdir()
            service=self.service();service.put_object.return_value={'VersionId':'FICTIONAL_VERSION'}
            with self.context(root):operation.probe_write(root,stage,service,new)
            self.assertEqual(service.delete_object.call_args.kwargs['VersionId'],'FICTIONAL_VERSION')

    def test_status_does_not_read_credential_checkpoint_and_has_exact_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root)
            state_store.write_text(root/operation.JOURNAL,'NOT_JSON_FICTIONAL_PRIVATE_CONTENT')
            with self.context(root),patch.object(operation,'private_json',side_effect=AssertionError('must not read')):
                lifecycle.status()
                self.assertTrue(lifecycle.emit.call_args.kwargs['s3_credential_recovery_pending'])
                self.assertIn('sudo bash '+str(root/'install.sh')+' s3-credentials-recover',lifecycle.emit.call_args.kwargs['next_actions'])

    def test_checkpoint_hardlinks_and_fifo_are_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'input';state_store.write_json(path,{'fictional':True})
            os.link(path,root/'linked')
            with self.assertRaises(RuntimeError):operation.private_json(path)
            with self.assertRaises(RuntimeError):operation.private_profile(path)
            os.mkfifo(root/'fifo',0o600)
            with self.assertRaises(RuntimeError):operation.private_json(root/'fifo')
            with self.assertRaises(RuntimeError):operation.private_profile(root/'fifo')


if __name__=='__main__':
    unittest.main()
