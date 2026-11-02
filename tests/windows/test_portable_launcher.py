import sys
import unittest
from pathlib import Path


if sys.platform == "win32":
    class WindowsPortableLauncherTests(unittest.TestCase):
        def test_batch_launcher_delegates_path_resolution_to_powershell_script(self):
            launcher = (Path(__file__).resolve().parents[2] / "启动批改器.bat").read_text(encoding="utf-8-sig")
            self.assertIn("start.ps1", launcher)
            self.assertNotIn("GetEnvironmentVariable", launcher)
            self.assertNotIn("启动批改器.ps1", launcher)
