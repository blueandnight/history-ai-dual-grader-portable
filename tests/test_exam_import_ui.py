import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
from exam_import_draft import _merge_answer_keys
from PIL import Image


def import_draft(*, pair_status="not_applicable", conflict=False):
    docx_key = {"1": "A"}
    pdf_key = {"1": "B"} if conflict else {}
    objective_key, crosscheck, _, conflicts, _ = _merge_answer_keys(docx_key, pdf_key)
    return {
        "status": "draft",
        "title": "测试考试",
        "objective_questions": [{
            "question_id": "1", "stem": "测试题干（ ）",
            "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
            "needs_teacher_review": False,
        }],
        "objective_answer_key": objective_key,
        "objective_answer_key_status": "draft",
        "objective_answer_key_confirmation_status": "pending_teacher_confirmation",
        "material_questions": [{
            "label": "17", "prompt": "材料背景",
            "sub_questions": [{
                "label": "1", "question_id": "17.1", "parent_question_id": "17",
                "question": "概括材料观点", "reference_answer": "答案甲；答案乙",
                "standard_answer": "答案甲；答案乙", "max_score": None,
                "rubric_draft": [
                    {"label": "答案甲", "score": None, "needs_teacher_confirmation": True},
                    {"label": "答案乙", "score": None, "needs_teacher_confirmation": True},
                ], "needs_teacher_confirmation": True,
            }],
        }],
        "source": {
            "content_source": {"type": "docx", "file_name": "测试.docx", "sha256": "docxhash"},
            "question_source": {"type": "docx", "file_name": "测试.docx", "sha256": "docxhash"},
            "answer_source": {"type": "docx", "file_name": "测试.docx", "sha256": "docxhash", "relation": "same_as_content"},
            "layout_source": None,
            "source_pair_status": pair_status,
            "source_conflicts": conflicts,
        },
        "layout_reference": {"pages": [], "page_roles": []},
        "answer_key_crosscheck": crosscheck,
        "warnings": [], "warning_codes": [], "pdf_semantic_fallback": False,
        "teacher_confirmation_status": "pending_teacher_confirmation",
    }


def confirmation_for(draft, *, teacher=True, source_pair=True, max_score="4", point_scores=("2", "2"), answer="A"):
    major = draft["material_questions"][0]
    sub = major["sub_questions"][0]
    return {
        "exam_name": "测试考试",
        "objective_answer_key": {"1": answer},
        "objective_points": {"1": "3"},
        "answer_conflict_resolutions": {"1": answer} if draft["answer_key_crosscheck"].get("status") == "conflict" else {},
        "material_questions": [{
            "label": major["label"], "prompt": major["prompt"],
            "sub_questions": [{
                "question_id": sub["question_id"], "question": sub["question"],
                "reference_answer": sub["reference_answer"], "max_score": max_score,
                "rubric_draft": [
                    {"label": "答案甲", "score": point_scores[0]},
                    {"label": "答案乙", "score": point_scores[1]},
                ],
            }],
        }],
        "teacher_confirmed": teacher,
        "source_pair_confirmed": source_pair,
    }


