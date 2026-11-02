"""Synthetic HTTP binding guards; no production database or provider requests."""
from contextlib import closing
import io
import unittest
import app
from tests import test_answer_links as links
from tests.test_unified_scores import seed_answers


class WorkspaceBindingTests(unittest.TestCase):
    def setUp(self):
        self.f = links.AnswerLinkTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.client = self.f.client
        with closing(app.get_db()) as c, c:
            seed_answers(c, self.f.obj[0])
            self.assessment = c.execute('SELECT assessment_id FROM objective_submissions WHERE submission_id=?', (self.f.obj[0],)).fetchone()[0]

    def stored(self):
        with closing(app.get_db()) as c:
            return {t: [tuple(r) for r in c.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] for t in ('objective_submissions', 'objective_answers', 'objective_jobs', 'objective_review_events')}

    def test_wrong_workspace_blocks_upload_review_and_retry_without_writes(self):
        before = self.stored()
        header = {'X-Workspace-Exam-ID': 'f' * 32}
        upload = self.client.post('/api/uploads', data={'assessment_id': self.assessment, 'mode': 'omr', 'files': (io.BytesIO(b'synthetic'), 'synthetic.png')}, headers=header)
        self.assertEqual(upload.status_code, 409)
        review = self.client.post('/api/submissions/' + self.f.obj[0] + '/answers/1/review', json={'option': 'A', 'reason': 'synthetic explicit review'}, headers=header)
        self.assertEqual(review.status_code, 409)
        retry = self.client.post('/api/submissions/' + self.f.obj[0] + '/retry', headers=header)
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(self.stored(), before)

    def test_matching_exam_and_legacy_clients_keep_existing_review_path(self):
        for header in ({'X-Workspace-Exam-ID': self.f.exam}, {}):
            r = self.client.post('/api/submissions/' + self.f.obj[0] + '/answers/1/review', json={'option': 'A', 'reason': 'synthetic explicit review'}, headers=header)
            self.assertEqual(r.status_code, 200, r.get_json())

    def test_only_embedded_objective_document_allows_same_origin_framing(self):
        # Main server wraps the existing objective document with its own headers.
        from urllib.request import Request, urlopen
        base = self.f.fixture.f.base_url
        # This fixture's server does not install objective WSGI by default.
        server = self.f.fixture.f.server
        server.objective_wsgi_app = app.create_integrated_objective_wsgi_app()
        for path, expected in (('/objective/?embedded=1', 'SAMEORIGIN'), ('/objective/?legacy=1', 'DENY')):
            with urlopen(Request(base + path), timeout=10) as response:
                self.assertEqual(response.headers['X-Frame-Options'], expected)

