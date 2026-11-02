from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from exam_import_draft import (
    _compare_question_sources,
    _merge_answer_keys,
    _read_pdf_layout_source,
    build_exam_import_draft,
    layout_reference_page_numbers,
)
from docx_question_import import parse_docx


def make_docx(blocks):
    def para(text):
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<w:p><w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"

    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(para(item) for item in blocks) + "</w:body></w:document>"
    ).encode("utf-8")
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return output.getvalue()


def sample_docx():
    return make_docx([
        "样卷 测试卷",
        "一、单选题",
        "1．先秦政治制度问题的题干内容（ ）", "A．选项甲\tB．选项乙", "C．选项丙\tD．选项丁",
        "2．秦朝地方制度问题的题干内容（ ）", "A．郡县制\tB．分封制", "C．行省制\tD．三省制",
        "3．唐朝制度问题的题干内容及其影响（ ）", "A．丞相制\tB．内阁制", "C．三省六部制\tD．军机处",
        "17．材料题",
        "（1）概括制度特点。",
        "参考答案",
        "题号 | 1 | 2 | 3", "答案 | B | A | C",
        "17．（1）主权在民；轮流执政。",
    ])


def matching_probe():
    stems = {
        "1": "先秦政治制度问题的题干内容",
        "2": "秦朝地方制度问题的题干内容",
        "3": "唐朝制度问题的题干内容及其影响",
    }
    return {
        "source": {"type": "pdf", "source_pdf_file_name": "layout.pdf", "source_pdf_sha256": "pdfhash", "page_count": 2, "exam_pages": [1], "answer_pages": [2]},
        "page_roles": [{"page_number": 1, "page_role": "exam"}, {"page_number": 2, "page_role": "answer"}],
        "text_available": True,
        "objective_ids": ["1", "2", "3"],
        "objective_questions": {qid: {"stem": stem} for qid, stem in stems.items()},
        "subjective_ids": ["17.1"],
        "objective_answer_key": {"1": "B", "2": "A", "3": "C"},
        "warnings": [],
    }


def fake_pdf_reader(pages):
    def reader(*_args, **_kwargs):
        return SimpleNamespace(
            is_encrypted=False,
            pages=[SimpleNamespace(extract_text=lambda text=text: text) for _, text in pages],
        )
    return reader


