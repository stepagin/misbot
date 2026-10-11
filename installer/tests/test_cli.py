"""CLI routes argv and owns only its own system command; no Docker required."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cli_commands
import command_wrapper as wrapper
import misbot

BUNDLE=Path(__file__).resolve().parents[1]


class CommandTests(unittest.TestCase):
    def installation(self,root):
        root.mkdir(mode=0o700)
        for name in wrapper.PUBLIC_FILES:
            (root/name).write_bytes((BUNDLE/name).read_bytes())
        for name in ('install.sh','generate.py','lifecycle.py','s3_diagnostics.py'):
            (root/name).write_text('# fictional backend\n')
        (root/'installation.json').write_text(json.dumps({'domain':'fictional.invalid','config_format_version':1}))
        return root

    def test_help_version_without_root_docker_or_secrets(self):
        for argv in ([],['help'],['storage','help'],['storage','s3','check','--help'],['version'],
                ['ui','--help'],['config','validate','--help'],['users','invitation','show','--help'],
                ['federation','help'],['federation','requests','approve','--help'],['federation','operations','--help'],
                ['homepage','help'],['homepage','apply','--help']):
            output=io.StringIO()
            with patch.object(os,'geteuid',return_value=1000),patch.object(wrapper,'default_directory',side_effect=AssertionError('secret read')),contextlib.redirect_stdout(output):
                self.assertEqual(misbot.main(argv),0)
            self.assertTrue(output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as version:
            self.assertEqual(misbot.main(['version']),0)
        self.assertIn('1.1.1',version.getvalue())

    def test_split_installation_routes_configuration_invitation_and_ui_without_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.installation(Path(folder)/'Matrix Test')
            (root/'installation.json').write_text(json.dumps({'domain':'fictional.invalid','config_format_version':2}))
            for operation,script,action in (('config validate','configuration.py','validate'),
                    ('config recover','configuration.py','recover'),('users invitation ensure','registration.py','ensure'),
                    ('credentials admin show','registration.py','admin-show')):
                cmd,opts,values=misbot.parse([*operation.split(),'--directory',str(root)])
                self.assertEqual(misbot.plan(cmd,opts,values,source=BUNDLE),
                    ['/usr/bin/python3',str(root/script),'--directory',str(root),action])
            cmd,opts,values=misbot.parse(['ui','--directory',str(root)])
            self.assertEqual(misbot.plan(cmd,opts,values,source=BUNDLE),
                ['/usr/bin/python3',str(BUNDLE/'terminal_ui.py'),'--directory',str(root)])

    def test_actual_help_from_other_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            for args in (['version'],['storage','s3','check','--config','/does/not/exist','--help']):
                result=subprocess.run([sys.executable,str(BUNDLE/'misbot.py'),*args],cwd=folder,capture_output=True,text=True,env={'PATH':'/nonexistent'})
                self.assertEqual(result.returncode,0,result.stderr)

    def test_homepage_opaque_arguments_spaces_and_metacharacters(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.installation(Path(folder)/'Matrix Test')
            description='Fictional description;$(touch /tmp/SHOULD_NOT_EXIST)'
            cmd,opts,values=misbot.parse(['homepage','apply','--directory',str(root),'--mode','landing',
                '--name','Team with spaces','--description',description,'--support-url',''])
            args=misbot.plan(cmd,opts,values,source=BUNDLE)
            self.assertEqual(args[:5],['/usr/bin/python3',str(root/'homepage.py'),'--directory',str(root),'apply'])
            self.assertIn(description,args);self.assertIn('Team with spaces',args);self.assertEqual(args[-1],'')

    def test_federation_uses_shared_backend_and_preserves_opaque_room_argument(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.installation(Path(folder)/'Matrix Test')
            room="!RoomCase:fictional.invalid;$(fictional)"
            cmd,opts,values=misbot.parse(['federation','rooms','invite','--server','peer.example',
                '--room',room,'--directory',str(root)])
            args=misbot.plan(cmd,opts,values,source=BUNDLE)
            self.assertEqual(args[:-1],['/usr/bin/python3',str(root/'federation.py'),'--directory',str(root),'room-invite'])
            self.assertEqual(json.loads(args[-1]),{'room':room,'server':'peer.example'})

    def test_privileges_non_tty_and_missing_sudo(self):
        for sudo in ('/usr/bin/sudo',None):
            output=io.StringIO()
            with patch.object(os,'geteuid',return_value=1000),patch.object(sys.stdin,'isatty',return_value=False),patch.object(misbot.shutil,'which',return_value=sudo),contextlib.redirect_stderr(output):
                self.assertEqual(misbot.main(['status']),77)
            self.assertIn('sudo -n' if sudo else 'sudo отсутствует',output.getvalue())

    def test_bad_input_is_safe(self):
        for args in (['fictional'],['status','--unknown','FICTIONAL_SECRET'],['storage','s3','check','--config','x','--purpose','invalid']):
            output=io.StringIO()
            with contextlib.redirect_stderr(output):self.assertEqual(misbot.main(args),2)
            self.assertNotIn('FICTIONAL_SECRET',output.getvalue())

    def test_missing_required_arguments_name_options_and_operation_help(self):
        for command in cli_commands.COMMANDS:
            if not command.arguments:continue
            with self.subTest(command=command.path):
                output=io.StringIO()
                with patch.object(os,'geteuid',side_effect=AssertionError('must validate before privileges')),contextlib.redirect_stderr(output):
                    self.assertEqual(misbot.main(command.path.split()),2)
                for argument in command.arguments:self.assertIn('--'+argument,output.getvalue())
                self.assertIn('Справка: misbot '+command.path+' --help',output.getvalue())

    def test_invalid_values_and_missing_option_value_are_safe_and_actionable(self):
        cases=[(['federation','check','--server'],'Для аргумента --server требуется значение.','misbot federation check --help'),
               (['--directory'],'Для аргумента --directory требуется значение.','misbot --help'),
               (['storage','s3','check','--config','FICTIONAL_PRIVATE_PATH','--purpose','FICTIONAL_SECRET'],
                'Допустимо: media, backup.','misbot storage s3 check --help'),
               (['status','--FICTIONAL_SECRET','FICTIONAL_PRIVATE_PATH'],
                'Указан неизвестный аргумент','misbot status --help')]
        for argv,message,help_command in cases:
            with self.subTest(argv=argv):
                output=io.StringIO()
                with contextlib.redirect_stderr(output):self.assertEqual(misbot.main(argv),2)
                self.assertIn(message,output.getvalue());self.assertIn(help_command,output.getvalue())
                self.assertNotIn('FICTIONAL_SECRET',output.getvalue())
                self.assertNotIn('FICTIONAL_PRIVATE_PATH',output.getvalue())

    def test_bootstrap_s3_json_before_any_installation(self):
        if os.geteuid()!=0:self.skipTest('isolated root CLI requires root')
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)/'private config.env'
            profile.write_text('S3_BUCKET=\nS3_SECRET_ACCESS_KEY=FICTIONAL_SECRET\n');profile.chmod(0o600)
            result=subprocess.run([sys.executable,str(BUNDLE/'misbot.py'),'storage','s3','check',
                '--config',str(profile),'--purpose','media','--json'],cwd=folder,capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertEqual(json.loads(result.stdout)['stages'][0]['code'],'bucket_required')
            self.assertNotIn('FICTIONAL_SECRET',result.stdout+result.stderr)

    def test_named_arguments_are_not_shell_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.installation(Path(folder)/'Matrix Test')
            dangerous="/root/file with spaces; $(touch /tmp/fictional-injection) 'quoted'"
            cmd,opts,values=misbot.parse(['storage','s3','connect','--config',dangerous,'--directory',str(root)])
            args=misbot.plan(cmd,opts,values,source=BUNDLE)
            self.assertEqual(args,['/bin/bash',str(root/'install.sh'),'media-s3',dangerous,str(root)])
            self.assertFalse(Path('/tmp/fictional-injection').exists())

    def test_missing_foreign_and_ambiguous_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            cmd,opts,values=misbot.parse(['status','--directory',str(root)])
            with self.assertRaises((RuntimeError,OSError)):misbot.plan(cmd,opts,values,source=BUNDLE)
        with patch.object(wrapper,'default_directory',return_value=None):
            cmd,opts,values=misbot.parse(['status'])
            with self.assertRaises(misbot.InputError):misbot.plan(cmd,opts,values,source=BUNDLE)

    def test_wrapper_install_repeat_update_remove_keeps_data(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=self.installation(base/'Matrix Test');system=base/'system'
            (root/'data').mkdir();(root/'data/identity').write_text('FICTIONAL_KEEP')
            previous_umask=os.umask(0o077)
            try:wrapper.install(root,system=system);wrapper.install(root,system=system)
            finally:os.umask(previous_umask)
            launcher,lib=wrapper.paths(system)
            result=subprocess.run([str(launcher),'version'],cwd='/tmp',capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('1.1.1',result.stdout)
            self.assertFalse((lib/'__pycache__').exists())
            if os.geteuid()==0:
                base.chmod(0o755);system.chmod(0o755)
                # The private backend stays inaccessible to nobody. Public
                # help/version must still work and must not contact Docker.
                for arguments in (['help'],['version']):
                    result=subprocess.run(['/usr/sbin/runuser','-u','nobody','--',str(launcher),*arguments],cwd='/tmp',capture_output=True,text=True)
                    self.assertEqual(result.returncode,0,result.stderr)
            (root/'misbot.py').write_bytes((root/'misbot.py').read_bytes()+b'\n# fictional source revision\n')
            wrapper.install(root,system=system)
            self.assertEqual((root/'misbot.py').read_bytes(),(lib/'misbot.py').read_bytes())
            self.assertTrue(wrapper.remove(root,system=system))
            self.assertFalse(wrapper.remove(root,system=system))
            self.assertEqual((root/'data/identity').read_text(),'FICTIONAL_KEEP')

    def test_foreign_command_and_alternate_default_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=self.installation(base/'one');other=self.installation(base/'two');system=base/'system'
            launcher,lib=wrapper.paths(system);launcher.parent.mkdir(parents=True)
            launcher.write_text('FICTIONAL_FOREIGN')
            with self.assertRaisesRegex(RuntimeError,'foreign'):wrapper.install(root,system=system)
            self.assertEqual(launcher.read_text(),'FICTIONAL_FOREIGN');launcher.unlink()
            wrapper.install(root,system=system)
            with self.assertRaisesRegex(RuntimeError,'Another installation'):wrapper.install(other,system=system)
            with self.assertRaisesRegex(RuntimeError,'does not own'):wrapper.remove(other,system=system)
            (lib/'misbot.py').write_text('FICTIONAL_FOREIGN')
            with self.assertRaisesRegex(RuntimeError,'modified'):wrapper.remove(root,system=system)
            self.assertTrue(launcher.exists())

    def test_registration_failure_rolls_back_public_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=self.installation(base/'one');system=base/'system'
            wrapper.install(root,system=system);original=wrapper.record(system)
            real=wrapper.atomic;calls=0
            def fail_once(*args):
                nonlocal calls
                calls+=1
                if calls==3:raise OSError('fictional disk failure')
                return real(*args)
            with patch.object(wrapper,'atomic',side_effect=fail_once):
                with self.assertRaises(OSError):wrapper.install(root,system=system)
            self.assertEqual(wrapper.record(system),original)

    def test_exec_retains_backend_exit_and_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.installation(Path(folder)/'Matrix Test')
            if os.geteuid()!=0:self.skipTest('actual process routing requires isolated root')
            for script,code in (('exit 23\n',23),('kill -TERM $$\n',-signal.SIGTERM)):
                (root/'install.sh').write_text(script)
                result=subprocess.run([sys.executable,str(BUNDLE/'misbot.py'),'--directory',str(root),'status'],capture_output=True,text=True)
                self.assertEqual(result.returncode,code,result.stderr)

    def test_legacy_restore_position_and_recovery_vocabulary(self):
        cmd=cli_commands.BY_PATH['backup restore']
        values={'archive':'/root/fictional archive','sha256':'0'*64,'public-ip':'203.0.113.20'}
        self.assertEqual(cli_commands.backend_argv(cmd,values,Path('/opt/Matrix Test')),
            ['restore',values['archive'],'/opt/Matrix Test',values['sha256'],values['public-ip']])
        self.assertIn("--directory '/opt/Matrix Test' update recover",cli_commands.recovery_command('update-recover',Path('/opt/Matrix Test')))

    def test_public_runtime_routes_bootstrap_operations_to_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=self.installation(base/'backend');system=base/'system'
            wrapper.install(root,system=system);_,lib=wrapper.paths(system)
            with patch.object(wrapper,'default_directory',return_value=root):
                for argv in (['backup','key','create','--key','/root/new key'],
                        ['backup','restore','--archive','/root/backup','--sha256','0'*64,'--public-ip','203.0.113.20','--directory',str(base/'empty target')]):
                    cmd,opts,values=misbot.parse(argv)
                    result=misbot.plan(cmd,opts,values,source=lib)
                    self.assertEqual(result[1],str(root/'install.sh'))

    def test_wrapper_hooks_follow_successful_lifecycle_only(self):
        import lifecycle,state_store
        with tempfile.TemporaryDirectory() as folder:
            root=self.installation(Path(folder)/'Matrix Test')
            for action,flag in (('up','--register-command'),('down','--remove-command')):
                for failure in (False,True):
                    with patch.object(sys,'argv',['lifecycle.py','--directory',str(root),action,flag]), \
                            patch.object(os,'geteuid',return_value=0), \
                            patch.object(state_store,'operation_guard',return_value=contextlib.nullcontext()), \
                            patch.object(state_store,'assert_update_allowed'),patch.object(lifecycle,'ownership_check'), \
                            patch.object(lifecycle,action,side_effect=RuntimeError('fictional failure') if failure else None), \
                            patch.object(lifecycle.subprocess,'run') as register,patch.object(lifecycle,'emit'):
                        if failure:
                            with self.assertRaises(SystemExit):lifecycle.main()
                            register.assert_not_called()
                        else:
                            lifecycle.main();register.assert_called_once()
                            self.assertIn('install' if action=='up' else 'remove',register.call_args.args[0])


if __name__=='__main__':unittest.main()
