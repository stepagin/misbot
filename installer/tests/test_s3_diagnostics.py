"""Fictional SDK/transport failures; never touches an external bucket."""
import io
import json
import os
from pathlib import Path
import socket
import ssl
import signal
import contextlib
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import s3_config
import s3_diagnostics as diagnostic


class SDKError(Exception):
    def __init__(self,code,status=403):
        super().__init__('FICTIONAL_SECRET signed URL response body')
        self.response={'Error':{'Code':code,'Message':'FICTIONAL_SECRET'},'ResponseMetadata':{'HTTPStatusCode':status}}


class Service:
    def __init__(self,fail=None):
        self.fail=fail or {};self.objects={};self.deleted=[];self.puts=[];self.version='fictional-version'
        self.meta=SimpleNamespace(endpoint_url='https://s3.fictional.invalid',
            service_model=SimpleNamespace(operation_model=lambda name:SimpleNamespace(input_shape=SimpleNamespace(members={'IfNoneMatch':True}))))
    def fault(self,stage):
        if stage in self.fail:raise self.fail[stage]
    def head_bucket(self,**args):self.fault('head')
    def put_object(self,**args):
        self.fault('put');assert args.get('IfNoneMatch')=='*'
        assert args['Key'] not in self.objects
        self.puts.append(args);self.objects[args['Key']]=(args['Body'],args['Metadata'])
        self.fault('lost_put');return {'VersionId':self.version}
    def get_object(self,**args):
        self.fault('get')
        if args['Key'] not in self.objects:raise SDKError('NoSuchKey',404)
        body,metadata=self.objects[args['Key']]
        return {'Body':io.BytesIO(body),'Metadata':metadata,'VersionId':self.version}
    def delete_object(self,**args):
        self.fault('delete');self.deleted.append(args);self.objects.pop(args['Key'],None)


