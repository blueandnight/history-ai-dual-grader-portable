import base64
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import app
from docx_question_import import _parse_objective_question, parse_docx, rubric_candidates_from_reference


def make_docx(paragraphs):
    def paragraph(text):
        escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        return f"<w:p><w:r><w:t>{escaped}</w:t></w:r></w:p>"

    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(paragraph(item) for item in paragraphs) + "</w:body></w:document>"
    ).encode("utf-8")
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


class DocxQuestionImportTests(unittest.TestCase):
    def setUp(self):
        self.docx = make_docx([
            "历史材料题测试卷",
            "9．阅读材料，完成下列要求。（10分）材料：明中期土地兼并严重。",
            "（1）概括一条鞭法实施的背景。（4分）",
            "（2）简析一条鞭法的作用。（6分）",
            "参考答案",
            "9．（1）背景：土地兼并严重；农民负担加重。（2）作用：增加财政收入；减轻人身控制。",
        ])

    def test_parses_material_question_and_reference_answers_as_editable_draft(self):
        result = parse_docx(self.docx)
        self.assertEqual(result["title"], "历史材料题测试卷")
        self.assertEqual(len(result["material_questions"]), 1)
        question = result["material_questions"][0]
        self.assertEqual(question["label"], "9")
        self.assertEqual(len(question["sub_questions"]), 2)
        first, second = question["sub_questions"]
        self.assertEqual(first["max_score"], 4.0)
        self.assertIn("土地兼并严重", first["standard_answer"])
        self.assertEqual(second["max_score"], 6.0)
        self.assertIn("增加财政收入", second["standard_answer"])
        self.assertEqual(first["criteriaText"], "")
        self.assertEqual(first["question_id"], "9.1")
        self.assertEqual(first["reference_answer"], first["standard_answer"])
        self.assertTrue(first["needs_teacher_confirmation"])
        self.assertEqual(result["objective_answer_key_status"], "draft")
        self.assertIn("评分点不会由解析器自动编造", "".join(result["warnings"]))

    def test_rejects_bad_or_oversized_upload(self):
        with self.assertRaisesRegex(ValueError, "docx_size_invalid"):
            parse_docx(b"")
        with self.assertRaisesRegex(ValueError, "docx_format_invalid"):
            parse_docx(b"not-a-docx")

    def test_section_heading_after_last_choice_is_not_added_to_option_d(self):
        question = _parse_objective_question(
            "16",
            ["16．题干（ ） A．甲 B．乙 C．丙 D．丁", "二、材料题"],
            {"16": "B"},
        )
        self.assertEqual(question["options"]["D"], "丁")

    def test_rubric_candidates_strip_scoring_notes_and_keep_historical_text(self):
        reference = (
            "汉武帝颁布推恩令（2分）\n"
            "2分：加强中央集权\n"
            "西汉（公元前202年）建立\n"
            "地方治理沿袭秦制（郡县制）\n"
            "（6分，每点2分，任答3点即可）"
        )
        candidates, status = rubric_candidates_from_reference("17.1", reference)
        self.assertEqual(status, "candidate_points_draft")
        self.assertEqual([item["label"] for item in candidates], [
            "汉武帝颁布推恩令",
            "加强中央集权",
            "西汉（公元前202年）建立",
            "地方治理沿袭秦制（郡县制）",
        ])
        self.assertTrue(all(item["score"] is None and item["needs_teacher_confirmation"] for item in candidates))
        self.assertIn("（2分）", reference)
        self.assertIn("任答3点即可", reference)

    def test_score_cleanup_preserves_substantive_conditionals_and_parentheses(self):
        reference = '君主专制（只要君主掌握权力即可）\n任选官员体现任官权\n2分之一的土地\n加强中央集权（任答3点即可）'
        candidates, _ = rubric_candidates_from_reference('17.1', reference)
        self.assertEqual([item['label'] for item in candidates], [
            '君主专制（只要君主掌握权力即可）', '任选官员体现任官权', '2分之一的土地', '加强中央集权'])

    def test_blank_answer_sheet_and_scoring_reference_are_separated(self):
        docx = make_docx([
            "合成试题卷",
            "一、选择题",
            "1．合成题干一（ ）", "A．甲 B．乙 C．丙 D．丁",
            "2．合成题干二（ ）", "A．甲 B．乙 C．丙 D．丁",
            "二、非选择题",
            "3．阅读材料，完成下列要求。（10分）", "材料：合成材料。",
            "（1）概括特点。（4分）", "（2）分析原因。（6分）",
            "答 题 卡",
            "一、选择题", "题号 | 1 | 2", "答案 |  | ",
            "二、非选择题", "3．（1） / （2）",
            "测试试题评分参考",
            "一、选择题", "题号 | 1 | 2", "答案 | B | C",
            "3．【答案】", "（1）特点：分层管理。（4分）", "（2）原因：技术发展。（6分）",
            "【解析】解释不是答案。",
        ])
        result = parse_docx(docx)
        self.assertEqual([q["question_id"] for q in result["objective_questions"]], ["1", "2"])
        self.assertEqual(result["objective_answer_key"], {"1": "B", "2": "C"})
        self.assertTrue(all(q["answer_found"] for q in result["objective_questions"]))
        subs = [s for major in result["material_questions"] for s in major["sub_questions"]]
        self.assertEqual([s["question_id"] for s in subs], ["3.1", "3.2"])
        self.assertEqual([s["max_score"] for s in subs], [4.0, 6.0])
        self.assertIn("分层管理", subs[0]["reference_answer"])
        self.assertIn("技术发展", subs[1]["reference_answer"])
        self.assertNotIn("解释不是答案", subs[1]["reference_answer"])
        self.assertGreater(result["source_regions"]["answer_sheet_blocks"], 0)
        self.assertGreater(result["source_regions"]["answer_blocks"], 0)
        self.assertTrue(any("answer_sheet_isolated" in warning for warning in result["warnings"]))

    def test_independent_answer_region_titles_are_recognized(self):
        for answer_title in ("参考答案", "答案与解析", "测试试题评分参考", "参考答案及评分标准", "评分参考"):
            with self.subTest(answer_title=answer_title):
                result = parse_docx(make_docx([
                    "合成试题卷", "一、选择题", "1．合成题干（ ）", "A．甲 B．乙 C．丙 D．丁",
                    answer_title, "题号 | 1", "答案 | B", "1．【答案】B【解析】解释不是答案。",
                ]))
                self.assertEqual(result["objective_answer_key"], {"1": "B"})
                self.assertEqual(len(result["material_questions"]), 0)

    def test_answer_sheet_heading_inside_a_sentence_is_not_a_boundary(self):
        result = parse_docx(make_docx([
            "合成试题卷", "请将答案写在答题卡上，不得折叠。",
            "一、选择题", "1．合成题干（ ）", "A．甲 B．乙 C．丙 D．丁",
            "参考答案", "题号 | 1", "答案 | B",
        ]))
        self.assertEqual([q["question_id"] for q in result["objective_questions"]], ["1"])
        self.assertEqual(result["source_regions"]["answer_sheet_blocks"], 0)
        self.assertEqual([q["question_id"] for q in result["objective_questions"]], ["1"])

    def test_repeated_question_number_in_answer_region_is_not_imported_twice(self):
        result = parse_docx(make_docx([
            "合成试题卷", "一、选择题", "1．合成题干一（ ）", "A．甲 B．乙 C．丙 D．丁",
            "2．合成题干二（ ）", "A．甲 B．乙 C．丙 D．丁",
            "二、非选择题", "3．阅读材料，完成下列要求。（10分）", "材料：合成材料。",
            "（1）概括特点。（4分）", "（2）分析原因。（6分）",
            "参考答案",
            "3．（1）特点：分层管理。（4分）", "（2）原因：技术发展。（6分）",
        ]))
        subs = [s for major in result["material_questions"] for s in major["sub_questions"]]
        self.assertEqual([s["question_id"] for s in subs], ["3.1", "3.2"])
        self.assertEqual([q["question_id"] for q in result["objective_questions"]], ["1", "2"])

    def test_local_import_endpoint_returns_draft_without_model_call(self):
        payload = {
            "filename": "history.docx",
            "data_base64": base64.b64encode(self.docx).decode("ascii"),
        }
        fake_handler = SimpleNamespace(
            require_local_client=lambda: True,
            read_json=lambda: payload,
        )
        replies = []
        with patch.object(app, "json_response", side_effect=lambda handler, body, status=200: replies.append((body, status))):
            app.AppHandler.import_docx_questions.__wrapped__(fake_handler)
        self.assertEqual(len(replies), 1)
        body, status = replies[0]
        self.assertEqual(status, 200)
        self.assertTrue(body["draft_only"])
        self.assertEqual(body["imported"]["material_questions"][0]["label"], "9")

    def test_import_endpoint_rejects_legacy_doc_without_parsing(self):
        fake_handler = SimpleNamespace(
            require_local_client=lambda: True,
            read_json=lambda: {"filename": "history.doc", "data_base64": "AA=="},
        )
        with patch.object(app, "json_response"):
            with self.assertRaisesRegex(ValueError, "旧式 .doc 不支持"):
                app.AppHandler.import_docx_questions.__wrapped__(fake_handler)


if __name__ == "__main__":
    unittest.main()
