"""Support ZIP from selected metadata; never copy business payloads or files."""
import io
import json
import re
import zipfile


def build_bundle(*, version, commit, schema, objective_schema, counts, audit, python, platform):
    states = {}
    for state, count in counts.get('states', {}).items():
        key = state if state in {'queued', 'grading', 'completed', 'failed', 'recovery_pending'} else 'other'
        states[key] = states.get(key, 0) + int(count)
    known = {'submission_created', 'submission_completed', 'submission_confirmed', 'submission_failed',
             'dual_review_model_configured', 'custom_model_preset_created', 'custom_model_preset_updated',
             'custom_model_preset_deleted', 'batch_created'}
    events = [{'event_type': e.get('event_type') if e.get('event_type') in known else 'other'}
              for e in audit.get('events', [])[:8]]
    payload = {'diagnostics_schema': 'history-grader-diagnostics-v1', 'app_version': str(version),
               'git_commit': str(commit) if re.fullmatch(r'[0-9a-f]{7,40}', str(commit)) else 'unknown',
               'db_schema_version': schema, 'objective_schema_version': objective_schema,
               'python': python, 'platform': platform,
               'counts': {'assignments': int(counts['assignments']), 'submissions_by_state': states,
                          'audit_events': int(counts['audit_events'])},
               'audit_chain': {'event_count': audit.get('event_count', 0), 'chain_valid': audit.get('chain_valid'), 'events': events}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('diagnostics.json', json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        archive.writestr('README.txt', '历史学科 AI 双评阅卷系统诊断包\n仅含版本、schema、运行环境、状态计数及筛选后的审计类型。\n不包含学生身份、答案、图片、数据库、密钥、路径或模型请求响应。\n')
    return output.getvalue()
