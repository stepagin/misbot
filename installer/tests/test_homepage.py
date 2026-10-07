"""Fictional site: injection, route preservation and real interrupted checkpoints."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import homepage
import configuration as config
import configuration_schema as schema
import state_store


class HomepageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.root.chmod(0o700)
        state_store.write_json(self.root/'installation.json',{'domain':'fictional.example.org','config_format_version':2})
        config.write_model(self.root,{'DOMAIN':'fictional.example.org'})
        (self.root/'config').mkdir();(self.root/'public').mkdir()
        self.legacy='fictional.example.org {\n    handle /.well-known/misbot { reverse_proxy unix//run/misbot/http.sock }\n    handle /_misbot/v1/* { reverse_proxy unix//run/misbot/http.sock }\n    handle {\n        redir https://web.fictional.example.org{uri} 302\n    }\n}\nmas.fictional.example.org { reverse_proxy mas:8080 }\n'
        state_store.write_text(self.root/'config/Caddyfile',self.legacy)
        self.backend=homepage.Backend(self.root,validator=lambda path:None,activate=lambda:None,verify=lambda model:None,attempts=1)
    def tearDown(self):self.temp.cleanup()
    def test_injection_registration_and_support(self):
        model={'DOMAIN':'fictional.example.org','SITE_NAME':'<script>alert("x")</script>','SITE_DESCRIPTION':'<img onerror=alert(1)>','SITE_SUPPORT_URL':'https://help.example.net/'}
        page=homepage.render(model)
        self.assertNotIn('<script>',page);self.assertNotIn('<img',page)
        self.assertIn('&lt;script&gt;',page);self.assertIn('#/login',page);self.assertIn('#/register',page)
        self.assertIn('<code>fictional.example.org</code>',page)
        self.assertNotIn('https://matrix.fictional.example.org',page)
        page=homepage.render({'DOMAIN':'fictional.example.org','OPEN_REGISTRATION':'false'})
        self.assertNotIn('#/register',page);self.assertNotIn('Связаться с поддержкой',page)
    def test_url_validation_and_cycles(self):
        for url in ('http://example.net','https://user:pass@example.net','https://example.net/{uri}','https://example.net/%0aevil','https://example.net:8443','https://fictional.example.org/','https://web.fictional.example.org/','https://example.net/\n'):
            with self.subTest(url=url),self.assertRaises(schema.InvalidConfiguration):schema.validate({'DOMAIN':'fictional.example.org','HOMEPAGE_MODE':'external','HOMEPAGE_URL':url})
        with self.assertRaises(schema.InvalidConfiguration):schema.validate({'DOMAIN':'fictional.example.org','HOMEPAGE_MODE':'external'})
        with self.assertRaises(schema.InvalidConfiguration):schema.validate({'DOMAIN':'пример.рф','HOMEPAGE_MODE':'external','HOMEPAGE_URL':'https://пример.рф/'})
        with self.assertRaises(schema.InvalidConfiguration):schema.validate({'DOMAIN':'fictional.example.org','PUBLIC_IP':'1.1.1.1','HOMEPAGE_MODE':'external','HOMEPAGE_URL':'https://1.1.1.1/'})
        self.assertTrue(schema.field_value('HOMEPAGE_URL','https://пример.рф/путь').startswith('https://xn--'))
    def test_routes_are_preserved_and_source_queries_not_forwarded(self):
        model=config.load(self.root,True);model.update(HOMEPAGE_MODE='external',HOMEPAGE_URL='https://community.example.net/welcome?language=ru')
        result=homepage.replace_route(self.legacy,model)
        self.assertIn('handle /_misbot/v1/*',result);self.assertIn('mas.fictional.example.org { reverse_proxy mas:8080 }',result)
        self.assertNotIn('{uri}',result);self.assertIn('302',result);self.assertIn('no-store',result)
        self.assertIn('welcome?language=ru#',result)
        self.assertEqual(result,homepage.replace_route(result,model))
    def test_apply_failure_rolls_back_actual_config_and_identity(self):
        calls=[]
        def verify(model):
            calls.append(model['HOMEPAGE_MODE'])
            if model['HOMEPAGE_MODE']=='external':raise RuntimeError('fictional unavailable route')
        self.backend.verify=verify
        before=config.load(self.root,True)
        with patch.object(self.backend,'ready'),state_store.operation_guard(self.root):
            with self.assertRaisesRegex(RuntimeError,'восстановлена'):self.backend.apply({'HOMEPAGE_MODE':'external','HOMEPAGE_URL':'https://community.example.net/'})
        self.assertEqual(config.load(self.root,True),before);self.assertEqual((self.root/'config/Caddyfile').read_text(),self.legacy)
        self.assertFalse((self.root/'homepage-transition.json').exists());self.assertEqual(calls,['external','element'])
    def test_failed_rollback_keeps_checkpoint_guard_and_can_recover(self):
        self.backend.activate=lambda:(_ for _ in ()).throw(RuntimeError('fictional restart failure'))
        with patch.object(self.backend,'ready'),state_store.operation_guard(self.root):
            with self.assertRaisesRegex(RuntimeError,'homepage rollback'):self.backend.apply({'SITE_NAME':'Fictional new name'})
        with self.assertRaisesRegex(RuntimeError,'homepage recover'):state_store.assert_update_allowed(self.root)
        self.backend.activate=lambda:None
        with state_store.operation_guard(self.root):self.backend.recover()
        self.assertEqual(config.load(self.root,True)['SITE_NAME'],'Fictional new name')
        self.assertFalse((self.root/'homepage-transition.json').exists())
    def test_new_config_area_one_loader_private_permissions(self):
        config.write_model(self.root,{'DOMAIN':'fictional.example.org','SITE_NAME':'Team with spaces'})
        self.assertEqual(config.load(self.root,True)['SITE_NAME'],'Team with spaces')
        self.assertEqual((self.root/'settings/site.env').stat().st_mode&0o777,0o600)

    def test_signal_leaves_detectable_checkpoint_and_retry_preserves_identity(self):
        self.backend.activate=lambda:(_ for _ in ()).throw(state_store.OperationInterrupted('fictional disconnect'))
        with patch.object(self.backend,'ready'),state_store.operation_guard(self.root):
            with self.assertRaises(state_store.OperationInterrupted):self.backend.apply({'SITE_NAME':'Fictional resumed'})
        self.assertTrue((self.root/'homepage-transition.json').is_file())
        with self.assertRaisesRegex(RuntimeError,'homepage recover'):state_store.assert_update_allowed(self.root)
        self.backend.activate=lambda:None
        with state_store.operation_guard(self.root):self.backend.recover()
        self.assertEqual(config.read_private_json(self.root/'installation.json')['domain'],'fictional.example.org')
        self.assertEqual(config.load(self.root,True)['SITE_NAME'],'Fictional resumed')

    def test_private_symlink_is_rejected_before_snapshot_or_publication(self):
        secret=self.root/'fictional-private';secret.mkdir(mode=0o700)
        state_store.write_text(secret/'index.html','FICTIONAL_PRIVATE_MARKER')
        (self.root/'public/site').symlink_to(secret,target_is_directory=True)
        with patch.object(self.backend,'ready'),state_store.operation_guard(self.root):
            with self.assertRaisesRegex(RuntimeError,'Небезопасный'):self.backend.apply({'SITE_NAME':'Fictional new name'})
        self.assertFalse((self.root/'homepage-transition.json').exists())
        self.assertEqual((secret/'index.html').read_text(),'FICTIONAL_PRIVATE_MARKER')

    def test_update_failure_restores_site_settings_and_assets(self):
        import update
        config.write_model(self.root,{'DOMAIN':'fictional.example.org','SITE_NAME':'Before update'})
        homepage.write_assets(self.root,config.load(self.root,True))
        original=(self.root/'public/site/index.html').read_bytes()
        saved=update.snapshot(self.root,{},'fictional-site-update')
        calls=[]
        def startup(root,log):
            calls.append(log.name)
            if len(calls)==1:
                config.write_model(root,{'DOMAIN':'fictional.example.org','SITE_NAME':'Failed candidate'})
                homepage.write_assets(root,config.load(root,True))
                return 1
            return 0
        with patch.object(update,'copy_sources',return_value=[]),patch.object(update,'run_up',side_effect=startup),patch.object(update,'refresh_command'):
            with self.assertRaisesRegex(RuntimeError,'rollback healthy=True'):
                update.apply_changes(self.root,saved,{}, {'version':'1.0.0'},'fictional-revision')
        self.assertEqual(config.load(self.root,True)['SITE_NAME'],'Before update')
        self.assertEqual((self.root/'public/site/index.html').read_bytes(),original)
        self.assertEqual(calls,['apply.log','rollback.log'])


if __name__=='__main__':unittest.main()
