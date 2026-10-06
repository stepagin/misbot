"""Disposable integration fixture only: local HTTP replaces TLS inside an isolated network."""
import http.cookiejar,json,urllib.parse,urllib.request,urllib.error,secrets,base64,hashlib
from html.parser import HTMLParser
BASE='http://mas:8080'
CALLBACK='https://web.bootstrap-test.invalid/check'
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**kw):return None
jar=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),NoRedirect())
def request(url,data=None,headers=None):
    parts=urllib.parse.urlsplit(url)
    if parts.netloc and parts.netloc not in ['mas:8080','mas.bootstrap-test.invalid','synapse:8008']:
        raise RuntimeError('Unexpected fixture origin')
    if parts.netloc=='mas.bootstrap-test.invalid':url=BASE+parts.path+('?' + parts.query if parts.query else '')
    if not parts.netloc:url=urllib.parse.urljoin(BASE,url)
    try:r=op.open(urllib.request.Request(url,data=data,headers=headers or {}),timeout=15)
    except urllib.error.HTTPError as e:r=e
    # The fixture has no public ports or TLS. This override never ships in production runtime.
    for cookie in jar:cookie.secure=False
    return r.code,r.headers,r.read().decode()
def form(values):return urllib.parse.urlencode(values).encode()
class Forms(HTMLParser):
    def __init__(self,html):super().__init__();self.forms=[];self.current=None;self.feed(html)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form':self.current={'action':attrs.get('action',''),'method':attrs.get('method','GET'),'inputs':{}};self.forms.append(self.current)
        if tag in ('input','button') and self.current is not None and attrs.get('name'):
            self.current['inputs'][attrs['name']]=attrs.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.current=None
status,_,body=request('/oauth2/registration',json.dumps({'client_name':'Disposable installer test',
    'client_uri':'https://web.bootstrap-test.invalid','redirect_uris':[CALLBACK],
    'grant_types':['authorization_code','refresh_token'],'response_types':['code'],
    'token_endpoint_auth_method':'none'}).encode(),{'Content-Type':'application/json'})
assert status==201,'registration'
cid=json.loads(body)['client_id']
verifier=secrets.token_urlsafe(48)
challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
state=secrets.token_urlsafe(24)
url=BASE+'/authorize?'+urllib.parse.urlencode({'client_id':cid,'redirect_uri':CALLBACK,
    'response_type':'code','scope':'openid urn:matrix:org.matrix.msc2967.client:api:* urn:matrix:org.matrix.msc2967.client:device:INSTALLCHECK urn:mas:admin urn:synapse:admin:*',
    'state':state,'nonce':secrets.token_urlsafe(20),'code_challenge':challenge,'code_challenge_method':'S256'})
code=None
for _ in range(20):
    if url.startswith(CALLBACK):
        response=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert response['state']==[state]
        code=response.get('code',[None])[0];break
    status,headers,body=request(url)
    if headers.get('Location'):url=urllib.parse.urljoin(url,headers['Location']);continue
    forms=Forms(body).forms
    password=next((f for f in forms if 'username' in f['inputs'] and 'password' in f['inputs']),None)
    selected=password or next((f for f in forms if f['method'].upper()=='POST'),None)
    print(json.dumps({'check':'fixture_authorization_step','status':status,'forms':len(forms),
        'password_form':password is not None,'post_form':selected is not None}),flush=True)
    assert selected is not None,'authorization form'
    values=dict(selected['inputs'])
    if password:values.update(username=CREDS['username'],password=CREDS['password'])
    if 'action' in values:values['action']='allow'
    target=urllib.parse.urljoin(url,selected['action'])
    status,headers,body=request(target,form(values),{'Content-Type':'application/x-www-form-urlencoded'})
    assert headers.get('Location'),'submission redirect'
    url=urllib.parse.urljoin(target,headers['Location'])
assert code,'code'
status,_,body=request('/oauth2/token',form({'grant_type':'authorization_code','client_id':cid,
    'code':code,'redirect_uri':CALLBACK,'code_verifier':verifier}),{'Content-Type':'application/x-www-form-urlencoded'})
assert status==200,'token exchange'
token=json.loads(body)
auth={'Authorization':'Bearer '+token['access_token']}
status,_,body=request('http://synapse:8008/_matrix/client/v3/account/whoami',headers=auth)
assert status==200 and json.loads(body)['user_id']=='@matrix-admin:bootstrap-test.invalid','whoami'
for url in ['/api/admin/v1/users','http://synapse:8008/_synapse/admin/v2/users?guests=false']:
    status,_,_=request(url,headers=auth);assert status==200,'admin API'
status,_,body=request('http://synapse:8008/_matrix/client/v3/createRoom',json.dumps({'preset':'private_chat','name':'Installer test'}).encode(),{**auth,'Content-Type':'application/json'})
assert status==200,'create room'
room=json.loads(body)['room_id']
status,_,body=request('http://synapse:8008/_matrix/client/v3/rooms/'+urllib.parse.quote(room,safe='')+'/state/m.room.encryption/',headers=auth)
assert status==200 and json.loads(body)['algorithm']=='m.megolm.v1.aes-sha2','encryption'
status,_,body=request('/oauth2/token',form({'grant_type':'refresh_token','client_id':cid,'refresh_token':token['refresh_token']}),{'Content-Type':'application/x-www-form-urlencoded'})
assert status==200 and json.loads(body).get('access_token'),'refresh'
print(json.dumps({'check':'isolated_oauth_and_encryption','pkce':True,'admin_apis':True,'refresh':True,'default_encryption':True}),flush=True)
# Private handoff inside this disposable fixture only; never print its token.
from pathlib import Path
test_client=Path('/data/installer-test-client.json')
test_client.write_text(json.dumps({'homeserver_url':'http://synapse:8008',
    'access_token':json.loads(body)['access_token'],'server_name':'bootstrap-test.invalid',
    'refresh_token':json.loads(body)['refresh_token'],'client_id':cid,
    'token_endpoint':'http://mas:8080/oauth2/token',
    'bot_user_id':'@referral-bot:bootstrap-test.invalid','bot_device_id':'MATRIX-REFERRAL-01'}))
test_client.chmod(0o600)
