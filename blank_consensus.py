"""Post-response blank consensus policy; never infers absence from a failed call."""
import copy
import hashlib
import json
import re

METHOD = 'automatic_blank_zero'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def observation(source, root):
    # Preserve the actual model statement before normalization replaces it with
    # a generic teacher-review message. A status label alone is insufficient.
    return copy.deepcopy(dict(status=source.get('answer_status'),
        confidence=source.get('confidence', root.get('confidence')),
        summary=source.get('summary', ''), criteria=source.get('criteria', []),
        suggested_total=source.get('suggested_total'), student_answer=source.get('student_answer'),
        blank_evidence=source.get('blank_evidence'),
        image_readable=source.get('image_readable'), region_complete=source.get('region_complete'),
        question_matched=source.get('question_matched')))


def definite(q):
    o = q.get('blank_observation') or {}
    if q.get('answer_status') != 'unanswered_candidate' or o.get('status') != 'unanswered_candidate':
        return False
    confidence = o.get('confidence')
    if type(confidence) not in (int, float) or not .9 <= confidence <= 1:
        return False
    if any(o.get(k) is False for k in ('image_readable', 'region_complete', 'question_matched')):
        return False
    if o.get('suggested_total') not in (None, 0) or o.get('student_answer'):
        return False
    criteria = o.get('criteria')
    if not isinstance(criteria, list) or any(not isinstance(p, dict) or p.get('student_evidence')
            or p.get('decision') not in (None, 'none') or p.get('suggested_score') not in (None, 0) for p in criteria):
        return False
    text = str(o.get('summary') or '') + ' ' + json.dumps(o.get('blank_evidence') or '', ensure_ascii=False)
    text += ' ' + ' '.join(str(p.get('reason') or '') for p in criteria)
    if re.search(r'疑似|可能|似乎|不确定|模糊|缺页|缺失|无法|未匹配|uncertain|maybe|blur|missing|unreadable', text, re.I):
        return False
    blank = re.search(r'未作答|没有作答|未书写|确实留白|确认空白|no answer|unanswered|blank', text, re.I)
    readable = o.get('image_readable') is True or re.search(r'清晰|可辨认|可辨读|readable|clear', text, re.I)
    complete = o.get('region_complete') is True or re.search(r'区域完整|完整可见|完整答题区域|region.{0,12}complete|fully visible', text, re.I)
    # Normalizer adds extra reasons for duplicate/unknown IDs. Never clear those.
    return bool(blank and readable and complete and len(q.get('block_reasons', [])) == 1)


