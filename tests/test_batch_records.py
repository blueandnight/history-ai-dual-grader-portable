"""Synthetic batch navigation queries; no real images, data or model calls."""
from contextlib import closing
import json
import unittest
from urllib.request import urlopen
from urllib.error import HTTPError
import app
from tests.test_batch_order import OrderedBatchTests


class BatchRecordTests(unittest.TestCase):
    def setUp(self):
        self.f=OrderedBatchTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.one=self.f.make_batch(['synthetic-one'])
        self.two=self.f.make_batch(['synthetic-two'])
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE grading_batches SET status='completed' WHERE id=?",(self.one['batch_id'],))

    def get(self,query=''):
        try:
            with urlopen(self.f.base_url+'/api/batches'+query) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)

    def test_completed_opt_in_and_exact_batch_lookup_are_readonly(self):
        with closing(app.get_db()) as c:before=list(c.iterdump())
        self.assertEqual([b['batch_id'] for b in self.get()[1]['batches']],[self.two['batch_id']])
        found=self.get('?include_completed=1&assignment_id='+'a'*12)[1]['batches']
        self.assertEqual({b['batch_id'] for b in found},{self.one['batch_id'],self.two['batch_id']})
        record=self.get('?include_completed=1&batch_id='+self.one['batch_id'])[1]['batches']
        self.assertEqual(len(record),1);self.assertEqual(record[0]['status'],'completed')
        self.assertEqual(record[0]['uploaded_count'],0);self.assertEqual(record[0]['processed_count'],0)
        self.assertEqual(record[0]['assignment_name'],'测试题')
        summary=self.get('?include_completed=1&summary=1&batch_id='+self.one['batch_id'])[1]['batches'][0]
        self.assertEqual(summary['items'],[]);self.assertEqual(summary['total_sequences'],1)
        with closing(app.get_db()) as c:self.assertEqual(before,list(c.iterdump()))

    def test_invalid_and_wrong_scope_do_not_fall_back(self):
        for query in ('?include_completed=1&batch_id=bad','?include_completed=1&batch_id=','?include_completed=1&exam_id=bad'):
            self.assertEqual(self.get(query)[0],400)
        for query in ('&assignment_id='+'b'*12,'&exam_id='+'e'*32):
            self.assertEqual(self.get('?include_completed=1&batch_id='+self.one['batch_id']+query)[1]['batches'],[])


class BatchRecordAuthTests(unittest.TestCase):
    def test_completed_listing_keeps_authentication_gate(self):
        from tests.test_teacher_accounts import TeacherAccountTests
        f=TeacherAccountTests();f.setUp()
        try:
            path='/api/batches?include_completed=1&summary=1'
            self.assertEqual(f.request_json('GET',path)[0],401)
            self.assertEqual(f.request_json('GET',path,headers=f.admin_headers())[0],200)
        finally:f.tearDown()
