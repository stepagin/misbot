"""Public-only adopted updates reuse a verified copy and recover failed writes."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import existing_update as update
import existing_features
import homepage
from existing_compose import digest


class ExistingUpdateTests(unittest.TestCase):
    def homepage_fixture(self, base):
        root = base / 'installation'
        root.mkdir()
        control = root / '.misbot'
        control.mkdir()
        features = control / 'features'
        features.mkdir()
        site = root / 'public/site'
        site.mkdir(parents=True)
        source = base / 'source'
        (source / 'site').mkdir(parents=True)
        backup = base / 'backups/accepted'
        backup.mkdir(parents=True)
        images = {'fixture': {'id': 'sha256:' + 'a' * 64}}
        (backup / 'manifest.json').write_text(
            json.dumps(
                {
                    'server_name': 'fictional.example.org',
                    'images': images,
                    'encrypted_archive_sha256': 'b' * 64,
                }
            )
        )
        (control / 'last-backup.json').write_text(
            json.dumps({'directory': str(backup), 'sha256': 'b' * 64, 'remote': {'verified': True}})
        )
        candidate = {
            'family': update.update.FAMILY,
            'version': '1.2.0',
            'update_scope': 'source-configuration-and-approved-components',
            'homepage_setup_url': 'https://code.example.org/next?from=landing&release=next',
        }
        (source / 'release.json').write_text(json.dumps(candidate))
        (root / 'release.json').write_text(
            json.dumps(dict(candidate, homepage_setup_url='https://code.example.org/old'))
        )
        entries = {'release.json': source / 'release.json'}
        for name in ('index.html', 'site.css'):
            (source / 'site' / name).write_bytes(
                (Path(homepage.__file__).parent / 'site' / name).read_bytes()
            )
            (site / name).write_text('previous ' + name)
            entries['site/' + name] = source / 'site' / name
        (root / 'SHA256SUMS').write_text('previous manifest\n')
        (source / 'SHA256SUMS').write_text('candidate manifest\n')
        plan = {
            'enabled': True,
            'site_directory': str(site),
            'web_url': 'https://chat.fictional.example.org/',
            'primary_domain': 'fictional.example.org',
            'admin_domain': 'admin.fictional.example.org',
        }
        installation = SimpleNamespace(
            root=root,
            control=control,
            state={'domain': 'fictional.example.org'},
            model={
                'backup_directory': str(base / 'backups'),
                'images': images,
                'features': plan,
                'identity': {'mas_config': 'mas.yaml'},
            },
            require_no_pending=Mock(),
            validate=Mock(),
            backup=Mock(),
            verify=Mock(),
        )
        (root / 'mas.yaml').write_text('account:\n  registration_token_required: true\n')
        model = homepage.schema.validate(
            {'DOMAIN': 'fictional.example.org', 'SITE_NAME': 'Our server'}
        )
        (features / 'site.json').write_text(json.dumps({key: model[key] for key in homepage.KEYS}))
        page = object.__new__(existing_features.Homepage)
        page.installation = installation
        page.folder = features
        page.plan = plan
        return installation, source, entries, page

    def test_downgrade_and_schema_regression_refused_before_backup_or_publication(self):
        for candidate in (
            {'version': '1.1.0'},
            {'version': '1.3.0'},
            {'version': '1.3.0', 'data_schema_version': 0},
        ):
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as temporary:
                installation, source, entries, _ = self.homepage_fixture(Path(temporary))
                current = json.loads((installation.root / 'release.json').read_text())
                current['data_schema_version'] = 1
                (installation.root / 'release.json').write_text(json.dumps(current))
                proposed = json.loads((source / 'release.json').read_text())
                proposed.update(candidate)
                (source / 'release.json').write_text(json.dumps(proposed))
                before = {
                    p.relative_to(installation.root): p.read_bytes()
                    for p in installation.root.rglob('*')
                    if p.is_file()
                }
                with (
                    patch.object(update, 'sources', return_value=(entries, 'candidate')),
                    patch.object(update.update, 'run_data_migrations') as migrate,
                    patch.object(update.telegram_container, 'prepare_update') as prepare,
                    self.assertRaisesRegex(RuntimeError, '[Dd]owngrade'),
                ):
                    update.apply(installation, source)
                installation.backup.assert_not_called()
                installation.validate.assert_not_called()
                migrate.assert_not_called()
                prepare.assert_not_called()
                self.assertEqual(
                    before,
                    {
                        p.relative_to(installation.root): p.read_bytes()
                        for p in installation.root.rglob('*')
                        if p.is_file()
                    },
                )

    def test_source_update_refreshes_default_landing_and_recovers_partial_publication(self):
        for failures in (0, 1, 2):
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as temporary:
                installation, source, entries, page = self.homepage_fixture(Path(temporary))
                site = Path(page.plan['site_directory'])
                saved_settings = (page.folder / 'site.json').read_bytes()
                saved_policy = (installation.root / 'mas.yaml').read_bytes()
                atomic = update.command_wrapper.atomic
                regular = update.command_wrapper.regular
                remaining = failures

                def owned(path):
                    info = regular(path)
                    return SimpleNamespace(st_uid=0, st_mode=info.st_mode)

                def publish(path, data, mode):
                    nonlocal remaining
                    if remaining and path == site / 'site.css':
                        remaining -= 1
                        raise RuntimeError('fictional interrupted stylesheet publication')
                    return atomic(path, data, mode)

                def private(path):
                    path.mkdir(mode=0o700, parents=True, exist_ok=True)
                    return path

                with (
                    patch.object(update, 'sources', return_value=(entries, 'candidate')),
                    patch.object(update.update, 'validate_manifest', return_value=({}, 'revision')),
                    patch.object(update.command_wrapper, 'regular', side_effect=owned),
                    patch.object(update.command_wrapper, 'install'),
                    patch.object(update, 'private_directory', side_effect=private),
                    patch.object(existing_features, 'Homepage', return_value=page),
                    patch.object(update.command_wrapper, 'atomic', side_effect=publish),
                ):
                    # Temporary fixtures run as a normal user as well as root.
                    stat_path = Path.stat

                    def root_site(path, *args, **kwargs):
                        info = stat_path(path, *args, **kwargs)
                        if path == site:
                            return os.stat_result((*info[:4], 0, *info[5:]))
                        return info

                    with patch.object(Path, 'stat', root_site):
                        if failures:
                            with self.assertRaisesRegex(RuntimeError, 'fictional interrupted'):
                                update.apply(installation, source)
                        else:
                            update.apply(installation, source)
                if failures:
                    if failures == 2:
                        # Recovery can itself be interrupted; the journal must remain.
                        self.assertTrue((installation.control / 'source-update.json').exists())
                        with (
                            patch.object(update.command_wrapper, 'regular', side_effect=owned),
                            patch.object(update.command_wrapper, 'install'),
                            patch.object(Path, 'stat', root_site),
                        ):
                            update.restore(installation)
                    self.assertEqual((site / 'index.html').read_text(), 'previous index.html')
                    self.assertEqual((site / 'site.css').read_text(), 'previous site.css')
                    self.assertIn(
                        'https://code.example.org/old',
                        (installation.root / 'release.json').read_text(),
                    )
                else:
                    markup = (site / 'index.html').read_text()
                    self.assertIn(
                        'https://code.example.org/next?from=landing&amp;release=next', markup
                    )
                    self.assertIn('Our server', markup)
                    self.assertIn('код приглашения', markup)
                    self.assertIn('https://chat.fictional.example.org/#/login', markup)
                    self.assertEqual(
                        (site / 'site.css').read_bytes(), (source / 'site/site.css').read_bytes()
                    )
                self.assertEqual((page.folder / 'site.json').read_bytes(), saved_settings)
                self.assertEqual((installation.root / 'mas.yaml').read_bytes(), saved_policy)
                self.assertFalse((installation.control / 'source-update.json').exists())
                installation.backup.assert_not_called()

    def test_source_update_keeps_custom_theme_and_non_landing_modes(self):
        for mode in ('theme', 'element', 'external'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                installation, source, entries, page = self.homepage_fixture(Path(temporary))
                if mode == 'theme':
                    theme = page.folder / 'site-template'
                    theme.mkdir()
                    (theme / 'index.html').write_text('individual theme')
                values = json.loads((page.folder / 'site.json').read_text())
                if mode != 'theme':
                    values['HOMEPAGE_MODE'] = mode
                    (page.folder / 'site.json').write_text(json.dumps(values))
                settings = (page.folder / 'site.json').read_bytes()

                def private(path):
                    path.mkdir(mode=0o700, parents=True, exist_ok=True)
                    return path

                with (
                    patch.object(update, 'sources', return_value=(entries, 'candidate')),
                    patch.object(update.update, 'validate_manifest', return_value=({}, 'revision')),
                    patch.object(update, 'private_directory', side_effect=private),
                    patch.object(
                        update.command_wrapper,
                        'regular',
                        return_value=SimpleNamespace(st_uid=0, st_mode=0o644),
                    ),
                    patch.object(update.command_wrapper, 'install'),
                ):
                    update.apply(installation, source)
                self.assertEqual(
                    (installation.root / 'release.json').read_bytes(),
                    (source / 'release.json').read_bytes(),
                )
                self.assertEqual((page.folder / 'site.json').read_bytes(), settings)
                site = Path(page.plan['site_directory'])
                self.assertEqual((site / 'index.html').read_text(), 'previous index.html')
                self.assertEqual((site / 'site.css').read_text(), 'previous site.css')
                if mode == 'theme':
                    self.assertEqual((theme / 'index.html').read_text(), 'individual theme')

    def test_public_source_license_is_accepted_without_allowing_private_paths(self):
        for license_path, accepted in (
            ('LICENSE', True),
            ('LICENSE.env', False),
            ('secrets/LICENSE', False),
        ):
            with self.subTest(path=license_path), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary).resolve() / 'source'
                source.mkdir(mode=0o700)
                names = update.update.ESSENTIAL | {
                    'existing_compose.py',
                    'existing_s3.py',
                    'existing_update.py',
                    license_path,
                }
                for name in names:
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('# fictional public source\n')
                (source / 'release.json').write_text(
                    json.dumps(
                        {
                            'family': update.update.FAMILY,
                            'version': '1.0.0',
                            'data_schema_version': 1,
                            'update_scope': 'source-configuration-and-approved-components',
                        }
                    )
                )
                (source / 'SHA256SUMS').write_text(
                    ''.join(digest(source / name) + '  ' + name + '\n' for name in sorted(names))
                )
                original_stat = Path.stat
                source_info = source.stat()
                root_owned_info = os.stat_result((*source_info[:4], 0, *source_info[5:]))

                def source_stat(path, *args, **kwargs):
                    if path == source:
                        return root_owned_info
                    return original_stat(path, *args, **kwargs)

                with (
                    patch.object(Path, 'stat', source_stat),
                    patch.object(
                        update.command_wrapper,
                        'regular',
                        return_value=SimpleNamespace(st_uid=0, st_mode=0o600),
                    ),
                ):
                    if accepted:
                        entries, revision = update.sources(source)
                        self.assertIn('LICENSE', entries)
                        self.assertEqual(revision, digest(source / 'SHA256SUMS'))
                    else:
                        with self.assertRaisesRegex(update.Error, 'приватный путь'):
                            update.sources(source)

    def test_current_sources_still_apply_declared_data_migrations_after_verified_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'installation'
            root.mkdir()
            control = root / '.misbot'
            control.mkdir()
            source = base / 'source'
            source.mkdir()
            candidate = {
                'family': update.update.FAMILY,
                'version': '1.1.0',
                'data_schema_version': 1,
                'update_scope': 'source-configuration-and-approved-components',
            }
            (source / 'release.json').write_text(json.dumps(candidate))
            (root / 'SHA256SUMS').write_text('unchanged manifest\n')
            (source / 'SHA256SUMS').write_text('unchanged manifest\n')
            backup = base / 'backups' / 'accepted'
            backup.mkdir(parents=True)
            images = {'fixture': {'id': 'sha256:' + 'a' * 64}}
            (backup / 'manifest.json').write_text(
                json.dumps(
                    {
                        'server_name': 'fictional.example.org',
                        'images': images,
                        'encrypted_archive_sha256': 'b' * 64,
                    }
                )
            )
            (control / 'last-backup.json').write_text(
                json.dumps(
                    {'directory': str(backup), 'sha256': 'b' * 64, 'remote': {'verified': True}}
                )
            )
            installation = SimpleNamespace(
                root=root,
                control=control,
                state={'domain': 'fictional.example.org'},
                model={'backup_directory': str(base / 'backups'), 'images': images},
                require_no_pending=Mock(),
                validate=Mock(),
                backup=Mock(),
                verify=Mock(),
            )

            def private(path):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                return path

            with (
                patch.object(update, 'sources', return_value=({}, 'same-revision')),
                patch.object(
                    update.update, 'validate_manifest', return_value=({}, 'same-revision')
                ),
                patch.object(update.update, 'run_data_migrations') as migrate,
                patch.object(update, 'private_directory', side_effect=private),
                patch.object(update.command_wrapper, 'install'),
            ):
                update.apply(installation, source)
            self.assertEqual([call.args[3] for call in migrate.call_args_list], ['check', 'apply'])
            self.assertTrue(migrate.call_args.args[4].is_relative_to(control / 'source-updates'))
            installation.backup.assert_not_called()

    def test_failed_public_update_restores_previous_files_without_new_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'installation'
            root.mkdir()
            control = root / '.misbot'
            control.mkdir()
            source = base / 'source'
            source.mkdir()
            (source / 'release.json').write_text(
                json.dumps(
                    {
                        'family': update.update.FAMILY,
                        'version': '1.1.0',
                        'update_scope': 'source-configuration-and-approved-components',
                    }
                )
            )
            backup = base / 'backups' / 'accepted'
            backup.mkdir(parents=True)
            images = {'fixture': {'id': 'sha256:' + 'a' * 64}}
            manifest = {
                'server_name': 'fictional.example.org',
                'images': images,
                'encrypted_archive_sha256': 'b' * 64,
            }
            (backup / 'manifest.json').write_text(json.dumps(manifest))
            (control / 'last-backup.json').write_text(
                json.dumps(
                    {'directory': str(backup), 'sha256': 'b' * 64, 'remote': {'verified': True}}
                )
            )
            (root / 'existing_compose.py').write_bytes(b'previous public source\n')
            (root / 'SHA256SUMS').write_bytes(b'previous manifest\n')
            entries = {}
            for name in ('existing_compose.py', 'new_public.py', 'SHA256SUMS'):
                path = source / name
                path.write_bytes(b'candidate public source\n')
                entries[name] = path
            installation = SimpleNamespace(
                root=root,
                control=control,
                state={'domain': 'fictional.example.org'},
                model={'backup_directory': str(base / 'backups'), 'images': images},
                require_no_pending=Mock(),
                validate=Mock(),
                backup=Mock(),
                verify=Mock(),
            )
            validation_calls = []

            def validate_manifest(path):
                validation_calls.append(path)
                if len(validation_calls) == 2:
                    raise RuntimeError('fictional failed validation')
                return {}, 'fictional-revision'

            def private(path):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                return path

            with (
                patch.object(update, 'sources', return_value=(entries, 'candidate')),
                patch.object(update.update, 'validate_manifest', side_effect=validate_manifest),
                patch.object(update, 'private_directory', side_effect=private),
                patch.object(
                    update.command_wrapper,
                    'regular',
                    return_value=SimpleNamespace(st_uid=0, st_mode=0o644),
                ),
                patch.object(update.command_wrapper, 'install'),
            ):
                with self.assertRaisesRegex(RuntimeError, 'fictional failed'):
                    update.apply(installation, source)
            self.assertEqual(
                (root / 'existing_compose.py').read_bytes(), b'previous public source\n'
            )
            self.assertEqual((root / 'SHA256SUMS').read_bytes(), b'previous manifest\n')
            self.assertFalse((root / 'new_public.py').exists())
            self.assertFalse((control / 'source-update.json').exists())
            installation.backup.assert_not_called()
            installation.verify.assert_not_called()

    def test_recovery_preserves_a_changed_new_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / '.misbot'
            control.mkdir()
            stage = control / 'source-updates' / 'fixture'
            stage.mkdir(parents=True)
            path = root / 'new_public.py'
            path.write_bytes(b'changed after failure')
            (stage / 'files.json').write_text('{}')
            (control / 'source-update.json').write_text(
                json.dumps({'checkpoint': str(stage), 'files': {'new_public.py': 'a' * 64}})
            )
            installation = SimpleNamespace(root=root, control=control)
            with self.assertRaisesRegex(update.Error, 'изменён'):
                update.restore(installation)
            self.assertEqual(path.read_bytes(), b'changed after failure')
            self.assertTrue((control / 'source-update.json').exists())


if __name__ == '__main__':
    unittest.main()
