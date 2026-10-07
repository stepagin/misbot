"""A real Linux PTY with fictional backend; no live Matrix claim."""
import io,json,os
from pathlib import Path
import pty,select,signal,subprocess,sys,tempfile,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import terminal_ui as ui
import configuration_schema as schema


class UITests(unittest.TestCase):
    def test_site_form_uses_common_schema_and_confirmation_preserves_on_cancel(self):
        import configuration,homepage,state_store
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);root.chmod(0o700)
            state_store.write_json(root/'installation.json',{'domain':'fictional.example.org','config_format_version':2})
            configuration.write_model(root,{'DOMAIN':'fictional.example.org'})
            before=configuration.load(root,True)
            entries=iter(['SITE_NAME','Fictional new name','применить','нет'])
            interface=ui.UI(root,draft=root/'draft.json',reader=lambda *args:next(entries))
            with patch('sys.stdout',new=io.StringIO()),patch.object(homepage.Backend,'apply',side_effect=AssertionError('must not apply')):interface.homepage_menu()
            self.assertEqual(configuration.load(root,True),before)
            self.assertEqual(json.loads((root/'draft.json').read_text())['values']['SITE_NAME'],'Fictional new name')

    def test_field_error_and_secret_empty_input_keep_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            values=iter(['wrong://domain','example.org',''])
            interface=ui.UI(directory=Path(folder)/'target',draft=Path(folder)/'draft.json',reader=lambda *args:next(values))
            with patch('sys.stdout',new=io.StringIO()):
                interface.field('DOMAIN')
                interface.model['GOOGLE_CLIENT_SECRET']='FICTIONAL_KEEP'
                interface.field('GOOGLE_CLIENT_SECRET')
            self.assertEqual(interface.model['DOMAIN'],'example.org')
            self.assertEqual(interface.model['GOOGLE_CLIENT_SECRET'],'FICTIONAL_KEEP')

    def test_non_tty_does_not_wait(self):
        result=subprocess.run([sys.executable,str(Path(ui.__file__))],input='',capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertIn('ssh -t',result.stderr)

    def test_real_small_dumb_pty_local_wizard_with_back_and_hidden_input(self):
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'worker.py';target=Path(folder)/'Matrix Test';draft=Path(folder)/'draft.json'
            script.write_text('''import sys,json,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import terminal_ui,preflight,state_store,shutil
shutil.which=lambda name:'/usr/bin/'+name
for name in ('system_check','dns_check','caa_check','network_check'):setattr(preflight,name,lambda *a:None)
def run(args):
    root=Path(sys.argv[2]);root.mkdir(mode=0o700,exist_ok=True)
    state_store.write_json(root/'installation.json',{'domain':'example.org','databases_initialized':False})
    return 0
terminal_ui.UI.finish=lambda self:print('FICTIONAL_FINISHED')
interface=terminal_ui.UI(directory=sys.argv[2],draft=sys.argv[3],runner=run)
raise SystemExit(interface.wizard())
''')
            master,slave=pty.openpty()
            import fcntl,struct,termios
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',12,40,0,0))
            process=subprocess.Popen([sys.executable,str(script),str(Path(ui.__file__).parent),str(target),str(draft)],stdin=slave,stdout=slave,stderr=slave,start_new_session=True,env={**os.environ,'TERM':'dumb','NO_COLOR':'1'})
            os.close(slave);output=b''
            def send_when(marker,value):
                nonlocal output
                deadline=time.monotonic()+8
                while marker.encode() not in output:
                    if time.monotonic()>deadline:raise AssertionError(output.decode(errors='replace'))
                    if select.select([master],[],[],.1)[0]:output+=os.read(master,65536)
                output=output.replace(marker.encode(),b'consumed',1)
                # Resize while the form is active; line-oriented fallback needs no redraw.
                fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',8,24,0,0))
                os.write(master,(value+'\n').encode())
            try:
                for marker,value in [('DOMAIN:','bad://name'),('DOMAIN:','example.org'),('абсолютный путь',str(target)),
                        ('PUBLIC_IP','8.8.8.8'),('ACME_EMAIL:',''),('MEDIA_MODE [local]:',''),
                        ('Подключить GOOGLE?','да'),('GOOGLE_CLIENT_ID:','FICTIONAL_ID'),('GOOGLE_CLIENT_SECRET:','FICTIONAL_HIDDEN_SECRET'),
                        ('Подключить TELEGRAM?','нет'),('INVITATION_LIFETIME_SECONDS [604800]:',''),
                        ('Настроить внешние копии','отложить'),('применить. Назад','назад'),
                        ('Настроить внешние копии','отложить'),('применить. Назад','применить')]:send_when(marker,value)
                process.wait(timeout=10)
                while select.select([master],[],[],.1)[0]:
                    try:output+=os.read(master,65536)
                    except OSError:break
                self.assertEqual(process.returncode,0,output.decode(errors='replace'))
                self.assertIn(b'FICTIONAL_FINISHED',output)
                self.assertNotIn(b'FICTIONAL_HIDDEN_SECRET',output)
                self.assertNotIn(b'\x1b[',output)
                self.assertEqual(json.loads(draft.read_text())['values']['GOOGLE_CLIENT_SECRET'],'FICTIONAL_HIDDEN_SECRET')
            finally:
                if process.poll() is None:process.kill();process.wait()
                os.close(master)

    def test_real_pty_ctrl_c_keeps_private_draft_and_resumes_field(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'Matrix Test';draft=Path(folder)/'draft.json'
            script=Path(folder)/'interrupt.py'
            script.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import terminal_ui
interface=terminal_ui.UI(directory=sys.argv[2],draft=sys.argv[3])
raise SystemExit(interface.wizard())
''')
            transcript=b''
            for attempt in range(2):
                master,slave=pty.openpty()
                child=subprocess.Popen([sys.executable,str(script),str(Path(ui.__file__).parent),str(target),str(draft)],
                    stdin=slave,stdout=slave,stderr=slave,start_new_session=True,env={**os.environ,'TERM':'dumb'})
                os.close(slave);output=b''
                def until(marker):
                    nonlocal output
                    deadline=time.monotonic()+8
                    while marker.encode() not in output:
                        if time.monotonic()>deadline:raise AssertionError(output.decode(errors='replace'))
                        if select.select([master],[],[],.1)[0]:output+=os.read(master,65536)
                try:
                    until('DOMAIN: ' if not attempt else 'DOMAIN [example.org]: ')
                    if not attempt:
                        os.write(master,b'example.org\n');until('абсолютный путь')
                    os.killpg(child.pid,signal.SIGINT);child.wait(timeout=8)
                    self.assertEqual(child.returncode,130)
                    self.assertEqual(json.loads(draft.read_text())['values']['DOMAIN'],'example.org')
                    self.assertEqual(draft.stat().st_mode&0o777,0o600)
                    self.assertFalse((target/'installation.json').exists())
                    transcript+=output
                finally:
                    if child.poll() is None:child.kill();child.wait()
                    os.close(master)
            self.assertNotIn(b'\x1b[',transcript)

    def test_interrupted_operation_waits_for_real_child_and_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder);done=source/'terminated'
            (source/'misbot.py').write_text('''import signal,sys,time
from pathlib import Path
def stop(*args):
    Path(__file__).with_name('terminated').write_text('finished')
    raise SystemExit(143)
signal.signal(signal.SIGTERM,stop)
Path(__file__).with_name('ready').write_text('ready')
while True:time.sleep(.05)
''')
            original=subprocess.Popen;children=[]
            def launch(*args,**kwargs):
                child=original(*args,**kwargs);children.append(child)
                deadline=time.monotonic()+5
                while not (source/'ready').exists():
                    if time.monotonic()>deadline:raise AssertionError('child not ready')
                    time.sleep(.02)
                wait=child.wait;first=True
                def interrupt_once(*args,**kwargs):
                    nonlocal first
                    if first:first=False;raise KeyboardInterrupt()
                    return wait(*args,**kwargs)
                child.wait=interrupt_once
                return child
            interface=ui.UI(directory=source/'target',source=source,draft=source/'draft.json')
            with patch.object(ui.subprocess,'Popen',side_effect=launch),patch('sys.stdout',new=io.StringIO()):
                self.assertEqual(interface.run('start'),130)
            self.assertTrue(done.exists());self.assertEqual(children[0].poll(),143)

    def test_real_ui_ssh_hangup_keeps_saved_draft(self):
        if os.geteuid()!=0:self.skipTest('UI requires an isolated root operator')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);target=base/'Matrix Test';master,slave=pty.openpty()
            child=subprocess.Popen([sys.executable,str(Path(ui.__file__)),'--directory',str(target)],
                stdin=slave,stdout=slave,stderr=slave,start_new_session=True,
                env={**os.environ,'HOME':str(base),'TERM':'dumb'})
            os.close(slave);output=b''
            def until(marker):
                nonlocal output
                deadline=time.monotonic()+8
                while marker.encode() not in output:
                    if time.monotonic()>deadline:raise AssertionError(output.decode(errors='replace'))
                    if select.select([master],[],[],.1)[0]:output+=os.read(master,65536)
            try:
                until('DOMAIN: ');os.write(master,b'example.org\n');until('абсолютный путь')
                os.killpg(child.pid,signal.SIGHUP);child.wait(timeout=8)
                self.assertEqual(child.returncode,130)
                drafts=list((base/'.local/state/misbot/wizard').glob('*.json'))
                self.assertEqual(len(drafts),1)
                self.assertEqual(json.loads(drafts[0].read_text())['values']['DOMAIN'],'example.org')
                self.assertEqual(drafts[0].stat().st_mode&0o777,0o600)
                self.assertFalse(target.exists())
            finally:
                if child.poll() is None:child.kill();child.wait()
                os.close(master)


if __name__=='__main__':unittest.main()
