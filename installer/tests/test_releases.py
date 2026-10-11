import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import releases
import update


def item(tag, **extra):
    return dict(tag_name=tag, body='changes', draft=False, prerelease=False, assets=[], **extra)


class ReleasesTests(unittest.TestCase):
    def test_semantic_latest_not_publication_order_and_only_releases(self):
        result = releases.latest(
            lambda url, limit: json.dumps(
                [
                    item('v1.2.0'),
                    item('1.10.1'),
                    item('v1.11.0-rc1'),
                    {**item('9.0.0'), 'draft': True},
                    {**item('10.0.0'), 'prerelease': True},
                ]
            ).encode()
        )
        self.assertEqual(result['version'], '1.10.1')

    def test_pagination_and_minor_semantics(self):
        calls = []

        def fetch(url, limit):
            calls.append(url)
            return json.dumps(
                [item('1.1.0')] * 100 if len(calls) == 1 else [item('1.9.0')]
            ).encode()

        self.assertEqual(releases.latest(fetch)['version'], '1.9.0')
        self.assertEqual(len(calls), 2)
        self.assertTrue(releases.minor_update('1.1.9', '1.2.0'))
        self.assertFalse(releases.minor_update('1.1.0', '1.1.9'))
        self.assertFalse(releases.minor_update('1.1.0', '2.0.0'))

    def archive(self, unsafe=None):
        files = {name: b'# public fixture\n' for name in update.ESSENTIAL}
        files['release.json'] = json.dumps(
            {
                'version': '1.2.0',
                'family': update.FAMILY,
                'update_scope': 'source-configuration-and-approved-components',
            }
        ).encode()
        files['SHA256SUMS'] = ''.join(
            hashlib.sha256(raw).hexdigest() + '  ' + name + '\n' for name, raw in files.items()
        ).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            for name, raw in files.items():
                member = tarfile.TarInfo('matrix-installer/' + name)
                member.size = len(raw)
                tar.addfile(member, io.BytesIO(raw))
            if unsafe:
                member = tarfile.TarInfo(unsafe)
                member.type = tarfile.SYMTYPE
                member.linkname = '/etc/passwd'
                tar.addfile(member)
        return output.getvalue()

    def release(self, raw):
        base = 'https://github.com/stepagin/misbot/releases/download/v1.2.0/'
        return {
            'version': '1.2.0',
            'tag': 'v1.2.0',
            'assets': [
                {
                    'name': name,
                    'state': 'uploaded',
                    'size': len(raw),
                    'browser_download_url': base + name,
                }
                for name in (releases.ARCHIVE, releases.ARCHIVE + '.sha256')
            ],
        }

    def test_verified_ready_archive_no_user_build(self):
        raw = self.archive()
        release = self.release(raw)

        def fetch(url, limit):
            return (
                (hashlib.sha256(raw).hexdigest() + '  ' + releases.ARCHIVE + '\n').encode()
                if url.endswith('.sha256')
                else raw
            )

        with tempfile.TemporaryDirectory() as folder:
            source = releases.download(Path(folder), release, fetch)
            self.assertEqual(update.release(source)['version'], '1.2.0')
            self.assertTrue((source / 'install.sh').is_file())

    def test_archive_path_and_symlink_rejected_after_outer_checksum(self):
        for name in ('matrix-installer/../../escape', 'matrix-installer/link'):
            raw = self.archive(name)
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                    releases.download(
                        Path(folder),
                        self.release(raw),
                        lambda u, l: (
                            hashlib.sha256(raw).hexdigest() + '  ' + releases.ARCHIVE
                        ).encode()
                        if u.endswith('.sha256')
                        else raw,
                    )

    def test_low_disk_missing_assets_and_wrong_checksum_leave_installed_files(self):
        raw = self.archive()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                releases.download(Path(folder), {'assets': []})
            with patch.object(
                releases.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()
            ):
                with self.assertRaisesRegex(RuntimeError, 'места'):
                    releases.download(
                        Path(folder),
                        self.release(raw),
                        lambda u, l: self.fail('download must not start'),
                    )
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                releases.download(
                    Path(folder),
                    self.release(raw),
                    lambda u, l: ('0' * 64 + '  ' + releases.ARCHIVE).encode()
                    if u.endswith('.sha256')
                    else raw,
                )


if __name__ == '__main__':
    unittest.main()
