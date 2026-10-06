"""Narrow Unix RPC + Unix HTTP service, proxied by existing Caddy HTTPS.

Matrix sender is a claim only the dedicated bot UID may supply; it is independently
revalidated against MAS. HTTP has no local administrative dispatcher.
"""
import argparse
from collections import deque
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import struct
import threading
import time
import hashlib

import configuration
import federation
import federation_protocol as protocol
import lifecycle
import state_store

BOT_ACTIONS={'servers','check','add','remove','invite','requests','approve','reject','cancel','retry',
             'request-status','peers','operations','room-invitations','room-accept','room-reject','room-leave','room-invite','auto-accept','recover','rollback'}
MUTATIONS=BOT_ACTIONS-{'servers','check','requests','request-status','peers','operations','room-invitations'}


class Rate:
    def __init__(self):self.times=deque();self.lock=threading.Lock()
    def admit(self):
        with self.lock:
            now=time.monotonic()
            while self.times and self.times[0]<now-60:self.times.popleft()
            if len(self.times)>=30:return False
            self.times.append(now);return True


def bot_call(backend, request, uid):
    if uid!=10003 or not isinstance(request,dict) or set(request)-{'action','argument','actor','event_id'}:
        raise protocol.ProtocolError('local_authorization_required')
    action=request.get('action')
    if action in ('notifications','auto-invitation','authorize'):
        with state_store.operation_guard(backend.root):
            backend.ready(action in ('notifications','authorize'));data=backend.load()
            if action=='authorize':
                actor=request.get('actor')
                if actor=='root':raise protocol.ProtocolError('admin_unconfirmed')
                backend.authorize(actor);return {'authorized':True}
            if action=='notifications':return {'requests':[backend.view(backend.request(data,k)) for k in data['requests'] if data['requests'][k]['direction']=='incoming'],
                'operations':[{'id':k,'state':v['state'],'result':v.get('result',{})} for k,v in data.get('jobs',{}).items() if v['state'] in ('complete','failed','interrupted')]}
            claim=json.loads(request.get('argument','{}'));mxid=claim.get('sender');room=claim.get('room');peers=data['peers']
            enabled=configuration.load(backend.root).get('FEDERATION_AUTO_ACCEPT','false')=='true'
            approved=any(p.get('approved') and p['document']['bot_mxid']==mxid and s in backend.servers()
                and data['requests'].get(p['request_id'],{}).get('state')=='connected' for s,p in peers.items())
            if enabled and approved:
                from federation_runtime import matrix_operation
                current=matrix_operation(backend.root,'room-invitations',None)['invitations']
                approved=any(item['room']==room and item['sender']==mxid for item in current)
            return {'allowed':bool(enabled and approved)}
    if action not in BOT_ACTIONS or request.get('actor')=='root' or not isinstance(request.get('actor'),str):
        raise protocol.ProtocolError('local_authorization_required')
    if not isinstance(request.get('event_id'),str):raise protocol.ProtocolError('event_required')
    if len(request['event_id'])>255:raise protocol.ProtocolError('invalid_event')
    if action in MUTATIONS:
        # The checked backup must stop this bot. A durable asynchronous job keeps
        # the accepted command alive and delivers the result after its restart.
        with state_store.operation_guard(backend.root):
            backend.ready(action in ('recover','rollback'));backend.authorize(request['actor']);data=backend.load()
            plan=backend.policy_plan(request.get('argument'),action=='add') if action in ('add','remove') else 'Проверка текущих прав; для политики — копия и перезапуск Synapse/Web.'
            jid='FO-'+hashlib.sha256(request['event_id'].encode()).hexdigest()[:32]
            jobs=data.setdefault('jobs',{})
            if jid not in jobs:
                if len(jobs)>=1000:raise protocol.ProtocolError('operation_capacity')
                jobs[jid]={'state':'queued','action':action,'argument':request.get('argument'),
                           'actor':request['actor'],'event_id':request['event_id']}
                backend.save(data)
            return {'operation_id':jid,'status':jobs[jid]['state'],'plan':plan,
                    'next_action':'Итог придёт после перезапуска бота; операции показывает сохранённое состояние.'}
    return backend.operate(action,request.get('argument'),request['actor'],request['event_id'])


def job_step(backend):
    with state_store.operation_guard(backend.root):
        data=backend.load();jobs=data.get('jobs',{})
        selected=next(((key,value) for key,value in jobs.items() if value['state']=='queued'),None)
        if not selected:return
        jid,job=selected;job['state']='running';backend.save(data)
    try:
        result=backend.operate(job['action'],job['argument'],job['actor'],job['event_id']);status='complete'
    except protocol.ProtocolError as error:result={'code':error.code};status='failed'
    except Exception:
        import cli_commands
        result={'code':'operation_unconfirmed','next_action':cli_commands.recovery_command('federation-recover',backend.root)};status='failed'
        if isinstance(job.get('argument'),str) and federation.REQUEST.fullmatch(job['argument']):
            result['retry']=cli_commands.recovery_command('federation-retry',backend.root)+' --request '+job['argument']
    with state_store.operation_guard(backend.root):
        data=backend.load();data['jobs'][jid].update(state=status,result=result);backend.save(data)


