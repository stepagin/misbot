"""Fresh invitation snapshots even when Synapse caches initial sync by filter."""

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import federation_matrix_api


class MatrixMembershipTests(unittest.TestCase):
    def test_each_operation_uses_fresh_filter_and_no_message_timeline(self):
        filters = []
        cache = {}
        current = {'room': '!first:fixture.example'}
        applied = []

        class Response(io.BytesIO):
            def __init__(self, value):
                super().__init__(json.dumps(value).encode())

        class Opener:
            def open(self, request, timeout):
                path = urllib.parse.urlsplit(request.full_url)
                if path.path.endswith('/account/whoami'):
                    return Response({'user_id': '@federation-bot:fixture.example'})
                if path.path.endswith('/sync'):
                    raw = urllib.parse.parse_qs(path.query)['filter'][0]
                    filters.append(json.loads(raw))
                    value = cache.setdefault(
                        raw,
                        {
                            'rooms': {
                                'invite': {
                                    current['room']: {
                                        'invite_state': {
                                            'events': [
                                                {
                                                    'type': 'm.room.member',
                                                    'state_key': '@federation-bot:fixture.example',
                                                    'sender': '@admin:fixture.example',
                                                    'content': {'membership': 'invite'},
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        },
                    )
                    return Response(value)
                if '/join/' in path.path:
                    applied.append(urllib.parse.unquote(path.path.split('/join/', 1)[1]))
                    return Response({})
                raise AssertionError('unexpected fixed path')

        for room in ('!first:fixture.example', '!second:fixture.example'):
            current['room'] = room
            payload = {
                'session': {
                    'user_id': '@federation-bot:fixture.example',
                    'access_token': 'FICTIONAL_SECRET',
                },
                'action': 'room-accept',
                'argument': room,
            }
            output = io.StringIO()
            with (
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))),
                patch('urllib.request.build_opener', return_value=Opener()),
                contextlib.redirect_stdout(output),
            ):
                federation_matrix_api.main()
            self.assertEqual(json.loads(output.getvalue())['result']['status'], 'applied')
            self.assertNotIn('FICTIONAL_SECRET', output.getvalue())
        self.assertEqual(applied, ['!first:fixture.example', '!second:fixture.example'])
        self.assertNotEqual(filters[0], filters[1])
        self.assertTrue(all(item['room']['timeline']['limit'] == 0 for item in filters))


if __name__ == '__main__':
    unittest.main()
