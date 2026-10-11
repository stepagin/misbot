"""Full local/S3 snapshot, encrypted offsite export, authenticated download."""

import argparse
import json
import os
import re
import signal
import ssl
import stat
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import update

import backup_archive
import backup_crypto
import lifecycle
import media_snapshot
import media_transfer
import preflight
import state_store
import s3_config


def failure_message(error):
    """Explain known failures without copying provider text or signed requests."""
    if isinstance(error, RuntimeError):
        return str(error)
    code = getattr(error, 'response', {}).get('Error', {}).get('Code', '')
    if code in ('InvalidAccessKeyId', 'SignatureDoesNotMatch', 'InvalidToken', 'ExpiredToken'):
        return (
            'S3 authentication failed. Correct S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY and the provider region '
            'in the private env, then retry. Previous verified copies are retained'
        )
    if code in ('AccessDenied', '403', 'Forbidden'):
        return (
            'S3 access denied. Check credentials, bucket/prefix permissions and endpoint privately, '
            'then retry. Previous verified copies are retained'
        )
    if code in ('NoSuchBucket', 'NotFound', '404'):
        return (
            'Backup S3 bucket/object was not found. Correct the private settings/object key and retry; '
            'do not delete original data or previous verified copies'
        )
    if type(error).__name__ in (
        'EndpointConnectionError',
        'ConnectTimeoutError',
        'ReadTimeoutError',
        'SSLError',
    ):
        return (
            'Backup S3 endpoint is unavailable, timed out or failed TLS verification. Check endpoint, '
            'DNS/network and trusted CA privately, then retry. Use install.sh status and backup-recover '
            'if writers have not returned; previous verified copies are retained'
        )
    return (
        'Backup failed or was interrupted. Keep original data, private env/key and catalog. '
        'Run install.sh status and backup-recover; check connectivity and free disk, then retry'
    )


def settings(path):
    values = s3_config.read_profile(path, purpose='backup')
    if set(values) - media_transfer.FIELDS:
        raise RuntimeError('The backup S3 env must contain only the six S3 parameter names')
    return values


def verify_archive(path):
    with tempfile.TemporaryDirectory(prefix='matrix-external-verify-', dir=path.parent) as folder:
        backup_archive.extract(path, folder)
        snapshot = backup_archive.verify_snapshot(folder)
        installed = Path(folder) / 'installed'
        installed.mkdir(mode=0o700)
        backup_archive.extract(snapshot / 'files.tar.gz', installed)
        update.validate_manifest(installed)


def fetch(values, key_file, object_key, expected_sha256, destination, service=None):
    if len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256):
        raise RuntimeError('Provide the expected ciphertext SHA256 recorded by backup-external')
    if not object_key.startswith(values['S3_PREFIX']) or '..' in object_key.split('/'):
        raise RuntimeError('Backup object must belong to the configured dedicated prefix')
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('Restored archive destination must be a new private file')
    service = service or media_transfer.client(values)
    key = backup_crypto.read_key(key_file)
    with tempfile.TemporaryDirectory(
        prefix='matrix-external-fetch-', dir=destination.parent
    ) as folder:
        encrypted = Path(folder) / 'download.enc'
        plain = Path(folder) / 'verified.tar.gz'
        from boto3.s3.transfer import TransferConfig

        service.download_file(
            values['S3_BUCKET'],
            object_key,
            str(encrypted),
            Config=TransferConfig(max_concurrency=1, use_threads=False),
        )
        encrypted.chmod(0o600)
        with encrypted.open('rb') as incoming:
            if media_transfer.stream_digest(incoming) != expected_sha256:
                raise RuntimeError('Downloaded backup checksum differs; nothing restored')
        backup_crypto.decrypt(encrypted, plain, key)
        verify_archive(plain)
        with plain.open('rb') as incoming:
            digest = media_transfer.stream_digest(incoming)
        os.link(plain, destination)
    return digest


