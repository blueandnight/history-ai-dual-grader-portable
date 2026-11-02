import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _windows_powershell():
    return os.name == "nt" and shutil.which("powershell.exe") is not None


@unittest.skipUnless(_windows_powershell(), "Windows PowerShell is required")
@unittest.skipUnless(shutil.which("python.exe") is not None, "python.exe on PATH is required")
class LauncherEndToEndTests(unittest.TestCase):
    """Exercise the launcher only with a synthetic local HTTP service."""

    def test_allocates_system_port_and_reuses_service_json_instance(self):
        launcher = ROOT / "启动批改器.ps1"
        self.assertTrue(launcher.is_file())
        launcher_text = launcher.read_text(encoding="utf-8-sig")
        self.assertIn("[System.Net.IPAddress]::Loopback, 0", launcher_text)
        self.assertIn("-WindowStyle Hidden", launcher_text)
        instance_id = "a" * 32
        fake_app = r'''
import argparse
import http.server
import json
import os
import pathlib
import sys
import traceback

INSTANCE_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=0)
parser.add_argument("--portable-root", required=True)
args = parser.parse_args()
root = pathlib.Path(args.portable_root)
config = root / "config"
error_file = config / "launcher-test-error.txt"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps({"ok": True, "portable_instance_id": INSTANCE_ID}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *_args):
        pass

try:
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    (config / "test-pid").write_text(str(os.getpid()), encoding="ascii")
    (config / "service.json").write_text(json.dumps({"port": server.server_address[1]}), encoding="utf-8")
    server.serve_forever()
except Exception:
    error_file.write_text(traceback.format_exc(), encoding="utf-8")
    raise
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "合成便携目录"
            (root / "config").mkdir(parents=True)
            (root / "config" / "portable-instance-id").write_text(instance_id, encoding="ascii")
            (root / "app.py").write_text(fake_app, encoding="utf-8")
            shutil.copy2(launcher, root / "启动批改器.ps1")
            port_file = root / "config" / "portable-port"
            service_file = root / "config" / "service.json"
            pid_file = root / "config" / "test-pid"
            error_file = root / "config" / "launcher-test-error.txt"
            env = os.environ.copy()
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

            def run_launcher():
                return subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "启动批改器.ps1"), "-NoBrowser"],
                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                )

            try:
                first = run_launcher()
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                self.assertTrue(port_file.is_file(), first.stdout + first.stderr)
                port = int(port_file.read_text(encoding="ascii").strip())
                self.assertGreaterEqual(port, 1)
                self.assertLessEqual(port, 65535)
                self.assertFalse(error_file.exists(), error_file.read_text(encoding="utf-8", errors="replace") if error_file.exists() else "")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as response:
                    self.assertEqual(json.loads(response.read().decode("utf-8"))["portable_instance_id"], instance_id)
                first_pid = int(pid_file.read_text(encoding="ascii"))
                self.assertFalse((root / "config" / "portable-browser-opened-at").exists())

                # With portable-port absent, service.json alone must locate the same instance.
                port_file.unlink()
                second = run_launcher()
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertEqual(int(port_file.read_text(encoding="ascii").strip()), port)
                self.assertEqual(int(pid_file.read_text(encoding="ascii")), first_pid)
                self.assertTrue(service_file.is_file())
            finally:
                if pid_file.is_file():
                    try:
                        os.kill(int(pid_file.read_text(encoding="ascii")), signal.SIGTERM)
                    except (OSError, ValueError):
                        pass
                time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