class ExamImportUiTests(unittest.TestCase):
    def fake_handler(self, payload):
        replies = []
        handler = SimpleNamespace(
            require_local_client=lambda: True,
            read_json=lambda: payload,
        )
        return handler, replies

    def test_import_endpoint_with_docx_and_pdf_only_writes_draft(self):
        draft = import_draft(pair_status="matched")
        payload = {
            "docx_filename": "测试.docx",
            "docx_data_base64": base64.b64encode(b"docx-bytes").decode("ascii"),
            "pdf_filename": "测试.pdf",
            "pdf_data_base64": base64.b64encode(b"%PDF-test").decode("ascii"),
        }
        handler, replies = self.fake_handler(payload)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(app, "DATA_DIR", Path(temp)), \
                 patch.object(app, "build_exam_import_draft", return_value=draft), \
                 patch.object(app, "_render_exam_import_references"):
                with patch.object(app, "json_response", side_effect=lambda h, body, status=200: replies.append((body, status))):
                    app.AppHandler.import_exam_draft.__wrapped__(handler)
                body, status = replies[0]
                self.assertEqual(status, 200)
                self.assertTrue(body["draft_only"])
                self.assertEqual(body["draft"]["source"]["source_pair_status"], "matched")
                draft_dir = Path(temp) / "exam_import_drafts" / body["draft_id"]
                self.assertTrue((draft_dir / "exam_import_draft.json").is_file())
                self.assertTrue((draft_dir / "content.docx").is_file())
                self.assertTrue((draft_dir / "layout.pdf").is_file())
                self.assertFalse((Path(temp) / "assignments").exists())

    def test_docx_only_and_pdf_only_import_modes_remain_drafts(self):
        cases = [
            ("docx", "DOCX-only.docx", b"docx", "docx", False),
            ("pdf", "PDF-only.pdf", b"%PDF-test", "pdf_semantic_fallback", True),
        ]
        with tempfile.TemporaryDirectory() as temp:
            for mode, filename, data, content_type, fallback in cases:
                with self.subTest(mode=mode):
                    draft = import_draft()
                    draft["pdf_semantic_fallback"] = fallback
                    draft["source"]["content_source"] = {
                        "type": content_type, "file_name": filename, "sha256": f"{mode}-hash"
                    }
                    draft["source"]["layout_source"] = (
                        {"type": "pdf", "file_name": filename, "sha256": "pdf-hash", "page_count": 1}
                        if mode == "pdf" else None
                    )
                    draft["source"]["source_pair_status"] = "not_applicable"
                    if mode == "docx":
                        payload = {
                            "docx_filename": filename,
                            "docx_data_base64": base64.b64encode(data).decode("ascii"),
                        }
                    else:
                        payload = {
                            "pdf_filename": filename,
                            "pdf_data_base64": base64.b64encode(data).decode("ascii"),
                        }
                    handler, replies = self.fake_handler(payload)
                    with patch.object(app, "DATA_DIR", Path(temp) / mode), \
                         patch.object(app, "build_exam_import_draft", return_value=draft), \
                         patch.object(app, "_render_exam_import_references"):
                        with patch.object(app, "json_response", side_effect=lambda h, body, status=200: replies.append((body, status))):
                            app.AppHandler.import_exam_draft.__wrapped__(handler)
                    body, status = replies[0]
                    self.assertEqual(status, 200)
                    self.assertTrue(body["draft_only"])
                    self.assertEqual(body["draft"]["pdf_semantic_fallback"], fallback)
                    self.assertFalse((Path(temp) / mode / "assignments").exists())

    def test_import_reference_rendering_covers_exam_pages_and_blank_answer_sheet(self):
        draft = {
            "source": {"layout_source": {
                "page_count": 8, "exam_pages": [1, 2, 3], "answer_sheet_pages": [4],
                "answer_pages": [5, 6, 7, 8], "reference_pages": [1, 2, 3, 4],
            }},
            "layout_reference": {},
        }
        rendered = []

        def fake_render(_pdf_path, page_number, _dpi, destination, method=None):
            rendered.append((page_number, method))
            Image.new("RGB", (10, 14), "white").save(destination)

        with tempfile.TemporaryDirectory() as temp, \
             patch.object(app, "DATA_DIR", Path(temp)), \
             patch("pdf_renderer.new_reference_method", return_value="pdfium-test"), \
             patch("pdf_renderer.render_pdf_page", side_effect=fake_render):
            references = app._render_exam_import_references(Path("unused.pdf"), "a" * 32, draft)
        self.assertEqual([page for page, _ in rendered], [1, 2, 3, 4])
        self.assertEqual([item["page_number"] for item in references], [1, 2, 3, 4])
        self.assertEqual(draft["layout_reference"]["render_status"], "generated")

    def test_import_reference_rendering_leaves_scoring_answer_pages_out(self):
        draft = {
            "source": {"layout_source": {
                "page_count": 8, "exam_pages": [1], "answer_sheet_pages": [],
                "answer_pages": [2], "reference_pages": [1],
            }},
            "layout_reference": {},
        }
        rendered = []

        def fake_render(_pdf_path, page_number, _dpi, destination, method=None):
            rendered.append(page_number)
            Image.new("RGB", (10, 14), "white").save(destination)

        with tempfile.TemporaryDirectory() as temp, \
             patch.object(app, "DATA_DIR", Path(temp)), \
             patch("pdf_renderer.new_reference_method", return_value="pdfium-test"), \
             patch("pdf_renderer.render_pdf_page", side_effect=fake_render):
            app._render_exam_import_references(Path("unused.pdf"), "b" * 32, draft)
        self.assertEqual(rendered, [1])

    def test_uncertain_pair_requires_explicit_teacher_confirmation(self):
        draft = import_draft(pair_status="needs_teacher_confirmation")
        confirm = confirmation_for(draft, source_pair=False)
        with self.assertRaises(ValueError):
            app._confirmed_import_payload(draft, confirm)

    def test_mismatch_cannot_be_created(self):
        draft = import_draft(pair_status="mismatch")
        with self.assertRaisesRegex(ValueError, "明显不一致"):
            app._confirmed_import_payload(draft, confirmation_for(draft))

    def test_missing_max_score_or_rubric_score_blocks_creation(self):
        draft = import_draft()
        with self.assertRaisesRegex(ValueError, "有效满分"):
            app._confirmed_import_payload(draft, confirmation_for(draft, max_score=""))
        with self.assertRaisesRegex(ValueError, "未填写分值"):
            app._confirmed_import_payload(draft, confirmation_for(draft, point_scores=("2", "")))

    def test_explicit_import_point_bank_preserves_values_reaches_readiness_and_caps(self):
        from auto_crop_double_review_poc import rubric_readiness
        draft = import_draft()
        confirm = confirmation_for(draft, max_score="8")
        sub = confirm["material_questions"][0]["sub_questions"][0]
        sub.update(scoring_mode="point_bank", point_value="2",
                   rubric_draft=[{"label": f"独立要点{i}", "score": "2"} for i in range(5)])
        saved, questions, warnings = app._confirmed_import_payload(draft, confirm)
        q = questions[0]
        self.assertEqual(q["scoring_mode"], "point_bank")
        self.assertEqual(q["point_value"], 2)
        self.assertEqual([p["score"] for p in q["criteria"]], [2]*5)
        self.assertEqual(saved["material_questions"][0]["sub_questions"][0]["scoring_mode"], "point_bank")
        self.assertEqual(warnings, [])
        self.assertEqual(rubric_readiness(questions, teacher_confirmation_status="confirmed_by_teacher",
                        teacher_confirmed_at="2026-10-21T00:00:00Z", required_question_ids=[q["id"]]), (True, []))
        result = app.normalize_sub_question_result({"sub_questions": [{"sub_question_id": q["id"], "criteria": [
            {"criterion_id": p["id"], "decision": "full", "suggested_score": 2, "student_evidence": p["label"]}
            for p in q["criteria"]]}]}, questions)
        self.assertEqual(result["suggested_total"], 8)
        sub["rubric_draft"][0]["score"] = "1"
        with self.assertRaisesRegex(ValueError, "rubric_point_value_mismatch"):
            app._confirmed_import_payload(draft, confirm)
        sub["rubric_draft"][0]["score"] = "2"
        sub["max_score"] = 12
        with self.assertRaisesRegex(ValueError, "不足"):
            app._confirmed_import_payload(draft, confirm)
        sub["scoring_mode"] = "invented"
        with self.assertRaisesRegex(ValueError, "评分方式无效"):
            app._confirmed_import_payload(draft, confirm)

    def test_conflicting_answer_key_requires_explicit_selection(self):
        draft = import_draft(conflict=True)
        confirm = confirmation_for(draft, answer="A")
        confirm["answer_conflict_resolutions"] = {"1": ""}
        with self.assertRaisesRegex(ValueError, "冲突尚未由教师明确裁决"):
            app._confirmed_import_payload(draft, confirm)
        confirm["answer_conflict_resolutions"] = {"1": "A"}
        confirmed, _, _ = app._confirmed_import_payload(draft, confirm)
        self.assertEqual(confirmed["objective_answer_key"]["1"], "A")
        self.assertEqual(confirmed["answer_key_crosscheck"]["status"], "resolved_by_teacher")

    def test_only_confirm_action_creates_assignment_exam_and_provenance(self):
        draft = import_draft()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            draft_id = "a" * 32
            draft_dir = root / "exam_import_drafts" / draft_id
            draft_dir.mkdir(parents=True)
            (draft_dir / "content.docx").write_bytes(b"docx-source")
            (draft_dir / "exam_import_draft.json").write_text(json.dumps(draft), encoding="utf-8")
            payload = {"draft_id": draft_id, "confirmation": confirmation_for(draft)}
            handler, replies = self.fake_handler(payload)
            with patch.object(app, "DATA_DIR", root), \
                 patch.object(app, "DB_PATH", root / "test.sqlite3"), \
                 patch.object(app, "BACKUP_DIR", root / "backups"):
                with patch.object(app, "json_response", side_effect=lambda h, body, status=200: replies.append((body, status))):
                    app.AppHandler.create_imported_exam.__wrapped__(handler)
                response, status = replies[0]
                self.assertEqual(status, 201)
                self.assertTrue(response["provenance"]["created_from_import"])
                self.assertEqual(response["provenance"]["content_source"]["sha256"], "docxhash")
                self.assertTrue((root / response["assignment"]["id"] / "imports" / "content.docx").is_file())
                conn = app.get_db()
                row = conn.execute(
                    "SELECT import_metadata_json, sub_questions_json FROM assignments WHERE id=?",
                    (response["assignment"]["id"],),
                ).fetchone()
                self.assertIsNotNone(row)
                metadata = json.loads(row["import_metadata_json"])
                self.assertTrue(metadata["created_from_import"])
                self.assertTrue(metadata["source"]["content_source"]["stored_path"].endswith("/imports/content.docx"))
                saved_sub_questions = json.loads(row["sub_questions_json"])
                self.assertEqual([item["label"] for item in saved_sub_questions], ["17.1"])
                self.assertEqual(len(saved_sub_questions[0]["criteria"]), 2)
                exam = conn.execute("SELECT id FROM grading_exams WHERE id=? AND assignment_id=?", (response["exam_id"], response["assignment"]["id"])).fetchone()
                self.assertIsNotNone(exam)
                conn.close()
                self.assertTrue((draft_dir / "exam_import_draft.json").is_file())

    def test_confirm_persists_pdf_and_exam_references_as_relative_formal_assets(self):
        draft = import_draft()
        draft_id = "c" * 32
        pdf_bytes = b"%PDF-formal-source"
        pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
        draft["source"]["layout_source"] = {
            "type": "pdf", "source_pdf_file_name": "测试.pdf", "source_pdf_sha256": pdf_hash,
            "file_size": len(pdf_bytes), "page_count": 1, "exam_pages": [1], "answer_pages": [],
        }
        draft["layout_reference"] = {
            "pages": [{
                "page_number": 1, "page_role": "exam", "reference_width": 64, "reference_height": 96,
                "reference_sha256": "", "render_dpi": 150, "render_method": "pdftoppm_150dpi",
                "image_file": f"data/exam_import_drafts/{draft_id}/layout_reference/page-1.png",
            }],
            "page_roles": [{"page_number": 1, "page_role": "exam"}],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            draft_dir = root / "data" / "exam_import_drafts" / draft_id
            reference_path = draft_dir / "layout_reference" / "page-1.png"
            reference_path.parent.mkdir(parents=True)
            Image.new("RGB", (64, 96), "white").save(reference_path, format="PNG")
            draft["layout_reference"]["pages"][0]["reference_sha256"] = hashlib.sha256(reference_path.read_bytes()).hexdigest()
            (draft_dir / "layout.pdf").write_bytes(pdf_bytes)
            (draft_dir / "exam_import_draft.json").write_text(json.dumps(draft), encoding="utf-8")
            handler, replies = self.fake_handler({"draft_id": draft_id, "confirmation": confirmation_for(draft)})
            with patch.object(app, "PORTABLE_ROOT", root), \
                 patch.object(app, "DATA_DIR", root / "data"), \
                 patch.object(app, "DB_PATH", root / "data" / "test.sqlite3"), \
                 patch.object(app, "BACKUP_DIR", root / "backups"):
                with patch.object(app, "json_response", side_effect=lambda h, body, status=200: replies.append((body, status))):
                    app.AppHandler.create_imported_exam.__wrapped__(handler)
                response, status = replies[0]
                self.assertEqual(status, 201)
                conn = app.get_db()
                row = conn.execute("SELECT import_metadata_json FROM assignments WHERE id=?", (response["assignment"]["id"],)).fetchone()
                conn.close()
            metadata = json.loads(row[0])
            layout = metadata["source"]["layout_source"]
            page = metadata["layout_reference"]["pages"][0]
            self.assertEqual(layout["asset_status"], "ready")
            self.assertEqual(layout["source_pdf_sha256"], pdf_hash)
            self.assertEqual(layout["file_size"], len(pdf_bytes))
            self.assertEqual(layout["stored_path"], f"data/exam_assets/{response['exam_id']}/{pdf_hash}/source.pdf")
            self.assertEqual(page["image_file"], f"data/exam_assets/{response['exam_id']}/{pdf_hash}/references/page_001.png")
            self.assertFalse(Path(layout["stored_path"]).is_absolute())
            asset_dir = root / layout["asset_relative_dir"]
            self.assertEqual((asset_dir / "source.pdf").read_bytes(), pdf_bytes)
            self.assertTrue((asset_dir / "references" / "page_001.png").is_file())
            self.assertTrue((asset_dir / "manifest.json").is_file())
            self.assertFalse((root / "data" / response["assignment"]["id"] / "imports" / "layout.pdf").exists())

    def test_formal_pdf_reference_persistence_failure_prevents_exam_creation(self):
        draft = import_draft()
        draft_id = "d" * 32
        pdf_bytes = b"%PDF-formal-source"
        draft["source"]["layout_source"] = {
            "type": "pdf", "source_pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "page_count": 1, "exam_pages": [1],
        }
        draft["layout_reference"] = {"pages": [], "page_roles": []}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            draft_dir = root / "data" / "exam_import_drafts" / draft_id
            draft_dir.mkdir(parents=True)
            (draft_dir / "layout.pdf").write_bytes(pdf_bytes)
            (draft_dir / "exam_import_draft.json").write_text(json.dumps(draft), encoding="utf-8")
            handler, _ = self.fake_handler({"draft_id": draft_id, "confirmation": confirmation_for(draft)})
            with patch.object(app, "PORTABLE_ROOT", root), patch.object(app, "DATA_DIR", root / "data"):
                with self.assertRaisesRegex(ValueError, "reference 未完整生成"):
                    app.AppHandler.create_imported_exam.__wrapped__(handler)
                self.assertFalse(list((root / "data" / "exam_assets").glob("**/source.pdf")) if (root / "data" / "exam_assets").exists() else [])


if __name__ == "__main__":
    unittest.main()
