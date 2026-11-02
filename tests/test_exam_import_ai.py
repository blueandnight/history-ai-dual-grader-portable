"""AI text-parsing regression tests: 26-question adaptability, merge safety, endpoints."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
from exam_import_ai import (
    build_exam_import_ai_payload,
    build_exam_import_ai_source,
    merge_exam_import_ai_result,
    normalize_exam_import_ai_result,
)


def synthetic_draft() -> dict:
    objectives = [
        {
            "question_id": str(number),
            "stem": f"原始题干{number}",
            "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
            "correct_answer_draft": "A",
            "answer_found": True,
            "needs_teacher_review": False,
        }
        for number in range(1, 17)
    ]
    subs = [
        {
            "label": str(part), "question_id": f"17.{part}", "parent_question_id": "17",
            "question": f"原始设问17.{part}", "reference_answer": f"原始答案17.{part}",
            "standard_answer": f"原始答案17.{part}", "max_score": None,
            "rubric_draft": [{"label": "原始要点", "score": None, "needs_teacher_confirmation": True}],
            "needs_teacher_confirmation": True,
        }
        for part in (1, 2, 3)
    ]
    return {
        "status": "draft", "title": "合成试卷",
        "objective_questions": objectives,
        "objective_answer_key": {str(number): "A" for number in range(1, 17)},
        "material_questions": [{"label": "17", "prompt": "原始材料", "sub_questions": subs}],
        "source": {"source_pair_status": "not_applicable", "layout_source": None},
        "layout_reference": {"pages": [], "page_roles": []},
        "warnings": [], "warning_codes": [],
    }


def raw_ai_result(objective_count: int = 26) -> dict:
    objectives = []
    for number in range(1, objective_count + 1):
        objectives.append({
            "question_id": f"{number:02d}" if number < 10 else str(number),
            "stem": f"AI题干{number}",
            "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
            "answer": "B" if number % 2 == 0 else "A",
        })
    return {
        "title": "AI 标题",
        "objective_questions": objectives,
        "subjective_questions": [
            {"question_id": "17.1", "major_label": "17", "question": "AI设问17.1", "reference_answer": "AI答案17.1", "max_score": 4},
            {"question_id": "17.2", "major_label": "17", "question": "AI设问17.2", "reference_answer": "原始答案17.2", "max_score": 6},
            {"question_id": "17.3", "major_label": "17", "question": "AI设问17.3", "reference_answer": "AI答案17.3", "max_score": 8},
            {"question_id": "18.1", "major_label": "18", "question": "AI设问18.1", "reference_answer": "AI答案18.1", "max_score": 10},
        ],
    }


class ExamImportAiModuleTests(unittest.TestCase):
    def test_subjective_parent_is_derived_from_canonical_question_id(self):
        parsed = normalize_exam_import_ai_result({"subjective_questions": [{
            "question_id": "018.01", "major_label": "17", "question": "设问", "reference_answer": "答案", "max_score": 2,
        }]})
        self.assertEqual(parsed["subjective_questions"][0]["major_label"], "18")
        self.assertTrue(any("大题号不一致" in issue for issue in parsed["issues"]))
        draft = synthetic_draft()
        merged, _ = merge_exam_import_ai_result(draft, parsed)
        major = next(m for m in merged["material_questions"] if m["label"] == "18")
        self.assertEqual(major["sub_questions"][0]["parent_question_id"], "18")
        self.assertEqual(merged["material_questions"][0]["prompt"], "原始材料")

    def test_missing_ai_answer_can_be_confirmed_but_missing_options_still_block(self):
        draft = {"title": "试卷", "source": {}, "objective_questions": [], "material_questions": []}
        raw = {"objective_questions": [{"question_id": "1", "stem": "题干", "options": dict(A="甲", B="乙", C="丙", D="丁"), "answer": None}]}
        merged, _ = merge_exam_import_ai_result(draft, normalize_exam_import_ai_result(raw))
        self.assertFalse(merged["objective_questions"][0]["needs_teacher_review"])
        confirmation = {"exam_name": "试卷", "teacher_confirmed": True, "objective_answer_key": {"1": "B"}, "material_questions": []}
        confirmed, _, _ = app._confirmed_import_payload(merged, confirmation)
        self.assertEqual(confirmed["objective_answer_key"], {"1": "B"})
        with self.assertRaisesRegex(ValueError, "尚未确认正确答案"):
            app._confirmed_import_payload(merged, {**confirmation, "objective_answer_key": {}})
        del raw["objective_questions"][0]["options"]["D"]
        broken, _ = merge_exam_import_ai_result(draft, normalize_exam_import_ai_result(raw))
        with self.assertRaisesRegex(ValueError, "结构未完整识别"):
            app._confirmed_import_payload(broken, confirmation)

    def test_normalize_adapts_to_26_questions_and_canonical_ids(self):
        parsed = normalize_exam_import_ai_result(raw_ai_result(26))
        self.assertEqual([item["question_id"] for item in parsed["objective_questions"]], [str(n) for n in range(1, 27)])
        self.assertEqual(parsed["objective_questions"][0]["answer"], "A")
        self.assertEqual([item["question_id"] for item in parsed["subjective_questions"]], ["17.1", "17.2", "17.3", "18.1"])

    def test_normalize_skips_bad_ids_duplicates_and_bad_answers(self):
        raw = {
            "objective_questions": [
                {"question_id": "01", "stem": "a", "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "answer": "A"},
                {"question_id": "1", "stem": "duplicate", "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "answer": "B"},
                {"question_id": "abc", "stem": "bad id"},
                {"question_id": "2", "stem": "b", "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "answer": "E"},
            ]
        }
        parsed = normalize_exam_import_ai_result(raw)
        self.assertEqual([item["question_id"] for item in parsed["objective_questions"]], ["1", "2"])
        self.assertEqual(parsed["objective_questions"][0]["answer"], "A")
        self.assertEqual(parsed["objective_questions"][1]["answer"], "")
        self.assertTrue(parsed["issues"])

    def test_normalize_rejects_empty_result(self):
        with self.assertRaisesRegex(ValueError, "未返回可用的题号/答案结构"):
            normalize_exam_import_ai_result({"objective_questions": [], "subjective_questions": []})
        with self.assertRaisesRegex(ValueError, "不是 JSON 对象"):
            normalize_exam_import_ai_result([])

    def test_merge_adds_new_objectives_without_deleting_original_text(self):
        merged, summary = merge_exam_import_ai_result(synthetic_draft(), normalize_exam_import_ai_result(raw_ai_result(26)))
        objectives = merged["objective_questions"]
        self.assertEqual(len(objectives), 26)
        self.assertEqual([item["question_id"] for item in objectives[:16]], [str(n) for n in range(1, 17)])
        self.assertEqual(objectives[0]["stem"], "原始题干1")
        self.assertEqual(objectives[16]["stem"], "AI题干17")
        self.assertEqual(summary["objective"]["added"], [str(n) for n in range(17, 27)])
        self.assertEqual(summary["objective"]["answer_conflicts"], [str(n) for n in range(2, 17, 2)])
        self.assertEqual(merged["objective_questions"][1]["correct_answer_draft"], "A")
        self.assertEqual(merged["objective_questions"][0]["correct_answer_draft"], "A")
        self.assertIn("ai_parse_applied", merged["warning_codes"])
        self.assertIn("ai_parse_answer_conflict", merged["warning_codes"])

    def test_merge_adds_new_subjective_major_and_keeps_original_answers_on_conflict(self):
        merged, summary = merge_exam_import_ai_result(synthetic_draft(), normalize_exam_import_ai_result(raw_ai_result(26)))
        subs = {
            sub["question_id"]: sub
            for major in merged["material_questions"] for sub in major["sub_questions"]
        }
        self.assertEqual(subs["17.1"]["reference_answer"], "原始答案17.1")
        self.assertIn("17.1", summary["subjective"]["answer_conflicts"])
        self.assertEqual(subs["17.2"]["reference_answer"], "原始答案17.2")
        self.assertEqual(subs["18.1"]["reference_answer"], "AI答案18.1")
        self.assertEqual(subs["18.1"]["parent_question_id"], "18")
        self.assertIn("18.1", summary["subjective"]["added"])
        self.assertEqual(merged["title"], "合成试卷")
        self.assertEqual(merged["material_questions"][0]["prompt"], "原始材料")

    def test_source_uses_text_only_and_rejects_empty_or_oversized_input(self):
        source = build_exam_import_ai_source("Word题干", ["PDF第一页", "PDF第二页"], title="合成试卷")
        self.assertIn("【Word 文档文字】", source["text"])
        self.assertIn("【PDF 第 2 页】", source["text"])
        self.assertEqual(source["pdf_page_count"], 2)
        self.assertNotIn("image", source)
        with self.assertRaisesRegex(ValueError, "没有可提取的文字"):
            build_exam_import_ai_source("", [])
        with self.assertRaisesRegex(ValueError, "超过 AI 解析上限"):
            build_exam_import_ai_source("字" * 100_001, [])

    def test_payload_is_text_only_and_requests_json_structure(self):
        payload = build_exam_import_ai_payload("试卷文字", "synthetic-model", "deepseek")
        content = payload["messages"][1]["content"]
        self.assertIsInstance(content, str)
        self.assertNotIn("image_url", json.dumps(payload, ensure_ascii=False))
        self.assertIn("objective_questions", content)
        self.assertIn("subjective_questions", content)
        self.assertEqual(payload["response_format"], {"type": "json_object"})


class ExamImportAiEndpointTests(unittest.TestCase):
    draft_id = "a" * 32

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_paths = app.APP_PATHS
        app.configure_paths(self.root / "portable", self.old_paths.program_dir)
        self.draft_dir = app.DATA_DIR / "exam_import_drafts" / self.draft_id
        self.draft_dir.mkdir(parents=True)
        (self.draft_dir / "exam_import_draft.json").write_text(
            json.dumps(synthetic_draft(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.source = build_exam_import_ai_source("合成 Word 文字", ["合成 PDF 文字"])
        self.events = []

    def tearDown(self):
        app._install_paths(self.old_paths)
        self.temp.cleanup()

    def handler(self, payload):
        return SimpleNamespace(require_local_client=lambda: True, read_json=lambda: payload)

    def call(self, method, payload):
        replies = []
        handler = self.handler(payload)
        config = {
            "provider": "synthetic", "provider_label": "合成模型", "model": "synthetic-model",
            "base_url": "https://api.example.test/v1", "api_key": "synthetic-key", "timeout": "30",
            "model_role": "AI-A",
        }
        with patch.object(app, "_exam_import_ai_source", return_value=self.source), \
             patch.object(app, "rubric_generation_model_config", return_value=config), \
             patch.object(app, "model_chat_completions_endpoint", return_value="https://api.example.test/v1/chat/completions"), \
             patch.object(app, "append_audit_event", side_effect=lambda *args: self.events.append(args)), \
             patch.object(app, "json_response", side_effect=lambda h, body, status=200: replies.append((body, status))):
            method.__wrapped__(handler)
        return replies[0]

    def preview(self):
        return self.call(app.AppHandler.exam_import_ai_preview, {"draft_id": self.draft_id})[0]

    def test_identity_text_blocks_preview_and_send_without_model_call(self):
        preview = self.preview()
        original_file = (self.draft_dir / "exam_import_draft.json").read_bytes()
        for text in ("姓名：合成测试者", "学号：SYNTHETIC_12345", "二维码内容：SYNTHETIC_QR"):
            self.source = build_exam_import_ai_source(text, [])
            with self.subTest(text=text), patch.object(app, "call_exam_import_ai_model") as model:
                with self.assertRaisesRegex(ValueError, "身份信息"):
                    self.preview()
                with self.assertRaisesRegex(ValueError, "身份信息"):
                    self.call(app.AppHandler.exam_import_ai_parse, {
                        "draft_id": self.draft_id, "confirm_send_text": True, "preview_sha256": preview["preview_sha256"],
                    })
                model.assert_not_called()
            self.assertEqual((self.draft_dir / "exam_import_draft.json").read_bytes(), original_file)

    def test_blank_identity_slots_allowed_and_roster_values_still_block(self):
        self.source = build_exam_import_ai_source("姓名：________班级：＿＿＿＿ 学号：______\n题干正文", [])
        self.assertTrue(self.preview()["ok"])
        conn = app.get_db()
        conn.execute("INSERT INTO roster_students (id,class_name,student_no,student_name,created_at) VALUES (?,?,?,?,?)",
                     ("synthetic-id", "测试班", "SYNTHETIC_ROSTER_123", "合成名单甲", app.utc_now()))
        conn.commit()
        conn.close()
        self.source = build_exam_import_ai_source("无标签但包含合成名单甲的文字", [])
        with self.assertRaisesRegex(ValueError, "身份信息"):
            self.preview()

    def test_preview_reports_text_only_source_and_fingerprint(self):
        body, status = self.call(app.AppHandler.exam_import_ai_preview, {"draft_id": self.draft_id})
        self.assertEqual(status, 200)
        self.test = body
        self.assertIn("Word", body["source_label"])
        self.assertGreater(body["text_chars"], 0)
        self.assertEqual(body["model"], "synthetic-model")
        self.assertIn("不发送页面图片", body["note"])
        self.assertEqual(len(body["preview_sha256"]), 64)

    def test_parse_requires_explicit_confirmation_and_matching_preview(self):
        preview = self.preview()
        with self.assertRaisesRegex(ValueError, "请先确认发送"):
            self.call(app.AppHandler.exam_import_ai_parse, {"draft_id": self.draft_id, "preview_sha256": preview["preview_sha256"]})
        with self.assertRaisesRegex(ValueError, "请重新预览"):
            self.call(app.AppHandler.exam_import_ai_parse, {
                "draft_id": self.draft_id, "confirm_send_text": True, "preview_sha256": "0" * 64,
            })

    def test_parse_persists_merged_draft_and_summary(self):
        preview = self.preview()
        replies = []
        handler = self.handler({
            "draft_id": self.draft_id,
            "confirm_send_text": True,
            "preview_sha256": preview["preview_sha256"],
        })
        config = {
            "provider": "synthetic", "provider_label": "合成模型", "model": "synthetic-model",
            "base_url": "https://api.example.test/v1", "api_key": "synthetic-key", "timeout": "30",
            "model_role": "AI-A",
        }
        with patch.object(app, "_exam_import_ai_source", return_value=self.source), \
             patch.object(app, "rubric_generation_model_config", return_value=config), \
             patch.object(app, "model_chat_completions_endpoint", return_value="https://api.example.test/v1/chat/completions"), \
             patch.object(app, "call_exam_import_ai_model", return_value=raw_ai_result(26)) as model_call, \
             patch.object(app, "append_audit_event", side_effect=lambda *args: self.events.append(args)), \
             patch.object(app, "json_response", side_effect=lambda h, body, status=200: replies.append((body, status))):
            app.AppHandler.exam_import_ai_parse.__wrapped__(handler)
        body, status = replies[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["objective"]["ai_count"], 26)
        self.assertEqual(len(body["draft"]["objective_questions"]), 26)
        self.assertTrue(body["draft"]["ai_parse"]["status"] == "applied")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1], "exam_import_ai_parse")
        model_call.assert_called_once()
        saved = json.loads((self.draft_dir / "exam_import_draft.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["objective_questions"]), 26)
        self.assertIn("ai_parse_applied", saved["warning_codes"])


if __name__ == "__main__":
    unittest.main()
