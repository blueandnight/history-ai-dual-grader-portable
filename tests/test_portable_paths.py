import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class PortablePathTests(unittest.TestCase):
    def setUp(self):
        self.previous_paths = app.APP_PATHS

    def tearDown(self):
        app._install_paths(self.previous_paths)

    def test_stored_data_paths_accept_both_separator_styles_and_mixed_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            app.configure_paths(temp_root / "portable", temp_root / "program")
            assignment_id = "a" * 12
            submission_id = "b" * 12
            student_dir = app.DATA_DIR / assignment_id / "students" / submission_id
            student_dir.mkdir(parents=True)
            processed = student_dir / "page_01.jpg"
            original = student_dir / "page_01.original"
            processed.write_bytes(b"synthetic processed image")
            original.write_bytes(b"synthetic original image")

            historical_posix = processed.relative_to(app.PORTABLE_ROOT).as_posix()
            historical_windows = original.relative_to(app.PORTABLE_ROOT).as_posix().replace("/", "\\")
            mixed = historical_posix.replace("/", "\\", 2)

            self.assertEqual(app.resolve_data_relative_path(historical_posix), processed.resolve())
            self.assertEqual(app.resolve_data_relative_path(historical_windows), original.resolve())
            self.assertEqual(app.resolve_data_relative_path(mixed), processed.resolve())
            self.assertEqual(app._submission_photo_paths([{"path": historical_posix}]), [processed.resolve()])
            self.assertEqual(
                app.submission_photo_directory(
                    assignment_id,
                    submission_id,
                    [{"path": historical_posix, "original_path": historical_windows}],
                ),
                student_dir.resolve(),
            )

    def test_stored_data_paths_reject_traversal_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            app.configure_paths(temp_root / "portable", temp_root / "program")
            unsafe_paths = (
                "data/../outside.jpg",
                r"data\..\outside.jpg",
                r"data/..\outside.jpg",
                "/tmp/outside.jpg",
                r"C:\data\outside.jpg",
                r"\\server\share\outside.jpg",
            )
            for unsafe_path in unsafe_paths:
                with self.subTest(path=unsafe_path), self.assertRaises(ValueError):
                    app.resolve_data_relative_path(unsafe_path)

    def test_frontend_idempotency_key_supports_mobile_browser_without_random_uuid(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('function createIdempotencyKey()', source)
        self.assertIn('getRandomValues', source)
        self.assertIn('fallbackIdempotencyCounter', source)
        self.assertNotIn('idempotency_key: crypto.randomUUID()', source)

    def test_frontend_result_card_distinguishes_review_from_failure(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "static" / "app.js").read_text(encoding="utf-8")
        html_source = (root / "static" / "index.html").read_text(encoding="utf-8")
        css_source = (root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("result-status-review", app_source)
        self.assertIn("AI建议分（尚未核对）", app_source)
        self.assertIn("请按 3 步完成", app_source)
        self.assertIn("已核对原图，解锁确认", app_source)
        self.assertIn("查看模型说明", app_source)
        self.assertIn("查看技术状态", app_source)
        self.assertIn("result-status-failed", css_source)
        self.assertIn(".action-guide", css_source)
        self.assertIn("result-review-steps", app_source)
        self.assertIn("result-confirmation", app_source)
        self.assertIn("prepare-next-result", app_source)
        self.assertIn("next-ready-state", app_source + css_source)
        self.assertIn('id="continuous-count"', html_source)
        self.assertNotIn("当前结果需要教师打开原图核对证据", app_source)

    def test_frontend_has_continuous_camera_queue_with_http_fallback(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "static" / "app.js").read_text(encoding="utf-8")
        html_source = (root / "static" / "index.html").read_text(encoding="utf-8")
        css_source = (root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("const MAX_CAMERA_PHOTOS = 20", app_source)
        self.assertIn("navigator.mediaDevices.getUserMedia", app_source)
        self.assertIn("系统相机逐张添加", app_source + html_source)
        self.assertIn('id="camera-fallback-input"', html_source)
        self.assertIn('id="camera-album-input" type="file" accept="image/*" multiple', html_source)
        self.assertIn('id="photos-album" type="file" accept="image/*" multiple', html_source)
        self.assertIn('id="batch-photos-album" type="file" accept="image/*" multiple', html_source)
        self.assertNotIn('id="photos-album" type="file" accept="image/*" capture=', html_source)
        self.assertNotIn('id="batch-photos-album" type="file" accept="image/*" capture=', html_source)
        self.assertIn("filesFromInputs(SINGLE_PHOTO_INPUTS)", app_source)
        self.assertIn("filesFromInputs(BATCH_PHOTO_INPUTS)", app_source)
        self.assertIn('id="camera-finish"', html_source)
        self.assertIn(".camera-capture-box", css_source)
        self.assertIn(".camera-session[hidden] { display: none; }", css_source)
        self.assertIn("renderBatchPreview(groups)", app_source)

    def test_batch_uploads_are_bounded_but_grading_results_are_consumed_in_order(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        server = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn("const BATCH_CONCURRENCY = 2", source)
        self.assertIn("runUploadWorker", source)
        self.assertIn("displayBatchResultsFrom(1", source)
        self.assertIn('int(row["batch_sequence"] or 0)', server)
        self.assertNotIn("runBatchWorker", source)
        self.assertIn("本份失败类型不建议重复调用", source)
        self.assertIn("recovery-no-retry", (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text(encoding="utf-8"))

    def test_batch_preview_reorders_manifest_and_server_controls_grading_sequence(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn("move-batch-group", source)
        self.assertIn("move-batch-page", source)
        self.assertIn("batch_request_id", source)
        self.assertIn("page_sequence", source)
        self.assertIn("/api/batches", source)
        self.assertIn("insertBatchRowInSequence", source)
        self.assertIn("_batch_sequence_is_runnable", server)
        self.assertIn("recover_interrupted_grade_tasks", server)
        self.assertIn("grading_batches", server)
        self.assertIn("const MAX_CAMERA_PHOTOS = 20", source)

    def test_frontend_has_four_role_based_workspaces_and_keyboard_navigation(self):
        root = Path(__file__).resolve().parents[1]
        html_source = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_source = (root / "static" / "app.js").read_text(encoding="utf-8")
        css_source = (root / "static" / "styles.css").read_text(encoding="utf-8")
        for name in ("grading", "rubric", "history", "management"):
            self.assertIn(f'data-app-view="{name}"', html_source)
            self.assertIn(f'data-app-view-target="{name}"', html_source)
        self.assertIn('role="tablist" aria-label="主导航"', html_source)
        self.assertIn('aria-label="批改准备状态"', html_source)
        self.assertIn('class="grading-mode-banner"', html_source)
        self.assertIn("setupAppNavigation()", app_source)
        self.assertIn("ArrowRight", app_source)
        self.assertIn("Home", app_source)
        self.assertIn("requestAnimationFrame(() => target.scrollIntoView", app_source)
        self.assertIn(".workflow-overview", css_source)
        self.assertIn(".main-nav-tab.active", css_source)
        self.assertIn(".app-view[hidden]", css_source)

    def test_frontend_layout_prioritizes_grading_and_mobile_workflows(self):
        root = Path(__file__).resolve().parents[1]
        html_source = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_source = (root / "static" / "app.js").read_text(encoding="utf-8")
        css_source = (root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("primary-workspace", html_source)
        self.assertIn('class="grading-mode-switcher"', html_source)
        self.assertIn('data-grading-mode="single"', html_source)
        self.assertIn('data-grading-mode="continuous"', html_source)
        self.assertIn('data-grading-mode="batch"', html_source)
        self.assertIn('id="mobile-access-panel"', html_source)
        self.assertIn('class="connection-steps"', html_source)
        self.assertIn('class="mobile-action-bar"', html_source)
        self.assertIn("setupGradingModes()", app_source)
        self.assertIn(".readiness-card", css_source)
        self.assertIn(".connection-console", css_source)
        self.assertIn(".mobile-action-bar", css_source)

    def test_frontend_has_local_evaluation_and_audit_panel(self):
        root = Path(__file__).resolve().parents[1]
        html_source = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_source = (root / "static" / "app.js").read_text(encoding="utf-8")
        css_source = (root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="evaluation-section"', html_source)
        self.assertIn('id="evaluation-summary"', html_source)
        self.assertIn("evaluation/manifest.json", html_source)
        self.assertIn("/api/evaluation/summary", app_source)
        self.assertIn("renderAuditSummary", app_source)
        self.assertIn(".evaluation-metrics", css_source)
        self.assertIn(".audit-valid", css_source)

    def test_point_bank_frontend_explains_capped_credit_and_incomplete_candidate_pool(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("满分封顶（实际计入", app_source)
        self.assertIn("候选不足：目前", app_source)
        self.assertIn("generatedForDraft(draft", app_source)
        self.assertIn("const byId = Object.fromEntries", app_source)
        self.assertIn("generated?.id !== draft.id", app_source)

    def test_frontend_has_usb_network_access_guidance_and_refresh(self):
        root = Path(__file__).resolve().parents[1]
        html_source = (root / "static" / "index.html").read_text(encoding="utf-8")
        app_source = (root / "static" / "app.js").read_text(encoding="utf-8")
        css_source = (root / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="refresh-mobile-network"', html_source)
        self.assertIn("USB 网络共享", html_source)
        self.assertIn("network_interfaces", app_source)
        self.assertIn("refreshMobileNetwork", app_source)
        self.assertIn("mobile-network-recommended", css_source)

    def test_grade_worker_count_is_bounded_and_configurable(self):
        with patch.dict(os.environ, {"HISTORY_GRADER_WORKERS": "3"}):
            self.assertEqual(app.configured_grade_worker_count(), 3)
        with patch.dict(os.environ, {"HISTORY_GRADER_WORKERS": "99"}):
            self.assertEqual(app.configured_grade_worker_count(), 4)
        with patch.dict(os.environ, {"HISTORY_GRADER_WORKERS": "0"}):
            self.assertEqual(app.configured_grade_worker_count(), 1)
        with patch.dict(os.environ, {"HISTORY_GRADER_WORKERS": "not-a-number"}):
            self.assertEqual(app.configured_grade_worker_count(), 1)

    def test_resolve_paths_separates_program_and_portable_root(self):
        with tempfile.TemporaryDirectory() as temp:
            portable_root = Path(temp) / "USB-root"
            program_dir = Path(temp) / "program"
            paths = app.resolve_paths(portable_root, program_dir)

            self.assertEqual(paths.program_dir, program_dir.resolve())
            self.assertEqual(paths.portable_root, portable_root.resolve())
            self.assertEqual(paths.static_dir, program_dir.resolve() / "static")
            self.assertEqual(paths.data_dir, portable_root.resolve() / "data")
            self.assertEqual(paths.config_dir, portable_root.resolve() / "config")
            self.assertEqual(
                paths.db_path,
                portable_root.resolve() / "data" / "history_grading.sqlite3",
            )
            self.assertNotEqual(paths.program_dir, paths.portable_root)

    def test_frozen_exe_defaults_portable_root_to_exe_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            resources = temp_dir / "package" / "_internal"
            executable = temp_dir / "package" / "历史学科AI双评阅卷系统.exe"
            with patch.object(app.sys, "frozen", True, create=True), patch.object(
                app.sys, "_MEIPASS", str(resources), create=True
            ), patch.object(app.sys, "executable", str(executable)), patch.dict(
                os.environ, {"HISTORY_GRADER_PORTABLE_ROOT": ""}, clear=False
            ):
                paths = app.resolve_paths()

            self.assertEqual(paths.program_dir, resources.resolve())
            self.assertEqual(paths.portable_root, executable.parent.resolve())
            self.assertEqual(paths.data_dir, executable.parent.resolve() / "data")
            self.assertEqual(paths.static_dir, resources.resolve() / "static")

    def test_portable_root_can_move_without_changing_data_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            first_root = Path(temp) / "USB-drive-A"
            second_root = Path(temp) / "USB-drive-B"
            program_dir = Path(temp) / "program"
            first_paths = app.resolve_paths(first_root, program_dir)
            first_paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            first_paths.db_path.write_bytes(b"portable-test-database")

            app.configure_paths(first_root, program_dir)
            first_instance_id = app.ensure_portable_instance_id()
            shutil.move(str(first_root), str(second_root))

            second_paths = app.resolve_paths(second_root, program_dir)
            app.configure_paths(second_root, program_dir)
            second_instance_id = app.ensure_portable_instance_id()

            self.assertTrue(second_paths.db_path.exists())
            self.assertEqual(second_paths.db_path.read_bytes(), b"portable-test-database")
            self.assertEqual(first_instance_id, second_instance_id)
            self.assertTrue(second_paths.instance_id_path.exists())

    def test_normal_config_is_written_under_config_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            portable_root = Path(temp) / "USB-root"
            program_dir = Path(temp) / "program"
            app.configure_paths(portable_root, program_dir)

            app.write_env_values(
                {
                    "HISTORY_GRADER_PROVIDER": "test-provider",
                    "HISTORY_GRADER_BASE_URL": "http://127.0.0.1:8000/v1",
                    "HISTORY_GRADER_MODEL": "test-vision-model",
                }
            )

            self.assertTrue(app.ENV_PATH.exists())
            self.assertFalse((program_dir / ".env").exists())
            values = app.read_env_file()
            self.assertEqual(values["HISTORY_GRADER_PROVIDER"], "test-provider")
            self.assertEqual(values["HISTORY_GRADER_MODEL"], "test-vision-model")

    def test_legacy_config_is_read_without_being_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            portable_root = Path(temp) / "USB-root"
            program_dir = Path(temp) / "program"
            program_dir.mkdir()
            legacy_env = program_dir / ".env"
            legacy_env.write_text(
                "HISTORY_GRADER_PROVIDER=legacy-provider\n"
                "HISTORY_GRADER_MODEL=legacy-model\n",
                encoding="utf-8",
            )
            app.configure_paths(portable_root, program_dir)

            self.assertEqual(app.read_env_file()["HISTORY_GRADER_MODEL"], "legacy-model")
            app.write_env_values({"HISTORY_GRADER_MODEL": "new-model"})

            self.assertEqual(
                legacy_env.read_text(encoding="utf-8"),
                "HISTORY_GRADER_PROVIDER=legacy-provider\n"
                "HISTORY_GRADER_MODEL=legacy-model\n",
            )
            self.assertEqual(app.read_env_file()["HISTORY_GRADER_MODEL"], "new-model")

    def test_config_reader_ignores_api_key_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            config_file = Path(temp) / "config.env"
            config_file.write_text(
                "HISTORY_GRADER_PROVIDER=test-provider\n"
                "HISTORY_GRADER_API_KEY=must-not-be-loaded\n"
                "DEEPSEEK_API_KEY=must-not-be-loaded\n",
                encoding="utf-8",
            )

            values = app.read_env_file(config_file)

            self.assertEqual(values, {"HISTORY_GRADER_PROVIDER": "test-provider"})

    def test_api_key_path_is_local_and_per_portable_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            portable_root = temp_dir / "USB-root"
            program_dir = temp_dir / "program"
            local_app_data = temp_dir / "local-app-data"
            app.configure_paths(portable_root, program_dir)

            with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                instance_id = app.ensure_portable_instance_id()
                path = app.api_key_path("Qwen/3.5")

            expected = (
                local_app_data.resolve()
                / "HistoryGrader"
                / "keys"
                / instance_id
                / "api-key-qwen35.dpapi"
            )
            self.assertEqual(path, expected)
            self.assertFalse(path.is_relative_to(portable_root.resolve()))
            self.assertFalse((portable_root / "api-key-qwen35.dpapi").exists())

            with patch.dict(os.environ, {"LOCALAPPDATA": str(temp_dir / "another-computer")}, clear=False):
                other_computer_path = app.api_key_path("Qwen/3.5")
            self.assertNotEqual(path, other_computer_path)

    def test_restart_environment_does_not_carry_plaintext_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            app.configure_paths(Path(temp) / "USB-root", Path(temp) / "program")
            with patch.dict(
                os.environ,
                {
                    "HISTORY_GRADER_API_KEY": "session-only-test-key",
                    "DEEPSEEK_API_KEY": "legacy-test-key",
                },
                clear=False,
            ):
                environment = app.build_restart_environment()

            self.assertNotIn("HISTORY_GRADER_API_KEY", environment)
            self.assertNotIn("DEEPSEEK_API_KEY", environment)

    def test_legacy_deepseek_key_is_not_reused_for_other_provider(self):
        with patch.dict(
            os.environ,
            {
                "HISTORY_GRADER_PROVIDER": "qwen",
                "HISTORY_GRADER_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "DEEPSEEK_API_KEY": "dummy-deepseek-test-key",
            },
            clear=True,
        ):
            app.load_env_file()
            self.assertNotIn("HISTORY_GRADER_API_KEY", os.environ)

    def test_legacy_deepseek_model_ids_migrate_to_official_model_in_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            app.configure_paths(temp_dir / "USB-root", temp_dir / "program")
            app.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
            app.ENV_PATH.write_text(
                "HISTORY_GRADER_PROVIDER=deepseek\n"
                "HISTORY_GRADER_BASE_URL=https://api.deepseek.com\n"
                "HISTORY_GRADER_MODEL=deepseek-v4.1-flash-expires-on-0910\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                app.load_env_file()
                self.assertEqual(os.environ["HISTORY_GRADER_MODEL"], "deepseek-flash")
            self.assertIn("deepseek-v4.1-flash-expires-on-0910", app.ENV_PATH.read_text(encoding="utf-8"))

    def test_corrupt_local_key_has_clear_error(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            app.configure_paths(temp_dir / "USB-root", temp_dir / "program")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(temp_dir / "local-app-data")}):
                path = app.api_key_path("deepseek")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("not-a-dpapi-document", encoding="ascii")
                with patch.object(app, "powershell_command", return_value=""):
                    with self.assertRaisesRegex(app.ApiKeyStoreError, "PowerShell"):
                        app.read_dpapi_api_key("deepseek")

    @unittest.skipUnless(os.name == "nt" and app.powershell_command(), "需要 Windows PowerShell DPAPI")
    def test_dpapi_round_trip_stays_in_local_app_data(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            portable_root = temp_dir / "USB-root"
            program_dir = temp_dir / "program"
            local_app_data = temp_dir / "local-app-data"
            app.configure_paths(portable_root, program_dir)
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                app.save_dpapi_api_key("unit-test-key", "deepseek")
                path = app.api_key_path("deepseek")
                restored = app.read_dpapi_api_key("deepseek")

            self.assertTrue(path.exists())
            self.assertEqual(restored, "unit-test-key")
            self.assertFalse((portable_root / "api-key-deepseek.dpapi").exists())


if __name__ == "__main__":
    unittest.main()
