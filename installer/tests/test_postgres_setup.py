import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import postgres_setup
import state_store


class Cluster:
    def __init__(self, root):
        self.root = root
        self.roles = {}
        self.databases = {}
        self.statements = []
        self.lost_reply = False

    def __call__(self, *args, stdin=None):
        self.statements.append((args, stdin))
        if stdin is not None:
            if 'CREATE ROLE ' not in stdin:
                return '1\n'
            name = re.search(r'CREATE ROLE (synapse|mas) LOGIN', stdin).group(1)
            owner = json.loads((self.root / 'config/external-postgres-owner.json').read_text())[
                'owner'
            ]
            self.roles.setdefault(name, {'name': name, 'marker': 'matrix-installer:' + owner})
            if self.lost_reply:
                self.lost_reply = False
                raise ConnectionError('Test response lost after role commit')
        elif args[-1].startswith('CREATE DATABASE '):
            name = args[-1].split()[2]
            self.databases[name] = {
                'name': name,
                'owner': name,
                'encoding': 'UTF8',
                'collate': 'C',
                'ctype': 'C',
            }
        else:
            return json.dumps(
                {'roles': list(self.roles.values()), 'databases': list(self.databases.values())}
            )
        return ''


class PostgresSetupTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'config').mkdir()
        (root / 'secrets').mkdir()
        state_store.write_json(root / 'installation.json', {'domain': 'example.invalid'})
        state_store.write_text(
            root / 'settings.env', 'POSTGRES_HOST=db.example.invalid\nPOSTGRES_PORT=5432\n'
        )
        for name in ('synapse', 'mas'):
            state_store.write_text(
                root / 'secrets' / ('db_' + name), "Example'PASSWORD-NAME-MARKER-ROLE-secret"
            )
        return Cluster(root)

    def test_lost_role_commit_can_resume_with_same_ownership_and_no_secret_argv(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cluster = self.fixture(root)
            cluster.lost_reply = True
            with self.assertRaises(ConnectionError):
                postgres_setup.initialize(root, cluster)
            saved = (root / 'config/external-postgres-owner.json').read_bytes()
            self.assertEqual(set(cluster.roles), {'synapse'})
            result = postgres_setup.initialize(root, cluster)
            self.assertEqual(result['installer_owned_databases'], 2)
            self.assertEqual((root / 'config/external-postgres-owner.json').read_bytes(), saved)
            postgres_setup.initialize(root, cluster)
            for args, stdin in cluster.statements:
                self.assertNotIn('Example', repr(args))
                if stdin and 'CREATE ROLE ' in stdin:
                    self.assertIn("PASSWORD 'Example''PASSWORD-NAME-MARKER-ROLE-secret'", stdin)
                    self.assertTrue(stdin.startswith(postgres_setup.PRIVATE_ROLE_LOGGING_SQL))

    def test_foreign_second_role_refused_without_creating_first(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cluster = self.fixture(root)
            cluster.roles['mas'] = {'name': 'mas', 'marker': None}
            with self.assertRaisesRegex(RuntimeError, 'foreign Matrix role'):
                postgres_setup.initialize(root, cluster)
            self.assertEqual(set(cluster.roles), {'mas'})
            self.assertTrue(
                all(
                    stdin is None or stdin == postgres_setup.PRIVATE_ROLE_LOGGING_SQL
                    for args, stdin in cluster.statements
                )
            )

    def test_changed_cluster_and_incompatible_database_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cluster = self.fixture(root)
            postgres_setup.initialize(root, cluster)
            cluster.databases['mas']['collate'] = 'C.UTF-8'
            with self.assertRaisesRegex(RuntimeError, 'incompatible'):
                postgres_setup.initialize(root, cluster)
            state_store.write_text(
                root / 'settings.env', 'POSTGRES_HOST=other.example.invalid\nPOSTGRES_PORT=5432\n'
            )
            count = len(cluster.statements)
            with self.assertRaisesRegex(RuntimeError, 'another cluster'):
                postgres_setup.initialize(root, cluster)
            self.assertEqual(
                len(cluster.statements), count
            )  # Changed destination is refused before any connection.

    def test_privacy_control_permission_failure_sends_no_password_or_ddl(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            calls = []

            def refused(*args, stdin=None):
                calls.append((args, stdin))
                raise RuntimeError('EXAMPLE_SECRET_ERROR_NOT_FOR_OUTPUT')

            with self.assertRaisesRegex(RuntimeError, 'No role password statement sent') as error:
                postgres_setup.initialize(root, refused)
            self.assertNotIn('EXAMPLE_SECRET_ERROR_NOT_FOR_OUTPUT', str(error.exception))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], postgres_setup.PRIVATE_ROLE_LOGGING_SQL)
            self.assertFalse((root / 'config/external-postgres-owner.json').exists())
            self.assertNotIn('Example', calls[0][1])
