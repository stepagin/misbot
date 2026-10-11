"""Real source inventories/checkpoints with fictional installation data.

Only service startup and the database backup subprocess are substituted; source
copy, snapshots, journal ownership, checksum validation and recovery are real.
"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import state_store
import update


def manifest(root, names):
    payload = ''.join(
        hashlib.sha256((root / name).read_bytes()).hexdigest() + '  ' + name + '\n'
        for name in sorted(names)
    )
    (root / 'SHA256SUMS').write_text(payload)
    return hashlib.sha256(payload.encode()).hexdigest()


class ReleaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'installed'
        self.root.mkdir()
        self.source = self.base / 'candidate'
        self.source.mkdir()
        pins = (Path(__file__).resolve().parents[1] / 'versions.json').read_bytes()
        for folder, version in ((self.root, '1.0.0'), (self.source, '1.0.1')):
            for name in update.ESSENTIAL:
                (folder / name).write_text('FICTIONAL_SOURCE_' + version)
            (folder / 'release.json').write_text(
                json.dumps(
                    {
                        'family': update.FAMILY,
                        'version': version,
                        'update_scope': 'source-configuration-and-approved-components',
                    }
                )
            )
            (folder / 'versions.json').write_bytes(pins)
        (self.root / 'old-developer-tool.py').write_text('FICTIONAL_PREVIOUS_ONLY_SOURCE')
        self.old_revision = manifest(self.root, update.ESSENTIAL | {'old-developer-tool.py'})
        self.new_revision = manifest(self.source, update.ESSENTIAL)
        self.metadata = {
            'domain': 'fictional.example',
            'config_format_version': 1,
            'databases_initialized': True,
            'applied_release_version': '1.0.0',
            'applied_release_revision': self.old_revision,
        }
        state_store.write_json(self.root / 'installation.json', self.metadata)
        for name in (
            'settings.env',
            'secrets/identity.key',
            'data/synapse/persistent',
            'data/mas/accounts',
            'data/synapse/media_store/attachment',
        ):
            target = self.root / name
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_text('FICTIONAL_PERSISTENT_' + name)
        self.persistent = {
            name: (self.root / name).read_bytes()
            for name in (
                'settings.env',
                'secrets/identity.key',
                'data/synapse/persistent',
                'data/mas/accounts',
                'data/synapse/media_store/attachment',
            )
        }

    def assert_persistent(self):
        self.assertEqual(
            self.persistent, {name: (self.root / name).read_bytes() for name in self.persistent}
        )

    def start(self, root, log):
        release = update.release(root)
        metadata = json.loads((root / 'installation.json').read_text())
        metadata.update(
            applied_release_version=release['version'],
            applied_release_revision=hashlib.sha256((root / 'SHA256SUMS').read_bytes()).hexdigest(),
        )
        state_store.write_json(root / 'installation.json', metadata)
        return 0

    def test_apply_and_repeat_preserve_persistent_data_and_backup_precedes_copy(self):
        events = []
        original = update.copy_sources

        def copy(entries, root):
            events.append('copy')
            return original(entries, root)

        with (
            patch.object(update, 'update_resource_check'),
            patch.object(
                update,
                'verified_backup',
                side_effect=lambda *args: events.append('verified_backup'),
            ),
            patch.object(update, 'copy_sources', side_effect=copy),
            patch.object(update, 'run_up', side_effect=self.start),
        ):
            update.perform_update(self.source, self.root, 'apply')
            update.perform_update(self.source, self.root, 'apply')
        self.assertEqual(events, ['verified_backup', 'copy', 'verified_backup', 'copy'])
        self.assertEqual(update.state(self.root)['domain'], 'fictional.example')
        self.assertEqual(update.state(self.root)['applied_release_version'], '1.0.1')
        self.assert_persistent()
        update.validate_manifest(self.root)

    def test_failed_copy_rolls_back_previous_only_files_and_complete_manifest(self):
        with (
            patch.object(update, 'update_resource_check'),
            patch.object(update, 'verified_backup'),
            patch.object(update, 'run_up', side_effect=[1, 0]),
        ):
            with self.assertRaisesRegex(RuntimeError, 'rollback healthy=True'):
                update.perform_update(self.source, self.root, 'apply')
        self.assertEqual(update.validate_manifest(self.root)[1], self.old_revision)
        self.assertEqual(update.state(self.root), self.metadata)
        self.assertFalse((self.root / 'update-journal.json').exists())
        self.assert_persistent()

    def test_interrupted_copy_recovers_previous_manifest_after_runtime_only_packaging(self):
        entries = update.validate_manifest(self.source)[0]
        saved = update.snapshot(self.root, entries, 'fictional-checkpoint')
        self.assertTrue((saved / 'old-developer-tool.py').is_file())
        # Simulate partial copy and missing old-only file; recovery must restore it.
        (self.root / 'install.sh').write_bytes((self.source / 'install.sh').read_bytes())
        (self.root / 'old-developer-tool.py').unlink()
        update.record_owner(
            self.root,
            {
                'format': 1,
                'stage': str(saved.relative_to(self.root)),
                'candidate_files': sorted(entries),
                'component_transition': None,
            },
        )
        with patch.object(update, 'run_up', side_effect=self.start):
            update.recover_update(self.root)
        self.assertEqual(update.validate_manifest(self.root)[1], self.old_revision)
        self.assertEqual(update.state(self.root)['applied_release_version'], '1.0.0')
        self.assert_persistent()

    def test_version_order_has_no_preproduct_exception(self):
        self.assertLess(update.version('1.0.0'), update.version('1.2.63'))
        metadata = dict(self.metadata, applied_release_version='1.2.63')
        state_store.write_json(self.root / 'installation.json', metadata)
        with (
            patch.object(update, 'verified_backup') as backup,
            patch.object(update, 'snapshot') as snapshot,
        ):
            with self.assertRaisesRegex(RuntimeError, 'Downgrade refused'):
                update.perform_update(self.source, self.root, 'apply')
            backup.assert_not_called()
            snapshot.assert_not_called()
        self.assert_persistent()

    def test_split_configuration_failure_and_recovery_preserve_all_areas(self):
        (self.root / 'settings.env').unlink()
        self.persistent.pop('settings.env')
        self.metadata['config_format_version'] = 2
        state_store.write_json(self.root / 'installation.json', self.metadata)
        for name in (
            'server.env',
            'policy.env',
            'media-s3.env',
            'backup.env',
            'auth.env',
            'database.env',
        ):
            target = self.root / 'settings' / name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            state_store.write_text(target, 'FICTIONAL_PRIVATE_' + name)
            self.persistent['settings/' + name] = target.read_bytes()
        entries = update.validate_manifest(self.source)[0]
        saved = update.snapshot(self.root, entries, 'split-checkpoint')
        for name in self.persistent:
            if name.startswith('settings/'):
                self.assertEqual((saved / name).read_bytes(), self.persistent[name])
        state_store.write_text(self.root / 'settings/auth.env', 'FICTIONAL_INTERRUPTED_VALUE')
        (self.root / 'install.sh').write_bytes((self.source / 'install.sh').read_bytes())
        update.record_owner(
            self.root,
            {
                'format': 1,
                'stage': str(saved.relative_to(self.root)),
                'candidate_files': sorted(entries),
                'component_transition': None,
            },
        )
        with patch.object(update, 'run_up', side_effect=self.start):
            update.recover_update(self.root)
        self.assert_persistent()
        self.assertEqual(update.state(self.root)['config_format_version'], 2)
        self.assertFalse((self.root / 'settings.env').exists())
