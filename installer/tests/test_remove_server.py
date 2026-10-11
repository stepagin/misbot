import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import remove_server
import update
import state_store

PROFILE={'S3_ENDPOINT_URL':'https://s3.example.org','S3_REGION':'example-region','S3_BUCKET':'fictional-bucket',
         'S3_PREFIX':'example-server/','S3_ACCESS_KEY_ID':'FICTIONAL_ACCESS','S3_SECRET_ACCESS_KEY':'FICTIONAL_SECRET'}


class S3:
    def __init__(self):
        self.versions=[{'Key':'example-server/media','VersionId':'1'},{'Key':'example-server/media','VersionId':'2'}]
        self.markers=[{'Key':'example-server/removed','VersionId':'3'}]
        self.current=[];self.uploads=[{'Key':'example-server/incomplete','UploadId':'up'}];self.calls=[];self.error=False
    def get_paginator(self,name):
        service=self
        class Pages:
            def paginate(self,**args):
                service.calls.append((name,args))
                if name=='list_object_versions':return iter([{'Versions':list(service.versions),'DeleteMarkers':list(service.markers)}])
                if name=='list_objects_v2':return iter([{'Contents':list(service.current)}])
                return iter([{'Uploads':list(service.uploads)}])
        return Pages()
    def delete_objects(self,**args):
        self.calls.append(('delete_objects',args))
        if self.error:return {'Errors':[{'Key':'example-server/media'}]}
        self.versions=[];self.markers=[];self.current=[];return {}
    def abort_multipart_upload(self,**args):self.calls.append(('abort',args));self.uploads=[]


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'server';self.root.mkdir(mode=0o700)
        state_store.write_json(self.root/'installation.json',{'domain':'example.org','config_format_version':2})
        files={name:b'# fixture public source\n' for name in update.ESSENTIAL}
        files['release.json']=json.dumps({'family':update.FAMILY,'version':'1.1.0','update_scope':'source-configuration-and-approved-components'}).encode()
        files['federation-bot/logic.js']=b'// public fixture\n'
        for name,raw in files.items():
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        (self.root/'SHA256SUMS').write_text(''.join(hashlib.sha256(raw).hexdigest()+'  '+name+'\n' for name,raw in files.items()))
        for name in ('config','secrets','data','stages','settings','backups'):
            (self.root/name).mkdir(mode=0o700);(self.root/name/'private-fixture').write_text('fictional-data')
    def tearDown(self):self.temp.cleanup()
    def record(self):return {'format':1,'domain':'example.org','directory':str(self.root),
        'local':dict.fromkeys(remove_server.LOCAL,True),'s3':[],'external_database':False,'vault':False,'completed':[]}
    def test_no_deletion_until_every_confirmation_and_exact_domain(self):
        answers=iter(['да','да','да','да','да','WRONG'])
        with self.assertRaises(RuntimeError):remove_server.plan(self.root,[],{},lambda _:next(answers),lambda _:None)
        self.assertTrue((self.root/'data/private-fixture').is_file());self.assertFalse((self.root/'remove-journal.json').exists())
        self.assertIsNone(remove_server.plan(self.root,[],{},lambda _:'нет',lambda _:None))
    def test_init_only_full_removal_keeps_utility_and_forgets_all_bot_settings(self):
        (self.root/'federation-bot/untracked-private.env').write_text('fictional secret')
        with patch.object(remove_server,'remove_containers'),patch.object(remove_server,'host_cleanup'):
            remove_server.execute(self.root,self.record(),[],stop=lambda:None)
        self.assertTrue((self.root/'install.sh').is_file());self.assertTrue((self.root/'release.json').is_file())
        self.assertTrue((self.root/'.misbot-empty.json').is_file());self.assertFalse((self.root/'installation.json').exists())
        for name in ('data','config','settings','secrets','stages','backups'):self.assertFalse((self.root/name).exists())
        self.assertTrue((self.root/'federation-bot/logic.js').exists())
        self.assertFalse((self.root/'federation-bot/untracked-private.env').exists())
    def test_partial_selection_retains_chosen_data_and_marks_decommissioned(self):
        record=self.record();record['local']['data']=False
        with patch.object(remove_server,'remove_containers'):
            remove_server.execute(self.root,record,[],stop=lambda:None)
        self.assertTrue((self.root/'data/private-fixture').exists())
        self.assertTrue(json.loads((self.root/'installation.json').read_text())['decommissioned'])
    def test_s3_failure_keeps_local_credentials_and_journal_for_retry(self):
        record=self.record();record['s3']=[remove_server.s3_identity(PROFILE)]
        targets=[{'profile':PROFILE,'identity':record['s3'][0]}]
        with patch.object(remove_server,'delete_s3',side_effect=RuntimeError('retention')):
            with self.assertRaises(RuntimeError):remove_server.execute(self.root,record,targets,stop=lambda:None)
        self.assertTrue((self.root/'secrets/private-fixture').exists());self.assertTrue((self.root/'remove-journal.json').exists())
        self.assertEqual(record['completed'],['services-stopped'])
    def test_s3_deletes_versions_markers_uploads_and_only_selected_prefix(self):
        service=S3();remove_server.delete_s3(PROFILE,service)
        self.assertFalse(service.versions+service.markers+service.uploads)
        for name,args in service.calls:
            self.assertEqual(args['Bucket'],PROFILE['S3_BUCKET'])
            if name.startswith('list_'):self.assertEqual(args['Prefix'],PROFILE['S3_PREFIX'])
        self.assertFalse(any(name=='delete_bucket' for name,args in service.calls))
    def test_s3_foreign_key_and_per_object_errors_refuse_completion(self):
        service=S3();service.versions=[{'Key':'other/data','VersionId':'1'}]
        with self.assertRaisesRegex(RuntimeError,'outside'):remove_server.delete_s3(PROFILE,service)
        self.assertFalse(any(name=='delete_objects' for name,args in service.calls))
        service=S3();service.error=True
        with self.assertRaisesRegex(RuntimeError,'refused'):remove_server.delete_s3(PROFILE,service)
    def test_symlink_inside_local_data_never_deletes_external_target(self):
        outside=Path(self.temp.name)/'outside';outside.mkdir();(outside/'keep').write_text('private fixture')
        (self.root/'data/external').symlink_to(outside,target_is_directory=True)
        remove_server.remove_path(self.root/'data');self.assertTrue((outside/'keep').is_file())
    def test_mount_in_scope_refuses_removal(self):
        with patch.object(remove_server.os.path,'ismount',side_effect=lambda p:Path(p)==self.root/'data'):
            with self.assertRaisesRegex(RuntimeError,'mounted'):remove_server.remove_path(self.root/'data')
        self.assertTrue((self.root/'data/private-fixture').is_file())
    def test_retry_finalizes_after_installation_metadata_was_already_removed(self):
        record=self.record()
        with patch.object(remove_server,'remove_containers'),patch.object(remove_server,'host_cleanup'):
            remove_server.execute(self.root,record,[],stop=lambda:None)
        state_store.write_json(self.root/'remove-journal.json',record)
        remove_server.execute(self.root,record,[],stop=lambda:self.fail('Service stop repeated'))
        self.assertFalse((self.root/'remove-journal.json').exists())
        self.assertTrue((self.root/'.misbot-empty.json').exists())
    def test_active_owned_update_worker_is_stopped_and_foreign_unit_refused(self):
        import release_update
        folder=self.root/'data/admin-panel';folder.mkdir(mode=0o700)
        identifier='a'*64;unit=release_update.unit(self.root,identifier)
        job={'id':identifier,'unit':unit,'state':'running'}
        state_store.write_json(release_update.job_path(self.root),job)
        with patch.object(remove_server.subprocess,'run',return_value=type('Result',(),{'returncode':0,'stdout':'loaded\n'})()),patch.object(remove_server.lifecycle,'run') as stop:
            remove_server.stop_update(self.root)
            stop.assert_called_once_with(['systemctl','stop',unit+'.service'])
        job['unit']='foreign-service';state_store.write_json(release_update.job_path(self.root),job)
        with patch.object(remove_server.subprocess,'run') as command:
            with self.assertRaisesRegex(RuntimeError,'Foreign'):remove_server.stop_update(self.root)
            command.assert_not_called()


if __name__=='__main__':unittest.main()
