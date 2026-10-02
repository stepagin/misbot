"""Run the shell entrypoint without root, Docker or network side effects."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BUNDLE = Path(__file__).resolve().parents[1]


class UpAdmissionTests(unittest.TestCase):
    def test_s3_credentials_shell_routes_private_file_and_recovery_without_packages(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'installation';root.mkdir()
            (root/'installation.json').write_text('{"domain":"fictional.invalid"}')
            (root/'s3_credentials.py').write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
            private=Path(folder)/'input.env';private.write_text('S3_ACCESS_KEY_ID=FICTIONAL')
            commands=Path(folder)/'commands';commands.mkdir()
            (commands/'id').write_text('#!/bin/sh\nprintf "0\\n"\n');(commands/'id').chmod(0o700)
            for args,expected in (
                (['s3-credentials',str(private),str(root)],['--directory',str(root),'--settings',str(private),'rotate']),
                (['s3-credentials-recover',str(root)],['--directory',str(root),'recover']),
                (['s3-credentials-cleanup',str(root)],['--directory',str(root),'cleanup'])):
                with self.subTest(action=args[0]):
                    result=subprocess.run(['bash',str(BUNDLE/'install.sh'),*args],capture_output=True,text=True,
                        env={**os.environ,'PATH':str(commands)+':'+os.environ['PATH']},timeout=10)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(json.loads(result.stdout),expected)

    def test_down_shell_dispatch_needs_no_package_or_preflight(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'installation';root.mkdir()
            (root/'installation.json').write_text('{"domain":"fictional.invalid"}')
            (root/'lifecycle.py').write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
            commands=Path(folder)/'commands';commands.mkdir()
            (commands/'id').write_text('#!/bin/sh\nprintf "0\\n"\n');(commands/'id').chmod(0o700)
            result=subprocess.run(['bash',str(BUNDLE/'install.sh'),'down',str(root)],capture_output=True,text=True,
                env={**os.environ,'PATH':str(commands)+':'+os.environ['PATH']},timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),['--directory',str(root),'down'])

    def test_pending_operations_explain_recovery_before_preflight_or_packages(self):
        for flag, continuation in (
                ('restore_pending', 'restore-activate source-stopped'),
                ('migration_frozen', 'move-abort target-stopped'),
                ('credential_transition_pending', 'admin-password-recover'),
                ('media_transition_pending', 'media-recover'),
                ('s3_credential_recovery_pending', 's3-credentials-recover')):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                root = base / 'installation'
                root.mkdir()
                (root / 'installation.json').write_text(json.dumps({
                    'domain': 'fictional.invalid', 'public_ip': '203.0.113.20', flag: True}))
                if flag == 's3_credential_recovery_pending':
                    # Admission must refuse before opening even a malformed
                    # private recovery journal or invoking host side effects.
                    (root / 's3-credentials-transition.json').write_text('FICTIONAL_BAD_CHECKPOINT')
                marker = base / 'unexpected-side-effect'
                (root / 'preflight.py').write_text(
                    'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\nraise SystemExit(91)\n')
                commands = base / 'commands'
                commands.mkdir()
                (commands / 'id').write_text('#!/bin/sh\nprintf "0\\n"\n')
                for name in ('docker', 'systemctl', 'apt-get'):
                    (commands / name).write_text('#!/bin/sh\ntouch ' + str(marker) + '\nexit 92\n')
                # Only the lifecycle root admission is simulated. File ownership,
                # operation guards, shell ordering and actual admission code run.
                (commands / 'python3').write_text(
                    '#!' + sys.executable + '\n'
                    'import importlib.util,os,sys\n'
                    'if len(sys.argv)>1 and sys.argv[1]==' + repr(str(BUNDLE / 'lifecycle.py')) + ':\n'
                    ' sys.path.insert(0,' + repr(str(BUNDLE)) + ')\n'
                    ' spec=importlib.util.spec_from_file_location("tested_lifecycle",sys.argv[1])\n'
                    ' module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
                    ' class HostIdentity:\n'
                    '  def geteuid(self):return 0\n'
                    '  def __getattr__(self,name):return getattr(os,name)\n'
                    ' module.os=HostIdentity();sys.argv=sys.argv[1:];module.main()\n'
                    'else:os.execv(' + repr(sys.executable) + ',[' + repr(sys.executable) + ',*sys.argv[1:]])\n')
                for command in commands.iterdir():
                    command.chmod(0o700)
                result = subprocess.run(['bash', str(BUNDLE / 'install.sh'), 'up', str(root)],
                    capture_output=True, text=True, timeout=20,
                    env={**os.environ, 'PATH': str(commands) + ':' + os.environ['PATH']})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(continuation, result.stdout + result.stderr)
                self.assertFalse(marker.exists(), result.stdout + result.stderr)
                self.assertEqual(json.loads((root / 'installation.json').read_text())[flag], True)


if __name__ == '__main__':
    unittest.main()
