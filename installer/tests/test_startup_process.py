"""Real child-process failures must stop startup without losing state or secrets."""

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import startup_process
import state_store
import terminal_process
import terminal_views


class StartupProcessTests(unittest.TestCase):
    def test_silent_child_and_resistant_descendant_terminate_and_release_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pid_file = root / 'child.pid'
            script = (
                'import subprocess,sys,time,signal; '
                'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                'child=subprocess.Popen([sys.executable,"-c",'
                '"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],'
                'close_fds=False); '
                'open(sys.argv[1],"w").write(str(child.pid)); time.sleep(60)'
            )
            before = time.monotonic()
            with (
                state_store.operation_guard(root),
                patch.object(startup_process, 'TERMINATE_GRACE', 0.1),
            ):
                with self.assertRaisesRegex(RuntimeError, 'Command timed out'):
                    startup_process.run([sys.executable, '-c', script, str(pid_file)], timeout=0.4)
            self.assertLess(time.monotonic() - before, 3)
            pid = int(pid_file.read_text())
            try:
                self.assertEqual(Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1][0], 'Z')
            except FileNotFoundError:
                pass
            with state_store.operation_guard(root):
                pass

    def test_dead_parent_with_descendant_pipe_is_not_a_hang(self):
        script = (
            'import subprocess,sys; subprocess.Popen([sys.executable,"-c",'
            '"import time; time.sleep(60)"])'
        )
        before = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'unfinished child'):
            startup_process.run([sys.executable, '-c', script], timeout=3)
        self.assertLess(time.monotonic() - before, 3)

    def test_dead_parent_with_closed_descendant_pipes_is_not_success(self):
        script = (
            'import os,time,signal; '
            'pid=os.fork(); '
            'os._exit(0) if pid else None; '
            'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'os.close(1); os.close(2); time.sleep(60)'
        )
        with patch.object(startup_process, 'TERMINATE_GRACE', 0.1):
            with self.assertRaisesRegex(RuntimeError, 'unfinished child'):
                startup_process.run([sys.executable, '-c', script], timeout=3)

    def test_interrupted_ui_tracks_descendant_after_leader_exit_and_pipe_eof(self):
        child = subprocess.Popen(
            [
                sys.executable,
                '-c',
                'import os,signal,time; pid=os.fork(); os._exit(0) if pid else None; '
                'signal.signal(signal.SIGTERM,signal.SIG_IGN); os.close(1); time.sleep(60)',
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            child.wait(timeout=2)
            messages = []
            before = time.monotonic()
            terminal_process.read_interrupted_output(child, messages.append, timeout=0.2)
            self.assertLess(time.monotonic() - before, 2)
            self.assertFalse(terminal_process.process_group_running(child))
            self.assertIn('принудительно', messages[0])
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.stdout.close()

    def test_nonzero_command_does_not_reach_following_startup_step_or_expose_secrets(self):
        @startup_process.bounded
        def sequence():
            lifecycle.run(
                [sys.executable, '-c', 'import sys; print("FICTIONAL_TOKEN"); sys.exit(7)']
            )
            self.fail('Startup continued after failure')

        with patch.object(lifecycle, 'emit') as emit:
            with self.assertRaisesRegex(RuntimeError, 'Command failed') as error:
                sequence()
        self.assertNotIn('FICTIONAL_TOKEN', str(error.exception))
        self.assertNotIn('FICTIONAL_TOKEN', str(emit.call_args_list))

    def test_download_uses_owned_tty_and_reports_measured_bytes_without_raw_output(self):
        with tempfile.TemporaryDirectory() as folder:
            docker = Path(folder) / 'docker'
            docker.write_text(
                '#!' + sys.executable + '\n'
                'import os,time\n'
                'assert os.isatty(1) and os.isatty(2)\n'
                'print("FICTIONAL_RAW_SECRET",flush=True)\n'
                'print("abcdef123456: Downloading [==>] 1MB/10MB",flush=True)\n'
                'time.sleep(1.1)\n'
                'print("abcdef123456: Downloading [====>] 3MB/10MB",flush=True)\n'
                'print("abcdef123456: Download complete",flush=True)\n'
            )
            docker.chmod(0o700)
            records = []
            result = startup_process.run(
                [str(docker), 'pull', 'fictional/image'],
                emit=lambda check, **values: records.append({'check': check, **values}),
                timeout=3,
            )
            self.assertEqual(result.returncode, 0)
            downloads = [item for item in records if item['check'] == 'download_progress']
            self.assertEqual([item['current'] for item in downloads], [1000000, 3000000])
            self.assertGreater(downloads[-1]['bytes_per_second'], 0)
            self.assertTrue(any(item['check'] == 'download_layer' for item in records))
            self.assertNotIn('FICTIONAL_RAW_SECRET', json.dumps(records))

    def test_probe_wallclock_budget_includes_child_execution(self):
        before = time.monotonic()
        with (
            patch.object(lifecycle, 'emit'),
            patch.object(lifecycle, 'ROOT', Path('/tmp/example'), create=True),
        ):
            with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
                lifecycle.wait(
                    lambda: lifecycle.run([sys.executable, '-c', 'import time; time.sleep(60)']),
                    attempts=1,
                    service='fictional-service',
                )
        self.assertLess(time.monotonic() - before, 4)

    def test_programming_error_does_not_turn_into_readiness_retry(self):
        with patch.object(lifecycle, 'emit'), patch.object(lifecycle.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'internal failure'):
                lifecycle.wait(lambda: (_ for _ in ()).throw(ValueError('internal failure')))
        sleep.assert_not_called()

    def test_missing_image_with_empty_array_is_pulled_and_failure_prevents_generate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'versions.json').write_text(
                json.dumps(
                    {
                        'digests': {'synapse': 'fictional@sha256:123'},
                        'tags': {'synapse': 'fictional'},
                    }
                )
            )
            missing = subprocess.CompletedProcess([], 1, '[]\n', 'No such image')
            with (
                patch.object(lifecycle, 'ROOT', root, create=True),
                patch.object(lifecycle, 'ownership_check'),
                patch.object(lifecycle, 'state', return_value={}),
                patch.object(lifecycle.referral, 'validate_saved'),
                patch('federation_runtime.validate'),
                patch.object(startup_process, 'run', return_value=missing),
                patch.object(lifecycle, 'run', side_effect=RuntimeError('pull failed')) as run,
                patch.object(lifecycle, 'emit'),
            ):
                with self.assertRaisesRegex(RuntimeError, 'pull failed'):
                    lifecycle.generate()
            run.assert_called_once_with(['docker', 'pull', 'fictional@sha256:123'])

    def test_sigterm_cli_stops_its_separate_child_group(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / 'pid'
            script = (
                'import os,signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                'open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(60)'
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(startup_process.__file__)),
                    '--timeout',
                    '30',
                    '--',
                    sys.executable,
                    '-c',
                    script,
                    str(pid_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 1)
                self.assertIn('interrupted', stderr)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_interrupted_ui_deadline_handles_child_that_closed_output_but_still_runs(self):
        child = subprocess.Popen(
            [
                sys.executable,
                '-c',
                'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                'os.close(1); time.sleep(60)',
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            messages = []
            code = terminal_process.read_interrupted_output(child, messages.append, timeout=0.2)
            self.assertEqual(code, -signal.SIGKILL)
            self.assertIn('принудительно', messages[0])
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            child.stdout.close()

    @unittest.skipIf(terminal_views.Console is None, 'Rich unavailable')
    def test_phase_percent_and_elapsed_stay_visible_after_many_heartbeat_lines(self):
        console = terminal_views.Console(
            file=io.StringIO(), force_terminal=True, width=100, height=10
        )
        frame = terminal_views.BusyFrame(
            terminal_views.TerminalView(console=console), 'Запустить сервер', True
        )
        for message in terminal_process.present_output(
            'start',
            json.dumps({'check': 'startup_progress', 'completed': 2, 'total': 10, 'stage': 'host'}),
        ):
            frame.log(message)
        for index in range(120):
            frame.log('Ожидаем ' + str(index))
        rendered = '\n'.join(
            ''.join(segment.text for segment in line)
            for line in console.render_lines(frame, pad=False)
        )
        self.assertIn('20% этапов', rendered)
        self.assertIn('00:00', rendered)
        self.assertNotIn('100%', rendered)


if __name__ == '__main__':
    unittest.main()
