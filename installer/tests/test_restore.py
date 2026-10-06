"""Staged restore repeat must allow only its own verified latest-backup link."""
from pathlib import Path
import errno
import hashlib
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import restore


class RestoreResumeTests(unittest.TestCase):
    def test_restore_generation_admits_only_related_child_with_matching_archive_and_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'target';root.mkdir()
            digest='c'*64;store=restore.state_store
            record={'stage':'files_ready','archive_sha256':digest,'pid':os.getpid(),
                'started':store.process_identity(os.getpid())[1]}
            store.write_json(root/'restore-journal.json',record)
            bundle=str(Path(__file__).resolve().parents[1])
            code='import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import state_store;s=state_store.operation_guard(Path(sys.argv[2]));s.__enter__();state_store.assert_update_allowed(Path(sys.argv[2]),allow_restore_generation=True);print("worker_admitted")'
            with store.operation_guard(root),patch.dict(os.environ,{'MATRIX_INSTALLER_RESTORE_ARCHIVE':digest}):
                with self.assertRaisesRegex(RuntimeError,'incomplete'):store.assert_update_allowed(root)
                store.assert_update_allowed(root,allow_restore_generation=True)
                child=subprocess.run([sys.executable,'-c',code,bundle,str(root)],capture_output=True,text=True,**store.child_process_options())
                self.assertEqual(child.returncode,0,child.stderr)
                self.assertIn('worker_admitted',child.stdout)
                with patch.dict(os.environ,{'MATRIX_INSTALLER_RESTORE_ARCHIVE':'d'*64}):
                    with self.assertRaisesRegex(RuntimeError,'incomplete'):store.assert_update_allowed(root,allow_restore_generation=True)
                with patch.dict(os.environ,{'MATRIX_INSTALLER_OPERATION_FD':'-1'}):
                    with self.assertRaisesRegex(RuntimeError,'incomplete'):store.assert_update_allowed(root,allow_restore_generation=True)
                record.update(pid=99999999,started='not-live');store.write_json(root/'restore-journal.json',record)
                with self.assertRaisesRegex(RuntimeError,'incomplete'):store.assert_update_allowed(root,allow_restore_generation=True)

    def test_interrupted_external_extraction_publishes_only_staged_local_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder);root=parent/'target';fixture=parent/'source';fixture.mkdir()
            bundle=Path(__file__).resolve().parents[1]
            (fixture/'installation.json').write_text(json.dumps({'domain':'restore.example.org',
                'database_mode':'external','databases_initialized':True,'storage_committed':True}))
            (fixture/'settings.env').write_text('DOMAIN=restore.example.org\nPOSTGRES_MODE=external\nPOSTGRES_HOST=external.example.invalid\n')
            (fixture/'versions.json').write_bytes((bundle/'versions.json').read_bytes())
            (fixture/'config').mkdir()
            (fixture/'config/vault-state.json').write_text(json.dumps({'dirty':False,'version':1}))
            archive=parent/'archive.tar.gz';archive.write_bytes(b'fictional full archive')
            snapshot=parent/'20261003T141000Z-aaaaaa';snapshot.mkdir()
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            def extract(path,destination,**kwargs):
                if path!=archive:
                    for item in fixture.rglob('*'):
                        target=destination/item.relative_to(fixture)
                        if item.is_dir():target.mkdir(exist_ok=True)
                        else:shutil.copy2(item,target)
            def interrupted_copy(source,target,**kwargs):
                # Simulate death after the live metadata file was copied, before
                # any subsequent write in the destination.
                shutil.copy2(source/'installation.json',target/'installation.json')
                raise InterruptedError('fictional interrupted extraction')
            with patch.object(restore.backup_archive,'extract',side_effect=extract), \
                    patch.object(restore.backup_archive,'verify_snapshot',return_value=snapshot), \
                    patch.object(restore.update,'validate_manifest',return_value=([],None)), \
                    patch.object(restore.update,'release',return_value={'version':'1.2.33'}), \
                    patch.object(restore.update,'copy_sources'),patch.object(restore.referral,'verify_archive'), \
                    patch.object(restore.lifecycle,'ownership_check'),patch.object(restore.lifecycle,'generate') as generate, \
                    patch('federation_runtime.verify_archive'), \
                    patch.object(restore.shutil,'copytree',side_effect=interrupted_copy):
                with self.assertRaises(InterruptedError):restore._restore_locked(archive,root,digest,'203.0.113.20',True)
                generate.assert_not_called()
            target_metadata=json.loads((root/'installation.json').read_text())
            self.assertTrue(target_metadata['restore_pending'])
            self.assertEqual(target_metadata['database_mode'],'local')
            self.assertNotIn('databases_initialized',target_metadata)
            with self.assertRaisesRegex(RuntimeError,'restore-local'):
                restore.state_store.assert_update_allowed(root)
            self.assertEqual(json.loads((fixture/'installation.json').read_text())['database_mode'],'external')
            self.assertFalse(json.loads((fixture/'config/vault-state.json').read_text())['dirty'])

    def test_partial_restore_journal_blocks_mutations_even_with_live_archived_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);journal=root/'restore-journal.json'
            for stage in ('new','extracting','files_ready','importing'):
                journal.write_text(json.dumps({'stage':stage,'local_postgres':True}));journal.chmod(0o600)
                with self.subTest(stage=stage),self.assertRaisesRegex(RuntimeError,'restore-local'):
                    restore.state_store.assert_update_allowed(root)
            journal.write_text(json.dumps({'stage':'ready','local_postgres':True}))
            restore.state_store.assert_update_allowed(root)
            journal.write_text(json.dumps({'stage':'unknown'}))
            with self.assertRaisesRegex(RuntimeError,'Invalid restore'):restore.state_store.assert_update_allowed(root)
            journal.write_text(json.dumps({'stage':'ready'}));journal.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError,'private owned'):restore.state_store.assert_update_allowed(root)

    def test_external_restore_requires_explicit_local_destination(self):
        with self.assertRaisesRegex(RuntimeError,'restore-local ARCHIVE EMPTY_DIRECTORY'):
            restore.validate_database_destination({'database_mode':'external'},False)
        restore.validate_database_destination({'database_mode':'external'},True)
        restore.validate_database_destination({},False)
        with self.assertRaisesRegex(RuntimeError,'Unknown source'):
            restore.validate_database_destination({'database_mode':'unknown'},True)

    def test_changed_database_policy_refused_before_archive_extraction_or_data_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'target';root.mkdir()
            archive=Path(folder)/'archive.tar.gz';archive.write_bytes(b'fictional archive')
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            marker=root/'restore-journal.json'
            for saved,requested in ((True,False),(False,True)):
                with self.subTest(saved=saved,requested=requested):
                    marker.write_text(json.dumps({'archive_sha256':digest,'public_ip':'203.0.113.20',
                        'stage':'importing','local_postgres':saved}))
                    before=marker.read_bytes()
                    with patch.object(restore.backup_archive,'extract') as extract:
                        with self.assertRaisesRegex(RuntimeError,'different database policy'):
                            restore._restore_locked(archive,root,digest,'203.0.113.20',requested)
                        extract.assert_not_called()
                    self.assertEqual(marker.read_bytes(),before)
                    self.assertEqual(list(root.iterdir()),[marker])

    def test_external_archive_configs_render_locally_without_source_connector(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'target';bundle=Path(__file__).resolve().parents[1]
            command=[sys.executable,str(bundle/'generate.py'),'--directory',str(root),
                '--domain','restore.example.org','--preview']
            initial=subprocess.run(command+['--initialize-env','--database-mode','external'],capture_output=True,text=True)
            self.assertEqual(initial.returncode,0,initial.stderr)
            import configuration, configuration_schema, shutil, state_store
            values={key:item.default for key,item in configuration_schema.FIELDS.items()};values.update(configuration.load(root))
            shutil.rmtree(root/'settings')
            state_store.write_text(root/'settings.env',configuration.serialize(values))
            settings=root/'settings.env'
            text=settings.read_text().replace('POSTGRES_HOST=','POSTGRES_HOST=external.example.invalid').replace(
                'POSTGRES_ADMIN_PASSWORD=','POSTGRES_ADMIN_PASSWORD=FICTIONAL_ADMIN_PASSWORD')
            settings.write_text(text)
            metadata=json.loads((root/'installation.json').read_text())
            metadata.update(database_mode='external',storage_committed=True,restore_pending=True)
            connectors=('external-postgres-owner.json','postgres-admin.pgpass','postgres-ca.pem')
            for name in connectors:(root/'config'/name).write_text('fictional external connector')
            seeds={p.name:p.read_bytes() for p in (root/'secrets').iterdir() if p.is_file()}
            restore.prepare_local_database(root,metadata)
            (root/'installation.json').write_text(json.dumps(metadata))
            self.assertNotIn('FICTIONAL_ADMIN_PASSWORD',settings.read_text())
            self.assertNotIn('external.example.invalid',settings.read_text())
            self.assertEqual(metadata['database_mode'],'local')
            self.assertTrue(all(not (root/'config'/name).exists() for name in connectors))
            rendered=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(rendered.returncode,0,rendered.stderr)
            import yaml
            self.assertIn('postgres',yaml.safe_load((root/'compose.yaml').read_text())['services'])
            self.assertEqual(yaml.safe_load((root/'config/homeserver.yaml').read_text())['database']['args']['host'],'postgres')
            self.assertEqual({p.name:p.read_bytes() for p in (root/'secrets').iterdir() if p.is_file()},seeds)
            self.assertEqual(json.loads((root/'installation.json').read_text())['domain'],'restore.example.org')

    def test_verified_download_is_retained_for_restore_check_and_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder);root = parent / 'target';root.mkdir()
            archive = parent / 'download.tar.gz';archive.write_bytes(b'example verified archive')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            stamp = '20261003T005031Z-622153'
            restore.register_verified_archive(archive, root, digest, stamp)
            latest = root / 'backups/latest.tar.gz'
            self.assertFalse(latest.readlink().is_absolute())
            restored = latest.resolve();inode = restored.stat().st_ino
            archive.unlink()
            restore.register_verified_archive(archive, root, digest, stamp)
            self.assertEqual(restored.stat().st_ino, inode)
            self.assertEqual(restored.read_bytes(), b'example verified archive')
            restore.validate_resume_links(root)

    def test_bad_copy_and_conflicting_archive_never_publish_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder);root = parent / 'target';root.mkdir()
            archive = parent / 'download.tar.gz';archive.write_bytes(b'example verified archive')
            stamp = '20261003T005031Z-622153'
            with self.assertRaisesRegex(RuntimeError, 'copy failed verification'):
                restore.register_verified_archive(archive, root, '0' * 64, stamp)
            self.assertEqual(list((root / 'backups').iterdir()), [])
            with patch.object(restore.shutil, 'copyfileobj', side_effect=OSError(errno.ENOSPC, 'example full disk')):
                with self.assertRaisesRegex(RuntimeError, 'df -h'):
                    restore.register_verified_archive(archive, root, '0' * 64, stamp)
            self.assertEqual(list((root / 'backups').iterdir()), [])
            target = root / 'backups' / ('matrix-' + stamp + '.tar.gz')
            target.write_bytes(b'other archive');target.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'different checksum'):
                restore.register_verified_archive(archive, root, '0' * 64, stamp)
            self.assertEqual(target.read_bytes(), b'other archive')
            self.assertFalse((root / 'backups/latest.tar.gz').exists())
            latest = root / 'backups/latest.tar.gz';latest.write_bytes(b'preserve this file')
            with self.assertRaisesRegex(RuntimeError, 'owns latest'):
                restore.register_verified_archive(archive, root, '0' * 64, stamp)
            self.assertEqual(latest.read_bytes(), b'preserve this file')

    def test_link_interruption_is_resumable_and_foreign_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder);root = parent / 'target';root.mkdir()
            archive = parent / 'download.tar.gz';archive.write_bytes(b'example verified archive')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest();stamp = '20261003T005031Z-622153'
            with patch.object(Path, 'symlink_to', side_effect=InterruptedError('example interrupt')):
                with self.assertRaises(InterruptedError):
                    restore.register_verified_archive(archive, root, digest, stamp)
            restore.validate_resume_links(root)
            restore.register_verified_archive(archive, root, digest, stamp)
            restore.validate_resume_links(root)
            (root / 'backups').chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'private'):
                restore.register_verified_archive(archive, root, digest, stamp)

    def test_ready_target_with_ordinary_latest_backup_can_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backups = root / 'backups'
            backups.mkdir()
            archive = backups / 'matrix-20261003T005031Z-622153.tar.gz'
            archive.write_bytes(b'example archive')
            (backups / 'latest.tar.gz').symlink_to(archive.name)
            restore.validate_resume_links(root)

    def test_unknown_escaping_absolute_and_chained_links_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backups = root / 'backups'
            backups.mkdir()
            latest = backups / 'latest.tar.gz'
            for destination in ('../../outside', '/tmp/other', 'other-link'):
                with self.subTest(destination=destination):
                    latest.symlink_to(destination)
                    with self.assertRaises(RuntimeError):
                        restore.validate_resume_links(root)
                    latest.unlink()
            name = 'matrix-20261003T005031Z-622153.tar.gz'
            (backups / name).symlink_to('/tmp/other')
            latest.symlink_to(name)
            with self.assertRaises(RuntimeError):
                restore.validate_resume_links(root)
            latest.unlink()
            (root / 'unknown').symlink_to('/tmp/other')
            with self.assertRaises(RuntimeError):
                restore.validate_resume_links(root)
