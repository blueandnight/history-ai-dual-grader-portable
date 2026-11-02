"""Optional release UI smoke; isolated synthetic data, no model or mail calls."""
import json
import shutil
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from playwright.sync_api import sync_playwright

import app
import password_auth
from tests.test_teacher_accounts import TeacherAccountTests
from tests.test_password_auth import TEST_USERNAME, TEST_PASSWORD


def main():
    test = TeacherAccountTests()
    test.setUp()
    try:
        shutil.copytree(Path(app.__file__).parent / 'static', app.STATIC_DIR, dirs_exist_ok=True)
        with patch.object(app, 'open_model_request', side_effect=AssertionError('model forbidden')), patch.object(password_auth, '_send_email_otp', side_effect=AssertionError('mail forbidden')):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel='msedge')
                errors = []

                def new_page():
                    context = browser.new_context(viewport={'width': 1280, 'height': 900}, bypass_csp=True)
                    page = context.new_page()
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.on('dialog', lambda d: d.accept())
                    # Adapt loopback HTTP to the fixture's externally visible HTTPS
                    # origin; the real auth endpoints, cookies and CSRF still run.
                    def proxy(route):
                        req = route.request
                        headers = dict(req.headers)
                        if 'origin' in headers:
                            headers['origin'] = test.origin
                        request = Request(req.url, data=req.post_data_buffer, headers=headers, method=req.method)
                        try:
                            response = test.opener.open(request, timeout=15)
                        except HTTPError as response_error:
                            response = response_error
                        with response:
                            raw = response.read()
                            outgoing = dict(response.headers)
                            if 'Set-Cookie' in outgoing:
                                outgoing['Set-Cookie'] = outgoing['Set-Cookie'].replace('; Secure', '')
                            route.fulfill(status=response.status, headers=outgoing, body=raw)
                    page.route('**/api/**', proxy)
                    page.goto(test.base + '/login')
                    return context, page

                def login(page, username, password, role):
                    page.locator('#auth-username').fill(username)
                    page.locator('#auth-password').fill(password)
                    page.locator('#workbench-login button[type=submit]').click()
                    page.wait_for_function("role => window.HISTORY_GRADER_AUTH?.authenticated && window.HISTORY_GRADER_AUTH.role === role", arg=role)
                    page.wait_for_function("typeof state !== 'undefined' && typeof openBatchSubmissionForReview === 'function'")

                admin_context, admin = new_page()
                login(admin, TEST_USERNAME, TEST_PASSWORD, 'admin')
                admin.locator('.main-nav [data-app-view-target=users]').click()
                admin.locator('#teacher-username').fill('test_teacher')
                admin.locator('#teacher-password').fill('synthetic-teacher-password-123')
                admin.locator('#create-teacher-form button').click()
                row = admin.locator('.user-row').filter(has_text='test_teacher')
                row.wait_for()
                row.get_by_role('button', name='停用', exact=True).click()
                row.get_by_role('button', name='恢复', exact=True).click()
                row.get_by_role('button', name='重置密码', exact=True).click()
                admin.locator('#reset-teacher-password').fill('synthetic-new-password-123')
                admin.locator('#reset-teacher-form button[type=submit]').click()
                admin.wait_for_function("!document.querySelector('#reset-teacher-dialog').open")
                admin.set_viewport_size({'width': 390, 'height': 844})
                assert admin.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')

                teacher_context, teacher = new_page()
                login(teacher, 'test_teacher', 'synthetic-new-password-123', 'teacher')
                assert not teacher.locator('.main-nav [data-app-view-target=users]').is_visible()
                assert not teacher.locator('.main-nav [data-app-view-target=management]').is_visible()
                assert teacher.locator('#view-grading #roster-section').count() == 1
                assert teacher.evaluate("fetch('/api/auth/users').then(r => r.status)") == 403
                assert teacher.evaluate("fetch('/api/settings').then(r => r.status)") == 403
                # Rendering fixture only: no scoring endpoint or final-score write.
                point = {'question_id': 'Q1', 'rubric_point_id': 'C1', 'score_a': 2, 'score_b': 0, 'decision_a': 'full', 'decision_b': 'none', 'evidence_a': '合成答案', 'evidence_b': '合成答案'}
                result = {'mode': 'vision-model', 'max_total': 2, 'suggested_total': 2, 'blocked': False, 'sub_questions': [{'sub_question_id': 'Q1', 'label': '第一题', 'suggested_total': 2, 'max_total': 2, 'criteria': [{'criterion_id': 'C1', 'criterion': '合成评分点中文内容', 'max_score': 2}]}], 'grading_scope': {'graded_question_ids': ['Q1'], 'scoped_max_score': 2}, 'dual_review': {'status': 'complete', 'results': {'A': {'suggested_total': 2}, 'B': {'suggested_total': 0}}, 'comparison': {'risk_level': 'HIGH', 'point_comparisons': [point]}}}
                submission = {'id': 'release-ui-fixture', 'student_label': '匿名合成答卷', 'state': 'completed', 'sequence': 1, 'final_total': None, 'result': result, 'photos': [{'image_role': 'answer_region', 'answer_region_provenance': {'question_id': 'Q1'}}]}
                teacher.route('**/api/submission?submission_id=release-ui-fixture', lambda route: route.fulfill(json={'submission': submission}))
                # Use an in-memory synthetic image for the existing crop viewer.
                from PIL import Image
                import io
                image = io.BytesIO()
                Image.new('RGB', (1000, 800), 'white').save(image, format='JPEG')
                teacher.route('**/api/submission-photo?**', lambda route: route.fulfill(content_type='image/jpeg', body=image.getvalue()))
                teacher.evaluate("s => { selectGradingMode('batch'); renderBatchShell(1); renderBatchRow(s); }", submission)
                teacher.locator('.batch-open-review').click()
                teacher.locator('#batch-review-workspace .teacher-point-title').filter(has_text='合成评分点中文内容').wait_for()
                assert '本次满分：2 分' in teacher.locator('.teacher-scope-banner').first.inner_text()
                for label, choice in [('给分', 'teacher_full'), ('不给分', 'teacher_none'), ('不确定', 'teacher_uncertain')]:
                    teacher.locator('.teacher-quick-choices').get_by_role('button', name=label, exact=True).click()
                    assert teacher.locator('.dual-point-final-choice').input_value() == choice
                    assert teacher.locator('#final-score').input_value() == '2'
                teacher.wait_for_function("document.querySelector('.teacher-crop-zoom img')?.naturalWidth > 0")
                teacher.locator('.teacher-crop-zoom').click()
                teacher.locator('.teacher-crop-dialog').wait_for(state='visible')
                teacher.keyboard.press('Escape')
                teacher.set_viewport_size({'width': 390, 'height': 844})
                assert teacher.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                teacher.locator('#return-to-batch').click()
                teacher.evaluate("s => { selectGradingMode('single'); s.final_total=1; renderResult(s,null,{readOnly:true}); }", submission)
                assert teacher.locator('.teacher-readonly-notice').is_visible()
                assert teacher.locator('.teacher-quick-choice').count() == 0
                assert teacher.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                assert errors == [], errors
                print(json.dumps({'admin_login': 'passed', 'teacher_login_no_otp': 'passed', 'user_management': 'passed', 'teacher_permissions': 'passed', 'batch_review': 'passed', 'point_shortcuts_preserve_total': 'passed', 'crop_zoom': 'passed', 'history_readonly': 'passed', 'desktop': 'passed', '390px': 'passed', 'page_errors': errors, 'real_model_calls': 0}))
                teacher_context.close()
                admin_context.close()
                browser.close()
    finally:
        test.tearDown()


if __name__ == '__main__':
    main()
