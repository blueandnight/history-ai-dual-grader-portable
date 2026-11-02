from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from pdf_question_import import (
    _answer_sections,
    _parse_objective_answer_key,
    _parse_objective_question,
    _rubric_candidates,
    classify_page_role,
    parse_pdf_question_import,
    resolve_page_roles,
)


def fake_pdf_reader(pages):
    def reader(*_args, **_kwargs):
        return SimpleNamespace(
            is_encrypted=False,
            pages=[SimpleNamespace(extract_text=lambda text=text: text) for _, text in pages],
        )
    return reader


class PdfQuestionImportTests(unittest.TestCase):
    def test_scoring_reference_on_answer_sheet_page_never_becomes_layout_reference(self):
        from exam_import_draft import _read_pdf_layout_source
        for heading in ("答题卡", "历史试卷第 1页，共2页\n答题卡"):
            mixed = heading + "\n测试试题评分参考\n一、选择题\n1．【答案】A【解析】" + "解析内容" * 25
            self.assertEqual(classify_page_role(mixed), "answer")
            self.assertEqual(resolve_page_roles([(1, mixed), (2, "后续解析")])[1]["page_role"], "answer")
            with patch("pypdf.PdfReader", fake_pdf_reader([(1, mixed)])):
                probe = _read_pdf_layout_source(b"%PDF-synthetic", "mixed.pdf")
            self.assertEqual(probe["source"]["reference_pages"], [])
            self.assertEqual(probe["source"]["answer_pages"], [1])

    def test_explicit_page_roles_and_unknown(self):
        self.assertEqual(classify_page_role("历史试卷第 2页，共4页\n题目"), "exam")
        self.assertEqual(classify_page_role("历史答案第 4页，共5页\n答案内容"), "answer")
        self.assertEqual(classify_page_role("附录内容，没有页角色标记"), "unknown")

    def test_objective_answer_table(self):
        text = "题号 1 2 3 4\n答案 B A D C\n题号 5 6\n答案 A B"
        answers, warnings = _parse_objective_answer_key(text)
        self.assertEqual(answers, {"1": "B", "2": "A", "3": "D", "4": "C", "5": "A", "6": "B"})
        self.assertEqual(warnings, [])

    def test_subjective_answer_sections_keep_only_concise_answer(self):
        sections = _answer_sections(
            "17．(1)表现：秦设丞相；唐设三省六部。\n(2)特征：主权在民，轮流执政。\n"
            "【知识点】后续解析不属于参考答案。\n18．(1)制定政策法规。"
        )
        self.assertIn("秦设丞相", sections["17"]["1"])
        self.assertNotIn("后续解析", sections["17"]["2"])
        self.assertEqual(sections["18"]["1"], "制定政策法规")

    def test_rubric_candidates_are_unscored_and_require_confirmation(self):
        candidates, status = _rubric_candidates("17.2", "特征：主权在民，轮流执政")
        self.assertEqual([item["label"] for item in candidates], ["主权在民", "轮流执政"])
        self.assertTrue(all(item["score"] is None for item in candidates))
        self.assertTrue(all(item["needs_teacher_confirmation"] for item in candidates))
        self.assertEqual(status, "candidate_points_draft")

    def test_sentence_boundary_splits_distinct_subjective_candidates(self):
        candidates, status = _rubric_candidates(
            "18.2",
            "评价：从后世价值看可提供借鉴。从历史局限看仍有不足。",
        )
        self.assertEqual(len(candidates), 2)
        self.assertEqual(status, "candidate_points_draft")

    def test_pdf_without_text_layer_returns_safe_error(self):
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        output = BytesIO()
        writer.write(output)
        result = parse_pdf_question_import(output.getvalue(), "blank.pdf")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "pdf_text_unavailable")
        self.assertEqual(result["page_roles"], [{"page_number": 1, "page_role": "unknown"}])

    def test_scoring_reference_title_is_answer_region_and_answer_sheet_is_isolated(self):
        pages = [
            (1, "一、选择题\n1．合成题干一（ ）\nA．甲 B．乙 C．丙 D．丁"),
            (2, "答 题 卡\n一、选择题\n题号 1\n答案\n二、非选择题\n2．（1） /"),
            (3, "测试试题评分参考\n一、选择题\n题号 1\n答案 B\n1．【答案】B【解析】解释不是答案。"),
            (4, "二、非选择题\n3．【答案】（1）分层管理。（4分）"),
        ]
        roles = {item["page_number"]: item["page_role"] for item in resolve_page_roles(pages)}
        self.assertEqual(roles, {1: "exam", 2: "answer_sheet", 3: "answer", 4: "answer"})
        self.assertEqual(classify_page_role("测试试题评分参考"), "answer")
        self.assertEqual(classify_page_role("答 题 卡"), "answer_sheet")
        self.assertEqual(classify_page_role("请将答案写在答题卡上，不得折叠。"), "unknown")

    def test_section_heading_after_final_choice_is_not_an_option(self):
        question = _parse_objective_question(
            "16",
            "合成题干（ ）\nA．甲\nB．乙\nC．丙\nD．丁\n二、非选择题",
            None,
        )
        self.assertEqual(question["options"]["D"], "丁")
        self.assertTrue(question["needs_teacher_review"] is False)

    def test_pdf_exam_answer_sheet_and_scoring_reference_are_parsed_separately(self):
        pages = [
            (1, "合成试题卷\n一、选择题\n"
                "1．合成题干一用于文本核对（ ）\nA．甲 B．乙 C．丙 D．丁\n"
                "2．合成题干二用于文本核对（ ）\nA．甲 B．乙 C．丙 D．丁\n"
                "二、非选择题\n3．阅读材料，完成下列要求。（10分）\n材料：合成材料。\n"
                "（1）概括特点。（4分）\n（2）分析原因。（6分）"),
            (2, "答 题 卡\n一、选择题\n题号 1 2\n答案\n二、非选择题\n3．（1） / （2）"),
            (3, "测试试题评分参考\n一、选择题\n题号 1 2\n答案 B C\n"
                "3．【答案】\n（1）特点：分层管理。（4分）\n（2）原因：技术发展。（6分）\n【解析】解释不是答案。"),
        ]
        with patch("pypdf.PdfReader", fake_pdf_reader(pages)):
            draft = parse_pdf_question_import(b"%PDF-synthetic", "synthetic.pdf")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["source"]["exam_pages"], [1])
        self.assertEqual(draft["source"]["answer_sheet_pages"], [2])
        self.assertEqual(draft["source"]["answer_pages"], [3])
        self.assertEqual(draft["source"]["reference_pages"], [1, 2])
        self.assertEqual([q["question_id"] for q in draft["objective_questions"]], ["1", "2"])
        self.assertEqual(draft["objective_answer_key"], {"1": "B", "2": "C"})
        subs = [s for m in draft["material_questions"] for s in m["sub_questions"]]
        self.assertEqual([s["question_id"] for s in subs], ["3.1", "3.2"])
        self.assertEqual([s["max_score"] for s in subs], [4.0, 6.0])
        self.assertIn("分层管理", subs[0]["reference_answer"])
        self.assertIn("技术发展", subs[1]["reference_answer"])
        self.assertNotIn("解释不是答案", subs[1]["reference_answer"])


if __name__ == "__main__":
    unittest.main()
