import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import admin_identity
import federation_runtime
import state_store


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'config').mkdir(mode=0o700)
        (self.root/'data/referral-control').mkdir(parents=True,mode=0o700)
        self.metadata={'domain':'example.org'}
        state_store.write_json(self.root/'installation.json',self.metadata)
    def tearDown(self):self.temp.cleanup()
    def test_default_local_choice_and_immutable_created_tag(self):
        self.assertEqual(federation_runtime.identity(self.root,self.metadata)['user_id'],'@admin-panel:example.org')
        chosen=admin_identity.choose(self.root,'@ops','@owner:example.org')
        self.assertEqual(chosen['user_id'],'@ops:example.org')
        self.metadata['federation_provisioned']=True
        state_store.write_json(self.root/'installation.json',self.metadata)
        with self.assertRaisesRegex(RuntimeError,'изменить нельзя'):
            admin_identity.choose(self.root,'renamed')
        self.assertEqual(admin_identity.choose(self.root,'ops'),chosen)
    def test_existing_different_tag_is_saved_and_reused(self):
        state_store.write_json(self.root/'data/referral-control/federation-session.json',{'config':{
            'user_id':'@federation-bot:example.org'}})
        state_store.write_json(self.root/'config/federation-bot.json',{
            'user_id':'@federation-bot:example.org','administrator_mxids':['@owner:example.org','@another:example.org']})
        chosen=federation_runtime.identity(self.root,self.metadata)
        self.assertEqual(chosen['user_id'],'@federation-bot:example.org')
        self.assertEqual(chosen['administrator_mxid'],'@owner:example.org')
        self.assertEqual(json.loads((self.root/'config/admin-panel-identity.json').read_text()),chosen)
        with self.assertRaises(RuntimeError):admin_identity.choose(self.root,'admin-panel')
    def test_foreign_tags_and_colliding_bots_are_refused(self):
        for tag in ('@owner:foreign.example','@server-guide','@referral-bot','@matrix-admin'):
            with self.subTest(tag=tag),self.assertRaises(ValueError):admin_identity.choose(self.root,tag)


if __name__=='__main__':unittest.main()
