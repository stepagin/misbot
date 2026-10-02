"""Reproduce DNS, subnet and SOCKS5 failures without touching real DNS or credentials."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preflight


class PreflightTests(unittest.TestCase):
    def test_partial_optional_provider_pair_refused_before_network_or_host_checks_without_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for provider in ('GOOGLE','TELEGRAM'):
                for supplied in ('CLIENT_ID','CLIENT_SECRET'):
                    with self.subTest(provider=provider,supplied=supplied):
                        (root/'settings.env').write_text(provider+'_'+supplied+'=FICTIONAL_PRIVATE_VALUE\n')
                        with patch.object(preflight,'resolve') as resolver,patch.object(preflight,'system_check') as system:
                            with self.assertRaisesRegex(RuntimeError,provider+'_CLIENT_SECRET') as error:
                                preflight.check(root)
                            resolver.assert_not_called();system.assert_not_called()
                            self.assertIn(str(root/'install.sh')+' preflight',str(error.exception))
                            self.assertNotIn('FICTIONAL_PRIVATE_VALUE',str(error.exception))

    def test_caa_inherits_nearest_policy_and_caches_parent_queries(self):
        queried = []
        def query(host):
            queried.append(host)
            return [(0, 'issue', 'letsencrypt.org')] if host == 'example.org' else []
        with patch.object(preflight, 'caa_query', side_effect=query):
            preflight.caa_check('chat.example.org')
        self.assertEqual(queried.count('example.org'), 1)
        self.assertNotIn('org', queried)

    def test_caa_nonwildcard_additive_policy_and_unknown_critical(self):
        for records in ([(0, 'issuewild', ';')], [(0, 'iodef', 'mailto:example@example.org')],
                [(0, 'issue', ';'), (0, 'issue', 'letsencrypt.org')], [(0, 'unknown', 'example')]):
            with patch.object(preflight, 'caa_query', return_value=records):
                preflight.caa_check('example.org')
        with patch.object(preflight, 'caa_query', return_value=[(128, 'unknown', 'example')]):
            with self.assertRaisesRegex(RuntimeError, 'unsupported critical'):
                preflight.caa_check('example.org')

    def test_caa_restrictive_policy_refuses_without_echoing_parameters(self):
        for value in (';', 'other.example', 'letsencrypt.org;accounturi=EXAMPLE_PRIVATE_PARAMETER'):
            with patch.object(preflight, 'caa_query', return_value=[(0, 'issue', value)]):
                with self.assertRaises(RuntimeError) as caught:
                    preflight.caa_check('example.org')
            self.assertIn('install.sh preflight', str(caught.exception))
            self.assertNotIn('EXAMPLE_PRIVATE_PARAMETER', str(caught.exception))

    def test_caa_recursive_answer_includes_cname_and_case_insensitive_tag(self):
        response = ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\nexample.org. 300 IN CNAME alias.example.\nalias.example. 300 IN CAA 0 ISSUE "letsencrypt.org"\n'
        with patch.object(preflight.shutil, 'which', return_value='/usr/bin/dig'), \
                patch.object(preflight.subprocess, 'run', return_value=type('Result', (), {'returncode':0, 'stdout':response})()) as run:
            self.assertEqual(preflight.caa_query('example.org'), [(0, 'issue', 'letsencrypt.org')])
        self.assertEqual(run.call_args.kwargs['timeout'], 8)

    def test_caa_query_servfail_and_timeout_are_not_absent_policy(self):
        with patch.object(preflight.shutil, 'which', return_value='/usr/bin/dig'):
            with patch.object(preflight.subprocess, 'run', return_value=type('Result', (), {'returncode':0, 'stdout':'status: SERVFAIL,'})()):
                with self.assertRaisesRegex(RuntimeError, 'answer is unavailable'):
                    preflight.caa_query('example.org')
            with patch.object(preflight.subprocess, 'run', side_effect=preflight.subprocess.TimeoutExpired(['dig'], 8)):
                with self.assertRaisesRegex(RuntimeError, 'query failed'):
                    preflight.caa_query('example.org')

    def test_caa_missing_dig_and_malformed_answer_are_actionable(self):
        with patch.object(preflight.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'apt-get install dnsutils'):
                preflight.caa_query('example.org')
        with patch.object(preflight.shutil, 'which', return_value='/usr/bin/dig'), \
                patch.object(preflight.subprocess, 'run', return_value=type('Result', (), {'returncode':0, 'stdout':'status: NOERROR,\nexample.org. 300 IN CAA 256 issue "example"'})()):
            with self.assertRaisesRegex(RuntimeError, 'cannot be parsed'):
                preflight.caa_query('example.org')

    def test_port_ownership_reads_only_selected_metadata_and_refuses_foreign_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def run(command, **kwargs):
                if command[1] == 'ps':
                    return type('Result', (), {'returncode':0, 'stdout':'example-container\n'})()
                self.assertIn('--format', command)
                projection = command[command.index('--format') + 1]
                self.assertNotIn('.Config.Env', projection)
                fields = [str(root), True, {'443/tcp':[{'HostPort':'443'}], '80/tcp':None}]
                return type('Result', (), {'returncode':0, 'stdout':'\t'.join(json.dumps(v) for v in fields) + '\n'})()
            with patch.object(preflight.shutil, 'which', return_value='/usr/bin/docker'), \
                    patch.object(preflight.subprocess, 'run', side_effect=run):
                self.assertEqual(preflight.owned_ports(root), {(443, 'tcp')})
                self.assertEqual(preflight.owned_ports(root), {(443, 'tcp')})
            result = type('Result', (), {'returncode':0, 'stdout':'"/opt/foreign"\tfalse\tnull\n'})()
            with patch.object(preflight.shutil, 'which', return_value='/usr/bin/docker'), \
                    patch.object(preflight.subprocess, 'run', side_effect=[
                        type('Result', (), {'returncode':0, 'stdout':'example-container\n'})(), result]):
                with self.assertRaisesRegex(RuntimeError, 'Another directory'):
                    preflight.owned_ports(root)
    def test_unavailable_docker_during_preflight_explains_same_safe_recovery(self):
        result=type('Result',(),{'returncode':1,'stdout':'FICTIONAL_PRIVATE_VALUE',
                               'stderr':'Cannot connect to the Docker daemon'})()
        with patch.object(preflight.shutil,'which',return_value='/usr/bin/docker'), \
                patch.object(preflight.subprocess,'run',return_value=result):
            with self.assertRaisesRegex(RuntimeError,'Docker daemon is unavailable') as caught:
                preflight.owned_ports(Path('/opt/fictional-stack'))
        message=str(caught.exception)
        self.assertIn('sudo systemctl start docker',message)
        self.assertIn('sudo bash /opt/fictional-stack/install.sh status',message)
        self.assertNotIn('FICTIONAL_PRIVATE_VALUE',message)

    def test_dns_cache_points_at_old_server_and_explains_retry(self):
        with patch.object(preflight, 'resolve', return_value={'198.51.100.20'}):
            with self.assertRaises(RuntimeError) as caught:
                preflight.dns_check('chat.example.org', '203.0.113.10')
        self.assertIn('expected only A=203.0.113.10', str(caught.exception))
        self.assertIn('198.51.100.20', str(caught.exception))
        self.assertIn('caches', str(caught.exception))
        self.assertIn('same directory', str(caught.exception))

    def test_aaaa_is_rejected_independently_of_correct_a(self):
        def resolve(host, family):
            return {'203.0.113.10'} if family == socket.AF_INET else {'2001:db8::10'}
        with patch.object(preflight, 'resolve', side_effect=resolve):
            with self.assertRaisesRegex(RuntimeError, 'unexpected AAAA'):
                preflight.dns_check('chat.example.org', '203.0.113.10')

    def test_dns_service_failure_is_not_a_raw_traceback(self):
        with patch.object(preflight.socket, 'getaddrinfo', side_effect=socket.gaierror()):
            with self.assertRaisesRegex(RuntimeError, 'no A record'):
                preflight.dns_check('chat.example.org', '203.0.113.10')

    def test_resolver_timeout_has_a_bounded_wait(self):
        def stalled(*args):
            time.sleep(0.1)
            return []
        with patch.object(preflight.socket, 'getaddrinfo', side_effect=stalled):
            with self.assertRaisesRegex(RuntimeError, 'DNS query timed out'):
                preflight.resolve('chat.example.org', socket.AF_INET, timeout=0.01)

    def test_existing_vpn_route_prevents_network_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'installation.json').write_text('{}')
            routes = json.dumps([{'dst': 'default'}, {'dst': '172.30.0.0/16', 'dev': 'test-vpn'}])
            result = type('Result', (), {'stdout': routes})()
            with patch.object(preflight, 'owned_ports', return_value={(p, protocol)
                    for protocol, ports in [('tcp', (80, 443, 7881)), ('udp', (3478, 7882))]
                    for p in ports}), patch.object(preflight.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(RuntimeError, 'overlaps'):
                    preflight.network_check(root, '172.30.50.0/24')

    def test_wrong_proxy_credentials_use_safe_message_and_optional_disable(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            def reject():
                connection, _ = listener.accept()
                with connection:
                    preflight.receive(connection, 3)
                    connection.sendall(b'\x05\x02')
                    preflight.receive(connection, 2)
                    connection.recv(512)
                    connection.sendall(b'\x01\x01')
            worker = threading.Thread(target=reject, daemon=True)
            worker.start()
            with self.assertRaises(RuntimeError) as caught:
                preflight.proxy_check({'TELEGRAM_CLIENT_ID': 'example-client',
                    'SOCKS5_CONFIG': f'socks5://example-user:EXAMPLE_PRIVATE_PASSWORD@127.0.0.1:{port}'})
            worker.join(timeout=2)
        self.assertNotIn('EXAMPLE_PRIVATE_PASSWORD', str(caught.exception))
        self.assertIn('clear both', str(caught.exception))
        self.assertIn('install.sh preflight', str(caught.exception))

    def test_partial_installation_accepts_only_its_labeled_docker_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'installation.json').write_text('{}')
            route={'dst':'172.30.50.0/24','dev':'br-aaaaaaaaaaaa'}
            network={'Id':'a'*64,'Labels':{'com.docker.compose.project':'matrix-stack',
                'org.misbot.matrix.installation.directory':str(root)},
                'IPAM':{'Config':[{'Subnet':'172.30.50.0/24'}]}}
            def run(command,**kwargs):
                value=[route] if command[0]=='ip' else [network]
                return type('Result',(),{'stdout':json.dumps(value),'returncode':0})()
            allowed={(p,protocol) for protocol,ports in [('tcp',(80,443,7881)),('udp',(3478,7882))] for p in ports}
            with patch.object(preflight,'owned_ports',return_value=allowed), \
                 patch.object(preflight.shutil,'which',return_value='/usr/bin/docker'), \
                 patch.object(preflight.subprocess,'run',side_effect=run):
                preflight.network_check(root,'172.30.50.0/24')
                network['Labels']['org.misbot.matrix.installation.directory']='/opt/other-stack'
                with self.assertRaisesRegex(RuntimeError,'overlaps'):
                    preflight.network_check(root,'172.30.50.0/24')
                network['Labels']['org.misbot.matrix.installation.directory']=str(root)
                route['dev']='test-vpn'
                with self.assertRaisesRegex(RuntimeError,'overlaps'):
                    preflight.network_check(root,'172.30.50.0/24')
                (root/'installation.json').write_text(json.dumps({'network_committed':True,'subnet':'172.30.50.0/24'}))
                with self.assertRaisesRegex(RuntimeError,'overlaps'):
                    preflight.network_check(root,'172.30.50.0/24')
                route['dev']='br-aaaaaaaaaaaa'
                del network['Labels']['org.misbot.matrix.installation.directory']
                preflight.network_check(root,'172.30.50.0/24')

    def test_unavailable_proxy_does_not_erase_settings(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        values = {'TELEGRAM_CLIENT_ID': 'example-client', 'SOCKS5_CONFIG': f'socks5://127.0.0.1:{port}'}
        before = dict(values)
        with self.assertRaisesRegex(RuntimeError, 'preflight failed'):
            preflight.proxy_check(values)
        self.assertEqual(values, before)

    def test_proxy_is_optional_when_telegram_is_disabled(self):
        with patch.object(preflight.socket, 'create_connection') as connect:
            preflight.proxy_check({'SOCKS5_CONFIG': 'socks5://proxy.example.org:1080'})
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
