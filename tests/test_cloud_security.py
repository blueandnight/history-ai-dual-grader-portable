import base64
import csv
import hashlib
import hmac
import io
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image, ImageDraw

import app
import web_auth
import password_auth
try:
    from .schema_fixtures import rebuild_legacy_submissions_table
except ImportError:
    from schema_fixtures import rebuild_legacy_submissions_table


class CloudSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_paths = app.APP_PATHS
        app.configure_paths(Path(self.temp.name) / "portable", Path(self.temp.name) / "program")
        self.old_auth_manager = web_auth.AUTH_MANAGER
        self.old_mobile_token = (app.MOBILE_TOKEN_HASH, app.MOBILE_TOKEN_EXPIRES_AT)
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        self.env_patch = patch.dict(os.environ, {
            "HISTORY_GRADER_CLOUD_MODE": "1",
            "HISTORY_GRADER_WEB_AUTH_REQUIRED": "false",
            "HISTORY_GRADER_ADMIN_USERNAME": "teacher",
            "HISTORY_GRADER_ADMIN_PASSWORD_HASH": "",
            "HISTORY_GRADER_COOKIE_SECURE": "false",
            "HISTORY_GRADER_TOTP_REQUIRED": "false",
            "HISTORY_GRADER_TOTP_SECRET": "",
            "HISTORY_GRADER_EMAIL_OTP_REQUIRED": "false",
            "HISTORY_GRADER_ADMIN_EMAIL": "",
            "HISTORY_GRADER_SMTP_HOST": "",
            "HISTORY_GRADER_SMTP_PORT": "465",
            "HISTORY_GRADER_SMTP_USERNAME": "",
            "HISTORY_GRADER_SMTP_PASSWORD": "",
            "HISTORY_GRADER_SMTP_FROM": "",
            "HISTORY_GRADER_API_KEY": "SYNTHETIC_TEST_KEY_ONLY",
            "HISTORY_GRADER_BASE_URL": "https://example.invalid/v1",
            "HISTORY_GRADER_MODEL": "synthetic-test-model",
            "HISTORY_GRADER_PROVIDER": "synthetic-test-provider",
            "HISTORY_GRADER_WORKERS": "1",
            "HISTORY_GRADER_RETENTION_DAYS": "0",
        })
        self.env_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.origin = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.env_patch.stop()
        web_auth.AUTH_MANAGER = self.old_auth_manager
        app.MOBILE_TOKEN_HASH, app.MOBILE_TOKEN_EXPIRES_AT = self.old_mobile_token
        app._install_paths(self.previous_paths)
        self.temp.cleanup()

    def request_json(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(self.base + path, data=payload, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}, response.headers
        except HTTPError as response:
            raw = response.read()
            code, headers = response.code, response.headers
            response.close()
            return code, json.loads(raw) if raw else {}, headers

    def configure_password(self):
        encoded = web_auth.hash_password("synthetic test password 123", iterations=100_000)
        os.environ["HISTORY_GRADER_ADMIN_PASSWORD_HASH"] = encoded
        return "synthetic test password 123"

    def login(self, password):
        status, data, headers = self.request_json(
            "POST",
            "/api/auth/login",
            {"username": "teacher", "password": password},
            {"Origin": self.origin},
        )
        cookie_header = headers.get("Set-Cookie", "")
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(web_auth.COOKIE_NAME)
        value = f"{web_auth.COOKIE_NAME}={morsel.value}" if morsel else ""
        return status, data, cookie_header, value

    def auth_headers(self, cookie, csrf=""):
        headers = {"Origin": self.origin, "Cookie": cookie}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return headers

    def test_http_logs_never_include_request_target_or_payload(self):
        handler = object.__new__(app.AppHandler)
        with patch("builtins.print") as emit:
            handler.log_message(
                '"POST /api/login?token=COOKIE_SENTINEL HTTP/1.1" 200 123',
                "POST /api/login?token=COOKIE_SENTINEL HTTP/1.1",
                200,
                123,
            )
        logged = " ".join(str(value) for value in emit.call_args.args)
        self.assertNotIn("COOKIE_SENTINEL", logged)
        self.assertNotIn("/api/login", logged)
        self.assertIn("status=200", logged)

    @staticmethod
    def image_data_url(size=(1000, 800), marker=False):
        image = Image.new("RGB", size, "white")
        if marker:
            draw = ImageDraw.Draw(image)
            draw.rectangle((100, 80, 300, 240), fill="black")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def create_assignment(self, question="合成题目：分析制度变化"):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1",
            "label": "（1）",
            "question": "说明制度变化",
            "standard_answer": "合成参考答案",
            "scoring_mode": "weighted",
            "criteria": [{"label": "说明变化", "score": 2, "keywords": ["制度"]}],
        }])
        assignment_id = "a" * 12
        conn = app.get_db()
        conn.execute(
            "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                assignment_id,
                "合成测试作业",
                question,
                "合成参考答案",
                json.dumps(app.flatten_criteria(sub_questions), ensure_ascii=False),
                json.dumps(sub_questions, ensure_ascii=False),
                app.utc_now(),
            ),
        )
        conn.commit()
        assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        conn.close()
        return assignment_id, sub_questions, assignment

    def _copy_accepted_answer_crop_fixture(self):
        import shutil

        run_id = "b0d4fec115104082ab16343846544d1e"
        source = Path(__file__).resolve().parents[1] / "reports" / "artifacts" / "student_auto_crop_poc_2026-10-04" / run_id
        if not source.is_dir():
            self.skipTest("已教师验收的 Beta crop fixture 不在当前 checkout")
        target = Path(app.PROGRAM_DIR) / "reports" / "artifacts" / "student_auto_crop_poc_2026-10-04" / run_id
        (target / "padding_validation").mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "run.json", target / "run.json")
        for name in ("17_1_crop_padded.png", "17_2_crop_padded.png"):
            shutil.copy2(source / "padding_validation" / name, target / "padding_validation" / name)
        return run_id, json.loads((target / "run.json").read_text(encoding="utf-8")), target

    def _seed_answer_region_beta_exam(self, run):
        from exam_layout_profile_service import build_exam_layout_profile

        assignment_id = run["assignment_id"]
        exam_id = run["exam_id"]
        references = [
            {
                "page_number": int(page["page_number"]),
                "reference_width": int(page["reference_width"]),
                "reference_height": int(page["reference_height"]),
                "reference_sha256": str(page["reference_sha256"]),
                "render_dpi": 150,
                "render_method": "pdftoppm_150dpi",
                "image_file": str(page["relative_path"]),
                "relative_path": str(page["relative_path"]),
            }
            for page in run["reference_assets"]
        ]
        sub_questions = app.clean_sub_questions([
            {
                "id": "Q17.1", "label": "17.1", "question": "结合材料一、材料二说明制度变化。",
                "standard_answer": "秦设丞相；唐三省六部；明废丞相设内阁。", "scoring_mode": "weighted",
                "max_score": 6, "needs_teacher_confirmation": False,
                "criteria": [
                    {"id": "C1", "label": "秦设丞相", "score": 2},
                    {"id": "C2", "label": "唐三省六部", "score": 2},
                    {"id": "C3", "label": "明废丞相", "score": 2},
                ],
            },
            {
                "id": "Q17.2", "label": "17.2", "question": "概括雅典民主政治的基本特征。",
                "standard_answer": "人民主权、轮番而治。", "scoring_mode": "weighted",
                "max_score": 6, "needs_teacher_confirmation": False,
                "criteria": [
                    {"id": "C1", "label": "人民主权", "score": 3},
                    {"id": "C2", "label": "轮番而治", "score": 3},
                ],
            },
        ])
        layout_source = {
            "type": "pdf", "source_pdf_file_name": "fixture.pdf",
            "source_pdf_sha256": run["source_pdf_sha256"], "page_count": 9,
            "exam_pages": [1, 2, 3, 4], "answer_pages": [5, 6, 7, 8, 9],
        }
        layout_metadata = {
            "teacher_confirmation_status": "confirmed_by_teacher",
            "teacher_confirmed_at": "2026-10-04T00:00:00Z",
            "source": {"layout_source": layout_source},
            "layout_reference": {
                "pages": references,
                "page_roles": [{"page_number": n, "page_role": "exam"} for n in (1, 2, 3, 4)],
            },
        }
        regions = [
            {
                "question_id": str(crop["question_id"]), "page_number": int(crop["page_number"]),
                "bbox": crop["configured_bbox"],
            }
            for crop in run["crops"]
        ]
        profile = build_exam_layout_profile(
            exam_id=exam_id,
            assignment_id=assignment_id,
            source_pdf_sha256=run["source_pdf_sha256"],
            references=references,
            required_question_ids=["17.1", "17.2"],
            regions=regions,
            layout_profile_id=run["layout_profile_id"],
            created_at="2026-10-04T00:00:00Z",
            updated_at="2026-10-04T00:00:00Z",
        )
        conn = app.get_db()
        conn.execute(
            "INSERT INTO assignments (id,name,question,standard_answer,criteria_json,sub_questions_json,created_at,import_metadata_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                assignment_id, "真实 Beta crop 隔离验收", "17.1 与 17.2", "参考答案",
                json.dumps(app.flatten_criteria(sub_questions), ensure_ascii=False),
                json.dumps(sub_questions, ensure_ascii=False), "2026-10-04T00:00:00Z",
                json.dumps(layout_metadata, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO grading_exams (id,assignment_id,created_at,layout_profile_id,layout_profile_json) VALUES (?,?,?,?,?)",
            (exam_id, assignment_id, "2026-10-04T00:00:00Z", profile["layout_profile_id"], json.dumps(profile, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
        return assignment_id, exam_id, sub_questions, layout_metadata

    def test_teacher_accepted_beta_crops_reach_normal_submission_model_boundary(self):
        dual_specs = {
            "A": {"provider": "deepseek", "provider_label": "DeepSeek", "model": "synthetic-deepseek", "api_origin": "https://api.deepseek.com", "base_url": "https://api.deepseek.com/v1", "preset_id": "deepseek-v41-flash", "label": "DeepSeek"},
            "B": {"provider": "mimo", "provider_label": "MiMo", "model": "mimo-v2.6-flash", "api_origin": "https://api.xiaomimimo.com", "base_url": "https://api.xiaomimimo.com/v1", "preset_id": "mimo-v26-flash", "label": "MiMo-V2.6-Flash"},
        }
        model_configs = {
            key: {**value, "api_key": f"SYNTHETIC_TEST_KEY_{key}_ONLY", "timeout_seconds": "1"}
            for key, value in dual_specs.items()
        }
        # This fixture owns both model configs for every request, including
        # preview and the unchecked privacy negative. No external keys are read.
        self.enterContext(patch.object(app, "dual_review_model_specs", return_value=dual_specs))
        self.enterContext(patch.object(app, "dual_review_key_is_configured",
                                      side_effect=lambda side: bool(model_configs[side]["api_key"])))
        self.enterContext(patch.object(app, "load_dual_review_model_configs", return_value=model_configs))
        unexpected_provider = self.enterContext(patch.object(
            app, "open_model_request", side_effect=AssertionError("Unexpected unmocked provider boundary")
        ))
        run_id, run, _run_dir = self._copy_accepted_answer_crop_fixture()
        assignment_id, exam_id, sub_questions, confirmed_metadata = self._seed_answer_region_beta_exam(run)
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        headers = self.auth_headers(cookie, session["csrf_token"])

        credentials_status, credentials, _ = self.request_json(
            "POST", "/api/student-auto-crop-answer-regions", {"alignment_run_id": run_id}, headers
        )
        self.assertEqual(credentials_status, 200, credentials)
        crops = credentials["answer_regions"]
        self.assertEqual([item["question_label"] for item in crops], ["17.1", "17.2"])
        self.assertTrue(all(item["image_role"] == "answer_region" and item["attestation"] for item in crops))
        regions = [
            {
                "region_id": f"accepted:{item['question_id']}", "profile_id": run["layout_profile_id"],
                "photo_index": index, "x": 0, "y": 0, "width": 1, "height": 1,
                "sub_question_ids": [item["question_id"]],
                "coordinate_system": "perspective_corrected_page_normalized",
                "rotation_ccw": 0, "invocation_scope": "answer_region",
            }
            for index, item in enumerate(crops, start=1)
        ]
        photos = [
            {
                "name": f"crop-{index}.png", "page_sequence": index,
                "data_url": item["data_url"], "original_data_url": item["data_url"],
                "image_role": "answer_region", "answer_region_attestation": item["attestation"],
                "ai_redactions": [],
            }
            for index, item in enumerate(crops, start=1)
        ]
        preview_body = {
            "assignment_id": assignment_id, "student_label": "BETA_FIXTURE",
            "photos": photos, "answer_regions": regions, "dual_review_enabled": True,
        }

        conn = app.get_db()
        row = conn.execute("SELECT import_metadata_json FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        pending_metadata = json.loads(row[0])
        pending_metadata["teacher_confirmation_status"] = "pending_teacher_confirmation"
        conn.execute("UPDATE assignments SET import_metadata_json=? WHERE id=?", (json.dumps(pending_metadata), assignment_id))
        conn.commit()
        conn.close()
        blocked_preview, blocked_body, _ = self.request_json(
            "POST", "/api/ai-payload-preview", preview_body, headers
        )
        self.assertEqual(blocked_preview, 400)
        self.assertIn("rubric_not_ready", blocked_body["error"])
        conn = app.get_db()
        conn.execute("UPDATE assignments SET import_metadata_json=? WHERE id=?", (json.dumps(confirmed_metadata, ensure_ascii=False), assignment_id))
        conn.commit()
        conn.close()

        preview_status, preview, _ = self.request_json(
            "POST", "/api/ai-payload-preview", preview_body, headers
        )
        self.assertEqual(preview_status, 200, preview)
        upload = {
            **preview_body,
            "answer_regions": preview["answer_regions"],
            "ai_privacy_confirmed": True,
            "privacy_status": "no_identity_visible",
            "ai_privacy_preview_token": preview["preview_token"],
            "ai_privacy_fingerprint": preview["preview_fingerprint"],
            "idempotency_key": "accepted-answer-region-normal-submission",
        }
        unchecked = {**upload, "privacy_status": "unchecked"}
        unchecked_status, unchecked_body, _ = self.request_json(
            "POST", "/api/submissions", unchecked, headers
        )
        self.assertEqual(unchecked_status, 400)
        self.assertIn("未确认时不会发送云模型", unchecked_body["error"])

        with patch.object(app, "assess_photo_quality", wraps=app.assess_photo_quality) as whole_page_gate, patch.object(
            app, "enqueue_submission", return_value=None
        ) as enqueue:
            accepted, created, _ = self.request_json("POST", "/api/submissions", upload, headers)
            self.assertEqual(accepted, 202, created)
            enqueue.assert_called_once()
            photo_meta = created["submission"]["photos"]
            self.assertEqual([item["image_role"] for item in photo_meta], ["answer_region", "answer_region"])
            self.assertEqual([item["answer_region_provenance"]["question_id"] for item in photo_meta], ["Q17.1", "Q17.2"])
            self.assertEqual(whole_page_gate.call_count, 0)

            self.assertEqual(created["submission"]["exam_id"], exam_id)
            self.assertEqual(created["submission"]["marking_code"], created["submission"]["student_label"])
            captured = []
            def capture(request, timeout):
                payload = json.loads(request.data)
                captured.append(payload)
                context = MagicMock()
                context.__enter__.return_value = {"test_payload": payload}
                return context

            def response(provider_response, *args, **kwargs):
                # Bind each response to its own request under parallel A/B calls.
                prompt = provider_response["test_payload"]["messages"][1]["content"][0]["text"]
                selected = [q for q in sub_questions if f'"id": "{q["id"]}"' in prompt]
                self.assertEqual(len(selected), 1)
                q = selected[0]
                return json.dumps({"confidence": 0.9, "sub_questions": [{
                    "sub_question_id": q["id"], "answer_status": "answered",
                    "criteria": [{"criterion_id": c["id"], "decision": "none",
                                  "suggested_score": 0, "student_evidence": "", "needs_review": False}
                                 for c in q["criteria"]],
                }]})

            with patch.object(app, "load_dual_review_model_configs", return_value=model_configs), patch.object(
                app, "open_model_request", side_effect=capture
            ) as provider_boundary, patch.object(app, "read_model_chat_content", side_effect=response):
                app.process_submission(created["submission"]["id"])
            self.assertEqual(provider_boundary.call_count, 4)
            self.assertEqual(whole_page_gate.call_count, 0)
            images_by_question = {}
            for request in captured:
                content = request["messages"][1]["content"]
                self.assertEqual(len(content), 2)
                prompt = content[0]["text"]
                qid = next(q["id"] for q in sub_questions if f'"id": "{q["id"]}"' in prompt)
                raw_image = base64.b64decode(content[1]["image_url"]["url"].split(',', 1)[1])
                images_by_question.setdefault(qid, []).append(raw_image)
                for forbidden in ("student_id", "identity_code", "display_name", "roster", created["submission"]["marking_code"]):
                    self.assertNotIn(forbidden, json.dumps(request))
            self.assertEqual(set(images_by_question), {"Q17.1", "Q17.2"})
            for qid, images in images_by_question.items():
                self.assertEqual(len(images), 2)
                self.assertEqual(images[0], images[1])
                image_index = next(i for i, r in enumerate(regions) if r["sub_question_ids"] == [qid])
                self.assertEqual(hashlib.sha256(images[0]).hexdigest(), preview["images"][image_index]["sha256"])


        conn = app.get_db()
        stored = conn.execute("SELECT state,result_json,photos_json FROM submissions WHERE id=?", (created["submission"]["id"],)).fetchone()
        conn.close()
        stored_result = json.loads(stored["result_json"])
        stored_photos = json.loads(stored["photos_json"])
        self.assertEqual(stored["state"], "completed")
        self.assertNotEqual(stored_result.get("mode"), "image-quality-error")
        self.assertNotEqual(stored_result.get("diagnostics", {}).get("error_category"), "image_quality_gate")
        self.assertFalse(stored_result["blocked"], stored_result.get("block_reasons"))
        self.assertEqual(stored_result["dual_review"]["status"], "complete")
        for side in ("A", "B"):
            runs = stored_result["dual_review"]["models"][side]["question_runs"]
            self.assertEqual(len(runs), 2)
            for question_run in runs:
                self.assertEqual(question_run["request_metadata"]["input_mode"], "answer_region")
                self.assertEqual(question_run["question_id"], question_run["request_metadata"]["crop_provenance"][0]["question_id"])
        # Use the server-verified stored claims, not a fabricated image_role.
        conn = app.get_db()
        context = app._exam_layout_profile_context(conn, exam_id)
        conn.close()
        verified = {i: photo["answer_region_provenance"] for i, photo in enumerate(stored_photos, 1)}
        with patch.object(app, "open_model_request") as network:
            for key, value in (("question_id", "Q-mismatch"), ("source_pdf_sha256", "0" * 64), ("exam_id", "f" * 32)):
                bad = {i: dict(v) for i, v in verified.items()}
                bad[1][key] = value
                with self.subTest(provenance_mismatch=key), self.assertRaisesRegex(ValueError, "answer_region_unavailable"):
                    app._require_formal_answer_regions(context, sub_questions, regions, bad)

            photo = stored_photos[0]
            with patch.object(Path, 'read_bytes', return_value=b'tampered-crop'):
                with self.assertRaisesRegex(ValueError, 'stored_crop_hash_mismatch'):
                    app._validate_persisted_answer_region(photo, app.resolve_data_relative_path(photo["path"]), regions)
            network.assert_not_called()
        self.assertEqual([item["image_role"] for item in stored_photos], ["answer_region", "answer_region"])
        self.assertTrue(all(item["answer_region_provenance"].get("alignment_status") == "aligned" for item in stored_photos))
        unexpected_provider.assert_not_called()
        self.crop_boundary_proof = {
            "provider_mock_calls": provider_boundary.call_count, "input_mode": "answer_region",
            "submission_exam_preserved": created["submission"]["exam_id"] == exam_id,
            "per_question_images": {qid: [hashlib.sha256(raw).hexdigest() for raw in images]
                                    for qid, images in images_by_question.items()},
            "question_ids": [q["id"] for q in sub_questions], "whole_page_gate_calls": whole_page_gate.call_count,
            "valid_result_written": not stored_result["blocked"], "privacy_approved": True,
            "identity_free_payload_asserted": True, "mismatch_negatives_passed": True,
        }


    def test_forged_answer_region_role_is_rejected_and_source_page_still_uses_whole_page_gate(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        _, session, _, cookie = self.login(password)
        headers = self.auth_headers(cookie, session["csrf_token"])
        assignment_id, _, _ = self.create_assignment("伪造答题区角色不得绕过质量门")
        status, response, _ = self.request_json(
            "POST", "/api/submissions",
            {
                "assignment_id": assignment_id, "privacy_status": "no_identity_visible",
                "ai_privacy_confirmed": True, "image_role": "answer_region",
                "photos": [{"data_url": self.image_data_url(), "image_role": "answer_region"}],
            }, headers,
        )
        self.assertEqual(status, 400)
        self.assertIn("answer_region_invalid", response["error"])

        tiny = Image.new("RGB", (100, 80), "white")
        image_buffer = io.BytesIO()
        tiny.save(image_buffer, format="JPEG", quality=80)
        target = Path(app.DATA_DIR) / "source-page-quality" / "page.jpg"
        with patch.object(app, "assess_photo_quality", wraps=app.assess_photo_quality) as whole_page_gate:
            meta = app.save_photo_bytes(image_buffer.getvalue(), "image/jpeg", target)
        whole_page_gate.assert_called_once()
        self.assertEqual(meta["image_role"], "source_page")
        self.assertFalse(meta["quality"]["ok"])

        meta.update({"name": "page_01.jpg", "page_sequence": 1})
        submission_id = "b" * 12
        now = app.utc_now()
        conn = app.get_db()
        conn.execute(
            "INSERT INTO submissions (id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,privacy_status,ai_privacy_confirmed) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                submission_id, assignment_id, "合成答卷", "",
                json.dumps([meta], ensure_ascii=False), json.dumps({"dual_review_requested": True}),
                now, "queued", "no_identity_visible", 1,
            ),
        )
        conn.execute(
            "INSERT INTO task_queue (id,submission_id,state,attempt_count,available_at,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("c" * 12, submission_id, "queued", 0, now, "", now, now),
        )
        conn.commit()
        conn.close()
        with patch.object(app, "model_configured", return_value=True), patch.object(
            app, "open_model_request", side_effect=AssertionError("source-page quality gate must block before model invocation")
        ) as provider:
            app.process_submission(submission_id)
        provider.assert_not_called()
        conn = app.get_db()
        result_row = conn.execute("SELECT state,result_json FROM submissions WHERE id=?", (submission_id,)).fetchone()
        conn.close()
        self.assertEqual(result_row["state"], "completed")
        self.assertEqual(json.loads(result_row["result_json"])["mode"], "image-quality-error")

    def test_cloud_loopback_fails_closed_without_password_and_gates_sensitive_apis(self):
        status, session, _ = self.request_json("GET", "/api/auth/session")
        self.assertEqual(status, 401)
        self.assertTrue(session["required"])
        self.assertFalse(session["configured"])
        self.assertTrue(session["cloud_mode"])
        for path in (
            "/api/assignments",
            "/api/backups",
            "/api/submission-photo?submission_id=aaaaaaaaaaaa&photo_index=1",
            "/api/exam-mapping?exam_id=" + "a" * 32,
        ):
            protected_status, _, _ = self.request_json("GET", path)
            self.assertEqual(protected_status, 401, path)
        login_status, _, set_cookie, _ = self.login("synthetic incorrect password")
        self.assertEqual(login_status, 503)
        self.assertNotIn("history_grader_session=", set_cookie)

    def test_authenticated_cloud_teacher_cannot_restart_or_shutdown_over_http(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        headers = self.auth_headers(cookie, session["csrf_token"])

        with patch.object(app.subprocess, "Popen") as spawn_child:
            for path in ("/api/restart", "/api/shutdown"):
                with self.subTest(path=path):
                    status, body, _ = self.request_json("POST", path, {}, headers)
                    self.assertEqual(status, 403)
                    self.assertIn("服务器管理员", body["error"])

        self.assertFalse(getattr(self.server, "restarting", False))
        self.assertFalse(getattr(self.server, "stopping", False))
        spawn_child.assert_not_called()
        health_status, health, _ = self.request_json("GET", "/api/health")
        self.assertEqual(health_status, 200)
        self.assertTrue(health["ok"])

    def test_authenticated_cloud_admin_can_select_only_server_model_presets(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        headers = self.auth_headers(cookie, session["csrf_token"])
        alternate = {
            "label": "合成备用模型",
            "provider": os.environ["HISTORY_GRADER_PROVIDER"],
            "provider_label": "合成服务商",
            "base_url": os.environ["HISTORY_GRADER_BASE_URL"],
            "model": "synthetic-test-alternate",
            "note": "仅用于权限测试",
            "experimental": False,
        }
        with patch.dict(app.MODEL_PRESETS, {"synthetic-test-alternate": alternate}):
            unauthenticated, _, _ = self.request_json(
                "POST", "/api/settings", {"preset_id": "synthetic-test-alternate"}, {"Origin": self.origin}
            )
            self.assertEqual(unauthenticated, 401)
            missing_csrf, _, _ = self.request_json(
                "POST", "/api/settings", {"preset_id": "synthetic-test-alternate"}, self.auth_headers(cookie)
            )
            self.assertEqual(missing_csrf, 403)
            status, result, _ = self.request_json(
                "POST", "/api/settings", {"preset_id": "synthetic-test-alternate"}, headers
            )
        self.assertEqual(status, 200)
        self.assertEqual(os.environ["HISTORY_GRADER_MODEL"], "synthetic-test-alternate")
        self.assertEqual(os.environ["HISTORY_GRADER_API_KEY"], "SYNTHETIC_TEST_KEY_ONLY")
        self.assertNotIn("api_key", result)
        selection = json.loads((app.CONFIG_DIR / app.SERVER_MODEL_SELECTION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(selection["preset_id"], "synthetic-test-alternate")
        self.assertNotIn("api_key", json.dumps(selection).casefold())

    def test_temporary_mobile_token_allows_scoped_mobile_grading_without_roster_access(self):
        token = "SYNTHETIC_MOBILE_UPLOAD_TOKEN"
        app.MOBILE_TOKEN_HASH = hashlib.sha256(token.encode("utf-8")).hexdigest()
        app.MOBILE_TOKEN_EXPIRES_AT = time.time() + 300
        conn = app.get_db()
        conn.execute(
            "INSERT INTO roster_students (id, class_name, student_no, student_name, created_at) VALUES (?, ?, ?, ?, ?)",
            ("synthetic-mobile-roster", "合成班级", "SYNTHETIC_NO_001", "SYNTHETIC_NAME_SENTINEL", app.utc_now()),
        )
        conn.commit()
        conn.close()
        app.STATIC_DIR.mkdir(parents=True, exist_ok=True)
        (app.STATIC_DIR / "index.html").write_text('<main data-app-view="grading">synthetic upload entry</main>', encoding="utf-8")

        with self.opener.open(self.base + "/?access_token=" + token, timeout=10) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("synthetic upload entry", body)
            mobile_cookie = response.headers.get("Set-Cookie", "")
            self.assertIn("history_grader_mobile=", mobile_cookie)
            self.assertIn("HttpOnly", mobile_cookie)
            self.assertIn("Secure", mobile_cookie)

        bearer = {"Authorization": "Bearer " + token}
        allowed, assignments, _ = self.request_json("GET", "/api/assignments", headers=bearer)
        self.assertEqual(allowed, 200)
        self.assertEqual(assignments["assignments"], [])
        classes_status, classes, _ = self.request_json("GET", "/api/rosters", headers=bearer)
        students_status, students, _ = self.request_json(
            "GET", "/api/roster-students?class_name=%E5%90%88%E6%88%90%E7%8F%AD%E7%BA%A7", headers=bearer
        )
        self.assertEqual(classes_status, 200)
        self.assertEqual(students_status, 200)
        self.assertEqual(classes, {"classes": []})
        self.assertEqual(students, {"students": []})
        self.assertNotIn("SYNTHETIC_NAME_SENTINEL", json.dumps(classes) + json.dumps(students))
        confirm_scope_status, _, _ = self.request_json(
            "POST", "/api/confirm", {}, {"Authorization": "Bearer " + token, "Origin": self.origin}
        )
        self.assertEqual(confirm_scope_status, 400)  # route allowed; invalid fixture payload rejected by business validation
        wrong_origin_status, wrong_origin, _ = self.request_json(
            "POST", "/api/ai-payload-preview", {},
            {"Authorization": "Bearer " + token, "Origin": self.base},
        )
        self.assertEqual(wrong_origin_status, 403)
        self.assertEqual(wrong_origin.get("error"), "请求来源校验失败")
        preview_status, _, _ = self.request_json(
            "POST", "/api/ai-payload-preview", {},
            {"Authorization": "Bearer " + token, "Origin": self.origin},
        )
        self.assertEqual(preview_status, 400)
        denied, _, _ = self.request_json("GET", "/api/backups", headers=bearer)
        self.assertEqual(denied, 403)
        denied_settings, _, _ = self.request_json(
            "POST", "/api/settings", {"preset_id": "deepseek-v41-flash"},
            {"Authorization": "Bearer " + token, "Origin": self.origin},
        )
        self.assertEqual(denied_settings, 403)

        dual_specs = {
            "A": {
                "provider": "qwen", "provider_label": "通义千问", "model": "qwen3.8-max",
                "api_origin": "https://dashscope.aliyuncs.com", "base_url": "https://dashscope.aliyuncs.com/v1",
                "preset_id": "qwen38-max", "label": "Qwen3.8-Max",
            },
            "B": {
                "provider": "deepseek", "provider_label": "DeepSeek", "model": "deepseek-flash",
                "api_origin": "https://api.deepseek.com", "base_url": "https://api.deepseek.com/v1",
                "preset_id": "deepseek-v41-flash", "label": "DeepSeek V4.1 Flash",
            },
        }
        with patch.object(app.AppHandler, "is_local_client", return_value=False), patch.object(
            app, "dual_review_model_specs", return_value=dual_specs
        ), patch.object(app, "dual_review_key_is_configured", return_value=True):
            readiness_status, readiness, _ = self.request_json(
                "GET", "/api/mobile/dual-review-readiness", headers=bearer
            )
            self.assertEqual(readiness_status, 200)
            self.assertTrue(readiness["mobile_read_only"])
            self.assertTrue(readiness["configured_a"] and readiness["configured_b"])
            self.assertTrue(readiness["pair_distinct"])
            self.assertEqual([item["model"] for item in readiness["available_models"]], [
                "qwen3.8-max", "deepseek-flash",
            ])
            for private_field in ("base_url", "api_key", "provider_a", "primary_model_configured"):
                self.assertNotIn(private_field, readiness)
            full_status, _, _ = self.request_json("GET", "/api/dual-review/status", headers=bearer)
            config_status, _, _ = self.request_json(
                "POST", "/api/dual-review/configuration", {"side": "A", "preset_id": "qwen38-max"},
                {**bearer, "Origin": self.origin},
            )
        self.assertEqual(full_status, 403)
        self.assertEqual(config_status, 403)

    def test_privacy_templates_require_teacher_session_and_csrf_and_follow_rubric_version(self):
        token = "SYNTHETIC_MOBILE_TEMPLATE_DENY_TOKEN"
        app.MOBILE_TOKEN_HASH = hashlib.sha256(token.encode("utf-8")).hexdigest()
        app.MOBILE_TOKEN_EXPIRES_AT = time.time() + 300
        denied_read, _, _ = self.request_json(
            "GET", "/api/privacy-templates?assignment_id=" + "a" * 12,
            headers={"Authorization": "Bearer " + token},
        )
        denied_write, _, _ = self.request_json(
            "POST", "/api/privacy-templates",
            {"assignment_id": "a" * 12, "page_count": 1, "answer_regions": [], "redactions": [[]]},
            {"Authorization": "Bearer " + token, "Origin": self.origin},
        )
        self.assertEqual(denied_read, 403)
        self.assertEqual(denied_write, 403)
        self.assertNotIn("/api/privacy-templates", app.AppHandler.MOBILE_READ_PATHS)
        self.assertNotIn("/api/privacy-templates", app.AppHandler.MOBILE_WRITE_PATHS)

        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        assignment_id, _, _ = self.create_assignment()
        template = {
            "assignment_id": assignment_id,
            "page_count": 1,
            "version": 2,
            "coordinate_system": "perspective_corrected_page_normalized",
            "answer_regions": [],
            "redactions": [[{"x": 0.1, "y": 0.02, "width": 0.25, "height": 0.06}]],
        }
        missing_csrf, _, _ = self.request_json(
            "POST", "/api/privacy-templates", template, self.auth_headers(cookie)
        )
        self.assertEqual(missing_csrf, 403)
        headers = self.auth_headers(cookie, session["csrf_token"])
        saved, save_result, _ = self.request_json("POST", "/api/privacy-templates", template, headers)
        self.assertEqual(saved, 200)
        self.assertTrue(save_result["ok"])
        read, body, _ = self.request_json("GET", f"/api/privacy-templates?assignment_id={assignment_id}", headers=headers)
        self.assertEqual(read, 200)
        self.assertEqual(body["templates"]["1"]["answer_regions"], [])
        self.assertEqual(body["templates"]["1"]["redactions"][0][0]["x"], 0.1)
        self.assertNotIn("api_key", json.dumps(body).casefold())

        crop_template = dict(template)
        crop_template["answer_regions"] = [{
            "photo_index": 1, "x": 0.1, "y": 0.2, "width": 0.7, "height": 0.6,
            "sub_question_ids": ["Q1"],
        }]
        crop_template_status, _, _ = self.request_json(
            "POST", "/api/privacy-templates", crop_template, headers
        )
        self.assertEqual(crop_template_status, 400)

        conn = app.get_db()
        conn.execute("UPDATE assignments SET question = ? WHERE id = ?", ("合成题目：评分细则已更新", assignment_id))
        conn.commit()
        conn.close()
        changed_read, changed_body, _ = self.request_json(
            "GET", f"/api/privacy-templates?assignment_id={assignment_id}", headers=headers
        )
        self.assertEqual(changed_read, 200)
        self.assertEqual(changed_body["templates"], {})

    def test_synthetic_model_result_cannot_be_confirmed_or_reviewed(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        assignment_id, _, _ = self.create_assignment()
        submission_id = "b" * 12
        conn = app.get_db()
        conn.execute(
            "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at, state, source_channel, identity_status, identity_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id, assignment_id, "SYNTHETIC_CODE", "", "[]",
                json.dumps({"mode": "vision-model", "simulation_model": True, "blocked": True, "suggested_total": 1, "max_total": 2}),
                app.utc_now(), "completed", "web", "anonymous", "marking_code",
            ),
        )
        conn.commit()
        conn.close()
        headers = self.auth_headers(cookie, session["csrf_token"])
        confirm_status, _, _ = self.request_json(
            "POST", "/api/confirm", {"submission_id": submission_id, "final_total": 1}, headers
        )
        review_status, _, _ = self.request_json(
            "POST", "/api/review", {"submission_id": submission_id, "confirm_original": True}, headers
        )
        self.assertEqual(confirm_status, 400)
        self.assertEqual(review_status, 400)

    def test_business_html_is_server_gated_and_login_shell_contains_no_app(self):
        preview=app.STATIC_DIR/'next';preview.mkdir(parents=True,exist_ok=True)
        (preview/'index.html').write_text('<main id="vue-login-shell">synthetic Vue shell</main>',encoding='utf-8')
        for path in ("/", "/index.html", "/legacy", "/static/index.html", "/static/%2e%2e/index.html"):
            with self.opener.open(self.base + path, timeout=10) as response:
                body = response.read().decode("utf-8")
                vue=path in ('/','/index.html')
                self.assertEqual(response.geturl(), self.base + ('/next/login' if vue else '/login'))
                self.assertIn('no-store',response.headers.get('Cache-Control',''))
                self.assertIn('id="vue-login-shell"' if vue else 'id="workbench-login"',body)
                self.assertNotIn('data-app-view=',body)
        password=self.configure_password();web_auth.AUTH_MANAGER=web_auth.SingleTeacherAuth()
        status,_,_,cookie=self.login(password);self.assertEqual(status,200)
        (app.STATIC_DIR/'index.html').write_text('<main data-app-view="grading">synthetic private app</main>',encoding='utf-8')
        for path,expected in (('/', 'vue-login-shell'),('/legacy','data-app-view="grading"')):
            with self.opener.open(Request(self.base+path,headers={'Cookie':cookie}),timeout=10) as response:
                self.assertIn(expected,response.read().decode('utf-8'))
                self.assertIn('no-store',response.headers.get('Cache-Control',''))

    def test_cloud_upload_failure_audit_drops_client_supplied_personal_text(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        _, session, _, cookie = self.login(password)
        csrf = session["csrf_token"]
        assignment_id, _, _ = self.create_assignment()
        status, batch, _ = self.request_json(
            "POST",
            "/api/batches",
            {
                "assignment_id": assignment_id,
                "class_name": "合成班级",
                "batch_request_id": "synthetic-cloud-log-test",
                "items": [{"student_label": "SYNTHETIC_LOG_STUDENT", "page_count": 1}],
            },
            self.auth_headers(cookie, csrf),
        )
        self.assertEqual(status, 201)
        status, _, _ = self.request_json(
            "POST",
            "/api/batches/upload-failed",
            {
                "batch_id": batch["batch_id"],
                "sequence": 1,
                "error": "SYNTHETIC_LOG_STUDENT named-photo-file.jpg COOKIE_SENTINEL",
            },
            self.auth_headers(cookie, csrf),
        )
        self.assertEqual(status, 200)
        conn = app.get_db()
        try:
            stored = conn.execute(
                "SELECT pause_reason, manifest_json FROM grading_batches WHERE id=?",
                (batch["batch_id"],),
            ).fetchone()
            audit = conn.execute(
                "SELECT payload_json FROM audit_events WHERE entity_id=? AND event_type='grading_batch_upload_failed'",
                (batch["batch_id"],),
            ).fetchone()
        finally:
            conn.close()
        manifest = json.loads(stored["manifest_json"])
        upload_error = manifest["items"][0].get("upload_error", "")
        persisted = json.dumps([stored["pause_reason"], upload_error, audit["payload_json"]], ensure_ascii=False)
        self.assertNotIn("SYNTHETIC_LOG_STUDENT", persisted)
        self.assertNotIn("named-photo-file.jpg", persisted)
        self.assertNotIn("COOKIE_SENTINEL", persisted)

    def test_login_throttling_secure_cookie_csrf_session_and_logout(self):
        password = self.configure_password()
        now = 1_800_000_000
        clock = [now]
        replies = []
        with patch.object(password_auth.time, "time", side_effect=lambda: clock[0]):
            for offset in (0, 1, 2, 4, 8):
                clock[0] = now + offset
                replies.append(web_auth.AUTH_MANAGER.login(
                    "teacher", "synthetic wrong password", f"198.51.100.{offset + 1}"
                )[0])
            self.assertEqual(replies, [401] + [429] * (web_auth.MAX_LOGIN_ATTEMPTS - 1))
            locked, locked_data, _ = web_auth.AUTH_MANAGER.login(
                "teacher", "synthetic wrong password", "198.51.100.99"
            )
            self.assertEqual(locked, 429)
            self.assertGreater(locked_data["retry_after"], 0)

        # Clear the synthetic throttle rows so this same test can exercise the
        # independent cookie, CSRF, and logout assertions below.
        conn = app.get_db()
        try:
            conn.execute("DELETE FROM auth_rate_limits")
            conn.commit()
        finally:
            conn.close()

        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        status, data, set_cookie, cookie = self.login(password)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Secure", set_cookie)
        csrf = data["csrf_token"]
        protected_status, _, _ = self.request_json("GET", "/api/assignments", headers=self.auth_headers(cookie))
        self.assertEqual(protected_status, 200)
        denied, _, _ = self.request_json("POST", "/api/retention/preview", {"retention_days": 0}, self.auth_headers(cookie))
        self.assertEqual(denied, 403)
        allowed, report, _ = self.request_json(
            "POST", "/api/retention/preview", {"retention_days": 0, "dry_run": True}, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(allowed, 200)
        self.assertFalse(report["deletion_performed"])
        logout, _, logout_headers = self.request_json(
            "POST", "/api/auth/logout", {}, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(logout, 200)
        self.assertIn("Max-Age=0", logout_headers.get("Set-Cookie", ""))
        expired, _, _ = self.request_json("GET", "/api/assignments", headers=self.auth_headers(cookie))
        self.assertEqual(expired, 401)

    def test_totp_is_verified_when_configured(self):
        secret = base64.b32encode(b"synthetic totp test secret").decode("ascii").rstrip("=")
        now = 1_700_000_000
        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
        counter = int(now // 30)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1_000_000:06d}"
        self.assertTrue(web_auth._verify_totp(secret, code, now))
        self.assertFalse(web_auth._verify_totp(secret, "000000", now))

        os.environ["HISTORY_GRADER_TOTP_REQUIRED"] = "true"
        os.environ["HISTORY_GRADER_TOTP_SECRET"] = secret
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        accepted, _, login_headers, _ = self.login(password)
        self.assertEqual(accepted, 200)
        self.assertIn("history_grader_session=", login_headers)

    def configure_email_otp(self):
        os.environ.update({
            "HISTORY_GRADER_EMAIL_OTP_REQUIRED": "true",
            "HISTORY_GRADER_ADMIN_EMAIL": "admin@example.test",
            "HISTORY_GRADER_SMTP_HOST": "smtp.example.test",
            "HISTORY_GRADER_SMTP_PORT": "587",
            "HISTORY_GRADER_SMTP_USERNAME": "mailer@example.test",
            "HISTORY_GRADER_SMTP_PASSWORD": "SYNTHETIC_SMTP_PASSWORD_ONLY",
            "HISTORY_GRADER_SMTP_FROM": "grader@example.test",
        })

    def test_email_otp_is_tls_sent_expiring_one_time_and_client_bound(self):
        self.configure_password()
        self.configure_email_otp()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        delivered = []
        with patch.object(password_auth, "_send_email_otp", side_effect=lambda config, code, **kwargs: delivered.append(code)):
            status, session, headers = self.request_json(
                "POST",
                "/api/auth/login",
                {"username": "teacher", "password": "synthetic test password 123"},
                {"Origin": self.origin},
            )
        self.assertEqual(status, 200)
        self.assertTrue(session["authenticated"])
        self.assertIn("history_grader_session=", headers.get("Set-Cookie", ""))
        self.assertEqual(delivered, [])

        otp_only, _, otp_headers = self.request_json(
            "POST",
            "/api/auth/login",
            {"challenge_id": "synthetic-challenge-id-that-is-not-a-login-credential", "email_code": "123456"},
            {"Origin": self.origin},
        )
        self.assertEqual(otp_only, 401)
        self.assertNotIn("history_grader_session=", otp_headers.get("Set-Cookie", ""))

    def test_email_otp_is_not_accepted_from_another_client_and_requires_smtp_configuration(self):
        self.configure_password()
        self.configure_email_otp()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        accepted, _, _ = web_auth.AUTH_MANAGER.login(
            "teacher", "synthetic test password 123", "client-a", handler=None
        )
        self.assertEqual(accepted, 200)
        os.environ["HISTORY_GRADER_SMTP_PASSWORD"] = ""
        config = web_auth.auth_config()
        self.assertTrue(config.configured)
        self.assertFalse(config.password_reset_configured)
        otp_only, _, _ = web_auth.AUTH_MANAGER.login(
            "", "", "client-b", challenge_id="synthetic-challenge-id-that-is-not-a-login-credential", email_code="123456"
        )
        self.assertEqual(otp_only, 401)

    def test_email_otp_sender_uses_verified_tls(self):
        self.configure_password()
        self.configure_email_otp()
        config = web_auth.auth_config()
        client = MagicMock()
        context_manager = MagicMock()
        context_manager.__enter__.return_value = client
        with patch.object(web_auth.smtplib, "SMTP", return_value=context_manager) as smtp:
            web_auth._send_email_otp(config, "123456")
        smtp.assert_called_once_with(config.smtp_host, config.smtp_port, timeout=10)
        client.starttls.assert_called_once()
        self.assertEqual(client.auth.call_args.args[0], "LOGIN")
        self.assertFalse(client.auth.call_args.kwargs["initial_response_ok"])
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "admin@example.test")
        self.assertIn("123456", message.get_content())
        self.assertNotIn("SYNTHETIC_SMTP_PASSWORD_ONLY", message.as_string())

    def test_email_otp_sender_uses_implicit_tls_on_port_465(self):
        self.configure_password()
        self.configure_email_otp()
        os.environ["HISTORY_GRADER_SMTP_PORT"] = "465"
        config = web_auth.auth_config()
        client = MagicMock()
        context_manager = MagicMock()
        context_manager.__enter__.return_value = client
        with patch.object(web_auth.smtplib, "SMTP_SSL", return_value=context_manager) as smtp_ssl:
            web_auth._send_email_otp(config, "654321")
        smtp_ssl.assert_called_once()
        self.assertEqual(smtp_ssl.call_args.args[:2], (config.smtp_host, 465))
        self.assertIn("context", smtp_ssl.call_args.kwargs)
        client.starttls.assert_not_called()
        self.assertEqual(client.auth.call_args.args[0], "LOGIN")
        self.assertFalse(client.auth.call_args.kwargs["initial_response_ok"])
        self.assertIn("654321", client.send_message.call_args.args[0].get_content())

    def test_email_otp_expires_and_smtp_failure_never_creates_a_session(self):
        self.configure_password()
        self.configure_email_otp()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        sent_codes = []
        with patch.object(password_auth, "_send_email_otp", side_effect=lambda config, code, **kwargs: sent_codes.append(code)):
            expired_status, pending, _ = self.request_json(
                "POST",
                "/api/auth/password-reset/request",
                {"identity": "teacher"},
                {"Origin": self.origin},
            )
            self.assertEqual(expired_status, 202)
            challenge_key = hashlib.sha256(pending["challenge_id"].encode("ascii")).hexdigest()
            conn = app.get_db()
            try:
                conn.execute("UPDATE auth_otp_challenges SET expires_at = ? WHERE challenge_hash = ?", (int(time.time()) - 1, challenge_key))
                conn.commit()
            finally:
                conn.close()
            expired, _, expired_headers = self.request_json(
                "POST", "/api/auth/password-reset/complete",
                {"challenge_id": pending["challenge_id"], "email_code": sent_codes[0], "new_password": "synthetic-new-password-456"},
                {"Origin": self.origin},
            )
            self.assertEqual(expired, 401)
            self.assertNotIn("history_grader_session=", expired_headers.get("Set-Cookie", ""))

        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        with patch.object(password_auth, "_send_email_otp", side_effect=OSError("synthetic SMTP failure")):
            failed, response, failed_headers = self.request_json(
                "POST", "/api/auth/password-reset/request", {"identity": "teacher"},
                {"Origin": self.origin},
            )
        self.assertEqual(failed, 503)
        self.assertEqual(response["error"], "验证码邮件发送失败，请稍后重试")
        self.assertNotIn("synthetic SMTP failure", json.dumps(response))
        self.assertNotIn("history_grader_session=", failed_headers.get("Set-Cookie", ""))

    def test_ai_preview_returns_exact_redacted_image_and_token_binds_upload(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        _, session, _, cookie = self.login(password)
        csrf = session["csrf_token"]
        assignment_id, sub_questions, _ = self.create_assignment()
        data_url = self.image_data_url(marker=True)
        redactions = [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}]
        corners = [[0.04, 0.03], [0.96, 0.02], [0.97, 0.97], [0.03, 0.96]]
        answer_regions = [{
            "photo_index": 1, "x": 0, "y": 0, "width": 1, "height": 1,
            "sub_question_ids": [sub_questions[0]["id"]],
        }]
        status, preview, _ = self.request_json(
            "POST",
            "/api/ai-payload-preview",
            {
                "assignment_id": assignment_id,
                "class_name": "合成班级",
                "student_label": "SYNTHETIC_STUDENT_NAME_SENTINEL",
                "photos": [{"data_url": data_url, "ai_redactions": redactions, "corners": corners}],
                "answer_regions": answer_regions,
            },
            self.auth_headers(cookie, csrf),
        )
        self.assertEqual(status, 200)
        self.assertFalse(preview["blocked"])
        self.assertIn("姓名/学号/班级/学生标签", preview["excluded_fields"])
        self.assertNotIn("SYNTHETIC_TEST_KEY_ONLY", json.dumps(preview))
        image_bytes = app.data_url_bytes(preview["images"][0]["data_url"], require_image=True)
        with Image.open(io.BytesIO(image_bytes)) as redacted:
            redacted = redacted.convert("RGB")
            pixel = redacted.getpixel((200, 160))
            self.assertLess(max(pixel), 35)

        payload = {
            "assignment_id": assignment_id,
            "class_name": "合成班级",
            "student_label": "SYNTHETIC_STUDENT_NAME_SENTINEL",
            "answer_text": "SYNTHETIC_ANSWER_TEXT_SENTINEL",
            "ai_privacy_confirmed": True,
            "privacy_status": "redacted",
            "ai_privacy_preview_token": preview["preview_token"],
            "ai_privacy_fingerprint": preview["preview_fingerprint"],
            "answer_regions": answer_regions,
            "idempotency_key": "synthetic-privacy-test-upload",
            "photos": [{
                "name": "SYNTHETIC_PRIVATE_FILENAME_SENTINEL.jpg",
                "corners": corners,
                "page_sequence": 1,
                "data_url": data_url,
                "original_data_url": data_url,
                "ai_redactions": redactions,
            }],
        }
        unchecked_payload = json.loads(json.dumps(payload))
        unchecked_payload["privacy_status"] = "unchecked"
        unchecked_status, unchecked_result, _ = self.request_json(
            "POST", "/api/submissions", unchecked_payload, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(unchecked_status, 400)
        self.assertIn("未确认时不会发送云模型", unchecked_result["error"])

        altered_corners_payload = json.loads(json.dumps(payload))
        altered_corners_payload["photos"][0]["corners"] = [[0.02, 0.03], [0.96, 0.02], [0.97, 0.97], [0.03, 0.96]]
        rejected_corners, _, _ = self.request_json(
            "POST", "/api/submissions", altered_corners_payload, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(rejected_corners, 400)

        changed_student_payload = json.loads(json.dumps(payload))
        changed_student_payload["student_label"] = "SYNTHETIC_OTHER_STUDENT_SENTINEL"
        rejected_student, _, _ = self.request_json(
            "POST", "/api/submissions", changed_student_payload, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(rejected_student, 400)

        second_login_status, second_session, _, second_cookie = self.login(password)
        self.assertEqual(second_login_status, 200)
        cross_session, _, _ = self.request_json(
            "POST", "/api/submissions", payload,
            self.auth_headers(second_cookie, second_session["csrf_token"]),
        )
        self.assertEqual(cross_session, 400)

        with patch.object(app, "enqueue_submission", return_value=None) as enqueue:
            with ThreadPoolExecutor(max_workers=8) as pool:
                concurrent = list(pool.map(
                    lambda _index: self.request_json(
                        "POST", "/api/submissions", payload, self.auth_headers(cookie, csrf)
                    ),
                    range(8),
                ))
        statuses = [item[0] for item in concurrent]
        self.assertEqual(statuses.count(202), 1)
        self.assertTrue(set(statuses).issubset({200, 202, 400}))
        enqueue.assert_called_once()
        accepted, response, _ = next(item for item in concurrent if item[0] == 202)
        self.assertEqual(accepted, 202, response)
        submission_id = response["submission"]["id"]
        conn = app.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions WHERE id=?", (submission_id,)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_queue WHERE submission_id=?", (submission_id,)).fetchone()[0], 1)
        row = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        photo_meta = json.loads(row["photos_json"])
        self.assertEqual(row["ai_privacy_confirmed"], 1)
        self.assertTrue(row["ai_privacy_fingerprint"])
        self.assertEqual(photo_meta[0]["name"], "page_01.jpg")
        self.assertEqual(len(photo_meta[0]["ai_redactions"]), 1)
        self.assertEqual(row["student_label"], row["marking_code"])
        self.assertEqual(row["student_name"], "")
        self.assertEqual(row["student_no"], "")
        self.assertEqual(row["class_name"], "")
        saved_original = app.PORTABLE_ROOT / photo_meta[0]["original_path"]
        self.assertEqual(saved_original.read_bytes(), base64.b64decode(data_url.split(",", 1)[1]))
        self.assertNotIn("SYNTHETIC_STUDENT_NAME_SENTINEL", str(saved_original))
        saved_processed = app.PORTABLE_ROOT / photo_meta[0]["path"]
        model_image = app.prepare_model_image(saved_processed, redactions)
        self.assertEqual(model_image, app.data_url_bytes(preview["processed_pages"][0]["data_url"], require_image=True))
        self.assertEqual(hashlib.sha256(model_image).hexdigest(), photo_meta[0]["approved_ai_page_sha256"])
        self.assertTrue(photo_meta[0]["approved_prompt_sha256"])
        conn.close()

        replay_status, replay, _ = self.request_json(
            "POST", "/api/submissions", payload, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["idempotent_replay"])
        reused_payload = json.loads(json.dumps(payload))
        reused_payload["idempotency_key"] = "synthetic-privacy-token-reuse"
        reused_status, _, _ = self.request_json(
            "POST", "/api/submissions", reused_payload, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(reused_status, 400)

        altered = dict(payload["photos"][0], ai_redactions=[])
        conn = app.get_db()
        saved_sub_questions = json.loads(conn.execute(
            "SELECT sub_questions_json FROM assignments WHERE id=?", (assignment_id,)
        ).fetchone()[0])
        conn.close()
        with self.assertRaisesRegex(ValueError, "预览与本次题目或图片不一致"):
            app._verify_ai_preview_token(
                preview["preview_token"], assignment_id, "合成题目：分析制度变化",
                app.clean_sub_questions(saved_sub_questions),
                [altered],
                answer_regions=answer_regions,
            )

    def test_two_phase_page_preview_crop_payload_and_grade_writeback_are_anonymous(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        _, session, _, cookie = self.login(password)
        csrf = session["csrf_token"]
        assignment_id, _single_question, _ = self.create_assignment("合成匿名裁切题")
        sub_questions = app.clean_sub_questions([
            {
                "id": "Q1", "label": "（1）", "question": "说明制度变化",
                "standard_answer": "变化甲", "scoring_mode": "weighted", "max_score": 2,
                "criteria": [{"id": "C1", "label": "变化甲", "score": 2}],
            },
            {
                "id": "Q2", "label": "（2）", "question": "说明制度影响",
                "standard_answer": "影响乙", "scoring_mode": "weighted", "max_score": 2,
                "criteria": [{"id": "C2", "label": "影响乙", "score": 2}],
            },
        ])
        conn = app.get_db()
        conn.execute(
            "UPDATE assignments SET sub_questions_json=?, criteria_json=? WHERE id=?",
            (json.dumps(sub_questions, ensure_ascii=False), json.dumps(app.flatten_criteria(sub_questions), ensure_ascii=False), assignment_id),
        )
        conn.execute(
            "INSERT INTO roster_students (id, class_name, student_no, student_name, created_at) VALUES (?, ?, ?, ?, ?)",
            ("synthetic-mapping-row", "合成班级", "SYNTHETIC_NO_001", "SYNTHETIC_NAME_SENTINEL", app.utc_now()),
        )
        conn.commit()
        conn.close()

        header = Image.new("RGB", (1200, 900), "white")
        draw = ImageDraw.Draw(header)
        draw.text((30, 25), "SYNTHETIC_NAME_SENTINEL SYNTHETIC_NO_001", fill="black")
        draw.line((180, 210, 960, 215), fill="black", width=10)
        draw.line((180, 250, 900, 255), fill="black", width=8)
        image_buffer = io.BytesIO()
        header.save(image_buffer, format="JPEG", quality=88)
        page_url = "data:image/jpeg;base64," + base64.b64encode(image_buffer.getvalue()).decode("ascii")
        corners = [[0.04, 0.06], [0.96, 0.04], [0.97, 0.96], [0.03, 0.97]]
        photos = [
            {"data_url": page_url, "corners": corners, "ai_redactions": []},
            {"data_url": page_url, "corners": None, "ai_redactions": []},
        ]
        batch_status, batch, _ = self.request_json(
            "POST", "/api/batches",
            {
                "assignment_id": assignment_id,
                "class_name": "合成班级",
                "batch_request_id": "synthetic-anonymous-crop-exam",
                "items": [{"student_label": "SYNTHETIC_NAME_SENTINEL", "page_count": 2}],
            },
            self.auth_headers(cookie, csrf),
        )
        self.assertEqual(batch_status, 201)
        self.assertRegex(batch["items"][0]["marking_code"], r"^[0-9A-F]{32}$")
        self.assertNotIn("SYNTHETIC_NAME_SENTINEL", json.dumps(batch["items"], ensure_ascii=False))

        with patch.object(app, "open_model_request") as no_model_call:
            prepared_status, prepared, _ = self.request_json(
                "POST", "/api/ai-payload-preview",
                {"preview_stage": "prepare", "assignment_id": assignment_id, "photos": photos},
                self.auth_headers(cookie, csrf),
            )
        self.assertEqual(prepared_status, 200)
        self.assertEqual(prepared["stage"], "prepared_pages")
        self.assertFalse(prepared["model_request_sent"])
        self.assertEqual(len(prepared["prepared_pages"]), 2)
        no_model_call.assert_not_called()

        regions = [
            {"photo_index": 1, "x": 0.2, "y": 0.24, "width": 0.44, "height": 0.30, "sub_question_ids": ["Q1", "Q2"]},
            {"photo_index": 2, "x": 0.25, "y": 0.36, "width": 0.40, "height": 0.25, "sub_question_ids": ["Q2"]},
        ]
        preview_status, preview, _ = self.request_json(
            "POST", "/api/ai-payload-preview",
            {
                "assignment_id": assignment_id,
                "class_name": "合成班级",
                "student_label": "SYNTHETIC_NAME_SENTINEL",
                "photos": photos,
                "answer_regions": regions,
            },
            self.auth_headers(cookie, csrf),
        )
        self.assertEqual(preview_status, 200)
        self.assertEqual(preview["image_count"], 2)
        self.assertNotIn("SYNTHETIC_NAME_SENTINEL", preview["prompt"])
        self.assertNotIn("SYNTHETIC_NO_001", preview["prompt"])
        self.assertEqual(
            [item["sha256"] for item in preview["processed_pages"]],
            [item["sha256"] for item in prepared["prepared_pages"]],
        )
        preview_crop_bytes = [app.data_url_bytes(item["data_url"], require_image=True) for item in preview["images"]]

        batch_item = batch["items"][0]
        upload = {
            "assignment_id": assignment_id,
            "class_name": "合成班级",
            "student_label": "SYNTHETIC_NAME_SENTINEL",
            "batch_id": batch["batch_id"],
            "submission_id": batch_item["submission_id"],
            "sequence": 1,
            "idempotency_key": f"{batch['batch_id']}:1",
            "answer_text": "SYNTHETIC_TYPED_ANSWER_SENTINEL",
            "answer_regions": regions,
            "ai_privacy_confirmed": True,
            "privacy_status": "pre_anonymized",
            "ai_privacy_preview_token": preview["preview_token"],
            "ai_privacy_fingerprint": preview["preview_fingerprint"],
            "photos": [
                {
                    "name": f"SYNTHETIC_NAME_SENTINEL-page-{index}.jpg",
                    "page_sequence": index,
                    "data_url": page_url,
                    "original_data_url": page_url,
                    "corners": corners if index == 1 else None,
                    "ai_redactions": [],
                }
                for index in (1, 2)
            ],
        }
        with patch.object(app, "assess_photo_quality", return_value={"ok": True, "issues": [], "metrics": {}}), patch.object(
            app, "enqueue_submission", return_value=None
        ):
            upload_status, created, _ = self.request_json(
                "POST", "/api/submissions", upload, self.auth_headers(cookie, csrf)
            )
        self.assertEqual(upload_status, 202)
        submission = created["submission"]
        self.assertEqual(submission["marking_code"], batch_item["marking_code"])
        self.assertEqual([photo["page_sequence"] for photo in submission["photos"]], [1, 2])
        self.assertEqual(submission["student_name"], "")
        self.assertEqual(submission["student_no"], "")
        self.assertEqual(submission["class_name"], "")

        conn = app.get_db()
        stored = conn.execute("SELECT * FROM submissions WHERE id=?", (submission["id"],)).fetchone()
        self.assertEqual((stored["student_label"], stored["class_name"], stored["student_no"], stored["student_name"], stored["answer_text"]), (batch_item["marking_code"], "", "", "", ""))
        self.assertNotIn("SYNTHETIC_NAME_SENTINEL", stored["result_json"])
        self.assertNotIn("SYNTHETIC_TYPED_ANSWER_SENTINEL", stored["result_json"])
        stored_regions = json.loads(stored["answer_regions_json"])
        self.assertTrue(all(region.get("approved_model_sha256") for region in stored_regions))
        conn.close()

        model_response = {
            "confidence": 0.9,
            "summary": "模拟匿名批改完成",
            "sub_questions": [
                {
                    "sub_question_id": "Q1", "criteria": [{
                        "criterion_id": "C1", "suggested_score": 2, "decision": "full",
                        "student_evidence": "合成证据甲", "reason": "符合模拟评分点", "needs_review": False,
                        "evidence": {"photo_index": 1, "x": 0.10, "y": 0.20, "width": 0.20, "height": 0.10},
                    }],
                },
                {
                    "sub_question_id": "Q2", "criteria": [{
                        "criterion_id": "C2", "suggested_score": 2, "decision": "full",
                        "student_evidence": "合成证据乙", "reason": "符合模拟评分点", "needs_review": False,
                        "evidence": {"photo_index": 2, "x": 0.15, "y": 0.25, "width": 0.25, "height": 0.15},
                    }],
                },
            ],
        }
        request_context = MagicMock()
        request_context.__enter__.return_value = object()
        with patch.object(app, "open_model_request", return_value=request_context) as model_request, patch.object(
            app, "read_model_chat_content", return_value=json.dumps(model_response, ensure_ascii=False)
        ):
            app.process_submission(submission["id"])
        outbound = json.loads(model_request.call_args.args[0].data.decode("utf-8"))
        serialized_payload = json.dumps(outbound, ensure_ascii=False)
        for sentinel in ("SYNTHETIC_NAME_SENTINEL", "SYNTHETIC_NO_001", "SYNTHETIC_TYPED_ANSWER_SENTINEL", batch_item["marking_code"]):
            self.assertNotIn(sentinel, serialized_payload)
        self.assertEqual(model_request.call_args.args[0].headers.get("Authorization"), "Bearer SYNTHETIC_TEST_KEY_ONLY")
        sent_images = [
            app.base64.b64decode(item["image_url"]["url"].split(",", 1)[1])
            for item in outbound["messages"][1]["content"]
            if item.get("type") == "image_url"
        ]
        self.assertEqual(sent_images, preview_crop_bytes)

        graded = self.request_json(
            "GET", f"/api/submission?submission_id={submission['id']}", headers=self.auth_headers(cookie)
        )[1]["submission"]
        self.assertEqual(graded["result"]["suggested_total"], 4)
        self.assertNotIn("student_identity", graded["result"])
        q1_evidence = graded["result"]["sub_questions"][0]["criteria"][0]["evidence"]
        q2_evidence = graded["result"]["sub_questions"][1]["criteria"][0]["evidence"]
        self.assertEqual(q1_evidence["photo_index"], 1)
        self.assertEqual(q1_evidence["coordinate_system"], "perspective_corrected_page_normalized")
        self.assertAlmostEqual(q1_evidence["x"], regions[0]["x"] + 0.10 * regions[0]["width"], places=5)
        self.assertEqual(q2_evidence["photo_index"], 2)
        self.assertAlmostEqual(q2_evidence["y"], regions[1]["y"] + 0.25 * regions[1]["height"], places=5)

        map_status, mapping_result, _ = self.request_json(
            "GET", f"/api/exam-mapping?exam_id={batch['exam_id']}", headers=self.auth_headers(cookie)
        )
        self.assertEqual(map_status, 200)
        self.assertEqual(mapping_result["mappings"][0]["binding_status"], "unmatched")
        self.assertIsNone(mapping_result["mappings"][0]["roster_student_id"])
        self.assertNotIn("SYNTHETIC_NAME_SENTINEL", json.dumps(mapping_result, ensure_ascii=False))
        central_status, central, _ = self.request_json(
            "GET", f"/api/assignment-markings?assignment_id={assignment_id}", headers=self.auth_headers(cookie)
        )
        self.assertEqual(central_status, 200)
        self.assertEqual(central["records"][0]["marking_code"], batch_item["marking_code"])
        self.assertEqual(central["records"][0]["state"], "completed")
        self.assertEqual(
            self.request_json("GET", f"/api/assignment-markings?assignment_id={assignment_id}")[0],
            401,
        )
        export_request = Request(
            self.base + f"/api/export.csv?assignment_id={assignment_id}",
            headers=self.auth_headers(cookie),
        )
        with self.opener.open(export_request, timeout=5) as response:
            before_confirm = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual(len(before_confirm), 1)
        anonymous_request = Request(
            self.base + f"/api/export-anonymous.csv?assignment_id={assignment_id}",
            headers=self.auth_headers(cookie),
        )
        with self.opener.open(anonymous_request, timeout=5) as response:
            anonymous_before_confirm = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual(len(anonymous_before_confirm), 1)
        confirm_status, _, _ = self.request_json(
            "POST", "/api/confirm",
            {"submission_id": submission["id"], "teacher_point_decisions": [
                {"question_id": "Q1", "rubric_point_id": "C1", "choice": "teacher_full"},
                {"question_id": "Q2", "rubric_point_id": "C2", "choice": "teacher_full"},
            ]},
            self.auth_headers(cookie, csrf),
        )
        self.assertEqual(confirm_status, 200)
        export_request = Request(
            self.base + f"/api/export.csv?assignment_id={assignment_id}",
            headers=self.auth_headers(cookie),
        )
        with self.opener.open(export_request, timeout=5) as response:
            exported = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual(len(exported), 1)
        anonymous_request = Request(
            self.base + f"/api/export-anonymous.csv?assignment_id={assignment_id}",
            headers=self.auth_headers(cookie),
        )
        with self.opener.open(anonymous_request, timeout=5) as response:
            anonymous_export = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual(len(anonymous_export), 2)
        self.assertEqual(anonymous_export[1][0], batch_item["marking_code"])
        self.assertEqual(anonymous_export[1][1], "4.0")
        self.assertNotIn("SYNTHETIC_NAME_SENTINEL", json.dumps(anonymous_export, ensure_ascii=False))
        self.assertNotIn("SYNTHETIC_NO_001", json.dumps(anonymous_export, ensure_ascii=False))
        bind_status, _, _ = self.request_json(
            "POST", "/api/bind-student",
            {"submission_id": submission["id"], "roster_student_id": "synthetic-mapping-row"},
            self.auth_headers(cookie, csrf),
        )
        self.assertEqual(bind_status, 200)
        export_request = Request(
            self.base + f"/api/export.csv?assignment_id={assignment_id}",
            headers=self.auth_headers(cookie),
        )
        with self.opener.open(export_request, timeout=5) as response:
            exported = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual(len(exported), 2)
        self.assertEqual(exported[1][1:3], ["SYNTHETIC_NO_001", "SYNTHETIC_NAME_SENTINEL"])

    def test_actual_cloud_model_payload_excludes_identity_and_answer_text(self):
        self.configure_password()
        assignment_id, sub_questions, assignment = self.create_assignment()
        data_url = self.image_data_url(marker=True)
        temp_dir = Path(self.temp.name) / "model-payload"
        temp_dir.mkdir()
        photo_path = temp_dir / "page_01.jpg"
        photo_meta = app.save_photo(data_url, photo_path, temp_dir / "page_01.original")
        photo_meta["ai_redactions"] = [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}]
        photo_meta["approved_ai_page_sha256"] = hashlib.sha256(
            app.prepare_model_image(photo_path, photo_meta["ai_redactions"])
        ).hexdigest()
        photo_meta["approved_prompt_sha256"] = hashlib.sha256(
            app.build_vision_prompt_text(str(assignment["question"]), sub_questions, privacy_mode=True).encode("utf-8")
        ).hexdigest()
        model_response = {
            "confidence": 0.8,
            "summary": "合成结果",
            "student_identity": {"student_name": "AI_IDENTITY_SENTINEL", "confidence": 1},
            "sub_questions": [{
                "sub_question_id": sub_questions[0]["id"],
                "label": sub_questions[0]["label"],
                "criteria": [{
                    "criterion_id": sub_questions[0]["criteria"][0]["id"],
                    "suggested_score": 2,
                    "decision": "full",
                    "student_evidence": "合成证据",
                    "evidence": {"photo_index": 1, "x": 0.4, "y": 0.4, "width": 0.2, "height": 0.1},
                    "reason": "合成理由",
                    "needs_review": False,
                }],
            }],
        }
        request_data = {}
        context = MagicMock()
        context.__enter__.return_value = object()
        with patch.object(app, "open_model_request", return_value=context) as open_request, patch.object(
            app, "read_model_chat_content", return_value=json.dumps(model_response, ensure_ascii=False)
        ), patch.dict(os.environ, {"HISTORY_GRADER_API_KEY": "SYNTHETIC_TEST_KEY_ONLY"}):
            result = app.call_vision_model(
                assignment,
                sub_questions,
                "SYNTHETIC_ANSWER_TEXT_SENTINEL",
                [photo_path],
                {},
                [photo_meta],
                True,
            )
            request_data = json.loads(open_request.call_args.args[0].data.decode("utf-8"))
            self.assertEqual(open_request.call_args.args[0].headers.get("Authorization"), "Bearer SYNTHETIC_TEST_KEY_ONLY")
        serialized = json.dumps(request_data, ensure_ascii=False)
        for sentinel in ("SYNTHETIC_ANSWER_TEXT_SENTINEL", "SYNTHETIC_STUDENT_NAME_SENTINEL", "AI_IDENTITY_SENTINEL", "SYNTHETIC_TEST_KEY_ONLY"):
            self.assertNotIn(sentinel, serialized)
        self.assertEqual(result["student_identity"]["student_name"], "")
        image_url = request_data["messages"][1]["content"][1]["image_url"]["url"]
        model_image = base64.b64decode(image_url.split(",", 1)[1])
        with Image.open(io.BytesIO(model_image)) as sent_image:
            pixel = sent_image.convert("RGB").getpixel((200, 160))
            self.assertLess(max(pixel), 35)

        changed_path = temp_dir / "changed-after-preview.jpg"
        app.save_photo(self.image_data_url(marker=False), changed_path)
        photo_path.write_bytes(changed_path.read_bytes())
        with patch.object(app, "open_model_request") as changed_image_request:
            with self.assertRaisesRegex(ValueError, "教师确认的来源图"):
                app.call_vision_model(
                    assignment, sub_questions, "", [photo_path], {}, [photo_meta], True
                )
            changed_image_request.assert_not_called()

        changed_assignment = dict(assignment)
        changed_assignment["question"] = str(assignment["question"]) + "（发送前被更改）"
        with patch.object(app, "open_model_request") as changed_prompt_request:
            with self.assertRaisesRegex(ValueError, "教师确认的 AI Payload"):
                app.call_vision_model(
                    changed_assignment, sub_questions, "", [changed_path], {}, [photo_meta], True
                )
            changed_prompt_request.assert_not_called()

    def test_rubric_payload_is_previewed_identity_scanned_session_bound_and_one_time(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        csrf = session["csrf_token"]
        conn = app.get_db()
        conn.execute(
            "INSERT INTO roster_students (id, class_name, student_no, student_name, created_at) VALUES (?, ?, ?, ?, ?)",
            ("synthetic-roster-id", "合成班级", "SYNTH_NO_ALPHA_2026", "SYNTH_NAME_ALPHA", app.utc_now()),
        )
        conn.commit()
        conn.close()

        request = {
            "question": "合成历史材料分析题",
            "sub_questions": [{
                "id": "Q1", "label": "（1）", "question": "概括制度变化",
                "standard_answer": "制度逐步调整并推动社会变化。", "scoring_mode": "weighted",
                "point_value": 2, "max_score": 2,
            }],
        }
        for sensitive_text in (
            "关于 SYNTH_NAME_ALPHA 的答题情况",
            "学号：SYNTH_NO_ALPHA_2026",
            "二维码内容：SYNTH_QR_CONTENT_ALPHA",
        ):
            leaked = json.loads(json.dumps(request))
            leaked["question"] = sensitive_text
            blocked, _, _ = self.request_json(
                "POST", "/api/rubric-payload-preview", leaked, self.auth_headers(cookie, csrf)
            )
            self.assertEqual(blocked, 409)

        status, preview, _ = self.request_json(
            "POST", "/api/rubric-payload-preview", request, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["payload"]["model"], "synthetic-test-model")
        self.assertIn("概括制度变化", preview["payload"]["messages"][1]["content"])
        self.assertNotIn("SYNTHETIC_TEST_KEY_ONLY", json.dumps(preview, ensure_ascii=False))

        without_token, _, _ = self.request_json(
            "POST", "/api/generate-rubric", request, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(without_token, 400)
        altered = json.loads(json.dumps(request))
        altered["question"] = "确认后被改写的合成题干"
        altered["rubric_preview_token"] = preview["preview_token"]
        mismatch, _, _ = self.request_json(
            "POST", "/api/generate-rubric", altered, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(mismatch, 400)

        second_status, second_session, _, second_cookie = self.login(password)
        self.assertEqual(second_status, 200)
        cross_session_body = {**request, "rubric_preview_token": preview["preview_token"]}
        cross_session, _, _ = self.request_json(
            "POST", "/api/generate-rubric", cross_session_body,
            self.auth_headers(second_cookie, second_session["csrf_token"]),
        )
        self.assertEqual(cross_session, 400)

        with patch.dict(os.environ, {"HISTORY_GRADER_BASE_URL": "https://different.invalid/v1"}):
            changed_destination, _, _ = self.request_json(
                "POST", "/api/generate-rubric", cross_session_body, self.auth_headers(cookie, csrf)
            )
        self.assertEqual(changed_destination, 400)

        model_response = {"sub_questions": [{
            "sub_question_id": "Q1",
            "criteria": [{"criterion_id": "C1", "label": "制度逐步调整", "score": 2, "keywords": [], "note": ""}],
        }]}
        context = MagicMock()
        context.__enter__.return_value = object()
        with patch.object(app, "open_model_request", return_value=context) as open_request, patch.object(
            app, "read_model_chat_content", return_value=json.dumps(model_response, ensure_ascii=False)
        ):
            accepted, generated, _ = self.request_json(
                "POST", "/api/generate-rubric", cross_session_body, self.auth_headers(cookie, csrf)
            )
        self.assertEqual(accepted, 200)
        self.assertTrue(generated["sub_questions"][0]["criteria"])
        sent = json.loads(open_request.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(sent, preview["payload"])
        self.assertEqual(
            open_request.call_args.args[0].headers.get("Authorization"),
            "Bearer SYNTHETIC_TEST_KEY_ONLY",
        )
        sent_text = json.dumps(sent, ensure_ascii=False)
        for sentinel in ("SYNTH_NAME_ALPHA", "SYNTH_NO_ALPHA_2026", "SYNTH_QR_CONTENT_ALPHA", "SYNTHETIC_TEST_KEY_ONLY"):
            self.assertNotIn(sentinel, sent_text)

        repeated, _, _ = self.request_json(
            "POST", "/api/generate-rubric", cross_session_body, self.auth_headers(cookie, csrf)
        )
        self.assertEqual(repeated, 400)

    def test_signed_preview_claims_expire(self):
        expired = app._sign_ai_preview({"kind": "rubric", "expires_at": int(time.time()) - 1})
        with self.assertRaisesRegex(ValueError, "已过期"):
            app._read_preview_claims(expired)

    def test_identical_preview_claims_get_distinct_signed_tokens(self):
        claims = {"kind": "rubric", "expires_at": int(time.time()) + 300, "payload_hash": "synthetic-hash"}
        first = app._sign_ai_preview(claims)
        second = app._sign_ai_preview(claims)

        self.assertNotEqual(first, second)
        first_claims = app._read_preview_claims(first)
        second_claims = app._read_preview_claims(second)
        self.assertEqual(first_claims["payload_hash"], "synthetic-hash")
        self.assertEqual(second_claims["payload_hash"], "synthetic-hash")
        self.assertTrue(first_claims["jti"])
        self.assertTrue(second_claims["jti"])
        self.assertNotEqual(first_claims["jti"], second_claims["jti"])
        self.assertNotIn("jti", claims)
        self.assertEqual(app._ai_preview_fingerprint(first_claims), app._ai_preview_fingerprint(claims))

    def test_image_type_validation_decode_concurrency_and_worker_setting(self):
        png = Image.new("RGB", (64, 64), "white")
        output = io.BytesIO()
        png.save(output, format="PNG")
        wrong_mime = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        with self.assertRaisesRegex(ValueError, "聲明格式與實際文件格式不一致|声明格式与实际文件格式不一致"):
            app.save_photo(wrong_mime, Path(self.temp.name) / "wrong.jpg")

        data_url = self.image_data_url()
        original_validate = app.validate_image_bytes
        counter_lock = threading.Lock()
        active = 0
        maximum = 0

        def tracked_validate(raw, media_type):
            nonlocal active, maximum
            with counter_lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.01)
                return original_validate(raw, media_type)
            finally:
                with counter_lock:
                    active -= 1

        with patch.object(app, "validate_image_bytes", side_effect=tracked_validate):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(app.save_photo, data_url, Path(self.temp.name) / f"decode-{index}.jpg")
                    for index in range(4)
                ]
                for future in futures:
                    future.result()
        self.assertEqual(maximum, 1)
        # Large image requests must not overlap inside the 384MiB cloud cgroup.
        self.assertEqual(app.MAX_ACTIVE_UPLOADS, 1)
        self.assertEqual(app.MAX_HTTP_CONNECTIONS, 16)
        with patch.dict(os.environ, {"HISTORY_GRADER_WORKERS": "1", "HISTORY_GRADER_GRADE_WORKERS": "4"}):
            self.assertEqual(app.configured_grade_worker_count(), 1)
        with patch.dict(os.environ, {"HISTORY_GRADER_WORKERS": "invalid"}):
            self.assertEqual(app.configured_grade_worker_count(), 1)

    def test_retention_preview_never_deletes_files_and_migration_is_additive(self):
        assignment_id, _, _ = self.create_assignment()
        photo_data = self.image_data_url()
        raw = base64.b64decode(photo_data.split(",", 1)[1])
        photo_dir = app.DATA_DIR / assignment_id / "students" / "synthetic"
        photo_dir.mkdir(parents=True)
        original = photo_dir / "page_01.original"
        original.write_bytes(raw)
        conn = app.get_db()
        now = app.utc_now()
        photo_meta = [{
            "path": str((photo_dir / "processed.jpg").relative_to(app.PORTABLE_ROOT)).replace("\\", "/"),
            "original_path": str(original.relative_to(app.PORTABLE_ROOT)).replace("\\", "/"),
            "original_bytes": len(raw),
        }]
        conn.execute(
            "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at, state, manual_reviewed, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', 1, ?)",
            ("b" * 12, assignment_id, "合成学生", "合成答案", json.dumps(photo_meta), "{}", "2020-01-01T00:00:00+00:00", now),
        )
        conn.execute("UPDATE schema_meta SET schema_version=5 WHERE id=1")
        conn.commit()
        rebuild_legacy_submissions_table(
            conn,
            {"ai_privacy_confirmed", "ai_privacy_reviewed_at", "ai_privacy_fingerprint"},
        )
        conn.close()
        conn = app.get_db()
        row = conn.execute("SELECT student_label, ai_privacy_confirmed FROM submissions WHERE id=?", ("b" * 12,)).fetchone()
        self.assertEqual(row["student_label"], "合成学生")
        self.assertEqual(row["ai_privacy_confirmed"], 0)
        conn.close()
        report = app.retention_report(30)
        self.assertEqual(report["hypothetical_eligible_images"], 1)
        self.assertFalse(report["deletion_performed"])
        self.assertFalse(report["automatic_deletion_enabled"])
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), raw)


    def test_default_full_page_preview_matches_exact_anonymous_model_image(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        assignment_id, sub_questions, assignment = self.create_assignment("合成整页默认评分题")
        data_url = self.image_data_url(marker=False)
        preview_status, preview, _ = self.request_json(
            "POST", "/api/ai-payload-preview",
            {
                "assignment_id": assignment_id,
                "class_name": "SYNTHETIC_CLASS_SENTINEL",
                "student_label": "SYNTHETIC_MARKING_CODE_SENTINEL",
                "photos": [{"data_url": data_url, "ai_redactions": [], "corners": None}],
                "answer_regions": [],
            },
            self.auth_headers(cookie, session["csrf_token"]),
        )
        self.assertEqual(preview_status, 200)
        self.assertEqual(preview["image_count"], 1)
        self.assertEqual(preview["images"][0]["image_kind"], "full_page")
        self.assertNotIn("SYNTHETIC_CLASS_SENTINEL", preview["prompt"])
        self.assertNotIn("SYNTHETIC_MARKING_CODE_SENTINEL", preview["prompt"])
        preview_image = app.data_url_bytes(preview["images"][0]["data_url"], require_image=True)

        photo_path = Path(self.temp.name) / "full-page-default.jpg"
        photo_meta = app.save_photo(data_url, photo_path)
        photo_meta.update({
            "ai_redactions": [],
            "approved_ai_page_sha256": preview["processed_pages"][0]["sha256"],
            "approved_prompt_sha256": hashlib.sha256(
                app.build_vision_prompt_text(
                    str(assignment["question"]), sub_questions, privacy_mode=True
                ).encode("utf-8")
            ).hexdigest(),
        })
        model_response = {
            "confidence": 0.8,
            "summary": "合成整页评分",
            "sub_questions": [{
                "sub_question_id": sub_questions[0]["id"],
                "criteria": [{
                    "criterion_id": sub_questions[0]["criteria"][0]["id"],
                    "suggested_score": 2,
                    "decision": "full",
                    "student_evidence": "合成证据",
                    "reason": "符合模拟评分点",
                    "needs_review": False,
                    "evidence": {"photo_index": 1, "x": 0.3, "y": 0.4, "width": 0.2, "height": 0.1},
                }],
            }],
        }
        request_context = MagicMock()
        request_context.__enter__.return_value = object()
        with patch.object(app, "open_model_request", return_value=request_context) as model_request, patch.object(
            app, "read_model_chat_content", return_value=json.dumps(model_response, ensure_ascii=False)
        ):
            result = app.call_vision_model(
                assignment, sub_questions, "SYNTHETIC_TYPED_ANSWER_SENTINEL",
                [photo_path], {}, [photo_meta], True, [], True,
            )

        self.assertEqual(
            result["sub_questions"][0]["criteria"][0]["evidence"]["coordinate_system"],
            "perspective_corrected_page_normalized",
        )
        self.assertEqual(result["sub_questions"][0]["criteria"][0]["evidence"]["source"], "full_page")
        self.assertEqual(model_request.call_count, 1)
        outbound = json.loads(model_request.call_args.args[0].data.decode("utf-8"))
        serialized = json.dumps(outbound, ensure_ascii=False)
        for sentinel in (
            "SYNTHETIC_CLASS_SENTINEL", "SYNTHETIC_MARKING_CODE_SENTINEL",
            "SYNTHETIC_TYPED_ANSWER_SENTINEL", "SYNTHETIC_TEST_KEY_ONLY",
        ):
            self.assertNotIn(sentinel, serialized)
        outbound_images = [
            app.base64.b64decode(item["image_url"]["url"].split(",", 1)[1])
            for item in outbound["messages"][1]["content"] if item.get("type") == "image_url"
        ]
        self.assertEqual(outbound_images, [preview_image])
        self.assertEqual(app.normalize_answer_regions([], sub_questions, 1), [])
        with self.assertRaises(ValueError):
            app.normalize_answer_regions(
                [{"photo_index": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2, "sub_question_ids": []}],
                sub_questions, 1,
            )

        flow = (Path(__file__).resolve().parents[1] / "static" / "privacy-flow.js").read_text(encoding="utf-8")
        self.assertIn("toolMode = editingPerspective ? 'corners' : 'none'", flow)
        self.assertIn("forcePerspectivePages: Array.from({ length: pageCount }", flow)
        self.assertIn("image.image_kind === 'full_page'", flow)
        self.assertNotIn("if (templatesByPageCount.get(count)) continue;", flow)


    def test_mobile_dual_full_page_upload_uses_desktop_pair_without_primary_model(self):
        token = "SYNTHETIC_FULL_PAGE_MOBILE_TOKEN"
        app.MOBILE_TOKEN_HASH = hashlib.sha256(token.encode("utf-8")).hexdigest()
        app.MOBILE_TOKEN_EXPIRES_AT = time.time() + 300
        assignment_id, _, _ = self.create_assignment("手机整页默认批改合成题")
        data_url = self.image_data_url(marker=True)
        headers = {
            "Authorization": "Bearer " + token,
            "Origin": self.origin,
        }
        dual_specs = {
            "A": {
                "provider": "qwen", "provider_label": "通义千问", "model": "qwen3.8-max",
                "api_origin": "https://dashscope.aliyuncs.com", "base_url": "https://dashscope.aliyuncs.com/v1",
                "preset_id": "qwen38-max", "label": "Qwen3.8-Max",
            },
            "B": {
                "provider": "deepseek", "provider_label": "DeepSeek", "model": "deepseek-flash",
                "api_origin": "https://api.deepseek.com", "base_url": "https://api.deepseek.com/v1",
                "preset_id": "deepseek-v41-flash", "label": "DeepSeek V4.1 Flash",
            },
        }
        with patch.object(app.AppHandler, "is_local_client", return_value=False), patch.object(
            app, "model_configured", return_value=False
        ), patch.object(app, "dual_review_model_specs", return_value=dual_specs), patch.object(
            app, "dual_review_key_is_configured", return_value=True
        ), patch.object(app, "enqueue_submission") as enqueue:
            preview_status, preview, _ = self.request_json(
                "POST", "/api/ai-payload-preview",
                {
                    "assignment_id": assignment_id,
                    "class_name": "",
                    "student_label": "SYNTHETIC_MOBILE_MARKING_CODE",
                    "dual_review_enabled": True,
                    "photos": [{"data_url": data_url, "ai_redactions": [], "corners": None}],
                    # Mobile flow intentionally omits answer_regions.
                },
                headers,
            )
            self.assertEqual(preview_status, 200, preview)
            self.assertEqual(preview["images"][0]["image_kind"], "full_page")
            self.assertEqual(preview["model"], "qwen3.8-max + deepseek-flash")
            self.assertEqual(preview["provider"], "通义千问 + DeepSeek")
            upload_status, upload, _ = self.request_json(
                "POST", "/api/submissions",
                {
                    "assignment_id": assignment_id,
                    "class_name": "",
                    "student_label": "SYNTHETIC_MOBILE_MARKING_CODE",
                    "dual_review_enabled": True,
                    "ai_privacy_confirmed": True,
                    "privacy_status": "pre_anonymized",
                    "ai_privacy_preview_token": preview["preview_token"],
                    "ai_privacy_fingerprint": preview["preview_fingerprint"],
                    "photos": [{
                        "name": "SYNTHETIC_MOBILE_PAGE.jpg",
                        "page_sequence": 1,
                        "data_url": data_url,
                        "original_data_url": data_url,
                        "corners": None,
                        "ai_redactions": [],
                    }],
                    # No answer_regions is sent by the phone upload.
                },
                headers,
            )
        self.assertEqual(upload_status, 202, upload)
        enqueue.assert_called_once_with(upload["submission"]["id"])
        conn = app.get_db()
        row = conn.execute(
            "SELECT source_channel, answer_regions_json, result_json, ai_privacy_confirmed FROM submissions WHERE id=?",
            (upload["submission"]["id"],),
        ).fetchone()
        conn.close()
        self.assertEqual(row["source_channel"], "mobile")
        self.assertEqual(json.loads(row["answer_regions_json"]), [])
        self.assertTrue(json.loads(row["result_json"])["dual_review_requested"])
        self.assertEqual(row["ai_privacy_confirmed"], 1)

        static_app = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        static_privacy = (Path(__file__).resolve().parents[1] / "static" / "privacy-flow.js").read_text(encoding="utf-8")
        self.assertIn("/api/mobile/dual-review-readiness", static_app)
        self.assertIn("const templatesByPageCount = mobileClient", static_privacy)


    def test_questions_17_18_full_page_batch_unmatched_remap_and_five_independent_scores(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        headers = self.auth_headers(cookie, session["csrf_token"])

        assignment_id, _, _ = self.create_assignment(
            "第17、18题合成正式批改验收",
        )
        overall_question = (
            "17政治民主化：中国古代丞相变化、雅典公民大会、美国1787年宪法妥协。"
            "18抗战时期陕甘宁边区开展森林保护的材料。两道大题共五道小题。"
        )
        raw_sub_questions = [
            {
                "id": "Q17-1", "label": "17(1)",
                "question": "根据材料一并结合所学，指出秦、唐、明三代从独相到群相再到兼相的制度表现。",
                "standard_answer": "秦设丞相；唐三省分权，群相议政；明废丞相。",
                "scoring_mode": "weighted", "criteria": [{"id": "C17-1", "label": "秦唐明制度演变", "score": 2}],
            },
            {
                "id": "Q17-2", "label": "17(2)",
                "question": "根据材料二概括雅典民主政治的基本特征。",
                "standard_answer": "公民直接参与，轮番而治，公民大会处理重大事务。",
                "scoring_mode": "weighted", "criteria": [{"id": "C17-2", "label": "直接民主与轮番而治", "score": 2}],
            },
            {
                "id": "Q17-3", "label": "17(3)",
                "question": "根据材料三并结合1787年宪法，指出妥协调和美国国内矛盾的表现。",
                "standard_answer": "大州小州、南北方及联邦与州之间通过宪法机制妥协。",
                "scoring_mode": "weighted", "criteria": [
                    {"id": "C17-3A", "label": "大小州代表权妥协", "score": 2},
                    {"id": "C17-3B", "label": "南北方或联邦州权妥协", "score": 2},
                ],
            },
            {
                "id": "Q18-1", "label": "18(1)",
                "question": "根据森林保护材料，概括陕甘宁边区政府推动森林保护的措施。",
                "standard_answer": "植树造林、法规保护、设林务机构、区划管理、调查和培养技术人员。",
                "scoring_mode": "weighted", "criteria": [{"id": "C18-1", "label": "森林保护措施", "score": 2}],
            },
            {
                "id": "Q18-2", "label": "18(2)",
                "question": "根据材料并结合所学，评价陕甘宁边区的森林保护实践。",
                "standard_answer": "制度化保护和生产并举，有成效但受战争和认识局限。",
                "scoring_mode": "weighted", "criteria": [{"id": "C18-2", "label": "历史评价", "score": 2}],
            },
        ]
        sub_questions = app.clean_sub_questions(raw_sub_questions)
        conn = app.get_db()
        conn.execute(
            "UPDATE assignments SET question=?, sub_questions_json=?, criteria_json=? WHERE id=?",
            (
                overall_question,
                json.dumps(sub_questions, ensure_ascii=False),
                json.dumps(app.flatten_criteria(sub_questions), ensure_ascii=False),
                assignment_id,
            ),
        )
        conn.commit()
        conn.close()

        batch_status, batch, _ = self.request_json(
            "POST", "/api/batches",
            {
                "assignment_id": assignment_id,
                "class_name": "",
                "batch_request_id": "synthetic-q17-q18-full-page-batch",
                "items": [{"student_label": "合成答卷甲", "page_count": 2}, {"student_label": "合成答卷乙", "page_count": 2}],
            },
            headers,
        )
        self.assertEqual(batch_status, 201, batch)
        first = batch["items"][0]
        marking_code = first["marking_code"]
        page_one = self.image_data_url((1200, 1600), marker=True)
        page_two = self.image_data_url((1200, 1600), marker=True)
        photos_for_preview = [
            {"data_url": page_one, "corners": None, "ai_redactions": []},
            {"data_url": page_two, "corners": None, "ai_redactions": []},
        ]
        preview_status, preview, _ = self.request_json(
            "POST", "/api/ai-payload-preview",
            {
                "assignment_id": assignment_id,
                "class_name": "",
                "student_label": marking_code,
                "photos": photos_for_preview,
                # Deliberately no answer_regions: same-page and cross-page
                # question mapping is delegated to the five flat rubric rows.
            },
            headers,
        )
        self.assertEqual(preview_status, 200, preview)
        self.assertEqual([image["image_kind"] for image in preview["images"]], ["full_page", "full_page"])
        self.assertEqual([item["label"] for item in sub_questions], ["17(1)", "17(2)", "17(3)", "18(1)", "18(2)"])
        for item in sub_questions:
            self.assertIn(item["label"], preview["prompt"])
            self.assertIn(item["question"], preview["prompt"])
            self.assertIn(item["criteria"][0]["label"], preview["prompt"])
        self.assertIn("材料一", preview["prompt"])
        self.assertIn("森林保护", preview["prompt"])
        self.assertNotIn(marking_code, preview["prompt"])

        upload_payload = {
            "assignment_id": assignment_id,
            "class_name": "",
            "student_label": marking_code,
            "answer_text": "",
            "batch_id": batch["batch_id"],
            "sequence": first["sequence"],
            "submission_id": first["submission_id"],
            "idempotency_key": "synthetic-q17-q18-first-upload",
            "ai_privacy_confirmed": True,
            "privacy_status": "pre_anonymized",
            "ai_privacy_preview_token": preview["preview_token"],
            "ai_privacy_fingerprint": preview["preview_fingerprint"],
            "photos": [
                {
                    "name": f"SYNTHETIC_PRIVATE_PAGE_{index}.jpg",
                    "page_sequence": index,
                    "data_url": data_url,
                    "original_data_url": data_url,
                    "corners": None,
                    "ai_redactions": [],
                }
                for index, data_url in enumerate((page_one, page_two), start=1)
            ],
        }
        with patch.object(app, "enqueue_submission") as enqueue:
            upload_status, uploaded, _ = self.request_json("POST", "/api/submissions", upload_payload, headers)
        self.assertEqual(upload_status, 202, uploaded)
        enqueue.assert_called_once_with(first["submission_id"])
        self.assertTrue(uploaded["submission"]["marking_code_id"])
        self.assertEqual(len(uploaded["submission"]["photos"]), 2)

        def scoring_response(*, include_q18_1):
            values = [
                ("Q17-1", [("C17-1", 2, "full", 1)]),
                ("Q17-2", [("C17-2", 1, "partial", 1)]),
                # The answer to 17(3) spans both pages. Other questions also
                # share those pages, so page number alone cannot choose a rubric.
                ("Q17-3", [("C17-3A", 2, "full", 1), ("C17-3B", 1, "partial", 2)]),
            ]
            if include_q18_1:
                values.append(("Q18-1", [("C18-1", 1, "partial", 2)]))
            values.append(("Q18-2", [("C18-2", 2, "full", 2)]))
            rows = []
            for question_id, criteria in values:
                rows.append({
                    "sub_question_id": question_id,
                    "answer_status": "answered",
                    "criteria": [
                        {
                            "criterion_id": criterion_id,
                            "suggested_score": score,
                            "decision": decision,
                            "student_evidence": "合成学生作答证据",
                            "reason": "合成评分理由",
                            "needs_review": False,
                            "evidence": {
                                "photo_index": photo_index,
                                "x": .20, "y": .25, "width": .18, "height": .09,
                            },
                        }
                        for criterion_id, score, decision, photo_index in criteria
                    ],
                })
            unmatched = [{
                "answer_id": "A1",
                "evidence": {"photo_index": 2, "x": .62, "y": .42, "width": .20, "height": .10},
            }] if not include_q18_1 else []
            return {"confidence": .9, "summary": "合成第17、18题评分", "sub_questions": rows, "unmapped_answers": unmatched}

        first_response = scoring_response(include_q18_1=False)
        first_context = MagicMock()
        first_context.__enter__.return_value = object()
        with patch.object(app, "model_configuration_is_synthetic", return_value=False), patch.object(
            app, "open_model_request", return_value=first_context
        ) as first_model_call, patch.object(
            app, "read_model_chat_content", return_value=json.dumps(first_response, ensure_ascii=False)
        ):
            app.process_submission(first["submission_id"])
        first_model_call.assert_called_once()
        conn = app.get_db()
        submitted = conn.execute("SELECT * FROM submissions WHERE id=?", (first["submission_id"],)).fetchone()
        paused = conn.execute("SELECT status, next_sequence, pause_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
        first_result = json.loads(submitted["result_json"])
        conn.close()
        self.assertEqual(submitted["state"], "completed")
        self.assertTrue(first_result["blocked"])
        self.assertEqual(first_result["unmapped_answers"][0]["photo_index"], 2)
        self.assertEqual(next(item["answer_status"] for item in first_result["sub_questions"] if item["label"] == "18(1)"), "question_unmatched")
        self.assertEqual((paused["status"], paused["next_sequence"], paused["pause_sequence"]), ("paused", 1, 1))
        conn = app.get_db()
        self.assertFalse(app._batch_sequence_is_runnable(conn, batch["batch_id"], 2))
        conn.close()
        rejected_confirm, _, _ = self.request_json(
            "POST", "/api/confirm", {"submission_id": first["submission_id"], "final_total": 8}, headers
        )
        self.assertEqual(rejected_confirm, 400)

        mapping = [{"answer_id": "A1", "sub_question_id": "Q18-1"}]
        remap_preview_status, remap_preview, _ = self.request_json(
            "POST", "/api/question-remap-preview",
            {"submission_id": first["submission_id"], "question_mapping": mapping}, headers,
        )
        self.assertEqual(remap_preview_status, 200, remap_preview)
        self.assertEqual([item["sha256"] for item in remap_preview["images"]], [item["sha256"] for item in preview["images"]])
        self.assertIn("Q18-1", remap_preview["prompt"])
        self.assertIn("18(1)", remap_preview["prompt"])
        self.assertNotIn(marking_code, remap_preview["prompt"])
        self.assertNotIn("SYNTHETIC_PRIVATE_PAGE", json.dumps(remap_preview))
        for shown, originally_shown in zip(remap_preview["images"], preview["images"]):
            self.assertEqual(
                app.data_url_bytes(shown["data_url"], require_image=True),
                app.data_url_bytes(originally_shown["data_url"], require_image=True),
            )

        wrong_mapping = [{"answer_id": "A1", "sub_question_id": "Q18-2"}]
        wrong_status, _, _ = self.request_json(
            "POST", "/api/reprocess-question-mapping",
            {
                "submission_id": first["submission_id"], "question_mapping": wrong_mapping,
                "ai_privacy_confirmed": True, "ai_privacy_preview_token": remap_preview["preview_token"],
                "ai_privacy_fingerprint": remap_preview["preview_fingerprint"],
            }, headers,
        )
        self.assertEqual(wrong_status, 400)

        reprocess_payload = {
            "submission_id": first["submission_id"],
            "question_mapping": mapping,
            "ai_privacy_confirmed": True,
            "ai_privacy_preview_token": remap_preview["preview_token"],
            "ai_privacy_fingerprint": remap_preview["preview_fingerprint"],
        }
        with patch.object(app, "enqueue_submission") as reenqueue:
            reprocess_status, reprocess_body, _ = self.request_json(
                "POST", "/api/reprocess-question-mapping", reprocess_payload, headers
            )
        self.assertEqual(reprocess_status, 202, reprocess_body)
        reenqueue.assert_called_once_with(first["submission_id"])

        final_response = scoring_response(include_q18_1=True)
        final_context = MagicMock()
        final_context.__enter__.return_value = object()
        with patch.object(app, "model_configuration_is_synthetic", return_value=False), patch.object(
            app, "open_model_request", return_value=final_context
        ) as final_model_call, patch.object(
            app, "read_model_chat_content", return_value=json.dumps(final_response, ensure_ascii=False)
        ):
            app.process_submission(first["submission_id"])
        final_model_call.assert_called_once()
        outbound = json.loads(final_model_call.call_args.args[0].data.decode("utf-8"))
        outbound_text = json.dumps(outbound, ensure_ascii=False)
        self.assertIn("Q18-1", outbound_text)
        self.assertIn("answer_id", outbound_text)
        self.assertNotIn(marking_code, outbound_text)
        self.assertNotIn("SYNTHETIC_PRIVATE_PAGE", outbound_text)
        sent_images = [
            app.base64.b64decode(item["image_url"]["url"].split(",", 1)[1])
            for item in outbound["messages"][1]["content"] if item.get("type") == "image_url"
        ]
        self.assertEqual(
            sent_images,
            [app.data_url_bytes(item["data_url"], require_image=True) for item in remap_preview["images"]],
        )
        conn = app.get_db()
        final_row = conn.execute("SELECT state, result_json FROM submissions WHERE id=?", (first["submission_id"],)).fetchone()
        final_result = json.loads(final_row["result_json"])
        batch_state = conn.execute("SELECT status, next_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
        conn.close()
        self.assertEqual(final_row["state"], "completed")
        self.assertFalse(final_result["blocked"])
        self.assertEqual([item["label"] for item in final_result["sub_questions"]], ["17(1)", "17(2)", "17(3)", "18(1)", "18(2)"])
        self.assertEqual([item["suggested_total"] for item in final_result["sub_questions"]], [2, 1, 3, 1, 2])
        self.assertEqual(final_result["suggested_total"], 9)
        cross_page = next(item for item in final_result["sub_questions"] if item["label"] == "17(3)")
        self.assertEqual({criterion["evidence"]["photo_index"] for criterion in cross_page["criteria"]}, {1, 2})
        self.assertTrue(all(criterion["evidence"]["source"] == "full_page" for criterion in cross_page["criteria"]))
        self.assertEqual((batch_state["status"], batch_state["next_sequence"]), ("active", 2))
        conn = app.get_db()
        self.assertTrue(app._batch_sequence_is_runnable(conn, batch["batch_id"], 2))
        result_row = conn.execute("SELECT result_json FROM submissions WHERE id=?", (first["submission_id"],)).fetchone()
        synthetic_result = json.loads(result_row["result_json"])
        # Keep the version-bound rubric hash when replacing the live dual review
        # with the synthetic teacher-final placeholder; the new confirm gate
        # validates this hash against the recorded content evidence.
        recorded_rubric_hash = (synthetic_result.get("dual_review") or {}).get("rubric_hash") or (
            synthetic_result.get("subjective_version_evidence") or {}
        ).get("rubric_hash")
        synthetic_result["dual_review"] = {
            "teacher_final": None,
            "rubric_hash": recorded_rubric_hash,
        }
        conn.execute(
            "UPDATE submissions SET result_json=? WHERE id=?",
            (json.dumps(synthetic_result, ensure_ascii=False), first["submission_id"]),
        )
        conn.commit()
        conn.close()

        confirmed, confirmation, _ = self.request_json(
            "POST", "/api/confirm",
            {"submission_id": first["submission_id"], "teacher_note": "合成验收确认",
             "teacher_point_decisions": [
                 {"question_id": q["id"], "rubric_point_id": c["id"],
                  "choice": "teacher_none" if q["id"] in {"Q17-2", "Q18-1"} else "teacher_full"}
                 for q in sub_questions for c in q["criteria"]
             ]},
            headers,
        )
        self.assertEqual(confirmed, 200, confirmation)
        confirmed_submission_status, confirmed_submission, _ = self.request_json(
            "GET", f"/api/submission?submission_id={first['submission_id']}", headers=headers
        )
        self.assertEqual(confirmed_submission_status, 200)
        self.assertEqual(confirmed_submission["submission"]["final_total"], 8)
        teacher_final = confirmed_submission["submission"]["result"]["dual_review"]["teacher_final"]
        self.assertEqual(teacher_final["review_provenance"]["app_version"], app.APP_VERSION)
        self.assertEqual(teacher_final["review_provenance"]["data_schema_version"], app.TEACHER_REVIEW_SCHEMA_VERSION)
        self.assertIn(teacher_final["review_provenance"]["environment"], {"portable_local", "online_beta"})


    def test_teacher_must_explicitly_confirm_blank_candidate_before_zero_can_be_saved(self):
        password = self.configure_password()
        web_auth.AUTH_MANAGER = web_auth.SingleTeacherAuth()
        login_status, session, _, cookie = self.login(password)
        self.assertEqual(login_status, 200)
        headers = self.auth_headers(cookie, session["csrf_token"])
        assignment_id, sub_questions, _ = self.create_assignment("合成空白候选确认")
        pending = app.normalize_sub_question_result({
            "sub_questions": [{
                "sub_question_id": "Q1", "answer_status": "unanswered_candidate", "criteria": [],
            }],
        }, sub_questions)
        submission_id = "c" * 12
        conn = app.get_db()
        conn.execute(
            "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at, state, source_channel, manual_reviewed) "
            "VALUES (?, ?, ?, '', '[]', ?, ?, 'completed', 'web', 1)",
            (submission_id, assignment_id, "SYNTHETIC_MARKING_CODE", json.dumps(pending, ensure_ascii=False), app.utc_now()),
        )
        conn.commit()
        conn.close()
        blocked, _, _ = self.request_json(
            "POST", "/api/confirm", {"submission_id": submission_id, "final_total": 0}, headers
        )
        self.assertEqual(blocked, 400)
        self.assertIn("未确认空白", self.request_json(
            "POST", "/api/confirm", {"submission_id": submission_id, "final_total": 0}, headers
        )[1]["error"])

        unreadable = app.normalize_sub_question_result({
            "sub_questions": [{
                "sub_question_id": "Q1", "answer_status": "image_unreadable", "criteria": [],
            }],
        }, sub_questions)
        unreadable_id = "d" * 12
        conn = app.get_db()
        conn.execute(
            "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at, state, source_channel, manual_reviewed) "
            "VALUES (?, ?, ?, '', '[]', ?, ?, 'completed', 'web', 1)",
            (unreadable_id, assignment_id, "SYNTHETIC_UNREADABLE_CODE", json.dumps(unreadable, ensure_ascii=False), app.utc_now()),
        )
        conn.commit()
        conn.close()
        unreadable_status, unreadable_body, _ = self.request_json(
            "POST", "/api/confirm", {"submission_id": unreadable_id, "final_total": 0}, headers
        )
        self.assertEqual(unreadable_status, 400)
        self.assertIn("图像无法读取", unreadable_body["error"])

        blank_status, blank_body, _ = self.request_json(
            "POST", "/api/confirm-blank-subquestion",
            {"submission_id": submission_id, "sub_question_id": "Q1"}, headers,
        )
        self.assertEqual(blank_status, 200, blank_body)
        result_status, result_body, _ = self.request_json(
            "GET", f"/api/submission?submission_id={submission_id}", headers=headers
        )
        self.assertEqual(result_status, 200)
        self.assertEqual(result_body["submission"]["result"]["sub_questions"][0]["answer_status"], "blank_confirmed")
        self.assertEqual(result_body["submission"]["result"]["suggested_total"], 0)
        self.assertFalse(result_body["submission"]["result"]["blocked"])
        confirmed_status, confirmed_body, _ = self.request_json(
            "POST", "/api/confirm", {"submission_id": submission_id, "teacher_point_decisions": [
                {"question_id": q["id"], "rubric_point_id": c["id"], "choice": "teacher_none"}
                for q in sub_questions for c in q["criteria"]
            ]}, headers
        )
        self.assertEqual(confirmed_status, 200, confirmed_body)






if __name__ == "__main__":
    unittest.main()
