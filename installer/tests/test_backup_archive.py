"""Untrusted archives must fail before touching destination files."""
import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_archive


class ArchiveTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid()==0,'numeric ownership requires root')
    def test_federation_bot_owner_is_restored_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'session';source.write_text('FICTIONAL_BOT_STATE');source.chmod(0o600);os.chown(source,10003,10003)
            archive=root/'snapshot.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(source,arcname='data/federation-bot/state.json')
            destination=root/'restored';destination.mkdir(mode=0o700)
            backup_archive.extract(archive,destination,owners=True)
            saved=destination/'data/federation-bot/state.json'
            self.assertEqual((saved.stat().st_uid,saved.stat().st_gid,saved.stat().st_mode&0o777),(10003,10003,0o600))
            self.assertEqual(saved.read_text(),'FICTIONAL_BOT_STATE')

    def test_only_the_known_transient_control_socket_is_omitted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); run = root / 'bot-run'; run.mkdir()
            with socket.socket(socket.AF_UNIX) as control:
                control.bind(str(run / 'control.sock'))
                archive = root / 'snapshot.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    backup_archive.add_tree(tar, run, 'data/referral-control/bot-run')
                with tarfile.open(archive, 'r:gz') as tar:
                    self.assertEqual([member.name for member in tar.getmembers()], ['data/referral-control/bot-run'])
                self.assertTrue((run / 'control.sock').exists())
                with tarfile.open(root / 'refused.tar.gz', 'w:gz') as tar:
                    with self.assertRaisesRegex(RuntimeError, 'symlink or special file'):
                        backup_archive.add_tree(tar, run, 'unexpected-directory')

    def test_killed_S3_publication_can_be_archived_and_restored_without_link_members(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); media = root / 'media'; media.mkdir(); ready = root / 'ready'
            child = '''import io, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1]); import media_snapshot
media = Path(sys.argv[2]); ready = Path(sys.argv[3]); original_link = os.link
class S3:
    def get_paginator(self, name): return self
    def paginate(self, **kwargs): yield {'Contents': [{'Key': 'example/original', 'Size': 7}]}
    def get_object(self, **kwargs): return {'Body': io.BytesIO(b'Example')}
def held_link(*args):
    original_link(*args); ready.write_text('own file published')
    while True: time.sleep(.05)
media_snapshot.os.link = held_link
media_snapshot.hydrate(media, {'S3_BUCKET': 'fictional', 'S3_PREFIX': 'example/'}, ready.parent / 'inventory', S3())
'''
            process = subprocess.Popen([sys.executable, '-c', child,
                str(Path(__file__).resolve().parents[1]), str(media), str(ready)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists():
                    self.assertIsNone(process.poll(), 'Hydrate exited before the controlled publication')
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                process.kill(); self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)
            finally:
                if process.poll() is None: process.kill(); process.wait(timeout=10)
            original = media / 'original'; scratch = list(media.glob('.matrix-s3-*'))
            self.assertEqual(len(scratch), 1); self.assertEqual(original.stat().st_nlink, 2)
            self.assertEqual(original.stat().st_ino, scratch[0].stat().st_ino)
            archive = root / 'snapshot.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                backup_archive.add_tree(tar, media, 'media')
            with tarfile.open(archive, 'r:gz') as tar:
                self.assertTrue(all(member.isdir() or member.isfile() for member in tar.getmembers()))
            target = root / 'target'; target.mkdir(); backup_archive.extract(archive, target)
            self.assertEqual((target / 'media/original').read_bytes(), b'Example')
            self.assertEqual(next((target / 'media').glob('.matrix-s3-*')).read_bytes(), b'Example')
            self.assertEqual((target / 'media/original').stat().st_nlink, 1)
            self.assertEqual(original.read_bytes(), b'Example')

    def test_backup_writer_refuses_source_symlinks_and_special_files(self):
        for kind in ('file_link', 'directory_link', 'fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); source = root / 'source'; source.mkdir()
                outside = root / 'outside'; outside.mkdir()
                private = outside / 'example'; private.write_bytes(b'FICTIONAL_OUTSIDE_PRIVATE_VALUE')
                if kind == 'file_link': (source / 'unsafe').symlink_to(private)
                elif kind == 'directory_link': (source / 'unsafe').symlink_to(outside, target_is_directory=True)
                else: os.mkfifo(source / 'unsafe')
                archive = root / 'refused.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    with self.assertRaisesRegex(RuntimeError, 'symlink or special file'):
                        backup_archive.add_tree(tar, source, 'source')
                with tarfile.open(archive, 'r:gz') as tar:
                    self.assertTrue(all(member.isdir() for member in tar.getmembers()))
                self.assertEqual(private.read_bytes(), b'FICTIONAL_OUTSIDE_PRIVATE_VALUE')

    def test_unsafe_paths_links_duplicates_and_special_files_are_rejected_before_extract(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE),
                           ('pipe', tarfile.FIFOTYPE), ('good', tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                archive = root / 'bad.tar.gz'
                destination = root / 'destination'
                destination.mkdir()
                with tarfile.open(archive, 'w:gz') as tar:
                    good = tarfile.TarInfo('good')
                    good.size = 4
                    tar.addfile(good, io.BytesIO(b'data'))
                    bad = tarfile.TarInfo(name)
                    bad.type = kind
                    bad.linkname = '../escape'
                    tar.addfile(bad, io.BytesIO(b''))
                with self.assertRaises(RuntimeError):
                    backup_archive.extract(archive, destination)
                self.assertEqual(list(destination.iterdir()), [])
                self.assertFalse((root / 'escape').exists())

    def test_restoration_cannot_overwrite_an_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            destination = root / 'destination'
            destination.mkdir()
            (destination / 'good').write_text('original')
            archive = root / 'archive.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo('good')
                member.size = 3
                tar.addfile(member, io.BytesIO(b'new'))
            with self.assertRaises(RuntimeError):
                backup_archive.extract(archive, destination)
            self.assertEqual((destination / 'good').read_text(), 'original')
