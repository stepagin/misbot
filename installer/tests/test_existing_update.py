"""Public-only adopted updates reuse a verified copy and recover failed writes."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import existing_update as update
from existing_compose import digest


class ExistingUpdateTests(unittest.TestCase):
    def test_current_sources_still_apply_declared_data_migrations_after_verified_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); root=base/'installation'; root.mkdir()
            control=root/'.misbot'; control.mkdir()
            source=base/'source'; source.mkdir()
            candidate={'family':update.update.FAMILY,'version':'1.1.0','data_schema_version':1,
                       'update_scope':'source-configuration-and-approved-components'}
            (source/'release.json').write_text(json.dumps(candidate))
            (root/'SHA256SUMS').write_text('unchanged manifest\n')
            (source/'SHA256SUMS').write_text('unchanged manifest\n')
            backup=base/'backups'/'accepted'; backup.mkdir(parents=True)
            images={'fixture':{'id':'sha256:'+'a'*64}}
            (backup/'manifest.json').write_text(json.dumps({'server_name':'fictional.example.org',
                'images':images,'encrypted_archive_sha256':'b'*64}))
            (control/'last-backup.json').write_text(json.dumps({'directory':str(backup),
                'sha256':'b'*64,'remote':{'verified':True}}))
            installation=SimpleNamespace(root=root,control=control,
                state={'domain':'fictional.example.org'},
                model={'backup_directory':str(base/'backups'),'images':images},
                require_no_pending=Mock(),validate=Mock(),backup=Mock(),verify=Mock())
            def private(path):
                path.mkdir(mode=0o700,parents=True,exist_ok=True); return path
            with (patch.object(update,'sources',return_value=({},'same-revision')),
                  patch.object(update.update,'validate_manifest',return_value=({},'same-revision')),
                  patch.object(update.update,'run_data_migrations') as migrate,
                  patch.object(update,'private_directory',side_effect=private),
                  patch.object(update.command_wrapper,'install')):
                update.apply(installation,source)
            self.assertEqual([call.args[3] for call in migrate.call_args_list],['check','apply'])
            self.assertTrue(migrate.call_args.args[4].is_relative_to(control/'source-updates'))
            installation.backup.assert_not_called()

    def test_failed_public_update_restores_previous_files_without_new_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);root=base/'installation';root.mkdir()
            control=root/'.misbot';control.mkdir()
            source=base/'source';source.mkdir()
            (source/'release.json').write_text(json.dumps({'family':update.update.FAMILY,
                'version':'1.1.0','update_scope':'source-configuration-and-approved-components'}))
            backup=base/'backups'/'accepted';backup.mkdir(parents=True)
            images={'fixture':{'id':'sha256:'+'a'*64}}
            manifest={'server_name':'fictional.example.org','images':images,
                      'encrypted_archive_sha256':'b'*64}
            (backup/'manifest.json').write_text(json.dumps(manifest))
            (control/'last-backup.json').write_text(json.dumps({'directory':str(backup),
                'sha256':'b'*64,'remote':{'verified':True}}))
            (root/'existing_compose.py').write_bytes(b'previous public source\n')
            (root/'SHA256SUMS').write_bytes(b'previous manifest\n')
            entries={}
            for name in ('existing_compose.py','new_public.py','SHA256SUMS'):
                path=source/name;path.write_bytes(b'candidate public source\n')
                entries[name]=path
            installation=SimpleNamespace(root=root,control=control,
                state={'domain':'fictional.example.org'},
                model={'backup_directory':str(base/'backups'),'images':images},
                require_no_pending=Mock(),validate=Mock(),backup=Mock(),verify=Mock())
            validation_calls=[]
            def validate_manifest(path):
                validation_calls.append(path)
                if len(validation_calls)==2:raise RuntimeError('fictional failed validation')
                return {},'fictional-revision'
            def private(path):
                path.mkdir(mode=0o700,parents=True,exist_ok=True);return path
            with (patch.object(update,'sources',return_value=(entries,'candidate')),
                 patch.object(update.update,'validate_manifest',side_effect=validate_manifest),
                 patch.object(update,'private_directory',side_effect=private),
                 patch.object(update.command_wrapper,'regular',return_value=SimpleNamespace(st_uid=0,st_mode=0o644)),
                 patch.object(update.command_wrapper,'install')):
                with self.assertRaisesRegex(RuntimeError,'fictional failed'):
                    update.apply(installation,source)
            self.assertEqual((root/'existing_compose.py').read_bytes(),b'previous public source\n')
            self.assertEqual((root/'SHA256SUMS').read_bytes(),b'previous manifest\n')
            self.assertFalse((root/'new_public.py').exists())
            self.assertFalse((control/'source-update.json').exists())
            installation.backup.assert_not_called()
            installation.verify.assert_not_called()

    def test_recovery_preserves_a_changed_new_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);control=root/'.misbot';control.mkdir()
            stage=control/'source-updates'/'fixture';stage.mkdir(parents=True)
            path=root/'new_public.py';path.write_bytes(b'changed after failure')
            (stage/'files.json').write_text('{}')
            (control/'source-update.json').write_text(json.dumps({'checkpoint':str(stage),
                'files':{'new_public.py':'a'*64}}))
            installation=SimpleNamespace(root=root,control=control)
            with self.assertRaisesRegex(update.Error,'изменён'):
                update.restore(installation)
            self.assertEqual(path.read_bytes(),b'changed after failure')
            self.assertTrue((control/'source-update.json').exists())


if __name__=='__main__':unittest.main()
