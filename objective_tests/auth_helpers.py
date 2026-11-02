from __future__ import annotations

import os
from flask.testing import FlaskClient

import web_auth

TEST_USERNAME = "synthetic-teacher"
TEST_PASSWORD = "SYNTHETIC-ONLY-password-384!"
os.environ["HISTORY_GRADER_WEB_AUTH_REQUIRED"] = "1"
os.environ["HISTORY_GRADER_CLOUD_MODE"] = "0"
os.environ["HISTORY_GRADER_COOKIE_SECURE"] = "0"
os.environ["HISTORY_GRADER_ADMIN_USERNAME"] = TEST_USERNAME
os.environ["HISTORY_GRADER_ADMIN_PASSWORD_HASH"] = web_auth.hash_password(TEST_PASSWORD, iterations=100_000)
os.environ["HISTORY_GRADER_TOTP_REQUIRED"] = "0"
os.environ["HISTORY_GRADER_EMAIL_OTP_REQUIRED"] = "0"

class ObjectiveTestClient(FlaskClient):
    csrf_token = ""

    def open(self, *args, **kwargs):
        method = str(kwargs.get("method", "GET")).upper()
        if method == "POST":
            headers = kwargs.setdefault("headers", {})
            if hasattr(headers, "setdefault"):
                headers.setdefault("Origin", "http://localhost")
                if self.csrf_token:
                    headers.setdefault("X-CSRF-Token", self.csrf_token)
        response = super().open(*args, **kwargs)
        # Werkzeug closes its multipart request-body TemporaryFile when the
        # TestResponse is closed. Real clients own that socket stream, but
        # tests otherwise leave large multipart bodies open until GC.
        request_body = response.request.environ.get("wsgi.input")
        try:
            response.get_data()
        finally:
            response.close()
            close_body = getattr(request_body, "close", None)
            if close_body is not None:
                close_body()
        return response

def login_client(client: FlaskClient) -> FlaskClient:
    response = client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    if response.status_code != 200:
        raise AssertionError(f"synthetic test login failed: {response.status_code}")
    session = client.get("/api/auth/session")
    if session.status_code != 200:
        raise AssertionError(f"synthetic test session failed: {session.status_code}")
    client.csrf_token = session.get_json()["csrf_token"]
    return client
