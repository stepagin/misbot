"""Interrupted two-role changes must resume with one durable replacement."""

import json
import ast
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import pg_credentials as operation
import state_store


class DatabasePasswordTests(unittest.TestCase):
    def test_cli_preserves_public_S3_recovery_hint_but_never_echoes_driver_errors(self):
        for error, hint, omitted in (
            (
                state_store.S3CredentialRecoveryRequired(
                    'Run sudo bash /fictional/install.sh s3-credentials-recover'
                ),
                's3-credentials-recover',
                'database-password-recover',
            ),
            (
                RuntimeError('FICTIONAL_PRIVATE_DRIVER_PASSWORD'),
                'database-password-recover',
                'FICTIONAL_PRIVATE_DRIVER_PASSWORD',
            ),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(operation, 'main', side_effect=error),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(operation.run_cli(), 1)
                result = json.loads(output.getvalue())
                self.assertFalse(result['passed'])
                self.assertIn(hint, result['message'])
                self.assertNotIn(omitted, result['message'])

    def fixture(self, root):
        for name in ('secrets', 'config', 'stages'):
            (root / name).mkdir()
        metadata = {
            'domain': 'fictional.invalid',
            'databases_initialized': True,
            'database_mode': 'local',
        }
        state_store.write_json(root / 'installation.json', metadata)
        for name in ('synapse', 'mas'):
            state_store.write_text(root / 'secrets' / ('db_' + name), 'FICTIONAL_OLD_' + name)
        state_store.write_text(root / 'secrets/identity-sentinel', 'FICTIONAL_UNRELATED_IDENTITY')
        return metadata

    def context(self, root):
        return patch.multiple(
            lifecycle,
            create=True,
            ROOT=root,
            state=lambda: json.loads((root / 'installation.json').read_text()),
            backup=Mock(),
            restore_check=Mock(),
            postgres=Mock(),
            generate=Mock(),
            dc=Mock(),
            service_rows=Mock(return_value=[]),
            check=Mock(),
            emit=Mock(),
        )

    def test_failure_before_checkpoint_does_not_change_passwords(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(
                    lifecycle, 'restore_check', side_effect=RuntimeError('restore failed')
                ),
            ):
                with self.assertRaises(RuntimeError):
                    operation.rotate(root)
                lifecycle.dc.assert_not_called()
            self.assertFalse((root / operation.JOURNAL).exists())
            self.assertEqual((root / 'secrets/db_synapse').read_text(), 'FICTIONAL_OLD_synapse')

    def test_lost_commit_response_keeps_checkpoint_and_forward_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            metadata = self.fixture(root)
            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(
                    operation, 'apply', side_effect=[RuntimeError('lost COMMIT'), None]
                ) as apply,
            ):
                with self.assertRaises(RuntimeError):
                    operation.rotate(root)
                replacement = apply.call_args.args[1]
                self.assertEqual((root / 'secrets/db_mas').read_text(), 'FICTIONAL_OLD_mas')
                with patch.dict(os.environ, {}, clear=True):
                    for guard in (
                        state_store.assert_update_allowed,
                        state_store.assert_database_allowed,
                    ):
                        with self.assertRaisesRegex(RuntimeError, 'database-password-recover'):
                            guard(root)
                operation.recover(root)
                self.assertEqual(apply.call_args.args[1], replacement)
                self.assertFalse((root / operation.JOURNAL).exists())
                operation.recover(root)
                self.assertEqual(apply.call_count, 2)
            self.assertEqual(json.loads((root / 'installation.json').read_text()), metadata)
            self.assertEqual(
                (root / 'secrets/identity-sentinel').read_text(), 'FICTIONAL_UNRELATED_IDENTITY'
            )

    def test_partial_local_publication_recovers_both_files_before_services(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            real_write = state_store.write_text

            def fail_second(path, text, *args, **kwargs):
                if Path(path) == root / 'secrets/db_mas':
                    raise OSError('disk full')
                return real_write(path, text, *args, **kwargs)

            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(operation, 'apply'),
                patch.object(state_store, 'write_text', side_effect=fail_second),
            ):
                with self.assertRaises(OSError):
                    operation.rotate(root)
                lifecycle.generate.assert_not_called()
                self.assertTrue((root / operation.JOURNAL).exists())
                self.assertFalse(any(call.args[0] == 'up' for call in lifecycle.dc.call_args_list))
            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(operation, 'apply'),
            ):
                saved = operation.record(root)
                expected = operation.replacement(root, saved)
                operation.recover(root)
            for role in ('synapse', 'mas'):
                self.assertEqual(
                    (root / 'secrets' / ('db_' + role)).read_text().strip(), expected[role]
                )

    def test_live_writer_blocks_password_statement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(
                    lifecycle,
                    'service_rows',
                    return_value=[{'Service': 'mas', 'State': 'restarting'}],
                ),
                patch.object(operation, 'apply') as apply,
            ):
                with self.assertRaisesRegex(RuntimeError, 'did not stop'):
                    operation.rotate(root)
                apply.assert_not_called()
            self.assertTrue((root / operation.JOURNAL).exists())

    def test_destination_change_refused_before_alter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(operation, 'apply', side_effect=RuntimeError('lost response')),
            ):
                with self.assertRaises(RuntimeError):
                    operation.rotate(root)
                state_store.write_json(root / 'installation.json', {'domain': 'another.invalid'})
                with self.assertRaisesRegex(RuntimeError, 'destination'):
                    operation.recover(root)

    def test_sql_has_privacy_controls_ownership_assertion_and_single_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                self.context(root),
                patch.object(lifecycle, 'external_database', return_value=False),
            ):
                operation.apply(
                    root, {'synapse': 'Fictional_password_1', 'mas': 'Fictional_password_2'}
                )
                call = lifecycle.postgres.call_args
            sql = call.kwargs['stdin']
            self.assertLess(sql.index("SET log_statement='none'"), sql.index('BEGIN;'))
            self.assertLess(sql.index('RAISE EXCEPTION'), sql.index('ALTER ROLE'))
            self.assertEqual(sql.count('BEGIN;'), 1)
            self.assertEqual(sql.count('COMMIT;'), 1)
            self.assertEqual(sql.count('ALTER ROLE '), 2)
            self.assertNotIn('Fictional_password_1', repr(call.args))

    def test_private_checkpoint_rejects_symlink_hardlink_fifo_and_oversize(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.json'
            state_store.write_json(source, {'fictional': True})
            linked = root / 'symlink.json'
            linked.symlink_to(source)
            with self.assertRaises(OSError):
                operation.private_json(linked)
            hard = root / 'hard.json'
            os.link(source, hard)
            with self.assertRaises(RuntimeError):
                operation.private_json(hard)
            fifo = root / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(RuntimeError):
                operation.private_json(fifo)
            large = root / 'large'
            state_store.write_text(large, 'x' * 16385)
            with self.assertRaises(RuntimeError):
                operation.private_json(large)

    def test_new_passwords_pass_actual_generator_secret_validation(self):
        # Execute the generator's real function, including its canonical format
        # validation; preview generation deliberately bypasses secret files.
        source = Path(operation.__file__).with_name('generate.py').read_text()
        function = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == 'secret'
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(operation, 'apply'),
            ):
                operation.rotate(root)

            def fail(message):
                raise RuntimeError(message)

            namespace = {
                'args': SimpleNamespace(preview=False),
                'secret_dir': root / 'secrets',
                'state': {'seeded': True},
                'fail': fail,
                're': operation.re,
            }
            exec(
                compile(
                    ast.Module(body=[function], type_ignores=[]), 'actual-generate-secret', 'exec'
                ),
                namespace,
            )
            for role in ('synapse', 'mas'):
                self.assertEqual(
                    namespace['secret']('db_' + role),
                    (root / 'secrets' / ('db_' + role)).read_text().strip(),
                )

    def test_legacy_checkpoint_upgrade_is_persisted_before_sql_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                self.context(root),
                patch.object(operation.credentials, 'wait_services'),
                patch.object(operation, 'apply', side_effect=RuntimeError('lost COMMIT')),
            ):
                with self.assertRaises(RuntimeError):
                    operation.rotate(root)
                saved = operation.record(root)
                path = root / saved['stage'] / 'replacement.json'
                state_store.write_json(
                    path,
                    {
                        'synapse': 'FictionalLegacy_' + 'a' * 32,
                        'mas': 'FictionalLegacy_' + 'b' * 32,
                    },
                )
                with patch.object(
                    operation, 'apply', side_effect=[OSError('lost upgrade COMMIT'), None]
                ) as apply:
                    with self.assertRaises(OSError):
                        operation.recover(root)
                    first = apply.call_args.args[1]
                    self.assertEqual(json.loads(path.read_text()), first)
                    self.assertTrue(
                        all(
                            operation.re.fullmatch('[0-9a-f]{64}', value)
                            for value in first.values()
                        )
                    )
                    operation.recover(root)
                    self.assertEqual(apply.call_args.args[1], first)


if __name__ == '__main__':
    unittest.main()
