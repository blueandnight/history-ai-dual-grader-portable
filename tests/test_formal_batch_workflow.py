"""Synthetic HTTP coverage of browser-used formal batch endpoints."""
import base64
import copy
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import numpy as np
from PIL import Image, ImageDraw
import app
from exam_layout_profile_service import build_exam_layout_profile
from tests import test_batch_order as batch_fixture


class FormalBatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'HISTORY_GRADER_CLOUD_MODE': '0', 'HISTORY_GRADER_WEB_AUTH_REQUIRED': '0'}))
        self.fixture = batch_fixture.OrderedBatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.assignment_id = 'a' * 12
        self.exam_id = 'e' * 32
        self.questions = app.clean_sub_questions([
            {'id': 'Q17.1', 'label': '17.1', 'question': 'Question one', 'standard_answer': 'Answer one',
             'scoring_mode': 'weighted', 'max_score': 2, 'criteria': [{'id': 'C1', 'label': 'First point', 'score': 2}], 'needs_teacher_confirmation': False},
            {'id': 'Q17.2', 'label': '17.2', 'question': 'Question two', 'standard_answer': 'Answer two',
             'scoring_mode': 'weighted', 'max_score': 2, 'criteria': [{'id': 'C1', 'label': 'Second point', 'score': 2}], 'needs_teacher_confirmation': False},
        ])
        image = Image.new('RGB', (1200, 1600), 'white')
        draw = ImageDraw.Draw(image)
        rng = np.random.default_rng(527)
        for x, y, r in rng.integers([30, 30, 2], [1170, 1570, 12], size=(420, 3)):
            draw.ellipse((int(x-r), int(y-r), int(x+r), int(y+r)), outline='black', width=2)
        out = io.BytesIO();image.save(out, 'PNG');self.source = out.getvalue()
        ref = app.DATA_DIR / 'synthetic-reference.png';ref.write_bytes(self.source)
        refs = [{'page_number': 1, 'reference_width': 1200, 'reference_height': 1600,
                 'reference_sha256': hashlib.sha256(self.source).hexdigest(), 'image_file': 'data/synthetic-reference.png', 'render_dpi': 150}]
        self.profile = build_exam_layout_profile(exam_id=self.exam_id, assignment_id=self.assignment_id,
            source_pdf_sha256='f'*64, references=refs, required_question_ids=['17.1', '17.2'],
            regions=[{'question_id': label, 'page_number': 1, 'bbox': dict(zip(('x1','y1','x2','y2'), bbox))}
                     for label,bbox in [('17.1',(.1,.5,.9,.7)),('17.2',(.1,.72,.9,.94))]],
            layout_profile_id='d'*32, created_at=app.utc_now(), updated_at=app.utc_now())
        metadata = {'teacher_confirmation_status': 'confirmed_by_teacher', 'teacher_confirmed_at': app.utc_now(),
            'source': {'layout_source': {'source_pdf_sha256':'f'*64,'page_count':1,'exam_pages':[1],'answer_pages':[]}},
            'layout_reference': {'pages':refs,'page_roles':[{'page_number':1,'page_role':'exam'}]}}
        with closing(app.get_db()) as c, c:
            c.execute('UPDATE assignments SET sub_questions_json=?,import_metadata_json=?,criteria_json=? WHERE id=?',
                      (json.dumps(self.questions),json.dumps(metadata),json.dumps(app.flatten_criteria(self.questions)),self.assignment_id))
            c.execute('INSERT INTO grading_exams(id,assignment_id,created_at,layout_profile_id,layout_profile_json) VALUES(?,?,?,?,?)',
                      (self.exam_id,self.assignment_id,'2026-10-01T00:00:00Z',self.profile['layout_profile_id'],json.dumps(self.profile)))
        self.specs = {side: {'provider': provider, 'provider_label': provider, 'model':'synthetic-'+provider,
            'api_origin':'https://example.invalid','base_url':'https://example.invalid/v1','preset_id':'synthetic','label':provider}
            for side,provider in [('A','deepseek'),('B','mimo')]}
        self.enterContext(patch.object(app,'dual_review_model_specs',return_value=self.specs))
        self.enterContext(patch.object(app,'dual_review_key_is_configured',return_value=True))
        self.enterContext(patch.object(app,'load_dual_review_model_configs',return_value={s:{**v,'api_key':'SYNTHETIC_ONLY','timeout_seconds':'1'} for s,v in self.specs.items()}))
        self.provider = self.enterContext(patch.object(app,'open_model_request',side_effect=AssertionError('External provider forbidden')))
        self.enterContext(patch.object(app,'enqueue_submission',return_value=None))

    def request(self,path,payload):
        return self.fixture.request_json_status(path,payload)

    def original_preview(self, exam_id=None):
        body = {'assignment_id': self.assignment_id, 'exam_id': self.exam_id if exam_id is None else exam_id,
                'grading_input_mode': 'original_photo', 'student_label': 'SYNTHETIC',
                'photos': [{'data_url': self.fixture.image_url, 'page_sequence': 1, 'ai_redactions': []}],
                'answer_regions': []}
        status, preview = self.request('/api/ai-payload-preview', body)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['answer_regions'], [])
        self.assertEqual(preview['images'][0]['image_kind'], 'full_page')
        return body, preview

    def test_original_legacy_multipart_without_image_role_uses_approved_source_page(self):
        body, preview = self.original_preview()
        status, batch = self.request('/api/batches', {'assignment_id': self.assignment_id, 'exam_id': self.exam_id,
            'grading_input_mode': 'original_photo', 'batch_request_id': 'original-multipart', 'items': [{'page_count': 1}]})
        self.assertEqual(status, 201, batch)
        metadata = {**body, 'batch_id': batch['batch_id'], 'sequence': 1,
            'submission_id': batch['items'][0]['submission_id'], 'ai_privacy_confirmed': True,
            'privacy_status': 'no_identity_visible', 'ai_privacy_preview_token': preview['preview_token'],
            'photos': [{'name': 'page_01.jpg', 'page_sequence': 1, 'corners': None, 'ai_redactions': []}]}
        raw = base64.b64decode(self.fixture.image_url.split(',', 1)[1])
        def upload(photo):
            boundary = 'original-multipart-regression'
            data = {**metadata, 'photos': [photo]}
            chunks = [f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()
                      + json.dumps(data).encode() + b'\r\n']
            for field in ('processed_0', 'original_0'):
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="page.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()+raw+b'\r\n')
            chunks.append(f'--{boundary}--\r\n'.encode())
            content_type = 'multipart/form-data; boundary='+boundary
            wire = b''.join(chunks)
            parsed = app.parse_multipart_submission(wire, content_type)
            self.assertEqual(parsed['photos'][0]['image_role'], str(photo.get('image_role') or ''))
            request = Request(self.fixture.base_url+'/api/submissions', data=wire, headers={'Content-Type': content_type})
            try:
                with urlopen(request, timeout=15) as response:
                    return response.status, json.load(response)
            except HTTPError as response:
                with response:return response.code, json.load(response)
        for role in ('unexpected', 'answer_region'):
            self.assertEqual(upload({**metadata['photos'][0], 'image_role': role})[0], 400)
        status, result = upload(metadata['photos'][0])
        self.assertEqual(status, 202, result)
        with closing(app.get_db()) as c:
            photo = json.loads(c.execute('SELECT photos_json FROM submissions WHERE id=?',
                                        (result['submission']['id'],)).fetchone()[0])[0]
        self.assertEqual(photo['image_role'], 'source_page')
        self.assertEqual(photo['grading_input_mode'], 'original_photo')
        self.assertTrue(photo['approved_ai_page_sha256'])
        self.provider.assert_not_called()

    def test_original_photo_without_layout_previews_queues_and_reaches_worker(self):
        with closing(app.get_db()) as c, c:
            c.execute("UPDATE grading_exams SET layout_profile_id=NULL,layout_profile_json='{}' WHERE id=?", (self.exam_id,))
        listed_assignment = self.fixture.get_json('/api/assignments')['assignments'][0]
        self.assertEqual([e['exam_id'] for e in listed_assignment['original_photo_exams']], [self.exam_id])
        self.assertEqual(listed_assignment['layout_exams'], [])
        with patch.object(app, 'model_configured', return_value=True):
            body, preview = self.original_preview()
            status, batch = self.request('/api/batches', {'assignment_id': self.assignment_id, 'exam_id': self.exam_id,
                'grading_input_mode': 'original_photo', 'batch_request_id': 'original', 'items': [{'page_count': 1}]})
            self.assertEqual(status, 201, batch)
            listed = self.fixture.get_json('/api/batches')['batches'][0]
            self.assertEqual(listed['grading_input_mode'], 'original_photo')
            upload = {**body, 'batch_id': batch['batch_id'], 'sequence': 1,
                      'submission_id': batch['items'][0]['submission_id'], 'ai_privacy_confirmed': True,
                      'privacy_status': 'no_identity_visible', 'ai_privacy_preview_token': preview['preview_token']}
            for changes in ({'grading_input_mode': 'auto_crop'}, {'exam_id': 'b'*32},
                            {'ai_privacy_confirmed': False}, {'privacy_status': 'unchecked'},
                            {'ai_privacy_preview_token': ''},
                            {'photos': [{'data_url': app.bytes_to_data_url(self.source, 'image/png'), 'page_sequence': 1}]}):
                self.assertEqual(self.request('/api/submissions', {**upload, **changes})[0], 400)
            empty_body, empty_preview = self.original_preview(exam_id='')
            self.assertEqual(self.request('/api/submissions', {**upload, 'exam_id': '',
                'ai_privacy_preview_token': empty_preview['preview_token']})[0], 400)
            status, result = self.request('/api/submissions', upload)
            self.assertEqual(status, 202, result)
            self.provider.assert_not_called()
            # A blank synthetic image deliberately fails the old heuristic
            # quality check. Explicitly reviewed original pages still reach grading.
            with patch.object(app, 'grade_submission', return_value=app.queued_result(2)) as grade:
                app.process_submission(result['submission']['id'])
            grade.assert_called_once()
        with closing(app.get_db()) as c:
            row = c.execute('SELECT photos_json,result_json,answer_regions_json FROM submissions WHERE id=?',
                            (result['submission']['id'],)).fetchone()
        photo = json.loads(row['photos_json'])[0]
        self.assertEqual(photo['grading_input_mode'], 'original_photo')
        self.assertEqual(photo['image_role'], 'source_page')
        self.assertTrue(photo['approved_ai_page_sha256'])
        self.assertEqual(json.loads(row['answer_regions_json']), [])
        self.assertEqual(json.loads(row['result_json'])['diagnostics']['grading_input_mode'], 'original_photo')
        self.assertFalse((app.DATA_DIR/'student_auto_crop').exists())

    def test_original_mode_is_signed_and_cannot_claim_crops(self):
        body, preview = self.original_preview()
        claims = app._read_preview_claims(preview['preview_token'])
        self.assertEqual(claims['grading_input_mode'], 'original_photo')
        self.assertEqual(claims['exam_id'], self.exam_id)
        for mode, exam_id in [('auto_crop', self.exam_id), ('original_photo', '')]:
            with self.assertRaises(ValueError):
                app._verify_ai_preview_token(preview['preview_token'], self.assignment_id,
                    'unused', self.questions, body['photos'], grading_input_mode=mode, exam_id=exam_id)
        for changes in ({'grading_input_mode': 'unknown'},
                        {'photos': [{**body['photos'][0], 'image_role': 'answer_region'}]},
                        {'photos': [{**body['photos'][0], 'answer_region_attestation': 'fake'}]},
                        {'answer_regions': [{'region_id': 'r', 'photo_index': 1, 'x': 0, 'y': 0,
                            'width': 1, 'height': 1, 'sub_question_ids': ['Q17.1', 'Q17.2']}]}):
            self.assertEqual(self.request('/api/ai-payload-preview', {**body, **changes})[0], 400)
        self.provider.assert_not_called()

    def test_original_photo_candidates_preserve_layout_list_and_reject_foreign_or_class_exams(self):
        other_assignment = 'c' * 12
        empty_exam, foreign_exam, class_exam = 'b' * 32, 'f' * 32, 'a' * 32
        with closing(app.get_db()) as c, c:
            c.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,created_at) VALUES(?,?,?,?,?,?)',
                      (other_assignment, 'Synthetic other assignment', 'q', 'a',
                       json.dumps(app.flatten_criteria(self.questions)), app.utc_now()))
            c.execute('INSERT INTO roster_classes(id,class_name,created_at) VALUES(?,?,?)',
                      ('c' * 32, 'Synthetic candidate class', app.utc_now()))
            for exam_id, assignment_id, class_id in ((empty_exam, self.assignment_id, None),
                    (foreign_exam, other_assignment, None), (class_exam, self.assignment_id, 'c' * 32)):
                c.execute('INSERT INTO grading_exams(id,assignment_id,class_id,created_at) VALUES(?,?,?,?)',
                          (exam_id, assignment_id, class_id, '2026-10-02T00:00:00Z'))
        assignments = {a['id']: a for a in self.fixture.get_json('/api/assignments')['assignments']}
        listed = assignments[self.assignment_id]
        self.assertEqual([e['exam_id'] for e in listed['original_photo_exams']], [empty_exam, self.exam_id])
        self.assertEqual([e['exam_id'] for e in listed['layout_exams']], [self.exam_id])
        self.assertEqual(listed['exam_id'], self.exam_id)
        self.assertEqual([e['exam_id'] for e in assignments[other_assignment]['original_photo_exams']], [foreign_exam])
        for exam_id in (foreign_exam, class_exam):
            status, _ = self.request('/api/batches', {'assignment_id': self.assignment_id, 'exam_id': exam_id,
                'grading_input_mode': 'original_photo', 'batch_request_id': 'reject-' + exam_id, 'items': [{'page_count': 1}]})
            self.assertEqual(status, 400)
        self.provider.assert_not_called()

    def test_original_photo_dual_models_receive_identical_approved_pages(self):
        body, _ = self.original_preview()
        body['dual_review_enabled'] = True
        status, preview = self.request('/api/ai-payload-preview', body)
        self.assertEqual(status, 200, preview)
        status, batch = self.request('/api/batches', {'assignment_id': self.assignment_id, 'exam_id': self.exam_id,
            'grading_input_mode': 'original_photo', 'batch_request_id': 'original-dual', 'items': [{'page_count': 1}]})
        self.assertEqual(status, 201, batch)
        status, result = self.request('/api/submissions', {**body, 'batch_id': batch['batch_id'], 'sequence': 1,
            'submission_id': batch['items'][0]['submission_id'], 'ai_privacy_confirmed': True,
            'privacy_status': 'no_identity_visible', 'ai_privacy_preview_token': preview['preview_token']})
        self.assertEqual(status, 202, result)
        captured = []
        def capture(request, timeout):
            captured.append(json.loads(request.data))
            response = MagicMock(); response.__enter__.return_value = {}
            return response
        response = json.dumps({'confidence': .9, 'sub_questions': [
            {'sub_question_id': q['id'], 'answer_status': 'answered', 'criteria': [
                {'criterion_id': 'C1', 'decision': 'none', 'suggested_score': 0,
                 'student_evidence': '', 'needs_review': False}]} for q in self.questions]})
        with patch.object(app, 'open_model_request', side_effect=capture) as provider, \
             patch.object(app, 'read_model_chat_content', return_value=response):
            app.process_submission(result['submission']['id'])
        self.assertEqual(provider.call_count, 2)
        contents = [p['messages'][1]['content'] for p in captured]
        self.assertEqual(contents[0], contents[1])
        for content in contents:
            self.assertNotIn('SYNTHETIC', json.dumps(content))
            blob = base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1])
            self.assertEqual(hashlib.sha256(blob).hexdigest(), preview['images'][0]['sha256'])

    def test_original_batch_can_create_new_exam_and_cannot_replay_as_crop(self):
        body, preview = self.original_preview(exam_id='')
        request = {'assignment_id': self.assignment_id, 'grading_input_mode': 'original_photo',
                   'batch_request_id': 'new-original', 'items': [{'page_count': 1}]}
        status, batch = self.request('/api/batches', request)
        self.assertEqual(status, 201, batch)
        self.assertNotEqual(batch['exam_id'], self.exam_id)
        self.assertEqual(self.request('/api/batches', {**request, 'grading_input_mode': 'auto_crop'})[0], 400)
        status, result = self.request('/api/submissions', {**body, 'batch_id': batch['batch_id'], 'sequence': 1,
            'submission_id': batch['items'][0]['submission_id'], 'ai_privacy_confirmed': True,
            'privacy_status': 'no_identity_visible', 'ai_privacy_preview_token': preview['preview_token']})
        self.assertEqual(status, 202, result)

    def test_original_class_batch_preserves_filename_identity_without_layout(self):
        with closing(app.get_db()) as c, c:
            c.execute('INSERT INTO roster_classes(id,class_name,created_at) VALUES(?,?,?)',
                      ('c'*32, 'Synthetic class', app.utc_now()))
            c.execute('INSERT INTO roster_students(id,class_id,class_name,student_no,student_name,identity_code,enabled,created_at) VALUES(?,?,?,?,?,?,1,?)',
                      ('b'*32, 'c'*32, 'Synthetic class', '01', 'Synthetic student', '01', app.utc_now()))
        status, batch = self.request('/api/batches', {'assignment_id': self.assignment_id,
            'grading_input_mode': 'original_photo', 'identity_mode': 'filename', 'class_name': 'Synthetic class',
            'batch_request_id': 'original-class', 'items': [{'page_count': 1, 'filenames': ['01.jpg']}]})
        self.assertEqual(status, 201, batch)
        self.assertEqual(batch['items'][0]['marking_status'], 'matched')
        with closing(app.get_db()) as c:
            self.assertEqual(c.execute('SELECT roster_student_id FROM exam_marking_codes WHERE batch_id=?',
                                      (batch['batch_id'],)).fetchone()[0], 'b'*32)
            exam = c.execute('SELECT class_id,layout_profile_id FROM grading_exams WHERE id=?',
                             (batch['exam_id'],)).fetchone()
            self.assertEqual(exam['class_id'], 'c'*32)
            self.assertFalse(exam['layout_profile_id'])
        self.provider.assert_not_called()

    def crop(self):
        boundary='synthetic-formal-boundary'
        metadata=json.dumps({'exam_id':self.exam_id,'input_mode':'single_page'}).encode()
        raw=(f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()+metadata+
             f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="student_image"; filename="synthetic.png"\r\nContent-Type: image/png\r\n\r\n'.encode()+self.source+
             f'\r\n--{boundary}--\r\n'.encode())
        req=Request(self.fixture.base_url+'/api/student-auto-crop',data=raw,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        with urlopen(req,timeout=25) as response:return json.load(response)

    def accepted(self):
        run=self.crop()
        self.assertEqual(run['status'],'cropped',run)
        self.assertEqual(len(run['crops']),2)
        run_id=run['alignment_run_id']
        self.assertTrue((app.DATA_DIR/'student_auto_crop'/run_id/'run.json').is_file())
        self.assertFalse((app.PROGRAM_DIR/'reports').exists())
        status,error=self.request('/api/student-auto-crop-answer-regions',{'alignment_run_id':run_id})
        self.assertEqual(status,400);self.assertIn('crop_not_teacher_accepted',error['error'])
        status,review=self.request('/api/student-auto-crop-review',{'alignment_run_id':run_id,'verdict':'passed'})
        self.assertEqual(status,200,review)
        status,result=self.request('/api/student-auto-crop-answer-regions',{'alignment_run_id':run_id})
        self.assertEqual(status,200,result)
        crops=result['answer_regions']
        return crops

    def test_batch_reuses_layout_exam_even_after_newer_empty_exam(self):
        with closing(app.get_db()) as c, c:
            c.execute('INSERT INTO grading_exams(id,assignment_id,created_at) VALUES(?,?,?)',('b'*32,self.assignment_id,'2099-01-01'))
        listed=self.fixture.get_json('/api/assignments')['assignments'][0]
        self.assertEqual(listed['exam_id'],self.exam_id)
        self.assertTrue(listed['formal_layout_required'])
        body={'assignment_id':self.assignment_id,'batch_request_id':'formal-batch','items':[{'page_count':2}]}
        status,result=self.request('/api/batches',body)
        self.assertEqual(status,201,result);self.assertEqual(result['exam_id'],self.exam_id)
        status,replay=self.request('/api/batches',body)
        self.assertEqual(status,200);self.assertEqual(replay['batch_id'],result['batch_id'])

    def test_anonymous_exam_selection_rejects_foreign_assignment_and_class(self):
        with closing(app.get_db()) as c, c:
            c.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,created_at) VALUES(?,?,?,?,?,?)',('c'*12,'Other','q','a','[]',app.utc_now()))
            c.execute('INSERT INTO grading_exams(id,assignment_id,created_at) VALUES(?,?,?)',('b'*32,'c'*12,app.utc_now()))
        status,_=self.request('/api/batches',{'assignment_id':self.assignment_id,'exam_id':'b'*32,'batch_request_id':'wrong-exam','items':[{'page_count':2}]})
        self.assertEqual(status,400)
        with closing(app.get_db()) as c, c:
            c.execute('INSERT INTO roster_classes(id,class_name,created_at) VALUES(?,?,?)',('c'*32,'Synthetic class',app.utc_now()))
            c.execute('UPDATE grading_exams SET class_id=? WHERE id=?',('c'*32,self.exam_id))
        status,_=self.request('/api/batches',{'assignment_id':self.assignment_id,'exam_id':self.exam_id,'batch_request_id':'wrong-class','items':[{'page_count':2}]})
        self.assertEqual(status,400)

    def test_real_alignment_review_preview_batch_and_worker_four_mock_calls(self):
        crops=self.accepted()
        photos=[{'name':f'crop-{i}.png','page_sequence':i,'data_url':crop['data_url'],'original_data_url':crop['data_url'],
                 'image_role':'answer_region','answer_region_attestation':crop['attestation'],'ai_redactions':[]}
                for i,crop in enumerate(crops,1)]
        regions=[{'region_id':f'accepted:{crop["question_id"]}','profile_id':crop['layout_profile_id'],'photo_index':i,
                  'x':0,'y':0,'width':1,'height':1,'sub_question_ids':[crop['question_id']],
                  'coordinate_system':'perspective_corrected_page_normalized','rotation_ccw':0,'invocation_scope':'answer_region'}
                 for i,crop in enumerate(crops,1)]
        body={'assignment_id':self.assignment_id,'exam_id':self.exam_id,'student_label':'SYNTHETIC',
              'dual_review_enabled':True,'photos':photos,'answer_regions':regions}
        status,preview=self.request('/api/ai-payload-preview',body)
        self.assertEqual(status,200,preview)
        self.assertTrue(all(r['invocation_scope']=='answer_region' for r in preview['answer_regions']))
        status,batch=self.request('/api/batches',{'assignment_id':self.assignment_id,'exam_id':self.exam_id,
            'batch_request_id':'real-aligned','items':[{'page_count':len(photos)}]})
        self.assertEqual(status,201,batch);self.assertEqual(batch['exam_id'],self.exam_id)
        upload={**body,'batch_id':batch['batch_id'],'sequence':1,'submission_id':batch['items'][0]['submission_id'],
            'idempotency_key':batch['batch_id']+':1','ai_privacy_confirmed':True,'privacy_status':'unchecked',
            'answer_regions':preview['answer_regions'],'ai_privacy_preview_token':preview['preview_token'],
            'ai_privacy_fingerprint':preview['preview_fingerprint']}
        self.assertEqual(self.request('/api/submissions',upload)[0],400)
        self.provider.assert_not_called()
        upload['privacy_status']='no_identity_visible'
        bad=copy.deepcopy(upload);bad['photos'][0]['data_url']=self.fixture.image_url
        self.assertEqual(self.request('/api/submissions',bad)[0],400)
        self.provider.assert_not_called()
        status,result=self.request('/api/submissions',upload)
        self.assertEqual(status,202,result)
        captured=[]
        def capture(request,timeout):
            payload=json.loads(request.data);captured.append(payload)
            response=MagicMock();response.__enter__.return_value=payload
            return response
        def read(payload,*args,**kwargs):
            prompt=payload['messages'][1]['content'][0]['text']
            q=next(q for q in self.questions if f'"id": "{q["id"]}"' in prompt)
            return json.dumps({'confidence':.9,'sub_questions':[{'sub_question_id':q['id'],'answer_status':'answered',
                'criteria':[{'criterion_id':'C1','decision':'none','suggested_score':0,'student_evidence':'','needs_review':False}]}]})
        with patch.object(app,'open_model_request',side_effect=capture) as provider,patch.object(app,'read_model_chat_content',side_effect=read):
            app.process_submission(result['submission']['id'])
        self.assertEqual(provider.call_count,4)
        with closing(app.get_db()) as c, c:
            row=c.execute('SELECT state,result_json,photos_json FROM submissions WHERE id=?',(result['submission']['id'],)).fetchone()
        self.assertEqual(row['state'],'completed')
        self.assertTrue(all(p['image_role']=='answer_region' for p in json.loads(row['photos_json'])))
        output=json.loads(row['result_json'])
        for side in ('A','B'):
            self.assertTrue(all(r['request_metadata']['input_mode']=='answer_region' for r in output['dual_review']['models'][side]['question_runs']))
        self.assertEqual(len(captured),4)
        images={}
        for payload in captured:
            content=payload['messages'][1]['content']
            q=next(q for q in self.questions if f'"id": "{q["id"]}"' in content[0]['text'])
            images.setdefault(q['id'],[]).append(base64.b64decode(content[1]['image_url']['url'].split(',',1)[1]))
            for forbidden in ('student_id','identity_code','SYNTHETIC'):
                self.assertNotIn(forbidden,json.dumps(payload))
        for blobs in images.values():
            self.assertEqual(len(blobs),2);self.assertEqual(blobs[0],blobs[1])

    def test_missing_or_stale_layout_blocks_before_batch_reservation(self):
        request={'assignment_id':self.assignment_id,'batch_request_id':'no-layout','items':[{'page_count':2}]}
        with closing(app.get_db()) as c,c:
            original=c.execute('SELECT layout_profile_json FROM grading_exams WHERE id=?',(self.exam_id,)).fetchone()[0]
            c.execute("UPDATE grading_exams SET layout_profile_id=NULL,layout_profile_json='{}' WHERE id=?",(self.exam_id,))
        status,error=self.request('/api/batches',request)
        self.assertEqual(status,400);self.assertIn('layout_profile_missing_or_stale',error['error'])
        bad=json.loads(original);bad['source_pdf_sha256']='0'*64
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE grading_exams SET layout_profile_id=?,layout_profile_json=? WHERE id=?',('d'*32,json.dumps(bad),self.exam_id))
        self.assertEqual(self.request('/api/batches',request)[0],400)
        with closing(app.get_db()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM grading_batches').fetchone()[0],0)
        self.provider.assert_not_called()

    def test_multiple_layout_exams_require_explicit_selection(self):
        with closing(app.get_db()) as c,c:
            profile=copy.deepcopy(self.profile);profile['exam_id']='b'*32;profile['layout_profile_id']='c'*32
            c.execute('INSERT INTO grading_exams(id,assignment_id,created_at,layout_profile_id,layout_profile_json) VALUES(?,?,?,?,?)',
                      ('b'*32,self.assignment_id,app.utc_now(),'c'*32,json.dumps(profile)))
        request={'assignment_id':self.assignment_id,'batch_request_id':'ambiguous-layout','items':[{'page_count':2}]}
        self.assertEqual(self.request('/api/batches',request)[0],400)
        request['exam_id']=self.exam_id
        self.assertEqual(self.request('/api/batches',request)[0],201)

    def test_failed_alignment_and_rejected_review_cannot_issue_credentials(self):
        run=self.crop();run_id=run['alignment_run_id']
        self.assertEqual(run['status'],'cropped')
        self.assertEqual(self.request('/api/student-auto-crop-review',{'alignment_run_id':run_id,'verdict':'failed'})[0],200)
        self.assertEqual(self.request('/api/student-auto-crop-answer-regions',{'alignment_run_id':run_id})[0],400)
        out=io.BytesIO();Image.new('RGB',(1200,1600),'white').save(out,'PNG');self.source=out.getvalue()
        failed=self.crop()
        self.assertNotEqual(failed['status'],'cropped')
        self.assertEqual(self.request('/api/student-auto-crop-review',{'alignment_run_id':failed['alignment_run_id'],'verdict':'passed'})[0],400)
        self.assertEqual(self.request('/api/student-auto-crop-answer-regions',{'alignment_run_id':failed['alignment_run_id']})[0],400)
        self.provider.assert_not_called()

    def test_class_binding_preserves_layout_and_crop_filename_identity(self):
        with closing(app.get_db()) as c,c:
            c.execute('INSERT INTO roster_classes(id,class_name,created_at) VALUES(?,?,?)',('b'*32,'Synthetic class',app.utc_now()))
            c.execute('INSERT INTO roster_students(id,class_id,class_name,student_no,student_name,identity_code,enabled,created_at) VALUES(?,?,?,?,?,?,1,?)',
                      ('c'*32,'b'*32,'Synthetic class','01','Synthetic student','01',app.utc_now()))
        request={'assignment_id':self.assignment_id,'exam_id':self.exam_id,'identity_mode':'filename','class_name':'Synthetic class',
                 'batch_request_id':'formal-class-crops','items':[{'page_count':2,'filenames':['01.jpg','01.jpg']}]}
        status,batch=self.request('/api/batches',request)
        self.assertEqual(status,201,batch);self.assertEqual(batch['exam_id'],self.exam_id)
        self.assertEqual(batch['items'][0]['marking_status'],'matched')
        with closing(app.get_db()) as c:
            row=c.execute('SELECT class_id,layout_profile_id FROM grading_exams WHERE id=?',(self.exam_id,)).fetchone()
        self.assertEqual((row['class_id'],row['layout_profile_id']),('b'*32,'d'*32))
        request['batch_request_id']='duplicate-crop-upload'
        status,duplicate=self.request('/api/batches',request)
        self.assertEqual(status,201);self.assertEqual(duplicate['items'][0]['identity_issue'],'duplicate_submission')
