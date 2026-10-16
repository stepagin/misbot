"""Private templates survive native generation and remain behind the same routes."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import homepage
import state_store


class SiteTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        (self.root / 'config').mkdir(mode=0o700)
        self.model = {'DOMAIN': 'example.org', 'SITE_NAME': 'A < B'}
        self.template = self.root / 'config/site-template'
        self.template.mkdir(mode=0o700)
        state_store.write_text(
            self.template / 'index.html',
            '<meta name="misbot-homepage"><h1>@@NAME@@</h1><a href="@@CLIENT@@">Chat</a>@@NOTE@@',
        )
        state_store.write_text(self.template / 'site.css', 'body { color: #123456; }')

    def test_repeat_render_escapes_values_and_preserves_template(self):
        before = {p.name: p.read_bytes() for p in self.template.iterdir()}
        for _ in range(2):
            homepage.write_assets(self.root, self.model)
            content = (self.root / 'public/site/index.html').read_text()
            self.assertIn('A &lt; B', content)
            self.assertIn('https://web.example.org/', content)
            self.assertIn('код приглашения', content)
            self.assertEqual(
                (self.root / 'public/site/site.css').read_text(), 'body { color: #123456; }'
            )
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.template.iterdir()})

    def test_embedded_svg_image_is_preserved_and_allowed_without_enabling_scripts_or_inline_styles(
        self,
    ):
        image = 'data:image/svg+xml,' + quote(
            '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
            '<rect width="8" height="8" fill="#abcdef"/></svg>'
        )
        source = (
            '<meta name="misbot-homepage"><img alt="Fictional illustration" src="' + image + '">'
        )
        state_store.write_text(self.template / 'index.html', source)
        homepage.write_assets(self.root, self.model)
        self.assertEqual((self.template / 'index.html').read_text(), source)
        self.assertEqual((self.root / 'public/site/index.html').read_text(), source)
        header = next(
            line.strip().split(' ', 2)[2]
            for line in homepage.route(self.model).splitlines()
            if 'header Content-Security-Policy ' in line
        )
        directives = {
            tokens[0]: tokens[1:]
            for part in json.loads(header).split(';')
            if (tokens := part.split())
        }
        self.assertEqual(
            directives,
            {
                'default-src': ["'none'"],
                'style-src': ["'self'"],
                'img-src': ["'self'", 'data:'],
                'base-uri': ["'none'"],
                'form-action': ["'none'"],
                'frame-ancestors': ["'none'"],
            },
        )

    def test_unsafe_or_invalid_template_does_not_replace_public_files(self):
        homepage.write_assets(self.root, self.model)
        public = self.root / 'public/site/index.html'
        before = public.read_bytes()
        path = self.template / 'index.html'
        original = path.read_text()
        for invalid in ('@@UNRECOGNIZED@@', '<h1>Missing readiness marker</h1>'):
            state_store.write_text(path, invalid)
            with self.assertRaises(RuntimeError):
                homepage.write_assets(self.root, self.model)
            self.assertEqual(public.read_bytes(), before)
        state_store.write_text(path, original)
        path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            homepage.write_assets(self.root, self.model)
        path.unlink()
        path.symlink_to(public)
        with self.assertRaises(RuntimeError):
            homepage.write_assets(self.root, self.model)
        path.unlink()
        os.link(public, path)
        with self.assertRaises(RuntimeError):
            homepage.write_assets(self.root, self.model)
        self.assertEqual(public.read_bytes(), before)

    def test_template_only_change_uses_checkpointed_apply_and_rollback(self):
        # A permitted source below 128 KiB expands beyond the old reader cap.
        self.model['SITE_DESCRIPTION'] = '&' * 1000
        state_store.write_text(
            self.template / 'index.html',
            '<meta name="misbot-homepage">' + 'x' * 90000 + '@@DESCRIPTION@@' * 60,
        )
        state_store.write_text(self.template / 'site.css', ' ' * (200 * 1024))
        state_store.write_json(
            self.root / 'installation.json', {'domain': 'example.org', 'preview': True}
        )
        configuration.write_model(self.root, self.model, preview=True)
        state_store.write_text(
            self.root / 'config/Caddyfile', 'example.org {\n' + homepage.route(self.model) + '}\n'
        )
        homepage.write_assets(self.root, self.model)
        backend = homepage.Backend(
            self.root,
            validator=lambda path: None,
            activate=lambda: None,
            verify=lambda model: None,
            attempts=1,
        )
        backend.ready = lambda: None
        backend.initial_launch_incomplete = lambda: False
        original = (self.root / 'public/site/index.html').read_bytes()
        self.assertGreater(len(original), 131072)
        state_store.write_text(
            self.template / 'index.html', '<meta name="misbot-homepage"><h1>New @@NAME@@</h1>'
        )
        # Fail after publication: rollback must restore exact former public bytes.
        activations = []

        def fail_once():
            activations.append(True)
            if len(activations) == 1:
                raise RuntimeError('fictional activation failure')

        backend.activate = fail_once
        with self.assertRaises(RuntimeError):
            backend.apply({})
        self.assertEqual((self.root / 'public/site/index.html').read_bytes(), original)
        self.assertEqual(len(activations), 2)
        self.assertFalse((self.root / 'homepage-transition.json').exists())
        backend.activate = lambda: None
        backend.apply({})
        self.assertIn('New A &lt; B', (self.root / 'public/site/index.html').read_text())
        self.assertEqual(backend.apply({}), {'status': 'unchanged'})

    def test_large_inputs_and_rendered_output_limit(self):
        path = self.template / 'index.html'
        state_store.write_text(path, '<meta name="misbot-homepage">' + 'x' * (200 * 1024))
        state_store.write_text(self.template / 'site.css', ' ' * (200 * 1024))
        homepage.write_assets(self.root, self.model)
        original = (self.root / 'public/site/index.html').read_bytes()
        self.assertGreater(len(original), 131072)
        self.model['SITE_DESCRIPTION'] = '&' * 1000
        state_store.write_text(path, '<meta name="misbot-homepage">' + '@@DESCRIPTION@@' * 1000)
        with self.assertRaises(RuntimeError):
            homepage.write_assets(self.root, self.model)
        self.assertEqual((self.root / 'public/site/index.html').read_bytes(), original)
        # The default private configuration limit is unchanged, including UTF-8 bytes.
        with self.assertRaises(RuntimeError):
            configuration.read_private(self.template / 'site.css')


if __name__ == '__main__':
    unittest.main()
