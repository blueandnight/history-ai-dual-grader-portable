import copy, unittest
import app

class TeacherDeterministicFinalTests(unittest.TestCase):
 def setUp(self):
  self.q=[{'id':'Q1','max_score':6,'scoring_mode':'weighted','criteria':[{'id':f'C{i}','score':2,'label':str(i)} for i in range(1,4)]},{'id':'Q2','max_score':8,'scoring_mode':'point_bank','criteria':[{'id':f'C{i}','score':2,'label':str(i)} for i in range(1,9)]}]
  self.d=[{'question_id':q['id'],'rubric_point_id':c['id'],'decision':'none'} for q in self.q for c in q['criteria']]
 def test_normal_subtotals(self):
  for i in [0,3,4]:self.d[i]['decision']='full'
  r=app.teacher_deterministic_score(self.q,self.d)
  self.assertEqual(r['final_total'],6); self.assertEqual([x['subtotal'] for x in r['question_subtotals']],[2,4])
 def test_bank_cap_six_hits(self):
  for d in self.d[3:9]:d['decision']='full'
  r=app.teacher_deterministic_score(self.q,self.d)
  self.assertEqual(r['final_total'],8);self.assertEqual(r['question_subtotals'][1]['raw_total'],12)
 def test_uncertain_missing_partial_block(self):
  for decision in ['uncertain','partial','missing']:
   d=copy.deepcopy(self.d);d[0]['decision']=decision
   with self.assertRaisesRegex(ValueError,'teacher_review_incomplete'):app.teacher_deterministic_score(self.q,d)
  with self.assertRaisesRegex(ValueError,'teacher_review_incomplete'):app.teacher_deterministic_score(self.q,self.d[:-1])
 def test_all_none_zero(self):self.assertEqual(app.teacher_deterministic_score(self.q,self.d)['final_total'],0)
 def test_unknown_duplicate_block(self):
  with self.assertRaises(ValueError):app.teacher_deterministic_score(self.q,self.d+[self.d[0]])
  d=copy.deepcopy(self.d);d[0]['rubric_point_id']='FORGED'
  with self.assertRaises(ValueError):app.teacher_deterministic_score(self.q,d)
 def test_uncertain_not_complete(self):
  self.d[0]['decision']='uncertain';self.assertNotEqual(app.teacher_review_completeness(self.q,self.d),'point_level_complete')


class TeacherFinalApiTests(unittest.TestCase):
 def test_confirm_history_exports_and_consistency_gate(self):
  import tempfile, json, csv, io
  from pathlib import Path
  from urllib.error import HTTPError
  from urllib.request import urlopen
  from tests.test_service_control import isolated_mobile_test_servers, request_json
  with tempfile.TemporaryDirectory() as td, isolated_mobile_test_servers(Path(td)) as (port,_):
   q=TeacherDeterministicFinalTests();q.setUp();questions=q.q
   for question in questions:question['label']=question['id']
   ai={'mode':'vision-model','suggested_total':14,'max_total':14,'blocked':False,'sub_questions':[{'sub_question_id':x['id'],'answer_status':'answered','criteria':[]} for x in questions]}
   ai['dual_review']={'results':{'A':{},'B':{}},'comparison':{'risk_level':'HIGH'}}
   conn=app.get_db();conn.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)',('a'*12,'合成终评测试','test','test','[]',json.dumps(questions),app.utc_now()))
   conn.execute('INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,source_channel,identity_status,student_name) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('b'*12,'a'*12,'合成学生','','[]',json.dumps(ai),app.utc_now(),'completed','computer','confirmed','合成学生'));conn.commit();conn.close()
   choices=[{'question_id':d['question_id'],'rubric_point_id':d['rubric_point_id'],'choice':'teacher_none'} for d in q.d]
   for ds,total in [(choices,14),(choices[:-1],None),([{**choices[0],'choice':'teacher_uncertain'},*choices[1:]],None)]:
    payload={'submission_id':'b'*12,'teacher_point_decisions':ds}
    if total is not None:payload['final_total']=total
    with self.assertRaises(HTTPError) as raised:request_json(port,'/api/confirm','POST',payload=payload)
    self.assertEqual(raised.exception.code,400)
    row=app.get_db();self.assertIsNone(row.execute('SELECT confirmed_at FROM submissions').fetchone()[0]);row.close()
   confirmed=request_json(port,'/api/confirm','POST',payload={'submission_id':'b'*12,'teacher_point_decisions':choices});self.assertEqual(confirmed['final_total'],0)
   sub=request_json(port,'/api/submission?submission_id='+'b'*12)['submission'];tf=sub['result']['dual_review']['teacher_final']
   self.assertEqual([s['subtotal'] for s in tf['question_subtotals']],[0,0]);self.assertEqual(tf['final_total'],sub['final_total']);self.assertEqual(sub['result']['dual_review']['results'],ai['dual_review']['results'])
   with urlopen(f'http://127.0.0.1:{port}/api/export.csv?assignment_id='+ 'a'*12) as response:rows=list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
   self.assertEqual(float(rows[0]['最终确认分']),0)
   # A historical manual final is retained on read and on rejected re-confirmation.
   conn=app.get_db()
   conn.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,source_channel,final_total,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ('c'*12,'a'*12,'旧人工终评','','[]',json.dumps(ai),app.utc_now(),'completed','computer',7,'2026-10-01T00:00:00Z'))
   conn.commit();conn.close()
   old=request_json(port,'/api/submission?submission_id='+'c'*12)['submission'];self.assertEqual(old['final_total'],7)
   with self.assertRaises(HTTPError):request_json(port,'/api/confirm','POST',payload={'submission_id':'c'*12,'final_total':7})
   old=request_json(port,'/api/submission?submission_id='+'c'*12)['submission'];self.assertEqual(old['final_total'],7);self.assertEqual(old['confirmed_at'],'2026-10-01T00:00:00Z')



   # The existing single-model subjective path uses the same teacher calculator.
   single=dict(ai);single.pop('dual_review')
   conn=app.get_db();conn.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,source_channel) VALUES(?,?,?,?,?,?,?,?,?)", ('d'*12,'a'*12,'单模型夹具','','[]',json.dumps(single),app.utc_now(),'completed','computer'));conn.commit();conn.close()
   final=request_json(port,'/api/confirm','POST',payload={'submission_id':'d'*12,'teacher_point_decisions':choices});self.assertEqual(final['final_total'],0)
   sub=request_json(port,'/api/submission?submission_id='+'d'*12)['submission'];self.assertEqual(sub['result']['teacher_final']['final_total'],0);self.assertEqual(sub['result']['suggested_total'],14)
