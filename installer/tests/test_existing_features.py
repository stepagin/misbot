import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import existing_compose as existing
import existing_features as features
import existing_update
import command_wrapper
import homepage
import misbot
from test_existing_compose import ExistingTests, BUNDLE


class FeatureTests(unittest.TestCase):
    def test_external_runtime_mount_is_limited_to_registered_readonly_referral_socket(self):
        installation=object.__new__(existing.Installation)
        source='/run/misbot-fictional'
        installation.model={'external_runtime_mounts':[{'container':'misbot-referral-bot','source':source,'target':'/run/control'}]}
        volume={'source':source,'target':'/run/control','read_only':True}
        info=SimpleNamespace(st_uid=0,st_gid=10001,st_mode=stat.S_IFDIR|0o750)
        with patch.object(Path,'lstat',return_value=info),patch.object(Path,'resolve',lambda path:path):
            self.assertTrue(installation.accepted_runtime_mount('misbot-referral-bot',volume))
            self.assertFalse(installation.accepted_runtime_mount('matrix-synapse',volume))
            self.assertFalse(installation.accepted_runtime_mount('misbot-referral-bot',dict(volume,read_only=False)))
            self.assertFalse(installation.accepted_runtime_mount('misbot-referral-bot',dict(volume,source='/etc')))
            info.st_mode=stat.S_IFDIR|0o777
            self.assertFalse(installation.accepted_runtime_mount('misbot-referral-bot',volume))

    def test_caddy_logging_and_tls_remain_site_options(self):
        body='\n log {\n output file /fictional/access.log\n format json\n }\n tls fictional@example.org\n handle / {\n respond `{"a":"}"}`\n }\n'
        options,handlers=features.split_site_options(body)
        self.assertIn('log {',options)
        self.assertIn('tls fictional@example.org',options)
        self.assertNotIn('log {',handlers)
        self.assertIn('respond `{"a":"}"}`',handlers)
        with self.assertRaises(existing.Error):features.split_site_options('\n import fictional\n')

    def test_caddy_nested_json_quotes_and_tokens_preserve_other_sites(self):
        text='other.example.org {\n respond "unchanged"\n}\nfictional.example.org {\n handle /discovery {\n  respond `{"value":"}"}`\n }\n reverse_proxy 127.0.0.1:8008\n}\n'
        a,b=features.caddy_block(text,'fictional.example.org')
        self.assertIn('reverse_proxy 127.0.0.1:8008',text[a:b])
        self.assertIn('respond `{"value":"}"}`',text[a:b])
        self.assertEqual(text[:a]+text[a:b]+text[b:],text)
        with self.assertRaises(existing.Error):features.caddy_block(text+text,'fictional.example.org')

    def test_homepage_uses_adopted_client_and_identity_without_double_prefix(self):
        model={'DOMAIN':'matrix.fictional.example.org','SITE_NAME':'<fictional>'}
        links={'client':'https://chat.fictional.example.org/','login':'https://chat.fictional.example.org/#/login',
               'register':'https://chat.fictional.example.org/#/register','homeserver':'https://matrix.fictional.example.org'}
        html=homepage.render(model,links)
        self.assertIn('https://chat.fictional.example.org/#/login',html)
        self.assertIn('&lt;fictional&gt;',html)
        self.assertNotIn('web.matrix.fictional.example.org',html)
        with self.assertRaises(RuntimeError):homepage.render(model,{'client':'https://chat.fictional.example.org/'})

    def test_adopted_cli_routes_features_federation_site_and_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            root,_=ExistingTests().fixture(Path(folder))
            for words,file in ((['features','status'],'existing_features.py'),
                    (['federation','servers'],'federation.py'),
                    (['homepage','apply','--name','fictional'],'existing_features.py'),
                    (['backup','schedule','status'],'existing_schedule.py')):
                command,options,values=misbot.parse([*words,'--directory',str(root)])
                args=misbot.plan(command,options,values,source=BUNDLE)
                self.assertEqual(args[1],str(root/file))
            command,options,values=misbot.parse(['update','apply','--directory',str(root),'--source',str(BUNDLE)])
            regular=command_wrapper.regular
            def info(path):
                return SimpleNamespace(st_uid=0,st_mode=0o644) if path==BUNDLE/'existing_update.py' else regular(path)
            with patch.object(command_wrapper,'regular',side_effect=info):
                self.assertEqual(misbot.plan(command,options,values,source=BUNDLE)[1],str(BUNDLE/'existing_update.py'))

    @unittest.skipUnless(os.geteuid()==0,'Root-owned lock integration requires an isolated root environment')
    def test_same_root_is_reentrant_but_other_process_cannot_borrow_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root,_=ExistingTests().fixture(Path(folder));(root/'.misbot').chmod(0o700)
            installation=existing.Installation(root)
            script='import sys;sys.path.insert(0,sys.argv[1]);from existing_compose import Installation\nwith Installation(sys.argv[2]).lock():pass'
            argv=[sys.executable,'-B','-c',script,str(BUNDLE),str(root)]
            with installation.lock():
                with existing.Installation(root).lock():pass
                result=subprocess.run(argv,capture_output=True)
                self.assertNotEqual(result.returncode,0)
                result=subprocess.run(argv,capture_output=True,**installation.child_options())
                self.assertEqual(result.returncode,0,result.stderr.decode())
                result=subprocess.run(argv,capture_output=True)
                self.assertNotEqual(result.returncode,0)
            self.assertEqual(subprocess.run(argv,capture_output=True).returncode,0)

    def test_foreign_feature_plan_cannot_modify_accepted_identity_or_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root,model=ExistingTests().fixture(Path(folder));(root/'.misbot').chmod(0o700)
            def directory(path):
                path=Path(path);path.mkdir(mode=0o700,parents=True,exist_ok=True);return path
            with patch.object(existing,'private_directory',side_effect=directory),patch.object(features,'private_directory',side_effect=directory):
                installation=features.Features(root)
            config=root/'.misbot/feature-plan.json'
            value={'format':1,'primary_domain':'fictional.example.org','admin_domain':'admin.fictional.example.org',
                'web_url':'https://chat.fictional.example.org/','caddyfile':'/etc/caddy/Caddyfile',
                'site_directory':'/srv/fictional','image_archive':str(installation.folder/'images.tar'),
                'image_manifest':str(installation.folder/'images.json')}
            config.write_text(json.dumps(value));config.chmod(0o600)
            with patch.object(command_wrapper,'regular',return_value=SimpleNamespace(st_uid=0,st_mode=0o600)):
                with self.assertRaises(existing.Error):installation.plan(config)
                installation.installation.model['external_backup_paths']=['/etc/caddy/Caddyfile']
                self.assertEqual(installation.plan(config)['primary_domain'],'fictional.example.org')
                for changes in ({'site_directory':'/srv/fictional\nrespond 200'},
                                {'admin_domain':'fictional.example.org'}, {'web_url':'https://secret@example.org/'}):
                    config.write_text(json.dumps(value|changes))
                    with self.assertRaises(existing.Error):installation.plan(config)


if __name__=='__main__':unittest.main()
