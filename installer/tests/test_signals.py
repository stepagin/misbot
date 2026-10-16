"""Real signals during captured child I/O must escape syscall retry loops."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class SignalTests(unittest.TestCase):
    def test_lifecycle_scheduled_and_external_backup_interrupt_captured_wait(self):
        bundle = Path(__file__).resolve().parents[1]
        for module in ('lifecycle', 'backup_schedule', 'external_backup'):
            for sent in (signal.SIGTERM, signal.SIGINT):
                with (
                    self.subTest(module=module, signal=sent),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    root = Path(folder)
                    marker = root / 'child-ready'
                    driver = root / 'driver.py'
                    sleeper = (
                        'from pathlib import Path;import time;Path('
                        + repr(str(marker))
                        + ').write_text("ready");time.sleep(30)'
                    )
                    driver.write_text(
                        'import json,os,sys\nfrom contextlib import nullcontext\n'
                        + 'sys.path.insert(0,'
                        + repr(str(bundle))
                        + ')\n'
                        + 'import lifecycle,backup_schedule,external_backup,state_store\n'
                        + 'os.geteuid=lambda:0\nstate_store.operation_guard=lambda *args:nullcontext()\n'
                        + 'state_store.assert_update_allowed=lambda *args:None\nlifecycle.ownership_check=lambda:None\n'
                        + 'def wait(*args):return lifecycle.run([sys.executable,"-c",'
                        + repr(sleeper)
                        + '])\n'
                        + 'lifecycle.up=wait\nbackup_schedule.snapshot=wait\nbackup_schedule.read_record=lambda *args:{}\n'
                        + 'external_backup.export=wait\nexternal_backup.settings=lambda *args:{}\n'
                        + 'module='
                        + repr(module)
                        + '\n'
                        + 'arguments={"lifecycle":["up","--directory",'
                        + repr(str(root))
                        + '], '
                        + '"backup_schedule":["run","--directory",'
                        + repr(str(root))
                        + '], '
                        + '"external_backup":["export","--directory",'
                        + repr(str(root))
                        + ',"--key-file","fictional-key","--settings","fictional-env"]}\n'
                        + 'sys.argv=[module+".py",*arguments[module]]\n'
                        + 'try:globals()[module].main()\n'
                        + 'except state_store.OperationInterrupted:print(json.dumps({"interrupted":True}),flush=True);sys.exit(1)\n'
                    )
                    parent = subprocess.Popen(
                        [sys.executable, str(driver)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                    try:
                        deadline = time.monotonic() + 5
                        while (
                            not marker.exists()
                            and parent.poll() is None
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.02)
                        self.assertTrue(marker.exists(), 'disposable child not ready')
                        parent.send_signal(sent)
                        out, err = parent.communicate(timeout=2)
                        self.assertEqual(parent.returncode, 1, err)
                        records = [
                            json.loads(line) for line in out.splitlines() if line.startswith('{')
                        ]
                        self.assertTrue(
                            any(
                                r.get('interrupted')
                                or (
                                    r.get('check') == 'failed'
                                    and 'Operation interrupted' in r.get('message', '')
                                )
                                for r in records
                            ),
                            out,
                        )
                    finally:
                        try:
                            os.killpg(parent.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        parent.communicate(timeout=5)
