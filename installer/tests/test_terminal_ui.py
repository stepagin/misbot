"""A real Linux PTY with fictional backend; no live Matrix claim."""
import io,json,os
from pathlib import Path
import pty,select,signal,subprocess,sys,tempfile,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import terminal_ui as ui
import configuration_schema as schema
import ui_actions


class UITests(unittest.TestCase):
    def test_task_catalog_uses_current_cli_without_removed_room_commands(self):
        self.assertNotIn('rooms',ui_actions.MENUS)
        for group,(_,actions) in ui_actions.MENUS.items():
            for action in actions:
                with self.subTest(group=group,path=action.path):
                    if action.path.startswith('@'):
                        self.assertIn(action.path[1:],ui_actions.MENUS)
                    else:
                        self.assertIn(action.path,ui.cli_commands.BY_PATH)
                        self.assertFalse(action.path.startswith('federation rooms'))
                        for argument in ui.cli_commands.BY_PATH[action.path].arguments:
                            self.assertIn(argument,ui_actions.ARGUMENTS)

    def test_navigation_and_read_only_action_do_not_require_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            entries=iter(['1','1','0','0'])
            interface=self.initialized_ui(folder,reader=lambda *args:next(entries))
            calls=[]
            with patch('sys.stdout',new=io.StringIO()),patch.object(interface,'run',side_effect=lambda path,**kw:calls.append(path) or 0):
                self.assertEqual(interface.menu(),0)
            self.assertEqual(calls,['status','status'])

    def test_enter_cancels_stop_and_secret_display(self):
        for path in ('stop','users invitation show','credentials admin show'):
            with self.subTest(path=path),tempfile.TemporaryDirectory() as folder:
                interface=self.initialized_ui(folder,reader=lambda *args:'')
                with patch('sys.stdout',new=io.StringIO()),patch.object(interface,'run') as run:
                    self.assertEqual(interface.execute(ui_actions.Action(path,'Fictional action')),0)
                run.assert_not_called()

    def test_invalid_menu_number_is_retried_without_running_anything(self):
        entries=iter(['99','invalid','0'])
        with tempfile.TemporaryDirectory() as folder:
            interface=ui.UI(directory=Path(folder)/'target',draft=Path(folder)/'draft.json',reader=lambda *args:next(entries))
            with patch('sys.stdout',new=io.StringIO()),patch.object(interface,'run') as run:
                self.assertEqual(interface.choose('Test',[('1','Read','')]),'0')
            run.assert_not_called()

    def test_existing_stack_skips_native_settings_and_filters_nested_menus(self):
        import state_store
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);root.chmod(0o700)
            state_store.write_json(root/'installation.json',{'domain':'example.org','backend':'existing-compose'})
            with patch('configuration.load',side_effect=AssertionError('native configuration read')):
                interface=ui.UI(directory=root,draft=root/'draft.json')
            for group in ui_actions.MENUS:
                for item in interface.available_actions(group):
                    if not item.path.startswith('@'):
                        self.assertIn(ui.cli_commands.BY_PATH[item.path].backend,ui.cli_commands.EXISTING_BACKENDS)
            self.assertEqual(interface.available_actions('users'),[])
            self.assertFalse(interface.supported('storage database connect'))
            self.assertTrue(interface.supported('features enable'))

    def test_ui_preserves_backend_failure_and_offers_selected_server_status(self):
        with tempfile.TemporaryDirectory() as folder:
            interface=self.initialized_ui(folder,reader=lambda *args:'да')
            with patch('sys.stdout',new=io.StringIO()) as output,patch.object(interface,'run',return_value=130):
                self.assertEqual(interface.execute(ui_actions.Action('start','Запустить сервер')),130)
            self.assertIn('Операция прервана',output.getvalue())
            self.assertIn(str(interface.root),output.getvalue())

    def test_settings_preview_cancel_keeps_active_configuration(self):
        import configuration
        with tempfile.TemporaryDirectory() as folder:
            entries=iter(['2','fictional@example.org','a','нет'])
            interface=self.initialized_ui(folder,reader=lambda *args:next(entries))
            before=configuration.load(interface.root,True)
            with patch('sys.stdout',new=io.StringIO()),patch.object(configuration,'apply_settings') as apply:
                interface.settings_menu()
            apply.assert_not_called()
            self.assertEqual(configuration.load(interface.root,True),before)
            self.assertEqual(json.loads(interface.draft.read_text())['values']['ACME_EMAIL'],'fictional@example.org')

    def test_cancelled_restore_does_not_switch_selected_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'new target'
            entries=iter([str(target),'/root/fictional.tar.gz','a'*64,'8.8.8.8','нет'])
            interface=self.initialized_ui(folder,reader=lambda *args:next(entries))
            previous=interface.root
            with patch('sys.stdout',new=io.StringIO()),patch.object(interface,'run') as run:
                self.assertEqual(interface.execute(ui_actions.Action('backup restore','Restore')),0)
            run.assert_not_called()
            self.assertEqual(interface.root,previous)
            self.assertFalse(target.exists())

    def test_new_installation_offers_restore_without_existing_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            entries=iter(['2','0','0'])
            interface=ui.UI(directory=Path(folder)/'target',draft=Path(folder)/'draft.json',reader=lambda *args:next(entries))
            with patch('sys.stdout',new=io.StringIO()) as output,patch.object(interface,'run') as run:
                self.assertEqual(interface.welcome(),0)
            run.assert_not_called()
            self.assertIn('Скачать и проверить копию',output.getvalue())
            self.assertNotIn('Активировать восстановленный сервер',output.getvalue())

    def test_empty_final_review_does_not_apply_installation(self):
        entries=iter(['','сохранить и выйти'])
        with tempfile.TemporaryDirectory() as folder:
            interface=ui.UI(directory=Path(folder)/'target',draft=Path(folder)/'draft.json',reader=lambda *args:next(entries))
            interface.model=schema.validate({'DOMAIN':'example.org'})
            with patch('sys.stdout',new=io.StringIO()),self.assertRaises(ui.SaveExit),patch.object(interface,'run') as run:
                interface.setup_step(6)
            run.assert_not_called()

    def test_completion_does_not_print_invitation_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            interface=self.initialized_ui(folder)
            import registration
            with patch('sys.stdout',new=io.StringIO()) as output,patch.object(registration,'manage',return_value={'state':'valid','expires_at':'2030-01-01','token':'FICTIONAL_HIDDEN_TOKEN'}):
                interface.finish()
            self.assertNotIn('FICTIONAL_HIDDEN_TOKEN',output.getvalue())
            self.assertIn('Показать код приглашения',output.getvalue())

    def initialized_ui(self,folder,step=7,reader=None):
        import configuration,state_store
        root=Path(folder)/'target';root.mkdir(mode=0o700)
        state_store.write_json(root/'installation.json',{'domain':'example.org','databases_initialized':True,'config_format_version':2,'mas_issuer':'https://mas.example.org/'})
        configuration.write_model(root,{'DOMAIN':'example.org'})
        interface=ui.UI(root,draft=Path(folder)/'draft.json',reader=reader)
        interface.step=step;interface.model={'DOMAIN':'stale.example.org'}
        interface.save()
        return interface

    def test_initialized_draft_resumes_check_without_reapplying(self):
        import configuration
        for step in (0,6,7,8,9):
            with self.subTest(step=step),tempfile.TemporaryDirectory() as folder:
                interface=self.initialized_ui(folder,step)
                before={p:p.read_bytes() for p in interface.root.rglob('*') if p.is_file()}
                calls=[]
                with patch('sys.stdout',new=io.StringIO()),patch.object(interface,'run',side_effect=lambda command,**kw:calls.append(command) or 0),patch.object(interface,'finish') as finish,patch.object(configuration,'write_model',side_effect=AssertionError('must not reapply')):
                    self.assertEqual(interface.wizard(),0)
                self.assertEqual(calls,[] if step==9 else ['check'])
                finish.assert_called_once()
                self.assertEqual(interface.model['DOMAIN'],'example.org')
                self.assertEqual(before,{p:p.read_bytes() for p in interface.root.rglob('*') if p.is_file()})

    def test_back_after_failed_check_opens_menu(self):
        with tempfile.TemporaryDirectory() as folder:
            interface=self.initialized_ui(folder,8,reader=lambda *args:'назад')
            with patch('sys.stdout',new=io.StringIO()) as output,patch.object(interface,'run',return_value=1),patch.object(interface,'menu',return_value=0) as menu:
                self.assertEqual(interface.wizard(),0)
            menu.assert_called_once()
            self.assertIn('sudo misbot start',output.getvalue())
            self.assertNotIn('config validate/status',output.getvalue())

    def test_initialization_completed_during_failed_start_resumes_check(self):
        import configuration,state_store
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'target';root.mkdir(mode=0o700)
            state_store.write_json(root/'installation.json',{'domain':'example.org','databases_initialized':False,'config_format_version':2})
            configuration.write_model(root,{'DOMAIN':'example.org'})
            interface=ui.UI(root,draft=Path(folder)/'draft.json',reader=lambda *args:'повторить')
            interface.step=7;calls=[]
            def run(command,**kwargs):
                calls.append(command)
                if command=='start':
                    state_store.write_json(root/'installation.json',{'domain':'example.org','databases_initialized':True,'config_format_version':2,'mas_issuer':'https://mas.example.org/'})
                    return 1
                return 0
            with patch('sys.stdout',new=io.StringIO()),patch.object(interface,'run',side_effect=run),patch.object(interface,'finish'):
                self.assertEqual(interface.wizard(),0)
            self.assertEqual(calls,['start','check'])

    def test_back_at_completion_opens_menu_without_initial_form(self):
        with tempfile.TemporaryDirectory() as folder:
            interface=self.initialized_ui(folder,9)
            with patch('sys.stdout',new=io.StringIO()),patch.object(interface,'finish',side_effect=ui.Back),patch.object(interface,'menu',return_value=0) as menu:
                self.assertEqual(interface.wizard(),0)
            menu.assert_called_once()

    def test_apply_guard_skips_write_when_installation_is_initialized(self):
        import configuration
        with tempfile.TemporaryDirectory() as folder:
            interface=self.initialized_ui(folder)
            with patch('sys.stdout',new=io.StringIO()),patch.object(configuration,'write_model',side_effect=AssertionError('must not write')),patch.object(interface,'run',side_effect=AssertionError('must not start')):
                interface.setup_step(7)
            self.assertEqual(interface.model['DOMAIN'],'example.org')

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

    def test_real_rich_pty_menu_resize_and_cancel_do_not_stop_server(self):
        import terminal_views
        if terminal_views.Console is None:self.skipTest('Rich is optional')
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'menu.py'
            script.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import terminal_ui,configuration,state_store
