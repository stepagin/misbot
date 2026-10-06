"""Bounded S3 probe with safe structured results and owned-object cleanup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import ssl
import stat
import threading
from urllib.parse import urlsplit
import urllib.parse
import urllib.request
import urllib.error
import uuid

import media_transfer
import s3_config
import state_store

NAMESPACE='.misbot-probes/v1/'
KNOWN={'InvalidAccessKeyId','InvalidToken','ExpiredToken','SignatureDoesNotMatch',
       'AuthorizationHeaderMalformed','InvalidRegion','PermanentRedirect','RequestTimeTooSkewed',
       'RequestExpired','AccessDenied','Forbidden','NoSuchBucket','NoSuchKey','NoSuchVersion','NotFound',
       'SlowDown','ServiceUnavailable','InternalError','RequestTimeout','PreconditionFailed',
       'ConditionalRequestConflict','NotImplemented','InvalidRequest','MethodNotAllowed',
       '400','401','403','404','409','412','429','500','502','503','504'}


class Failure(RuntimeError):
    def __init__(self,code,message):super().__init__(message);self.code=code


def explanation(error,stage):
    if isinstance(error,(s3_config.ConfigError,Failure)):return error.code,str(error)
    if isinstance(error,ModuleNotFoundError):return 'dependency_missing','Не установлен SDK. На Ubuntu/Debian выполните sudo apt-get install python3-boto3 и повторите проверку.'
    if isinstance(error,socket.gaierror):return 'dns_failed','Имя endpoint не разрешается. Проверьте S3_ENDPOINT_URL, DNS resolver и сетевой доступ.'
    if isinstance(error,(ssl.SSLError,ssl.CertificateError)) or type(error).__name__=='SSLError':
        return 'tls_failed','TLS не прошёл проверку. Проверьте hostname, цепочку сертификатов и доверенный CA; не отключайте проверку TLS.'
    if isinstance(error,(TimeoutError,socket.timeout)) or type(error).__name__ in ('ConnectTimeoutError','ReadTimeoutError'):
        return 'timeout','Истекло время ожидания. Проверьте endpoint, DNS, firewall и доступность провайдера; повторите позже.'
    if isinstance(error,(ConnectionError,ConnectionRefusedError)) or type(error).__name__=='EndpointConnectionError' or (stage=='connection' and isinstance(error,OSError)):
        return 'connection_failed','Соединение не установлено. Проверьте endpoint, порт, routing/firewall и доступность провайдера.'
    response=getattr(error,'response',{})
    code=response.get('Error',{}).get('Code','') if isinstance(response,dict) else ''
    status=response.get('ResponseMetadata',{}).get('HTTPStatusCode') if isinstance(response,dict) else None
    safe=code if code in KNOWN else ('http_'+str(status) if type(status)==int and 100<=status<=599 else 'sdk_error')
    if code in ('InvalidAccessKeyId','InvalidToken','ExpiredToken'):
        return safe,'Провайдер отверг credentials. Проверьте активную пару access/secret key приватно; временные session credentials этим форматом не поддерживаются.'
    if code=='SignatureDoesNotMatch':return safe,'Подпись не совпала. Проверьте secret key, S3_REGION у провайдера, endpoint и прокси; это не однозначное доказательство неверного пароля.'
    if code in ('AuthorizationHeaderMalformed','InvalidRegion','PermanentRedirect'):
        return safe,'Проверьте S3_REGION и endpoint по документации провайдера. Region используется в SigV4; автоматическая подмена не выполняется.'
    if code in ('RequestTimeTooSkewed','RequestExpired'):
        return safe,'Время системы не подходит для подписи. Проверьте timedatectl status и синхронизацию времени; затем повторите.'
    if code in ('SlowDown','ServiceUnavailable','InternalError','RequestTimeout','429','500','502','503','504') or status in (429,500,502,503,504):
        return safe,'Временный отказ провайдера. Сохраните config/catalog и повторите позже; не меняйте credentials автоматически.'
    if code in ('PreconditionFailed','ConditionalRequestConflict','412','409'):
        return safe,'Условная запись не выполнена; существующий объект не заменялся. Повторите проверку: будет создан новый уникальный probe.'
    if stage in ('authentication','bucket') and (code in ('AccessDenied','Forbidden','NoSuchBucket','NotFound','400','403','404') or status in (400,403,404)):
        return safe,'Bucket отсутствует либо нет доступа к нему: HEAD не всегда позволяет различить эти причины. Проверьте S3_BUCKET, endpoint и ListBucket/prefix permissions; HTTP 403 не означает автоматически неверные credentials.'
    if stage in ('read','cleanup') and (code in ('AccessDenied','Forbidden','403') or status==403):
        permission='GetObject для проверки владения и DeleteObject/DeleteObjectVersion' if stage=='cleanup' else 'GetObject'
        return safe,'Недостаточно прав '+permission+' на собственный probe namespace; текущие медиа и копии не изменялись.'
    if stage=='write' and (code in ('AccessDenied','Forbidden','403') or status==403):
        return safe,'Недостаточно прав PutObject на собственный probe namespace. Проверьте prefix policy; аутентификация могла пройти.'
    if code in ('NotImplemented','MethodNotAllowed','InvalidRequest'):
        return safe,'Провайдер не поддержал требуемую операцию или условную запись. Проверьте совместимость S3 API; небезопасный режим перезаписи не используется.'
    return safe,'Ответ SDK/HTTP не позволяет установить причину. Проверьте параметры и права этапа, статус провайдера; повторите проверку. Сообщение и тело ответа намеренно не выводятся.'


def network(endpoint,emit):
    parsed=urlsplit(endpoint)
    if parsed.scheme!='https' or not parsed.hostname:raise Failure('endpoint_format','SDK endpoint должен использовать HTTPS.')
    emit('dns','running','dns_started','Проверка разрешения имени endpoint.')
    addresses=[];errors=[];done=threading.Event()
    def resolve():
        try:addresses.extend(socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM))
        except BaseException as error:errors.append(error)
        finally:done.set()
    threading.Thread(target=resolve,daemon=True).start()
    if not done.wait(8):raise TimeoutError()
    if errors:raise errors[0]
    if not addresses:raise socket.gaierror()
    emit('dns','passed','dns_resolved','Имя endpoint разрешено; это не доказывает доступ к S3.')
    emit('connection','running','connection_started','Проверка соединения с endpoint.')
    failure=None
    for family,kind,protocol,_,address in addresses:
        connection=socket.socket(family,kind,protocol);connection.settimeout(8)
        try:connection.connect(address);break
        except OSError as error:failure=error;connection.close()
    else:raise failure or ConnectionError()
    emit('connection','passed','tcp_connected','TCP соединение установлено.')
    emit('tls','running','tls_started','Проверка сертификата endpoint.')
    try:
        context=ssl.create_default_context(cafile=os.environ.get('AWS_CA_BUNDLE') or None)
        with context.wrap_socket(connection,server_hostname=parsed.hostname):pass
    finally:connection.close()
    emit('tls','passed','tls_verified','Сертификат и hostname проверены.')


def save_catalog(path,record):
    state_store.write_json(path,record)


def private_catalog(path):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd) as incoming:
        info=os.fstat(incoming.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077 or info.st_nlink!=1 or info.st_size>65536):
            raise Failure('catalog_permissions','Catalog должен быть приватным обычным файлом владельца root, mode 0600.')
        record=json.load(incoming)
    if (record.get('format')!=1 or record.get('family')!='misbot-s3-probe' or
            not isinstance(record.get('id'),str) or len(record['id'])!=32 or
            any(c not in '0123456789abcdef' for c in record['id']) or
            record.get('purpose') not in ('media','backup') or record.get('size')!=256 or
            not isinstance(record.get('sha256'),str) or len(record['sha256'])!=64 or
            any(c not in '0123456789abcdef' for c in record['sha256'])):
        raise Failure('catalog_invalid','Неизвестный probe catalog; никакие объекты не удалены.')
    return record


def owned_response(service,values,record):
    args={'Bucket':values['S3_BUCKET'],'Key':record['key']}
    if record.get('version') is not None:args['VersionId']=record['version']
    response=service.get_object(**args)
    body=response['Body']
    try:
        # No arbitrary user object is ever read beyond one bounded probe payload.
        data=body.read(4097)
        original_metadata=response.get('Metadata',{})
        metadata={key.lower():value for key,value in original_metadata.items()}
        if (len(data)!=record['size'] or hashlib.sha256(data).hexdigest()!=record['sha256'] or
                len(metadata)!=len(original_metadata) or metadata.get('misbot-probe')!=record['id']):
            raise Failure('probe_ownership_mismatch','Удаление запрещено: объект не совпадает с собственным probe. Сохраните catalog и проверьте его приватно.')
    finally:body.close()
    return response


def clean(service,values,record,path):
    expected=values['S3_PREFIX']+NAMESPACE+record['id']
    if record.get('identity')!=media_transfer.identity(values) or record.get('key')!=expected:
        raise Failure('catalog_destination_mismatch','Catalog относится к другому назначению. Используйте прежний private config; ничего не удалено.')
    if record.get('state')=='cleaned':return
    try:response=owned_response(service,values,record)
    except Exception as error:
        code=getattr(error,'response',{}).get('Error',{}).get('Code')
        if code in ('NoSuchKey','NoSuchVersion','404','NotFound'):
            record['state']='cleaned';save_catalog(path,record);return
        raise
    version=record.get('version')
    if version is None:version=response.get('VersionId')
    if version is not None:
        if not isinstance(version,str) or not version or len(version)>2048:
            raise Failure('version_invalid','Провайдер вернул неподдерживаемый VersionId; catalog сохранён, удаления не было.')
        record['version']=version;save_catalog(path,record)
    args={'Bucket':values['S3_BUCKET'],'Key':record['key']}
    if version is not None:args['VersionId']=version
    service.delete_object(**args)
    # Check exactly the owned version/current key, without listing any objects.
    try:
        service.get_object(**args)['Body'].close()
    except Exception as error:
        if getattr(error,'response',{}).get('Error',{}).get('Code') not in ('NoSuchKey','NoSuchVersion','404','NotFound'):raise
    else:raise Failure('cleanup_unconfirmed','Удаление не подтверждено. Проверьте retention/Object Lock и повторите cleanup по catalog.')
    record['state']='cleaned';save_catalog(path,record)


def conditional_put(service,values,record,payload):
    args={'Bucket':values['S3_BUCKET'],'Key':record['key'],'Body':payload,
          'Metadata':{'misbot-probe':record['id'],'sha256':record['sha256']}}
    model=service.meta.service_model.operation_model('PutObject').input_shape.members
    if 'IfNoneMatch' in model:args['IfNoneMatch']='*'
    else:
        # Older distro SDK: attach the same conditional header before signing.
        def condition(request,**kwargs):request.headers['If-None-Match']='*'
        service.meta.events.register('before-sign.s3.PutObject',condition)
    return service.put_object(**args)


def private_backup_probe(service,values,record):
    """Same anonymous denial requirement as external backups; only our probe."""
    url=service.meta.endpoint_url.rstrip('/')+'/'+urllib.parse.quote(values['S3_BUCKET'],safe='')+'/'+urllib.parse.quote(record['key'],safe='/')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(NoRedirect(),urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url,method='GET'),timeout=15):
            raise Failure('backup_public_access','Собственный backup probe доступен без credentials. Исправьте private bucket/prefix policy до внешних копий; текущие данные не менялись.')
    except urllib.error.HTTPError as error:
        if error.code in (403,404):return
        raise Failure('backup_privacy_unconfirmed','Приватность внешней копии не подтверждена. Проверьте bucket policy и доступность endpoint, затем повторите.') from None
    except urllib.error.URLError as error:
        if isinstance(error.reason,ssl.SSLError):raise error.reason from None
        if isinstance(error.reason,TimeoutError):raise error.reason from None
        raise Failure('backup_privacy_unconfirmed','Анонимная HTTPS-проверка собственного probe не завершена; проверьте endpoint/DNS/TLS и повторите.') from None


def next_actions(config,purpose,directory=None,read_only=False):
    retry=entry_command()+' storage s3 check --config '+shlex.quote(str(config))+' --purpose '+purpose+(' --read-only' if read_only else '')
    if directory is not None:retry+=' --directory '+shlex.quote(str(directory))
    actions=[retry,'Исправьте private config и повторите; можно выйти сейчас с сохранением настроек.']
    if purpose=='media':
        metadata={}
        if directory is not None:
            try:metadata=json.loads((Path(directory)/'installation.json').read_text())
            except (OSError,ValueError):pass
        if directory is not None and not metadata.get('storage_committed') and metadata.get('media_mode','local')=='local':
            actions+=['sudo misbot --directory '+shlex.quote(str(directory))+' storage local',
                      'sudo misbot --directory '+shlex.quote(str(directory))+' start']
        elif directory is None:actions+=['До настройки: sudo python3 misbot.py config init --domain example.org --directory /opt/matrix-stack; defaults используют локальные медиа.']
        actions+=['Позднее подключение: sudo misbot storage s3 connect --config '+shlex.quote(str(config))+((' --directory '+shlex.quote(str(directory))) if directory is not None else '')]
        if metadata.get('storage_committed') or metadata.get('media_mode')=='s3':
            actions+=['Сохраните действующие S3-настройки и ключи; не используйте init или пустое локальное хранилище. Смена credentials: misbot credentials s3 rotate --help.']
        for name,command in (('update-journal.json','update recover --source /root/matrix-installer'),
                ('database-transition.json','storage database recover'),('s3-credentials-transition.json','credentials s3 recover'),
                ('database-password-transition.json','credentials database recover'),('backup-writers.json','backup recover'),
                ('secrets/user-password-pending.json','users password recover')):
            if directory is not None and (Path(directory)/name).exists():actions+=['Pending: sudo misbot --directory '+shlex.quote(str(directory))+' '+command]
        if directory is not None:
            for flag,command in (('credential_transition_pending','credentials admin recover'),('media_transition_pending','storage recover')):
                if metadata.get(flag):actions+=['Pending: sudo misbot --directory '+shlex.quote(str(directory))+' '+command]
    else:actions+=['Для полной копии: misbot backup external create --help; probe не проверяет encryption/GET всего архива или восстановление БД.']
    return actions


def entry_command():
    # A bootstrap probe must not prescribe a command that is not installed yet.
    if Path('/usr/local/bin/misbot').is_file():return 'sudo misbot'
    return 'sudo /usr/bin/python3 '+shlex.quote(str(Path(__file__).resolve().parent/'misbot.py'))


def diagnose(config,purpose,directory=None,read_only=False,catalog_directory=None,service=None,transport=network,cleanup_catalog=None,privacy=private_backup_probe,progress=None):
    stages=[];caps={'read':False,'write':False,'cleanup':False}
    result={'schema':1,'operation':'s3_cleanup' if cleanup_catalog else 's3_check','purpose':purpose,
            'stages':stages,'capabilities':caps,'exit_code':1,'passed':False,'next_actions':next_actions(config,purpose,directory,read_only)}
    current='configuration';record=None;path=None;values=None
    def emit(stage,status,code,message):
        nonlocal current
        current=stage
        item={'stage':stage,'status':status,'code':code,'explanation':message,'next_actions':[]}
        stages.append(item)
        if progress:progress(item)
    try:
        values=s3_config.read_profile(config,purpose=purpose)
        emit('configuration','passed','config_valid','Файл приватный; bucket, region, credentials, endpoint и prefix проверены.')
        current='dependency'
        service=service or media_transfer.client(values)
        emit('dependency','passed','sdk_available','SDK доступен; SigV4 и проверка TLS включены.')
        current='dns'
        # Network callback updates current before the potentially failing stage.
        transport(service.meta.endpoint_url,emit)
        current='authentication'
        service.head_bucket(Bucket=values['S3_BUCKET'])
        emit('authentication','passed','signed_head_accepted','Подписанный HEAD принят; права GET/PUT ещё не подтверждены.')
        emit('bucket','passed','bucket_accessible','Bucket доступен для HEAD. Object permissions проверяются отдельно.')
        if cleanup_catalog:
            current='cleanup';path=Path(cleanup_catalog);record=private_catalog(path)
            clean(service,values,record,path);caps['cleanup']=True
            emit('cleanup','passed','probe_removed','Удаление собственной версии/текущего объекта подтверждено; неизвестные старые версии не перечислялись.')
        elif read_only:
            for stage in ('write','read','cleanup'):emit(stage,'not_checked','read_only','Режим без записи: возможность не проверялась.')
        else:
            current='write'
            parent=Path(catalog_directory or (Path.home()/'.local/state/misbot/s3-probes'))
            parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            info=parent.lstat()
            if parent.is_symlink() or parent.resolve()!=parent.absolute() or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
                raise Failure('catalog_directory_permissions','Нужен собственный каталог probe catalogs, mode 0700, без symlinks.')
            probe_id=uuid.uuid4().hex;payload=os.urandom(256)
            path=parent/(probe_id+'.json')
            record={'format':1,'family':'misbot-s3-probe','purpose':purpose,'id':probe_id,
                'identity':media_transfer.identity(values),'key':values['S3_PREFIX']+NAMESPACE+probe_id,
                'sha256':hashlib.sha256(payload).hexdigest(),'size':len(payload),'state':'attempted','version':None}
            save_catalog(path,record) # persist intent before PUT, including lost replies/SIGKILL
            response=conditional_put(service,values,record,payload)
            record.update(state='written',version=response.get('VersionId'));save_catalog(path,record)
            caps['write']=True;emit('write','passed','probe_written','Уникальный вымышленный probe записан условно; существующие объекты не заменялись.')
            current='read';owned_response(service,values,record);caps['read']=True
            emit('read','passed','probe_verified','Содержимое собственного probe совпало по размеру, metadata и SHA256.')
            if purpose=='backup':
                current='privacy';privacy(service,values,record)
                emit('privacy','passed','anonymous_access_denied','Анонимное чтение собственного backup probe запрещено.')
            current='cleanup';clean(service,values,record,path);caps['cleanup']=True
            emit('cleanup','passed','probe_removed','Удаление собственной версии/текущего объекта подтверждено; неизвестные версии не перечислялись.')
        result.update(passed=True,exit_code=0,conclusion='HEAD/TLS доступны; GET/PUT/DELETE не проверены.' if read_only else 'Проверка завершена; действующие настройки, данные и ключи не менялись.')
    except BaseException as error:
        interrupted=isinstance(error,(KeyboardInterrupt,InterruptedError,state_store.OperationInterrupted))
        code,message=('interrupted','Проверка прервана; настройки сохранены, probe catalog сохранён до подтверждённой очистки.') if interrupted else explanation(error,current)
        emit(current,'failed',code,message)
        result.update(exit_code=130 if interrupted else 1,conclusion=message)
        # After failed GET/PUT attempt, try only the exact owned probe. A lost PUT
        # response is resolved by metadata+digest, never by listing or blind delete.
        if record is not None and record.get('state')!='cleaned' and current!='cleanup':
            try:
                clean(service,values,record,path);caps['cleanup']=True
                emit('cleanup','passed','probe_removed','Собственный probe после отказа очищен.')
            except BaseException as cleanup_error:
                cleanup_code,cleanup_message=explanation(cleanup_error,'cleanup')
                emit('cleanup','failed',cleanup_code,cleanup_message)
    finally:
        if record is not None and record.get('state')!='cleaned':
            command=entry_command()+' storage s3 cleanup --config '+shlex.quote(str(config))+' --catalog '+shlex.quote(str(path))
            if directory is not None:command+=' --directory '+shlex.quote(str(directory))
            result['cleanup_pending']=True;result['next_actions'].insert(0,command)
        else:result['cleanup_pending']=False
        for item in stages:
            if item['status']=='failed':item['next_actions']=list(result['next_actions'])
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','cleanup'))
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--purpose',choices=('media','backup'))
    parser.add_argument('--catalog',type=Path)
    parser.add_argument('--directory',type=Path)
    parser.add_argument('--catalog-directory',type=Path)
    parser.add_argument('--read-only',action='store_true');parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('Use sudo -n in non-TTY or run as root; no password is requested automatically')
    if args.action=='check' and not args.purpose:parser.error('--purpose media|backup is required')
    def interrupt(signum,frame):raise state_store.OperationInterrupted('S3 probe interrupted')
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,interrupt)
    purpose=args.purpose
    if args.action=='cleanup':
        if not args.catalog:parser.error('--catalog is required')
        try:purpose=private_catalog(args.catalog).get('purpose')
        except (OSError,ValueError,RuntimeError):
            result={'schema':1,'passed':False,'exit_code':1,'stages':[{'stage':'configuration','status':'failed','code':'catalog_invalid','explanation':'Private catalog отсутствует или не прошёл проверку; ничего не удалено.','next_actions':['Проверьте путь и права catalog.']}],'capabilities':{'read':False,'write':False,'cleanup':False},'next_actions':['Проверьте путь и права catalog.'],'conclusion':'Очистка не выполнена.'}
        else:result=diagnose(args.config,purpose,args.directory,cleanup_catalog=args.catalog)
    else:result=diagnose(args.config,purpose,args.directory,args.read_only,args.catalog_directory)
    if args.json:print(json.dumps(result,ensure_ascii=False))
    else:
        for item in result['stages']:print(item['stage']+': '+item['status']+' ['+item['code']+'] — '+item['explanation'])
        print('\nИтог: '+result['conclusion'])
        print('GET: '+str(result['capabilities']['read'])+'; PUT: '+str(result['capabilities']['write'])+'; cleanup: '+str(result['capabilities']['cleanup']))
        print('\nДальше:\n'+'\n'.join(result['next_actions']))
    return result['exit_code']


if __name__=='__main__':raise SystemExit(main())
