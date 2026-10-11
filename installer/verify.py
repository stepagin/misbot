#!/usr/bin/env python3
"""Public protocol checks, using validated installation metadata. No account secrets."""
import argparse
import json
from pathlib import Path
import socket
import ssl
import sys
import urllib.error
import urllib.request

parser=argparse.ArgumentParser()
parser.add_argument('--directory',required=True)
args=parser.parse_args()
root=Path(args.directory)
metadata=json.loads((root/'installation.json').read_text())
domain=metadata['domain']
import configuration,configuration_schema
model=configuration.load(root,True)
configuration.assert_auth_identity(root,model,metadata)
mas='https://'+configuration_schema.mas_host(model)
tests=[
    ('discovery','https://'+domain+'/.well-known/matrix/client',200),
    ('federation','https://'+domain+'/.well-known/matrix/server',200),
    ('synapse','https://matrix.'+domain+'/_matrix/client/versions',200),
    ('oauth_metadata','https://matrix.'+domain+'/_matrix/client/v1/auth_metadata',200),
    ('mas_discovery',mas+'/.well-known/openid-configuration',200),
    ('mas_health',mas+'/health',200),
    ('web','https://web.'+domain+'/',200),
    ('admin','https://admin.'+domain+'/',200),
    ('rtc_jwt','https://rtc.'+domain+'/livekit/jwt/healthz',200),
    ('legacy_login_disabled','https://matrix.'+domain+'/_matrix/client/v3/login',404),
    ('synapse_admin_protected','https://matrix.'+domain+'/_synapse/admin/v2/users',401),
    ('mas_admin_protected',mas+'/api/admin/v1/users',401),
]
failed=0
try:
    import configuration,homepage
    result=homepage.probe(configuration.load(root,True))
    print(json.dumps({'check':'homepage_routes','passed':True,**result}),flush=True)
except Exception as error:
    print(json.dumps({'check':'homepage_routes','passed':False,'error_type':type(error).__name__}),flush=True)
    failed+=1
for name,url,expected in tests:
    try:
        headers={'Authorization':'Bearer invalid'} if name=='mas_admin_protected' else {}
        try:response=urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=20)
        except urllib.error.HTTPError as error:response=error
        with response:status,body=response.code,response.read()
        passed=status==expected
        if name in ['oauth_metadata','mas_discovery'] and passed:
            document=json.loads(body)
            passed=(document['issuer']==mas+'/' and document['token_endpoint']==mas+'/oauth2/token'
                    and document['authorization_endpoint']==mas+'/authorize')
        if name=='discovery' and passed:
            document=json.loads(body)
            passed=(document['m.homeserver']['base_url']=='https://matrix.'+domain
                and document['org.matrix.msc2965.authentication']['issuer']==mas+'/')
        if name=='federation' and passed:passed=json.loads(body)['m.server']=='matrix.'+domain+':443'
        print(json.dumps({'check':name,'status':status,'expected':expected,'passed':passed}),flush=True)
        failed+=not passed
    except Exception as error:
        print(json.dumps({'check':name,'passed':False,'error_type':type(error).__name__}),flush=True)
        failed+=1
try:
    host='turn.'+domain
    with socket.create_connection((host,443),timeout=15) as raw:
        with ssl.create_default_context().wrap_socket(raw,server_hostname=host) as tls:
            print(json.dumps({'check':'turn_tls','passed':True,'tls':tls.version()}),flush=True)
except Exception as error:
    print(json.dumps({'check':'turn_tls','passed':False,'error_type':type(error).__name__}),flush=True)
    failed+=1
print(json.dumps({'check':'verification_summary','failures':failed}),flush=True)
sys.exit(bool(failed))
