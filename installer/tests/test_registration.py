"""Lost API responses must reuse a durable invitation and never grant admin."""
import copy
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import configuration
import registration
import state_store


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'secrets').mkdir(mode=0o700)
        state_store.write_json(self.root/'installation.json',{'domain':'fictional.example.org','databases_initialized':True})
        state_store.write_text(self.root/'settings.env','DOMAIN=fictional.example.org\n')
        self.items={};self.created=0;self.lost=False
        self.owned=patch.object(registration.lifecycle,'ownership_check');self.owned.start()
    def tearDown(self):self.owned.stop();self.temp.cleanup()
    def transport(self,root,path,method='GET',body=None):
        if path==registration.BASE and method=='POST':
            self.created+=1;identifier='0'*25+str(self.created)
            self.items[identifier]={'id':identifier,'attributes':dict(body,times_used=0,revoked_at=None)}
            if self.lost:self.lost=False;raise RuntimeError('FICTIONAL_LOST_RESPONSE')
            return {'status':201,'body':{'data':copy.deepcopy(self.items[identifier])}}
        if path==registration.BASE:return {'status':200,'body':{'data':copy.deepcopy(list(self.items.values()))}}
        identifier=path.split('/')[-2] if path.endswith('/revoke') else path.split('/')[-1]
        if path.endswith('/revoke'):self.items[identifier]['attributes']['revoked_at']='2026-01-01T00:00:00Z'
        return {'status':200,'body':{'data':copy.deepcopy(self.items[identifier])}}
    def test_lost_create_repeat_use_revoke_and_explicit_replacement(self):
        self.lost=True
        with self.assertRaises(RuntimeError):registration.manage(self.root,'ensure',self.transport)
        saved=configuration.read_private_json(self.root/'secrets/first-registration.json')['token']
        result=registration.manage(self.root,'ensure',self.transport);self.assertEqual(self.created,1);self.assertEqual(result['state'],'valid')
        self.assertNotIn(saved,json.dumps(result));self.assertEqual(result['usage_limit'],1)
        identifier=next(iter(self.items));self.items[identifier]['attributes']['times_used']=1
        self.assertEqual(registration.manage(self.root,'ensure',self.transport)['state'],'used');self.assertEqual(self.created,1)
        self.assertEqual(registration.manage(self.root,'revoke',self.transport)['state'],'revoked')
        registration.manage(self.root,'new',self.transport);self.assertEqual(self.created,2)
        self.assertEqual(sum(not x['attributes']['revoked_at'] for x in self.items.values()),1)
        self.assertFalse(any('admin' in item['attributes'] for item in self.items.values()))
    def test_pending_status_does_not_issue_and_local_revoke_prevents_later_ensure(self):
        state_store.write_json(self.root/'secrets/first-registration.json',{'format':1,'domain':'fictional.example.org','token':'FICTIONAL_PENDING',
            'expires_at':'2030-01-01T00:00:00+00:00','state':'pending'})
        self.assertEqual(registration.manage(self.root,'status',self.transport)['state'],'pending');self.assertEqual(self.created,0)
        self.assertEqual(registration.manage(self.root,'revoke',self.transport)['state'],'revoked')
        self.assertEqual(registration.manage(self.root,'ensure',self.transport)['state'],'revoked');self.assertEqual(self.created,0)

    def test_pending_database_and_identity_block_issuance(self):
        state_store.write_json(self.root/'installation.json',{'domain':'fictional.example.org','databases_initialized':False})
        with self.assertRaises(RuntimeError):registration.manage(self.root,'ensure',self.transport)
        self.assertEqual(self.created,0)
