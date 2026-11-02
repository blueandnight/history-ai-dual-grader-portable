from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
from http.cookies import SimpleCookie
import ipaddress
import json
import os
import re
import secrets
import smtplib
import ssl
import struct
import threading
import time
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import parse_qs, urlparse, urlencode


from password_auth import (
    AUTH_MANAGER as _INITIAL_AUTH_MANAGER,
    COOKIE_NAME,
    AuthConfig,
    EMAIL_OTP_MAX_ATTEMPTS,
    EMAIL_OTP_TTL_SECONDS,
    LOGIN_WINDOW_SECONDS,
    MAX_LOGIN_ATTEMPTS,
    SESSION_TTL_DEFAULT,
    SESSION_TTL_MAX,
    SessionRecord,
    SingleTeacherAuth,
    _env_bool,
    _send_email_otp,
    _valid_email_address,
    _valid_totp_secret,
    _verify_totp,
    auth_config,
    cloud_mode_enabled,
    hash_password,
    SERVER_OPERATOR_ONLY_PATHS,
    web_auth_required,
)

AUTH_MANAGER = _INITIAL_AUTH_MANAGER

# Workspace grading APIs remain shared. Only system/account administration is restricted.
ADMIN_ONLY_PATHS = {
    "/api/status", "/api/settings", "/api/dual-review/status", "/api/dual-review/configuration",
    "/api/storage", "/api/backups", "/api/retention", "/api/retention/preview",
    "/api/backup/create", "/api/backup/restore", "/api/custom-models",
    "/api/custom-models/save", "/api/custom-models/delete", "/api/custom-models/list", "/api/diagnostics-export", "/api/mobile-token",
    "/api/mobile-token/revoke", "/api/restart", "/api/shutdown",
    "/api/auth/email-change/request", "/api/auth/email-change/complete",
}


def _admin_only(path: str, method: str) -> bool:
    return path == "/users" or path.startswith("/api/auth/users") or path in ADMIN_ONLY_PATHS or (
        method != "GET" and path == "/api/privacy-templates"
    )