def serve(root):
    root=Path(root);backend=federation.Backend(root);lifecycle.configure(root,'federation-service')
    # Keep local status/recovery available after an interrupted policy publish.
    # Each normal RPC/exchange still uses its own strict admission gate.
    backend.ready(True);federation_runtime=__import__('federation_runtime')
    federation_runtime.validate(root,configuration.read_private_json(root/'installation.json'))
    run=root/'run/federation';run.mkdir(mode=0o755,parents=True,exist_ok=True);run.chmod(0o755)
    # Persistent state is excluded from the socket mount. Never replace unknown entries.
    for name in ('control.sock','http.sock'):
        path=run/name
        if path.exists() or path.is_symlink():
            info=path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=0:raise RuntimeError('Foreign controller socket')
            path.unlink()
    rate=Rate();work=threading.Lock();public_document=backend.doc()
    with state_store.operation_guard(root):
        data=backend.load()
        for job in data.get('jobs',{}).values():
            if job['state']=='running':job.update(state='interrupted',result={'code':'interrupted','next_action':'sudo misbot federation recover; проверьте federation requests'})
        backend.save(data)
    def jobs():
        while True:
            time.sleep(1)
            if not work.acquire(blocking=False):continue
            try:job_step(backend)
            except Exception:pass # busy external operation; retain queued checkpoint
            finally:work.release()
    threading.Thread(target=jobs,daemon=True).start()
    class HTTP(http.server.BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def setup(self):super().setup();self.connection.settimeout(15)
        def reply(self,status,value):
            raw=protocol.canonical(value);self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            if self.path!=protocol.DISCOVERY:return self.reply(404,{'code':'not_found'})
            if not rate.admit():return self.reply(429,{'code':'rate_limit'})
            try:
                metadata=configuration.read_private_json(root/'installation.json')
                if metadata.get('restore_pending') or metadata.get('migration_frozen'):raise ValueError()
                self.reply(200,public_document)
            except Exception:self.reply(503,{'code':'installation_busy_or_pending'})
        def do_POST(self):
            if self.path!=protocol.API:return self.reply(404,{'code':'not_found'})
            if not rate.admit():return self.reply(429,{'code':'rate_limit'})
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:raise protocol.ProtocolError('invalid_length')
                length=int(self.headers['Content-Length'])
                if not 0<length<=protocol.LIMIT:raise protocol.ProtocolError('request_too_large')
                raw=self.rfile.read(length)
                if len(raw)!=length:raise protocol.ProtocolError('incomplete_request')
                message=json.loads(raw)
                if not work.acquire(blocking=False):raise RuntimeError('busy')
                try:result=backend.receive(message)
                finally:work.release()
                self.reply(200,result)
            except protocol.ProtocolError as error:self.reply(400,{'code':error.code})
            except (ValueError,TypeError,KeyError):self.reply(400,{'code':'invalid_request'})
            except Exception:self.reply(503,{'code':'installation_busy_or_pending'})
    class RPC(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(300)
            try:
                _,uid,_=struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                raw=self.rfile.readline(protocol.LIMIT+1)
                if len(raw)>protocol.LIMIT or not raw.endswith(b'\n'):raise protocol.ProtocolError('request_too_large')
                if not work.acquire(blocking=False):raise RuntimeError('busy')
                try:result=bot_call(backend,json.loads(raw),uid)
                finally:work.release()
                value={'ok':True,'result':result}
            except protocol.ProtocolError as error:value={'ok':False,'code':error.code}
            except Exception:value={'ok':False,'code':'busy_pending_or_operation_failed'}
            self.wfile.write(protocol.canonical(value)+b'\n')
    # Serial HTTP requests bound memory/concurrency; independent RPC listener.
    class Web(socketserver.UnixStreamServer):allow_reuse_address=True
    web=Web(str(run/'http.sock'),HTTP);web.server_name='misbot';web.server_port=0
    rpc=socketserver.UnixStreamServer(str(run/'control.sock'),RPC)
    os.chmod(run/'http.sock',0o666)  # public HTTP only; no admin routes
    os.chown(run/'control.sock',0,10003);os.chmod(run/'control.sock',0o660)
    thread=threading.Thread(target=web.serve_forever,daemon=True);thread.start()
    try:rpc.serve_forever()
    finally:web.shutdown();web.server_close();rpc.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True,type=Path);args=parser.parse_args()
    try:
        state_store.require_root();__import__('command_wrapper').trusted_installation(args.directory);serve(args.directory)
    except Exception:
        print('Federation controller unavailable; retain identity and use misbot status/recovery',flush=True)
        raise SystemExit(1)
