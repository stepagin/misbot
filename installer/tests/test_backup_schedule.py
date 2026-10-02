"""Timer admission and rollback protect old schedules and persistent data."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_schedule as schedule
import state_store


class ScheduleTests(unittest.TestCase):
    def fixture(self, parent):
        root = parent / 'stack';root.mkdir()
        (root/'config').mkdir();(root/'stages').mkdir()
        units = parent/'units';units.mkdir()
        record={'format':1,'root':str(root),'unit':schedule.unit_name(root),
            'mode':'local','calendar':'*-*-* 03:00:00 UTC','settings':None,'key_file':None}
        return root,units,record

    def contexts(self, root, units):
        context=ExitStack()
        context.enter_context(patch.object(schedule,'UNIT_DIRECTORY',units))
        context.enter_context(patch.object(schedule.lifecycle,'ROOT',root,create=True))
        context.enter_context(patch.object(schedule.lifecycle,'state',return_value={
            'domain':'example.invalid','media_mode':'local','databases_initialized':True}))
        context.enter_context(patch.object(schedule.lifecycle,'emit'))
        return context

    def test_destination_failure_preserves_existing_timer_and_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder))
            state_store.write_json(root/'config/backup-schedule.json',dict(record,enabled=True))
            texts=schedule.unit_texts(root,record)
            for name,text in texts.items():(units/name).write_text(text)
            previous=(root/'config/backup-schedule.json').read_bytes()
            command=Mock(return_value=subprocess.CompletedProcess([],0,stdout=''))
            with self.contexts(root,units),patch.object(schedule,'command',command), \
                    patch.object(schedule,'snapshot',side_effect=RuntimeError('destination unavailable')):
                with self.assertRaisesRegex(RuntimeError,'destination unavailable'):
                    schedule.configure(root,dict(record,calendar='*-*-* 04:00:00 UTC'))
            self.assertEqual((root/'config/backup-schedule.json').read_bytes(),previous)
            self.assertEqual({name:(units/name).read_text() for name in texts},texts)
            self.assertFalse(any(call.args[0]=='systemctl' for call in command.call_args_list))

    def test_failed_enable_restores_old_unit_files_and_reenables_old_timer(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder))
            state_store.write_json(root/'config/backup-schedule.json',dict(record,enabled=True))
            previous=(root/'config/backup-schedule.json').read_bytes()
            texts=schedule.unit_texts(root,record)
            for name,text in texts.items():(units/name).write_text(text)
            attempts=[]
            def command(*args,**kwargs):
                if args[:2]==('systemctl','enable'):
                    attempts.append(args)
                    if len(attempts)==1:raise RuntimeError('enable failed EXAMPLE_DO_NOT_ECHO')
                return subprocess.CompletedProcess(args,0,stdout='')
            with self.contexts(root,units),patch.object(schedule,'command',side_effect=command), \
                    patch.object(schedule,'snapshot'):
                with self.assertRaises(RuntimeError) as caught:
                    schedule.configure(root,dict(record,calendar='*-*-* 04:00:00 UTC'))
            self.assertIn('Previous units and configuration restored',str(caught.exception))
            self.assertIn('backup-schedule-status',str(caught.exception))
            self.assertIn('admitted backup retained',str(caught.exception))
            self.assertNotIn('EXAMPLE_DO_NOT_ECHO',str(caught.exception))
            self.assertEqual((root/'config/backup-schedule.json').read_bytes(),previous)
            self.assertEqual({name:(units/name).read_text() for name in texts},texts)
            self.assertEqual(len(attempts),2)

    def test_foreign_unit_is_refused_before_backup_or_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder))
            target=units/(record['unit']+'.timer');target.write_text('# Foreign resource\n')
            with self.contexts(root,units),patch.object(schedule,'validate_calendar'), \
                    patch.object(schedule,'snapshot') as snapshot:
                with self.assertRaisesRegex(RuntimeError,'Another resource'):
                    schedule.configure(root,record)
                snapshot.assert_not_called()
            self.assertEqual(target.read_text(),'# Foreign resource\n')

    def test_timer_job_restores_both_dumps_and_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder));events=[]
            with self.contexts(root,units),patch.object(schedule,'validate_record'), \
                    patch.object(schedule.lifecycle,'ownership_check'), \
                    patch.object(schedule.lifecycle,'backup',side_effect=lambda:events.append('backup')), \
                    patch.object(schedule.lifecycle,'restore_check',side_effect=lambda:events.append('restore')):
                schedule.snapshot(root,record)
                self.assertEqual(events,['backup','restore'])
                verified=(root/'config/backup-schedule-verified.json').read_bytes()
                self.assertEqual((root/'config/backup-schedule-verified.json').stat().st_mode & 0o777,0o600)
                with patch.object(schedule.lifecycle,'restore_check',side_effect=RuntimeError('dump invalid')):
                    with self.assertRaisesRegex(RuntimeError,'dump invalid'):schedule.snapshot(root,record)
                self.assertEqual((root/'config/backup-schedule-verified.json').read_bytes(),verified)
                with patch.object(schedule.lifecycle,'state',return_value={'databases_initialized':True,'migration_frozen':True}):
                    with self.assertRaisesRegex(RuntimeError,'frozen'):schedule.snapshot(root,record)

    def test_calendar_injection_symlink_and_local_s3_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder))
            with patch.object(schedule,'command') as command:
                for calendar in ('daily\nUnit=foreign.service','%n',''):
                    with self.assertRaisesRegex(RuntimeError,'one systemd calendar'):schedule.validate_calendar(calendar)
                command.assert_not_called()
            alias=Path(folder)/'alias';alias.symlink_to(root)
            with self.assertRaisesRegex(RuntimeError,'symlinks'):schedule.safe_path(alias)
            with self.contexts(root,units),patch.object(schedule,'validate_calendar'), \
                    patch.object(schedule.lifecycle,'state',return_value={'media_mode':'s3'}):
                with self.assertRaisesRegex(RuntimeError,'independent backup S3'):schedule.validate_record(root,record)

    def test_moved_schedule_never_controls_source_units(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder))
            moved=dict(record,root='/opt/old-machine',unit='matrix-backup-source',enabled=True)
            state_store.write_json(root/'config/backup-schedule.json',moved)
            with self.contexts(root,units),patch.object(schedule,'command') as command, \
                    patch.object(schedule.lifecycle,'emit') as emit:
                schedule.status(root)
                self.assertTrue(emit.call_args.kwargs['needs_reconfigure'])
                schedule.disable(root)
                command.assert_not_called()
            saved=json.loads((root/'config/backup-schedule.json').read_text())
            self.assertFalse(saved['enabled']);self.assertEqual(saved['root'],str(root))
            self.assertEqual(saved['unit'],schedule.unit_name(root))

    def test_verified_result_survives_systemd_execution_metadata_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder))
            state_store.write_json(root/'config/backup-schedule.json',dict(record,enabled=False))
            state_store.write_json(root/'config/backup-schedule-verified.json',{
                'format':1,'root':str(root),'unit':schedule.unit_name(root),'mode':'local',
                'completed_utc':'2026-10-03T03:00:00Z','two_database_restores':True,
                'unexpected_secret':'EXAMPLE_DO_NOT_EMIT'})
            reset=subprocess.CompletedProcess([],0,stdout='ActiveState=inactive\nResult=success\nExecMainStatus=0\nExecMainStartTimestampMonotonic=0\n')
            with self.contexts(root,units),patch.object(schedule,'command',return_value=reset), \
                    patch.object(schedule.lifecycle,'emit') as emit:
                schedule.status(root)
            events=[call.kwargs for call in emit.call_args_list]
            self.assertEqual(events[0]['last_verified']['completed_utc'],'2026-10-03T03:00:00Z')
            self.assertFalse(events[0]['enabled'])
            self.assertFalse(events[-1]['execution_metadata_available'])
            self.assertNotIn('EXAMPLE_DO_NOT_EMIT',repr(events))

    def test_moved_or_unsafe_verification_cannot_confirm_target_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root,units,record=self.fixture(Path(folder))
            path=root/'config/backup-schedule-verified.json'
            state_store.write_json(path,{'format':1,'root':'/opt/old-host','unit':'old-unit',
                'mode':'local','completed_utc':'2026-10-03T03:00:00Z','two_database_restores':True})
            self.assertEqual(schedule.verified_status(root),{
                'last_verified':None,'verification_on_this_target_required':True})
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError,'private regular'):schedule.verified_status(root)