def private_remote(service, values, object_key):
    url = (
        service.meta.endpoint_url.rstrip('/')
        + '/'
        + urllib.parse.quote(values['S3_BUCKET'], safe='')
        + '/'
        + urllib.parse.quote(object_key, safe='/')
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    for attempt in range(1, 4):
        try:
            with opener.open(urllib.request.Request(url, method='GET'), timeout=15) as response:
                if response.status == 200:
                    raise RuntimeError(
                        'External backup is anonymously accessible; correct bucket policy and retry. Previous verified copies are retained'
                    )
                raise RuntimeError(
                    'Unable to confirm private backup access; correct bucket policy and retry'
                )
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                return
            if error.code not in (502, 503, 504):
                raise RuntimeError(
                    'Unable to confirm private backup access; correct bucket policy and retry'
                ) from None
            reason = 'temporary_http_failure'
        except (urllib.error.URLError, TimeoutError) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(cause, ssl.SSLCertVerificationError):
                raise RuntimeError(
                    'Backup privacy could not be verified: TLS certificate validation failed. Correct the endpoint or trusted CA privately, then retry install.sh backup-external PRIVATE_S3_ENV KEY_FILE [DIRECTORY]; keep the local snapshot, recovery key and previous verified copies'
                ) from None
            reason = (
                'timeout'
                if isinstance(cause, TimeoutError)
                else 'tls_connection_failure'
                if isinstance(cause, ssl.SSLError)
                else 'dns_or_network_failure'
            )
        if attempt < 3:
            lifecycle.emit(
                'backup_privacy_retry',
                attempt=attempt,
                max_attempts=3,
                reason=reason,
                next_action='Wait two seconds for another HTTPS privacy check; no backup is marked verified yet',
            )
            time.sleep(2)
    raise RuntimeError(
        'Backup privacy could not be verified after three HTTPS attempts. Check endpoint DNS/network/TLS privately, then retry install.sh backup-external PRIVATE_S3_ENV KEY_FILE [DIRECTORY]; keep the local snapshot, recovery key and previous verified copies'
    )


def cleanup_failed_upload(service, values, object_key, folder):
    """Retain the primary failure even when cleanup or its audit cannot finish."""
    requested = False
    try:
        service.delete_object(Bucket=values['S3_BUCKET'], Key=object_key)
        requested = True
    except Exception:
        # SDK responses can contain credentials or signed requests.
        pass
    catalog = folder / 'catalog.json'
    recorded = False
    try:
        state = json.loads(catalog.read_text())
        state.update(cleanup_delete_requested=requested, unverified_object_may_remain=True)
        state_store.write_json(catalog, state)
        recorded = True
    except Exception:
        # Full disk or interrupted audit must not replace the export error.
        pass
    message = (
        'Removal of this unique unverified upload was requested. Provider versioning or retention '
        'may keep older versions; previous verified copies are untouched'
        if requested
        else 'The unique unverified upload could not be removed and is not a usable backup. '
        'Keep the local snapshot and recovery key. Check S3 DeleteObject permission/network privately; '
        'Retry install.sh backup-cleanup PRIVATE_S3_ENV PRIVATE_CATALOG [DIRECTORY] '
        'for only this unverified object. '
        'Retry install.sh backup-external PRIVATE_S3_ENV KEY_FILE [DIRECTORY]; previous verified copies are untouched'
    )
    fields = dict(
        delete_requested=requested,
        catalog_updated=recorded,
        unverified_object_may_remain=True,
        private_catalog=str(catalog),
        message=message,
    )
    try:
        lifecycle.emit('external_backup_cleanup', **fields)
    except Exception:
        try:
            print(json.dumps(dict(check='external_backup_cleanup', **fields)), flush=True)
        except Exception:
            pass
    return requested


def storage_identity(values):
    return {
        name: values.get(name, '')
        for name in ('S3_BUCKET', 'S3_PREFIX', 'S3_REGION', 'S3_ENDPOINT_URL')
    }


def cleanup_catalog(root, values, catalog_file):
    path = Path(catalog_file).absolute()
    if (
        path.is_symlink()
        or path.resolve(strict=True) != path
        or path.name != 'catalog.json'
        or path.parent.parent != root / 'stages/external-backups'
        or not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{32}', path.parent.name)
    ):
        raise RuntimeError(
            "Choose this installation's original private unverified backup catalog; no object removed"
        )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'r') as incoming:
        info = os.fstat(incoming.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > 16384
        ):
            raise RuntimeError(
                'Backup cleanup catalog must be a private root-owned regular file without hardlinks'
            )
        catalog = json.load(incoming)
    expected = values['S3_PREFIX'] + path.parent.name + '.matrix.enc'
    if (
        not isinstance(catalog, dict)
        or catalog.get('format') != 1
        or catalog.get('upload_verified') is not False
        or catalog.get('object_key') != expected
        or catalog.get('storage') != storage_identity(values)
    ):
        raise RuntimeError(
            'Refusing cleanup of verified, foreign or legacy backup. Use its original private settings/catalog; no object removed'
        )
    if catalog.get('cleanup_delete_requested') is True:
        lifecycle.emit(
            'external_backup_cleanup',
            delete_requested=True,
            already_requested=True,
            private_catalog=str(path),
            message='Deletion was already requested; no additional object or version is removed',
        )
        return
    service = media_transfer.client(values)
    if not cleanup_failed_upload(service, values, expected, path.parent):
        raise RuntimeError(
            'Unverified backup cleanup failed. Correct private S3 permissions/network and retry install.sh backup-cleanup PRIVATE_S3_ENV PRIVATE_CATALOG [DIRECTORY]; retain the local snapshot and recovery key'
        )


