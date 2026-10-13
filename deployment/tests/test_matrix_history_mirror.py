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

    def test_run_preserves_long_scan_across_intervals_and_resets_after_completion(self):
        root = Path(self.temp.name)
        token = root / 'token'
        token.write_text('fictional-token')
        token.chmod(0o600)
        config = {'interval': 5, 'reconcile_interval': 60}
        for side in ('source', 'target'):
            config.update(
                {
                    side + '_url': 'https://' + side + '.example',
                    side + '_user': '@mirror:' + side + '.example',
                    side + '_token_file': str(token),
                }
            )
        mirror.write_private(root / 'config.json', config)
        now = [1000]
        cycles = []
        requests = {'source': [], 'target': []}
        joined = [True]

        class Client:
            def __init__(self, side, config):
                self.side = side

            def get(self, path):
                if path == 'account/whoami':
                    return {'user_id': config[self.side + '_user']}
                return {'joined_rooms': [ROOM] if joined[0] or self.side == 'source' else []}

            def sync(self, since=None):
                return {
                    'next_batch': str(now[0]),
                    'rooms': {'join': {ROOM: {'timeline': {'events': [], 'prev_batch': '1'}}}},
                }

            def messages(self, room_id, cursor):
                page = int(cursor)
                requests[self.side].append(page)
                response = {'chunk': [{'event_id': '$' + str(page)}]}
                if page < 100:
                    response['end'] = str(page + 1)
                return response

        def tick(db, seconds):
            cycles.append(
                db.execute('SELECT source_done, target_done, status FROM rooms').fetchone()
            )
            now[0] += 70
            if len(cycles) == 21:
                joined[0] = False
            if len(cycles) >= 22:
                mirror.STOP = True

        with (
            patch.object(mirror, 'STOP', False),
            patch.object(mirror, 'MatrixClient', Client),
            patch.object(mirror.time, 'time', side_effect=lambda: now[0]),
            patch.object(mirror, 'sleep_until_next', side_effect=tick),
            patch.object(mirror.signal, 'signal'),
        ):
            mirror.run(root)
        # Five pages per side/cycle: an elapsed interval must not erase progress.
        self.assertIn((1, 1, 'LIVE'), cycles)
        self.assertEqual(requests['source'][:100], list(range(1, 101)))
        self.assertEqual(requests['target'][:100], list(range(1, 101)))
        self.assertEqual(requests['source'][100], 1)
        self.assertEqual(cycles[-1][2], 'WAIT_JOIN')

    def test_membership_survives_reconcile_and_rejoin_preserves_inventory(self):
        self.db.execute(
            'INSERT INTO rooms(room_id,source_done,target_done) VALUES (?,1,1)', (ROOM,)
        )
        mirror.record_events(self.db, 'source', ROOM, [{'event_id': '$saved'}])
        mirror.record_events(self.db, 'target', ROOM, [{'event_id': '$saved'}])
        for joined, expected in (({ROOM}, 'LIVE'), (set(), 'WAIT_JOIN'), ({ROOM}, 'LIVE')):
            mirror.reconcile(self.db, joined)
            self.assertEqual(self.db.execute('SELECT status FROM rooms').fetchone()[0], expected)
            self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 2)

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
        mirror.reconcile(self.db, {ROOM})
        self.assertEqual(self.db.execute('SELECT status FROM rooms').fetchone()[0], 'GAPS')

        # A later full scan can retrieve the old event after federation recovers.
        mirror.reset_side(self.db, 'target', ROOM)
        mirror.apply_sync(self.db, 'target', target_sync, {ROOM})
        target = FakeClient(
            {'target-first': {'chunk': [{'event_id': '$old'}], 'start': 'target-first'}}
        )
        mirror.scan_room(self.db, target, 'target', ROOM)
        mirror.reconcile(self.db, {ROOM})
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
