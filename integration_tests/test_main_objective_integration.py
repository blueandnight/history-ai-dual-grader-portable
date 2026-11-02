from __future__ import annotations

import http.client
import io
import json
import os
import random
import sqlite3
import tempfile
import threading
import unittest

import app as main_app
import web_auth
from objective.synthetic import photograph, render_omr_card
from objective.templates import BUILTIN_TEMPLATES
from objective_tests.auth_helpers import TEST_PASSWORD, TEST_USERNAME


def _http(method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=15)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


class MainServiceObjectiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global _PORT
        os.environ["HISTORY_GRADER_WEB_AUTH_REQUIRED"] = "1"
        os.environ["HISTORY_GRADER_CLOUD_MODE"] = "0"
        os.environ["HISTORY_GRADER_COOKIE_SECURE"] = "0"
        os.environ["HISTORY_GRADER_ADMIN_USERNAME"] = TEST_USERNAME
        os.environ["HISTORY_GRADER_ADMIN_PASSWORD_HASH"] = web_auth.hash_password(TEST_PASSWORD, iterations=100_000)
        os.environ["HISTORY_GRADER_TOTP_REQUIRED"] = "0"
        os.environ["HISTORY_GRADER_EMAIL_OTP_REQUIRED"] = "0"
        cls._temp = tempfile.TemporaryDirectory(prefix="stage4-main-integration-")
        main_app.configure_paths(portable_root=cls._temp.name)
        db = main_app.get_db()
        db.close()

        # Seed a synthetic material record and snapshot its table definition/data
        # before the objective schema is added to the shared candidate database.
        conn = sqlite3.connect(main_app.DB_PATH)
        conn.execute("INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)", ("a" * 12, "synthetic-material", "synthetic question", "synthetic answer", "[]", "[]", "stage4"))
        conn.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at) VALUES(?,?,?,?,?,?,?)", ("b" * 32, "a" * 12, "synthetic-student-label", "synthetic answer text", "[]", "{}", "stage4"))
        conn.commit()
        cls.material_snapshot = {}
        for table in ("schema_meta", "assignments", "submissions"):
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            cols = [tuple(row) for row in conn.execute(f'PRAGMA table_info("{table}")')]
            data = [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            cls.material_snapshot[table] = (ddl, cols, data)
        conn.close()

        cls.objective_wsgi_app = main_app.create_integrated_objective_wsgi_app()
        conn = sqlite3.connect(main_app.DB_PATH)
        cls.user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        cls.material_schema_version = conn.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()[0]
        for table, expected in cls.material_snapshot.items():
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            cols = [tuple(row) for row in conn.execute(f'PRAGMA table_info("{table}")')]
            data = [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            if (ddl, cols, data) != expected:
                raise AssertionError(f"objective schema migration changed material table {table}")
        cls.objective_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'objective_%'")}
        conn.close()

        cls.server = main_app.PortableHTTPServer(("127.0.0.1", 0), main_app.AppHandler)
        cls.server.objective_wsgi_app = cls.objective_wsgi_app
        cls.server.restarting = False
        cls.server.stopping = False
        _PORT = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        origin = f"http://127.0.0.1:{_PORT}"
        status, headers, data = _http("POST", "/api/auth/login", body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}).encode(), headers={"Content-Type": "application/json", "Origin": origin})
        if status != 200:
            raise AssertionError(f"synthetic main login failed: {status}")
        cls.cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        login_result = json.loads(data)
        cls.csrf = login_result.get("csrf_token", "")
        if not cls.cookie or not cls.csrf:
            raise AssertionError("main login did not return a session and CSRF token")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._temp.cleanup()

    def _auth_headers(self, *, csrf: str | None = None, origin: str | None = None) -> dict[str, str]:
        headers = {"Cookie": self.cookie, "Origin": origin or f"http://127.0.0.1:{_PORT}"}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        return headers

    def test_objective_schema_is_namespaced_and_material_data_is_unchanged(self) -> None:
        self.assertEqual(self.user_version, 5)
        self.assertEqual(self.material_schema_version, main_app.DB_SCHEMA_VERSION)
        self.assertTrue({"objective_assessments", "objective_submissions", "objective_review_items"} <= self.objective_tables)
        self.assertNotIn("objective_schema_meta", self.objective_tables)

    def test_main_auth_blocks_objective_page_apis_and_material_api_without_session(self) -> None:
        status, headers, _ = _http("GET", "/objective/")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")
        status, headers, _ = _http("GET", "/objective/login")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")
        for path in ("/objective/api/status", "/objective/api/submissions?assessment_id=unknown", "/objective/api/export.csv?assessment_id=unknown", "/api/assignments"):
            with self.subTest(path=path):
                status, _headers, _data = _http("GET", path)
                self.assertEqual(status, 401)

    def test_source_exam_routes_keep_auth_csrf_and_origin_gates(self):
        for path in ('/objective/api/source-exams','/objective/api/source-exams/'+'f'*32):
            self.assertEqual(_http('GET',path)[0],401)
        path='/objective/api/assessments/from-exam';payload=json.dumps({'exam_id':'f'*32}).encode()
        self.assertEqual(_http('POST',path,body=payload,headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{_PORT}'})[0],401)
        for csrf,origin in [(None,None),('wrong',None),(self.csrf,'https://attacker.invalid')]:
            self.assertEqual(_http('POST',path,body=payload,headers={'Content-Type':'application/json',**self._auth_headers(csrf=csrf,origin=origin)})[0],403)
        self.assertEqual(_http('POST',path,body=payload,headers={'Content-Type':'application/json',**self._auth_headers(csrf=self.csrf)})[0],404)

    def test_answer_link_routes_preserve_main_auth_and_csrf(self):
        self.assertEqual(_http('GET','/objective/api/answer-links?exam_id='+'f'*32)[0],401)
        path='/objective/api/answer-links';body=json.dumps({'exam_id':'f'*32,'confirmed':True,'originals_checked':True,'reason':'synthetic'}).encode()
        origin=f'http://127.0.0.1:{_PORT}'
        self.assertEqual(_http('POST',path,body=body,headers={'Content-Type':'application/json','Origin':origin})[0],401)
        self.assertEqual(_http('POST',path,body=body,headers={'Content-Type':'application/json',**self._auth_headers(csrf='wrong')})[0],403)
        self.assertEqual(_http('POST',path,body=body,headers={'Content-Type':'application/json',**self._auth_headers(csrf=self.csrf)})[0],404)

    def test_unified_scores_is_authenticated_readonly(self):
        path='/objective/api/unified-scores?exam_id='+'f'*32
        self.assertEqual(_http('GET',path)[0],401)
        self.assertEqual(_http('GET',path,headers=self._auth_headers())[0],404)
        self.assertEqual(_http('POST',path,body=b'{}',headers={'Content-Type':'application/json',**self._auth_headers(csrf='wrong')})[0],403)

    def test_unauthenticated_objective_upload_is_rejected(self) -> None:
        body = json.dumps({"assessment_id": "synthetic-only"}).encode()
        status, _headers, _data = _http("POST", "/objective/api/uploads", body=body, headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{_PORT}"})
        self.assertEqual(status, 401)

    def test_objective_writes_require_main_csrf_and_same_origin(self) -> None:
        payload = json.dumps({"title": "synthetic-csrf", "answer_key": [{"question_no": 1, "correct_option": "A", "points": 1}]}).encode()
        base = {"Content-Type": "application/json", **self._auth_headers()}
        for extra in ({}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": self.csrf, "Origin": "https://attacker.invalid"}):
            headers = {key: value for key, value in base.items() if key != "X-CSRF-Token"}
            headers.update(extra)
            with self.subTest(headers=extra):
                status, _response_headers, _data = _http("POST", "/objective/api/assessments", body=payload, headers=headers)
                self.assertEqual(status, 403)
        status, _headers, data = _http("POST", "/objective/api/assessments", body=payload, headers={"Content-Type": "application/json", **self._auth_headers(csrf=self.csrf)})
        self.assertEqual(status, 201, data.decode("utf-8", "replace"))

    def test_anonymous_code_is_not_an_authentication_credential(self) -> None:
        answer_key = [{"question_no": index, "correct_option": "A", "points": 1} for index in range(1, 21)]
        status, _headers, body = _http("POST", "/objective/api/assessments", body=json.dumps({"title": "synthetic-anon", "answer_key": answer_key}).encode(), headers={"Content-Type": "application/json", **self._auth_headers(csrf=self.csrf)})
        self.assertEqual(status, 201)
        assessment_id = json.loads(body)["assessment_id"]
        image = photograph(render_omr_card(BUILTIN_TEMPLATES[0]["config"], ["A"] * 20), random.Random(441))
        boundary = "stage4syntheticboundary"
        fields = [("assessment_id", assessment_id), ("mode", "omr"), ("grouping", "one_per_image"), ("template_id", "auto")]
        chunks = []
        for name, value in fields:
            chunks.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()])
        chunks.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"synthetic.png\"\r\nContent-Type: image/png\r\n\r\n".encode(), image, f"\r\n--{boundary}--\r\n".encode()])
        status, _headers, body = _http("POST", "/objective/api/uploads", body=b"".join(chunks), headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **self._auth_headers(csrf=self.csrf)})
        self.assertEqual(status, 202, body.decode("utf-8", "replace"))
        job_id = json.loads(body)["job_id"]
        status, _headers, body = _http("GET", f"/objective/api/jobs/{job_id}", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        anonymous_code = json.loads(body)["submissions"][0]["anonymous_code"]
        self.assertTrue(anonymous_code)
        unauthenticated = {"Authorization": f"Bearer {anonymous_code}", "X-Anonymous-Code": anonymous_code}
        for path in (f"/objective/api/submissions?assessment_id={assessment_id}", f"/objective/api/export.csv?assessment_id={assessment_id}"):
            with self.subTest(path=path):
                status, _headers, _data = _http("GET", path, headers=unauthenticated)
                self.assertIn(status, {401, 403})

    def test_authenticated_material_entry_and_objective_entry_share_one_service(self) -> None:
        status, _headers, body = _http("GET", "/", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("/objective/", html)
        self.assertIn("材料题批改", html)
        status, _headers, body = _http("GET", "/objective/", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn("快速匿名批改", body.decode("utf-8"))
        status, headers, _ = _http("GET", "/objective/login", headers={"Cookie": self.cookie})
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")
        status, _headers, body = _http("GET", "/objective/static/app.js", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"objectiveBase", body)
        status, _headers, _body = _http("GET", "/api/status", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        status, _headers, body = _http("GET", "/objective/api/status", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        objective_status = json.loads(body)
        self.assertEqual(objective_status["status"], "integrated-local")
        self.assertEqual(objective_status["external_model"], "vision-dual")
        self.assertEqual(objective_status["handwriting_mode"], {"experimental": True, "default": False, "high_reliability": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
