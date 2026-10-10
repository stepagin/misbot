"""Detached update worker. Result survives bot/controller restarts and failures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import configuration
import releases
import state_store


def job_path(root):
    return Path(root) / 'data/admin-panel/update.json'


def unit(root, identifier):
    return 'misbot-update-' + hashlib.sha256(str(root).encode()).hexdigest()[:12] + '-' + identifier[:16]


def queue(root, number, event):
    releases.version(number)
    root = Path(root)
    if configuration.read_private_json(root/'installation.json').get('backend')=='existing-compose':
        raise RuntimeError('Для existing-compose используйте проверенный source plan и update apply --source через SSH.')
    current = releases.installed(root)
    if releases.version(number) <= releases.version(current) or releases.version(number)[0] != releases.version(current)[0]:
        raise RuntimeError('Обновление уже установлено или требует отдельного решения о major-версии.')
    path = job_path(root)
    identifier = hashlib.sha256(event.encode()).hexdigest()
    if path.exists():
        result_notice(__import__('admin_panel').Panel(root))
        previous = configuration.read_private_json(path)
        if previous['id'] == identifier:
            return {'status': previous['state'], 'version': previous['version']}
        if previous['state'] in ('queued', 'running'):
            raise RuntimeError('Обновление уже выполняется; результат придёт в этот чат.')
    job = {'id': identifier, 'version': number, 'before': current, 'state': 'queued', 'created': time.time(),
           'unit': unit(root, identifier)}
    state_store.write_json(path, job)
    try:
        # A separate systemd unit survives the controller stopping during apply.
        subprocess.run(['systemd-run', '--quiet', '--collect', '--unit=' + job['unit'],
            '--property=Type=exec', '--property=UMask=0077', '--property=TimeoutStopSec=30',
            '/usr/bin/python3', str(root / 'release_update.py'), '--directory', str(root),
            '--job', identifier], check=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        job.update(state='failed', detail='Не удалось запустить задачу обновления systemd. Выполните sudo misbot update latest через SSH.')
        state_store.write_json(path, job)
    return {'status': job['state'], 'version': number, 'message': 'Результат сохранится и будет доставлен после перезапуска панели.'}


def apply(root, number=None, progress=None):
    root = Path(root)
    release = releases.latest()
    if not release:
        raise RuntimeError('Пока нет опубликованного стабильного релиза Misbot.')
    if number and release['version'] != number:
        raise RuntimeError('Последняя версия изменилась после подтверждения. Запросите «версия» и подтвердите обновление снова.')
    current = releases.installed(root)
    if releases.version(release['version']) <= releases.version(current):
        return {'installed': current, 'detail': 'Последняя стабильная версия уже установлена.'}
    if releases.version(release['version'])[0] != releases.version(current)[0]:
        raise RuntimeError('Major-обновление требует отдельной инструкции и локального решения администратора.')
    if progress:
        progress('download')
    source = releases.download(root, release)
    if progress:
        progress('apply', source=str(source))
    folder = configuration.directory(root / 'data/admin-panel')
    log = folder / ('update-' + str(int(time.time())) + '.log')
    with log.open('x') as output:
        os.chmod(log, 0o600)
        result = subprocess.run(['/usr/bin/python3', str(source / 'update.py'), 'apply',
            '--source', str(source), '--directory', str(root)], stdin=subprocess.DEVNULL,
            stdout=output, stderr=output, **state_store.child_process_options())
    current = releases.installed(root)
    if result.returncode or current != release['version']:
        return {'installed': current, 'success': False, 'exit_code': result.returncode,
                'detail': 'Обновление не подтверждено. Приватный журнал: ' + str(log)
                    + '. Проверьте sudo misbot status. При pending update: sudo misbot update recover --source ' + str(source)}
    return {'installed': current, 'success': True, 'exit_code': 0,
            'detail': 'Новая версия применена; проверки служб завершены. Приватный журнал: ' + str(log)}


def run_job(root, identifier):
    path = job_path(root)
    job = configuration.read_private_json(path)
    if job['id'] != identifier or job['state'] != 'queued':
        raise RuntimeError('Unexpected update job')
    job.update(state='running', stage='checking', pid=os.getpid(),
               started=state_store.process_identity(os.getpid())[1])
    state_store.write_json(path, job)
    def progress(stage, **values):
        job.update(stage=stage, **values)
        state_store.write_json(path, job)
    try:
        result = apply(root, job['version'], progress)
        job.update(state='complete' if result.get('success', True) else 'failed', result=result)
    except (Exception, KeyboardInterrupt) as error:
        # Never forward network/provider exception text or subprocess output to Matrix.
        stage = job.get('stage', 'checking')
        safe_detail=str(error)[:1200] if type(error) is RuntimeError else 'Не удалось подтвердить операцию; детали SDK скрыты.'
        job.update(state='failed', result={'success': False, 'installed': releases.installed(root),
            'detail': 'Этап: ' + stage + '. '+safe_detail+' Обновление прервано или отказало (диск, GitHub, checksum, совместимость или службы). '
                      'Проверьте sudo misbot status и свободный диск: df -h. Повторите проверку версии; '
                      'если есть pending update, используйте указанную status команду update recover.'})
    job['finished'] = time.time()
    state_store.write_json(path, job)


def recover_job(root):
    path = job_path(root)
    if not path.exists():
        return
    job = configuration.read_private_json(path)
    if job['state'] == 'queued' and time.time() - job['created'] < 120:
        return
    if job['state'] == 'running':
        try:
            if state_store.process_identity(job['pid'])[1] == job['started']:
                return
        except (OSError, KeyError):
            pass
    if job['state'] in ('queued', 'running'):
        job.update(state='interrupted', result={'installed': releases.installed(root),
            'detail': 'Процесс обновления завершился без итогового ответа. Проверьте sudo misbot status; '
                      'при pending выполните update recover из доверенного комплекта. Данные сохранены.'})
        state_store.write_json(path, job)


def result_notice(panel):
    path = job_path(panel.root)
    if not path.exists():
        return
    job = configuration.read_private_json(path)
    if job['state'] in ('complete', 'failed', 'interrupted'):
        result = job.get('result', {})
        panel.notice('update-result:' + job['id'], 'Обновление Misbot: ' + job['state']
            + '\nБыло: ' + job['before'] + '; запрошено: ' + job['version']
            + '; установлено: ' + result.get('installed', releases.installed(panel.root))
            + '\nЭтап: ' + job.get('stage','launch') + '; код завершения: ' + str(result.get('exit_code','недоступен'))
            + '\n' + result.get('detail', job.get('detail', 'Проверьте sudo misbot status.')))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--job')
    args = parser.parse_args()
    state_store.require_root()
    __import__('command_wrapper').trusted_installation(args.directory)
    if args.job:
        run_job(args.directory, args.job)
    else:
        print(json.dumps(apply(args.directory), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError):
        print('Обновление не начато или не подтверждено. Проверьте GitHub Releases, df -h и sudo misbot status.', file=sys.stderr)
        raise SystemExit(1)
