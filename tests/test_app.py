import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

import app


def diagnostic_test_sub_questions():
    return app.clean_sub_questions([
        {
            "id": f"Q{index}",
            "label": f"（{index}）",
            "question": f"测试小题 {index}",
            "standard_answer": "测试参考答案",
            "criteria": [{"id": "C1", "label": "测试评分点", "score": 2}],
        }
        for index in range(1, 6)
    ])


def diagnostic_test_response(question_ids=("Q1", "Q2", "Q3", "Q4", "Q5"), id_field="sub_question_id"):
    return {
        "sub_questions": [
            {
                id_field: question_id,
                "answer_status": "answered",
                "criteria": [{"criterion_id": "C1", "suggested_score": 0, "decision": "none"}],
            }
            for question_id in question_ids
        ],
        "unmapped_answers": [],
    }


class HistoryGraderTests(unittest.TestCase):
    def test_dpapi_key_read_uses_utf8_output_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            encrypted_file = Path(directory) / "encrypted-key.dpapi"
            encrypted_file.write_text("synthetic encrypted content", encoding="ascii")
            completed = SimpleNamespace(returncode=0, stdout="\ufeffsynthetic-test-key")
            with patch.object(app, "api_key_path", return_value=encrypted_file), patch.object(
                app, "powershell_command", return_value="pwsh.exe"
            ), patch.object(
                app, "read_api_key_binding",
                return_value={"portable_instance_id": "test-instance", "api_origin": "https://example.invalid"},
            ), patch.object(app, "ensure_portable_instance_id", return_value="test-instance"), patch.object(
                app.subprocess, "run", return_value=completed
            ) as run:
                key = app.read_dpapi_api_key("qwen", expected_origin="https://example.invalid")
            self.assertEqual(key, "synthetic-test-key")
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8-sig")
            self.assertIn("UTF8Encoding", run.call_args.args[0][-1])

    def test_deepseek_json_calls_disable_thinking_mode(self):
        with patch.dict(
            app.os.environ,
            {"HISTORY_GRADER_PROVIDER": "deepseek", "HISTORY_GRADER_MODEL": "deepseek-flash"},
            clear=True,
        ):
            self.assertEqual(app.deepseek_json_payload_options(), {"thinking": {"type": "disabled"}})

        with patch.dict(
            app.os.environ,
            {"HISTORY_GRADER_PROVIDER": "qwen", "HISTORY_GRADER_MODEL": "qwen-vl-max"},
            clear=True,
        ):
            self.assertEqual(app.deepseek_json_payload_options(), {})

        self.assertEqual(
            app.deepseek_json_payload_options("mimo", "mimo-v2.6-flash"),
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(
            app.deepseek_json_payload_options("qwen", "qwen3.8-max"),
            {"reasoning_effort": "low"},
        )

    def test_default_dual_review_pair_is_deepseek_and_mimo_v26(self):
        self.assertEqual(
            app.DUAL_REVIEW_DEFAULT_PRESETS,
            {"A": "deepseek-v41-flash", "B": "mimo-v26-flash"},
        )
        specs = app.dual_review_model_specs(dict(app.DUAL_REVIEW_DEFAULT_PRESETS))
        self.assertEqual((specs["A"]["model"], specs["B"]["model"]), ("deepseek-flash", "mimo-v2.6-flash"))
        self.assertTrue(app.MODEL_PRESETS["qwen38-max"]["experimental"])

    def test_mimo_vision_request_uses_completion_limit_and_schema_validation(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "question": "识别图中字母",
            "standard_answer": "A", "criteria": [{"id": "C1", "label": "写出A", "score": 1}],
        }])
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "synthetic-answer.jpg"
            Image.new("RGB", (320, 200), "white").save(image_path, format="JPEG")
            response_json = json.dumps({
                "sub_questions": [{
                    "sub_question_id": "Q1",
                    "criteria": [{
                        "criterion_id": "C1", "suggested_score": 1, "decision": "full",
                        "student_evidence": "A", "reason": "图中有A",
                        "evidence": {"photo_index": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.3},
                    }],
                }],
            })
            context = MagicMock()
            context.__enter__.return_value = object()
            diagnostics = {}
            with patch.object(app, "open_model_request", return_value=context) as open_request, patch.object(
                app, "read_model_chat_content", return_value=response_json
            ), patch.object(app, "preflight_private_model_diagnostic_dir", return_value=None), patch.object(
                app, "cloud_mode_enabled", return_value=False
            ):
                result = app.call_vision_model(
                    {"id": "synthetic", "question": "识别图中字母"},
                    sub_questions, "", [image_path], diagnostics, [], False,
                    model_config_override={
                        "provider": "mimo", "model": "mimo-v2.6-flash",
                        "base_url": "https://api.xiaomimimo.com/v1", "api_key": "SYNTHETIC_MIMO_KEY",
                    },
                )
            payload = json.loads(open_request.call_args.args[0].data.decode("utf-8"))
            self.assertEqual(diagnostics["request_metadata"]["invocation_scope"], "whole_paper")
            self.assertEqual(diagnostics["request_metadata"]["output_limit_type"], "max_completion_tokens")
            self.assertEqual(diagnostics["request_metadata"]["image_count"], 1)
            self.assertEqual(diagnostics["schema_validation"], "passed")

            with patch.object(app, "open_model_request", return_value=context) as qwen_request, patch.object(
                app, "read_model_chat_content", return_value=response_json
            ):
                qwen_diagnostics = {}
                app.call_vision_model(
                    {"id": "synthetic", "question": "识别图中字母"},
                    sub_questions, "", [image_path], qwen_diagnostics, [], False,
                    model_config_override={
                        "provider": "qwen", "model": "qwen3.8-max",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "SYNTHETIC_QWEN_KEY",
                    },
                    invocation_scope="single_question",
                )
                qwen_payload = json.loads(qwen_request.call_args.args[0].data.decode("utf-8"))
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(result["sub_questions"][0]["suggested_total"], 1.0)
        self.assertEqual(result["sub_questions"][0]["criteria"][0]["student_evidence"], "A")
        self.assertIn("max_completion_tokens", qwen_payload)
        self.assertNotIn("max_tokens", qwen_payload)
        self.assertEqual(qwen_payload["reasoning_effort"], "low")
        self.assertEqual(qwen_diagnostics["request_metadata"]["invocation_scope"], "single_question")

    def test_pydantic_grading_schema_rejects_malformed_decision_shape(self):
        valid = diagnostic_test_response(("Q1",))
        parser_output, normalized = app.parse_vision_model_output(json.dumps(valid), diagnostic_test_sub_questions()[:1], 1)
        self.assertEqual(parser_output["sub_questions"][0]["sub_question_id"], "Q1")
        self.assertEqual(normalized["sub_questions"][0]["answer_status"], "answered")
        invalid = {"sub_questions": [{
            "sub_question_id": "Q1",
            "criteria": [{"criterion_id": "C1", "decision": ["full"], "suggested_score": 1}],
        }]}
        with self.assertRaises(Exception):
            app.parse_vision_model_output(json.dumps(invalid), diagnostic_test_sub_questions()[:1], 1)

    def test_http_response_ignores_client_disconnect(self):
        class BrokenWriter:
            def write(self, body):
                raise ConnectionAbortedError()

        class FakeHandler:
            wfile = BrokenWriter()

            def send_response(self, status):
                pass

            def send_header(self, name, value):
                pass

            def end_headers(self):
                pass

        self.assertFalse(app.write_http_response(FakeHandler(), b"{}", "application/json"))

    def test_network_kind_identifies_usb_tethering_without_hardware_details(self):
        self.assertEqual(app._network_kind("Ethernet 3", "Remote NDIS Compatible Device"), "usb")
        self.assertEqual(app._network_kind("WLAN", "Intel Wi-Fi adapter"), "wifi")
        self.assertEqual(app._network_kind("以太网", "有线网卡"), "ethernet")

    def test_network_interfaces_prioritize_usb_and_keep_legacy_addresses(self):
        records = [
            {"address": "192.168.1.3", "interface_name": "Ethernet", "connection_type": "ethernet", "connection_label": "网线"},
            {"address": "192.168.42.129", "interface_name": "Ethernet 3", "connection_type": "usb", "connection_label": "USB 数据线"},
            {"address": "192.168.50.8", "interface_name": "Wi-Fi", "connection_type": "wifi", "connection_label": "Wi‑Fi"},
        ]
        with patch.object(app, "_powershell_network_interfaces", return_value=records):
            interfaces = app.local_network_interfaces()
            self.assertEqual([item["address"] for item in interfaces], ["192.168.42.129", "192.168.50.8", "192.168.1.3"])
            self.assertTrue(interfaces[0]["recommended"])
            self.assertFalse(interfaces[1]["recommended"])
            self.assertEqual(
                [item["address"] for item in interfaces],
                app.local_network_ipv4_addresses(),
            )

    def test_model_presets_have_openai_compatible_vision_defaults(self):
        for preset_id in (
            "deepseek-v41-flash", "deepseek-v4-vision", "mimo-v25", "mimo-v26-flash", "qwen35-flash",
            "qwen-vl-max", "glm-46v", "openai-gpt-41", "openai-gpt-41-mini",
            "gemini-38-flash", "qwen38-max",
        ):
            preset = app.MODEL_PRESETS[preset_id]
            self.assertTrue(preset["base_url"].startswith(("http://", "https://")))
            self.assertTrue(preset["model"])

    def test_mainstream_vision_presets_use_supported_chat_completion_ids(self):
        self.assertEqual(app.MODEL_PRESETS["openai-gpt-41"]["model"], "gpt-4.1")
        self.assertEqual(app.MODEL_PRESETS["openai-gpt-41-mini"]["model"], "gpt-4.1-mini")
        self.assertEqual(app.MODEL_PRESETS["gemini-38-flash"]["model"], "gemini-3.8-flash")
        self.assertEqual(
            app.MODEL_PRESETS["gemini-38-flash"]["base_url"],
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )

    def test_deepseek_v41_flash_uses_official_multimodal_model_name(self):
        preset = app.MODEL_PRESETS["deepseek-v41-flash"]
        self.assertEqual(preset["model"], "deepseek-flash")
        self.assertFalse(preset["experimental"])
        self.assertIn("正式", preset["label"])

    def test_clean_criteria_and_rule_score(self):
        criteria = app.clean_criteria([
            {"label": "原因", "score": 2, "keywords": ["原因"]},
            {"label": "影响", "score": 1, "keywords": ["影响"]},
        ])
        result = app.score_by_rules(criteria, "材料说明了原因，但没有写出影响")
        self.assertEqual(result["suggested_total"], 3.0)
        self.assertEqual(len(result["criteria"]), 2)
        self.assertTrue(result["needs_review"])

    def test_save_photo_resizes_and_writes_jpeg(self):
        image = Image.new("RGB", (4000, 2000), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "photo.jpg"
            meta = app.save_photo(data_url, target)
            self.assertTrue(target.exists())
            self.assertEqual(meta["width"], 3000)
            self.assertEqual(meta["height"], 1500)
            self.assertIn("quality", meta)

    def test_model_image_is_smaller_in_memory_and_does_not_change_source(self):
        image = Image.new("RGB", (4000, 2000), "white")
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "processed.jpg"
            image.save(source, format="JPEG", quality=95)
            before = source.read_bytes()
            model_bytes = app.prepare_model_image(source)
            with Image.open(io.BytesIO(model_bytes)) as model_image:
                self.assertLessEqual(max(model_image.size), app.MODEL_IMAGE_LONG_EDGE)
                self.assertEqual(model_image.mode, "RGB")
            self.assertEqual(source.read_bytes(), before)

    def test_multipart_submission_parser_keeps_processed_and_original_images(self):
        boundary = "----history-grader-test"
        processed = b"processed-jpeg-bytes"
        original = b"original-jpeg-bytes"
        metadata = json.dumps(
            {"assignment_id": "a1", "student_label": "001", "photos": [{"name": "page.jpg"}]},
            ensure_ascii=False,
        ).encode("utf-8")

        def field(name, value, filename=None, media_type=None):
            disposition = f'form-data; name="{name}"'
            if filename:
                disposition += f'; filename="{filename}"'
            headers = [f"Content-Disposition: {disposition}"]
            if media_type:
                headers.append(f"Content-Type: {media_type}")
            return (
                f"--{boundary}\r\n".encode()
                + "\r\n".join(headers).encode()
                + b"\r\n\r\n"
                + value
                + b"\r\n"
            )

        body = b"".join(
            [
                field("metadata", metadata),
                field("processed_0", processed, "page.processed.jpg", "image/jpeg"),
                field("original_0", original, "page.jpg", "image/jpeg"),
                f"--{boundary}--\r\n".encode(),
            ]
        )
        payload = app.parse_multipart_submission(body, f'multipart/form-data; boundary="{boundary}"')
        self.assertEqual(payload["assignment_id"], "a1")
        self.assertEqual(payload["photos"][0]["name"], "page.jpg")
        photo = payload["photos"][0]
        self.assertNotIn("data_url", photo)
        self.assertNotIn("original_data_url", photo)
        self.assertEqual(app.photo_upload_bytes(photo), (processed, "image/jpeg"))
        self.assertEqual(
            app.photo_upload_bytes(photo, original=True), (original, "image/jpeg")
        )

    def test_save_photo_keeps_separate_original_upload(self):
        processed = Image.new("RGB", (1200, 900), "white")
        processed_buffer = io.BytesIO()
        processed.save(processed_buffer, format="JPEG", quality=70)
        original = Image.new("RGB", (1600, 1200), "white")
        original_buffer = io.BytesIO()
        original.save(original_buffer, format="PNG")
        processed_url = "data:image/jpeg;base64," + base64.b64encode(processed_buffer.getvalue()).decode()
        original_bytes = original_buffer.getvalue()
        original_url = "data:image/png;base64," + base64.b64encode(original_bytes).decode()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "photo.jpg"
            original_target = Path(temp) / "photo.original"
            meta = app.save_photo(processed_url, target, original_target, original_data_url=original_url)
            self.assertEqual(original_target.read_bytes(), original_bytes)
            self.assertEqual(meta["original_bytes"], len(original_bytes))
            self.assertEqual(meta["original_content_type"], "image/png")

    def test_manual_quad_keeps_top_right_and_bottom_left_in_place(self):
        image = Image.new("RGB", (400, 400), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 199, 199), fill="red")
        draw.rectangle((200, 0, 399, 199), fill="green")
        draw.rectangle((200, 200, 399, 399), fill="blue")
        draw.rectangle((0, 200, 199, 399), fill="yellow")
        corrected, _ = app.rectify_image(image, [(0, 0), (1, 0), (1, 1), (0, 1)])
        self.assertEqual(corrected.getpixel((50, 50)), (255, 0, 0))
        self.assertEqual(corrected.getpixel((350, 50)), (0, 128, 0))
        self.assertEqual(corrected.getpixel((350, 350)), (0, 0, 255))
        self.assertEqual(corrected.getpixel((50, 350)), (255, 255, 0))

    def test_photo_quality_gate_rejects_unusable_page(self):
        image = Image.new("RGB", (1200, 900), "white")
        quality = app.assess_photo_quality(image)
        self.assertFalse(quality["ok"])
        self.assertTrue(any("对比度" in issue or "清晰度" in issue for issue in quality["issues"]))

    def test_photo_quality_gate_accepts_high_contrast_page(self):
        image = Image.new("RGB", (1600, 1200), "white")
        draw = ImageDraw.Draw(image)
        for y in range(100, 1100, 55):
            draw.line((100, y, 1500, y), fill="black", width=4)
        quality = app.assess_photo_quality(image)
        self.assertTrue(quality["ok"], quality)

    def test_extract_json_from_fence(self):
        value = app.extract_json("```json\n{\"confidence\": 0.5}\n```")
        self.assertEqual(value["confidence"], 0.5)

    def test_extract_json_handles_nested_objects_and_trailing_text(self):
        value = app.extract_json(
            '模型说明：{"summary":"证据包含 {括号}","criteria":[{"score":2}]} 以上。'
        )
        self.assertEqual(value["criteria"][0]["score"], 2)

    def test_read_model_chat_content_accepts_openai_content_blocks(self):
        response = io.BytesIO(
            b'\xef\xbb\xbf{"choices":[{"finish_reason":"stop","message":'
            b'{"content":[{"type":"text","text":"{\\"ok\\":true}"}]}}]}'
        )
        diagnostics = {}
        content = app.read_model_chat_content(response, diagnostics)
        self.assertEqual(content, '{"ok":true}')
        self.assertEqual(diagnostics["finish_reason"], "stop")

    def test_model_diagnostic_parser_keeps_q1_to_q5(self):
        rubric = diagnostic_test_sub_questions()
        raw = json.dumps(diagnostic_test_response(), ensure_ascii=False)
        parser_output, normalized = app.parse_vision_model_output(raw, rubric, photo_count=1)
        self.assertEqual(app._response_sub_question_ids(parser_output), ["Q1", "Q2", "Q3", "Q4", "Q5"])
        self.assertEqual(
            [item["sub_question_id"] for item in normalized["sub_questions"]],
            ["Q1", "Q2", "Q3", "Q4", "Q5"],
        )

    def test_model_diagnostic_parser_accepts_id_alias_for_q4_q5(self):
        rubric = diagnostic_test_sub_questions()
        raw = json.dumps(diagnostic_test_response(id_field="id"), ensure_ascii=False)
        parser_output, normalized = app.parse_vision_model_output(raw, rubric, photo_count=1)
        self.assertEqual(app._response_sub_question_ids(parser_output), ["Q1", "Q2", "Q3", "Q4", "Q5"])
        self.assertEqual(
            [item["answer_status"] for item in normalized["sub_questions"]],
            ["answered"] * 5,
        )

    def test_model_diagnostic_parser_marks_raw_missing_q4_q5_without_scores(self):
        rubric = diagnostic_test_sub_questions()
        raw = json.dumps(diagnostic_test_response(("Q1", "Q2", "Q3")), ensure_ascii=False)
        parser_output, normalized = app.parse_vision_model_output(raw, rubric, photo_count=1)
        self.assertEqual(app._response_sub_question_ids(parser_output), ["Q1", "Q2", "Q3"])
        normalized_by_id = {item["sub_question_id"]: item for item in normalized["sub_questions"]}
        self.assertEqual(set(normalized_by_id), {"Q1", "Q2", "Q3", "Q4", "Q5"})
        for question_id in ("Q4", "Q5"):
            self.assertEqual(normalized_by_id[question_id]["answer_status"], "question_unmatched")
            self.assertIsNone(normalized_by_id[question_id]["suggested_total"])
            self.assertTrue(normalized_by_id[question_id]["blocked"])

    def test_read_model_chat_content_records_length_finish_reason_and_reasoning_usage(self):
        response = io.BytesIO(json.dumps({
            "id": "response-diagnostic-test",
            "status": "incomplete",
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 10,
                "total_tokens": 22,
                "completion_tokens_details": {"reasoning_tokens": 4},
            },
            "choices": [{"finish_reason": "length", "message": {"content": '{"sub_questions":[]}'}}],
        }).encode("utf-8"))
        diagnostics = {}
        content = app.read_model_chat_content(response, diagnostics)
        self.assertEqual(content, '{"sub_questions":[]}')
        self.assertEqual(diagnostics["provider_response_id"], "response-diagnostic-test")
        self.assertEqual(diagnostics["provider_response_status"], "incomplete")
        self.assertEqual(diagnostics["finish_reason"], "length")
        self.assertEqual(diagnostics["token_usage"]["input_tokens"], 12)
        self.assertEqual(diagnostics["token_usage"]["output_tokens"], 10)
        self.assertEqual(diagnostics["token_usage"]["reasoning_tokens"], 4)

    def test_private_model_call_artifact_writes_raw_parser_and_normalized_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "private_model_diagnostic_dir", return_value=Path(directory)):
                artifact = {
                    "artifact_schema_version": app.MODEL_CALL_DIAGNOSTIC_SCHEMA_VERSION,
                    "call_id": "diagnostic-test-id",
                    "raw_assistant_content": "raw",
                    "parser_output": {"sub_questions": []},
                    "normalized_result": {"sub_questions": []},
                }
                artifact_id = app.persist_private_model_call_artifact(artifact)
            saved = json.loads((Path(directory) / f"{artifact_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["raw_assistant_content"], "raw")
        self.assertIn("parser_output", saved)
        self.assertIn("normalized_result", saved)

    def test_read_model_chat_content_reports_safe_response_stage(self):
        response = io.BytesIO(b'{"choices":[{"finish_reason":"length","message":{"content":""}}]}')
        with self.assertRaises(app.ModelProviderResponseError) as error:
            app.read_model_chat_content(response)
        self.assertEqual(error.exception.stage, "message_content_empty")
        diagnostic = app.classify_model_error(error.exception)
        self.assertEqual(diagnostic["error_category"], "invalid_provider_response")
        self.assertEqual(diagnostic["response_stage"], "message_content_empty")
        self.assertEqual(diagnostic["finish_reason"] if "finish_reason" in diagnostic else None, None)

    def test_large_question_has_subquestion_totals(self):
        sub_questions = app.clean_sub_questions([
            {
                "id": "Q1",
                "label": "（1）",
                "question": "归纳职责",
                "standard_answer": "侦缉监视",
                "criteria": [{"label": "侦缉", "score": 2, "keywords": ["侦缉"]}],
            },
            {
                "id": "Q2",
                "label": "（2）",
                "question": "概括特点",
                "standard_answer": "直属皇帝",
                "criteria": [{"label": "直属皇帝", "score": 2, "keywords": ["直属皇帝"]}],
            },
        ])
        result = app.score_sub_questions(sub_questions, "侦缉，直属皇帝")
        self.assertEqual(result["max_total"], 4.0)
        self.assertEqual(len(result["sub_questions"]), 2)
        self.assertEqual(result["sub_questions"][0]["suggested_total"], 2.0)

    def test_nested_model_result_is_normalized(self):
        sub_questions = app.clean_sub_questions([
            {
                "id": "Q1",
                "label": "（1）",
                "question": "归纳职责",
                "standard_answer": "侦缉",
                "criteria": [{"label": "侦缉", "score": 2, "keywords": []}],
            },
            {
                "id": "Q2",
                "label": "（2）",
                "question": "概括特点",
                "standard_answer": "直属",
                "criteria": [{"label": "直属", "score": 2, "keywords": []}],
            },
        ])
        result = app.normalize_sub_question_result(
            {
                "confidence": 0.8,
                "summary": "需复核",
                "sub_questions": [
                    {"sub_question_id": "Q1", "criteria": [{"criterion_id": "C1", "suggested_score": 2, "decision": "full", "student_evidence": "侦缉", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .2, "height": .2}}]},
                    {"sub_question_id": "Q2", "criteria": [{"criterion_id": "C1", "suggested_score": 1, "decision": "partial", "student_evidence": "直属", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .2, "height": .2}}]},
                ],
            },
            sub_questions,
        )
        self.assertEqual(result["suggested_total"], 3.0)
        self.assertEqual(result["sub_questions"][1]["suggested_total"], 1.0)

    def test_generated_rubric_is_normalized(self):
        drafts = [
            {"id": "Q1", "label": "（1）", "question": "归纳职责", "standard_answer": "侦缉和监视"},
        ]
        result = app.normalize_generated_rubric(
            {
                "sub_questions": [
                    {
                        "sub_question_id": "Q1",
                        "criteria": [
                            {"criterion_id": "C1", "label": "侦缉职能", "score": 2, "keywords": "侦缉，缉捕"},
                            {"criterion_id": "C2", "label": "监视职能", "points": 1, "keywords": ["监视"]},
                        ],
                    }
                ]
            },
            drafts,
        )
        self.assertEqual(len(result["sub_questions"][0]["criteria"]), 2)
        self.assertEqual(result["sub_questions"][0]["criteria"][0]["keywords"], ["侦缉", "缉捕"])
        self.assertEqual(result["sub_questions"][0]["criteria"][1]["score"], 1.0)

    def test_local_rubric_draft_uses_ai_a_without_legacy_single_model_config(self):
        config = {
            "provider": "qwen",
            "provider_label": "通义千问",
            "model": "qwen3.8-max",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "SYNTHETIC_AI_A_KEY",
        }
        with patch.object(app, "cloud_mode_enabled", return_value=False), patch.object(
            app, "load_dual_review_model_configs", return_value={"A": dict(config)}
        ), patch.object(app, "model_configured", return_value=False):
            selected = app.rubric_generation_model_config()
        self.assertEqual(selected["model"], "qwen3.8-max")
        self.assertEqual(selected["model_role"], "AI-A")

        draft = [{"id": "Q1", "label": "（1）", "question": "概括背景", "standard_answer": "土地兼并严重"}]
        payload = app.build_rubric_model_payload("材料题", draft, selected["model"], selected["provider"])
        self.assertEqual(payload["model"], "qwen3.8-max")

        model_response = {"sub_questions": [{
            "sub_question_id": "Q1",
            "criteria": [{"criterion_id": "C1", "label": "土地兼并严重", "score": 2, "keywords": []}],
        }]}
        context = MagicMock()
        context.__enter__.return_value = object()
        with patch.object(app, "open_model_request", return_value=context) as open_request, patch.object(
            app, "read_model_chat_content", return_value=json.dumps(model_response, ensure_ascii=False)
        ):
            normalized = app.call_rubric_model("材料题", draft, payload, model_config=selected)
        self.assertEqual(normalized["sub_questions"][0]["criteria"][0]["label"], "土地兼并严重")
        request = open_request.call_args.args[0]
        self.assertEqual(request.full_url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(request.headers.get("Authorization"), "Bearer SYNTHETIC_AI_A_KEY")
        self.assertEqual(json.loads(request.data.decode("utf-8"))["model"], "qwen3.8-max")
        self.assertNotIn("SYNTHETIC_AI_A_KEY", json.dumps(payload, ensure_ascii=False))

    def test_generated_rubric_rejects_missing_unknown_and_duplicate_ids(self):
        drafts = [{"id": "Q1", "label": "（1）", "question": "题目", "standard_answer": "参考答案"}]
        missing = {"sub_questions": [{"sub_question_id": "Q1", "criteria": [{"label": "要点", "score": 2}]}]}
        unknown = {"sub_questions": [{"sub_question_id": "Q9", "criteria": [{"criterion_id": "C1", "label": "要点", "score": 2}]}]}
        duplicate = {"sub_questions": [{"sub_question_id": "Q1", "criteria": [
            {"criterion_id": "C1", "label": "要点一", "score": 2},
            {"criterion_id": "C1", "label": "要点二", "score": 2},
        ]}]}
        for value in (missing, unknown, duplicate):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app.normalize_generated_rubric(value, drafts)

    def test_point_bank_rubric_draft_keeps_all_supported_candidate_points(self):
        draft = [{
            "id": "Q1", "label": "（1）", "question": "题目", "standard_answer": "包含五个要点",
            "scoring_mode": "point_bank", "point_value": 2, "max_score": 6,
        }]
        model_draft = {"sub_questions": [{
            "sub_question_id": "Q1",
            "criteria": [{"criterion_id": f"C{i}", "label": f"有依据的要点{i}", "score": 2} for i in range(1, 6)],
        }]}
        normalized = app.normalize_generated_rubric(model_draft, draft)["sub_questions"][0]
        self.assertEqual(len(normalized["criteria"]), 5)
        self.assertEqual([item["score"] for item in normalized["criteria"]], [2] * 5)

    def test_numeric_decision_is_rejected_as_invalid_model_output(self):
        criteria = app.clean_criteria([{"label": "职责", "score": 2, "keywords": []}])
        result = app.normalize_model_result(
            {
                "criteria": [
                    {
                        "criterion_id": "C1",
                        "suggested_score": 2,
                        "decision": "0",
                        "reason": "未答出该要点",
                    }
                ]
            },
            criteria,
        )
        self.assertTrue(result["blocked"])
        self.assertIn("判定类型", " ".join(result["block_reasons"]))

    def test_duplicate_model_criteria_are_counted_once(self):
        criteria = app.clean_criteria([{"label": "职责", "score": 2, "keywords": []}])
        result = app.normalize_model_result(
            {
                "criteria": [
                    {"criterion_id": "C1", "suggested_score": 2, "reason": "命中"},
                    {"criterion_id": "C1", "suggested_score": 2, "reason": "重复返回"},
                ]
            },
            criteria,
        )
        self.assertTrue(result["blocked"])
        self.assertEqual(result["suggested_total"], 0.0)
        self.assertEqual(len(result["criteria"]), 1)

    def test_reordered_model_criteria_are_restored_by_stable_id(self):
        criteria = app.clean_criteria([
            {"id": "C-A", "label": "甲", "score": 2},
            {"id": "C-B", "label": "乙", "score": 2},
        ])
        result = app.normalize_model_result(
            {"criteria": [
                {"criterion_id": "C-B", "decision": "full", "suggested_score": 2, "student_evidence": "乙证据", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .2, "height": .2}},
                {"criterion_id": "C-A", "decision": "full", "suggested_score": 2, "student_evidence": "甲证据", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .2, "height": .2}},
            ]},
            criteria,
        )
        self.assertEqual([item["criterion_id"] for item in result["criteria"]], ["C-A", "C-B"])
        self.assertFalse(result["blocked"])

    def test_unknown_missing_and_duplicate_ids_block_model_result(self):
        criteria = app.clean_criteria([
            {"id": "C1", "label": "甲", "score": 2},
            {"id": "C2", "label": "乙", "score": 2},
        ])
        base = {"decision": "full", "suggested_score": 2, "student_evidence": "原文", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .2, "height": .2}}
        unknown = app.normalize_model_result({"criteria": [{**base, "criterion_id": "C1"}, {**base, "criterion_id": "C9"}, {**base, "criterion_id": "C2"}]}, criteria)
        duplicate = app.normalize_model_result({"criteria": [{**base, "criterion_id": "C1"}, {**base, "criterion_id": "C1"}, {**base, "criterion_id": "C2"}]}, criteria)
        missing_id = app.normalize_model_result({"criteria": [{**base, "criterion_id": "C1"}, {**base, "criterion_id": "C2"}, base]}, criteria)
        self.assertTrue(unknown["blocked"])
        self.assertTrue(duplicate["blocked"])
        self.assertTrue(missing_id["blocked"])

    def test_point_bank_retains_all_valid_hits_after_cap(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "scoring_mode": "point_bank", "point_value": 2, "max_score": 4,
            "criteria": [{"id": f"C{i}", "label": f"要点{i}", "score": 2} for i in range(1, 5)],
        }])
        result = app.normalize_sub_question_result({"sub_questions": [{
            "sub_question_id": "Q1",
            "criteria": [{
                "criterion_id": f"C{i}", "decision": "full", "suggested_score": 2,
                "student_evidence": f"学生原文{i}",
                "evidence": {"photo_index": 1, "x": .05 * i, "y": .1, "width": .1, "height": .2},
            } for i in range(1, 5)],
        }]}, sub_questions)
        criteria = result["sub_questions"][0]["criteria"]
        self.assertEqual(result["suggested_total"], 4)
        self.assertEqual([item["claimed_score"] for item in criteria], [2, 2, 2, 2])
        self.assertEqual([item["suggested_score"] for item in criteria], [2, 2, 0, 0])
        self.assertTrue(all(item["student_evidence"] and item["evidence"] for item in criteria))
        self.assertEqual([item["credit_status"] for item in criteria], ["credited", "credited", "capped", "capped"])

    def test_point_bank_six_points_five_candidates_three_hits_reaches_full_marks(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "scoring_mode": "point_bank", "point_value": 2, "max_score": 6,
            "criteria": [{"id": f"C{i}", "label": f"候选要点{i}", "score": 2} for i in range(1, 6)],
        }])
        result = app.normalize_sub_question_result({"sub_questions": [{
            "sub_question_id": "Q1",
            "criteria": [{
                "criterion_id": f"C{i}", "decision": "full", "suggested_score": 2,
                "student_evidence": f"可核查原文{i}",
                "evidence": {"photo_index": 1, "x": .05 * i, "y": .1, "width": .1, "height": .2},
            } for i in range(1, 6)],
        }]}, sub_questions)
        items = result["sub_questions"][0]["criteria"]
        self.assertEqual(result["suggested_total"], 6)
        self.assertEqual([item["claimed_score"] for item in items], [2] * 5)
        self.assertEqual([item["suggested_score"] for item in items], [2, 2, 2, 0, 0])
        self.assertTrue(all(item["student_evidence"] and item["evidence"] for item in items))

    def test_nondivisible_point_bank_displays_actual_remainder_and_keeps_later_hits(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "scoring_mode": "point_bank", "point_value": 2, "max_score": 5,
            "criteria": [{"id": f"C{i}", "label": f"候选要点{i}", "score": 2} for i in range(1, 6)],
        }])
        result = app.normalize_sub_question_result({"sub_questions": [{
            "sub_question_id": "Q1",
            "criteria": [
                {"criterion_id": "C1", "decision": "full", "suggested_score": 2, "student_evidence": "答出点1", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .1, "height": .1}},
                {"criterion_id": "C2", "decision": "full", "suggested_score": 2, "student_evidence": "答出点2", "evidence": {"photo_index": 1, "x": .2, "y": .1, "width": .1, "height": .1}},
                {"criterion_id": "C3", "decision": "partial", "suggested_score": 1, "student_evidence": "答出点3", "evidence": {"photo_index": 1, "x": .3, "y": .1, "width": .1, "height": .1}},
                {"criterion_id": "C4", "decision": "full", "suggested_score": 0, "student_evidence": "答出点4", "evidence": {"photo_index": 1, "x": .4, "y": .1, "width": .1, "height": .1}},
                {"criterion_id": "C5", "decision": "full", "suggested_score": 0, "student_evidence": "答出点5", "evidence": {"photo_index": 1, "x": .5, "y": .1, "width": .1, "height": .1}},
            ],
        }]}, sub_questions)
        items = result["sub_questions"][0]["criteria"]
        self.assertEqual(result["suggested_total"], 5)
        self.assertEqual([item["suggested_score"] for item in items], [2, 2, 1, 0, 0])
        self.assertEqual([item["claimed_score"] for item in items], [2] * 5)
        self.assertEqual([item["credit_status"] for item in items], ["credited", "credited", "capped", "capped", "capped"])
        self.assertEqual(items[2]["max_score"], 2)

    def test_duplicate_point_evidence_is_not_counted_twice(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "scoring_mode": "point_bank", "point_value": 2, "max_score": 4,
            "criteria": [{"id": "C1", "label": "甲点", "score": 2}, {"id": "C2", "label": "乙点", "score": 2}],
        }])
        result = app.normalize_sub_question_result({"sub_questions": [{"sub_question_id": "Q1", "criteria": [
            {"criterion_id": "C1", "decision": "full", "suggested_score": 2, "student_evidence": "同一句原文", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .1, "height": .1}},
            {"criterion_id": "C2", "decision": "full", "suggested_score": 2, "student_evidence": "同一句，原文。", "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .1, "height": .1}},
        ]}]}, sub_questions)
        self.assertEqual(result["suggested_total"], 2)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["sub_questions"][0]["criteria"][1]["credit_status"], "duplicate_evidence")

    def test_subquestion_unknown_duplicate_and_missing_ids_are_blocked_without_fallback(self):
        sub_questions = app.clean_sub_questions([
            {"id": "Q-A", "label": "（1）", "criteria": [{"id": "C1", "label": "甲", "score": 2}]},
            {"id": "Q-B", "label": "（2）", "criteria": [{"id": "C1", "label": "乙", "score": 2}]},
        ])
        def sub_result(sub_id, score=2):
            return {
                "sub_question_id": sub_id,
                "criteria": [{"criterion_id": "C1", "decision": "full", "suggested_score": score,
                    "student_evidence": "学生原文" + sub_id,
                    "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .2, "height": .2}}],
            }
        reordered = app.normalize_sub_question_result({"sub_questions": [sub_result("Q-B"), sub_result("Q-A")]}, sub_questions)
        self.assertFalse(reordered["blocked"])
        self.assertEqual([item["sub_question_id"] for item in reordered["sub_questions"]], ["Q-A", "Q-B"])
        for incoming in (
            [sub_result("Q-A"), sub_result("Q-UNKNOWN")],
            [sub_result("Q-A"), sub_result("Q-A"), sub_result("Q-B")],
            [{"label": "（1）", "criteria": []}, sub_result("Q-B")],
        ):
            with self.subTest(incoming=incoming):
                self.assertTrue(app.normalize_sub_question_result({"sub_questions": incoming}, sub_questions)["blocked"])

    def test_duplicate_rubric_ids_and_nonfinite_scores_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "评分点 ID 重复"):
            app.clean_criteria([{"id": "same", "label": "甲", "score": 1}, {"id": "same", "label": "乙", "score": 1}])
        with self.assertRaisesRegex(ValueError, "有限数字"):
            app.clean_criteria([{"label": "甲", "score": float("nan")}])
        with self.assertRaisesRegex(ValueError, "小题 ID 重复"):
            app.clean_sub_questions([
                {"id": "Q", "label": "甲", "criteria": [{"label": "点", "score": 1}]},
                {"id": "Q", "label": "乙", "criteria": [{"label": "点", "score": 1}]},
            ])
        with self.assertRaisesRegex(ValueError, "相同或近义"):
            app.clean_sub_questions([{
                "id": "Q1", "label": "按点计分", "scoring_mode": "point_bank", "point_value": 2, "max_score": 2,
                "criteria": [{"label": "中央集权", "score": 2}, {"label": "中央-集权", "score": 2}],
            }])

    def test_legacy_question_without_scoring_mode_remains_weighted(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q-old", "label": "旧题", "max_score": 999,
            "criteria": [{"id": "C1", "label": "旧规则一", "score": 1}, {"id": "C2", "label": "旧规则二", "score": 2}],
        }])
        self.assertEqual(sub_questions[0]["scoring_mode"], "weighted")
        self.assertEqual(sub_questions[0]["max_score"], 3)

    def test_legacy_weighted_question_recomputes_nonpositive_stored_max(self):
        for stored_max in (0, -1):
            with self.subTest(stored_max=stored_max):
                normalized = app.clean_sub_questions([{
                    "id": "Q-old", "label": "旧题", "max_score": stored_max,
                    "criteria": [{"id": "C1", "label": "旧规则", "score": 3}],
                }])
                self.assertEqual(normalized[0]["scoring_mode"], "weighted")
                self.assertEqual(normalized[0]["max_score"], 3)

    def test_point_bank_warns_when_candidate_pool_cannot_reach_maximum(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "scoring_mode": "point_bank", "point_value": 2, "max_score": 6,
            "criteria": [{"label": "候选一", "score": 2}, {"label": "候选二", "score": 2}],
        }])
        self.assertEqual(len(app.scoring_warnings(sub_questions)), 1)
        self.assertIn("至少 3 个", app.scoring_warnings(sub_questions)[0])

    def test_rubric_hash_is_stable_and_changes_when_any_rule_changes(self):
        sub_questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "scoring_mode": "point_bank", "point_value": 2, "max_score": 6,
            "question": "题目", "standard_answer": "参考答案",
            "criteria": [{"id": "C1", "label": "要点", "score": 2, "keywords": ["同义表达"]}],
        }])
        initial = app.rubric_fingerprint(sub_questions)
        reordered_keys = [{key: item[key] for key in reversed(list(item))} for item in sub_questions]
        self.assertEqual(initial, app.rubric_fingerprint(reordered_keys))
        changed = json.loads(json.dumps(sub_questions, ensure_ascii=False))
        changed[0]["criteria"][0]["label"] = "变更后的要点"
        self.assertNotEqual(initial, app.rubric_fingerprint(changed))
        context = app.grading_version_context("vision-model", sub_questions)
        self.assertEqual(context["rubric_sha256"], initial)
        self.assertEqual(len(initial), 64)

    def test_model_result_blocks_scored_criterion_without_evidence(self):
        criteria = app.clean_criteria([{"label": "职责", "score": 2, "keywords": []}])
        result = app.normalize_model_result(
            {"criteria": [{"criterion_id": "C1", "suggested_score": 2, "decision": "full"}]},
            criteria,
        )
        self.assertTrue(result["blocked"])
        self.assertIn("证据", " ".join(result["block_reasons"]))

    def test_image_quality_failure_is_retryable_and_blocked(self):
        sub_questions = app.clean_sub_questions(
            [
                {
                    "id": "Q1",
                    "label": "（1）",
                    "question": "归纳职责",
                    "standard_answer": "侦缉",
                    "criteria": [{"label": "侦缉", "score": 2, "keywords": []}],
                }
            ]
        )
        result = app.image_quality_failure_result(sub_questions, ["第1张照片：图片整体清晰度不足"])
        self.assertEqual(result["mode"], "image-quality-error")
        self.assertTrue(result["blocked"])
        self.assertTrue(result["retryable"])
        self.assertIsNone(result["suggested_total"])

    def test_model_failure_is_explicitly_retryable(self):
        sub_questions = app.clean_sub_questions(
            [
                {
                    "id": "Q1",
                    "label": "（1）",
                    "question": "归纳职责",
                    "standard_answer": "侦缉",
                    "criteria": [{"label": "侦缉", "score": 2, "keywords": []}],
                }
            ]
        )
        with patch.object(app, "model_configured", return_value=True), patch.object(
            app, "call_vision_model", side_effect=RuntimeError("model unavailable")
        ):
            result = app.grade_submission({}, sub_questions, "", [Path("not-read.jpg")])
        self.assertEqual(result["mode"], "vision-model-error")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["suggested_total"], 0.0)
        self.assertEqual(result["max_total"], 2.0)
        self.assertNotIn("rules-fallback", result["mode"])
        self.assertEqual(result["diagnostics"]["error_category"], "unexpected_model_error")
        self.assertIn("grade_ms", result["diagnostics"])

    def test_model_error_diagnostic_classifies_http_without_body_or_secret(self):
        error = HTTPError("https://provider.invalid/chat/completions", 429, "rate limited", {}, None)
        try:
            diagnostic = app.classify_model_error(error)
            self.assertEqual(diagnostic["error_category"], "provider_transient")
            self.assertTrue(diagnostic["retryable"])
            self.assertEqual(diagnostic["http_status"], 429)
            self.assertNotIn("rate limited", json.dumps(diagnostic, ensure_ascii=False))
            self.assertNotIn("api_key", json.dumps(diagnostic, ensure_ascii=False).casefold())
        finally:
            error.close()

    def test_model_error_diagnostic_does_not_retry_request_too_large(self):
        error = HTTPError("https://provider.invalid/chat/completions", 413, "too large", {}, None)
        try:
            diagnostic = app.classify_model_error(error)
            self.assertEqual(diagnostic["error_category"], "request_too_large")
            self.assertFalse(diagnostic["retryable"])
        finally:
            error.close()

    def test_manual_retry_policy_rejects_non_transient_model_failures(self):
        allowed, _ = app.manual_retry_policy(
            {"mode": "vision-model-error", "diagnostics": {"retryable": True, "message": "暂时不可用"}},
            "completed",
        )
        self.assertTrue(allowed)
        legacy_allowed, legacy_reason = app.manual_retry_policy(
            {"mode": "vision-model-error", "retryable": True}, "completed"
        )
        self.assertTrue(legacy_allowed)
        self.assertIn("旧版本", legacy_reason)
        blocked, reason = app.manual_retry_policy(
            {"mode": "vision-model-error", "diagnostics": {"retryable": False, "message": "请求过大"}},
            "completed",
        )
        self.assertFalse(blocked)
        self.assertIn("请求过大", reason)
        processing_allowed, _ = app.manual_retry_policy({"mode": "processing-error"}, "failed")
        self.assertTrue(processing_allowed)
        no_photo_allowed, no_photo_reason = app.manual_retry_policy(
            {"mode": "processing-error"}, "failed", has_photos=False
        )
        self.assertFalse(no_photo_allowed)
        self.assertIn("重新提交照片", no_photo_reason)

    def test_manual_retry_backoff_is_bounded_and_increases(self):
        self.assertEqual(app.manual_retry_backoff_seconds(0, "not-a-timestamp"), 0)
        with patch.object(app.time, "time", return_value=100.0):
            self.assertEqual(
                app.manual_retry_backoff_seconds(1, "1970-01-01T00:01:36+00:00"),
                1,
            )
            self.assertEqual(
                app.manual_retry_backoff_seconds(99, "1970-01-01T00:01:36+00:00"),
                56,
            )

    def test_parse_csv_roster_preserves_student_numbers(self):
        raw = "班级,学号,姓名\n高一（1）班,0101,张三\n高一（1）班,0102,李四\n".encode("gb18030")
        data_url = "data:text/csv;base64," + base64.b64encode(raw).decode()
        records = app.parse_roster_upload("名单.csv", data_url)
        self.assertEqual(records[0]["student_no"], "0101")
        self.assertEqual(records[1]["student_no"], "0102")

    def test_parse_simple_xlsx_roster(self):
        files = {
            "xl/workbook.xml": (
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                '</Relationships>'
            ),
            "xl/sharedStrings.xml": (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="6" uniqueCount="6">'
                '<si><t>班级</t></si><si><t>学号</t></si><si><t>姓名</t></si>'
                '<si><t>高一（1）班</t></si><si><t>0101</t></si><si><t>张三</t></si></sst>'
            ),
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
                '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c><c r="C2" t="s"><v>5</v></c></row>'
                '</sheetData></worksheet>'
            ),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        data_url = "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64," + base64.b64encode(buffer.getvalue()).decode()
        records = app.parse_roster_upload("名单.xlsx", data_url)
        self.assertEqual(records, [{"class_name": "高一（1）班", "student_no": "0101", "student_name": "张三"}])

    def test_roster_prefers_seat_number_over_long_student_id(self):
        raw = "新班,序号,学籍号,姓名\n1,7,G445381200812241772,张三\n".encode("utf-8")
        data_url = "data:text/csv;base64," + base64.b64encode(raw).decode()
        records = app.parse_roster_upload("名单.csv", data_url)
        self.assertEqual(records, [{"class_name": "1", "student_no": "7", "student_name": "张三"}])

    def test_grading_version_context_is_versioned_and_secret_free(self):
        with patch.dict(
            app.os.environ,
            {
                "HISTORY_GRADER_PROVIDER": "test-provider",
                "HISTORY_GRADER_BASE_URL": "https://example.invalid/v1",
                "HISTORY_GRADER_MODEL": "test-vision-model",
            },
            clear=True,
        ):
            context = app.grading_version_context("rules-demo")
        self.assertEqual(context["audit_record_version"], app.AUDIT_RECORD_VERSION)
        self.assertEqual(context["app_version"], app.APP_VERSION)
        self.assertEqual(context["grading_rules_version"], app.GRADING_RULE_VERSION)
        self.assertEqual(context["mode"], "rules-demo")
        self.assertNotIn("api_key", context)
        self.assertNotIn("base_url", context)

    def test_evaluation_summary_uses_only_explicit_anonymous_manifest_cases(self):
        old_paths = app.APP_PATHS
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "portable"
            app.configure_paths(root, Path(temp) / "program")
            try:
                conn = app.get_db()
                conn.execute(
                    "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("aaaaaaaaaaaa", "评测题", "题干", "答案", "[]", "[]", app.utc_now()),
                )
                result = {
                    "mode": "vision-model",
                    "suggested_total": 3,
                    "max_total": 4,
                    "grading_context": {
                        "app_version": app.APP_VERSION,
                        "grading_rules_version": app.GRADING_RULE_VERSION,
                        "prompt_version": app.VISION_PROMPT_VERSION,
                    },
                }
                conn.execute(
                    "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("bbbbbbbbbbbb", "aaaaaaaaaaaa", "匿名样本", "", "[]", json.dumps(result, ensure_ascii=False), app.utc_now()),
                )
                conn.commit()
                conn.close()
                app.EVALUATION_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
                app.EVALUATION_MANIFEST_PATH.write_text(
                    json.dumps(
                        {
                            "format": app.EVALUATION_MANIFEST_FORMAT,
                            "name": "匿名测试",
                            "severe_error_threshold": 2,
                            "cases": [
                                {
                                    "case_id": "sample-001",
                                    "submission_id": "bbbbbbbbbbbb",
                                    "gold_total": 3,
                                    "max_total": 4,
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                summary = app.evaluation_summary()
                self.assertTrue(summary["manifest"]["configured"])
                self.assertEqual(summary["metrics"]["scored_cases"], 1)
                self.assertEqual(summary["metrics"]["within_one_rate"], 1.0)
                self.assertEqual(summary["metrics"]["severe_error_rate"], 0.0)
                self.assertEqual(summary["metrics"]["failure_rate"], 0.0)
                self.assertTrue(summary["audit"]["chain_valid"])
            finally:
                app._install_paths(old_paths)

    def test_evaluation_manifest_rejects_personal_data_fields(self):
        old_manifest_path = app.EVALUATION_MANIFEST_PATH
        with tempfile.TemporaryDirectory() as temp:
            app.EVALUATION_MANIFEST_PATH = Path(temp) / "manifest.json"
            app.EVALUATION_MANIFEST_PATH.write_text(
                json.dumps(
                    {
                        "format": app.EVALUATION_MANIFEST_FORMAT,
                        "cases": [],
                        "student_name": "must-not-be-here",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            try:
                with self.assertRaisesRegex(ValueError, "禁止放入答案、照片或学生个人信息"):
                    app.read_evaluation_manifest()
            finally:
                app.EVALUATION_MANIFEST_PATH = old_manifest_path


    def test_unanswered_unmatched_and_unreadable_subquestions_never_become_confirmable_zero(self):
        sub_questions = app.clean_sub_questions([
            {"id": "17-1", "label": "17(1)", "question": "材料一", "criteria": [{"id": "C1", "label": "一", "score": 2}]},
            {"id": "17-2", "label": "17(2)", "question": "材料二", "criteria": [{"id": "C2", "label": "二", "score": 2}]},
            {"id": "17-3", "label": "17(3)", "question": "材料三", "criteria": [{"id": "C3", "label": "三", "score": 2}]},
            {"id": "18-1", "label": "18(1)", "question": "材料四", "criteria": [{"id": "C4", "label": "四", "score": 2}]},
            {"id": "18-2", "label": "18(2)", "question": "材料五", "criteria": [{"id": "C5", "label": "五", "score": 2}]},
        ])
        result = app.normalize_sub_question_result({
            "sub_questions": [
                {"sub_question_id": "17-1", "answer_status": "answered", "criteria": [{
                    "criterion_id": "C1", "suggested_score": 2, "decision": "full", "student_evidence": "合成证据",
                    "reason": "合成理由", "needs_review": False,
                    "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .2, "height": .1},
                }]},
                {"sub_question_id": "17-2", "answer_status": "unanswered_candidate", "criteria": []},
                {"sub_question_id": "17-3", "answer_status": "image_unreadable", "criteria": []},
                # 18(1) is omitted: the server must mark it unmatched, not zero.
                {"sub_question_id": "18-2", "answer_status": "answered", "criteria": [{
                    "criterion_id": "C5", "suggested_score": 0, "decision": "none", "student_evidence": "",
                    "reason": "无证据", "needs_review": False,
                }]},
            ],
            "unmapped_answers": [{
                "answer_id": "A1", "evidence": {
                    "photo_index": 2, "x": .2, "y": .3, "width": .15, "height": .08,
                },
            }],
        }, sub_questions, photo_count=2)
        by_id = {item["sub_question_id"]: item for item in result["sub_questions"]}
        self.assertEqual(by_id["17-2"]["answer_status"], "unanswered_candidate")
        self.assertIsNone(by_id["17-2"]["suggested_total"])
        self.assertEqual(by_id["17-3"]["answer_status"], "image_unreadable")
        self.assertIsNone(by_id["17-3"]["suggested_total"])
        self.assertEqual(by_id["18-1"]["answer_status"], "question_unmatched")
        self.assertIsNone(by_id["18-1"]["suggested_total"])
        self.assertEqual(result["unmapped_answers"][0]["photo_index"], 2)
        self.assertTrue(result["requires_question_mapping"])
        self.assertTrue(result["blocked"])
        self.assertTrue(app.result_has_unresolved_answer_status(result))

        mapping = app.build_teacher_question_mapping(
            [{"answer_id": "A1", "sub_question_id": "18-1"}], result, sub_questions
        )
        self.assertEqual(mapping[0]["label"], "18(1)")
        self.assertEqual(mapping[0]["photo_index"], 2)
        for invalid in (
            [{"answer_id": "not-present", "sub_question_id": "18-1"}],
            [{"answer_id": "A1", "sub_question_id": "missing-question"}],
            [],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                app.build_teacher_question_mapping(invalid, result, sub_questions)



if __name__ == "__main__":
    unittest.main()
