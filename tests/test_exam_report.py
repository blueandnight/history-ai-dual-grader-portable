import json
import sqlite3
import unittest

import exam_report


class ExamReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE assignments (id TEXT, name TEXT, question TEXT, sub_questions_json TEXT)")
        self.conn.execute(
            "CREATE TABLE submissions (id TEXT, assignment_id TEXT, final_total REAL, result_json TEXT, "
            "confirmed_at TEXT, batch_id TEXT, exam_id TEXT, state TEXT, created_at TEXT)"
        )
        questions = [
            {"label": "17.1", "question": "措施", "standard_answer": "推恩令", "max_score": 4,
             "criteria": [{"id": "C1", "label": "颁布推恩令", "score": 2}, {"id": "C2", "label": "削弱诸侯", "score": 2}]},
            {"label": "17.2", "question": "作用", "standard_answer": "科举", "max_score": 6,
             "criteria": [{"id": "C1", "label": "打破特权", "score": 2}]},
        ]
        self.conn.execute("INSERT INTO assignments VALUES (?,?,?,?)", (
            "a" * 12, "测试考试", "合成", json.dumps(questions, ensure_ascii=False)
        ))
        results = [
            {"suggested_total": 4, "max_total": 10, "sub_questions": [
                {"label": "17.1", "suggested_total": 4, "max_total": 4, "answer_status": "answered", "criteria": [
                    {"criterion_id": "C1", "criterion": "颁布推恩令", "suggested_score": 2, "max_score": 2},
                    {"criterion_id": "C2", "criterion": "削弱诸侯", "suggested_score": 2, "max_score": 2},
                ]},
                {"label": "17.2", "suggested_total": 0, "max_total": 6, "answer_status": "unanswered_candidate", "criteria": []},
            ]},
            {"suggested_total": 2, "max_total": 10, "sub_questions": [
                {"label": "17.1", "suggested_total": 2, "max_total": 4, "answer_status": "answered", "criteria": [
                    {"criterion_id": "C1", "criterion": "颁布推恩令", "suggested_score": 2, "max_score": 2},
                    {"criterion_id": "C2", "criterion": "削弱诸侯", "suggested_score": 0, "max_score": 2},
                ]},
                {"label": "17.2", "suggested_total": 0, "max_total": 6, "answer_status": "answered", "criteria": [
                    {"criterion_id": "C1", "criterion": "打破特权", "suggested_score": 0, "max_score": 2},
                ]},
            ]},
        ]
        for index, result in enumerate(results):
            self.conn.execute(
                "INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?)",
                (f"s{index}", "a" * 12, (None if index else result["suggested_total"]), json.dumps(result, ensure_ascii=False),
                 None if index else "2026-10-22T00:00:00Z", "", "", "completed", "2026-10-22T00:00:00Z"),
            )
        self.conn.commit()

    def test_builds_confirmed_and_unconfirmed_statistics(self):
        data = exam_report.build_exam_report_data(self.conn, "a" * 12)
        self.assertEqual(data["student_count"], 2)
        self.assertEqual(data["confirmed_count"], 1)
        self.assertEqual(data["unconfirmed_count"], 1)
        self.assertEqual(data["max_total"], 10)
        self.assertEqual(data["average"], 3)
        first = next(item for item in data["questions"] if item["label"] == "17.1")
        self.assertEqual(first["average"], 2)
        self.assertEqual(first["teacher_scored"], 0)
        self.assertEqual(first["ai_scored"], 1)
        self.assertEqual(first["missing_score"], 1)
        self.assertEqual(first["unanswered"], 0)
        self.assertIn("缺少逐题教师终评", " ".join(data["warnings"]))
        second = next(item for item in data["questions"] if item["label"] == "17.2")
        self.assertEqual(second["unanswered"], 0)
        self.assertEqual(second["missing_score"], 1)
        misses = data["common_misses"]
        self.assertTrue(any(item["label"] == "削弱诸侯" for item in misses))
        self.assertFalse(data["objective"]["available"])

    def test_ai_prompt_uses_aggregate_and_learning_objectives(self):
        data = exam_report.build_exam_report_data(self.conn, "a" * 12)
        messages = exam_report.build_ai_report_prompt(data, "能说明推恩令的作用")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("教学评一体化", messages[0]["content"])
        self.assertIn("能说明推恩令的作用", messages[1]["content"])
        self.assertIn("17.1", messages[1]["content"])
        self.assertNotIn("学生姓名", messages[1]["content"])


    def add_record(self, result, *, confirmed=None, final=None, batch='new'):
        self.conn.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?,?)',
                          (str(self.conn.total_changes), 'a'*12, final, json.dumps(result), confirmed, batch, '', 'completed', '2026'))

    def test_mixed_equal_max_scopes_have_no_common_denominator(self):
        for key in ('Q17.1','Q17.2'):
            self.add_record({'suggested_total':4,'max_total':10,'grading_scope':{'graded_question_ids':[key],'scoped_max_score':4}})
        data=exam_report.build_exam_report_data(self.conn,'a'*12,batch_id='new')
        self.assertTrue(data['mixed_scopes']); self.assertIsNone(data['max_total']); self.assertEqual(data['score_rate'],100)

    def test_unconfirmed_stale_final_not_promoted_and_exclusions_counted(self):
        self.add_record({'suggested_total':2,'max_total':4,'teacher_final':{'final_total':4}},final=4)
        for flag in ('blocked','simulation_model','teacher_reconfirmation_required'):
            self.add_record({'suggested_total':4,'max_total':4,flag:True},confirmed='2026',final=4)
        self.add_record({'suggested_total':float('nan'),'max_total':4})
        data=exam_report.build_exam_report_data(self.conn,'a'*12,batch_id='new')
        self.assertEqual(data['average'],2); self.assertEqual(data['student_count'],1)
        self.assertEqual(data['coverage']['missing_total_count'],1)
        self.assertEqual([data[k] for k in ('blocked_count','simulation_count','reconfirmation_count')],[1,1,1])
        json.dumps(data,allow_nan=False)

    def test_uncertain_point_is_not_counted_as_missed_and_prompt_is_pure(self):
        self.add_record({'suggested_total':0,'max_total':4,'sub_questions':[{'label':'17.1','max_total':4,'suggested_total':0,
            'criteria':[{'criterion_id':'C1','criterion':'uncertain point','suggested_score':0,'decision':'uncertain'}]}]})
        data=exam_report.build_exam_report_data(self.conn,'a'*12,batch_id='new')
        self.assertEqual(data['common_misses'],[])
        before=json.dumps(data,sort_keys=True); prompt=exam_report.build_ai_report_prompt(data,'goal')
        self.assertEqual(json.dumps(data,sort_keys=True),before)
        for field in ('assignment_id','exam_id','batch_id','criterion_id'):
            self.assertNotIn(field,prompt[1]['content'])

    def test_teacher_zero_and_scoped_max_override_ai_values(self):
        result = {
            "suggested_total": 4, "max_total": 4,
            "grading_scope": {"graded_question_ids": ["Q17.1"], "scoped_max_score": 4, "source": "answer_regions"},
            "sub_questions": [{
                "label": "17.1", "sub_question_id": "Q17.1", "suggested_total": 4, "max_total": 4,
                "answer_status": "answered", "criteria": [
                    {"criterion_id": "C1", "criterion": "颁布推恩令", "suggested_score": 2, "max_score": 2, "decision": "full"},
                    {"criterion_id": "C2", "criterion": "削弱诸侯", "suggested_score": 2, "max_score": 2, "decision": "full"},
                ],
            }],
            "dual_review": {"teacher_final": {
                "question_subtotals": [{"question_id": "Q17.1", "subtotal": 0, "max_score": 4}],
                "final_total": 0,
                "point_decisions": [
                    {"question_id": "Q17.1", "rubric_point_id": "C1", "decision": "none"},
                    {"question_id": "Q17.1", "rubric_point_id": "C2", "decision": "none"},
                ],
            }},
        }
        self.conn.execute(
            "INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?)",
            ("teacher-zero", "a" * 12, 0, json.dumps(result, ensure_ascii=False),
             "2026-10-22T00:00:00Z", "b1", "", "completed", "2026-10-22T00:00:00Z"),
        )
        self.conn.commit()
        data = exam_report.build_exam_report_data(self.conn, "a" * 12, batch_id="b1")
        self.assertEqual(data["student_count"], 1)
        self.assertEqual(data["max_total"], 4)
        self.assertEqual(data["average"], 0)
        self.assertEqual(data["score_rate"], 0)
        self.assertEqual(data["questions"][0]["average"], 0)
        self.assertEqual(data["questions"][0]["answered"], 1)
        self.assertEqual(data["questions"][0]["teacher_scored"], 1)
        self.assertTrue(all(item["rate"] == 0 for item in data["questions"][0]["criteria"]))


if __name__ == "__main__":
    unittest.main()

