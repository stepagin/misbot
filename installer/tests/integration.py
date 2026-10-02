#!/usr/bin/env python3
"""Disposable core test on a Linux Docker host. No public ports, systemd or host tuning."""
import hashlib,json,os
from pathlib import Path
import shutil,subprocess,sys,tarfile,tempfile,time,uuid
import yaml
BUNDLE=Path(__file__).resolve().parents[1]
DEVELOPER_BUNDLE=BUNDLE
DEVELOPER_BOT=BUNDLE.parent/'bots/matrix-referral'
ROOT=Path(tempfile.mkdtemp(prefix='matrix-installer-check-',dir='/tmp'))/'stack'
PROJECT='installercheck'+uuid.uuid4().hex[:8]
compose=['docker','compose','-p',PROJECT,'--project-directory',str(ROOT),'-f',str(ROOT/'integration.yaml')]
started=False
def emit(check,**data):print(json.dumps({'check':check,**data}),flush=True)
def run(cmd,stdin=None):
    p=subprocess.run(cmd,input=stdin,capture_output=True,text=True)
    if p.returncode:
        ROOT.mkdir(parents=True,exist_ok=True)
        (ROOT/'private-command-error.txt').write_text(p.stderr+'\n'+p.stdout)
        for line in p.stdout.splitlines():
            try:
                record=json.loads(line)
                if record.get('check')=='fixture_authorization_step':
                    emit('fixture_authorization_step',**{key:record[key] for key in ['status','forms','password_form','post_form']})
            except Exception:pass
        raise RuntimeError(Path(cmd[0]).name+' failed')
    return p.stdout
def wait(fn):
    for _ in range(90):
        try:
            if fn():return
        except Exception:pass
        time.sleep(2)
    raise RuntimeError('fixture did not become ready')