root=Path(sys.argv[2]);root.mkdir(mode=0o700)
state_store.write_json(root/'installation.json',{'domain':'example.org','databases_initialized':True,'config_format_version':2,'mas_issuer':'https://mas.example.org/'})
configuration.write_model(root,{'DOMAIN':'example.org'})
def run(args):
    print('FICTIONAL BACKEND:'+ ' '.join(args[4:]),flush=True)
    return 0
interface=terminal_ui.UI(root,draft=root/'draft.json',runner=run)
interface.model['GOOGLE_CLIENT_SECRET']='FICTIONAL_UNSHOWN_SECRET'
raise SystemExit(interface.menu())
''')
            master,slave=pty.openpty()
            import fcntl,struct,termios
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
            environment={key:value for key,value in os.environ.items() if key!='NO_COLOR'}
            environment['TERM']='xterm-256color'
            child=subprocess.Popen([sys.executable,str(script),str(Path(ui.__file__).parent),str(Path(folder)/'target')],
                stdin=slave,stdout=slave,stderr=slave,start_new_session=True,env=environment)
            os.close(slave);transcript=b'';pending=b''
            def answer(marker,value):
                nonlocal transcript,pending
                deadline=time.monotonic()+8
                while marker.encode() not in pending:
                    if time.monotonic()>deadline:raise AssertionError(transcript.decode(errors='replace'))
                    if select.select([master],[],[],.1)[0]:
                        try:block=os.read(master,65536)
                        except OSError:raise AssertionError(transcript.decode(errors='replace')) from None
                        transcript+=block;pending+=block
                pending=pending.split(marker.encode(),1)[1]
                os.write(master,(value+'\n').encode())
            try:
                answer('Номер [0]:','1')
                fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',12,24,0,0))
                answer('Номер [0]:','5')
                answer('Выполнить операцию? да/нет [нет]:','')
                answer('Номер [0]:','0')
                answer('Номер [0]:','0')
                child.wait(timeout=8)
                self.assertEqual(child.returncode,0,transcript.decode(errors='replace'))
                self.assertIn(b'\x1b[',transcript)
                self.assertIn(b'FICTIONAL BACKEND:status',transcript)
                self.assertNotIn(b'FICTIONAL BACKEND:stop',transcript)
                self.assertNotIn(b'FICTIONAL_UNSHOWN_SECRET',transcript)
            finally:
                if child.poll() is None:child.kill();child.wait()
                os.close(master)

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
                        ('MAS_DOMAIN:',''),('PUBLIC_IP','8.8.8.8'),('ACME_EMAIL:',''),('MEDIA_MODE [local]:',''),
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
                until('С чего начать?');os.write(master,b'1\n')
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
