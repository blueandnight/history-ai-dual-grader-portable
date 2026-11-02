from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import app
import password_auth
import web_auth


TEST_USERNAME = "teacher-test"
TEST_EMAIL = "teacher@example.test"
TEST_PASSWORD = "synthetic-old-password-123"


class PasswordAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_paths = app.APP_PATHS
        app.configure_paths(Path(self.temp.name) / "runtime", Path(self.temp.name) / "program")
        self.old_manager = web_auth.AUTH_MANAGER
        self.env = patch.dict(os.environ, {
            "HISTORY_GRADER_CLOUD_MODE": "1",
            "HISTORY_GRADER_WEB_AUTH_REQUIRED": "0",
            "HISTORY_GRADER_ADMIN_USERNAME": TEST_USERNAME,
            "HISTORY_GRADER_ADMIN_EMAIL": TEST_EMAIL,
            "HISTORY_GRADER_ADMIN_PASSWORD_HASH": password_auth.hash_password(TEST_PASSWORD),
            "HISTORY_GRADER_COOKIE_SECURE": "1",
            "HISTORY_GRADER_TRUST_PROXY": "1",
            "HISTORY_GRADER_SESSION_TTL": str(password_auth.SESSION_TTL_DEFAULT),
            # Legacy switch is deliberately true in one test; it must not put OTP on login.
            "HISTORY_GRADER_EMAIL_OTP_REQUIRED": "1",
            "HISTORY_GRADER_TOTP_REQUIRED": "1",
            "HISTORY_GRADER_TOTP_SECRET": "",
            "HISTORY_GRADER_SMTP_HOST": "smtp.example.test",
            "HISTORY_GRADER_SMTP_PORT": "587",
            "HISTORY_GRADER_SMTP_USERNAME": "mailer@example.test",
            "HISTORY_GRADER_SMTP_PASSWORD": "SYNTHETIC_SMTP_PASSWORD_ONLY",
            "HISTORY_GRADER_SMTP_FROM": "grader@example.test",
            "HISTORY_GRADER_AUTH_DB": str(app.DB_PATH),
        })
        self.env.start()
        self.manager = web_auth.SingleTeacherAuth()
        web_auth.AUTH_MANAGER = self.manager
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # Simulate the externally visible HTTPS origin while the test server is loopback HTTP.
        self.origin = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        web_auth.AUTH_MANAGER = self.old_manager
        self.env.stop()
        app._install_paths(self.previous_paths)
        self.temp.cleanup()

    def request_json(self, method, path, body=None, headers=None):
        outgoing = dict(headers or {})
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            outgoing.setdefault("Content-Type", "application/json")
        request = Request(self.base + path, data=data, headers=outgoing, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}, response.headers
        except HTTPError as response:
            raw = response.read()
            code, response_headers = response.code, response.headers
            response.close()
            return code, json.loads(raw) if raw else {}, response_headers

    def login(self, identity=TEST_USERNAME, password=TEST_PASSWORD, client_ip=""):
        headers = {"Origin": self.origin}
        if client_ip:
            headers["X-Real-IP"] = client_ip
        return self.request_json(
            "POST", "/api/auth/login", {"username": identity, "password": password},
            headers,
        )

    @staticmethod
    def cookie_value(headers):
        parsed = SimpleCookie()
        parsed.load(headers.get("Set-Cookie", ""))
        morsel = parsed.get(web_auth.COOKIE_NAME)
        return f"{web_auth.COOKIE_NAME}={morsel.value}" if morsel else ""

    def test_password_hash_is_argon2id_and_correct_login_needs_no_email_or_totp(self):
        encoded = os.environ["HISTORY_GRADER_ADMIN_PASSWORD_HASH"]
        self.assertTrue(encoded.startswith("$argon2id$"))
        with patch.object(password_auth, "_send_email_otp") as send_otp:
            status, data, headers = self.login(TEST_EMAIL)
        self.assertEqual(status, 200)
        self.assertTrue(data["authenticated"])
        self.assertTrue(data["csrf_token"])
        self.assertIn("history_grader_session=", headers.get("Set-Cookie", ""))
        send_otp.assert_not_called()
        conn = sqlite3.connect(app.DB_PATH)
        try:
            saved = conn.execute("SELECT password_hash FROM auth_users").fetchone()[0]
        finally:
            conn.close()
        self.assertTrue(saved.startswith("$argon2id$"))
        self.assertNotIn(TEST_PASSWORD, saved)

    def test_legacy_pbkdf2_bootstrap_hash_upgrades_to_argon2id_on_first_login(self):
        salt = b"synthetic legacy salt"
        iterations = 120_000
        digest = hashlib.pbkdf2_hmac("sha256", TEST_PASSWORD.encode("utf-8"), salt, iterations)
        encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        os.environ["HISTORY_GRADER_ADMIN_PASSWORD_HASH"] = (
            f"pbkdf2_sha256${iterations}${encode(salt)}${encode(digest)}"
        )
        status, _, _ = self.login(TEST_USERNAME)
        self.assertEqual(status, 200)
        conn = sqlite3.connect(app.DB_PATH)
        try:
            stored = conn.execute(
                "SELECT password_hash FROM auth_users WHERE username = ?", (TEST_USERNAME,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertTrue(stored.startswith("$argon2id$"))
        self.assertNotIn(TEST_PASSWORD, stored)

    def test_wrong_password_and_account_ip_progressive_rate_limit(self):
        now = 1_800_000_000
        clock = [now]
        replies = []
        with patch.object(password_auth.time, "time", side_effect=lambda: clock[0]):
            for offset, client in zip((0, 3, 5, 8, 13), ("203.0.113.10", "203.0.113.11", "203.0.113.12", "203.0.113.13", "203.0.113.14")):
                clock[0] = now + offset
                replies.append(self.manager.login(TEST_USERNAME, "synthetic-wrong-password", client)[0])
        self.assertEqual(replies[0], 401)
        self.assertTrue(all(status == 429 for status in replies[1:]))
        self.assertGreaterEqual(
            self.manager.login(TEST_USERNAME, "synthetic-wrong-password", "203.0.113.99")[1]["retry_after"],
            800,
        )

    def test_login_same_origin_required_and_cookie_has_secure_attributes(self):
        status, _, _ = self.login()
        self.assertEqual(status, 200)
        _, _, cookie_header = self.login()
        cookie = cookie_header.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn(f"Max-Age={30 * 24 * 60 * 60}", cookie)
        malicious, _, _ = self.request_json(
            "POST", "/api/auth/login", {"username": TEST_USERNAME, "password": TEST_PASSWORD},
            {"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(malicious, 403)

    def test_session_survives_manager_restart_expires_and_logout_revokes(self):
        status, data, headers = self.login()
        self.assertEqual(status, 200)
        cookie = self.cookie_value(headers)
        restarted_manager = web_auth.SingleTeacherAuth()
        self.assertIsNotNone(restarted_manager.authenticate(cookie))
        session_cookie = cookie.split("=", 1)[1]
        token_hash = hashlib.sha256(session_cookie.encode("ascii")).hexdigest()
        conn = sqlite3.connect(app.DB_PATH)
        try:
            conn.execute("UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?", (int(time.time()) - 1, token_hash))
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(restarted_manager.authenticate(cookie))

        status, data, headers = self.login()
        cookie = self.cookie_value(headers)
        csrf = data["csrf_token"]
        logout, _, logout_headers = self.request_json(
            "POST", "/api/auth/logout", {},
            {"Origin": self.origin, "Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(logout, 200)
        self.assertIn("Max-Age=0", logout_headers.get("Set-Cookie", ""))
        self.assertIsNone(web_auth.AUTH_MANAGER.authenticate(cookie))

    def test_csrf_and_malicious_origin_rejected_on_material_and_objective_paths(self):
        status, data, headers = self.login()
        self.assertEqual(status, 200)
        cookie = self.cookie_value(headers)
        denied, _, _ = self.request_json(
            "POST", "/api/retention/preview", {"retention_days": 0, "dry_run": True},
            {"Origin": self.origin, "Cookie": cookie},
        )
        self.assertEqual(denied, 403)
        evil, _, _ = self.request_json(
            "POST", "/api/retention/preview", {"retention_days": 0, "dry_run": True},
            {"Origin": "https://attacker.invalid", "Cookie": cookie, "X-CSRF-Token": data["csrf_token"]},
        )
        self.assertEqual(evil, 403)
        material, _, _ = self.request_json("GET", "/api/assignments", headers={"Cookie": cookie})
        self.assertEqual(material, 200)

    def test_email_otp_is_recovery_only_and_reset_revokes_sessions(self):
        status, _, login_headers = self.login()
        self.assertEqual(status, 200)
        old_cookie = self.cookie_value(login_headers)
        sent = []

        def capture(config, code, recipient=None, purpose=""):
            sent.append((code, recipient, purpose))

        with patch.object(password_auth, "_send_email_otp", side_effect=capture):
            requested, pending, _ = self.request_json(
                "POST", "/api/auth/password-reset/request", {"identity": TEST_EMAIL}, {"Origin": self.origin}
            )
        self.assertEqual(requested, 202)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], TEST_EMAIL)
        self.assertIn("密码重置", sent[0][2])

        otp_only_login, _, _ = self.request_json(
            "POST", "/api/auth/login", {"challenge_id": pending["challenge_id"], "email_code": sent[0][0]},
            {"Origin": self.origin},
        )
        self.assertEqual(otp_only_login, 401)
        wrong_code, _, _ = self.request_json(
            "POST", "/api/auth/password-reset/complete",
            {"challenge_id": pending["challenge_id"], "email_code": "111111", "new_password": "synthetic-new-password-456"},
            {"Origin": self.origin},
        )
        self.assertEqual(wrong_code, 401)
        reset, reset_data, reset_headers = self.request_json(
            "POST", "/api/auth/password-reset/complete",
            {"challenge_id": pending["challenge_id"], "email_code": sent[0][0], "new_password": "synthetic-new-password-456"},
            {"Origin": self.origin},
        )
        self.assertEqual(reset, 200)
        self.assertNotIn("Set-Cookie", reset_headers)
        self.assertIsNone(web_auth.AUTH_MANAGER.authenticate(old_cookie))
        old_login, _, _ = self.login(password=TEST_PASSWORD, client_ip="198.51.100.21")
        self.assertEqual(old_login, 401)
        new_login, new_data, new_headers = self.login(password="synthetic-new-password-456", client_ip="198.51.100.21")
        self.assertEqual(new_login, 200)
        self.assertIn("csrf_token", new_data)
        self.assertIn("history_grader_session=", new_headers.get("Set-Cookie", ""))
        conn = sqlite3.connect(app.DB_PATH)
        try:
            saved = conn.execute("SELECT password_hash FROM auth_users WHERE username = ?", (TEST_USERNAME,)).fetchone()[0]
        finally:
            conn.close()
        self.assertTrue(saved.startswith("$argon2id$"))
        self.assertNotIn("synthetic-new-password-456", saved)

    def test_reset_is_origin_checked_one_time_and_expiring(self):
        self.assertEqual(self.login()[0], 200)
        sent = []
        with patch.object(password_auth, "_send_email_otp", side_effect=lambda config, code, **kw: sent.append(code)):
            status, pending, _ = self.request_json(
                "POST", "/api/auth/password-reset/request", {"identity": TEST_USERNAME}, {"Origin": self.origin}
            )
        self.assertEqual(status, 202)
        evil, _, _ = self.request_json(
            "POST", "/api/auth/password-reset/complete",
            {"challenge_id": pending["challenge_id"], "email_code": sent[0], "new_password": "synthetic-new-password-789"},
            {"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(evil, 403)
        challenge_hash = hashlib.sha256(pending["challenge_id"].encode("ascii")).hexdigest()
        conn = sqlite3.connect(app.DB_PATH)
        try:
            conn.execute("UPDATE auth_otp_challenges SET expires_at = ? WHERE challenge_hash = ?", (int(time.time()) - 1, challenge_hash))
            conn.commit()
        finally:
            conn.close()
        expired, _, _ = self.request_json(
            "POST", "/api/auth/password-reset/complete",
            {"challenge_id": pending["challenge_id"], "email_code": sent[0], "new_password": "synthetic-new-password-789"},
            {"Origin": self.origin},
        )
        self.assertEqual(expired, 401)

    def test_email_change_requires_session_csrf_and_email_otp(self):
        status, session, headers = self.login()
        self.assertEqual(status, 200)
        cookie = self.cookie_value(headers)
        csrf = session["csrf_token"]
        sent = []
        with patch.object(password_auth, "_send_email_otp", side_effect=lambda config, code, **kw: sent.append((code, kw))):
            requested, pending, _ = self.request_json(
                "POST", "/api/auth/email-change/request", {"new_email": "new-teacher@example.test"},
                {"Origin": self.origin, "Cookie": cookie, "X-CSRF-Token": csrf},
            )
            self.assertEqual(requested, 202)
            self.assertEqual(sent[0][1]["recipient"], TEST_EMAIL)
            completed, _, _ = self.request_json(
                "POST", "/api/auth/email-change/complete",
                {"challenge_id": pending["challenge_id"], "email_code": sent[0][0]},
                {"Origin": self.origin, "Cookie": cookie, "X-CSRF-Token": csrf},
            )
        self.assertEqual(completed, 200)
        self.assertEqual(self.login(identity="new-teacher@example.test")[0], 200)
        self.assertEqual(self.login(identity=TEST_EMAIL)[0], 401)

    def test_auth_schema_migration_is_namespaced_idempotent_and_preserves_business_version(self):
        Path(app.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(app.DB_PATH)
        try:
            conn.execute("CREATE TABLE submissions (id TEXT PRIMARY KEY, marker TEXT)")
            conn.execute("INSERT INTO submissions VALUES ('synthetic-submission', 'keep')")
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        finally:
            conn.close()
        with password_auth._connect() as conn:
            conn.execute("SELECT 1 FROM auth_users")
        with password_auth._connect() as conn:
            conn.execute("SELECT 1 FROM auth_users")
        conn = sqlite3.connect(app.DB_PATH)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT marker FROM submissions WHERE id='synthetic-submission'").fetchone()[0], "keep")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue({"auth_schema_migrations", "auth_users", "auth_sessions", "auth_otp_challenges", "auth_rate_limits"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
