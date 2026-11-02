from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import app
import password_auth
from tests import test_password_auth as existing_auth


class TeacherAccountTests(existing_auth.PasswordAuthTests):
    def test_teacher_can_use_roster_and_correct_formal_identity(self):
        admin = self.admin_headers()
        self.create_teacher(admin)
        teacher = self.teacher_headers()
        status, first, _ = self.request_json('POST', '/api/roster-student', {
            'class_name': '合成身份班级', 'identity_code': '01', 'display_name': '合成甲',
        }, teacher)
        self.assertEqual(status, 200)
        status, second, _ = self.request_json('POST', '/api/roster-student', {
            'class_name': '合成身份班级', 'identity_code': '02', 'display_name': '合成乙',
        }, teacher)
        self.assertEqual(status, 200)
        conn = app.get_db()
        try:
            conn.execute("INSERT INTO assignments (id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES ('aaaaaaaaaaaa','合成题','题目','答案','[]','[]',?)", (app.utc_now(),))
            conn.commit()
        finally:
            conn.close()
        status, batch, _ = self.request_json('POST', '/api/batches', {
            'assignment_id': 'aaaaaaaaaaaa', 'class_name': '合成身份班级', 'identity_mode': 'filename',
            'batch_request_id': 'teacher-identity', 'items': [{'page_count': 1, 'filenames': ['unknown.jpg']}],
        }, teacher)
        self.assertEqual(status, 201)
        item = batch['items'][0]
        conn = app.get_db()
        try:
            conn.execute("INSERT INTO submissions (id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,marking_code_id,marking_code,exam_id) VALUES (?,?,?,?,?,?,?,'queued',?,?,?)",
                         (item['submission_id'], 'aaaaaaaaaaaa', item['marking_code'], '', '[]', '{}', app.utc_now(), item['marking_code_id'], item['marking_code'], batch['exam_id']))
            conn.commit()
        finally:
            conn.close()
        status, _, _ = self.request_json('POST', '/api/bind-student', {
            'submission_id': item['submission_id'], 'roster_student_id': second['student_id'],
        }, teacher)
        self.assertEqual(status, 200)
        status, mappings, _ = self.request_json('GET', '/api/exam-mapping?exam_id=' + batch['exam_id'], headers=teacher)
        self.assertEqual(status, 200)
        self.assertEqual(mappings['mappings'][0]['roster_student_id'], second['student_id'])
        self.assertEqual(self.request_json('GET', '/api/rosters', headers=teacher)[0], 200)
        self.assertEqual(self.request_json('GET', '/api/auth/users', headers=teacher)[0], 403)

    def admin_headers(self):
        status, data, headers = self.login()
        self.assertEqual(status, 200)
        self.assertEqual(data['role'], 'admin')
        return {'Cookie': self.cookie_value(headers), 'X-CSRF-Token': data['csrf_token'], 'Origin': self.origin}

    def create_teacher(self, headers):
        status, _, _ = self.request_json('POST', '/api/auth/users/create', {
            'username': 'test_teacher', 'password': 'synthetic-teacher-password-123',
        }, headers)
        self.assertEqual(status, 200)

    def teacher_headers(self):
        with patch.object(password_auth, '_send_email_otp') as sender:
            status, data, headers = self.login('test_teacher', 'synthetic-teacher-password-123')
        self.assertEqual(status, 200)
        self.assertEqual(data['role'], 'teacher')
        sender.assert_not_called()
        return {'Cookie': self.cookie_value(headers), 'X-CSRF-Token': data['csrf_token'], 'Origin': self.origin}

    def test_create_without_email_shared_workspace_and_permissions(self):
        admin = self.admin_headers()
        self.create_teacher(admin)
        teacher = self.teacher_headers()
        with password_auth._connect() as conn:
            row = conn.execute("SELECT email, password_hash, role, enabled FROM auth_users WHERE username='test_teacher'").fetchone()
            self.assertIsNone(row['email'])
            self.assertTrue(row['password_hash'].startswith('$argon2id$'))
            self.assertEqual((row['role'], row['enabled']), ('teacher', 1))
        status, session, _ = self.request_json('GET', '/api/auth/session', headers=teacher)
        self.assertEqual(status, 200)
        self.assertFalse(session['password_reset_enabled'])
        self.assertFalse(session['email_otp_required'])
        self.assertFalse(session['totp_required'])
        for route in ['/api/auth/users', '/users', '/api/status', '/api/custom-models', '/api/storage', '/api/backups', '/api/dual-review/status']:
            self.assertEqual(self.request_json('GET', route, headers=teacher)[0], 403, route)
        for route in ['/api/auth/users/create', '/api/auth/users/reset-password', '/api/auth/users/disable', '/api/auth/users/enable', '/api/settings', '/api/dual-review/configuration', '/api/backup/restore', '/api/auth/email-change/request']:
            self.assertEqual(self.request_json('POST', route, {'username': existing_auth.TEST_USERNAME, 'role': 'admin'}, teacher)[0], 403, route)
        # No ownership filtering: both sessions read the same business workspace.
        app.get_db().close()
        a = self.request_json('GET', '/api/assignments', headers=admin)
        t = self.request_json('GET', '/api/assignments', headers=teacher)
        self.assertEqual(a[0], 200)
        self.assertEqual(a[:2], t[:2])
        self.assertEqual(self.request_json('GET', '/api/mobile/dual-review-readiness', headers=teacher)[0], 200)
        for route in ['/api/import-exam-draft', '/api/exam-layout-profile', '/api/review', '/api/confirm']:
            status = self.request_json('POST', route, {}, teacher)[0]
            self.assertNotIn(status, (401, 403), route)

    def test_reset_revokes_sessions_old_password_fails_new_password_works(self):
        admin = self.admin_headers(); self.create_teacher(admin); teacher = self.teacher_headers()
        status, _, _ = self.request_json('POST', '/api/auth/users/reset-password', {
            'username': 'test_teacher', 'password': 'synthetic-new-password-456',
        }, admin)
        self.assertEqual(status, 200)
        self.assertEqual(self.request_json('GET', '/api/assignments', headers=teacher)[0], 401)
        self.assertEqual(self.login('test_teacher', 'synthetic-teacher-password-123')[0], 401)
        self.assertEqual(self.login('test_teacher', 'synthetic-new-password-456')[0], 200)

    def test_disable_enable_and_authentication_checks_current_db_status(self):
        admin = self.admin_headers(); self.create_teacher(admin); teacher = self.teacher_headers()
        # Check the join in authenticate, not only session deletion in disable.
        with password_auth._connect() as conn:
            conn.execute("UPDATE auth_users SET enabled=0 WHERE username='test_teacher'")
        self.assertEqual(self.request_json('GET', '/api/assignments', headers=teacher)[0], 401)
        self.assertEqual(self.request_json('POST', '/api/auth/users/disable', {'username': 'test_teacher'}, admin)[0], 200)
        self.assertEqual(self.login('test_teacher', 'synthetic-teacher-password-123')[0], 401)
        self.assertEqual(self.request_json('POST', '/api/auth/users/enable', {'username': 'test_teacher'}, admin)[0], 200)
        self.assertEqual(self.request_json('GET', '/api/assignments', headers=teacher)[0], 401)
        self.assertEqual(self.login('test_teacher', 'synthetic-teacher-password-123')[0], 200)

    def test_admin_protection_unique_username_and_csrf(self):
        admin = self.admin_headers(); self.create_teacher(admin)
        self.assertEqual(self.request_json('POST', '/api/auth/users/create', {'username': 'TEST_TEACHER', 'password': 'synthetic-password-123'}, admin)[0], 409)
        self.assertEqual(self.request_json('POST', '/api/auth/users/create', {'username': 'new_admin', 'password': 'synthetic-password-123', 'role': 'admin'}, admin)[0], 400)
        for action in ['disable', 'reset-password', 'enable']:
            self.assertEqual(self.request_json('POST', '/api/auth/users/' + action, {'username': existing_auth.TEST_USERNAME, 'password': 'synthetic-password-123'}, admin)[0], 403)
        no_csrf = {k: v for k, v in admin.items() if k != 'X-CSRF-Token'}
        self.assertEqual(self.request_json('POST', '/api/auth/users/disable', {'username': 'test_teacher'}, no_csrf)[0], 403)
        status, users, _ = self.request_json('GET', '/api/auth/users', headers=admin)
        self.assertEqual(status, 200)
        self.assertTrue(all('password_hash' not in u and 'email' not in u for u in users['users']))
        self.assertEqual(self.login()[0], 200)

    def test_teacher_password_recovery_never_sends_mail(self):
        admin = self.admin_headers(); self.create_teacher(admin)
        with patch.object(password_auth, '_send_email_otp') as sender:
            status, _, _ = self.request_json('POST', '/api/auth/password-reset/request', {'identity': 'test_teacher'}, {'Origin': self.origin})
        self.assertEqual(status, 403)
        sender.assert_not_called()

    def test_auth_v1_copy_migration_preserves_existing_admin_and_business_rows(self):
        # A separate copy of the original v1 auth schema and an unrelated business row.
        source = Path(self.temp.name) / 'auth-v1.sqlite3'
        copy = Path(self.temp.name) / 'auth-copy.sqlite3'
        conn = sqlite3.connect(source)
        conn.executescript('''CREATE TABLE auth_schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER);
        INSERT INTO auth_schema_migrations VALUES(1, 1);
        CREATE TABLE auth_users(username TEXT PRIMARY KEY COLLATE NOCASE, email TEXT UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE business_marker(id TEXT PRIMARY KEY, score INTEGER);
        INSERT INTO business_marker VALUES('synthetic', 4);
        PRAGMA user_version=3;''')
        encoded = password_auth.hash_password('synthetic-legacy-admin-123')
        conn.execute('INSERT INTO auth_users VALUES(?,NULL,?,1,1)', ('legacy-admin', encoded)); conn.commit()
        target = sqlite3.connect(copy); conn.backup(target); target.close(); conn.close()
        target = sqlite3.connect(copy); password_auth._ensure_schema(target); password_auth._ensure_schema(target)
        self.assertEqual(target.execute('SELECT role,enabled,password_hash FROM auth_users').fetchone(), ('admin', 1, encoded))
        self.assertEqual(target.execute('SELECT * FROM business_marker').fetchall(), [('synthetic', 4)])
        self.assertEqual(target.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertEqual(target.execute('SELECT MAX(version) FROM auth_schema_migrations').fetchone()[0], 2)
        target.close()
        original = sqlite3.connect(source)
        self.assertNotIn('role', [r[1] for r in original.execute('PRAGMA table_info(auth_users)')]); original.close()
