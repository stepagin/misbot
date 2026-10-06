"""Private, fixed MAS admin-rights query. Never return credentials or user payloads."""
import base64
import json
import sys
import urllib.parse
import urllib.request


def authorized(attributes):
    return bool(attributes.get('admin',attributes.get('can_request_admin',False))) and not any(
        attributes.get(k) for k in ('locked_at','deactivated_at','legacy_guest'))


def main():
    payload=json.load(sys.stdin);client=payload['client'];username=payload['username']
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(NoRedirect(),urllib.request.ProxyHandler({}))
    auth=base64.b64encode((client['client_id']+':'+client['client_secret']).encode()).decode()
    req=urllib.request.Request('http://mas:8080/oauth2/token',
        data=urllib.parse.urlencode({'grant_type':'client_credentials','scope':'urn:mas:admin'}).encode(),
        headers={'Authorization':'Basic '+auth})
    with opener.open(req,timeout=10) as response:token=json.load(response)['access_token']
    req=urllib.request.Request('http://mas:8080/api/admin/v1/users/by-username/'+urllib.parse.quote(username,safe=''),
        headers={'Authorization':'Bearer '+token})
    with opener.open(req,timeout=10) as response:attr=json.load(response)['data']['attributes']
    valid=authorized(attr)
    print(json.dumps({'authorized':bool(valid)}))


if __name__=='__main__':
    try:main()
    except Exception: print('{"authorized": false}')