def fingerprint():
    paths=list((ROOT/'secrets').iterdir())+[ROOT/'config/mas-signing.pem',ROOT/'config/mas-defaults.yaml']
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
try:
    assert os.geteuid()==0
    if not (BUNDLE/'referral-bot').is_dir():
        archive_path=ROOT.parent/'matrix-installer.tar.gz'
        run(['python3',str(BUNDLE/'build.py'),'--output',str(archive_path)])
        with tarfile.open(archive_path) as archive:
            for member in archive:
                parts=Path(member.name).parts
                if not member.isfile() or not parts or parts[0]!='matrix-installer' or '..' in parts:
                    raise RuntimeError('Unexpected installer archive member')
                target=ROOT.parent/member.name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        BUNDLE=ROOT.parent/'matrix-installer'
    sys.path.insert(0,str(BUNDLE))
    import referral
    run(['python3',str(BUNDLE/'generate.py'),'--directory',str(ROOT),'--domain','bootstrap-test.invalid','--initialize-env'])
    env=ROOT/'settings.env'
    env.write_text(env.read_text().replace('PUBLIC_IP=\n','PUBLIC_IP=1.1.1.1\n').replace('172.30.50.0/24','172.30.250.0/24'))
    for name in ['SHA256SUMS']+[line.split('  ',1)[1] for line in (BUNDLE/'SHA256SUMS').read_text().splitlines()]:
        (ROOT/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(BUNDLE/name,ROOT/name)
    run(['python3',str(ROOT/'lifecycle.py'),'--directory',str(ROOT),'generate'])
    emit('actual_image_config_validators',passed=True)
    before=fingerprint()
    run(['python3',str(ROOT/'generate.py'),'--directory',str(ROOT)])
    assert fingerprint()==before
    emit('production_generation_preserves_all_keys_and_password_salts',passed=True)
    config=yaml.safe_load((ROOT/'compose.yaml').read_text())
    config['services']={key:config['services'][key] for key in ['postgres','synapse','mas']}
    for name,limit in [('postgres','128m'),('synapse','192m'),('mas','112m')]:
        config['services'][name]['mem_limit']=limit
        config['services'][name]['memswap_limit']=str(int(limit[:-1])*2)+'m'
        config['services'][name]['cpus']=0.25
        config['services'][name]['restart']='no'
    config['secrets']={'db_admin':config['secrets']['db_admin']}
    (ROOT/'integration.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
    started=True
    run(compose+['up','-d'])
    probe="import urllib.request; urllib.request.urlopen('http://mas:8080/health',timeout=3).read(); urllib.request.urlopen('http://synapse:8008/_matrix/client/versions',timeout=3).read()"
    wait(lambda:run(compose+['exec','-T','synapse','python','-c',probe]) is not None)
    credentials=json.loads((ROOT/'secrets/admin-access.json').read_text())
    run(compose+['exec','-T','synapse','python','-c',(ROOT/'mas_admin.py').read_text()],
        stdin=json.dumps({'credentials':credentials,
                         'client':json.loads((ROOT/'secrets/referral-client.json').read_text())}))
    fixture='CREDS='+repr(credentials)+'\n'+(DEVELOPER_BUNDLE/'tests/oauth_internal.py').read_text()
    output=run(compose+['exec','-T','synapse','python','-'],stdin=fixture)
    for line in output.splitlines():
        if line.startswith('{'):print(line,flush=True)
    # Add only the two private referral services; never publish test ports.
    generated=yaml.safe_load((ROOT/'compose.yaml').read_text())
    for name in ['referral-control','referral-bot']:
        config['services'][name]=generated['services'][name]
        config['services'][name]['restart']='no'
        config['services'][name]['cpus']=0.25
    (ROOT/'integration.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
    def dc(*parts,**kwargs):return run(compose+list(parts),**kwargs)
    dc('up','-d','referral-control')
    wait(lambda:dc('exec','-T','referral-control','python','-c',
        "from common import bridge_call; bridge_call({'action':'servers'},path='/config/bot-run/control.sock',timeout=2)") is not None)
    metadata=json.loads((ROOT/'installation.json').read_text())
    referral.provision(ROOT,dc,metadata)
    (ROOT/'installation.json').write_text(json.dumps(metadata))
    private_before=hashlib.sha256((ROOT/'config/referral-bot.json').read_bytes()).hexdigest()
    referral.provision(ROOT,dc,metadata)
    assert hashlib.sha256((ROOT/'config/referral-bot.json').read_bytes()).hexdigest()==private_before
    dc('up','-d','referral-bot')
    readiness="fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
    wait(lambda:dc('exec','-T','referral-bot','node','-e',readiness) is not None)
    referral.note_ready(ROOT,metadata)
    (ROOT/'installation.json').write_text(json.dumps(metadata))
    emit('automatic_referral_provision',passed=True,ordinary_account=True,stable_session=True,cross_signing=True)
    testing=ROOT/'referral-test'
    testing.mkdir(mode=0o700)
    os.chown(testing,10002,10001)
    cfg=testing/'config.json'
    cfg.write_bytes((ROOT/'data/synapse/installer-test-client.json').read_bytes())
    cfg.chmod(0o600)
    os.chown(cfg,10002,10001)
    network=json.loads(run(['docker','inspect',dc('ps','-q','synapse').strip()]))[0]['NetworkSettings']['Networks']
    network=next(iter(network))
    acceptance=run(['docker','run','--rm','--network',network,'--user','10002:10001','--read-only',
        '--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '-v',str(testing)+':/test','-v',str(DEVELOPER_BOT/'live-test.js')+':/app/live-test.js:ro',
        generated['services']['referral-bot']['image'],'node','/app/live-test.js'])
    # Script emits assertions only, never decoded messages or tokens.
    print(acceptance,flush=True)
    emit('referral_e2ee_invites_and_numbered_revoke',passed=True,domain='bootstrap-test.invalid')
    cross_before=hashlib.sha256((ROOT/'data/referral-bot/cross-signing.json').read_bytes()).hexdigest()
    dc('restart','referral-control','referral-bot')
    wait(lambda:dc('exec','-T','referral-bot','node','-e',readiness) is not None)
    assert hashlib.sha256((ROOT/'data/referral-bot/cross-signing.json').read_bytes()).hexdigest()==cross_before
    assert hashlib.sha256((ROOT/'config/referral-bot.json').read_bytes()).hexdigest()==private_before
    run(['python3',str(ROOT/'generate.py'),'--directory',str(ROOT)])
    assert hashlib.sha256((ROOT/'config/referral-bot.json').read_bytes()).hexdigest()==private_before
    assert hashlib.sha256((ROOT/'data/referral-bot/cross-signing.json').read_bytes()).hexdigest()==cross_before
    emit('referral_restart_and_regeneration_preserve_identity',passed=True)
    # Exercise the shipped consistent backup/restore checker against fixture DBs.
    import importlib.util
    original_argv=sys.argv
    sys.argv=['lifecycle.py','--directory',str(ROOT),'backup']
    spec=importlib.util.spec_from_file_location('fixture_lifecycle',ROOT/'lifecycle.py')
    lifecycle=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)
    sys.argv=original_argv
    lifecycle.configure(ROOT,'backup')
    lifecycle.COMPOSE=compose
    lifecycle.backup()
    lifecycle.restore_check()
    emit('isolated_consistent_backup_and_restore',passed=True)
    emit('fresh_database_bootstrap',passed=True,production_ports_published=False)
except Exception as error:
    emit('isolated_integration_failed',error_type=type(error).__name__,message=str(error) if type(error) is RuntimeError else 'assertion')
    # Private diagnostics remain only in the protected temporary fixture if failed.
    emit('private_fixture_for_diagnostics',path=str(ROOT))
    if started:
        logs=subprocess.run(compose+['logs','--no-color','--tail','80'],capture_output=True,text=True)
        (ROOT/'private-container-logs.txt').write_text(logs.stdout+logs.stderr)
        inspect=subprocess.run(compose+['ps','--all','--format','json'],capture_output=True,text=True)
        for line in inspect.stdout.splitlines():
            try:
                row=json.loads(line)
                emit('fixture_container',service=row['Service'],state=row['State'],exit_code=row.get('ExitCode'),health=row.get('Health'))
            except Exception:pass
    sys.exit(1)
finally:
    cleaned=True
    if started:
        try:run(compose+['down','--remove-orphans'])
        except Exception:
            cleaned=False
            emit('fixture_cleanup_failed',project=PROJECT)
    if cleaned and sys.exc_info()==(None,None,None):shutil.rmtree(ROOT.parent)
