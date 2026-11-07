"""Entirely synthetic Word/table fixture, no original examination or models."""
import copy
import io
import unittest
from pathlib import Path
from zipfile import ZipFile
from xml.sax.saxutils import escape

import app
from docx_question_import import parse_docx, _question_number
from holistic_draft import validate, simulate
from tests.test_exam_import_ui import import_draft, confirmation_for


def synthetic_document():
    def p(text):return '<w:p><w:r><w:t>'+escape(text)+'</w:t></w:r></w:p>'
    texts=['合成历史试卷','19题 历史小论文（12分）','根据表格，自选观点进行论证。']
    table='<w:tbl>'+''.join('<w:tr>'+''.join('<w:tc>'+p(c)+'</w:tc>' for c in row)+'</w:tr>' for row in [('时间','制度'),('阶段甲','制度甲'),('阶段乙','制度乙')])+'</w:tbl>'
    xml=''.join(map(p,texts))+table+''.join(map(p,[
        '19．【答案】','参考范例一：观点甲，史实甲，论证甲。','参考范例二：观点乙，史实乙，论证乙。',
        '评分标准：观点、史实、论证和逻辑综合评价。',
        '第20题 阅读材料（12分）','材料：合成展览有四处史实错误。',
        '（1）任选两处错误纠正。（4分）','注意区分史实和解释。',
        '（2）设计展览，撰写序言或结束语。（8分）',
        '20．【答案】','（1）四处可选：纠错甲；纠错乙；纠错丙；纠错丁。任选两处，每处2分。',
        '（2）可撰写序言或结束语。评分标准：主题明确、史实准确、叙述连贯，共8分。']))
    buffer=io.BytesIO()
    with ZipFile(buffer,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+xml+'</w:body></w:document>')
    return buffer.getvalue()