class ExamImportDraftTests(unittest.TestCase):
    def test_docx_parses_objectives_answers_subjective_answers_and_unscored_rubric(self):
        result = parse_docx(sample_docx())
        self.assertEqual([item["question_id"] for item in result["objective_questions"]], ["1", "2", "3"])
        self.assertEqual(list(result["objective_questions"][0]["options"]), ["A", "B", "C", "D"])
        self.assertEqual(result["objective_answer_key"], {"1": "B", "2": "A", "3": "C"})
        sub = result["material_questions"][0]["sub_questions"][0]
        self.assertEqual(sub["question_id"], "17.1")
        self.assertEqual(sub["max_score"], None)
        self.assertEqual([point["label"] for point in sub["rubric_draft"]], ["主权在民", "轮流执政"])
        self.assertTrue(sub["needs_teacher_confirmation"])
        self.assertTrue(all(point["score"] is None for point in sub["rubric_draft"]))

    def test_same_source_pair_binds_pdf_layout_to_one_docx_draft(self):
        with patch("exam_import_draft._read_pdf_layout_source", return_value=matching_probe()):
            result = build_exam_import_draft(
                docx_bytes=sample_docx(), docx_file_name="paper.docx",
                pdf_bytes=b"%PDF-test", pdf_file_name="layout.pdf",
                pdf_reference_pages=[{"page_number": 1, "reference_width": 1000, "reference_height": 1400, "reference_sha256": "refhash", "render_dpi": 150}],
            )
        self.assertEqual(result["source"]["source_pair_status"], "matched")
        self.assertEqual(result["source"]["content_source"]["type"], "docx")
        self.assertEqual(result["source"]["question_source"]["file_name"], "paper.docx")
        self.assertEqual(result["source"]["layout_source"]["source_pdf_file_name"], "layout.pdf")
        self.assertEqual(result["layout_reference"]["pages"][0]["reference_width"], 1000)
        self.assertEqual(result["answer_key_crosscheck"]["status"], "passed")

    def test_question_id_mismatch_is_exposed(self):
        draft = parse_docx(sample_docx())
        probe = matching_probe()
        probe["objective_ids"] = ["1", "2", "4"]
        relation = _compare_question_sources(draft, probe)
        self.assertEqual(relation["status"], "mismatch")
        self.assertIn("question_id_mismatch", relation["warning_codes"])
        self.assertNotIn("question_count_mismatch", relation["warning_codes"])
        self.assertTrue(relation["conflicts"])

    def test_question_count_mismatch_has_separate_warning(self):
        draft = parse_docx(sample_docx())
        probe = matching_probe()
        probe["objective_ids"] = ["1", "2"]
        relation = _compare_question_sources(draft, probe)
        self.assertIn("question_id_mismatch", relation["warning_codes"])
        self.assertIn("question_count_mismatch", relation["warning_codes"])

    def test_same_question_numbers_without_text_layer_remain_uncertain(self):
        draft = parse_docx(sample_docx())
        probe = matching_probe()
        probe["text_available"] = False
        relation = _compare_question_sources(draft, probe)
        self.assertEqual(relation["status"], "needs_teacher_confirmation")
        self.assertIn("source_pair_uncertain", relation["warning_codes"])

    def test_pdf_conflict_is_preserved_without_selecting_either_value(self):
        merged, comparison, sources, conflicts, warnings = _merge_answer_keys(
            {"1": "A"}, {"1": "B", "2": "C"}
        )
        self.assertEqual(merged, {"2": "C"})
        self.assertEqual(sources, {"2": "pdf_fallback"})
        self.assertEqual(comparison["status"], "conflict")
        self.assertEqual(comparison["docx_answer_key"]["1"], "A")
        self.assertEqual(conflicts[0]["pdf_answer"], "B")
        self.assertTrue(warnings)

    def test_pair_conflict_keeps_both_answer_keys_and_emits_warning(self):
        probe = matching_probe()
        probe["objective_answer_key"]["1"] = "C"
        with patch("exam_import_draft._read_pdf_layout_source", return_value=probe):
            result = build_exam_import_draft(
                docx_bytes=sample_docx(), docx_file_name="paper.docx",
                pdf_bytes=b"%PDF-test", pdf_file_name="layout.pdf",
            )
        self.assertNotIn("1", result["objective_answer_key"])
        question = next(item for item in result["objective_questions"] if item["question_id"] == "1")
        self.assertIsNone(question["correct_answer_draft"])
        self.assertFalse(question["answer_found"])
        self.assertEqual(result["answer_key_crosscheck"]["status"], "conflict")
        self.assertEqual(result["answer_key_crosscheck"]["docx_answer_key"]["1"], "B")
        self.assertEqual(result["answer_key_crosscheck"]["pdf_answer_key"]["1"], "C")
        self.assertIn("answer_key_conflict", result["warning_codes"])
        self.assertTrue(any(item.get("type") == "answer_key_conflict" for item in result["source"]["source_conflicts"]))

    def test_unmatched_pdf_key_is_not_merged_into_docx_draft(self):
        probe = matching_probe()
        probe["objective_ids"] = ["1", "2", "4"]
        docx_draft = parse_docx(sample_docx())
        docx_draft["objective_answer_key"].pop("3")
        with patch("exam_import_draft.parse_docx", return_value=docx_draft), patch(
            "exam_import_draft._read_pdf_layout_source", return_value=probe
        ):
            result = build_exam_import_draft(
                docx_bytes=sample_docx(), docx_file_name="paper.docx",
                pdf_bytes=b"%PDF-test", pdf_file_name="other-layout.pdf",
            )
        self.assertNotIn("3", result["objective_answer_key"])
        self.assertEqual(result["answer_key_crosscheck"]["status"], "source_pair_mismatch")
        self.assertEqual(result["answer_key_crosscheck"]["pdf_answer_key"]["3"], "C")

    def test_docx_only_keeps_working_draft_and_warns_layout_missing(self):
        result = build_exam_import_draft(docx_bytes=sample_docx(), docx_file_name="paper.docx")
        self.assertEqual(result["status"], "draft")
        self.assertIsNone(result["source"]["layout_source"])
        self.assertIn("layout_source_missing", result["warning_codes"])
        self.assertFalse(result["pdf_semantic_fallback"])

    def test_pdf_only_uses_semantic_fallback(self):
        fallback = {
            "status": "draft", "title": "PDF sample", "objective_questions": [],
            "objective_answer_key": {}, "material_questions": [], "warnings": [],
            "source": {"source_pdf_file_name": "fallback.pdf", "source_pdf_sha256": "pdfhash", "page_count": 2, "exam_pages": [1], "answer_pages": [2]},
            "page_roles": [{"page_number": 1, "page_role": "exam"}],
        }
        with patch("exam_import_draft.parse_pdf_question_import", return_value=fallback), patch(
            "exam_import_draft._read_pdf_layout_source", return_value=matching_probe()
        ):
            result = build_exam_import_draft(pdf_bytes=b"%PDF-test", pdf_file_name="fallback.pdf")
        self.assertTrue(result["pdf_semantic_fallback"])
        self.assertEqual(result["source"]["source_pair_status"], "not_applicable")
        self.assertEqual(result["source"]["layout_source"]["source_pdf_file_name"], "layout.pdf")

    def test_pdf_without_text_layer_stops_safely(self):
        fallback = {"status": "error", "error_code": "pdf_text_unavailable", "source": {"page_count": 1}, "page_roles": [{"page_number": 1, "page_role": "unknown"}], "warnings": ["no text"]}
        with patch("exam_import_draft.parse_pdf_question_import", return_value=fallback):
            result = build_exam_import_draft(pdf_bytes=b"%PDF-test", pdf_file_name="scan.pdf")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "pdf_text_unavailable")

    def test_pdf_layout_probe_isolates_answer_sheet_and_keeps_first_duplicate(self):
        pages = [
            (1, "一、选择题\n1．首次题干内容用于同源比较（ ）\nA．甲 B．乙 C．丙 D．丁\n"
                "1．被覆盖的重复题干内容（ ）\nA．甲 B．乙 C．丙 D．丁\n"
                "2．第二题干内容用于同源比较（ ）\nA．甲 B．乙 C．丙 D．丁"),
            (2, "答 题 卡\n一、选择题\n题号 1 2\n答案"),
            (3, "测试试题评分参考\n一、选择题\n题号 1 2\n答案 B C"),
        ]
        with patch("pypdf.PdfReader", fake_pdf_reader(pages)):
            probe = _read_pdf_layout_source(b"%PDF-synthetic", "synthetic.pdf")
        self.assertEqual(probe["source"]["exam_pages"], [1])
        self.assertEqual(probe["source"]["answer_sheet_pages"], [2])
        self.assertEqual(probe["source"]["answer_pages"], [3])
        self.assertEqual(probe["source"]["reference_pages"], [1, 2])
        self.assertEqual(probe["objective_ids"], ["1", "2"])
        self.assertTrue(probe["objective_questions"]["1"]["stem"].startswith("首次题干内容"))
        self.assertEqual(probe["objective_answer_key"], {"1": "B", "2": "C"})
        self.assertTrue(any("重复识别到客观题号" in warning for warning in probe["warnings"]))
        self.assertTrue(any("answer_sheet_isolated" in warning for warning in probe["warnings"]))

    def test_layout_reference_pages_include_blank_answer_sheet_only(self):
        self.assertEqual(layout_reference_page_numbers({"exam_pages": [1, 2, 3], "answer_sheet_pages": [4], "answer_pages": [5, 6]}), [1, 2, 3, 4])
        self.assertEqual(layout_reference_page_numbers({"exam_pages": [1, 2]}), [1, 2])
        self.assertEqual(layout_reference_page_numbers({"reference_pages": [2, 1, "3", None]}), [1, 2, 3])
        self.assertEqual(layout_reference_page_numbers(None), [])

    def test_question_text_mismatch_carries_real_comparison_evidence(self):
        draft = parse_docx(sample_docx())
        probe = matching_probe()
        for question_id in ("1", "2", "3"):
            probe["objective_questions"][question_id]["stem"] = f"另一份完全不同的试卷题干内容{question_id}"
        relation = _compare_question_sources(draft, probe)
        self.assertEqual(relation["status"], "mismatch")
        self.assertIn("source_pair_mismatch", relation["warning_codes"])
        conflict = next(item for item in relation["conflicts"] if item["type"] == "question_text_mismatch")
        self.assertEqual(conflict["compared"], 3)
        self.assertEqual(conflict["matched"], 0)
        self.assertEqual(conflict["compared_ids"], ["1", "2", "3"])
        self.assertEqual(conflict["unmatched_ids"], ["1", "2", "3"])
        # No fabricated Word/PDF-only lists for a text mismatch; the frontend
        # must render the real compared/matched counts instead of "Word —；PDF —".
        self.assertNotIn("docx_only", conflict)
        self.assertNotIn("pdf_only", conflict)

    def test_question_id_mismatch_conflict_has_readable_section_label(self):
        draft = parse_docx(sample_docx())
        probe = matching_probe()
        probe["objective_ids"] = ["1", "2", "4"]
        relation = _compare_question_sources(draft, probe)
        conflict = next(item for item in relation["conflicts"] if item["type"] == "question_id_mismatch")
        self.assertEqual(conflict["label"], "客观题")
        self.assertEqual(conflict["docx_only"], ["3"])
        self.assertEqual(conflict["pdf_only"], ["4"])

    def test_no_exam_pages_in_pdf_remains_uncertain_instead_of_mismatch(self):
        draft = parse_docx(sample_docx())
        probe = matching_probe()
        probe["source"]["exam_pages"] = []
        probe["objective_ids"] = []
        probe["objective_questions"] = {}
        probe["subjective_ids"] = []
        relation = _compare_question_sources(draft, probe)
        self.assertEqual(relation["status"], "needs_teacher_confirmation")
        self.assertIn("source_pair_uncertain", relation["warning_codes"])
        self.assertNotIn("question_id_mismatch", relation["warning_codes"])

    def test_genuinely_different_documents_are_still_blocked(self):
        pages = [
            (1, "一、选择题\n1．另一份试卷的完全不同题干内容（ ）\nA．甲 B．乙 C．丙 D．丁\n"
                "2．另一份试卷的另一个不同题干（ ）\nA．甲 B．乙 C．丙 D．丁\n"
                "3．另一份试卷的第三个不同题干（ ）\nA．甲 B．乙 C．丙 D．丁"),
            (2, "测试试题评分参考\n一、选择题\n题号 1 2 3\n答案 A A A"),
        ]
        with patch("pypdf.PdfReader", fake_pdf_reader(pages)):
            result = build_exam_import_draft(
                docx_bytes=sample_docx(), docx_file_name="paper.docx",
                pdf_bytes=b"%PDF-synthetic", pdf_file_name="other.pdf",
            )
        self.assertEqual(result["source"]["source_pair_status"], "mismatch")
        self.assertIn("source_pair_mismatch", result["warning_codes"])
        self.assertEqual(result["answer_key_crosscheck"]["status"], "source_pair_mismatch")
        self.assertEqual(result["answer_key_crosscheck"]["docx_answer_key"], {"1": "B", "2": "A", "3": "C"})
        self.assertEqual(result["answer_key_crosscheck"]["pdf_answer_key"], {"1": "A", "2": "A", "3": "A"})
        self.assertEqual(result["objective_answer_key"], {"1": "B", "2": "A", "3": "C"})


if __name__ == "__main__":
    unittest.main()
