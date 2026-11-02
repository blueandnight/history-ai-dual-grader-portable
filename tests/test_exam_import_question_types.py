"""Number-independent import classification with synthetic document content only."""
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from docx_question_import import parse_docx, question_kinds, reference_sections
from pdf_question_import import parse_pdf_question_import, resolve_page_roles
from exam_import_draft import build_exam_import_draft
from tests.test_docx_question_import import make_docx


class GenericExamQuestionTypeTests(unittest.TestCase):
    def setUp(self):
        self.exam = ["合成试题", "一、单选题"]
        self.exam += [f"{i}．合成的制度比较题干用于测试文本对应。 A．甲 B．乙 C．丙 D．丁" for i in range(1, 27)]
        self.exam += ["二、材料题", "27、材料：合成材料说明某地制度发生变化。", "根据材料，概括制度特点。（8分）",
                      "28、材料：合成材料说明技术发展。", "问题：结合材料分析原因。（8分）"]
        self.answers = ["参考答案", "题号 | " + " | ".join(str(i) for i in range(1, 27)),
                        "答案 | " + " | ".join('A' for _ in range(26)),
                        "27．【答案】特点：分层管理；职责明确。\n【解析】不应成为参考答案的解释。",
                        "28、解析：此句不是答案。", "参考答案：", "原因：技术发展；制度支持。", "评分标准：每点2分。"]
        self.pages = [(1, "\n".join(self.exam[:15])), (2, "\n".join(self.exam[15:])),
                      (3, "\n".join(self.answers[:4]).replace(' | ', ' ')),
                      (4, "\n".join(self.answers[4:]))]

    def pdf_reader(self, *_args, **_kwargs):
        return SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda text=text: text) for _, text in self.pages])

    def check_draft(self, draft):
        self.assertEqual([q['question_id'] for q in draft['objective_questions']], [str(i) for i in range(1, 27)])
        self.assertTrue(all(not q['needs_teacher_review'] for q in draft['objective_questions']))
        self.assertEqual([q['label'] for q in draft['material_questions']], ['27', '28'])
        subs = [s for q in draft['material_questions'] for s in q['sub_questions']]
        self.assertEqual([s['question_id'] for s in subs], ['27.1', '28.1'])
        self.assertEqual([s['max_score'] for s in subs], [8, 8])
        self.assertTrue(all(s['answer_found'] and s['needs_teacher_confirmation'] for s in subs))
        self.assertNotIn('不是答案', subs[1]['reference_answer'])
        self.assertNotIn('评分标准', subs[1]['reference_answer'])
        self.assertNotIn('不应成为参考答案', subs[0]['reference_answer'])
        self.assertTrue(all(p['score'] is None for s in subs for p in s['rubric_draft']))

    def test_docx_twenty_six_choices_and_two_unnumbered_material_tasks(self):
        self.check_draft(parse_docx(make_docx(self.exam + self.answers)))

    def test_pdf_and_continuation_pages_are_not_bound_to_seventeen_eighteen(self):
        with patch('pypdf.PdfReader', self.pdf_reader):
            draft = parse_pdf_question_import(b'%PDF-synthetic', 'synthetic.pdf')
        self.check_draft(draft)
        self.assertEqual(draft['source']['exam_pages'], [1, 2])
        self.assertEqual(draft['source']['answer_pages'], [3, 4])

    def test_docx_pdf_pair_uses_same_variable_question_contract(self):
        with patch('pypdf.PdfReader', self.pdf_reader):
            draft = build_exam_import_draft(docx_bytes=make_docx(self.exam + self.answers), docx_file_name='synthetic.docx', pdf_bytes=b'%PDF-synthetic', pdf_file_name='synthetic.pdf')
        self.check_draft(draft)
        self.assertEqual(draft['source']['source_pair_status'], 'matched')
        self.assertEqual(draft['teacher_confirmation_status'], 'pending_teacher_confirmation')

    def test_no_early_stop_after_first_subjective_and_choice_statements_are_not_subquestions(self):
        blocks = ['1．选择题', '（1）陈述甲', '（2）陈述乙', 'A．甲 B．乙 C．丙 D．丁',
                  '2．阅读材料，回答问题。', '（1）概括特点。（4分）',
                  '3．选择另一正确项。 A．甲 B．乙 C．丙 D．丁']
        draft = parse_docx(make_docx(blocks))
        self.assertEqual([q['question_id'] for q in draft['objective_questions']], ['1', '3'])
        self.assertEqual([q['label'] for q in draft['material_questions']], ['2'])

    def test_explicit_material_section_takes_priority_over_quoted_letter_markers(self):
        kinds = question_kinds(['二、材料题', '5．材料记载 A．某项制度 B．另一制度。', '根据材料说明变化。（6分）'])
        self.assertEqual(kinds['5'], 'subjective')

    def test_unknown_pages_and_incomplete_choices_are_not_fabricated(self):
        self.assertEqual(resolve_page_roles([(1, '仅有附录内容')])[0]['page_role'], 'unknown')
        draft = parse_docx(make_docx(['一、单选题', '1．不完整题干。', 'B．乙 C．丙 D．丁']))
        self.assertTrue(draft['objective_questions'][0]['needs_teacher_review'])
        self.assertNotIn('A', draft['objective_questions'][0]['options'])
        self.assertEqual(question_kinds(['1．无题型依据的文字']), {'1':'unknown'})

    def test_explicit_word_list_numbering_restores_option_but_numid_zero_does_not(self):
        text = make_docx(['一、单选题', '1．合成题干。', '甲', 'B．乙 C．丙 D．丁', '参考答案', '题号 | 1', '答案 | A'])
        with ZipFile(BytesIO(text)) as z:
            xml = z.read('word/document.xml').decode()
        xml = xml.replace('<w:p><w:r><w:t>甲</w:t>', '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr></w:pPr><w:r><w:t>甲</w:t>')
        numbering = '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="upperLetter"/><w:lvlText w:val="%1．"/></w:lvl></w:abstractNum><w:num w:numId="7"><w:abstractNumId w:val="1"/></w:num></w:numbering>'
        def document(body):
            out=BytesIO()
            with ZipFile(out,'w') as z:
                z.writestr('word/document.xml',body);z.writestr('word/numbering.xml',numbering)
            return out.getvalue()
        question = parse_docx(document(xml))['objective_questions'][0]
        self.assertEqual(list(question['options']), ['A','B','C','D'])
        self.assertEqual(question['options']['A'], '甲')
        self.assertFalse(question['needs_teacher_review'])
        self.assertTrue(parse_docx(document(xml.replace('numId w:val="7"','numId w:val="0"')))['objective_questions'][0]['needs_teacher_review'])

    def test_old_numbered_parts_and_reference_boundaries_still_work(self):
        sections = reference_sections('9．（1）特点一。\n（2）特点二。\n【解析】解释。\n10．【答案】概括内容。')
        self.assertEqual(sections['9'], {'1':'特点一','2':'特点二'})
        self.assertEqual(sections['10'], {'1':'概括内容'})


if __name__ == '__main__':
    unittest.main()
