import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_crypto
import existing_s3 as s3
import misbot
from test_existing_compose import ExistingTests, BUNDLE

VALUES = {'S3_BUCKET': 'fictional-bucket', 'S3_REGION': 'fictional-region',
          'S3_ENDPOINT_URL': 'https://s3.example.org', 'S3_ACCESS_KEY_ID': 'FICTIONAL_ID',
          'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET', 'S3_PREFIX': 'fictional/backups/'}


class Service:
    def __init__(self):
        self.objects, self.parts, self.aborted = {}, {}, False
        self.metadata = {}
        self.meta = SimpleNamespace(service_model=SimpleNamespace(operation_model=lambda name:
            SimpleNamespace(input_shape=SimpleNamespace(members={'IfNoneMatch': None}))))

    def create_multipart_upload(self, **args):
        self.current = args
        return {'UploadId': 'FICTIONAL_UPLOAD'}

    def upload_part(self, **args):
        self.parts[args['PartNumber']] = args['Body']
        return {'ETag': str(args['PartNumber'])}

    def complete_multipart_upload(self, **args):
        assert args['IfNoneMatch'] == '*'
        if args['Key'] in self.objects:
            raise RuntimeError('collision')
        self.objects[args['Key']] = b''.join(self.parts[n] for n in sorted(self.parts))

    def abort_multipart_upload(self, **args):
        self.aborted = True

    def get_object(self, **args):
        if args['Key'] not in self.objects:
            error = RuntimeError('missing')
            error.response = {'Error': {'Code': 'NoSuchKey'}}
            raise error
        return {'Body': io.BytesIO(self.objects[args['Key']]), 'Metadata': self.metadata.get(args['Key'], self.current['Metadata'] if hasattr(self, 'current') else {})}

    def put_object(self, **args):
        assert args['IfNoneMatch'] == '*'
        if args['Key'] in self.objects:
            raise RuntimeError('collision')
        self.objects[args['Key']] = args['Body']
        self.metadata[args['Key']] = args['Metadata']