def _send_json(handler: Any, value: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> None:
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return


def _read_small_json(handler: Any) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError("请求体长度无效") from exc
    if length <= 0 or length > 8192:
        raise ValueError("登录请求体为空或过大")
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError("登录请求未完整接收")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("登录请求格式无效") from exc
    if not isinstance(value, dict):
        raise ValueError("登录请求格式无效")
    return value


def _client_key(handler: Any) -> str:
    immediate = str(handler.client_address[0])
    config = auth_config(handler)
    if config.trust_proxy:
        try:
            if ipaddress.ip_address(immediate).is_loopback:
                forwarded = handler.headers.get("X-Real-IP", "").strip()
                if forwarded:
                    return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return immediate


def _same_origin(handler: Any) -> bool:
    origin = handler.headers.get("Origin", "").strip()
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().casefold()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    if not origin:
        return fetch_site == "same-origin"
    parsed = urlparse(origin)
    if parsed.username or parsed.password or not parsed.netloc:
        return False
    config = auth_config(handler)
    scheme = "https" if config.cookie_secure else "http"
    if config.trust_proxy:
        forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().casefold()
        if forwarded_proto in {"http", "https"}:
            scheme = forwarded_proto
    expected = f"{scheme}://{handler.headers.get('Host', '').strip()}".casefold()
    return origin.rstrip("/").casefold() == expected.rstrip("/")


def apply_single_teacher_auth(base_handler: type) -> type:
    class SingleTeacherHandler(base_handler):
        def _local_request_allowed(self, *, write: bool = False) -> bool:
            """Protect the password-free local service from browser cross-site requests."""
            if web_auth_required(self):
                return True  # Cloud authentication/origin/CSRF behavior is unchanged.
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                _send_json(self, {"error": "请求主机校验失败", "code": "local_host_rejected"}, 403)
                return False
            try:
                parsed = urlparse("http://" + hosts[0])
                actual_host = self.connection.getsockname()[0]
                allowed = {"localhost", "127.0.0.1", "::1", actual_host}
                bound_host, port = self.server.server_address[:2]
                if bound_host not in {"0.0.0.0", "::", ""}:
                    allowed.add(bound_host)
                valid = (not parsed.username and not parsed.password and not parsed.path and not parsed.query
                         and not parsed.fragment and parsed.hostname in allowed and (parsed.port or 80) == port)
            except (ValueError, AttributeError):
                valid = False
            if not valid:
                _send_json(self, {"error": "请求主机校验失败", "code": "local_host_rejected"}, 403)
                return False
            origin = self.headers.get("Origin", "").strip()
            referer = self.headers.get("Referer", "").strip()
            site = self.headers.get("Sec-Fetch-Site", "").casefold()
            expected = "http://" + hosts[0].casefold()
            try:
                candidate = urlparse(origin or referer)
                same = (candidate.scheme + "://" + candidate.netloc).casefold() == expected
                same = same and not candidate.username and not candidate.password
            except ValueError:
                same = False
            api = urlparse(self.path).path.startswith(("/api/", "/objective/api/"))
            if (origin or (write and referer)) and not same or (write or api) and site in {"cross-site", "same-site"}:
                _send_json(self, {"error": "请求来源校验失败", "code": "local_origin_rejected"}, 403)
                return False
            # Header-less native clients remain compatible. Browsers supplying
            # fetch metadata must positively identify a same-origin request.
            if write and not origin and site and site != "same-origin":
                _send_json(self, {"error": "请求来源校验失败", "code": "local_origin_rejected"}, 403)
                return False
            if write:
                kind = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
                multipart = urlparse(self.path).path in {"/api/submissions", "/api/student-auto-crop"} or urlparse(self.path).path.startswith("/objective/api/")
                if kind != "application/json" and not (multipart and kind == "multipart/form-data"):
                    _send_json(self, {"error": "请求格式必须为JSON或受支持的上传格式", "code": "local_content_type_rejected"}, 415)
                    return False
            return True

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            embedded_objective = urlparse(self.path).path in {"/objective", "/objective/"} and parse_qs(urlparse(self.path).query).get("embedded") == ["1"]
            self.send_header("X-Frame-Options", "SAMEORIGIN" if embedded_objective else "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                "connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "object-src 'none'; base-uri 'none'; form-action 'self'; "
                + ("frame-ancestors 'self'" if embedded_objective else "frame-ancestors 'none'"),
            )
            if self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-store")
            mobile_token_cookie = getattr(self, "_mobile_token_set_cookie", "")
            if mobile_token_cookie:
                self.send_header("Set-Cookie", mobile_token_cookie)
            super().end_headers()

        def _auth_session(self) -> SessionRecord | None:
            return AUTH_MANAGER.authenticate(self.headers.get("Cookie", ""), self)

        def is_local_client(self) -> bool:
            if web_auth_required(self):
                return self._auth_session() is not None
            return super().is_local_client()

        def _require_web_session(self) -> bool:
            session = self._auth_session()
            if session is None:
                config = auth_config(self)
                _send_json(
                    self,
                    {
                        "error": "请先登录教师账户",
                        "authentication_required": True,
                        "configured": AUTH_MANAGER.is_configured(config, self),
                    },
                    401,
                )
                return False
            self._web_auth_session = session
            return True

        def require_api_access(self, path: str = "") -> bool:
            if web_auth_required(self):
                session = getattr(self, "_web_auth_session", None)
                if session is not None:
                    if _admin_only(path, self.command) and session.role != "admin":
                        _send_json(self, {"error": "此操作仅限管理员"}, 403)
                        return False
                    return True
                # Anonymous bearer tokens are still limited by AppHandler's
                # existing mobile path allowlist; they never become sessions.
                return super().require_api_access(path)
            return super().require_api_access(path)

        def require_local_client(self) -> bool:
            if web_auth_required(self):
                if getattr(self, "_web_auth_session", None) is not None:
                    return True
                _send_json(self, {"error": "此操作需要已登录的教师账户"}, 403)
                return False
            return super().require_local_client()

        def do_GET(self) -> None:
            if not self._local_request_allowed():
                return
            path = urlparse(self.path).path
            if path == "/api/health":
                from pdf_renderer import pdf_renderer_available
                _send_json(self, {"ok": True, "service": "history-grader", "pdf_renderer": pdf_renderer_available()})
                return
            if path == "/api/auth/session":
                config = auth_config(self)
                required = web_auth_required(self)
                if not required:
                    _send_json(self, {"required": False, "configured": False, "authenticated": True, "cloud_mode": False})
                    return
                session = self._auth_session()
                if session is None:
                    _send_json(
                        self,
                        {
                            "required": True,
                            "configured": AUTH_MANAGER.is_configured(config, self),
                            "authenticated": False,
                            "cloud_mode": cloud_mode_enabled(self),
                            "totp_required": False,
                            "email_otp_required": False,
                            "password_reset_enabled": config.password_reset_configured,
                            "trusted_session_days": 30,
                            "error": "请先登录教师账户",
                        },
                        401,
                    )
                    return
                self._web_auth_session = session
                _send_json(self, {
                    "required": True,
                    "configured": True,
                    "cloud_mode": cloud_mode_enabled(self),
                    "totp_required": False,
                    "email_otp_required": False,
                    "password_reset_enabled": config.password_reset_configured and session.role == "admin",
                    "trusted_session_days": 30,
                    **AUTH_MANAGER.public_session(session),
                })
                return
            if web_auth_required(self) and path == "/login":
                session = self._auth_session()
                if session is not None:
                    self._web_auth_session = session
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>阅卷工作台登录</title><link rel="stylesheet" href="/static/auth.css"><script src="/static/login.js" defer></script></head>
<body><main class="auth-gate"><form class="auth-card" id="workbench-login" autocomplete="on">
<span class="eyebrow">私人试用 · 安全登录</span><h2>阅卷工作台登录</h2>
<p id="auth-message">正在检查服务器登录配置。</p>
<label id="auth-username-row">邮箱或用户名<input id="auth-username" name="username" autocomplete="username" value="trial-" maxlength="254" required></label>
<label id="auth-password-row">密码<input id="auth-password" name="password" type="password" autocomplete="current-password" maxlength="512" required></label>
<button class="button primary" type="submit">登录工作台</button>
<button class="button" id="forgot-password" type="button">管理员忘记密码？通过绑定邮箱重置</button>
<p class="auth-error" id="auth-error" role="alert" aria-live="polite"></p>
 </form>
<form class="auth-card" id="password-reset" autocomplete="on" hidden>
<span class="eyebrow">账户恢复</span><h2>重置密码</h2>
<p id="reset-message">输入账户邮箱或用户名，验证码将发送到绑定邮箱。</p>
<label>邮箱或用户名<input id="reset-identity" autocomplete="username" maxlength="254" required></label>
<button class="button" id="request-reset-code" type="button">发送重置验证码</button>
<label id="reset-code-row" hidden>邮箱验证码<input id="reset-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6"></label>
<label id="reset-password-row" hidden>新密码<input id="reset-password-value" type="password" autocomplete="new-password" minlength="12" maxlength="512"></label>
<label id="reset-confirm-row" hidden>再次输入新密码<input id="reset-password-confirm" type="password" autocomplete="new-password" minlength="12" maxlength="512"></label>
<button class="button primary" id="complete-reset" type="submit" hidden>重置密码</button>
<button class="button" id="back-to-login" type="button">返回登录</button>
<p class="auth-error" id="reset-error" role="alert" aria-live="polite"></p>
</form></main></body></html>"""
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, private")
                self.end_headers()
                self.wfile.write(body)
                return
            is_business_html = path in {"/", "/index.html", "/legacy", "/legacy/", "/legacy/index.html", "/users", "/objective", "/objective/", "/objective/login", "/objective/static/index.html", "/next", "/next/", "/next/index.html"} or (
                path.startswith("/static/")
                and path.replace("\\", "/").rsplit("/", 1)[-1].casefold() == "index.html"
            )
            if web_auth_required(self) and is_business_html:
                session = self._auth_session()
                if session is None:
                    mobile_token = self.mobile_page_token() if path in {"/", "/index.html"} else ""
                    if not mobile_token:
                        self.send_response(303)
                        login_target = "/next/login" if path.startswith("/next") or path in {"/", "/index.html"} else "/login"
                        if path in {"/objective", "/objective/"}:
                            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                            if query.get("embedded") != ["1"] and query.get("legacy") != ["1"]:
                                context = {target: query[source][0] for source, target in (("exam_id", "exam"), ("assessment_id", "assessment")) if source in query}
                                login_target = "/next/login#/objective" + ("?" + urlencode(context) if context else "")
                        self.send_header("Location", login_target)
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        return
                    self._mobile_token_only = True
                    query_token = parse_qs(urlparse(self.path).query).get("access_token", [""])[0].strip()
                    if query_token and query_token == mobile_token:
                        self._mobile_token_set_cookie = (
                            "history_grader_mobile=" + mobile_token
                            + "; Max-Age=2700; Path=/; Secure; HttpOnly; SameSite=Strict"
                        )
                else:
                    self._web_auth_session = session
            is_objective_api = path.startswith("/objective/api/")
            if web_auth_required(self) and _admin_only(path, "GET"):
                self._web_auth_session = self._auth_session()
                if not self.require_api_access(path) or not self._require_web_session():
                    return
                if path == "/api/auth/users":
                    status, response = AUTH_MANAGER.manage_users(self._web_auth_session, "list", {}, self)
                    _send_json(self, response, status)
                    return
                if path == "/users":
                    self.path = "/index.html"
            if web_auth_required(self) and (path.startswith("/api/") or is_objective_api):
                session = self._auth_session()
                if session is not None:
                    self._web_auth_session = session
                elif not self.require_api_access(path):
                    return
            super().do_GET()

        def do_POST(self) -> None:
            if not self._local_request_allowed(write=True):
                return
            path = urlparse(self.path).path
            if not web_auth_required(self):
                super().do_POST()
                return
            if not _same_origin(self):
                _send_json(self, {"error": "请求来源校验失败"}, 403)
                return
            if path in {"/api/auth/password-reset/request", "/api/auth/password-reset/complete"}:
                try:
                    payload = _read_small_json(self)
                except ValueError as exc:
                    _send_json(self, {"error": str(exc)}, 400)
                    return
                client_key = _client_key(self)
                if path.endswith("/request"):
                    status, response = AUTH_MANAGER.request_password_reset(
                        str(payload.get("identity", "")), client_key, self
                    )
                else:
                    status, response = AUTH_MANAGER.complete_password_reset(
                        str(payload.get("challenge_id", "")),
                        str(payload.get("email_code", "")).strip(),
                        str(payload.get("new_password", "")),
                        client_key,
                        self,
                    )
                _send_json(self, response, status)
                return
            if path == "/api/auth/login":
                try:
                    payload = _read_small_json(self)
                except ValueError as exc:
                    _send_json(self, {"error": str(exc)}, 400)
                    return
                username = str(payload.get("username", "")).strip()[:254]
                password = str(payload.get("password", ""))
                status, response, cookie = AUTH_MANAGER.login(
                    username,
                    password,
                    _client_key(self),
                    handler=self,
                )
                headers = {"Set-Cookie": cookie} if cookie else None
                _send_json(self, response, status, headers)
                return
            session = self._auth_session()
            if session is None:
                if not self.require_api_access(path):
                    return
                super().do_POST()
                return
            self._web_auth_session = session
            if not self.require_api_access(path):
                return
            supplied_csrf = self.headers.get("X-CSRF-Token", "").strip()
            if not supplied_csrf or not secrets.compare_digest(supplied_csrf, session.csrf_token):
                _send_json(self, {"error": "CSRF 校验失败，请刷新页面后重试"}, 403)
                return
            if path == "/api/auth/logout":
                cookie = AUTH_MANAGER.logout(self.headers.get("Cookie", ""), self)
                _send_json(self, {"ok": True}, 200, {"Set-Cookie": cookie})
                return
            if path.startswith("/api/auth/users/"):
                try:
                    payload = _read_small_json(self)
                except ValueError as exc:
                    _send_json(self, {"error": str(exc)}, 400)
                    return
                status, response = AUTH_MANAGER.manage_users(session, path.removeprefix("/api/auth/users/"), payload, self)
                _send_json(self, response, status)
                return
            if path in {"/api/auth/email-change/request", "/api/auth/email-change/complete"}:
                try:
                    payload = _read_small_json(self)
                except ValueError as exc:
                    _send_json(self, {"error": str(exc)}, 400)
                    return
                if path.endswith("/request"):
                    status, response = AUTH_MANAGER.request_email_change(
                        session,
                        str(payload.get("new_email", "")),
                        _client_key(self),
                        self,
                    )
                else:
                    status, response = AUTH_MANAGER.complete_email_change(
                        session,
                        str(payload.get("challenge_id", "")),
                        str(payload.get("email_code", "")).strip(),
                        self.headers.get("Cookie", ""),
                        _client_key(self),
                        self,
                    )
                _send_json(self, response, status)
                return
            if path in SERVER_OPERATOR_ONLY_PATHS:
                _send_json(
                    self,
                    {"error": "网站版的服务重启和退出只能由服务器管理员执行"},
                    403,
                )
                return
            if path in {"/api/custom-models/save", "/api/custom-models/delete"}:
                _send_json(
                    self,
                    {"error": "网站版只允许选择服务器已配置的模型；模型预设和接口 Key 由服务器管理员维护"},
                    403,
                )
                return
            super().do_POST()

    SingleTeacherHandler.__name__ = base_handler.__name__
    SingleTeacherHandler.__qualname__ = base_handler.__qualname__
    return SingleTeacherHandler
