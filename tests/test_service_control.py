import base64
import csv
import ctypes
import io
import json
import os
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image

import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "app.py"


@contextmanager
def restart_test_directory():
    """Allow Windows a bounded interval to release a just-exited process cwd."""
    temp_root = Path(tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix="history-service-restart-")).resolve()
    if root.parent != temp_root:
        raise RuntimeError("Restart test temporary directory escaped TEMP")
    try:
        yield str(root)
    finally:
        for attempt in range(30):
            try:
                shutil.rmtree(root)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(.1)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(
    port: int,
    path: str,
    method: str = "GET",
    host: str = "127.0.0.1",
    headers: dict | None = None,
    payload: dict | None = None,
) -> dict:
    request = Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(payload or {}).encode("utf-8") if method == "POST" else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


class SyntheticRemoteAddressHTTPServer(ThreadingHTTPServer):
    """Use a deterministic non-loopback peer address without external routes."""

    def get_request(self):
        request, address = super().get_request()
        return request, ("192.0.2.77", address[1])


@contextmanager
def isolated_mobile_test_servers(portable_root: Path):
    previous_paths = app.APP_PATHS
    environment_patch = patch.dict(
        os.environ,
        {
            "HISTORY_GRADER_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
            "HISTORY_GRADER_WORKERS": "1",
        },
    )
    network_patch = patch.object(app, "cached_local_network_interfaces", return_value=[])
    servers = []
    threads = []
    environment_patch.start()
    network_patch.start()
    try:
        app.configure_paths(portable_root, PROJECT_ROOT)
        conn = app.get_db()
        conn.close()
        for server_type in (ThreadingHTTPServer, SyntheticRemoteAddressHTTPServer):
            server = server_type(("127.0.0.1", 0), app.AppHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        yield servers[0].server_address[1], servers[1].server_address[1]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        app.stop_grade_worker(timeout_seconds=1)
        app.revoke_mobile_token()
        with app.ENQUEUED_SUBMISSIONS_LOCK:
            app.ENQUEUED_SUBMISSIONS.clear()
        app._install_paths(previous_paths)
        network_patch.stop()
        environment_patch.stop()


def port_occupancy(port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return "listening" if sock.connect_ex(("127.0.0.1", port)) == 0 else "closed"


def open_process_exit_probe(process_id: int):
    if os.name != "nt" or process_id <= 0:
        return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000 | 0x00001000, False, process_id)
    return (kernel, handle) if handle else None


def queried_process_exit_code(probe) -> int | None:
    if probe is None:
        return None
    from ctypes import wintypes

    kernel, handle = probe
    if kernel.WaitForSingleObject(handle, 0) != 0:
        return None
    exit_code = wintypes.DWORD()
    if kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
        return int(exit_code.value)
    return None


def safe_startup_summary(lines: list[str]) -> list[str]:
    selected = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        folded = line.casefold()
        if "历史学科 ai 双评阅卷系统已启动" in folded:
            label = "service_started"
        elif "视觉模型" in line:
            label = "model_unconfigured_rules_mode" if "未配置" in line else "model_configured_status_only"
        elif any(marker in line for marker in ("程序资源目录", "数据目录", "配置目录")):
            label = "filesystem_path_line_redacted"
        elif "便携实例" in line:
            label = "instance_identifier_redacted"
        elif any(marker in folded for marker in ("traceback", "exception", "error", "failed")) or "启动失败" in line:
            label = "startup_error_detail_redacted"
        else:
            continue
        if label not in selected:
            selected.append(label)
    return selected[-8:]


class ServiceControlTests(unittest.TestCase):
    def start_service(
        self,
        portable_root: Path,
        port: int,
        host: str = "127.0.0.1",
        separate_stderr: bool = False,
    ) -> subprocess.Popen:
        environment = os.environ.copy()
        environment.pop("HISTORY_GRADER_API_KEY", None)
        environment.pop("DEEPSEEK_API_KEY", None)
        return subprocess.Popen(
            [
                sys.executable,
                str(APP_FILE),
                "--host",
                host,
                "--port",
                str(port),
                "--portable-root",
                str(portable_root),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
            text=True,
        )

    def wait_for_status(self, port: int, process: subprocess.Popen, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                self.fail(f"服务提前退出（{process.returncode}）：{output}")
            try:
                return request_json(port, "/api/status")
            except (OSError, URLError):
                time.sleep(0.1)
        self.fail("服务在测试超时时间内没有就绪")

    def stop_service(self, port: int, process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                request_json(port, "/api/shutdown", "POST")
            except (OSError, URLError):
                process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout and not getattr(process, "_diagnostic_streams", False):
            process.stdout.read()
            process.stdout.close()
        if process.stderr and not getattr(process, "_diagnostic_streams", False):
            process.stderr.read()
            process.stderr.close()

    @staticmethod
    def update_temp_database(portable_root: Path, statement: str, values: tuple = ()) -> None:
        """Execute a fixture-only write and explicitly close its SQLite handle."""
        conn = sqlite3.connect(portable_root / "data" / "history_grading.sqlite3")
        try:
            conn.execute(statement, values)
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def mark_temp_submission_confirmed(cls, portable_root: Path, submission_id: str) -> None:
        """Prepare an already-confirmed fixture state in this test's temp DB."""
        cls.update_temp_database(
            portable_root,
            "UPDATE submissions SET final_total = 0, confirmed_at = 'test-confirmed' WHERE id = ?",
            (submission_id,),
        )

    def test_dual_review_readiness_endpoint_is_get(self):
        with tempfile.TemporaryDirectory() as directory:
            port = free_port()
            portable_root = Path(directory) / "portable"
            process = self.start_service(portable_root, port)
            try:
                self.wait_for_status(port, process)
                readiness = request_json(port, "/api/dual-review/status")
                self.assertEqual(readiness["model_a"], "deepseek-flash")
                self.assertEqual(readiness["model_b"], "mimo-v2.6-flash")
                self.assertEqual(readiness["provider_label_a"], "DeepSeek")
                self.assertEqual(readiness["provider_label_b"], "小米 MiMo")
                self.assertEqual(readiness["preset_a"], "deepseek-v41-flash")
                self.assertEqual(readiness["preset_b"], "mimo-v26-flash")
                self.assertTrue(any(item["id"] == "openai-gpt-41" for item in readiness["available_models"]))
                self.assertIn("configured_a", readiness)
                self.assertIn("configured_b", readiness)
                self.assertIn("primary_model_configured", readiness)
            finally:
                self.stop_service(port, process)

    def test_local_rubric_preview_and_send_reuse_ai_a_key_not_legacy_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_mobile_test_servers(Path(directory) / "portable") as (local_port, _):
                model_config = {
                    "provider": "qwen",
                    "provider_label": "通义千问",
                    "model": "qwen3.8-max",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "api_key": "SYNTHETIC_AI_A_KEY",
                }
                request = {
                    "question": "材料题题干",
                    "sub_questions": [{
                        "id": "Q1", "label": "（1）", "question": "概括背景",
                        "standard_answer": "土地兼并严重", "scoring_mode": "weighted", "point_value": 2,
                    }],
                }
                with patch.object(app, "cloud_mode_enabled", return_value=False), patch.object(
                    app, "model_configured", return_value=False
                ), patch.object(app, "load_dual_review_model_configs", return_value={"A": dict(model_config)}) as load_models:
                    preview = request_json(local_port, "/api/rubric-payload-preview", "POST", payload=request)
                    self.assertEqual(preview["model_role"], "AI-A")
                    self.assertEqual(preview["payload"]["model"], "qwen3.8-max")
                    self.assertEqual(preview["provider"], "通义千问")
                    self.assertNotIn("SYNTHETIC_AI_A_KEY", json.dumps(preview, ensure_ascii=False))

                    model_response = {"sub_questions": [{
                        "sub_question_id": "Q1",
                        "criteria": [{"criterion_id": "C1", "label": "土地兼并严重", "score": 2, "keywords": []}],
                    }]}
                    context = MagicMock()
                    context.__enter__.return_value = object()
                    with patch.object(app, "open_model_request", return_value=context) as open_request, patch.object(
                        app, "read_model_chat_content", return_value=json.dumps(model_response, ensure_ascii=False)
                    ):
                        generated = request_json(
                            local_port,
                            "/api/generate-rubric",
                            "POST",
                            payload={**request, "rubric_preview_token": preview["preview_token"]},
                        )
                    self.assertEqual(generated["sub_questions"][0]["criteria"][0]["label"], "土地兼并严重")
                    self.assertEqual(open_request.call_args.args[0].headers.get("Authorization"), "Bearer SYNTHETIC_AI_A_KEY")
                    self.assertEqual(load_models.call_args_list[0].args, (("A",),))
                    self.assertEqual(load_models.call_args_list[1].args, (("A",),))

    def test_dual_review_models_are_selectable_and_credentials_are_side_specific(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            with isolated_mobile_test_servers(root) as (local_port, remote_port):
                with patch.object(app, "save_dpapi_api_key") as save_key:
                    for side, provider, origin in (
                        ("A", "openai", "https://api.openai.com"),
                        ("B", "google", "https://generativelanguage.googleapis.com"),
                    ):
                        secret = f"test-secret-{side}"
                        result = request_json(
                            local_port,
                            "/api/dual-review/configuration",
                            "POST",
                            payload={
                                "side": side,
                                "preset_id": "openai-gpt-41" if side == "A" else "gemini-38-flash",
                                "api_key": secret,
                            },
                        )
                        self.assertTrue(result["ok"])
                        self.assertNotIn(secret, json.dumps(result))
                    self.assertEqual(save_key.call_args_list[0].args, ("test-secret-A", "dual_a_openai"))
                    self.assertEqual(save_key.call_args_list[0].kwargs["api_origin"], "https://api.openai.com")
                    self.assertEqual(save_key.call_args_list[1].args, ("test-secret-B", "dual_b_google"))
                    self.assertEqual(save_key.call_args_list[1].kwargs["api_origin"], "https://generativelanguage.googleapis.com")

                selected = request_json(local_port, "/api/dual-review/status")
                self.assertEqual(selected["preset_a"], "openai-gpt-41")
                self.assertEqual(selected["preset_b"], "gemini-38-flash")
                self.assertTrue(selected["pair_distinct"])
                self.assertEqual(
                    json.loads((root / "config" / "dual-review-models.json").read_text(encoding="utf-8"))["models"],
                    {"A": "openai-gpt-41", "B": "gemini-38-flash"},
                )

                with self.assertRaises(HTTPError) as remote_error:
                    request_json(
                        remote_port,
                        "/api/dual-review/configuration",
                        "POST",
                        payload={"side": "A", "preset_id": "openai-gpt-41", "api_key": "must-not-save"},
                    )
                self.assertIn(remote_error.exception.code, {401, 403})

                with self.assertRaises(HTTPError) as invalid_side:
                    request_json(
                        local_port,
                        "/api/dual-review/configuration",
                        "POST",
                        payload={"side": "C", "preset_id": "openai-gpt-41", "api_key": "test"},
                    )
                self.assertEqual(invalid_side.exception.code, 400)

    def test_single_instance_and_safe_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            first = self.start_service(root, port)
            try:
                status = self.wait_for_status(port, first)
                self.assertIn("network_addresses", status)
                self.assertIsInstance(status["network_addresses"], list)
                self.assertEqual(status["service_port"], port)
                self.assertIn("grade_worker_count", status)
                self.assertIn("active_grade_workers", status)
                self.assertIn("queued_in_memory", status)
                self.assertGreaterEqual(status["active_grade_workers"], 1)
                self.assertLessEqual(status["active_grade_workers"], status["grade_worker_count"])
                second = self.start_service(root, port)
                second_output, _ = second.communicate(timeout=8)
                self.assertEqual(second.returncode, 2)
                self.assertIn("已经在运行", second_output)

                request_json(port, "/api/shutdown", "POST")
                first.wait(timeout=8)
                self.assertEqual(first.returncode, 0)
                self.assertEqual(status["portable_instance_id"], (root / "config" / "portable-instance-id").read_text(encoding="ascii").strip())
                self.assertTrue((root / "config" / "service.lock").exists())
                self.assertFalse((root / "config" / "service.json").exists())
            finally:
                self.stop_service(port, first)

    def test_port_conflict_has_clear_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            occupier.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupier.bind(("127.0.0.1", port))
            occupier.listen(1)
            try:
                process = self.start_service(root, port)
                output, _ = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 3)
                self.assertIn(f"端口 {port}", output)
            finally:
                occupier.close()

    @unittest.skipUnless(
        sys.platform == "win32",
        "便携目录的应用内重启属于 Windows 本地模式；Linux 云端生命周期由 systemd 管理",
    )
    def test_restart_releases_old_process_and_keeps_instance(self):
        with restart_test_directory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            first = self.start_service(root, port, separate_stderr=True)
            first._diagnostic_streams = True
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            stdout_reader = threading.Thread(
                target=lambda: stdout_lines.extend(line.rstrip("\r\n") for line in first.stdout),
                daemon=True,
            )
            stderr_reader = threading.Thread(
                target=lambda: stderr_lines.extend(line.rstrip("\r\n") for line in first.stderr),
                daemon=True,
            )
            stdout_reader.start()
            stderr_reader.start()
            replacement_status = None
            replacement_probe = None
            replacement_pid = None
            shutdown_requested = False
            state_changes = []
            started_at = time.monotonic()
            wait_started_at = started_at
            readiness_wait_seconds = 0.0
            last_state = None
            try:
                first_status = self.wait_for_status(port, first)
                response = request_json(port, "/api/restart", "POST")
                self.assertTrue(response["ok"])
                first.wait(timeout=10)
                wait_started_at = time.monotonic()
                initial_port_state = port_occupancy(port)
                state_changes.append(
                    {
                        "after_seconds": 0.0,
                        "status": "not_yet_probed",
                        "port": initial_port_state,
                        "service_pid": None,
                    }
                )
                last_state = ("not_yet_probed", initial_port_state, None)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    try:
                        replacement_status = request_json(port, "/api/status")
                        status_state = "ready"
                        observed_pid = int(replacement_status.get("service_pid") or 0)
                    except (OSError, URLError):
                        replacement_status = None
                        status_state = "unavailable"
                        observed_pid = 0
                    port_state = port_occupancy(port)
                    elapsed = round(time.monotonic() - wait_started_at, 2)
                    current_state = (status_state, port_state, observed_pid or None)
                    if current_state != last_state:
                        state_changes.append(
                            {
                                "after_seconds": elapsed,
                                "status": status_state,
                                "port": port_state,
                                "service_pid": observed_pid or None,
                            }
                        )
                        last_state = current_state
                    if replacement_status is not None:
                        replacement_pid = observed_pid
                        replacement_probe = open_process_exit_probe(replacement_pid)
                        break
                    time.sleep(0.2)
                readiness_wait_seconds = round(time.monotonic() - wait_started_at, 2)
                if replacement_status is not None:
                    self.assertEqual(first_status["portable_instance_id"], replacement_status["portable_instance_id"])
                    request_json(port, "/api/shutdown", "POST")
                    shutdown_requested = True
                else:
                    self.fail("replacement service did not become ready within 12 seconds")
            finally:
                if port_occupancy(port) == "listening" and not shutdown_requested:
                    try:
                        request_json(port, "/api/shutdown", "POST")
                        shutdown_requested = True
                    except HTTPError as error:
                        error.close()
                    except (OSError, URLError):
                        pass
                shutdown_deadline = time.monotonic() + 8
                replacement_exit_code = None
                while time.monotonic() < shutdown_deadline:
                    replacement_exit_code = queried_process_exit_code(replacement_probe)
                    if port_occupancy(port) == "closed" and (
                        replacement_probe is None or replacement_exit_code is not None
                    ):
                        break
                    time.sleep(0.1)
                self.stop_service(port, first)
                stdout_reader.join(timeout=2)
                stderr_reader.join(timeout=2)
                for stream in (first.stdout, first.stderr):
                    if stream and not stream.closed:
                        stream.close()
                if replacement_probe is not None:
                    kernel, handle = replacement_probe
                    kernel.CloseHandle(handle)
                diagnostics = {
                    "replacement_observed": replacement_pid is not None and replacement_pid != first.pid,
                    "replacement_pid": replacement_pid,
                    "replacement_exit_code": replacement_exit_code if replacement_probe else "unavailable on this platform",
                    "old_process_pid": first.pid,
                    "old_process_exit_code": first.returncode,
                    "port_and_status_changes_during_wait": state_changes,
                    "wait_seconds": readiness_wait_seconds,
                    "stdout_summary": safe_startup_summary(stdout_lines),
                    "stderr_summary": safe_startup_summary(stderr_lines),
                }
                print("RESTART_TEST_DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=True))

    def test_mobile_api_requires_temporary_bearer_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            with isolated_mobile_test_servers(root) as (local_port, remote_port):
                with self.assertRaises(HTTPError) as error:
                    request_json(remote_port, "/api/status")
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                token = request_json(local_port, "/api/mobile-token", "POST")["token"]
                authorized = request_json(
                    remote_port,
                    "/api/status",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertTrue(authorized["ok"])
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        remote_port,
                        "/api/ai-payload-preview",
                        "POST",
                        headers={"Authorization": f"Bearer {token}"},
                        payload={},
                    )
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        remote_port,
                        "/api/custom-models",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                self.assertEqual(error.exception.code, 403)
                error.exception.close()

    def test_local_can_delete_one_submission_photo_cache_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                assignment = request_json(
                    port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "照片缓存清理测试题",
                        "question": "根据材料回答问题",
                        "sub_questions": [{
                            "id": "Q1",
                            "label": "（1）",
                            "question": "概括特点",
                            "criteria": [{"label": "特点", "score": 2, "keywords": ["特点"]}],
                        }],
                    },
                )
                assignment_id = assignment["assignment"]["id"]
                image = Image.new("RGB", (1000, 800), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                created = request_json(
                    port,
                    "/api/submissions",
                    "POST",
                    payload={
                        "assignment_id": assignment_id,
                        "student_label": "照片清理学生",
                        "answer_text": "特点",
                        "idempotency_key": "photo-cache-cleanup-test",
                        "photos": [{"name": "student.jpg", "data_url": data_url}],
                    },
                )
                submission_id = created["submission"]["id"]
                deadline = time.monotonic() + 8
                completed = None
                while time.monotonic() < deadline:
                    completed = request_json(port, f"/api/submission?submission_id={submission_id}")["submission"]
                    if completed["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)
                self.assertEqual(completed["state"], "completed")
                self.assertEqual(len(completed["photos"]), 1)
                photo_path = root / completed["photos"][0]["path"]
                student_dir = photo_path.parent
                self.assertTrue(photo_path.exists())
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        port,
                        "/api/delete-submission-photos",
                        "POST",
                        payload={"submission_id": submission_id},
                    )
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                self.mark_temp_submission_confirmed(root, submission_id)
                deleted = request_json(
                    port,
                    "/api/delete-submission-photos",
                    "POST",
                    payload={"submission_id": submission_id},
                )
                self.assertTrue(deleted["ok"])
                self.assertTrue(deleted["submission"]["photos_deleted"])
                self.assertEqual(deleted["submission"]["photos"], [])
                self.assertFalse(student_dir.exists())
                persisted = request_json(port, f"/api/submission?submission_id={submission_id}")["submission"]
                self.assertEqual(persisted["id"], submission_id)
                self.assertEqual(persisted["assignment_id"], assignment_id)
                self.assertEqual(persisted["result"]["mode"], "rules-demo")
                self.assertTrue(persisted["photos_deleted"])
                with self.assertRaises(HTTPError) as error:
                    request_json(port, "/api/retry", "POST", payload={"submission_id": submission_id})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                assignments = request_json(port, "/api/assignments")["assignments"]
                self.assertEqual(assignments[0]["id"], assignment_id)
                self.assertEqual(assignments[0]["submission_count"], 1)
            finally:
                self.stop_service(port, process)

    def test_mobile_can_review_original_and_confirm_its_blocked_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            with isolated_mobile_test_servers(root) as (local_port, remote_port):
                assignment = request_json(
                    local_port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "手机教师复核测试题",
                        "question": "合成测试题，不包含真实学生数据",
                        "sub_questions": [{
                            "id": "Q1",
                            "label": "（1）",
                            "question": "概括特点",
                            "criteria": [{"label": "特点", "score": 2, "keywords": ["特点"]}],
                        }],
                    },
                )
                token = request_json(local_port, "/api/mobile-token", "POST")["token"]
                image = Image.new("RGB", (128, 96), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                headers = {"Authorization": f"Bearer {token}"}
                with patch.object(app, "enqueue_submission"):
                    created = request_json(
                        remote_port,
                        "/api/submissions",
                        "POST",
                        headers=headers,
                        payload={
                            "assignment_id": assignment["assignment"]["id"],
                            "student_label": "SYNTHETIC-MOBILE-REVIEW",
                            "answer_text": "合成测试答案",
                            "idempotency_key": "mobile-review-confirmation-test",
                            "photos": [{"name": "synthetic.jpg", "data_url": data_url}],
                        },
                    )
                submission_id = created["submission"]["id"]
                result = {
                    "mode": "vision-model",
                    "blocked": True,
                    "suggested_total": 1,
                    "max_total": 2,
                    "sub_questions": [{"sub_question_id": "Q1", "answer_status": "answered", "criteria": []}],
                }
                self.update_temp_database(
                    root,
                    "UPDATE submissions SET state = 'completed', result_json = ? WHERE id = ?",
                    (json.dumps(result, ensure_ascii=False), submission_id),
                )

                # The phone must first open this mobile-submitted original.
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        remote_port,
                        "/api/review",
                        "POST",
                        headers=headers,
                        payload={"submission_id": submission_id, "confirm_original": True},
                    )
                self.assertEqual(error.exception.code, 400)
                error.exception.close()

                photo_url = f"http://127.0.0.1:{remote_port}/api/submission-photo?submission_id={submission_id}&photo_index=1"
                with urlopen(Request(photo_url, headers=headers), timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                    self.assertTrue(response.read().startswith(b"\xff\xd8"))
                self.update_temp_database(
                    root, "UPDATE submissions SET source_channel = 'local' WHERE id = ?", (submission_id,)
                )
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        remote_port,
                        "/api/review",
                        "POST",
                        headers=headers,
                        payload={"submission_id": submission_id, "confirm_original": True},
                    )
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                self.update_temp_database(
                    root, "UPDATE submissions SET source_channel = 'mobile' WHERE id = ?", (submission_id,)
                )
                reviewed = request_json(
                    remote_port,
                    "/api/review",
                    "POST",
                    headers=headers,
                    payload={"submission_id": submission_id, "confirm_original": True, "review_note": "synthetic"},
                )
                self.assertTrue(reviewed["ok"])
                confirmed = request_json(
                    remote_port,
                    "/api/confirm",
                    "POST",
                    headers=headers,
                    payload={"submission_id": submission_id, "teacher_point_decisions": [{"question_id": "Q1", "rubric_point_id": "C1", "choice": "teacher_full"}]},
                )
                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["final_total"], 2)

    def test_mobile_can_delete_photo_cache_for_its_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            with isolated_mobile_test_servers(root) as (local_port, remote_port):
                assignment = request_json(
                    local_port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "手机照片清理测试题",
                        "question": "根据材料回答问题",
                        "sub_questions": [{
                            "id": "Q1",
                            "label": "（1）",
                            "question": "概括特点",
                            "criteria": [{"label": "特点", "score": 2, "keywords": ["特点"]}],
                        }],
                    },
                )
                token = request_json(local_port, "/api/mobile-token", "POST")["token"]
                image = Image.new("RGB", (1000, 800), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                created = request_json(
                    remote_port,
                    "/api/submissions",
                    "POST",
                    headers={"Authorization": f"Bearer {token}"},
                    payload={
                        "assignment_id": assignment["assignment"]["id"],
                        "student_label": "手机学生",
                        "answer_text": "特点",
                        "idempotency_key": "mobile-photo-cache-cleanup-test",
                        "photos": [{"name": "mobile.jpg", "data_url": data_url}],
                    },
                )
                submission_id = created["submission"]["id"]
                deadline = time.monotonic() + 8
                completed = None
                while time.monotonic() < deadline:
                    completed = request_json(remote_port, f"/api/submission?submission_id={submission_id}", headers={"Authorization": f"Bearer {token}"})["submission"]
                    if completed["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)
                self.assertEqual(completed["state"], "completed")
                photo_path = root / completed["photos"][0]["path"]
                photo_url = f"http://127.0.0.1:{remote_port}/api/submission-photo?submission_id={submission_id}&photo_index=1"
                photo_request = Request(photo_url, headers={"Authorization": f"Bearer {token}"})
                with urlopen(photo_request, timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                    self.assertEqual(response.headers.get("Cache-Control"), "no-store, private")
                    self.assertTrue(response.read().startswith(b"\xff\xd8"))
                own_record = request_json(
                    remote_port,
                    f"/api/submission?submission_id={submission_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(own_record["submission"]["source_channel"], "mobile")
                evidence_image = Image.new("RGB", (32, 32), "white")
                evidence_path = photo_path.parent / "evidence" / "Q1_C1.jpg"
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_image.save(evidence_path, format="JPEG")
                fake_result = {
                    "mode": "vision-model",
                    "sub_questions": [{
                        "sub_question_id": "Q1",
                        "criteria": [{
                            "criterion_id": "C1",
                            "evidence": {"crop_path": evidence_path.relative_to(root).as_posix()},
                        }],
                    }],
                }
                self.update_temp_database(
                    root,
                    "UPDATE submissions SET result_json = ? WHERE id = ?",
                    (json.dumps(fake_result), submission_id),
                )
                evidence_url = (
                    f"http://127.0.0.1:{remote_port}/api/submission-evidence?submission_id={submission_id}"
                    "&sub_question_id=Q1&criterion_id=C1"
                )
                with urlopen(Request(evidence_url, headers={"Authorization": f"Bearer {token}"}), timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                    self.assertTrue(response.read().startswith(b"\xff\xd8"))
                fake_result["sub_questions"][0]["criteria"][0]["evidence"]["crop_path"] = "data/config/../../outside.jpg"
                self.update_temp_database(
                    root,
                    "UPDATE submissions SET result_json = ? WHERE id = ?",
                    (json.dumps(fake_result), submission_id),
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(evidence_url, headers={"Authorization": f"Bearer {token}"}), timeout=2)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
                self.update_temp_database(
                    root,
                    "UPDATE submissions SET result_json = ? WHERE id = ?",
                    (json.dumps(completed["result"], ensure_ascii=False), submission_id),
                )
                local_created = request_json(
                    local_port,
                    "/api/submissions",
                    "POST",
                    payload={
                        "assignment_id": assignment["assignment"]["id"],
                        "student_label": "本机提交测试样本",
                        "answer_text": "特点",
                        "idempotency_key": "local-photo-scope-test",
                        "photos": [{"name": "local.jpg", "data_url": data_url}],
                    },
                )
                local_submission_id = local_created["submission"]["id"]
                local_deadline = time.monotonic() + 8
                while time.monotonic() < local_deadline:
                    local_record = request_json(local_port, f"/api/submission?submission_id={local_submission_id}")["submission"]
                    if local_record["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)
                self.assertEqual(local_record["source_channel"], "local")
                for path in (
                    f"/api/submission?submission_id={local_submission_id}",
                    f"/api/submission-photo?submission_id={local_submission_id}&photo_index=1",
                ):
                    with self.assertRaises(HTTPError) as error:
                        request_json(
                            remote_port,
                            path,
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        remote_port,
                        "/api/delete-submission-photos",
                        "POST",
                        headers={"Authorization": f"Bearer {token}"},
                        payload={"submission_id": submission_id},
                    )
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                self.mark_temp_submission_confirmed(root, submission_id)
                deleted = request_json(
                    remote_port,
                    "/api/delete-submission-photos",
                    "POST",
                    headers={"Authorization": f"Bearer {token}"},
                    payload={"submission_id": submission_id},
                )
                self.assertTrue(deleted["ok"])
                self.assertTrue(deleted["submission"]["photos_deleted"])
                self.assertEqual(deleted["submission"]["photos"], [])
                # The mobile route is intentionally reachable, but the normal
                # safety gate still rejects the rules-demo result as a formal score.
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        remote_port,
                        "/api/confirm",
                        "POST",
                        headers={"Authorization": f"Bearer {token}"},
                        payload={"submission_id": submission_id, "final_total": 0},
                    )
                self.assertEqual(error.exception.code, 400)
                error.exception.close()

    def test_confirm_rejects_nonfinite_scores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                for invalid_score in (float("nan"), float("inf"), float("-inf")):
                    with self.subTest(score=invalid_score):
                        with self.assertRaises(HTTPError) as error:
                            request_json(
                                port,
                                "/api/confirm",
                                "POST",
                                payload={"submission_id": "a" * 12, "final_total": invalid_score},
                            )
                        self.assertEqual(error.exception.code, 400)
                        self.assertIn("有限数字", error.exception.read().decode("utf-8"))
                        error.exception.close()
            finally:
                self.stop_service(port, process)

    def test_formal_csv_contains_only_confirmed_vision_results_and_neutralizes_formulas(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                assignment = request_json(
                    port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "CSV隔离测试题",
                        "question": "测试题干",
                        "standard_answer": "测试参考答案",
                        "criteria": [{"label": "测试要点", "score": 2}],
                    },
                )["assignment"]
                self.update_temp_database(
                    root,
                    "INSERT INTO roster_students (id, class_name, student_no, student_name, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("csv-synthetic-roster", "合成班级", "SYN-CSV-001", "confirmed sample", "2026-01-01T00:00:00+00:00"),
                )
                image = Image.new("RGB", (400, 300), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                submitted = []
                for label, key in (("confirmed sample", "csv-confirmed"), ("unconfirmed sample", "csv-unconfirmed")):
                    value = request_json(
                        port,
                        "/api/submissions",
                        "POST",
                        payload={
                            "assignment_id": assignment["id"],
                            "class_name": "合成班级",
                            "student_label": label,
                            "answer_text": "测试答案",
                            "idempotency_key": key,
                            "photos": [{"name": "sample.jpg", "data_url": data_url}],
                        },
                    )["submission"]
                    submitted.append(value["id"])
                for submission_id in submitted:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        record = request_json(port, f"/api/submission?submission_id={submission_id}")["submission"]
                        if record["state"] in {"completed", "failed"}:
                            break
                        time.sleep(0.1)
                safe_result = {
                    "mode": "vision-model", "suggested_total": 2, "max_total": 2,
                    "blocked": False, "summary": "=HYPERLINK(\"https://invalid.example\")", "sub_questions": [],
                }
                self.update_temp_database(
                    root,
                    "UPDATE submissions SET state='completed', result_json=?, final_total=2, confirmed_at='test-confirmed' WHERE id=?",
                    (json.dumps(safe_result, ensure_ascii=False), submitted[0]),
                )
                request_json(
                    port,
                    "/api/bind-student",
                    "POST",
                    payload={
                        "submission_id": submitted[0],
                        "roster_student_id": "csv-synthetic-roster",
                    },
                )
                with urlopen(f"http://127.0.0.1:{port}/api/export.csv?assignment_id={assignment['id']}", timeout=2) as response:
                    body = response.read().decode("utf-8-sig")
                rows = list(csv.reader(io.StringIO(body)))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[1][2], "confirmed sample")
                summary_column = rows[0].index("评语摘要")
                self.assertEqual(rows[1][summary_column], "'=HYPERLINK(\"https://invalid.example\")")
                self.assertIn("批次原始序号", rows[0])
            finally:
                self.stop_service(port, process)

    def test_manual_review_and_score_changes_are_versioned_and_audited(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                assignment = request_json(
                    port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "审核记录测试题", "question": "测试题干", "standard_answer": "参考答案",
                        "sub_questions": [{"id": "Q1", "label": "（1）", "question": "测试小题",
                            "standard_answer": "测试要点", "criteria": [{"id": "C1", "label": "要点", "score": 2}]}],
                    },
                )["assignment"]
                image = Image.new("RGB", (600, 400), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                submission = request_json(
                    port,
                    "/api/submissions",
                    "POST",
                    payload={
                        "assignment_id": assignment["id"], "student_label": "审计隔离样本", "answer_text": "测试答案",
                        "idempotency_key": "manual-review-audit-test",
                        "photos": [{"name": "sample.jpg", "data_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")}],
                    },
                )["submission"]
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    record = request_json(port, f"/api/submission?submission_id={submission['id']}")["submission"]
                    if record["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)
                photo_url = f"http://127.0.0.1:{port}/api/submission-photo?submission_id={submission['id']}&photo_index=1"
                with urlopen(photo_url, timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                fake_result = {
                    "mode": "vision-model", "blocked": True, "block_reasons": ["无效页码引用需复核"],
                    "suggested_total": 1, "max_total": 2, "summary": "模拟复核结果",
                    "sub_questions": [{"sub_question_id": "Q1", "suggested_total": 1, "max_total": 2,
                        "criteria": [{"criterion_id": "C1", "suggested_score": 1, "max_score": 2,
                            "evidence": {"photo_index": 99}}]}],
                }
                self.update_temp_database(
                    root, "UPDATE submissions SET result_json=? WHERE id=?",
                    (json.dumps(fake_result, ensure_ascii=False), submission["id"]),
                )
                with self.assertRaises(HTTPError) as error:
                    request_json(
                        port, "/api/confirm", "POST",
                        payload={"submission_id": submission["id"], "final_total": 1},
                    )
                self.assertEqual(error.exception.code, 400)
                self.assertIn("安全判分门禁", error.exception.read().decode("utf-8"))
                error.exception.close()
                request_json(
                    port, "/api/review", "POST",
                    payload={"submission_id": submission["id"], "confirm_original": True, "review_note": "测试复核说明"},
                )
                for invalid_total in (-0.1, 2.1):
                    with self.subTest(final_total=invalid_total):
                        with self.assertRaises(HTTPError) as error:
                            request_json(
                                port, "/api/confirm", "POST",
                                payload={"submission_id": submission["id"], "final_total": invalid_total, "teacher_point_decisions": [{"question_id": "Q1", "rubric_point_id": "C1", "choice": "teacher_none"}]},
                            )
                        self.assertEqual(error.exception.code, 400)
                        self.assertIn("teacher_final_total_mismatch", error.exception.read().decode("utf-8"))
                        error.exception.close()
                request_json(port, "/api/confirm", "POST", payload={"submission_id": submission["id"], "teacher_point_decisions": [{"question_id": "Q1", "rubric_point_id": "C1", "choice": "teacher_none"}]})
                request_json(port, "/api/confirm", "POST", payload={"submission_id": submission["id"], "teacher_point_decisions": [{"question_id": "Q1", "rubric_point_id": "C1", "choice": "teacher_full"}]})
                conn = sqlite3.connect(root / "data" / "history_grading.sqlite3")
                try:
                    events = conn.execute(
                        "SELECT event_type, payload_json FROM audit_events WHERE entity_id=? ORDER BY id",
                        (submission["id"],),
                    ).fetchall()
                finally:
                    conn.close()
                reviewed = [json.loads(payload) for event_type, payload in events if event_type == "submission_reviewed"]
                confirmations = [json.loads(payload) for event_type, payload in events if event_type == "submission_confirmed"]
                self.assertEqual(len(reviewed), 1)
                self.assertFalse(reviewed[0]["previous_manual_reviewed"])
                self.assertEqual(reviewed[0]["operation_source"], "computer-local")
                self.assertEqual(len(confirmations), 2)
                self.assertIsNone(confirmations[0]["previous_final_total"])
                self.assertEqual(confirmations[0]["ai_suggested_total"], 1)
                self.assertEqual(confirmations[0]["final_total"], 0)
                self.assertEqual(confirmations[1]["previous_final_total"], 0)
                self.assertEqual(confirmations[1]["final_total"], 2)
                self.assertTrue(all(item["manual_reviewed"] for item in confirmations))
                self.assertTrue(all(item["operation_source"] == "computer-local" for item in confirmations))
            finally:
                self.stop_service(port, process)

    def test_teacher_point_decisions_persist_with_point_level_completeness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                assignment = request_json(
                    port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "逐点评判保存测试",
                        "question": "测试题干",
                        "standard_answer": "测试参考答案",
                        "sub_questions": [{
                            "id": "Q1", "label": "（1）", "question": "测试小题",
                            "standard_answer": "两个要点",
                            "criteria": [
                                {"id": "C1", "label": "要点一", "score": 1},
                                {"id": "C2", "label": "要点二", "score": 1},
                            ],
                        }],
                    },
                )["assignment"]
                image = Image.new("RGB", (600, 400), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                submission = request_json(
                    port,
                    "/api/submissions",
                    "POST",
                    payload={
                        "assignment_id": assignment["id"],
                        "student_label": "匿名逐点评判样本",
                        "answer_text": "合成测试答案",
                        "idempotency_key": "teacher-point-review-persistence-test",
                        "photos": [{
                            "name": "synthetic.jpg",
                            "data_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
                        }],
                    },
                )["submission"]
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    current = request_json(port, f"/api/submission?submission_id={submission['id']}")["submission"]
                    if current["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)

                synthetic_result = {
                    "mode": "vision-model",
                    "blocked": False,
                    "suggested_total": 1,
                    "max_total": 2,
                    "sub_questions": [{
                        "sub_question_id": "Q1", "answer_status": "answered",
                        "suggested_total": 1, "max_total": 2,
                    }],
                    "dual_review": {
                        "results": {
                            "A": {"sub_questions": [{"sub_question_id": "Q1", "criteria": [
                                {"criterion_id": "C1", "decision": "full"},
                                {"criterion_id": "C2", "decision": "none"},
                            ]}]},
                            "B": {"sub_questions": [{"sub_question_id": "Q1", "criteria": [
                                {"criterion_id": "C1", "decision": "none"},
                                {"criterion_id": "C2", "decision": "full"},
                            ]}]},
                        },
                        "teacher_final": None,
                    },
                }
                self.update_temp_database(
                    root,
                    "UPDATE submissions SET state='completed', result_json=? WHERE id=?",
                    (json.dumps(synthetic_result, ensure_ascii=False), submission["id"]),
                )
                confirmed = request_json(
                    port,
                    "/api/confirm",
                    "POST",
                    payload={
                        "submission_id": submission["id"],
                        "final_total": 1,
                        "expanded_full_answer": True,
                        "teacher_point_decisions": [
                            {"question_id": "Q1", "rubric_point_id": "C1", "choice": "support_A"},
                            {"question_id": "Q1", "rubric_point_id": "C2", "choice": "both_wrong_custom_not_hit"},
                        ],
                    },
                )
                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["final_total"], 1)

                conn = sqlite3.connect(root / "data" / "history_grading.sqlite3")
                try:
                    saved = json.loads(conn.execute(
                        "SELECT result_json FROM submissions WHERE id=?", (submission["id"],)
                    ).fetchone()[0])
                finally:
                    conn.close()
                teacher_final = saved["dual_review"]["teacher_final"]
                self.assertEqual(teacher_final["final_total"], 1)
                self.assertTrue(teacher_final["expanded_full_answer"])
                self.assertEqual(teacher_final["teacher_review_completeness"], "point_level_complete")
                self.assertFalse(teacher_final["manual_score_override"])
                self.assertEqual(
                    [(item["rubric_point_id"], item["teacher_decision"], item["resolution"])
                     for item in teacher_final["point_decisions"]],
                    [("C1", "hit", "support_A"), ("C2", "not_hit", "both_wrong_custom")],
                )
                self.assertEqual(teacher_final["review_provenance"]["data_schema_version"], app.TEACHER_REVIEW_SCHEMA_VERSION)
            finally:
                self.stop_service(port, process)

    def test_scoped_teacher_final_uses_only_answer_region_questions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                sub_questions = [
                    {"id": "Q17.1", "label": "17.1", "question": "小题一", "standard_answer": "答案一",
                     "criteria": [{"id": f"C{index}", "label": f"要点{index}", "score": 2} for index in range(1, 4)]},
                    {"id": "Q17.2", "label": "17.2", "question": "小题二", "standard_answer": "答案二",
                     "criteria": [{"id": f"C{index}", "label": f"要点{index}", "score": 3} for index in range(1, 3)]},
                    *[
                        {"id": question_id, "label": question_id, "question": "范围外小题", "standard_answer": "答案",
                         "criteria": [{"id": "C1", "label": "范围外要点", "score": 6}]}
                        for question_id in ("Q17.3", "Q18.1", "Q18.2")
                    ],
                ]
                assignment = request_json(
                    port, "/api/assignments", "POST",
                    payload={"name": "答题区范围确认测试", "question": "整场考试", "sub_questions": sub_questions},
                )["assignment"]
                self.assertEqual(app.total_sub_questions(assignment["sub_questions"]), 30)
                image = Image.new("RGB", (600, 400), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                submission = request_json(
                    port, "/api/submissions", "POST",
                    payload={
                        "assignment_id": assignment["id"], "student_label": "范围测试样本",
                        "answer_text": "合成测试答案", "idempotency_key": "scoped-teacher-final-test",
                        "photos": [{"name": "synthetic.jpg", "data_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")}],
                    },
                )["submission"]
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    current = request_json(port, f"/api/submission?submission_id={submission['id']}")["submission"]
                    if current["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)

                def model_question(question_id, point_count):
                    return {
                        "sub_question_id": question_id, "answer_status": "answered",
                        "suggested_total": 0, "max_total": 6,
                        "criteria": [{"criterion_id": f"C{index}", "decision": "none", "suggested_score": 0}
                                     for index in range(1, point_count + 1)],
                    }

                scoped_result = {
                    "mode": "vision-model", "blocked": False, "suggested_total": 0, "max_total": 12,
                    "grading_scope": {"graded_question_ids": ["Q17.1", "Q17.2"], "scoped_max_score": 12, "source": "answer_regions"},
                    "teacher_reconfirmation_required": True,
                    "previous_teacher_final": {"final_total": 12, "max_total": 30, "confirmed_at": "old-confirmation"},
                    "sub_questions": [model_question("Q17.1", 3), model_question("Q17.2", 2)],
                    "dual_review": {
                        "base_side": "A", "teacher_final": None,
                        "grading_scope": {"graded_question_ids": ["Q17.1", "Q17.2"], "scoped_max_score": 12, "source": "answer_regions"},
                        "results": {
                            side: {"sub_questions": [model_question("Q17.1", 3), model_question("Q17.2", 2)]}
                            for side in ("A", "B")
                        },
                    },
                }
                self.update_temp_database(
                    root, "UPDATE submissions SET state='completed', final_total=12, confirmed_at='old-confirmation', result_json=? WHERE id=?",
                    (json.dumps(scoped_result, ensure_ascii=False), submission["id"]),
                )
                history = request_json(port, f"/api/assignment-submissions?assignment_id={assignment['id']}")["submissions"][0]
                self.assertTrue(history["teacher_reconfirmation_required"])
                self.assertEqual(history["max_total"], 12)
                self.assertEqual(history["previous_teacher_final"]["final_total"], 12)
                self.assertEqual(history["previous_teacher_final"]["max_total"], 30)
                point_decisions = [
                    {"question_id": question_id, "rubric_point_id": f"C{index}", "choice": "teacher_full"}
                    for question_id, point_count in (("Q17.1", 3), ("Q17.2", 2))
                    for index in range(1, point_count + 1)
                ]
                confirmed = request_json(
                    port, "/api/confirm", "POST",
                    payload={"submission_id": submission["id"], "teacher_point_decisions": point_decisions},
                )
                self.assertTrue(confirmed["ok"])
                self.assertEqual(confirmed["max_total"], 12)
                self.assertEqual(confirmed["final_total"], 12)
                saved_submission = request_json(port, f"/api/submission?submission_id={submission['id']}")["submission"]
                teacher_final = saved_submission["result"]["dual_review"]["teacher_final"]
                self.assertEqual(teacher_final["teacher_review_completeness"], "point_level_complete")
                self.assertEqual(teacher_final["max_total"], 12)
                self.assertEqual(teacher_final["grading_scope"]["graded_question_ids"], ["Q17.1", "Q17.2"])
                self.assertFalse(saved_submission["result"]["teacher_reconfirmation_required"])
                self.assertEqual(saved_submission["result"]["previous_teacher_final"]["final_total"], 12)
                self.assertEqual(saved_submission["result"]["previous_teacher_final"]["max_total"], 30)
                self.assertEqual(saved_submission["final_total"], 12)
                self.assertEqual(saved_submission["result"]["max_total"], 12)
                self.assertEqual(app.total_sub_questions(assignment["sub_questions"]), 30)
            finally:
                self.stop_service(port, process)

    def test_custom_model_preset_api_is_local_and_persistent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                created = request_json(
                    port,
                    "/api/custom-models/save",
                    "POST",
                    payload={
                        "label": "测试自定义视觉模型",
                        "base_url": "http://127.0.0.1:8000/v1",
                        "model": "history-vision-test",
                        "note": "仅测试非敏感配置保存",
                    },
                )
                self.assertTrue(created["ok"])
                preset_id = created["preset"]["id"]
                self.assertTrue(preset_id.startswith("custom-"))
                self.assertTrue((root / "config" / "custom-models.json").exists())
                stored = (root / "config" / "custom-models.json").read_text(encoding="utf-8")
                self.assertNotIn("api_key", stored.casefold())
                status = self.wait_for_status(port, process)
                self.assertTrue(any(item["id"] == preset_id for item in status["model_presets"]))
                listed = request_json(port, "/api/custom-models")
                self.assertEqual(listed["presets"][0]["id"], preset_id)
                deleted = request_json(
                    port,
                    "/api/custom-models/delete",
                    "POST",
                    payload={"preset_id": preset_id},
                )
                self.assertTrue(deleted["ok"])
                self.assertEqual(deleted["presets"], [])
            finally:
                self.stop_service(port, process)

    def test_backup_endpoints_report_storage_and_restore_safely(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                created = request_json(port, "/api/backup/create", "POST")
                self.assertTrue(created["ok"])
                self.assertTrue((root / "backups" / created["filename"]).exists())
                storage = request_json(port, "/api/storage")
                self.assertIn("disk_free_bytes", storage)
                backups = request_json(port, "/api/backups")
                self.assertEqual(backups["backups"][0]["filename"], created["filename"])
                with self.assertRaises(HTTPError) as error:
                    request_json(port, "/api/backup/restore", "POST")
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                restored = request_json(
                    port,
                    "/api/backup/restore",
                    "POST",
                    payload={"filename": created["filename"]},
                )
                self.assertTrue(restored["ok"])
                self.assertTrue((root / "backups" / restored["safety_backup"]).exists())
            finally:
                self.stop_service(port, process)

    def test_recovery_submission_list_is_local_and_survives_restart_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                assignment = request_json(
                    port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "恢复列表测试题",
                        "question": "根据材料回答问题",
                        "sub_questions": [
                            {
                                "id": "Q1",
                                "label": "（1）",
                                "question": "概括特点",
                                "standard_answer": "直属皇帝",
                                "criteria": [{"label": "直属皇帝", "score": 2, "keywords": ["直属"]}],
                            }
                        ],
                    },
                )
                created = request_json(
                    port,
                    "/api/submissions",
                    "POST",
                    payload={
                        "assignment_id": assignment["assignment"]["id"],
                        "student_label": "恢复测试学生",
                        "answer_text": "测试答案",
                        "idempotency_key": "recovery-list-test-1",
                        "photos": [],
                    },
                )
                submission_id = created["submission"]["id"]
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    current = request_json(port, f"/api/submission?submission_id={submission_id}")["submission"]
                    if current["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)
                conn = sqlite3.connect(root / "data" / "history_grading.sqlite3")
                conn.execute(
                    "UPDATE submissions SET state = 'recovery_pending', result_json = ?, uncertain_model_call = 0 WHERE id = ?",
                    (json.dumps({"mode": "queued", "blocked": True, "needs_review": True, "summary": "服务中断，待人工确认"}, ensure_ascii=False), submission_id),
                )
                conn.execute(
                    "UPDATE task_queue SET state = 'recovery_pending', updated_at = updated_at WHERE submission_id = ?",
                    (submission_id,),
                )
                conn.commit()
                conn.close()
                listed = request_json(port, "/api/recovery-submissions")
                self.assertEqual(listed["submissions"][0]["id"], submission_id)
                self.assertEqual(listed["submissions"][0]["assignment_name"], "恢复列表测试题")
            finally:
                self.stop_service(port, process)

    def test_submission_queue_completes_and_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                assignment = request_json(
                    port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "队列测试题",
                        "question": "根据材料回答问题",
                        "sub_questions": [
                            {
                                "id": "Q1",
                                "label": "（1）",
                                "question": "概括特点",
                                "standard_answer": "直属皇帝",
                                "criteria": [{"label": "直属皇帝", "score": 2, "keywords": ["直属"]}],
                            }
                        ],
                    },
                )
                image = Image.new("RGB", (1000, 800), "white")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG")
                data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                payload = {
                    "assignment_id": assignment["assignment"]["id"],
                    "student_label": "队列测试学生",
                    "answer_text": "直属",
                    "idempotency_key": "queue-test-idempotency-1",
                    "photos": [{"name": "test.jpg", "data_url": data_url}],
                }
                created = request_json(port, "/api/submissions", "POST", payload=payload)
                submission_id = created["submission"]["id"]
                deadline = time.monotonic() + 8
                completed = None
                while time.monotonic() < deadline:
                    completed = request_json(
                        port,
                        f"/api/submission?submission_id={submission_id}",
                    )["submission"]
                    if completed["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.1)
                self.assertEqual(completed["state"], "completed")
                self.assertEqual(completed["result"]["mode"], "rules-demo")
                self.assertTrue(completed["result"]["blocked"])

                replay = request_json(port, "/api/submissions", "POST", payload=payload)
                self.assertTrue(replay["idempotent_replay"])
                self.assertEqual(replay["submission"]["id"], submission_id)
                with self.assertRaises(HTTPError) as error:
                    request_json(port, "/api/confirm", "POST", payload={"submission_id": submission_id, "final_total": 0})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            finally:
                self.stop_service(port, process)

    def test_delete_assignment_removes_persisted_queue_children(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            port = free_port()
            process = self.start_service(root, port)
            try:
                self.wait_for_status(port, process)
                assignment = request_json(
                    port,
                    "/api/assignments",
                    "POST",
                    payload={
                        "name": "删除队列测试题",
                        "question": "根据材料回答问题",
                        "sub_questions": [{
                            "id": "Q1",
                            "label": "（1）",
                            "question": "概括特点",
                            "criteria": [{"label": "特点", "score": 2, "keywords": ["特点"]}],
                        }],
                    },
                )
                assignment_id = assignment["assignment"]["id"]
                created = request_json(
                    port,
                    "/api/submissions",
                    "POST",
                    payload={
                        "assignment_id": assignment_id,
                        "student_label": "删除测试学生",
                        "answer_text": "特点",
                        "idempotency_key": "delete-assignment-test",
                        "photos": [],
                    },
                )
                submission_id = created["submission"]["id"]
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    current = request_json(port, f"/api/submission?submission_id={submission_id}")["submission"]
                    if current["state"] == "completed":
                        break
                    time.sleep(0.1)
                deleted = request_json(
                    port,
                    "/api/delete-assignment",
                    "POST",
                    payload={"assignment_id": assignment_id},
                )
                self.assertTrue(deleted["ok"])
                conn = sqlite3.connect(root / "data" / "history_grading.sqlite3")
                self.assertIsNone(conn.execute("SELECT 1 FROM submissions WHERE id = ?", (submission_id,)).fetchone())
                self.assertIsNone(conn.execute("SELECT 1 FROM task_queue WHERE submission_id = ?", (submission_id,)).fetchone())
                conn.close()
            finally:
                self.stop_service(port, process)


if __name__ == "__main__":
    unittest.main()
