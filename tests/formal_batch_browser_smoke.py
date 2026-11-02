"""Optional real-browser smoke. Requires Node Playwright (NODE_PATH) and Edge."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import app
from tests import test_formal_batch_workflow as fixtures


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = fixtures.FormalBatchWorkflowTests()
    fixture.setUp()
    try:
        shutil.copytree(Path(app.__file__).parent/'static', app.STATIC_DIR, dirs_exist_ok=True)
        source = output/'synthetic.png';source.write_bytes(fixture.source)
        captured=[]
        def capture(request, timeout):
            payload=json.loads(request.data);captured.append(payload)
            context=MagicMock();context.__enter__.return_value=payload;return context
        def respond(payload,*args,**kwargs):
            prompt=payload['messages'][1]['content'][0]['text']
            q=next(q for q in fixture.questions if f'"id": "{q["id"]}"' in prompt)
            return json.dumps({'confidence':.9,'sub_questions':[{'sub_question_id':q['id'],'answer_status':'answered',
                'criteria':[{'criterion_id':'C1','decision':'none','suggested_score':0,'student_evidence':'','needs_review':False}]}]})
        with patch.object(app,'open_model_request',side_effect=capture), patch.object(app,'read_model_chat_content',side_effect=respond):
            for width in (1280,390):
                start=len(captured);processed=set()
                process=subprocess.Popen(['node',str(Path(__file__).with_suffix('.js')),fixture.fixture.base_url,str(source),str(output/f'ui-{width}'),str(width)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                deadline=time.monotonic()+90
                try:
                    while process.poll() is None and time.monotonic()<deadline:
                        with closing(app.get_db()) as c:
                            rows=c.execute("SELECT id FROM submissions WHERE state='queued'").fetchall()
                        for row in rows:
                            if row['id'] not in processed:
                                processed.add(row['id']);app.process_submission(row['id'])
                        time.sleep(.15)
                    stdout,stderr=process.communicate(timeout=5)
                    assert process.returncode==0,(stdout,stderr)
                    assert len(captured)-start==4,len(captured)-start
                    with closing(app.get_db()) as c:
                        batches=c.execute('SELECT exam_id,manifest_json FROM grading_batches').fetchall()
                    assert all(b['exam_id']==fixture.exam_id for b in batches)
                    assert all(json.loads(b['manifest_json'])['items'][0]['page_count']==2 for b in batches)
                finally:
                    if process.poll() is None:process.terminate();process.wait(timeout=10)
        (output/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','mock_provider_calls':len(captured),'real_provider_calls':0,'production_access':False},indent=2),encoding='utf-8')
        print('Desktop and 390px PASS; 8 mock calls; real provider 0')
    finally:
        fixture.doCleanups()


if __name__=='__main__':main()
