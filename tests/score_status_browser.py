"""Real read-only listing and Vue against isolated synthetic abnormal score records."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from unittest.mock import patch
import app
from tests.test_batch_adoption import AdoptionTests


def main():
    build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
    f=AdoptionTests();f.setUp()
    try:
        def blank(r):
            r['dual_review']['models']['A']['provider']='deepseek'
            r['dual_review']['models']['B']['provider']='mimo'
            for model in (r,r['dual_review']['results']['A'],r['dual_review']['results']['B']):
                model['suggested_total']=0
                for q in model['sub_questions']:
                    q['suggested_total']=0
                    for p in q['criteria']:p.update(decision='none',suggested_score=0)
            for model in (r,r['dual_review']['results']['A']):
                model['sub_questions'][1].update(answer_status='unanswered_candidate',suggested_total=None,criteria=[])
        def filtered(r):
            d=r['dual_review'];d['results'].pop('B');d['status']='degraded'
            d['models']['B']['provider']='mimo'
            d['models']['B'].update(status='error',http_status=200,finish_reason='content_filter')
            d['errors']={'B':{'error_category':'request_or_response_validation'}}
        f.mutate(f.ids[0],blank);f.mutate(f.ids[1],filtered)
        ids={'blank':f.ids[0],'filtered':f.ids[1]}
        with closing(app.get_db()) as c,c:
            for label,status,state in [('paused','paused','queued'),('queued','active','queued'),('recovery','paused','recovery_pending'),('grading','active','grading')]:
                bid=uuid.uuid4().hex[:12];sid=uuid.uuid4().hex[:12];ids[label]=sid
                c.execute('INSERT INTO grading_batches(id,request_id,assignment_id,exam_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (bid,bid,f.assignment,f.exam,1,status,'{}',bid,app.utc_now(),app.utc_now()))
                c.execute("INSERT INTO submissions(id,assignment_id,exam_id,student_label,answer_text,photos_json,result_json,created_at,state,batch_id,batch_sequence) VALUES(?,?,?,'synthetic','','[]','{}',?,?,?,1)",
                          (sid,f.assignment,f.exam,app.utc_now(),state,bid))
        def grades():
            with closing(app.get_db()) as c:
                return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in ('submissions','assignments','grading_exams','grading_batches','task_queue','exam_marking_codes')}
        before=grades()
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
        f.http.server.objective_wsgi_app=f.f.f.fixture.objective
        f.http.server.restarting=f.http.server.stopping=False
        ids_path=out/'synthetic-ids.json';ids_path.write_text(json.dumps(ids),encoding='utf-8')
        with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as model:
            for width in (1280,390):
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,f.assignment,f.exam,str(ids_path),str(out/str(width)),str(width)],capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'),timeout=70)
                assert result.returncode==0,result.stdout+result.stderr
                print(result.stdout)
            model.assert_not_called()
        assert before==grades(),'browser read mutated synthetic grading records'
    finally:f.doCleanups()


if __name__=='__main__':main()
