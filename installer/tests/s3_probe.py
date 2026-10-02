#!/usr/bin/env python3
"""Explicit live probe: create/read/remove only one unique object in a test prefix."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import quote
import urllib.error
import urllib.request


def main():
    import boto3
    from botocore.config import Config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    values = {}
    for line in args.settings.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"\'')
    prefix = values['S3_PREFIX']
    if not prefix.endswith('/') or prefix.startswith('/') or '..' in prefix.split('/'):
        raise ValueError('Use a dedicated relative test prefix ending in /')
    client = boto3.client('s3', endpoint_url=values.get('S3_ENDPOINT_URL') or None,
        region_name=values['S3_REGION'], aws_access_key_id=values['S3_ACCESS_KEY_ID'],
        aws_secret_access_key=values['S3_SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=15,
                      retries={'max_attempts': 1}, s3={'addressing_style': 'path'}))
    bucket = values['S3_BUCKET']
    client.head_bucket(Bucket=bucket)
    print(json.dumps({'check': 's3_signed_head', 'passed': True,
                      'region': values['S3_REGION']}), flush=True)
    key = prefix + '.installer-probe-' + secrets.token_hex(16)
    payload = secrets.token_bytes(256)
    ledger = args.settings.with_name('s3-probe-created-object.json')
    ledger.write_text(json.dumps({'bucket': bucket, 'key': key, 'created': False}))
    # Only this randomly selected, previously absent object is ever deleted.
    try:
        # A timed-out PUT can have committed remotely. Delete the unique key even
        # when no successful response arrived; retain the ledger if cleanup fails.
        client.put_object(Bucket=bucket, Key=key, Body=payload)
        ledger.write_text(json.dumps({'bucket': bucket, 'key': key, 'created': True}))
        body = client.get_object(Bucket=bucket, Key=key)['Body'].read()
        if hashlib.sha256(body).digest() != hashlib.sha256(payload).digest():
            raise RuntimeError('S3 object checksum mismatch')
        print(json.dumps({'check': 's3_put_get_checksum', 'passed': True}), flush=True)
        if values.get('S3_ENDPOINT_URL'):
            url = values['S3_ENDPOINT_URL'].rstrip('/') + '/' + quote(bucket, safe='') + '/' + quote(key)
            try:
                with urllib.request.urlopen(url, timeout=15) as response:
                    anonymous = response.status
            except urllib.error.HTTPError as error:
                anonymous = error.code
            if anonymous == 200:
                raise RuntimeError('Anonymous GET succeeded; disable public bucket policy before using private Matrix media')
            if anonymous not in (401, 403, 404):
                raise RuntimeError('Anonymous object access could not be verified')
            print(json.dumps({'check': 's3_anonymous_get_denied', 'passed': True,
                              'status': anonymous}), flush=True)
    finally:
        client.delete_object(Bucket=bucket, Key=key)
        print(json.dumps({'check': 's3_probe_object_deleted', 'passed': True}), flush=True)
        ledger.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # SDK messages can contain signed URLs or other private request parameters.
        print(json.dumps({'check': 's3_probe', 'passed': False,
                          'error_type': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
