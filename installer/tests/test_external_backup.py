import hashlib
import io
import json
import os
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
import ssl
import urllib.error
from unittest.mock import MagicMock, Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_crypto
import external_backup


class ExternalBackupTests(unittest.TestCase):
    values = {
        'S3_BUCKET': 'example-backups',
        'S3_PREFIX': 'backups/',
        'S3_REGION': 'example',
        'S3_ENDPOINT_URL': 'https://s3.example.invalid',
    }

    def test_export_preserves_each_primary_failure_when_delete_is_denied(self):
        class ProviderError(Exception):
            response = {
                'Error': {'Code': 'AccessDenied', 'Message': 'EXAMPLE_PRIVATE_SIGNED_REQUEST'}
            }

        for stage in ('upload', 'privacy', 'fetch'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                parent = Path(folder)
                root = parent / 'stack'
                root.mkdir()
                key = parent / 'key'
                backup_crypto.create_key(key)
                archive = root / 'snapshot.tar.gz'
                archive.write_bytes(b'fictional frozen snapshot')
                before = key.read_bytes()
                service = Mock()
                primary = (
                    RuntimeError('Anonymous access refused')
                    if stage == 'privacy'
                    else ProviderError()
                )
                service.delete_object.side_effect = ProviderError()
                if stage == 'upload':
                    service.upload_file.side_effect = primary
                transfer = SimpleNamespace(TransferConfig=lambda **kwargs: kwargs)
                with (
                    patch.dict(sys.modules, {'boto3.s3.transfer': transfer}),
                    patch.object(
                        external_backup.lifecycle, 'state', return_value={'media_mode': 'local'}
                    ),
                    patch.object(
                        external_backup.lifecycle, 'paused_writers', return_value=nullcontext()
                    ),
                    patch.object(external_backup.lifecycle, 'backup', return_value=archive),
                    patch.object(external_backup.lifecycle, 'emit') as emit,
                    patch.object(external_backup.media_transfer, 'client', return_value=service),
                    patch.object(
                        external_backup,
                        'private_remote',
                        side_effect=primary if stage == 'privacy' else None,
                    ),
                    patch.object(
                        external_backup, 'fetch', side_effect=primary if stage == 'fetch' else None
                    ),
                ):
                    with self.assertRaises(Exception) as caught:
                        external_backup.export(root, self.values, key)
                self.assertIs(caught.exception, primary)
                catalog_path = next(root.glob('stages/external-backups/*/catalog.json'))
                catalog = json.loads(catalog_path.read_text())
                self.assertFalse(catalog['upload_verified'])
                self.assertFalse(catalog['cleanup_delete_requested'])
                service.delete_object.assert_called_once_with(
                    Bucket=self.values['S3_BUCKET'], Key=catalog['object_key']
                )
                self.assertEqual(key.read_bytes(), before)
                self.assertEqual(archive.read_bytes(), b'fictional frozen snapshot')
                self.assertIn(
                    'backup-cleanup PRIVATE_S3_ENV PRIVATE_CATALOG',
                    emit.call_args.kwargs['message'],
                )
                self.assertNotIn('EXAMPLE_PRIVATE_SIGNED_REQUEST', str(emit.call_args_list))

    def test_cleanup_audit_and_logger_failure_do_not_mask_primary_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'catalog.json').write_text('{"upload_verified":false}')
            primary = RuntimeError('original export failure')
            service = Mock()
            service.delete_object.side_effect = OSError('EXAMPLE_PRIVATE_SIGNED_REQUEST')
            output = io.StringIO()
            with (
                patch.object(
                    external_backup.state_store, 'write_json', side_effect=OSError('full disk')
                ),
                patch.object(
                    external_backup.lifecycle, 'emit', side_effect=OSError('log disk full')
                ),
                redirect_stdout(output),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    try:
                        raise primary
                    finally:
                        external_backup.cleanup_failed_upload(
                            service, self.values, 'backups/fictional.enc', directory
                        )
            self.assertIs(caught.exception, primary)
            record = json.loads(output.getvalue())
            self.assertFalse(record['catalog_updated'])
            self.assertFalse(record['delete_requested'])
            self.assertNotIn('EXAMPLE_PRIVATE_SIGNED_REQUEST', output.getvalue())

    def cleanup_fixture(self, root):
        folder = root / 'stages/external-backups' / ('20261006T030000Z-' + 'a' * 32)
        folder.mkdir(parents=True)
        path = folder / 'catalog.json'
        catalog = {
            'format': 1,
            'upload_verified': False,
            'object_key': self.values['S3_PREFIX'] + folder.name + '.matrix.enc',
            'storage': external_backup.storage_identity(self.values),
        }
        external_backup.state_store.write_json(path, catalog)
        return path, catalog

    def test_cleanup_own_unverified_catalog_and_repeat_without_second_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, catalog = self.cleanup_fixture(root)
            service = Mock()
            real_fstat = os.fstat

            def root_metadata(descriptor):
                info = real_fstat(descriptor)
                return SimpleNamespace(
                    st_uid=0, st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=info.st_size
                )

            with (
                patch.object(external_backup.os, 'fstat', side_effect=root_metadata),
                patch.object(external_backup.media_transfer, 'client', return_value=service),
                patch.object(external_backup.lifecycle, 'emit') as emit,
            ):
                external_backup.cleanup_catalog(root, self.values, path)
                external_backup.cleanup_catalog(root, self.values, path)
            service.delete_object.assert_called_once_with(
                Bucket=self.values['S3_BUCKET'], Key=catalog['object_key']
            )
            self.assertTrue(json.loads(path.read_text())['cleanup_delete_requested'])
            self.assertTrue(emit.call_args.kwargs['already_requested'])

    def test_cleanup_refuses_verified_foreign_legacy_or_unsafe_catalog_before_sdk(self):
        real_fstat = os.fstat

        def root_metadata(descriptor):
            info = real_fstat(descriptor)
            return SimpleNamespace(
                st_uid=0, st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=info.st_size
            )

        for case in (
            'verified',
            'foreign_key',
            'foreign_storage',
            'legacy',
            'mode',
            'hardlink',
            'symlink',
            'foreign_root',
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, catalog = self.cleanup_fixture(root)
                selected_root = root
                if case == 'verified':
                    catalog['upload_verified'] = True
                elif case == 'foreign_key':
                    catalog['object_key'] = 'backups/previous-verified.matrix.enc'
                elif case == 'foreign_storage':
                    catalog['storage']['S3_BUCKET'] = 'another-bucket'
                elif case == 'legacy':
                    catalog.pop('storage')
                external_backup.state_store.write_json(path, catalog)
                if case == 'mode':
                    path.chmod(0o644)
                elif case == 'hardlink':
                    os.link(path, root / 'hardlink')
                elif case == 'symlink':
                    original = path.with_name('original.json')
                    path.rename(original)
                    path.symlink_to(original)
                elif case == 'foreign_root':
                    selected_root = root / 'foreign-stack'
                with (
                    patch.object(external_backup.os, 'fstat', side_effect=root_metadata),
                    patch.object(external_backup.media_transfer, 'client') as client,
                ):
                    with self.assertRaises(RuntimeError):
                        external_backup.cleanup_catalog(selected_root, self.values, path)
                client.assert_not_called()

    def test_privacy_network_retry_requires_an_actual_anonymous_refusal(self):
        opener = Mock()
        opener.open.side_effect = [
            urllib.error.URLError(OSError('EXAMPLE_PRIVATE_RESPONSE')),
            urllib.error.HTTPError('https://s3.example.invalid', 403, 'private', {}, None),
        ]
        service = SimpleNamespace(meta=SimpleNamespace(endpoint_url='https://s3.example.invalid'))
        with (
            patch.object(external_backup.urllib.request, 'build_opener', return_value=opener),
            patch.object(external_backup.time, 'sleep') as pause,
            patch.object(external_backup.lifecycle, 'emit') as emit,
        ):
            external_backup.private_remote(service, self.values, 'backups/example.enc')
        self.assertEqual(opener.open.call_count, 2)
        pause.assert_called_once_with(2)
        self.assertEqual(emit.call_args.kwargs['reason'], 'dns_or_network_failure')
        self.assertNotIn('EXAMPLE_PRIVATE_RESPONSE', str(emit.call_args))

    def test_privacy_timeout_and_transient_http_then_not_found(self):
        opener = Mock()
        opener.open.side_effect = [
            TimeoutError('EXAMPLE_PRIVATE_RESPONSE'),
            urllib.error.HTTPError('https://s3.example.invalid', 503, 'temporary', {}, None),
            urllib.error.HTTPError('https://s3.example.invalid', 404, 'private', {}, None),
        ]
        service = SimpleNamespace(meta=SimpleNamespace(endpoint_url='https://s3.example.invalid'))
        with (
            patch.object(external_backup.urllib.request, 'build_opener', return_value=opener),
            patch.object(external_backup.time, 'sleep') as pause,
            patch.object(external_backup.lifecycle, 'emit') as emit,
        ):
            external_backup.private_remote(service, self.values, 'backups/example.enc')
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(
            [call.kwargs['reason'] for call in emit.call_args_list],
            ['timeout', 'temporary_http_failure'],
        )

    def test_privacy_exhausted_retries_keep_a_protected_exact_recovery_path(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLError('EXAMPLE_PRIVATE_RESPONSE'))
        service = SimpleNamespace(meta=SimpleNamespace(endpoint_url='https://s3.example.invalid'))
        with (
            patch.object(external_backup.urllib.request, 'build_opener', return_value=opener),
            patch.object(external_backup.time, 'sleep') as pause,
            patch.object(external_backup.lifecycle, 'emit') as emit,
        ):
            with self.assertRaisesRegex(RuntimeError, 'after three HTTPS attempts') as caught:
                external_backup.private_remote(service, self.values, 'backups/example.enc')
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(pause.call_count, 2)
        self.assertIn('install.sh backup-external PRIVATE_S3_ENV KEY_FILE', str(caught.exception))
        self.assertNotIn('EXAMPLE_PRIVATE_RESPONSE', str(caught.exception))
        self.assertTrue(
            all(call.kwargs['reason'] == 'tls_connection_failure' for call in emit.call_args_list)
        )

    def test_privacy_certificate_and_public_response_fail_without_retry(self):
        service = SimpleNamespace(meta=SimpleNamespace(endpoint_url='https://s3.example.invalid'))
        certificate = urllib.error.URLError(
            ssl.SSLCertVerificationError('EXAMPLE_PRIVATE_RESPONSE')
        )
        public = MagicMock()
        public.__enter__.return_value.status = 200
        for outcome, expected in (
            (certificate, 'certificate validation failed'),
            (public, 'anonymously accessible'),
        ):
            opener = Mock()
            if isinstance(outcome, Exception):
                opener.open.side_effect = outcome
            else:
                opener.open.return_value = outcome
            with (
                patch.object(external_backup.urllib.request, 'build_opener', return_value=opener),
                patch.object(external_backup.time, 'sleep') as pause,
            ):
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    external_backup.private_remote(service, self.values, 'backups/example.enc')
            self.assertEqual(opener.open.call_count, 1)
            pause.assert_not_called()
            self.assertNotIn('EXAMPLE_PRIVATE_RESPONSE', str(caught.exception))

    def test_provider_errors_identify_safe_recovery_without_provider_secret_text(self):
        class ProviderError(Exception):
            def __init__(self, code):
                self.response = {
                    'Error': {'Code': code, 'Message': 'EXAMPLE_PRIVATE_SIGNED_REQUEST'}
                }

        for code, expected in (
            ('SignatureDoesNotMatch', 'authentication'),
            ('AccessDenied', 'permissions'),
            ('403', 'permissions'),
            ('NoSuchBucket', 'not found'),
            ('UnknownCode', 'status'),
        ):
            message = external_backup.failure_message(ProviderError(code))
            self.assertIn(expected, message)
            self.assertNotIn('EXAMPLE_PRIVATE_SIGNED_REQUEST', message)
        timeout = type('ReadTimeoutError', (Exception,), {})('EXAMPLE_PRIVATE_SIGNED_REQUEST')
        self.assertIn('backup-recover', external_backup.failure_message(timeout))
        self.assertNotIn('EXAMPLE_PRIVATE_SIGNED_REQUEST', external_backup.failure_message(timeout))

    def test_snapshot_failure_returns_only_previously_running_writers(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            root = parent / 'stack'
            root.mkdir()
            backup_crypto.create_key(parent / 'key')
            metadata = {'media_mode': 's3'}
            metadata['domain'] = 'example.invalid'
            with (
                patch.object(external_backup.lifecycle, 'ROOT', root, create=True),
                patch.object(external_backup.lifecycle, 'state', return_value=metadata),
                patch.object(external_backup.lifecycle, 'wait_writers'),
                patch.object(
                    external_backup.lifecycle,
                    'service_rows',
                    return_value=[
                        {'Service': 'synapse', 'State': 'running'},
                        {'Service': 'mas', 'State': 'running'},
                    ],
                ),
                patch.object(external_backup.lifecycle, 'dc') as dc,
                patch.object(
                    external_backup.preflight,
                    'settings',
                    return_value={'S3_BUCKET': 'example-media', 'S3_PREFIX': 'media/'},
                ),
                patch.object(external_backup.media_transfer, 'client', return_value=Mock()),
                patch.object(
                    external_backup.media_snapshot,
                    'hydrate',
                    side_effect=RuntimeError('snapshot resource failure'),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, 'snapshot resource failure'):
                    external_backup.export(root, self.values, parent / 'key')
            self.assertEqual(dc.call_args_list[0].args, ('stop', '-t', '30', 'synapse', 'mas'))
            self.assertEqual(dc.call_args_list[-1].args, ('up', '-d', 'synapse', 'mas'))

    def test_overlapping_prefix_and_key_inside_installation_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backup_crypto.create_key(root / 'key')
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                external_backup.export(root, self.values, root / 'key')
            with tempfile.TemporaryDirectory() as keys:
                key = Path(keys) / 'key'
                backup_crypto.create_key(key)
                with (
                    patch.object(
                        external_backup.lifecycle, 'state', return_value={'media_mode': 's3'}
                    ),
                    patch.object(
                        external_backup.preflight,
                        'settings',
                        return_value={'S3_BUCKET': 'example-backups', 'S3_PREFIX': 'backups/live/'},
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, 'overlap'):
                        external_backup.export(root, self.values, key)

    def test_pending_credentials_refuse_export_before_stopping_services(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            root = parent / 'stack'
            root.mkdir()
            backup_crypto.create_key(parent / 'key')
            with (
                patch.object(
                    external_backup.lifecycle,
                    'state',
                    return_value={'credential_transition_pending': True},
                ),
                patch.object(external_backup.lifecycle, 'dc') as dc,
            ):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    external_backup.export(root, self.values, parent / 'key')
                dc.assert_not_called()

    def test_download_rejects_wrong_checksum_key_and_invalid_archive_without_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root / 'key'
            wrong = root / 'wrong'
            backup_crypto.create_key(key)
            backup_crypto.create_key(wrong)
            plain = root / 'source'
            plain.write_bytes(b'not an archive')
            encrypted = root / 'cipher'
            backup_crypto.encrypt(plain, encrypted, backup_crypto.read_key(key))
            checksum = hashlib.sha256(encrypted.read_bytes()).hexdigest()
            service = Mock()

            def download(bucket, object_key, destination, **kwargs):
                Path(destination).write_bytes(encrypted.read_bytes())

            service.download_file.side_effect = download
            transfer = SimpleNamespace(TransferConfig=lambda **kwargs: kwargs)
            with patch.dict(sys.modules, {'boto3.s3.transfer': transfer}):
                for selected, digest in ((key, '0' * 64), (wrong, checksum), (key, checksum)):
                    with self.assertRaises(Exception):
                        external_backup.fetch(
                            self.values,
                            selected,
                            'backups/test.matrix.enc',
                            digest,
                            root / 'restored',
                            service,
                        )
                    self.assertFalse((root / 'restored').exists())
                    self.assertFalse(list(root.glob('matrix-external-fetch-*')))
                service.download_file.reset_mock()
                (root / 'restored').write_bytes(b'preserve existing archive')
                with self.assertRaisesRegex(RuntimeError, 'new private file'):
                    external_backup.fetch(
                        self.values,
                        key,
                        'backups/test.matrix.enc',
                        checksum,
                        root / 'restored',
                        service,
                    )
                service.download_file.assert_not_called()
                self.assertEqual((root / 'restored').read_bytes(), b'preserve existing archive')
