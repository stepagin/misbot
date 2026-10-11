"""Shared federation backend. Remote exchange has no general admin dispatcher."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import time
import urllib.parse

import configuration as config
import configuration_schema as schema
import federation_protocol as protocol
import lifecycle
import state_store

TERMINAL = ('rejected','cancelled','expired')
REQUEST = re.compile(r'MB-[0-9a-f]{32}\Z')
ACTIONS = frozenset(('servers','check','add','remove','invite','requests','approve','reject',
                     'cancel','retry','request-status','peers','operations','recover','rollback'))


def failure(code, root=None, action=None):
    prefix='sudo misbot'+(' --directory '+shlex.quote(str(root)) if root else '')
    return {'stage':'federation','status':'error','code':code,
            'explanation': 'Операция не подтверждена. Проверьте status и существующие checkpoints; неверный ввод исправьте по справке.',
            'next_action':prefix+' federation '+({'add':'add --help','remove':'remove --help','check':'check --help','invite':'invite --help'}.get(action,'help')),
            'status_command':prefix+' status'}


def directory(root): return config.directory(Path(root)/'data/federation')


def setup(root, metadata, preview=False):
    folder=directory(root); key=folder/'identity.pem'; path=folder/'state.json'
    if metadata.get('federation_seeded') and (not key.is_file() or not path.is_file()):
        raise RuntimeError('Federation identity is missing; restore its private backup, do not regenerate')
    if path.exists() and not key.is_file():
        raise RuntimeError('Saved federation state has no private key; restore its backup')
    if not key.exists(): state_store.create_rsa_key(key)
    if not path.exists(): state_store.write_json(path,{'format':1,'instance_id':secrets.token_hex(16),
        'server_name':metadata['domain'],'requests':{},'peers':{},'nonces':{},'events':{}})
    saved=config.read_private_json(path)
    if saved.get('format')!=1 or saved.get('server_name')!=metadata['domain']: raise RuntimeError('Foreign federation identity')
    metadata['federation_seeded']=True
    # Socket parents are traversable only in the container mounts; state is not mounted.
    run=Path(root)/'run/federation';run.mkdir(parents=True,exist_ok=True);run.chmod(0o755)
    return saved


class Backend:
    def __init__(self, root, transport=None, admin=None, apply=None, admission=True):
        self.root=Path(root); self.transport=transport or protocol.https
        self.admin=admin or self.check_admin; self.applier=apply or self.apply_policy
        self.admission=admission

    @property
    def path(self): return self.root/'data/federation/state.json'
    @property
    def key(self): return self.root/'data/federation/identity.pem'
    def load(self):
        data=config.read_private_json(self.path)
        if data.get('format')!=1 or data.get('server_name')!=config.read_private_json(self.root/'installation.json')['domain']:
            raise protocol.ProtocolError('identity_mismatch')
        return data
    def save(self, data): state_store.write_json(self.path,data)
    def guard(self): return state_store.operation_guard(self.root)
    def auto_accept(self): return config.load(self.root).get('FEDERATION_AUTO_ACCEPT','false')=='true'
    def matrix_operation(self, action, argument):
        from federation_runtime import matrix_operation
        return matrix_operation(self.root,action,argument)
    def restart_scope(self):return 'synapse and web'
    def doc(self, data=None):
        data=data or self.load()
        cfg=self.root/'config/federation-bot.json'
        mxid=json.loads(cfg.read_text()).get('user_id') if cfg.exists() else None
        return protocol.document(data['server_name'],data['instance_id'],self.key,mxid)

    def ready(self, recovery=False):
        if not self.admission: return
        state_store.assert_update_allowed(self.root,allow_federation=recovery,allow_configuration=recovery)
        metadata=config.read_private_json(self.root/'installation.json')
        if any(metadata.get(k) for k in ('restore_pending','migration_frozen','credential_transition_pending','media_transition_pending')):
            raise protocol.ProtocolError('installation_pending')
        if (self.root/'federation-transition.json').exists() and not recovery:
            raise RuntimeError('Federation apply needs recovery: sudo misbot --directory '+shlex.quote(str(self.root))+' federation recover')
        lifecycle.configure(self.root,'federation');lifecycle.ownership_check()

    def check_admin(self, actor):
        # CLI/root approval is local and never accepted from HTTP or bot payloads.
        if actor=='root': return os.geteuid()==0
        domain=self.load()['server_name'];suffix=':'+domain
        if not isinstance(actor,str) or not actor.startswith('@') or not actor.endswith(suffix): return False
        name=actor[1:-len(suffix)]
        if not name or ':' in name or len(name)>255: return False
        import registration
        try:
            payload={'client':config.read_private_json(self.root/'secrets/referral-client.json'),
                'username':name}
            result=json.loads(lifecycle.dc('exec','-T','synapse','python','-c',
                (self.root/'federation_admin_api.py').read_text(),stdin=json.dumps(payload)))
            return result=={'authorized':True}
        except (RuntimeError,OSError,ValueError): return False

    def authorize(self, actor):
        if not self.admin(actor): raise protocol.ProtocolError('admin_unconfirmed')

    def servers(self): return config.load(self.root)['FEDERATION_SERVERS'].split(',')

    def policy_plan(self, server, add):
        server=schema.domain(server)
        before=self.servers();own=self.load()['server_name']
        if not add and server==own: raise protocol.ProtocolError('own_identity_preserved')
        after=list(dict.fromkeys([*before,server])) if add else [x for x in before if x!=server]
        after=after or [own]  # never replace a restrictive policy by no whitelist
        return {'before':before,'after':after,'backup':'checked consistent copy',
                'restart':self.restart_scope() if before!=after else 'none','data':'identity, databases, media and keys preserved'}

    def change(self, server, add, actor):
        self.authorize(actor);plan=self.policy_plan(server,add);before=plan['before'];after=plan['after']
        if before==after: return {'status':'unchanged','servers':before,'plan':plan}
        if self.admission:lifecycle.emit('federation_policy_plan',**plan)
        if self.applier==self.apply_policy:self.applier(after,actor=actor)
        else:self.applier(after)
        return {'status':'applied','servers':after,'plan':plan,'restart':self.restart_scope(),'federation_exchange':'not_verified'}

    def apply_policy(self, servers, actor='root'):
        import yaml
        values=config.load(self.root,True); proposed=dict(values);proposed['FEDERATION_SERVERS']=','.join(servers)
        schema.validate(proposed)
        lifecycle.backup();lifecycle.restore_check()
        # Rights may be revoked while the checked backup restarts MAS/Synapse.
        # Recheck after that pause, before publishing any policy files.
        self.authorize(actor)
        stage=config.directory(self.root/'stages/federation'/secrets.token_hex(16))
        config.snapshot(self.root,stage)
        for name in ('homeserver.yaml',): (stage/name).write_bytes((self.root/'config'/name).read_bytes());(stage/name).chmod(0o600)
        state_store.write_json(stage/'web-config.json',json.loads((self.root/'public/web-config.json').read_text()))
        journal={'format':1,'stage':str(stage.relative_to(self.root)),'servers':servers,'phase':'prepared'}
        state_store.write_json(self.root/'federation-transition.json',journal)
        self.recover_policy()

    def recover_policy(self, rollback=False):
        import yaml
        path=self.root/'federation-transition.json'
        if not path.exists(): return {'status':'unchanged'}
        journal=config.read_private_json(path)
        if journal.get('format')!=1 or not re.fullmatch('stages/federation/[0-9a-f]{32}',journal.get('stage','')):
            raise protocol.ProtocolError('invalid_checkpoint')
        stage=self.root/journal['stage'];servers=[schema.domain(v) for v in journal['servers']]
        if not servers: raise protocol.ProtocolError('empty_policy')
        # Interrupted split publication is resolved before touching generated runtime.
        if (self.root/'configuration-transition.json').exists(): config.recover(self.root,rollback=rollback)
        if rollback:
            config.restore_snapshot(self.root,stage)
            state_store.write_text(self.root/'public/web-config.json',json.dumps(config.read_private_json(stage/'web-config.json'),ensure_ascii=False,indent=2)+'\n',0o644)
            state_store.write_text(self.root/'config/homeserver.yaml',(stage/'homeserver.yaml').read_text())
        else:
            model=config.load(self.root,True);model['FEDERATION_SERVERS']=','.join(servers);config.write_model(self.root,model)
            synapse=yaml.safe_load((stage/'homeserver.yaml').read_text())
            if synapse.get('server_name')!=model['DOMAIN']: raise protocol.ProtocolError('identity_mismatch')
            synapse['federation_domain_whitelist']=servers
            state_store.write_text(self.root/'config/homeserver.yaml',yaml.safe_dump(synapse,sort_keys=False))
            web=config.read_private_json(stage/'web-config.json');web.setdefault('room_directory',{})['servers']=list(dict.fromkeys([model['DOMAIN'],*servers]))
            state_store.write_text(self.root/'public/web-config.json',json.dumps(web,ensure_ascii=False,indent=2)+'\n',0o644)
        # Atomic publication changes the inode of a single-file bind mount.
        # Restart alone keeps the old inode; recreate only these two services.
        lifecycle.dc('up','-d','--no-deps','--force-recreate','synapse','web')
        expected={'server_name':config.read_private_json(self.root/'installation.json')['domain'],
                  'servers':yaml.safe_load((self.root/'config/homeserver.yaml').read_text()).get('federation_domain_whitelist'),
                  'directory':json.loads((self.root/'public/web-config.json').read_text())['room_directory']}
        probe="import json,sys,yaml,urllib.request;from pathlib import Path;e=json.load(sys.stdin);c=yaml.safe_load(Path('/config/homeserver.yaml').read_text());assert c['server_name']==e['server_name'];assert c.get('federation_domain_whitelist')==e['servers'];urllib.request.urlopen('http://localhost:8008/_matrix/client/versions',timeout=3).read();w=json.load(urllib.request.urlopen('http://web:8080/config.json',timeout=3));assert w['room_directory']==e['directory']"
        lifecycle.wait(lambda:lifecycle.dc('exec','-T','synapse','python','-c',
            probe,stdin=json.dumps(expected)) is not None,service='synapse and web policy')
        if not rollback and yaml.safe_load((self.root/'config/homeserver.yaml').read_text()).get('federation_domain_whitelist')!=servers:
            raise protocol.ProtocolError('policy_result_mismatch')
        path.unlink();return {'status':'rolled_back' if rollback else 'applied'}

    def request(self, data, rid):
        if not isinstance(rid,str) or not REQUEST.fullmatch(rid): raise protocol.ProtocolError('invalid_request_id')
        item=data['requests'].get(rid)
        if item is None: raise protocol.ProtocolError('request_not_found')
        if item['expires_at']<int(time.time()) and item['state'] not in ('connected',*TERMINAL):
            item['state']='expired';self.save(data)
        return item

    def view(self, item):
        return {k:item[k] for k in ('id','server','direction','state','expires_at','local_applied','remote_applied')}

    def peer(self, data, server):
        doc=protocol.discover(server,self.transport)
        old=data['peers'].get(server)
        if old and any(old['document'][k]!=doc[k] for k in ('instance_id','key_id','bot_mxid')):
            raise protocol.ProtocolError('peer_identity_changed')
        return doc

    def send(self, data, item, action):
        body={'action':action,'request_id':item['id'],'expires_at':item['expires_at'],
              'approved':bool(item.get('local_approval')),'applied':item['local_applied']}
        remote=item['document'];message=protocol.envelope(self.doc(data),self.key,remote,body)
        response=self.transport(item['server'],protocol.API,message)
        # Responses are bound to this exact nonce, recipient and request id.
        value=protocol.authenticate(response,self.doc(data),remote,{})
        reply=value['body']
        if reply.get('reply_to')!=message['payload']['nonce'] or reply.get('request_id')!=item['id']:
            raise protocol.ProtocolError('invalid_response')
        if reply.get('approved'): item['remote_approval']=True
        item['remote_applied']=bool(reply.get('applied'))
        if reply.get('state') in TERMINAL: item['state']=reply['state']
        self.save(data);return reply

    def local_apply(self, data, item):
        if item['state'] in TERMINAL or not item.get('local_approval') or not item.get('remote_approval'): return
        # Revocation/blocked status is rechecked before deferred execution.
        self.authorize(item['actor'])
        if not item['local_applied']:
            item['state']='applying';self.save(data)
            self.change(item['server'],True,item['actor'])
            item['local_applied']=True;item['state']='partially_applied';self.save(data)
        if item['remote_applied']:
            item['state']='connected';data['peers'][item['server']]={'document':item['document'],'approved':True,'request_id':item['id']}
            self.save(data)

    def invite(self, data, server, actor):
        server=schema.domain(server)
        if server==data['server_name']: raise protocol.ProtocolError('self_invitation')
        for old in data['requests'].values():
            if old['server']==server and old['direction']=='outgoing' and self.request(data,old['id'])['state'] not in TERMINAL:
                return self.retry(data,old['id'])
        doc=self.peer(data,server);rid='MB-'+secrets.token_hex(16)
        item={'id':rid,'server':server,'document':doc,'direction':'outgoing','actor':actor,
              'expires_at':int(time.time())+604800,'state':'pending','local_approval':True,
              'remote_approval':False,'local_applied':False,'remote_applied':False,
              'entry_preexisted':server in self.servers()}
        data['requests'][rid]=item;self.save(data)
        return self.retry(data,rid)

    def retry(self, data, rid):
        item=self.request(data,rid)
        if item['state'] in TERMINAL:
            # Retry cancellation/rejection delivery after a lost response.
            if item.get('delivery_pending'):
                self.send(data,item,item['state']);item.pop('delivery_pending',None);self.save(data)
            return self.view(item)
        try:
            self.send(data,item,'invite' if item['direction']=='outgoing' and not item.get('remote_approval') else 'status')
            self.local_apply(data,item)
            if item.get('local_approval') and item.get('remote_approval'):
                self.send(data,item,'decision');self.local_apply(data,item)
        except protocol.ProtocolError as error:
            item['safe_error']=error.code;self.save(data)
            return {**self.view(item),'code':error.code,'next_action':'sudo misbot federation requests retry --request '+rid}
        return self.view(item)

    def receive(self, message):
        with self.guard():
            self.ready();data=self.load();own=self.doc(data)
            try:
                sender=message['payload']['sender']
                if not isinstance(sender,str):raise ValueError()
                server=schema.domain(sender)
            except (ValueError,KeyError,TypeError): raise protocol.ProtocolError('invalid_request') from None
            remote=self.peer(data,server)
            data['nonces']={k:v for k,v in data['nonces'].items() if v>time.time()-600}
            value=protocol.authenticate(message,own,remote,data['nonces'])
            body=value['body']
            if not isinstance(body,dict) or set(body)-{'action','request_id','expires_at','approved','applied'}: raise protocol.ProtocolError('invalid_request')
            rid=body.get('request_id');action=body.get('action')
            if not isinstance(rid,str) or not REQUEST.fullmatch(rid): raise protocol.ProtocolError('invalid_request_id')
            if action not in ('invite','status','decision','cancelled','rejected'): raise protocol.ProtocolError('remote_operation_denied')
            if type(body.get('approved')) is not bool or type(body.get('applied')) is not bool: raise protocol.ProtocolError('invalid_request')
            data['nonces'][value['nonce']]=int(time.time());self.save(data)
            item=data['requests'].get(rid)
            if action=='invite' and item is None:
                expiration=body.get('expires_at')
                # The seven-day lifetime starts at the signed sender timestamp;
                # its already-validated clock skew must not reject a valid TTL.
                if type(expiration) is not int or not time.time()<expiration<=value['timestamp']+604800: raise protocol.ProtocolError('invalid_expiration')
                if len(data['requests'])>=1000: raise protocol.ProtocolError('request_capacity')
                item={'id':rid,'server':server,'document':remote,'direction':'incoming','actor':None,
                    'expires_at':expiration,'state':'pending','local_approval':False,
                    'remote_approval':body['approved'],'local_applied':False,'remote_applied':False,
                    'entry_preexisted':server in self.servers()}
                data['requests'][rid]=item;self.save(data)
            item=self.request(data,rid)
            if item['server']!=server or item['document']['instance_id']!=remote['instance_id'] or item['document']['key_id']!=remote['key_id']:
                raise protocol.ProtocolError('request_owner')
            if action=='rejected' and item['direction']!='outgoing': raise protocol.ProtocolError('invalid_transition')
            if item['state'] not in TERMINAL:
                if action in ('cancelled','rejected'):
                    # Removing policy is a SEPARATE local admin operation, never implicit rollback.
                    item['state']=action
                else:
                    if body['approved']: item['remote_approval']=True
                    item['remote_applied']=body['applied']
                    self.save(data);self.local_apply(data,item)
            self.save(data)
            reply={'request_id':rid,'reply_to':value['nonce'],'state':item['state'],
                   'approved':bool(item.get('local_approval')),'applied':item['local_applied']}
            return protocol.envelope(own,self.key,remote,reply)

    def operate(self, action, argument=None, actor='root', event_id=None):
        if action not in ACTIONS: raise protocol.ProtocolError('unknown_operation')
        with self.guard():
            self.ready(action in ('recover','rollback','operations','requests','request-status','peers','servers'));self.authorize(actor);data=self.load()
            if event_id:
                if not isinstance(event_id,str) or len(event_id)>255: raise protocol.ProtocolError('invalid_event')
                digest=hashlib.sha256(event_id.encode()).hexdigest()
                if digest in data['events']: return {'status':'already_processed'}
                # Persist before side effects: replay can only use explicit retry/recovery.
                if len(data['events'])>=100000: raise protocol.ProtocolError('event_capacity')
                data['events'][digest]={'action':action,'time':int(time.time())};self.save(data)
            if action=='servers': return {'servers':self.servers()}
            if action in ('add','remove'): return self.change(argument,action=='add',actor)
            if action in ('recover','rollback'): return self.recover_policy(action=='rollback')
            if action=='check':
                server=schema.domain(argument);result={'server':server,'input':'valid','policy':server in self.servers(),'federation_exchange':'not_verified'}
                try:
                    discovery=self.transport(server,'/.well-known/matrix/server')
                    target=discovery['m.server'];host,separator,port=target.partition(':')
                    if separator and port!='443':raise protocol.ProtocolError('matrix_discovery_port_unsupported')
                    host=schema.domain(host);self.transport(host,'/_matrix/federation/v1/version')
                    result.update(matrix_discovery='available',matrix_https='available')
                except protocol.ProtocolError as error:result.update(matrix_discovery='unconfirmed',matrix_code=error.code)
                except (KeyError,TypeError,ValueError):result.update(matrix_discovery='unconfirmed',matrix_code='invalid_matrix_discovery')
                try: result.update(misbot_discovery=self.peer(data,server),https='available')
                except protocol.ProtocolError as error: result.update(https='unconfirmed',code=error.code)
                return result
            if action=='invite': return self.invite(data,argument,actor)
            if action=='requests': return {'requests':[self.view(self.request(data,k)) for k in data['requests']]}
            if action=='operations':return {'operations':[{'id':k,'state':v['state'],'result':v.get('result',{})} for k,v in data.get('jobs',{}).items()]}
            if action=='peers': return {'peers':[{'server':s,'bot_mxid':p['document']['bot_mxid'],'approved':p['approved'],
                'state':data['requests'].get(p['request_id'],{}).get('state','unconfirmed')} for s,p in data['peers'].items()]}
            if action in ('approve','reject','cancel','retry','request-status'):
                item=self.request(data,argument)
                if action=='request-status':return self.view(item)
                if action=='retry':return self.retry(data,argument)
                if action=='approve':
                    if item['direction']!='incoming' or item['state'] in TERMINAL: raise protocol.ProtocolError('invalid_transition')
                    item.update(local_approval=True,actor=actor,state='accepted');self.save(data)
                    return self.retry(data,argument)
                if action=='reject' and item['direction']!='incoming': raise protocol.ProtocolError('invalid_transition')
                item.update(state='rejected' if action=='reject' else 'cancelled',delivery_pending=True);self.save(data)
                return self.retry(data,argument)
            raise protocol.ProtocolError('unknown_operation')


def backend(root, **kwargs):
    if json.loads((Path(root)/'installation.json').read_text()).get('backend')=='existing-compose':
        from existing_features import Federation
        return Federation(root, **kwargs)
    return Backend(root, **kwargs)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('action',choices=sorted(ACTIONS));parser.add_argument('argument',nargs='?');args=parser.parse_args()
    try:
        state_store.require_root();__import__('command_wrapper').trusted_installation(args.directory)
        result=backend(args.directory).operate(args.action,args.argument)
        print(json.dumps(result,ensure_ascii=False));return 1 if result.get('code') else 0
    except protocol.ProtocolError as error: print(json.dumps(failure(error.code,args.directory,args.action),ensure_ascii=False));return 1
    except RuntimeError as error: print(str(error),file=sys.stderr);return 1
    except (OSError,ValueError,KeyError,TypeError): print(json.dumps(failure('local_state_or_input',args.directory,args.action),ensure_ascii=False));return 1
    except KeyboardInterrupt: return 130


if __name__=='__main__': raise SystemExit(main())
