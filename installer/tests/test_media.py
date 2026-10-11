"""A failed optional resource and interrupted adoption must leave a local recovery path."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import lifecycle
import media


class MediaOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'installation.json').write_text(json.dumps({'domain':'chat.example.org','media_mode':'local'}))
        (self.root/'settings.env').write_text('DOMAIN=chat.example.org\nMEDIA_MODE=local\n')
        lifecycle.configure(self.root,'media-test')
    def tearDown(self):self.temp.cleanup()
    def test_bad_s3_credentials_preserve_settings_and_hide_sdk_secret(self):
        settings=self.root/'private-s3.env'
        settings.write_text('S3_BUCKET=example-bucket\nS3_REGION=us-east-1\nS3_ACCESS_KEY_ID=FICTIONAL_ACCESS\nS3_SECRET_ACCESS_KEY=FICTIONAL_SECRET\nS3_PREFIX=media/example/\n')
        settings.chmod(0o600)
        previous=(self.root/'settings.env').read_bytes()
        with patch.object(media.media_transfer,'client',side_effect=RuntimeError('EXAMPLE_PRIVATE_SECRET')):
            with self.assertRaises(RuntimeError) as caught:media.s3(self.root,settings)
        self.assertNotIn('EXAMPLE_PRIVATE_SECRET',str(caught.exception))
        self.assertIn('current local storage is unchanged',str(caught.exception))
        self.assertEqual((self.root/'settings.env').read_bytes(),previous)

    def test_public_or_symlinked_s3_env_refused_before_connection_or_changes(self):
        settings=self.root/'public-s3.env'
        settings.write_text('S3_BUCKET=example-bucket\n');settings.chmod(0o644)
        previous=(self.root/'settings.env').read_bytes()
        with patch.object(media.media_transfer,'client') as client:
            with self.assertRaisesRegex(RuntimeError,'mode 0600'):
                media.s3(self.root,settings)
            settings.chmod(0o600)
            alias=self.root/'alias';alias.symlink_to(settings)
            with self.assertRaisesRegex(RuntimeError,'недоступен'):
                media.s3(self.root,alias)
        client.assert_not_called()
        self.assertEqual((self.root/'settings.env').read_bytes(),previous)
    def test_committed_s3_cannot_be_disabled_without_verifying_local_originals(self):
        (self.root/'installation.json').write_text(json.dumps({'domain':'chat.example.org','storage_committed':True,'media_mode':'s3'}))
        previous=(self.root/'settings.env').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'every local original'):media.local(self.root)
        self.assertEqual((self.root/'settings.env').read_bytes(),previous)
    def test_recovery_uses_saved_local_settings_without_contacting_s3(self):
        folder=self.root/'stages/media-transfers/fixture'
        folder.mkdir(parents=True)
        original={'domain':'chat.example.org','media_mode':'local','databases_initialized':True,'mas_issuer':'https://mas.chat.example.org/'}
        (folder/'installation.json').write_text(json.dumps(original))
        (folder/'settings.env').write_text('DOMAIN=chat.example.org\nMEDIA_MODE=local\n')
        (self.root/'installation.json').write_text(json.dumps({**original,'media_transition_pending':True,'media_transition_dir':'stages/media-transfers/fixture'}))
        with patch.object(media.lifecycle,'up') as up,patch.object(media.media_transfer,'client') as client:
            media.recover(self.root)
        up.assert_called_once()
        client.assert_not_called()
        self.assertEqual(json.loads((self.root/'installation.json').read_text()),original)
