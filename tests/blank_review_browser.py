"""Actual Vue blank-review writes against isolated synthetic records only."""
from contextlib import closing
import copy,json,os,shutil,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import app
from tests.test_batch_adoption import AdoptionTests


def main():
    build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
    f=AdoptionTests();f.setUp()
    try:
        originals={}
        with closing(app.get_db()) as c:
            for sid in f.ids:
                originals[sid]=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0])
        def seed():
            with closing(app.get_db()) as c,c:
                for sid,original in originals.items():
                    result=copy.deepcopy(original)
                    q=result['sub_questions'][0]
                    q.update(answer_status='unanswered_candidate',suggested_total=None,criteria=[])
                    result['dual_review']['results']['A']['sub_questions'][0]=copy.deepcopy(q)
                    result['blocked']=True
                    c.execute('UPDATE submissions SET result_json=?,confirmed_at=NULL,final_total=NULL WHERE id=?',(json.dumps(result),sid))
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
        f.http.server.objective_wsgi_app=f.f.f.fixture.objective
        f.http.server.restarting=f.http.server.stopping=False
        with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as models:
            for width,fail in ((1280,False),(390,False),(390,True)):
                seed()
                with closing(app.get_db()) as c:
                    raw={r['id']:json.loads(r['result_json'])['dual_review'] for r in c.execute('SELECT id,result_json FROM submissions')}
                run=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,f.assignment,f.exam,str(out/(str(width)+('-failure' if fail else ''))),str(width)]+(['failure'] if fail else []),capture_output=True,text=True,encoding='utf-8',timeout=90,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
                assert run.returncode==0,run.stdout+run.stderr
                print(run.stdout)
                with closing(app.get_db()) as c:
                    for row in c.execute('SELECT * FROM submissions'):
                        r=json.loads(row['result_json'])
                        assert r['sub_questions'][0]['answer_status'] in ('blank_confirmed','unanswered_candidate') if fail else r['sub_questions'][0]['answer_status']=='blank_confirmed'
                        assert row['confirmed_at'] is None and row['final_total'] is None
                        assert r['dual_review']==raw[row['id']], 'raw dual results must remain unchanged'
                if fail:
                    with closing(app.get_db()) as c:
                        statuses=[json.loads(r[0])['sub_questions'][0]['answer_status'] for r in c.execute('SELECT result_json FROM submissions')]
                        assert statuses.count('blank_confirmed')==1 and statuses.count('unanswered_candidate')==1
            models.assert_not_called()
    finally:f.doCleanups()


if __name__=='__main__':main()
