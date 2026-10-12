"""Lost API responses must reuse a durable invitation and never grant admin."""

import copy
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from datetime import datetime, timezone
import sqlite3
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import registration
import state_store


class RegistrationTests(unittest.TestCase):
    def test_list_accepts_zero_usage_limit_without_exposing_token(self):
        result = registration.list_invitations(
            self.root,
            lambda *_: {
                'status': 200,
                'body': {
                    'data': [
                        {
                            'id': '0' * 26,
                            'attributes': {
                                'usage_limit': 0,
                                'times_used': 0,
                                'expires_at': None,
                                'token': 'PRIVATE_FIXTURE',
                            },
                        }
                    ]
                },
            },
        )
        self.assertEqual(result['invitations'][0]['state'], 'used')
        self.assertNotIn('PRIVATE_FIXTURE', json.dumps(result))

    def test_fifo_issuer_database_is_rejected_without_waiting(self):
        path = self.root / 'data/referral-control/data/bot-control.sqlite3'
        path.parent.mkdir(mode=0o700, parents=True)
        os.mkfifo(path, mode=0o600)
        code = (
            'import sys; from pathlib import Path; '
            'sys.path.insert(0, sys.argv[1]); import registration\n'
            "try: registration.invitation_issuers(Path(sys.argv[2]), 'fictional.example.org')\n"
            'except RuntimeError: raise SystemExit(0)\n'
            'raise SystemExit(1)\n'
        )
        child = subprocess.run(
            [
                sys.executable,
                '-B',
                '-c',
                code,
                str(Path(registration.__file__).parent),
                str(self.root),
            ],
            capture_output=True,
            timeout=2,
        )
        self.assertEqual(child.returncode, 0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'secrets').mkdir(mode=0o700)
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'fictional.example.org', 'databases_initialized': True},
        )
        state_store.write_text(self.root / 'settings.env', 'DOMAIN=fictional.example.org\n')
        self.items = {}
        self.created = 0
        self.lost = False
        self.owned = patch.object(registration.lifecycle, 'ownership_check')
        self.owned.start()

    def tearDown(self):
        self.owned.stop()
        self.temp.cleanup()

    def transport(self, root, path, method='GET', body=None):
        if path == registration.BASE and method == 'POST':
            self.created += 1
            identifier = '0' * 25 + str(self.created)
            self.items[identifier] = {
                'id': identifier,
                'attributes': dict(body, times_used=0, revoked_at=None),
            }
            if self.lost:
                self.lost = False
                raise RuntimeError('FICTIONAL_LOST_RESPONSE')
            return {'status': 201, 'body': {'data': copy.deepcopy(self.items[identifier])}}
        if path == registration.BASE:
            return {'status': 200, 'body': {'data': copy.deepcopy(list(self.items.values()))}}
        identifier = path.split('/')[-2] if path.endswith('/revoke') else path.split('/')[-1]
        if path.endswith('/revoke'):
            self.items[identifier]['attributes']['revoked_at'] = '2026-01-01T00:00:00Z'
        return {'status': 200, 'body': {'data': copy.deepcopy(self.items[identifier])}}

    def test_lost_create_repeat_use_revoke_and_explicit_replacement(self):
        self.lost = True
        with self.assertRaises(RuntimeError):
            registration.manage(self.root, 'ensure', self.transport)
        saved = configuration.read_private_json(self.root / 'secrets/first-registration.json')[
            'token'
        ]
        result = registration.manage(self.root, 'ensure', self.transport)
        self.assertEqual(self.created, 1)
        self.assertEqual(result['state'], 'valid')
        self.assertNotIn(saved, json.dumps(result))
        self.assertEqual(result['usage_limit'], 1)
        identifier = next(iter(self.items))
        self.items[identifier]['attributes']['times_used'] = 1
        self.assertEqual(registration.manage(self.root, 'ensure', self.transport)['state'], 'used')
        self.assertEqual(self.created, 1)
        self.assertEqual(
            registration.manage(self.root, 'revoke', self.transport)['state'], 'revoked'
        )
        registration.manage(self.root, 'new', self.transport)
        self.assertEqual(self.created, 2)
        self.assertEqual(sum(not x['attributes']['revoked_at'] for x in self.items.values()), 1)
        self.assertFalse(any('admin' in item['attributes'] for item in self.items.values()))

    def test_pending_status_does_not_issue_and_local_revoke_prevents_later_ensure(self):
        state_store.write_json(
            self.root / 'secrets/first-registration.json',
            {
                'format': 1,
                'domain': 'fictional.example.org',
                'token': 'FICTIONAL_PENDING',
                'expires_at': '2030-01-01T00:00:00+00:00',
                'state': 'pending',
            },
        )
        self.assertEqual(
            registration.manage(self.root, 'status', self.transport)['state'], 'pending'
        )
        self.assertEqual(self.created, 0)
        self.assertEqual(
            registration.manage(self.root, 'revoke', self.transport)['state'], 'revoked'
        )
        self.assertEqual(
            registration.manage(self.root, 'ensure', self.transport)['state'], 'revoked'
        )
        self.assertEqual(self.created, 0)

    def test_pending_database_and_identity_block_issuance(self):
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'fictional.example.org', 'databases_initialized': False},
        )
        with self.assertRaises(RuntimeError):
            registration.manage(self.root, 'ensure', self.transport)
        self.assertEqual(self.created, 0)

    def test_list_all_invitation_states_and_issuers_without_tokens(self):
        path = self.root / 'data/referral-control/data/bot-control.sqlite3'
        path.parent.mkdir(mode=0o700, parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE invitations(server,resource,uid,inviter_name,code)')
            db.execute(
                'INSERT INTO invitations VALUES(?,?,?,?,?)',
                (
                    'fictional.example.org',
                    '0' * 25 + '1',
                    '@alice:fictional.example.org',
                    None,
                    'PRIVATE_FIXTURE_TOKEN',
                ),
            )
            db.execute(
                'INSERT INTO invitations VALUES(?,?,?,?,?)',
                (
                    'fictional.example.org',
                    '0' * 25 + '2',
                    42,
                    'Имя <пользователя>',
                    'PRIVATE_FIXTURE_TOKEN_TWO',
                ),
            )
        path.chmod(0o600)
        kinds = (
            {'expires_at': '2031-01-01T00:00:00Z'},
            {'times_used': 1},
            {'expires_at': '2020-01-01T00:00:00Z'},
            {'revoked_at': '2020-01-01T00:00:00Z'},
        )
        items = [
            {
                'id': '0' * 25 + str(number),
                'attributes': dict(
                    token='PRIVATE_FIXTURE_TOKEN_' + str(number),
                    usage_limit=1,
                    times_used=0,
                    expires_at='2031-01-01T00:00:00Z',
                )
                | attributes,
            }
            for number, attributes in enumerate(kinds, 1)
        ]
        calls = []

        def pages(root, request):
            calls.append(request)
            if request == registration.BASE:
                return {
                    'status': 200,
                    'body': {'data': items[:2], 'links': {'next': registration.BASE + '?page=2'}},
                }
            return {'status': 200, 'body': {'data': items[2:]}}

        result = registration.list_invitations(
            self.root, pages, datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(
            [item['state'] for item in result['invitations']],
            ['valid', 'used', 'expired', 'revoked'],
        )
        self.assertEqual(result['invitations'][0]['issuer'], '@alice:fictional.example.org')
        self.assertEqual(result['invitations'][1]['issuer'], 'Имя <пользователя>')
        self.assertEqual(result['invitations'][2]['issuer'], 'не указан')
        self.assertNotIn('PRIVATE_FIXTURE', json.dumps(result))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.created, 0)

    def test_list_missing_metadata_does_not_create_database_or_invitation(self):
        result = registration.manage(self.root, 'list', self.transport)
        self.assertEqual(result, {'invitations': []})
        self.assertFalse((self.root / 'data/referral-control/data/bot-control.sqlite3').exists())
        self.assertFalse((self.root / 'secrets/first-registration.json').exists())
        self.assertEqual(self.created, 0)

    def test_list_rejects_repeated_or_foreign_pagination_without_token_echo(self):
        for next_path in (
            registration.BASE,
            'https://other.example/api',
            registration.BASE + '\nsecret',
        ):
            with self.subTest(next_path=next_path), self.assertRaises(RuntimeError):
                registration.list_invitations(
                    self.root,
                    lambda *args: {
                        'status': 200,
                        'body': {'data': [], 'links': {'next': next_path}},
                    },
                )

    def test_list_rejects_unsafe_database_and_reads_legacy_issuer_schema(self):
        path = self.root / 'data/referral-control/data/bot-control.sqlite3'
        path.parent.mkdir(mode=0o700, parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE invitations(server,resource,uid)')
            db.execute(
                'INSERT INTO invitations VALUES(?,?,?)',
                ('fictional.example.org', '0' * 26, '@alice:fictional.example.org'),
            )
        path.chmod(0o600)
        self.assertEqual(
            registration.invitation_issuers(self.root, 'fictional.example.org'),
            {'0' * 26: '@alice:fictional.example.org'},
        )
        path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            registration.invitation_issuers(self.root, 'fictional.example.org')