class ComplexExamTests(unittest.TestCase):
    def test_number_forms_and_pdf_shared_prefix(self):
        from pdf_question_import import _numbered_groups
        for prefix in ('19.','19．','19、','19题','第19题'):
            self.assertEqual(_question_number(prefix+' 设问'),('19','设问'))
            self.assertEqual(_numbered_groups(prefix+' 设问')[0][:2],('19','设问'))

    def test_interleaved_answers_table_examples_and_multiline(self):
        d=parse_docx(synthetic_document());a,b=d['material_questions']
        self.assertEqual([a['label'],b['label']],['19','20'])
        self.assertEqual([q['question_id'] for m in (a,b) for q in m['sub_questions']],['19.1','20.1','20.2'])
        self.assertEqual([q['max_score'] for m in (a,b) for q in m['sub_questions']],[12,4,8])
        self.assertEqual(a['sub_questions'][0]['scoring_mode'],'holistic')
        self.assertEqual(a['sub_questions'][0]['rubric_draft'],[])
        self.assertEqual(a['sub_questions'][0]['holistic']['levels'],[])
        self.assertEqual(b['sub_questions'][0]['scoring_mode'],'point_bank')
        self.assertEqual(d['source_tables'][0],[['时间','制度'],['阶段甲','制度甲'],['阶段乙','制度乙']])
        self.assertIn('阶段甲 | 制度甲',a['sub_questions'][0]['question'])
        self.assertIn('范例一',a['sub_questions'][0]['reference_answer']);self.assertIn('范例二',a['sub_questions'][0]['reference_answer'])
        self.assertIn('注意区分史实',b['sub_questions'][0]['question'])
        self.assertIn('任选两处',b['sub_questions'][0]['reference_answer'])
        self.assertIn('序言或结束语',b['sub_questions'][1]['reference_answer'])
        self.assertIn('评分标准',b['sub_questions'][1]['reference_answer'])

    def test_structure_edit_creates_without_mutating_original(self):
        d=import_draft();before=copy.deepcopy(d);c=confirmation_for(d)
        m=c['material_questions'][0];m['label']='20';m['sub_questions'][0]['question_id']='20.2'
        saved,questions,_=app._confirmed_import_payload(d,c)
        self.assertEqual(questions[0]['id'],'Q20.2');self.assertEqual(saved['material_questions'][0]['label'],'20')
        self.assertEqual(d,before)
        m['sub_questions'].append(copy.deepcopy(m['sub_questions'][0]))
        with self.assertRaises(ValueError):app._confirmed_import_payload(d,c)

    def test_numbered_reference_examples_do_not_create_questions(self):
        from tests.test_docx_question_import import make_docx
        d=parse_docx(make_docx(['合成试卷','19．历史小论文（12分）','19．【答案】',
            '1．参考范例一：观点甲。','2．参考范例二：观点乙。',
            '20题 阅读材料（4分）','（1）指出史实。（4分）','20．【答案】','（1）合成史实。']))
        self.assertEqual([m['label'] for m in d['material_questions']],['19','20'])
        self.assertIn('范例二',d['material_questions'][0]['sub_questions'][0]['reference_answer'])

    def test_answer_total_before_subparts_and_repeated_rubric_heading(self):
        from tests.test_docx_question_import import make_docx
        d=parse_docx(make_docx(['合成试卷','20题 材料任务（12分）',
            '（1）任选两项纠正。（4分）','（2）设计主题和介绍文字。（8分）',
            '20．【答案】（12分）','（1）纠错甲；纠错乙；纠错丙。',
            '（2）示例一：主题甲及介绍甲。','示例二：主题乙及介绍乙。',
            '（1）主题评分：主题完整2分。','（2）介绍文字评分',
            '史实、主题关联与表达分别评价，共6分。']))
        a,b=d['material_questions'][0]['sub_questions']
        self.assertIn('纠错丙',a['reference_answer']);self.assertNotIn('示例',a['reference_answer']);self.assertNotIn('主题评分',a['reference_answer'])
        self.assertIn('示例二',b['reference_answer']);self.assertIn('主题评分',b['reference_answer'])
        self.assertIn('分别评价',b['reference_answer'])

    def test_holistic_saved_no_reference_and_never_sent_to_model(self):
        d=import_draft();c=confirmation_for(d);q=c['material_questions'][0]['sub_questions'][0]
        config=dict(levels=[dict(min_score=0,max_score=3,description='观点与史实不足'),dict(min_score=4,max_score=8,description='观点明确，史实基本准确'),dict(min_score=9,max_score=12,description='论证充分，逻辑清楚')],dimensions='观点、史实、论证、逻辑',notes='多个示例均为参考，不限制其他合理观点')
        q.update(scoring_mode='holistic',reference_answer='',max_score=12,rubric_draft=[],holistic=config,question_type='essay')
        saved,questions,_=app._confirmed_import_payload(d,c)
        self.assertEqual(questions[0]['holistic']['levels'],validate(config,12)['levels'])
        self.assertEqual(questions[0]['criteria'],[])
        self.assertEqual(saved['material_questions'][0]['sub_questions'][0]['question_type'],'essay')
        output=simulate(config,12,2,10);self.assertEqual(output['suggested_total'],10);self.assertTrue(output['simulation_model']);self.assertTrue(output['blocked'])
        with self.assertRaises(ValueError):simulate(config,12,0,10)
        with self.assertRaisesRegex(ValueError,'评分步长'):app.call_vision_model({},questions,'',[])
        from auto_crop_double_review_poc import rubric_readiness
        ready,reasons=rubric_readiness(questions,teacher_confirmation_status='confirmed_by_teacher',teacher_confirmed_at='synthetic',required_question_ids=[questions[0]['id']])
        self.assertFalse(ready);self.assertTrue(any('holistic' in r for r in reasons))

    def test_invalid_levels(self):
        for levels in ([],[None],[dict(min_score=None,max_score=12,description='无效')],[dict(min_score=1,max_score=12,description='无零分档')],[dict(min_score=0,max_score=12,description='')],[dict(min_score=0,max_score=8,description='甲'),dict(min_score=8,max_score=12,description='乙')]):
            with self.assertRaises(ValueError):validate(dict(levels=levels),12)

    def test_holistic_exam_creation_persists_without_mutating_import_source(self):
        import json, tempfile
        from unittest.mock import patch
        from tests.test_exam_import_ui import ExamImportUiTests
        draft=import_draft();confirmation=confirmation_for(draft)
        q=confirmation['material_questions'][0]['sub_questions'][0]
        q.update(scoring_mode='holistic',max_score=12,reference_answer='',rubric_draft=[],holistic=dict(levels=[dict(min_score=0,max_score=12,description='合成综合标准')]))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);did='d'*32;folder=root/'exam_import_drafts'/did;folder.mkdir(parents=True)
            (folder/'exam_import_draft.json').write_text(json.dumps(draft),encoding='utf-8')
            handler,replies=ExamImportUiTests().fake_handler(dict(draft_id=did,confirmation=confirmation))
            with patch.object(app,'DATA_DIR',root),patch.object(app,'DB_PATH',root/'synthetic.sqlite3'),patch.object(app,'BACKUP_DIR',root/'backups'),patch.object(app,'json_response',side_effect=lambda h,body,status=200:replies.append((body,status))):
                app.AppHandler.create_imported_exam.__wrapped__(handler)
                response,status=replies[0];self.assertEqual(status,201)
                conn=app.get_db()
                try:
                    row=conn.execute('SELECT sub_questions_json,import_metadata_json FROM assignments WHERE id=?',(response['assignment']['id'],)).fetchone()
                    official=json.loads(row['sub_questions_json']);meta=json.loads(row['import_metadata_json'])
                    self.assertEqual(official[0]['scoring_mode'],'holistic')
                    self.assertEqual(official[0]['holistic']['levels'][0]['max_score'],12)
                    self.assertEqual(meta['score_totals']['subjective'],12)
                finally:conn.close()
                self.assertEqual(json.loads((folder/'exam_import_draft.json').read_text()),draft)

    def test_four_prompt_functions_unchanged(self):
        import ast
        import hashlib
        expected = {
            'build_rubric_model_payload': '359f365b26229e6f1cef93285837a018b3aefcd4055d265e8f4bf1a84957e9e4',
            'build_vision_prompt_text': '17190ef1d36a228871dba7d48c7df682e6dfaba5aca9b09b9afbbcf5f25d4cde',
            '_objective_vision_prompt': '8611192fc56c4c288ac08f5b40c5dba1c550a10189ff94c2d9ffdbc3f9962060',
            'build_exam_import_ai_payload': '5d60e83e74fe05037638355aee08f9bf6e6bac6e6e74af3f6cb985ccbf3da66f',
        }
        observed = {}
        for filename in ('app.py', 'exam_import_ai.py'):
            tree = ast.parse(Path(filename).read_text(encoding='utf-8-sig'))
            for function in tree.body:
                if isinstance(function, ast.FunctionDef) and function.name in expected:
                    syntax = ast.dump(function, include_attributes=False)
                    observed[function.name] = hashlib.sha256(syntax.encode()).hexdigest()
        self.assertEqual(observed, expected)
