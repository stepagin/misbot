"""Fictional site: injection, route preservation and real interrupted checkpoints."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import homepage
import configuration as config
import configuration_schema as schema
import state_store


class HomepageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'fictional.example.org', 'config_format_version': 2},
        )
        config.write_model(self.root, {'DOMAIN': 'fictional.example.org'})
        (self.root / 'config').mkdir()
        (self.root / 'public').mkdir()
        self.legacy = 'fictional.example.org {\n    handle /.well-known/misbot { reverse_proxy unix//run/misbot/http.sock }\n    handle /_misbot/v1/* { reverse_proxy unix//run/misbot/http.sock }\n    handle {\n        redir https://web.fictional.example.org{uri} 302\n    }\n}\nmas.fictional.example.org { reverse_proxy mas:8080 }\n'
        state_store.write_text(self.root / 'config/Caddyfile', self.legacy)
        self.backend = homepage.Backend(
            self.root,
            validator=lambda path: None,
            activate=lambda: None,
            verify=lambda model: None,
            attempts=1,
        )

    def tearDown(self):
        self.temp.cleanup()

    def http_fixture(self, api_response=(503, {'code': 'api_disabled'}), matrix_response=None):
        from io import BytesIO
        from unittest.mock import Mock
        from urllib.error import HTTPError

        def get(url, **kwargs):
            if '/.well-known/misbot' in url:
                code, body = api_response
            elif '/.well-known/matrix/client' in url:
                code, body = matrix_response or (
                    200,
                    {'m.homeserver': {'base_url': 'https://matrix.fictional.example.org'}},
                )
            elif '/.well-known/matrix/server' in url:
                code, body = 200, {'m.server': 'matrix.fictional.example.org:443'}
            elif '/_matrix/client/versions' in url:
                code, body = 200, {'versions': ['v1.1']}
            else:
                mode = config.load(self.root, True)['HOMEPAGE_MODE']
                response = Mock()
                response.status = 200 if mode == 'landing' else 302
                response.headers = {'Location': 'https://web.fictional.example.org'}
                response.read.return_value = b'<meta name="misbot-homepage">'
                context = Mock()
                context.__enter__ = Mock(return_value=response)
                context.__exit__ = Mock(return_value=False)
                return context
            payload = json.dumps(body).encode()
            if code != 200:
                raise HTTPError(url, code, 'fixture', {}, BytesIO(payload))
            response = Mock(status=code, headers={})
            response.read.return_value = payload
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            return context

        return Mock(open=Mock(side_effect=get))

    def test_disabled_api_apply_recover_and_rollback_use_saved_setting(self):
        (self.root / 'data/federation').mkdir(parents=True)
        state_store.write_json(
            self.root / 'data/federation/state.json',
            {'format': 1, 'server_name': 'fictional.example.org', 'api_enabled': False},
        )
        self.backend.verify = self.backend.probe
        with (
            patch.object(homepage.urllib.request, 'build_opener', return_value=self.http_fixture()),
            patch.object(self.backend, 'ready'),
            state_store.operation_guard(self.root),
        ):
            self.backend.apply({'HOMEPAGE_MODE': 'landing'})
            self.assertFalse((self.root / 'homepage-transition.json').exists())
            for rollback in (False, True):
                previous_name = config.load(self.root, True)['SITE_NAME']
                self.backend.activate = lambda: (_ for _ in ()).throw(
                    state_store.OperationInterrupted()
                )
                with self.assertRaises(state_store.OperationInterrupted):
                    self.backend.apply({'SITE_NAME': 'Interrupted name ' + str(rollback)})
                self.backend.activate = lambda: None
                self.backend.recover(rollback=rollback)
                self.assertEqual(
                    config.load(self.root, True)['SITE_NAME'],
                    previous_name if rollback else 'Interrupted name False',
                )
                self.assertFalse((self.root / 'homepage-transition.json').exists())
        state_store.assert_update_allowed(self.root)

    def test_disabled_api_does_not_accept_wrong_error_or_broken_matrix_discovery(self):
        model = config.load(self.root, True)
        for enabled, response, matrix in (
            (False, (503, {'code': 'installation_busy_or_pending'}), None),
            (False, (200, {'server_name': model['DOMAIN'], 'protocol': 1}), None),
            (True, (503, {'code': 'api_disabled'}), None),
            (False, (503, {'code': 'api_disabled'}), (503, {'code': 'api_disabled'})),
            (
                False,
                (503, {'code': 'api_disabled'}),
                (200, {'m.homeserver': {'base_url': 'https://wrong.example'}}),
            ),
        ):
            with (
                self.subTest(enabled=enabled, response=response, matrix=matrix),
                patch.object(
                    homepage.urllib.request,
                    'build_opener',
                    return_value=self.http_fixture(response, matrix),
                ),
                self.assertRaises(RuntimeError),
            ):
                homepage.probe(model, api_enabled=enabled)

    def test_setup_link_follows_release_metadata_and_escapes_query(self):
        source = self.root / 'source'
        (source / 'site').mkdir(parents=True)
        (source / 'site/index.html').write_bytes(
            (Path(homepage.__file__).parent / 'site/index.html').read_bytes()
        )
        model = {'DOMAIN': 'fictional.example.org', 'REGISTRATION_TOKEN_REQUIRED': 'true'}
        for url in (
            'https://github.com/stepagin/misbot',
            'https://code.example.org/project?from=site&version=next',
        ):
            with self.subTest(url=url):
                (source / 'release.json').write_text(json.dumps({'homepage_setup_url': url}))
                page = homepage.render(model, source=source)
                self.assertIn('href="' + url.replace('&', '&amp;') + '"', page)
                self.assertIn('Подключить свой сервер', page)
                self.assertIn('код приглашения', page)
                self.assertIn('https://web.fictional.example.org/#/login', page)
                self.assertIn('https://web.fictional.example.org/#/register', page)
                self.assertNotIn('@@SETUPURL@@', page)

    def test_setup_link_rejects_empty_unsafe_or_non_https_release_url(self):
        for url in (
            '',
            'http://code.example.org/',
            'javascript:alert(1)',
            'https://user:password@code.example.org/',
        ):
            with self.subTest(url=url):
                state_store.write_json(self.root / 'release.json', {'homepage_setup_url': url})
                with self.assertRaises(RuntimeError):
                    homepage.render(
                        {'DOMAIN': 'fictional.example.org'},
                        template='<a href="@@SETUPURL@@">Setup</a>',
                        source=self.root,
                    )

    def test_old_custom_template_needs_no_new_release_metadata(self):
        page = homepage.render(
            {'DOMAIN': 'fictional.example.org'}, template='<h1>@@NAME@@</h1>', source=self.root
        )
        self.assertEqual(page, '<h1>fictional.example.org</h1>')

    def test_injection_registration_and_support(self):
        model = {
            'DOMAIN': 'fictional.example.org',
            'SITE_NAME': '<script>alert("x")</script>',
            'SITE_DESCRIPTION': '<img onerror=alert(1)>',
            'SITE_SUPPORT_URL': 'https://help.example.net/',
        }
        page = homepage.render(model)
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('#/login', page)
        self.assertIn('#/register', page)
        self.assertIn('<code>fictional.example.org</code>', page)
        self.assertNotIn('https://matrix.fictional.example.org', page)
        page = homepage.render({'DOMAIN': 'fictional.example.org', 'OPEN_REGISTRATION': 'false'})
        self.assertNotIn('#/register', page)
        self.assertNotIn('Связаться с поддержкой', page)

    def test_url_validation_and_cycles(self):
        for url in (
            'http://example.net',
            'https://user:pass@example.net',
            'https://example.net/{uri}',
            'https://example.net/%0aevil',
            'https://example.net:8443',
            'https://fictional.example.org/',
            'https://web.fictional.example.org/',
            'https://example.net/\n',
        ):
            with self.subTest(url=url), self.assertRaises(schema.InvalidConfiguration):
                schema.validate(
                    {
                        'DOMAIN': 'fictional.example.org',
                        'HOMEPAGE_MODE': 'external',
                        'HOMEPAGE_URL': url,
                    }
                )
        with self.assertRaises(schema.InvalidConfiguration):
            schema.validate({'DOMAIN': 'fictional.example.org', 'HOMEPAGE_MODE': 'external'})
        with self.assertRaises(schema.InvalidConfiguration):
            schema.validate(
                {
                    'DOMAIN': 'пример.рф',
                    'HOMEPAGE_MODE': 'external',
                    'HOMEPAGE_URL': 'https://пример.рф/',
                }
            )
        with self.assertRaises(schema.InvalidConfiguration):
            schema.validate(
                {
                    'DOMAIN': 'fictional.example.org',
                    'PUBLIC_IP': '1.1.1.1',
                    'HOMEPAGE_MODE': 'external',
                    'HOMEPAGE_URL': 'https://1.1.1.1/',
                }
            )
        self.assertTrue(
            schema.field_value('HOMEPAGE_URL', 'https://пример.рф/путь').startswith('https://xn--')
        )

    def test_routes_are_preserved_and_source_queries_not_forwarded(self):
        model = config.load(self.root, True)
        model.update(
            HOMEPAGE_MODE='external',
            HOMEPAGE_URL='https://community.example.net/welcome?language=ru',
        )
        result = homepage.replace_route(self.legacy, model)
        self.assertIn('handle /_misbot/v1/*', result)
        self.assertIn('mas.fictional.example.org { reverse_proxy mas:8080 }', result)
        self.assertNotIn('{uri}', result)
        self.assertIn('302', result)
        self.assertIn('no-store', result)
        self.assertIn('welcome?language=ru#', result)
        self.assertEqual(result, homepage.replace_route(result, model))

    def test_apply_failure_rolls_back_actual_config_and_identity(self):
        calls = []

        def verify(model):
            calls.append(model['HOMEPAGE_MODE'])
            if model['HOMEPAGE_MODE'] == 'external':
                raise RuntimeError('fictional unavailable route')

        self.backend.verify = verify
        before = config.load(self.root, True)
        with patch.object(self.backend, 'ready'), state_store.operation_guard(self.root):
            with self.assertRaisesRegex(RuntimeError, 'восстановлена'):
                self.backend.apply(
                    {'HOMEPAGE_MODE': 'external', 'HOMEPAGE_URL': 'https://community.example.net/'}
                )
        self.assertEqual(config.load(self.root, True), before)
        self.assertEqual((self.root / 'config/Caddyfile').read_text(), self.legacy)
        self.assertFalse((self.root / 'homepage-transition.json').exists())
        self.assertEqual(calls, ['external', 'element'])

    def test_failed_rollback_keeps_checkpoint_guard_and_can_recover(self):
        self.backend.activate = lambda: (_ for _ in ()).throw(
            RuntimeError('fictional restart failure')
        )
        with patch.object(self.backend, 'ready'), state_store.operation_guard(self.root):
            with self.assertRaisesRegex(RuntimeError, 'homepage rollback'):
                self.backend.apply({'SITE_NAME': 'Fictional new name'})
        with self.assertRaisesRegex(RuntimeError, 'homepage recover'):
            state_store.assert_update_allowed(self.root)
        self.backend.activate = lambda: None
        with state_store.operation_guard(self.root):
            self.backend.recover()
        self.assertEqual(config.load(self.root, True)['SITE_NAME'], 'Fictional new name')
        self.assertFalse((self.root / 'homepage-transition.json').exists())

    def test_new_config_area_one_loader_private_permissions(self):
        config.write_model(
            self.root, {'DOMAIN': 'fictional.example.org', 'SITE_NAME': 'Team with spaces'}
        )
        self.assertEqual(config.load(self.root, True)['SITE_NAME'], 'Team with spaces')
        self.assertEqual((self.root / 'settings/site.env').stat().st_mode & 0o777, 0o600)

    def test_signal_leaves_detectable_checkpoint_and_retry_preserves_identity(self):
        self.backend.activate = lambda: (_ for _ in ()).throw(
            state_store.OperationInterrupted('fictional disconnect')
        )
        with patch.object(self.backend, 'ready'), state_store.operation_guard(self.root):
            with self.assertRaises(state_store.OperationInterrupted):
                self.backend.apply({'SITE_NAME': 'Fictional resumed'})
        self.assertTrue((self.root / 'homepage-transition.json').is_file())
        with self.assertRaisesRegex(RuntimeError, 'homepage recover'):
            state_store.assert_update_allowed(self.root)
        self.backend.activate = lambda: None
        with state_store.operation_guard(self.root):
            self.backend.recover()
        self.assertEqual(
            config.read_private_json(self.root / 'installation.json')['domain'],
            'fictional.example.org',
        )
        self.assertEqual(config.load(self.root, True)['SITE_NAME'], 'Fictional resumed')

    def test_private_symlink_is_rejected_before_snapshot_or_publication(self):
        secret = self.root / 'fictional-private'
        secret.mkdir(mode=0o700)
        state_store.write_text(secret / 'index.html', 'FICTIONAL_PRIVATE_MARKER')
        (self.root / 'public/site').symlink_to(secret, target_is_directory=True)
        with patch.object(self.backend, 'ready'), state_store.operation_guard(self.root):
            with self.assertRaisesRegex(RuntimeError, 'Небезопасный'):
                self.backend.apply({'SITE_NAME': 'Fictional new name'})
        self.assertFalse((self.root / 'homepage-transition.json').exists())
        self.assertEqual((secret / 'index.html').read_text(), 'FICTIONAL_PRIVATE_MARKER')

    def test_partial_bootstrap_site_rollback_restores_files_without_starting_services(self):
        import lifecycle

        before = config.load(self.root, True)
        self.backend.activate = lambda: (_ for _ in ()).throw(state_store.OperationInterrupted())
        with patch.object(self.backend, 'ready'), state_store.operation_guard(self.root):
            with self.assertRaises(state_store.OperationInterrupted):
                self.backend.apply({'SITE_NAME': 'Pending candidate'})
        state_store.write_json(
            self.root / 'installation.json',
            {
                'domain': 'fictional.example.org',
                'config_format_version': 2,
                'databases_initialized': True,
                'mas_issuer': 'https://mas.fictional.example.org/',
                'federation_seeded': True,
            },
        )
        with (
            patch.object(
                lifecycle, 'service_rows', return_value=[{'Service': 'synapse', 'State': 'running'}]
            ),
            patch.object(self.backend, 'activate') as activate,
            patch.object(self.backend, 'verify') as verify,
            state_store.operation_guard(self.root),
        ):
            with self.assertRaisesRegex(RuntimeError, 'Вернуть прежний сайт'):
                self.backend.recover()
            self.assertTrue((self.root / 'homepage-transition.json').exists())
            result = self.backend.recover(rollback=True)
            self.assertEqual(result['status'], 'rolled_back')
            self.assertEqual(result['public_checks'], 'deferred')
            self.assertEqual(self.backend.recover(rollback=True)['status'], 'unchanged')
            activate.assert_not_called()
            verify.assert_not_called()
        self.assertFalse((self.root / 'homepage-transition.json').exists())
        self.assertEqual(config.load(self.root, True), before)
        self.assertEqual((self.root / 'config/Caddyfile').read_text(), self.legacy)
        state_store.assert_update_allowed(self.root)

    def test_initial_bootstrap_gate_never_applies_to_existing_or_previously_started_proxy(self):
        import lifecycle

        base = {
            'domain': 'fictional.example.org',
            'databases_initialized': True,
            'federation_seeded': True,
        }
        for changes, rows in (
            ({'backend': 'existing-compose'}, []),
            ({'applied_release_version': '1.1.0'}, []),
            ({'welcome_crypto_initialized': True}, []),
            ({}, [{'Service': 'tls-router', 'State': 'exited'}]),
        ):
            with self.subTest(changes=changes, rows=rows):
                state_store.write_json(self.root / 'installation.json', {**base, **changes})
                with patch.object(lifecycle, 'service_rows', return_value=rows):
                    self.assertFalse(self.backend.initial_launch_incomplete())

    def test_site_changes_are_rejected_before_checkpoint_during_partial_installation(self):
        import lifecycle

        state_store.write_json(
            self.root / 'installation.json',
            {
                'domain': 'fictional.example.org',
                'databases_initialized': True,
                'mas_issuer': 'https://mas.fictional.example.org/',
                'federation_seeded': True,
            },
        )
        with (
            patch.object(lifecycle, 'configure'),
            patch.object(lifecycle, 'ownership_check'),
            patch.object(lifecycle, 'service_rows', return_value=[]),
            state_store.operation_guard(self.root),
        ):
            self.backend.ready()  # Read-only validation remains available before launch.
            with self.assertRaisesRegex(RuntimeError, 'Первоначальный запуск'):
                self.backend.apply({'SITE_NAME': 'Must not publish'})
        self.assertFalse((self.root / 'homepage-transition.json').exists())
        self.assertEqual(config.load(self.root, True)['SITE_NAME'], '')
        self.assertEqual((self.root / 'config/Caddyfile').read_text(), self.legacy)

    def test_update_failure_restores_site_settings_and_assets(self):
        import update

        config.write_model(
            self.root, {'DOMAIN': 'fictional.example.org', 'SITE_NAME': 'Before update'}
        )
        homepage.write_assets(self.root, config.load(self.root, True))
        original = (self.root / 'public/site/index.html').read_bytes()
        saved = update.snapshot(self.root, {}, 'fictional-site-update')
        calls = []

        def startup(root, log):
            calls.append(log.name)
            if len(calls) == 1:
                config.write_model(
                    root, {'DOMAIN': 'fictional.example.org', 'SITE_NAME': 'Failed candidate'}
                )
                homepage.write_assets(root, config.load(root, True))
                return 1
            return 0

        with (
            patch.object(update, 'copy_sources', return_value=[]),
            patch.object(update, 'run_up', side_effect=startup),
            patch.object(update, 'refresh_command'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'rollback healthy=True'):
                update.apply_changes(
                    self.root, saved, {}, {'version': '1.0.0'}, 'fictional-revision'
                )
        self.assertEqual(config.load(self.root, True)['SITE_NAME'], 'Before update')
        self.assertEqual((self.root / 'public/site/index.html').read_bytes(), original)
        self.assertEqual(calls, ['apply.log', 'rollback.log'])


if __name__ == '__main__':
    unittest.main()
