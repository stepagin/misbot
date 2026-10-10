"""Optional verified backup timer for adopted installations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal

import backup_schedule
import command_wrapper
from existing_compose import Installation, Error, private_directory, save, emit, interrupted


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--key',type=Path)
    parser.add_argument('--calendar')
    parser.add_argument('action',choices=('backup-schedule','backup-schedule-local','backup-schedule-status','backup-schedule-disable','run'))
    args=parser.parse_args()
    if os.geteuid()!=0:return 77
    os.umask(0o077)
    for signum in (signal.SIGTERM,signal.SIGINT,signal.SIGALRM,signal.SIGHUP):signal.signal(signum,interrupted)
    try:
        installation=Installation(args.directory)
        folder=private_directory(installation.control/'features')
        record_path=folder/'backup-schedule.json'
        record=json.loads(record_path.read_text()) if record_path.exists() else None
        name='misbot-existing-backup-'+hashlib.sha256(str(installation.root).encode()).hexdigest()[:16]
        marker='# Misbot adopted backup '+str(installation.root)+'\n'
        if args.action=='backup-schedule-status':
            emit('existing_backup_schedule',configured=record is not None,
                 enabled=bool(record and record.get('enabled')),calendar=record.get('calendar') if record else None)
            if record:
                _,result=installation.run(['systemctl','show',name+'.timer','--property=ActiveState','--property=NextElapseUSecRealtime'],allowed=(0,1,4))
                print(result.decode(),end='')
            return 0
        with installation.lock():
            installation.require_no_pending()
            if args.action=='run':
                if not record or record.get('root')!=str(installation.root):raise Error('Сначала настройте backup schedule на этом сервере.')
                installation.backup(record.get('config'),record.get('key'))
                if record['mode']=='local':installation.verify()
                save(folder/'backup-schedule-verified.json',{'format':1,'verified':True})
                return 0
            paths={suffix:Path('/etc/systemd/system')/(name+'.'+suffix) for suffix in ('service','timer')}
            for path in paths.values():
                if path.exists():
                    info=command_wrapper.regular(path)
                    if info.st_uid!=0 or info.st_mode & 0o022 or not path.read_text().startswith(marker):raise Error('Существующий systemd unit принадлежит другой задаче.')
            if args.action=='backup-schedule-disable':
                if record:
                    installation.run(['systemctl','disable','--now',name+'.timer'])
                    record['enabled']=False;save(record_path,record)
                emit('existing_backup_schedule',enabled=False);return 0
            backup_schedule.validate_calendar(args.calendar)
            external=args.action=='backup-schedule'
            if external and (not args.config or not args.key):raise Error('Нужны --config и --key для external schedule.')
            if not external and (installation.control/'s3/backup-active.json').exists():raise Error('На подключённом S3 используйте backup schedule external.')
            installation.backup(args.config if external else None,args.key if external else None)
            if not external:installation.verify()
            record={'format':1,'root':str(installation.root),'mode':'external' if external else 'local','calendar':args.calendar,
                    'config':str(args.config) if external else None,'key':str(args.key) if external else None,'enabled':False}
            # Paths are fixed/quoted in systemd syntax; no shell or credential values.
            quote=lambda value:'"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
            service=marker+'[Unit]\nDescription=Verified Misbot backup\nAfter=docker.service network-online.target\n[Service]\nType=oneshot\nUMask=0077\nKillMode=mixed\nTimeoutStartSec=60min\nTimeoutStopSec=3min\nExecStart=/usr/bin/python3 -B '+quote(installation.root/'existing_schedule.py')+' --directory '+quote(installation.root)+' run\n'
            timer=marker+'[Unit]\nDescription=Misbot backup timer\n[Timer]\nOnCalendar='+args.calendar+'\nPersistent=true\nUnit='+name+'.service\n[Install]\nWantedBy=timers.target\n'
            previous={p:p.read_bytes() if p.exists() else None for p in paths.values()}
            prior=record_path.read_bytes() if record_path.exists() else None
            try:
                for suffix,data in (('service',service),('timer',timer)):command_wrapper.atomic(paths[suffix],data.encode(),0o644)
                installation.run(['systemd-analyze','verify',*[str(p) for p in paths.values()]])
                save(record_path,record)
                installation.run(['systemctl','daemon-reload'])
                installation.run(['systemctl','enable','--now',name+'.timer'])
                record['enabled']=True;save(record_path,record)
            except BaseException:
                installation.run(['systemctl','disable','--now',name+'.timer'],allowed=(0,1,4,5))
                for path,data in previous.items():
                    if data is None:path.unlink(missing_ok=True)
                    else:command_wrapper.atomic(path,data,0o644)
                if prior is None:record_path.unlink(missing_ok=True)
                else:command_wrapper.atomic(record_path,prior,0o600)
                installation.run(['systemctl','daemon-reload'])
                if prior and json.loads(prior).get('enabled'):installation.run(['systemctl','enable','--now',name+'.timer'])
                raise
            emit('existing_backup_schedule',enabled=True,calendar=args.calendar,writers_pause_limit_minutes=15)
        return 0
    except Exception as error:
        emit('existing_backup_schedule',passed=False,explanation=str(error) if isinstance(error,Error) else 'Backup schedule не подтверждён; проверьте misbot backup schedule status.')
        return 1


if __name__=='__main__':raise SystemExit(main())