def export(root, values, key_file):
    key = backup_crypto.read_key(key_file)
    key_path = Path(key_file).resolve(strict=True)
    if key_path.is_relative_to(root):
        raise RuntimeError('Keep the recovery key outside the installation and its archives')
    metadata = lifecycle.state()
    if any(
        metadata.get(k)
        for k in ('restore_pending', 'media_transition_pending', 'credential_transition_pending')
    ):
        raise RuntimeError(
            'Finish staged restore or interrupted media/password operations before exporting a backup'
        )
    if metadata.get('media_mode') == 's3':
        live = preflight.settings(root)
        if values['S3_BUCKET'] == live['S3_BUCKET'] and (
            values['S3_PREFIX'].startswith(live['S3_PREFIX'])
            or live['S3_PREFIX'].startswith(values['S3_PREFIX'])
        ):
            raise RuntimeError(
                'Backup storage needs a separate bucket or a prefix that does not overlap live media'
            )
    service = media_transfer.client(values)
    service.head_bucket(Bucket=values['S3_BUCKET'])
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex
    folder = root / 'stages/external-backups' / stamp
    folder.mkdir(mode=0o700, parents=True)
    object_key = values['S3_PREFIX'] + stamp + '.matrix.enc'
    attempted_upload = False
    verified = False
    with lifecycle.paused_writers():
        if metadata.get('media_mode') == 's3':
            result = media_snapshot.hydrate(
                root / 'data/synapse/media_store',
                preflight.settings(root),
                root / 'data/synapse/external-media-inventory.json',
            )
            lifecycle.emit('external_media_snapshot', passed=True, **result)
        archive = lifecycle.backup(leave_stopped=True)
    # S3 download verification runs after writers have returned, using the frozen archive.
    encrypted = folder / 'archive.enc'
    downloaded = folder / 'downloaded.tar.gz'
    try:
        backup_crypto.encrypt(archive, encrypted, key)
        with encrypted.open('rb') as incoming:
            checksum = media_transfer.stream_digest(incoming)
        state_store.write_json(
            folder / 'catalog.json',
            {
                'format': 1,
                'object_key': object_key,
                'ciphertext_sha256': checksum,
                'key_file': str(key_path),
                'source_archive': str(archive),
                'upload_verified': False,
                'storage': storage_identity(values),
            },
        )
        from boto3.s3.transfer import TransferConfig

        attempted_upload = True
        service.upload_file(
            str(encrypted),
            values['S3_BUCKET'],
            object_key,
            Config=TransferConfig(max_concurrency=1, use_threads=False),
        )
        private_remote(service, values, object_key)
        plain_checksum = fetch(values, key_path, object_key, checksum, downloaded, service)
        with archive.open('rb') as incoming:
            if media_transfer.stream_digest(incoming) != plain_checksum:
                raise RuntimeError('Decrypted external backup differs from the original archive')
        state_store.write_json(
            folder / 'catalog.json',
            {
                'format': 1,
                'object_key': object_key,
                'ciphertext_sha256': checksum,
                'archive_sha256': plain_checksum,
                'key_file': str(key_path),
                'source_archive': str(archive),
                'downloaded_archive': str(downloaded),
                'upload_verified': True,
            },
        )
        verified = True
        lifecycle.emit(
            'encrypted_external_backup',
            passed=True,
            object_key=object_key,
            ciphertext_sha256=checksum,
            archive_sha256=plain_checksum,
            private_catalog=str(folder / 'catalog.json'),
            downloaded_archive=str(downloaded),
        )
    finally:
        if attempted_upload and not verified:
            # The key is unique to this invocation; never delete another backup or prefix.
            cleanup_failed_upload(service, values, object_key, folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('key', 'export', 'fetch', 'cleanup'))
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--settings', type=Path)
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--object-key')
    parser.add_argument('--sha256')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run external backup commands as root')
    if args.action != 'cleanup' and args.key_file is None:
        raise RuntimeError('Provide --key-file for key/export/fetch')

    def interrupted(signum, frame):
        raise state_store.OperationInterrupted('External backup interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if args.action == 'key':
        backup_crypto.create_key(args.key_file)
        print(
            json.dumps(
                {
                    'check': 'backup_recovery_key',
                    'created': True,
                    'key_file': str(args.key_file),
                    'contents_omitted': True,
                }
            )
        )
        return
    values = settings(args.settings)
    if args.action == 'fetch':
        digest = fetch(values, args.key_file, args.object_key, args.sha256, args.destination)
        print(
            json.dumps(
                {
                    'check': 'external_backup_download',
                    'passed': True,
                    'archive': str(args.destination),
                    'archive_sha256': digest,
                }
            )
        )
    else:
        root = args.directory.resolve(strict=True)
        lifecycle.configure(root, 'backup-external')
        with state_store.operation_guard(root):
            state_store.assert_update_allowed(root)
            lifecycle.ownership_check()
            if args.action == 'cleanup':
                if args.catalog is None:
                    raise RuntimeError('Provide the original private --catalog for cleanup')
                cleanup_catalog(root, values, args.catalog)
            else:
                export(root, values, args.key_file)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {'check': 'external_backup', 'passed': False, 'message': failure_message(error)}
            )
        )
        raise SystemExit(1)
