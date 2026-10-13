"""Interrupted private checkpoint writes must preserve the prior valid state."""

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import state_store


class CheckpointTests(unittest.TestCase):
    def test_guard_waits_for_short_background_operation_without_unlocking_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'installation'
            code = (
                'import sys; sys.path.insert(0,sys.argv[1]); import state_store; '
                'guard=state_store.operation_guard(sys.argv[2]); guard.__enter__(); '
                'print("acquired",flush=True); sys.stdin.read(); guard.__exit__(None,None,None)'
            )
            owner = subprocess.Popen(
                [sys.executable, '-c', code, str(Path(state_store.__file__).parent), str(root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'acquired')
                notified = []

                def release(message):
                    notified.append(message)
                    owner.stdin.close()
                    owner.wait(timeout=5)

                with state_store.operation_guard(root, wait_seconds=2, progress=release):
                    self.assertTrue(state_store.operation_running(root))
                self.assertEqual(len(notified), 1)
                self.assertFalse(state_store.operation_running(root))
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                owner.stdout.close()
                owner.stderr.close()

    def test_guard_wait_deadline_does_not_mutate_or_remove_busy_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'installation'
            with state_store.operation_guard(root):
                path = state_store.operation_lock_path(root)
                before = path.read_bytes()
                with patch.dict(os.environ, {}, clear=True):
                    started = time.monotonic()
                    with self.assertRaises(state_store.OperationBusy):
                        with state_store.operation_guard(root, wait_seconds=0.02):
                            self.fail('busy operation was admitted')
                    self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertTrue(state_store.operation_running(root))

    def test_rsa_failure_never_publishes_partial_key_and_retry_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root / 'example-key.pem'

            def fail_after_partial(command, **kwargs):
                Path(command[-1]).write_bytes(b'EXAMPLE_PARTIAL_KEY_NOT_A_REAL_SECRET')
                raise subprocess.CalledProcessError(1, command)

            with patch.object(state_store.subprocess, 'run', side_effect=fail_after_partial):
                with self.assertRaisesRegex(RuntimeError, 'before publication'):
                    state_store.create_rsa_key(key)
            self.assertFalse(key.exists())
            self.assertEqual(list(root.iterdir()), [])
            state_store.create_rsa_key(key)
            result = subprocess.run(
                ['openssl', 'pkey', '-in', str(key), '-noout', '-check'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            inode = key.stat().st_ino
            with self.assertRaisesRegex(RuntimeError, 'already occupied'):
                state_store.create_rsa_key(key)
            self.assertEqual(key.stat().st_ino, inode)

    def test_pending_update_permits_only_current_updater_descendants(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(
                root / 'update-journal.json',
                {
                    'stage': 'stages/updates/example',
                    'pid': os.getpid(),
                    'started': state_store.process_identity(os.getpid())[1],
                },
            )
            with self.assertRaisesRegex(RuntimeError, 'update-recover'):
                state_store.assert_update_allowed(root)
            with patch.dict(
                os.environ, {'MATRIX_INSTALLER_UPDATE_STAGE': 'stages/updates/example'}
            ):
                state_store.assert_update_allowed(root)
                with patch.object(state_store, 'related_owner', return_value=False):
                    with self.assertRaisesRegex(RuntimeError, 'update-recover'):
                        state_store.assert_update_allowed(root)

    def guard_child(self, root, env=None):
        code = (
            'import sys; sys.path.insert(0,sys.argv[1]); import state_store; '
            'guard=state_store.operation_guard(sys.argv[2]); guard.__enter__(); '
            'print("acquired",flush=True); guard.__exit__(None,None,None)'
        )
        return subprocess.run(
            [sys.executable, '-c', code, str(Path(state_store.__file__).parent), str(root)],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_guard_accepts_nested_child_and_blocks_independent_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'installation'
            with state_store.operation_guard(root):
                with state_store.operation_guard(root):
                    self.assertEqual(self.guard_child(root).returncode, 0)
                independent = dict(os.environ)
                independent.pop('MATRIX_INSTALLER_OPERATION_TOKEN')
                result = self.guard_child(root, independent)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('do not remove the lock file', result.stderr)
                self.assertFalse(root.exists())
            self.assertEqual(self.guard_child(root).returncode, 0)

    def test_copied_owner_token_does_not_admit_unrelated_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'installation'
            code = (
                'import os,json,sys; sys.path.insert(0,sys.argv[1]); import state_store; '
                'guard=state_store.operation_guard(sys.argv[2]); guard.__enter__(); '
                'print(json.dumps(dict(os.environ)),flush=True); sys.stdin.read()'
            )
            owner = subprocess.Popen(
                [sys.executable, '-c', code, str(Path(state_store.__file__).parent), str(root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                inherited = json.loads(owner.stdout.readline())
                result = self.guard_child(root, inherited)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Another installer operation', result.stderr)
            finally:
                owner.kill()
                owner.communicate(timeout=5)
            self.assertEqual(self.guard_child(root).returncode, 0)

    def test_killed_parent_keeps_child_operation_locked_until_child_exits(self):
        import signal

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'installation'
            child_code = (
                'import sys,time; sys.path.insert(0,sys.argv[1]); import state_store; '
                'guard=state_store.operation_guard(sys.argv[2]); guard.__enter__(); '
                'print("borrowed",flush=True); time.sleep(60)'
            )
            owner_code = (
                'import subprocess,sys; sys.path.insert(0,sys.argv[1]); import state_store; '
                'guard=state_store.operation_guard(sys.argv[2]); guard.__enter__(); '
                'child=subprocess.Popen([sys.executable,"-c",sys.argv[3],sys.argv[1],sys.argv[2]],'
                'stdin=subprocess.DEVNULL,**state_store.child_process_options()); '
                'print("childpid",child.pid,flush=True); sys.stdin.read()'
            )
            owner = subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    owner_code,
                    str(Path(state_store.__file__).parent),
                    str(root),
                    child_code,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            child_pid = None
            try:
                lines = [owner.stdout.readline().strip(), owner.stdout.readline().strip()]
                self.assertIn('borrowed', lines)
                child_pid = int(
                    next(line.split()[1] for line in lines if line.startswith('childpid '))
                )
                owner.kill()
                owner.wait(timeout=5)
                blocked = self.guard_child(root)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn('Another installer operation', blocked.stderr)
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                if child_pid is not None:
                    os.kill(child_pid, signal.SIGTERM)
                owner.communicate(timeout=5)
            self.assertEqual(self.guard_child(root).returncode, 0)

    def test_guard_refuses_symlink_and_hardlink_without_truncating_other_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'installation'
            other = Path(folder) / 'other'
            other.write_text('retain this')
            other.chmod(0o600)
            lock = state_store.operation_lock_path(root)
            lock.symlink_to(other)
            with self.assertRaises(OSError), state_store.operation_guard(root):
                pass
            lock.unlink()
            os.link(other, lock)
            with self.assertRaises(RuntimeError), state_store.operation_guard(root):
                pass
            self.assertEqual(other.read_text(), 'retain this')

    def test_failed_commit_preserves_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'installation.json'
            state_store.write_json(path, {'committed': True})
            original = path.read_bytes()
            with patch.object(state_store.os, 'replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    state_store.write_json(path, {'committed': False})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_success_preserves_owner_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.json'
            state_store.write_json(path, {'revision': 1})
            owner = (path.stat().st_uid, path.stat().st_gid)
            state_store.write_json(path, {'revision': 2})
            self.assertEqual(json.loads(path.read_text()), {'revision': 2})
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), owner)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_symlink_cannot_replace_another_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / 'real'
            real.write_text('original')
            link = Path(folder) / 'link'
            link.symlink_to(real)
            with self.assertRaises(RuntimeError):
                state_store.write_text(link, 'replacement')
            self.assertEqual(real.read_text(), 'original')