class ExistingS3Tests(unittest.TestCase):
    def test_conditional_parts_round_trip_and_tampering(self):
        service = Service()
        with tempfile.TemporaryDirectory() as folder:
            writer = s3.ObjectStream(VALUES, Path(folder) / 'catalog.json', lambda *a: None, service)
            writer.part_size = 1024 * 1024
            payload = b'fictional archive' * 190000
            backup_crypto.encrypt_stream(io.BytesIO(payload), writer, b'X' * 32)
            self.assertNotIn(writer.key, service.objects)
            self.assertLess(len(writer.buffer), writer.part_size)
            record = writer.complete()
            remote = s3.open_backup(VALUES, record, service)
            self.assertEqual(b''.join(backup_crypto.decrypted_stream(remote, b'X' * 32)), payload)
            self.assertEqual(remote.hash.hexdigest(), record['sha256'])
            chunk = record['chunks'][0]['key']
            original = service.objects[chunk]
            service.objects[chunk] = b'changed' + original[7:]
            with self.assertRaises(Exception):
                b''.join(backup_crypto.decrypted_stream(s3.open_backup(VALUES, record, service), b'X' * 32))
            service.objects[chunk] = original
            changed = dict(record, chunks=[dict(record['chunks'][0], key='foreign/path')])
            with self.assertRaises(RuntimeError):
                s3.open_backup(VALUES, changed, service)

    def test_conditional_part_collision_retains_old_data_and_intent(self):
        service = Service()
        with tempfile.TemporaryDirectory() as folder:
            catalog = Path(folder) / 'catalog.json'
            writer = s3.ObjectStream(VALUES, catalog, lambda p, v: p.write_text(json.dumps(v)), service)
            first = VALUES['S3_PREFIX'] + writer.owner + '/00000'
            service.objects[first] = b'existing'
            writer.write(b'new')
            with self.assertRaises(RuntimeError):
                writer.complete()
            writer.abort()
            self.assertEqual(service.objects[first], b'existing')
            self.assertTrue(json.loads(catalog.read_text())['incomplete_parts_retained'])

    def test_stream_archive_round_trip_final_frame_and_bounded_parts(self):
        service = Service()
        with tempfile.TemporaryDirectory() as folder:
            catalog = Path(folder) / 'catalog.json'
            writer = s3.Multipart(VALUES, catalog, lambda p, v: p.write_text(json.dumps(v)), service)
            writer.part_size = 1024 * 1024
            payload = b'fictional archive' * 190000
            backup_crypto.encrypt_stream(io.BytesIO(payload), writer, b'X' * 32)
            self.assertLess(len(writer.buffer), writer.part_size)
            self.assertFalse(service.objects)
            record = writer.complete()
            remote = s3.open_backup(VALUES, record, service)
            self.assertEqual(b''.join(backup_crypto.decrypted_stream(remote, b'X' * 32)), payload)
            self.assertEqual(remote.hash.hexdigest(), record['sha256'])
            self.assertEqual(remote.size, record['bytes'])
            with self.assertRaises(RuntimeError):
                b''.join(backup_crypto.decrypted_stream(io.BytesIO(service.objects[record['key']][:-1]), b'X' * 32))

    def test_wrong_profile_and_foreign_owner_are_refused(self):
        service = Service()
        with tempfile.TemporaryDirectory() as folder:
            writer = s3.Multipart(VALUES, Path(folder) / 'catalog.json', lambda *a: None, service)
            backup_crypto.encrypt_stream(io.BytesIO(b'fictional'), writer, b'X' * 32)
            record = writer.complete()
            for changed in (dict(record, owner='0' * 32), dict(record, storage={})):
                with self.assertRaises(RuntimeError): s3.open_backup(VALUES, changed, service)
            service.current['Metadata'] = {'misbot-backup': 'another-owner'}
            with self.assertRaises(RuntimeError): s3.open_backup(VALUES, record, service)
            self.assertFalse(service.aborted)

    def test_multipart_metadata_names_are_case_insensitive(self):
        service = Service()
        with tempfile.TemporaryDirectory() as folder:
            writer = s3.Multipart(VALUES, Path(folder) / 'catalog.json', lambda *a: None, service)
            backup_crypto.encrypt_stream(io.BytesIO(b'fictional'), writer, b'X' * 32)
            record = writer.complete()
            service.current['Metadata'] = {'Misbot-Backup': writer.owner}
            remote = s3.open_backup(VALUES, record, service)
            self.assertEqual(b''.join(backup_crypto.decrypted_stream(remote, b'X' * 32)), b'fictional')

    def test_overlapping_destinations_are_refused(self):
        for prefix in ('fictional/', 'fictional/backups/', 'fictional/backups/nested/'):
            with self.assertRaises(RuntimeError): s3.separate(VALUES, dict(VALUES, S3_PREFIX=prefix))
        s3.separate(VALUES, dict(VALUES, S3_PREFIX='fictional/media/'))

    def test_existing_s3_cli_keeps_all_arguments(self):
        fixture = ExistingTests()
        with tempfile.TemporaryDirectory() as folder:
            root, _ = fixture.fixture(Path(folder))
            for words in (['storage', 's3', 'connect', '--config', '/root/fictional.env'],
                          ['storage', 's3', 'check', '--config', '/root/fictional.env', '--purpose', 'media', '--read-only'],
                          ['backup', 'external', 'create', '--config', '/root/fictional.env', '--key', '/root/fictional.key']):
                command, options, values = misbot.parse([*words, '--directory', str(root)])
                result = misbot.plan(command, options, values, source=BUNDLE)
                self.assertEqual(result[1], str(root / 'existing_compose.py'))
                self.assertIn('/root/fictional.env', result)
                if '--key' in words: self.assertIn('/root/fictional.key', result)
                if '--read-only' in words: self.assertIn('--read-only', result)

    def test_remote_collision_never_replaces_existing_ciphertext(self):
        service = Service()
        with tempfile.TemporaryDirectory() as folder:
            writer = s3.Multipart(VALUES, Path(folder) / 'catalog.json', lambda *a: None, service)
            writer.write(b'new')
            service.objects[writer.key] = b'existing'
            with self.assertRaises(RuntimeError): writer.complete()
            writer.abort()
            self.assertTrue(service.aborted)
            self.assertEqual(service.objects[writer.key], b'existing')


if __name__ == '__main__':
    unittest.main()
