import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools import build_smoke


class BuildSmokeTests(unittest.TestCase):
    def test_child_environment_cannot_load_existing_model_credentials(self):
        with patch.dict(build_smoke.os.environ, {'HISTORY_GRADER_API_KEY': 'synthetic',
            'MIMO_API_KEY': 'synthetic', 'CUSTOM_API_KEY': 'synthetic',
            'HISTORY_GRADER_CLOUD_MODE': '1', 'LOCALAPPDATA': 'old-private-root',
            'PATH': 'keep-path', 'PYTHONPATH': 'old-pythonpath'}, clear=True):
            environment = build_smoke.smoke_environment(Path('synthetic-root'))
        self.assertEqual(environment['PATH'], 'keep-path')
        self.assertEqual(environment['PYTHONPATH'], '')
        self.assertNotEqual(environment['LOCALAPPDATA'], 'old-private-root')
        self.assertFalse(any(k.startswith('HISTORY_GRADER') or k.endswith('_API_KEY') for k in environment))

    def test_health_alone_does_not_pass_if_pdfium_is_missing_and_child_is_stopped(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / 'synthetic.exe'
            exe.write_bytes(b'not executed')
            response = MagicMock()
            response.__enter__.return_value.status = 200
            response.__enter__.return_value.read.return_value = json.dumps({'ok': True, 'pdf_renderer': False}).encode()
            opener = MagicMock()
            opener.open.return_value = response
            child = MagicMock()
            child.poll.return_value = None
            with patch.object(build_smoke, 'build_opener', return_value=opener), \
                 patch.object(build_smoke.subprocess, 'Popen', return_value=child):
                with self.assertRaisesRegex(RuntimeError, 'PDFium'):
                    build_smoke.run_smoke(exe)
            child.terminate.assert_called_once()

    def test_missing_executable_never_launches_a_process(self):
        with tempfile.TemporaryDirectory() as td, patch.object(build_smoke.subprocess, 'Popen') as spawn:
            with self.assertRaises(FileNotFoundError):
                build_smoke.run_smoke(Path(td) / 'missing.exe')
            spawn.assert_not_called()