def resolve(results, metadata, errors, questions, photos):
    """Return effective scores plus immutable raw evidence, or leave untouched."""
    if errors or set(results) != {'A', 'B'} or set(metadata) != {'A', 'B'} or not photos:
        return results, None
    a, b = metadata['A'], metadata['B']
    if any(m.get('status') != 'ok' or any(v in ('length', 'content_filter') for v in
            (m['finish_reason'] if isinstance(m.get('finish_reason'), list) else [m.get('finish_reason')])) for m in (a, b)):
        return results, None
    if not a.get('input_hash') or a.get('input_hash') != b.get('input_hash'):
        return results, None
    if any(not m.get('provider') or not m.get('model_id') for m in (a,b)) or (a.get('provider'), a.get('model_id')) == (b.get('provider'), b.get('model_id')):
        return results, None
    if any(p.get('quality', {}).get('ok') is False for p in photos):
        return results, None
    wanted = {q['id'] for q in questions}
    maps = []
    for side in ('A', 'B'):
        model = results[side]
        subs = model.get('sub_questions') or []
        ids = [q.get('sub_question_id') for q in subs]
        if (model.get('mode') != 'vision-model' or model.get('simulation_model') or model.get('unmapped_answers')
                or len(ids) != len(set(ids)) or set(ids) != wanted):
            return results, None
        maps.append({q['sub_question_id']: q for q in subs})
    ids = [q['id'] for q in questions if all(definite(m[q['id']]) for m in maps)]
    if not ids:
        return results, None
    effective = copy.deepcopy(results)
    for model in effective.values():
        old_reasons = {r for q in model['sub_questions'] if q['sub_question_id'] in ids for r in q.get('block_reasons', [])}
        for q in model['sub_questions']:
            if q['sub_question_id'] not in ids:
                continue
            rubric = next(v for v in questions if v['id'] == q['sub_question_id'])
            q.update(answer_status='blank_confirmed', blank_confirmation_method=METHOD,
                     suggested_total=0, confidence=q['blank_observation']['confidence'], blocked=False, needs_review=False, block_reasons=[],
                     summary='双模型独立确认答题区域完整清晰且未作答，自动计0分',
                     criteria=[dict(criterion_id=p['id'], decision='none', suggested_score=0,
                         max_score=p['score'], student_evidence='', needs_review=False,
                         reason='双侧明确空白，系统计零；非教师逐点确认', score_source=METHOD) for p in rubric['criteria']])
        model['block_reasons'] = [r for r in model.get('block_reasons', []) if r not in old_reasons]
        model['blocked'] = bool(model['block_reasons']) or any(q.get('blocked') for q in model['sub_questions'])
        model['requires_question_mapping'] = bool(model.get('unmapped_answers')) or any(
            q.get('answer_status') in ('unanswered_candidate','question_unmatched','image_unreadable') for q in model['sub_questions'])
        values = [q.get('suggested_total') for q in model['sub_questions']]
        model['suggested_total'] = sum(v for v in values if type(v) in (int, float))
        model['needs_review'] = model['blocked'] or any(q.get('needs_review') for q in model['sub_questions'])
    return effective, dict(policy_version=1, question_ids=ids, raw_results_sha256=digest(results),
                           effective_results_sha256=digest(effective), models_sha256=digest(metadata))


def valid(result):
    dual = result.get('dual_review') or {}
    receipt = dual.get('blank_resolution') or {}
    return bool(dual.get('status') == 'complete' and not dual.get('errors')
        and receipt.get('policy_version') == 1 and receipt.get('question_ids')
        and receipt.get('raw_results_sha256') == digest(dual.get('raw_results'))
        and receipt.get('effective_results_sha256') == digest(dual.get('results'))
        and receipt.get('models_sha256') == digest(dual.get('models')))


def auto_confirm(conn, sid, api):
    """Called inside the grading write transaction, never during a GET."""
    from batch_adoption import version
    import teacher_total
    row = teacher_total.read(conn, sid)
    result = json.loads(row['result_json'] or '{}')
    if (row['confirmed_at'] or result.get('simulation_model') or result.get('blocked')
            or row['uncertain_model_call'] or not row['ai_privacy_confirmed']
            or row['privacy_status'] not in api.PRIVACY_STATUS_APPROVED or not valid(result)):
        return False
    if row['exam_id'] and not conn.execute('SELECT 1 FROM grading_exams WHERE id=? AND assignment_id=?', (row['exam_id'], row['assignment_id'])).fetchone():
        return False
    try:
        questions, scope = version(row, result, api)
    except (ValueError, KeyError, TypeError):
        return False
    receipt = result['dual_review']['blank_resolution']
    if set(receipt['question_ids']) != {q['id'] for q in questions}:
        return False
    if not all(q.get('answer_status') == 'blank_confirmed' and q.get('suggested_total') == 0 for q in result.get('sub_questions', [])):
        return False
    when = api.utc_now()
    record = dict(method=METHOD, final_total=0, max_score=scope['scoped_max_score'], grading_scope=scope,
        confirmed_at=when, actor='system:dual_blank_consensus', teacher_confirmed=False,
        content_hash=result['subjective_version_evidence']['content_hash'], receipt_sha256=digest(receipt))
    result.update(confirmation_method=METHOD, automatic_blank_zero=record, teacher_reconfirmation_required=False)
    conn.execute('UPDATE submissions SET final_total=0,confirmed_at=?,result_json=? WHERE id=?', (when,json.dumps(result,ensure_ascii=False),sid))
    api.append_audit_event(conn, METHOD, 'submission', sid, record)
    return True
