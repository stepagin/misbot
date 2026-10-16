import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_snapshot


class Objects:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, name):
        return self

    def paginate(self, **kwargs):
        yield {'Contents': [{'Key': k, 'Size': len(v)} for k, v in self.objects.items()]}

    def get_object(self, **kwargs):
        return {'Body': io.BytesIO(self.objects[kwargs['Key']])}


class MediaSnapshotTests(unittest.TestCase):
    values = {
        'S3_BUCKET': 'example-bucket',
        'S3_PREFIX': 'example/',
        'S3_REGION': 'example-region',
        'S3_ENDPOINT_URL': 'https://s3.example.invalid',
    }

    def test_existing_original_also_requires_download_scratch_space(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / 'media'
            media.mkdir()
            original = media / 'object'
            original.write_bytes(b'Example')
            service = Objects({'example/object': b'Example'})
            service.get_object = Mock(wraps=service.get_object)
            with patch.object(
                media_snapshot.shutil, 'disk_usage', return_value=Mock(free=32 * 1024**2)
            ):
                with self.assertRaisesRegex(RuntimeError, 'free disk'):
                    media_snapshot.hydrate(media, self.values, root / 'inventory', service)
            service.get_object.assert_not_called()
            self.assertEqual(original.read_bytes(), b'Example')
            self.assertFalse((root / 'inventory').exists())
            self.assertFalse(list(media.glob('.matrix-s3-*')))

    def test_missing_original_restored_and_repeat_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'media').mkdir()
            service = Objects({'example/local_content/aa/bb/object': b'Example media.'})
            result = media_snapshot.hydrate(
                root / 'media', self.values, root / 'inventory', service
            )
            self.assertEqual(result['missing_local_objects_restored'], 1)
            self.assertEqual(
                (root / 'media/local_content/aa/bb/object').read_bytes(), b'Example media.'
            )
            result = media_snapshot.hydrate(
                root / 'media', self.values, root / 'inventory', service
            )
            self.assertEqual(result['missing_local_objects_restored'], 0)
            self.assertEqual(len(json.loads((root / 'inventory').read_text())['files']), 1)

    def test_conflicting_original_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'media').mkdir()
            (root / 'media/object').write_bytes(b'Local example.')
            with self.assertRaisesRegex(RuntimeError, 'differ'):
                media_snapshot.hydrate(
                    root / 'media',
                    self.values,
                    root / 'inventory',
                    Objects({'example/object': b'Remote example.'}),
                )
            self.assertEqual((root / 'media/object').read_bytes(), b'Local example.')
            self.assertFalse(list((root / 'media').glob('.matrix-s3-*')))

    def test_path_traversal_and_local_symlink_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'media').mkdir()
            (root / 'outside').mkdir()
            for key in ('example/../escape', 'example//escape'):
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    media_snapshot.hydrate(
                        root / 'media', self.values, root / 'inventory', Objects({key: b'Example'})
                    )
            (root / 'media/link').symlink_to(root / 'outside', target_is_directory=True)
            with self.assertRaises(RuntimeError):
                media_snapshot.hydrate(
                    root / 'media',
                    self.values,
                    root / 'inventory',
                    Objects({'example/link/object': b'Example'}),
                )
            self.assertFalse(list((root / 'outside').iterdir()))
            (root / 'alias').symlink_to(root / 'outside', target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'directory'):
                media_snapshot.hydrate(root / 'alias', self.values, root / 'inventory', Objects({}))
