"""Backup encryption must reject tampering before publishing plaintext."""

import builtins
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_crypto


class BackupCryptoTests(unittest.TestCase):
    def test_missing_encryption_package_never_publishes_an_unencrypted_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.write_bytes(b'Fictional private archive data')
            key = b'X' * 32
            backup_crypto.encrypt(source, root / 'encrypted', key)
            original_import = builtins.__import__

            def restricted_import(name, *args, **kwargs):
                if name.startswith('cryptography'):
                    raise ModuleNotFoundError('Encryption unavailable', name=name)
                return original_import(name, *args, **kwargs)

            with patch('builtins.__import__', side_effect=restricted_import):
                for name, operation in (
                    ('new-backup', backup_crypto.encrypt),
                    ('plaintext', backup_crypto.decrypt),
                ):
                    incoming = source if name == 'new-backup' else root / 'encrypted'
                    with self.subTest(operation=name):
                        with self.assertRaises(ModuleNotFoundError):
                            operation(incoming, root / name, key)
                        self.assertFalse((root / name).exists())
            self.assertEqual({path.name for path in root.iterdir()}, {'source', 'encrypted'})
            self.assertEqual(source.read_bytes(), b'Fictional private archive data')

    def test_multiframe_round_trip_and_fresh_archive_salt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backup_crypto.create_key(root / 'key')
            key = backup_crypto.read_key(root / 'key')
            original = b'Example public archive data.' * 100000
            (root / 'input').write_bytes(original)
            backup_crypto.encrypt(root / 'input', root / 'encrypted', key)
            backup_crypto.encrypt(root / 'input', root / 'encrypted2', key)
            self.assertNotEqual(
                (root / 'encrypted').read_bytes(), (root / 'encrypted2').read_bytes()
            )
            backup_crypto.decrypt(root / 'encrypted', root / 'restored', key)
            self.assertEqual((root / 'restored').read_bytes(), original)
            self.assertEqual((root / 'restored').stat().st_mode & 0o777, 0o600)

    def test_wrong_key_tampering_truncation_and_appended_data_publish_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = b'X' * 32
            (root / 'input').write_bytes(b'Example archive.' * 100000)
            backup_crypto.encrypt(root / 'input', root / 'encrypted', key)
            data = (root / 'encrypted').read_bytes()
            tampered = bytearray(data)
            tampered[100] ^= 1
            cases = [
                (data, b'Y' * 32),
                (bytes(tampered), key),
                (data[:-20], key),
                (data + b'garbage', key),
            ]
            for index, (payload, candidate_key) in enumerate(cases):
                with self.subTest(index=index):
                    (root / 'bad').write_bytes(payload)
                    with self.assertRaises(Exception):
                        backup_crypto.decrypt(root / 'bad', root / 'restored', candidate_key)
                    self.assertFalse((root / 'restored').exists())
                    self.assertFalse(list(root.glob('.matrix-private-*')))

    def test_existing_key_or_archive_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'existing').write_bytes(b'EXAMPLE_EXISTING')
            with self.assertRaises(RuntimeError):
                backup_crypto.create_key(root / 'existing')
            self.assertEqual((root / 'existing').read_bytes(), b'EXAMPLE_EXISTING')

    def test_public_key_permissions_and_symlink_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backup_crypto.create_key(root / 'key')
            (root / 'key').chmod(0o644)
            with self.assertRaises(RuntimeError):
                backup_crypto.read_key(root / 'key')
            (root / 'linked').symlink_to(root / 'key')
            with self.assertRaises(RuntimeError):
                backup_crypto.read_key(root / 'linked')
