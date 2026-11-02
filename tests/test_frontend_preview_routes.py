from pathlib import Path
import tempfile,unittest
from unittest.mock import Mock
from urllib.request import Request
from urllib.error import HTTPError
import app
from frontend_routes import serve_preview
from tests import test_cloud_security as fixtures


class PreviewRouteTests(unittest.TestCase):
    def test_missing_build_and_asset_traversal_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            h=Mock();root=Path(td)
            self.assertTrue(serve_preview(h,'/next/',root));h.send_error.assert_called_once_with(503,'Preview build unavailable')
            h.reset_mock()
            for path in ['/next/assets/../../app.py','/next/assets/%2e%2e/index.html','/next/assets/secret.env']:
                self.assertTrue(serve_preview(h,path,root))
            self.assertEqual(h.send_error.call_count,3);h.serve_file.assert_not_called()
            self.assertFalse(serve_preview(h,'/api/assignments',root))

    def test_cloud_preview_document_needs_session_but_login_shell_is_public(self):
        f=fixtures.CloudSecurityTests();f.setUp()
        try:
            target=app.STATIC_DIR/'next';target.mkdir(parents=True)
            (target/'index.html').write_text('<html>synthetic-preview-shell</html>',encoding='utf-8')
            # opener follows the redirect to the public login shell.
            for path in ['/','/index.html','/next','/next/','/next/index.html']:
                with f.opener.open(f.base+path) as r:
                    self.assertTrue(r.url.endswith('/next/login'))
                    self.assertIn(b'synthetic-preview-shell',r.read())
            with f.opener.open(f.base+'/next/login') as r:self.assertEqual(r.status,200)
            f.configure_password();status,data,_,cookie=f.login('synthetic test password 123')
            self.assertEqual(status,200)
            with f.opener.open(Request(f.base+'/next/',headers={'Cookie':cookie})) as r:self.assertEqual(r.status,200)
            self.assertEqual(f.request_json('GET','/api/assignments')[0],401)
            with f.opener.open(Request(f.base+'/',headers={'Cookie':cookie})) as r:self.assertIn(b'synthetic-preview-shell',r.read())
            (app.STATIC_DIR/'index.html').write_text('synthetic-legacy-capabilities',encoding='utf-8')
            with f.opener.open(Request(f.base+'/legacy',headers={'Cookie':cookie})) as r:self.assertIn(b'synthetic-legacy-capabilities',r.read())
            self.assertEqual(f.opener.open(f.base+'/legacy').url,f.base+'/login')
            # Only the objective document may be framed by the same origin.
            with f.opener.open(Request(f.base+'/next/',headers={'Cookie':cookie})) as r:
                self.assertEqual(r.headers['X-Frame-Options'],'DENY')
                self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
        finally:f.tearDown()
