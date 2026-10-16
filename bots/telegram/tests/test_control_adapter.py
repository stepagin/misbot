from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import BotError
import control


class ControlAdapterTests(unittest.TestCase):
    def test_adapter_passes_injected_runner_and_statistics_period(self):
        settings = {'stack_root': '/fictional/stack', 'management_actions': ['statistics']}
        with (
            patch('control.subprocess.run') as runner,
            patch('control.server_management.run_control', return_value={'ok': True}) as shared,
        ):
            self.assertEqual(
                control.run_control(settings, 'statistics', period='month'), {'ok': True}
            )
        shared.assert_called_once_with(settings, 'statistics', period='month', runner=runner)

    def test_adapter_hides_unsafe_backend_exception_details(self):
        for error in (
            RuntimeError('FICTIONAL_SECRET'),
            OSError('FICTIONAL_SECRET'),
            ValueError('FICTIONAL_SECRET'),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch('control.server_management.run_control', side_effect=error),
            ):
                with self.assertRaises(BotError) as caught:
                    control.run_control({}, 'status')
                self.assertNotIn('FICTIONAL_SECRET', str(caught.exception))

    def test_command_definitions_come_from_shared_backend(self):
        self.assertIs(control.COMMANDS, control.server_management.COMMANDS)
        self.assertIs(control.WRITES, control.server_management.WRITES)
        self.assertIs(control.subprocess, subprocess)

    @staticmethod
    def copied_adapter(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(Path(control.__file__).read_text())
        spec = importlib.util.spec_from_file_location('fixture_control_adapter', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def backend_fixture(folder):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'server_management.py').write_text(
            "COMMANDS = {'status': ['status']}\nWRITES = frozenset()\n"
            "def run_control(config, operation, **kwargs):\n    return {'ok': True}\n"
            'class command_wrapper:\n    @staticmethod\n'
            '    def trusted_installation(root):\n        return root\n'
        )
        (folder / 'command_wrapper.py').write_text('pass\n')

    def test_packaged_telegram_bot_imports_from_installation_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'package'
            self.backend_fixture(folder)
            adapter = self.copied_adapter(folder / 'telegram-bot/control.py')
            backend = adapter.load_backend()
            self.assertEqual(Path(backend.__file__), folder / 'server_management.py')
            self.assertEqual(adapter.run_control({}, 'status'), {'ok': True})

    def test_standalone_optional_bridge_imports_before_management_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            adapter = self.copied_adapter(folder / 'lib/misbot/telegram/control.py')
            self.assertEqual(adapter._BACKENDS, {})
            runtime = folder / 'stack'
            self.backend_fixture(runtime)
            backend = adapter.load_backend({'stack_root': str(runtime)})
            self.assertEqual(Path(backend.__file__), runtime / 'server_management.py')
            self.assertEqual(
                adapter.run_control({'stack_root': str(runtime)}, 'status'), {'ok': True}
            )


if __name__ == '__main__':
    unittest.main()
