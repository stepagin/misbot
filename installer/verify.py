#!/usr/bin/env python3
"""Public protocol checks, using validated installation metadata. No account secrets."""

import argparse
import errno
import json
from pathlib import Path
import socket
import ssl
import sys
import urllib.error
import urllib.request

import configuration
import configuration_schema
import homepage


def failure_code(error):
    """Classify errors without disclosing URLs, credentials or exception text."""
    if isinstance(error, homepage.ServiceUnavailable):
        return 'http_status'
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, ssl.SSLCertVerificationError):
        return 'tls_certificate'
    if isinstance(error, ssl.SSLError):
        return 'tls_connection'
    if isinstance(error, socket.gaierror):
        return 'dns_resolution'
    if isinstance(error, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(error, ConnectionRefusedError):
        return 'connection_refused'
    if isinstance(error, OSError) and error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return 'network_unreachable'
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return 'unexpected_response'
    return 'check_failed'


def failed_record(name, error):
    return {
        'check': name,
        'passed': False,
        'error_type': type(error).__name__,
        'failure_code': failure_code(error),
        **({'status': error.status} if isinstance(error, homepage.ServiceUnavailable) else {}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    args = parser.parse_args(argv)
    root = Path(args.directory)
    metadata = json.loads((root / 'installation.json').read_text())
    domain = metadata['domain']

    model = configuration.load(root, True)
    configuration.assert_auth_identity(root, model, metadata)
    endpoints = configuration_schema.public_endpoints(model)
    mas = 'https://' + endpoints['mas']
    tests = [
        ('discovery', 'https://' + domain + '/.well-known/matrix/client', 200),
        ('federation', 'https://' + domain + '/.well-known/matrix/server', 200),
        ('synapse', 'https://' + endpoints['matrix'] + '/_matrix/client/versions', 200),
        (
            'oauth_metadata',
            'https://' + endpoints['matrix'] + '/_matrix/client/v1/auth_metadata',
            200,
        ),
        ('mas_discovery', mas + '/.well-known/openid-configuration', 200),
        ('mas_health', mas + '/health', 200),
        ('web', 'https://' + endpoints['web'] + '/', 200),
        ('admin', 'https://' + endpoints['admin'] + '/', 200),
        ('rtc_jwt', 'https://' + endpoints['rtc'] + '/livekit/jwt/healthz', 200),
        (
            'legacy_login_disabled',
            'https://' + endpoints['matrix'] + '/_matrix/client/v3/login',
            404,
        ),
        (
            'synapse_admin_protected',
            'https://' + endpoints['matrix'] + '/_synapse/admin/v2/users',
            401,
        ),
        ('mas_admin_protected', mas + '/api/admin/v1/users', 401),
    ]
    if model.get('MAS_MODE') == 'external':
        tests = [test for test in tests if test[0] != 'mas_health']
    failed = 0
    try:
        result = homepage.probe(configuration.load(root, True))
        print(json.dumps({'check': 'homepage_routes', 'passed': True, **result}), flush=True)
    except Exception as error:
        print(
            json.dumps(failed_record('homepage_routes', error)),
            flush=True,
        )
        failed += 1
    for name, url, expected in tests:
        try:
            headers = {'Authorization': 'Bearer invalid'} if name == 'mas_admin_protected' else {}
            try:
                response = urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=20
                )
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status, body = response.code, response.read()
            passed = status == expected
            if name in ['oauth_metadata', 'mas_discovery'] and passed:
                document = json.loads(body)
                passed = (
                    document['issuer'] == mas + '/'
                    and document['token_endpoint'] == mas + '/oauth2/token'
                    and document['authorization_endpoint'] == mas + '/authorize'
                )
            if name == 'discovery' and passed:
                document = json.loads(body)
                passed = (
                    document['m.homeserver']['base_url'] == 'https://' + endpoints['matrix']
                    and document['org.matrix.msc2965.authentication']['issuer'] == mas + '/'
                )
            if name == 'federation' and passed:
                passed = json.loads(body)['m.server'] == endpoints['matrix'] + ':443'
            print(
                json.dumps(
                    {
                        'check': name,
                        'status': status,
                        'expected': expected,
                        'passed': passed,
                        **(
                            {
                                'failure_code': 'http_status'
                                if status != expected
                                else 'unexpected_response'
                            }
                            if not passed
                            else {}
                        ),
                    }
                ),
                flush=True,
            )
            failed += not passed
        except Exception as error:
            print(
                json.dumps(failed_record(name, error)),
                flush=True,
            )
            failed += 1
    try:
        host = endpoints['turn']
        with socket.create_connection((host, 443), timeout=15) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                print(
                    json.dumps({'check': 'turn_tls', 'passed': True, 'tls': tls.version()}),
                    flush=True,
                )
    except Exception as error:
        print(
            json.dumps(failed_record('turn_tls', error)),
            flush=True,
        )
        failed += 1
    print(json.dumps({'check': 'verification_summary', 'failures': failed}), flush=True)
    return int(bool(failed))


if __name__ == '__main__':
    sys.exit(main())
