import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

import app
from pdf_renderer import new_reference_method, render_pdf_page
from tests import test_batch_order as batch_fixture


class ExportAndLocalOriginTests(unittest.TestCase):
    setUp = batch_fixture.OrderedBatchTests.setUp
    tearDown = batch_fixture.OrderedBatchTests.tearDown
    request_json = batch_fixture.OrderedBatchTests.request_json

    def request(self, path, payload=None, headers=None):
        request = Request(self.base_url + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers={'Content-Type':'application/json', **(headers or {})})
        try:
            res = build_opener(ProxyHandler({})).open(request, timeout=10)
        except HTTPError as exc:
            res = exc
        with res:
            return res.status, res.read()

    def test_real_export_routes_use_scoped_denominator_but_keep_whole_exam_legacy(self):
        qs=[{'id':'Q1','label':'Q1','max_score':6},{'id':'Q2','label':'Q2','max_score':4},{'id':'Q3','label':'Q3','max_score':20}]
        _,batch=self.request_json('/api/batches',{'assignment_id':'a'*12,'batch_request_id':'synthetic-export','items':[{'page_count':1}]})
        item=batch['items'][0]
        result={'mode':'vision-model','suggested_total':10,'blocked':False,'grading_scope':{'graded_question_ids':['Q1','Q2'],'scoped_max_score':10},'sub_questions':[]}
        c=app.get_db()
        c.execute('UPDATE assignments SET sub_questions_json=? WHERE id=?',(json.dumps(qs),'a'*12))
        for sid,code in [('b'*12,None),(item['submission_id'],item['marking_code_id'])]:
            c.execute('INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,confirmed_at,final_total,marking_code_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (sid,'a'*12,'synthetic','','[]',json.dumps(result),app.utc_now(),'completed',app.utc_now(),10,code))
        c.commit()
        try:
            for path in ['/api/export.csv','/api/export-anonymous.csv']:
                status,raw=self.request(path+'?assignment_id='+'a'*12)
                self.assertEqual(status,200)
                rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
                self.assertEqual(len(rows),1)
                self.assertEqual((float(rows[0]['最终确认分']),float(rows[0]['满分'])),(10,10))
            self.assertEqual(app.export_scope_maximum(qs,[],{}),30)
            bad=copy.deepcopy(result);bad['grading_scope']['scoped_max_score']=12
            with self.assertRaises(ValueError):app.export_scope_maximum(qs,[],bad)
            c.execute('UPDATE submissions SET final_total=11 WHERE id=?',('b'*12,));c.commit()
            self.assertEqual(len(list(csv.DictReader(io.StringIO(self.request('/api/export.csv?assignment_id='+'a'*12)[1].decode('utf-8-sig'))))),0)
        finally:c.close()

    def test_foreign_host_cross_site_json_and_simple_forms_are_blocked(self):
        for headers in [{'Host':'evil.invalid'}, {'Origin':'https://evil.invalid'},
                        {'Origin':self.base_url,'Sec-Fetch-Site':'cross-site'},
                        {'Content-Type':'text/plain','Origin':'https://evil.invalid'},
                        {'Content-Type':'text/plain'}, {'Referer':'https://evil.invalid/'}]:
            status,_=self.request('/api/mobile-token',{},headers)
            self.assertIn(status,(403,415),headers)
        self.assertEqual(self.request('/api/status',headers={'Host':'evil.invalid'})[0],403)
        self.assertEqual(self.request('/api/status',headers={'Sec-Fetch-Site':'cross-site'})[0],403)

    def test_same_origin_and_native_clients_work_without_promoting_remote_privileges(self):
        self.assertEqual(self.request('/api/health')[0],200)
        self.assertEqual(self.request('/api/mobile-token',{}, {'Origin':self.base_url,'Sec-Fetch-Site':'same-origin'})[0],200)
        self.assertEqual(self.request('/api/mobile-token',{})[0],200)


class PackagedPdfRendererTests(unittest.TestCase):
    def test_pdfium_renders_and_repeats_without_poppler(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as td, patch('shutil.which',return_value=None):
            root=Path(td);pdf=root/'source.pdf';writer=PdfWriter();writer.add_blank_page(width=144,height=216)
            with pdf.open('wb') as f:writer.write(f)
            method=new_reference_method();self.assertEqual(method,'pdfium-5.14.0')
            first=root/'one.png';second=root/'two.png'
            render_pdf_page(pdf,1,150,first,method=method);render_pdf_page(pdf,1,150,second,method=method)
            self.assertEqual(first.read_bytes(),second.read_bytes())
            from PIL import Image
            with Image.open(first) as im:
                self.assertEqual(im.width,300)
                self.assertIn(im.height,(450,451))  # PDFium rounds the floating scale upwards.
            with self.assertRaises(ValueError):render_pdf_page(pdf,1,150,second,method='pdfium-0.0')

    def test_reference_rebuild_keeps_renderer_and_hash_contract(self):
        import hashlib
        from pypdf import PdfWriter
        from exam_reference_assets import resolve_exam_reference_page
        with tempfile.TemporaryDirectory() as td, patch('shutil.which',return_value=None):
            root=Path(td);(root/'data').mkdir();pdf=root/'data/reference.pdf';w=PdfWriter();w.add_blank_page(width=144,height=216)
            with pdf.open('wb') as f:w.write(f)
            original=root/'original.png';method=new_reference_method();render_pdf_page(pdf,1,150,original,method=method)
            from PIL import Image
            with Image.open(original) as image:width,height=image.size
            page={'page_number':1,'page_role':'exam','reference_width':width,'reference_height':height,'reference_sha256':hashlib.sha256(original.read_bytes()).hexdigest(),'render_dpi':150,'render_method':method,'image_file':'data/missing.png'}
            args=dict(portable_root=root,exam_id='e'*32,source_pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),source_pdf_path=pdf,path_resolver=lambda rel:root/rel)
            result=resolve_exam_reference_page(page=page,**args)
            self.assertEqual(result['status'],'reference_rebuilt')
            bad={**page,'image_file':'data/still-missing.png','reference_sha256':'0'*64}
            self.assertEqual(resolve_exam_reference_page(page=bad,**args)['status'],'reference_rebuild_mismatch')
            legacy={**page,'render_method':'pdftoppm_150dpi','image_file':'data/legacy-missing.png'}
            self.assertEqual(resolve_exam_reference_page(page=legacy,**args)['reason'],'pdf_renderer_unavailable')
