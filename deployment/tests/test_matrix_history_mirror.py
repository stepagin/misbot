import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'matrix-history-mirror.py'
SPEC = importlib.util.spec_from_file_location('matrix_history_mirror', SCRIPT)
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)
ROOM = '!room:old.example'


class FakeClient:
    def __init__(self, pages):
        self.pages = pages

    def messages(self, room_id, cursor):
        assert room_id == ROOM
        return self.pages[cursor]


class FailingOnceClient(FakeClient):
    def __init__(self, pages):
        super().__init__(pages)
        self.failed = False

    def messages(self, room_id, cursor):
        if not self.failed:
            self.failed = True
            raise mirror.MirrorError('source', 'UNAVAILABLE')
        return super().messages(room_id, cursor)


class MirrorStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = mirror.connect(Path(self.temp.name))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_backfill_resumes_and_reaches_live_only_after_event_ids_match(self):
        source_sync = {
            'next_batch': 'source-next',
            'rooms': {
                'join': {
                    ROOM: {
                        'timeline': {'events': [{'event_id': '$new'}], 'prev_batch': 'source-first'}
                    }
                }
            },
        }
        target_sync = {
            'next_batch': 'target-next',
            'rooms': {
                'join': {
                    ROOM: {
                        'timeline': {'events': [{'event_id': '$new'}], 'prev_batch': 'target-first'}
                    }
                }
            },
        }
        mirror.apply_sync(self.db, 'source', source_sync, {ROOM})
        mirror.apply_sync(self.db, 'target', target_sync, {ROOM})
        source = FakeClient(
            {'source-first': {'chunk': [{'event_id': '$old'}], 'start': 'source-first'}}
        )
        target = FakeClient(
            {
                'target-first': {'chunk': [], 'start': 'target-first', 'end': 'target-second'},
                'target-second': {'chunk': [], 'start': 'target-second'},
            }
        )
        mirror.scan_room(self.db, source, 'source', ROOM)
        mirror.scan_room(self.db, target, 'target', ROOM)
        mirror.reconcile(self.db)
        self.assertEqual(self.db.execute('SELECT status FROM rooms').fetchone()[0], 'GAPS')

        # A later full scan can retrieve the old event after federation recovers.
        mirror.reset_side(self.db, 'target', ROOM)
        mirror.apply_sync(self.db, 'target', target_sync, {ROOM})
        target = FakeClient(
            {'target-first': {'chunk': [{'event_id': '$old'}], 'start': 'target-first'}}
        )
        mirror.scan_room(self.db, target, 'target', ROOM)
        mirror.reconcile(self.db)
        self.assertEqual(self.db.execute('SELECT status FROM rooms').fetchone()[0], 'LIVE')

        # The checkpoint and event IDs survive a worker restart.
        self.db.close()
        self.db = mirror.connect(Path(self.temp.name))
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 4)
        self.assertEqual(mirror.get_meta(self.db, 'source_since'), 'source-next')

    def test_limited_sync_restarts_backfill_from_prev_batch(self):
        mirror.apply_sync(
            self.db,
            'source',
            {
                'next_batch': 'one',
                'rooms': {'join': {ROOM: {'timeline': {'events': [], 'prev_batch': 'old'}}}},
            },
            {ROOM},
        )
        mirror.apply_sync(
            self.db,
            'source',
            {
                'next_batch': 'two',
                'rooms': {
                    'join': {
                        ROOM: {
                            'timeline': {
                                'events': [{'event_id': '$new'}],
                                'prev_batch': 'gap',
                                'limited': True,
                            }
                        }
                    }
                },
            },
            {ROOM},
        )
        self.assertEqual(
            self.db.execute('SELECT source_cursor, source_done FROM rooms').fetchone(), ('gap', 0)
        )

    def test_source_failure_keeps_cursor_for_retry(self):
        mirror.apply_sync(
            self.db,
            'source',
            {
                'next_batch': 'one',
                'rooms': {'join': {ROOM: {'timeline': {'events': [], 'prev_batch': 'saved'}}}},
            },
            {ROOM},
        )
        source = FailingOnceClient({'saved': {'chunk': [{'event_id': '$old'}], 'start': 'saved'}})
        with self.assertRaises(mirror.MirrorError):
            mirror.scan_room(self.db, source, 'source', ROOM)
        self.assertEqual(self.db.execute('SELECT source_cursor FROM rooms').fetchone()[0], 'saved')
        mirror.scan_room(self.db, source, 'source', ROOM)
        self.assertEqual(self.db.execute('SELECT source_done FROM rooms').fetchone()[0], 1)


class SecurityTests(unittest.TestCase):
    def test_rejects_http_and_url_credentials(self):
        for url in (
            'http://old.example',
            'https://name:secret@old.example',
            'https://old.example/path',
        ):
            with self.assertRaises(ValueError):
                mirror.validate_url(url)

    def test_rejects_loose_token_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('secret')
            token.chmod(0o644)
            with self.assertRaises(ValueError):
                mirror.private_file(token)
            token.chmod(0o600)
            mirror.private_file(token)


class MediaTests(unittest.TestCase):
    def test_manifest_and_cached_only_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'media.tsv'
            manifest.write_text(
                'AbCd123\t3\tba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n'
            )
            manifest.chmod(0o600)
            db = mirror.connect(root / 'state')
            config = {'media_manifest': str(manifest), 'media_origin': 'old.example'}
            mirror.load_media_manifest(db, config)
            with patch.object(
                mirror,
                'fetch_media',
                return_value=(
                    3,
                    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
                ),
            ) as fetch:
                mirror.prefetch_media(db, config, object())
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args_list[1].args[3], True)
            self.assertEqual(db.execute('SELECT status FROM media').fetchone()[0], 'VERIFIED')
            db.close()

    def test_media_mismatch_is_reported_and_retried_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'media.tsv'
            manifest.write_text('AbCd123\t3\n')
            manifest.chmod(0o600)
            db = mirror.connect(root / 'state')
            config = {'media_manifest': str(manifest), 'media_origin': 'old.example'}
            mirror.load_media_manifest(db, config)
            with patch.object(mirror, 'fetch_media', side_effect=[(3, 'a'), (3, 'b')]):
                mirror.prefetch_media(db, config, object())
            status, attempts, retry = db.execute(
                'SELECT status, attempts, next_retry FROM media'
            ).fetchone()
            self.assertEqual((status, attempts), ('ERROR', 1))
            self.assertGreater(retry, 0)
            db.close()


if __name__ == '__main__':
    unittest.main()
