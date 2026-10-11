"""Real narrow Unix HTTP/RPC listeners, isolated fake installation and peers."""
import http.client
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import federation
import federation_service
import federation_runtime
import state_store


class ServiceTests(unittest.TestCase):
    def test_proxy_public_mount_survives_controller_restarts(self):
        if not hasattr(socket,'SO_PEERCRED') or os.geteuid()!=0:self.skipTest('isolated Linux root controller required')
        domain='fixture-'+uuid.uuid4().hex+'.example'
        public=Path('/run/misbot-federation-'+hashlib.sha256(domain.encode()).hexdigest()[:16])
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            metadata={'domain':domain,'backend':'existing-compose','existing':{'proxy':{'format':1}}}
            state_store.write_json(root/'installation.json',metadata)
            federation.setup(root,{'domain':domain})
            original=federation.Backend
            def worker():
                signal.signal(signal.SIGTERM,signal.SIG_DFL)
                with patch.object(federation,'Backend',side_effect=lambda p:original(p,admission=False)),\
                     patch.object(federation_runtime,'validate'),patch.object(federation_service.lifecycle,'configure'):
                    federation_service.serve(root)
            inode=None
            try:
                for _ in range(2):
                    process=multiprocessing.get_context('fork').Process(target=worker);process.start()
                    try:
                        for _ in range(100):
                            if (public/'http.sock').exists() and (root/'run/federation/control.sock').exists():
                                client=socket.socket(socket.AF_UNIX);client.settimeout(.1)
                                try:
                                    client.connect(str(public/'http.sock'));break
                                except (ConnectionRefusedError,FileNotFoundError):pass
                                finally:client.close()
                            if not process.is_alive():self.fail('isolated service exited')
                            time.sleep(.02)
                        else:self.fail('isolated sockets missing')
                        info=public.stat()
                        self.assertEqual((info.st_uid,info.st_gid,info.st_mode&0o777),(0,10001,0o750))
                        if inode is None:inode=info.st_ino
                        self.assertEqual(info.st_ino,inode)
                    finally:
                        process.terminate();process.join(5)
                        if process.is_alive():process.kill();process.join(5)
            finally:
                if public.exists():
                    (public/'http.sock').unlink(missing_ok=True);public.rmdir()

    def test_public_limits_and_no_local_administrative_dispatch(self):
        if not hasattr(socket,'SO_PEERCRED') or os.geteuid()!=0:self.skipTest('isolated Linux root controller required')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);state_store.write_json(root/'installation.json',{'domain':'fixture.example'})
            federation.setup(root,{'domain':'fixture.example'})
            original=federation.Backend
            def worker():
                signal.signal(signal.SIGTERM,signal.SIG_DFL)
                with patch.object(federation,'Backend',side_effect=lambda p:original(p,admission=False)),\
                     patch.object(federation_runtime,'validate'),patch.object(federation_service.lifecycle,'configure'):
                    federation_service.serve(root)
            process=multiprocessing.get_context('fork').Process(target=worker);process.start()
            try:
                path=root/'run/federation/http.sock'
                for _ in range(100):
                    if path.exists() and (root/'run/federation/control.sock').exists():break
                    if not process.is_alive():self.fail('isolated service exited')
                    time.sleep(.02)
                else:self.fail('isolated sockets missing')
                def request(method,route,body=None,headers=None):
                    client=http.client.HTTPConnection('fixture',timeout=2)
                    client.sock=socket.socket(socket.AF_UNIX);client.sock.settimeout(2);client.sock.connect(str(path))
                    try:
                        client.request(method,route,body=body,headers=headers or {})
                        response=client.getresponse();return response.status,json.loads(response.read())
                    finally:client.close()
                status,doc=request('GET','/.well-known/misbot')
                self.assertEqual(status,200);self.assertEqual(doc['server_name'],'fixture.example')
                self.assertNotIn('requests',doc);self.assertNotIn('private_key',doc)
                self.assertEqual(request('POST','/_misbot/v1/admin',b'{}')[0],404)
                status,result=request('POST','/_misbot/v1/exchange',b'{}',{'Content-Length':'32769'})
                self.assertEqual((status,result['code']),(400,'request_too_large'))
                self.assertEqual(request('POST','/_misbot/v1/exchange',b'not-json')[1]['code'],'invalid_request')
                rpc=socket.socket(socket.AF_UNIX);rpc.settimeout(2);rpc.connect(str(root/'run/federation/control.sock'))
                try:
                    rpc.sendall(b'{"action":"add","actor":"root","event_id":"$fictional","argument":"other.example"}\n')
                    reply=json.loads(rpc.recv(32768));self.assertFalse(reply['ok'])
                    self.assertEqual(reply['code'],'local_authorization_required')
                finally:rpc.close()
                statuses=[request('GET','/.well-known/misbot')[0] for _ in range(31)]
                self.assertIn(429,statuses)
            finally:
                process.terminate();process.join(5)
                if process.is_alive():process.kill();process.join(5)


if __name__=='__main__':unittest.main()