def transport(endpoint,emit):
    for name in ('dns','connection','tls'):emit(name,'passed','fictional_'+name,'Fictional transport')


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.config=self.root/'private config.env'
        self.values={'S3_BUCKET':'fictional-bucket','S3_REGION':'us-east-1','S3_ENDPOINT_URL':'https://s3.fictional.invalid',
            'S3_ACCESS_KEY_ID':'FICTIONAL_ACCESS','S3_SECRET_ACCESS_KEY':'FICTIONAL_SECRET','S3_PREFIX':'fictional/media/'}
        self.write();self.catalogs=self.root/'catalogs'
        (self.root/'installation.json').write_text(json.dumps({'domain':'fictional.invalid','storage_committed':True,'media_mode':'s3'}))
        (self.root/'identity').write_text('FICTIONAL_KEEP')
        self.before={p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
    def tearDown(self):self.temp.cleanup()
    def write(self):
        self.config.write_text(''.join(k+'='+v+'\n' for k,v in self.values.items()));self.config.chmod(0o600)
    def run_check(self,service=None,**options):
        return diagnostic.diagnose(self.config,options.pop('purpose','media'),directory=self.root,
            catalog_directory=self.catalogs,service=service or Service(),transport=transport,**options)
    def assert_safe(self,result):
        text=json.dumps(result)
        for marker in ('FICTIONAL_SECRET','FICTIONAL_ACCESS','signed URL response body'):self.assertNotIn(marker,text)
        self.assertEqual(self.before,{p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_success_repeat_unique_exact_version_cleanup(self):
        service=Service()
        for _ in range(2):
            result=self.run_check(service);self.assertTrue(result['passed'],result)
            self.assertEqual(result['capabilities'],{'read':True,'write':True,'cleanup':True});self.assert_safe(result)
        self.assertEqual(len({p['Key'] for p in service.puts}),2)
        self.assertTrue(all(d['VersionId']==service.version for d in service.deleted))
        self.assertEqual(service.objects,{})
        for path in self.catalogs.iterdir():
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(diagnostic.private_catalog(path)['state'],'cleaned')

    def test_backup_private_and_public_are_distinct(self):
        calls=[]
        result=self.run_check(purpose='backup',privacy=lambda *a:calls.append(True))
        self.assertTrue(result['passed']);self.assertEqual(calls,[True])
        def public(*args):raise diagnostic.Failure('backup_public_access','Fictional public bucket denied')
        service=Service();result=self.run_check(service,purpose='backup',privacy=public)
        self.assertFalse(result['passed']);self.assertEqual(result['stages'][-2]['code'],'backup_public_access')
        self.assertEqual(service.objects,{})

    def test_read_only_never_probes_get_put_delete(self):
        service=Service({'put':AssertionError(),'get':AssertionError(),'delete':AssertionError()})
        result=self.run_check(service,read_only=True)
        self.assertTrue(result['passed']);self.assertFalse(any(result['capabilities'].values()))
        self.assertEqual([s['status'] for s in result['stages'][-3:]],['not_checked']*3)
        self.assertFalse(self.catalogs.exists())

    def test_parameter_errors_are_literal_and_private(self):
        variants=[({'S3_REGION':''},'region_required'),({'S3_BUCKET':''},'bucket_required'),
            ({'S3_ENDPOINT_URL':'https://user:FICTIONAL_SECRET@fictional.invalid'},'endpoint_format'),
            ({'S3_ENDPOINT_URL':'http://fictional.invalid'},'endpoint_format'),({'S3_ENDPOINT_URL':'https://fictional.invalid:bad'},'endpoint_format'),
            ({'S3_SECRET_ACCESS_KEY':''},'credentials_pair_required'),({'S3_PREFIX':'../media/'},'prefix_format')]
        for change,code in variants:
            with self.subTest(code=code):
                values=dict(self.values,**change)
                with self.assertRaises(s3_config.ConfigError) as caught:s3_config.validate(values)
                self.assertEqual(caught.exception.code,code)
        original=self.config.read_text()
        for extra,code in (('UNKNOWN=FICTIONAL_SECRET\n','unknown_parameters'),('S3_BUCKET=other-bucket\n','duplicate_parameters')):
            self.config.write_text(original+extra)
            with self.assertRaises(s3_config.ConfigError) as caught:s3_config.read_profile(self.config)
            self.assertEqual(caught.exception.code,code);self.assertNotIn('FICTIONAL_SECRET',str(caught.exception))
        self.config.write_text(original);self.config.chmod(0o644)
        with self.assertRaises(s3_config.ConfigError):s3_config.read_profile(self.config)
        self.config.chmod(0o600)
        alias=self.root/'symlink';alias.symlink_to(self.config)
        with self.assertRaises(s3_config.ConfigError):s3_config.read_profile(alias)
        self.values['S3_SECRET_ACCESS_KEY']='$(touch /tmp/fictional-shell-execution)';self.write()
        self.assertEqual(s3_config.read_profile(self.config)['S3_SECRET_ACCESS_KEY'],self.values['S3_SECRET_ACCESS_KEY'])
        self.assertFalse(Path('/tmp/fictional-shell-execution').exists())

    def test_credentials_signature_region_time_bucket_and_temporary_failures(self):
        for code in ('InvalidAccessKeyId','SignatureDoesNotMatch','AuthorizationHeaderMalformed','RequestTimeTooSkewed',
                'NoSuchBucket','AccessDenied','SlowDown','ServiceUnavailable','FICTIONAL_SECRET_CODE'):
            with self.subTest(code=code):
                result=self.run_check(Service({'head':SDKError(code)}))
                self.assertFalse(result['passed']);self.assert_safe(result)
                self.assertEqual(result['stages'][-1]['stage'],'authentication')
                if code=='AccessDenied':self.assertIn('либо',result['conclusion'])

    def test_read_write_delete_denials_and_safe_cleanup_retry(self):
        for name,stage in (('get','read'),('put','write'),('delete','cleanup')):
            with self.subTest(name=name):
                service=Service({name:SDKError('AccessDenied')});result=self.run_check(service)
                self.assertFalse(result['passed']);self.assert_safe(result)
                self.assertTrue(any(s['stage']==stage and s['status']=='failed' for s in result['stages']))
                if name!='put':
                    self.assertTrue(result['cleanup_pending'])
                    self.assertIn('storage s3 cleanup --config',result['next_actions'][0])
                    self.assertTrue(result['capabilities']['write'])
                    service.fail.clear()
                    path=next(p for p in self.catalogs.iterdir() if diagnostic.private_catalog(p)['state']!='cleaned')
                    cleanup=self.run_check(service,cleanup_catalog=path)
                    self.assertTrue(cleanup['passed']);self.assertEqual(service.objects,{})
                    self.assertTrue(self.run_check(service,cleanup_catalog=path)['passed'])
                else:self.assertFalse(service.deleted)

    def test_lost_put_reply_and_interrupt_clean_only_proved_object(self):
        for error,exit_code in ((TimeoutError(),1),(InterruptedError(),130)):
            service=Service({'lost_put':error});result=self.run_check(service)
            self.assertEqual(result['exit_code'],exit_code);self.assertFalse(result['cleanup_pending'])
            self.assertEqual(service.objects,{});self.assert_safe(result)

    def test_bad_object_proof_never_deleted(self):
        service=Service({'delete':SDKError('AccessDenied')});result=self.run_check(service)
        path=next(self.catalogs.iterdir());key=next(iter(service.objects))
        service.objects[key]=(b'FICTIONAL_USER_DATA',{'misbot-probe':'not-owned'})
        service.fail.clear();result=self.run_check(service,cleanup_catalog=path)
        self.assertFalse(result['passed']);self.assertFalse(service.deleted)
        self.assertTrue(result['cleanup_pending']);self.assert_safe(result)

    def test_http_metadata_key_case_is_insensitive(self):
        service=Service()
        get=service.get_object
        def mixed_case(**args):
            response=get(**args)
            response['Metadata']={k.title():v for k,v in response['Metadata'].items()}
            return response
        service.get_object=mixed_case
        result=self.run_check(service)
        self.assertTrue(result['passed'],result);self.assertEqual(service.objects,{})

    def test_older_sdk_condition_is_added_before_signing(self):
        service=Service();hooks=[]
        service.meta.service_model.operation_model=lambda name:SimpleNamespace(input_shape=SimpleNamespace(members={}))
        service.meta.events=SimpleNamespace(register=lambda event,handler:hooks.append((event,handler)))
        def put(**args):
            request=SimpleNamespace(headers={})
            for event,handler in hooks:
                self.assertEqual(event,'before-sign.s3.PutObject');handler(request)
            self.assertEqual(request.headers['If-None-Match'],'*')
            self.assertNotIn('IfNoneMatch',args)
            service.puts.append(args);service.objects[args['Key']]=(args['Body'],args['Metadata'])
            return {}
        service.put_object=put
        self.assertTrue(self.run_check(service)['passed'])

    def test_catalog_destination_tamper_refused(self):
        service=Service({'delete':SDKError('AccessDenied')});self.run_check(service)
        path=next(self.catalogs.iterdir());record=diagnostic.private_catalog(path)
        record['key']='fictional/user-media';diagnostic.save_catalog(path,record)
        service.fail.clear();result=self.run_check(service,cleanup_catalog=path)
        self.assertFalse(result['passed']);self.assertFalse(service.deleted)

    def test_network_stages_and_errors_do_not_leak(self):
        for stage,error,code in (('dns',socket.gaierror('FICTIONAL_SECRET'),'dns_failed'),
                ('connection',ConnectionRefusedError('FICTIONAL_SECRET'),'connection_failed'),
                ('connection',TimeoutError('FICTIONAL_SECRET'),'timeout'),
                ('tls',ssl.SSLError('FICTIONAL_SECRET'),'tls_failed')):
            def broken(endpoint,emit):emit(stage,'running','started','Fictional');raise error
            result=diagnostic.diagnose(self.config,'media',service=Service(),transport=broken)
            self.assertEqual(result['stages'][-1]['stage'],stage);self.assertEqual(result['stages'][-1]['code'],code)
            self.assertNotIn('FICTIONAL_SECRET',json.dumps(result))

    def test_dependency_failure_is_not_a_provider_failure(self):
        with patch.object(diagnostic.media_transfer,'client',side_effect=ModuleNotFoundError('FICTIONAL_SECRET')):
            result=diagnostic.diagnose(self.config,'media')
        self.assertEqual(result['stages'][-1]['code'],'dependency_missing')
        self.assertIn('apt-get install python3-boto3',result['conclusion']);self.assert_safe(result)

    def test_actual_sigterm_returns_json_and_cleans_probe(self):
        if os.geteuid()!=0:self.skipTest('isolated root main requires root-owned profile')
        service=Service();put=service.put_object
        def interrupted_put(**args):
            put(**args)
            os.kill(os.getpid(),signal.SIGTERM)
        service.put_object=interrupted_put
        original=diagnostic.diagnose;output=io.StringIO()
        handlers={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
        try:
            with patch.object(sys,'argv',['s3_diagnostics.py','check','--config',str(self.config),'--purpose','media',
                    '--catalog-directory',str(self.catalogs),'--json']), \
                    patch.object(diagnostic,'diagnose',side_effect=lambda *a,**kw:original(*a,**kw,service=service,transport=transport)),contextlib.redirect_stdout(output):
                self.assertEqual(diagnostic.main(),130)
        finally:
            for sig,handler in handlers.items():signal.signal(sig,handler)
        result=json.loads(output.getvalue());self.assertEqual(result['exit_code'],130)
        self.assertFalse(result['cleanup_pending']);self.assertEqual(service.objects,{})


if __name__=='__main__':unittest.main()
