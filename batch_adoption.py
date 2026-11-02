"""Teacher batch adoption of verified AI consensus, distinct from point review."""
import hashlib
import json
import math
import secrets

from itsdangerous import URLSafeTimedSerializer, BadSignature

METHOD = 'teacher_batch_adoption'
TOKENS = URLSafeTimedSerializer(secrets.token_hex(32), salt='batch-ai-consensus-v1')


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('分数缺失或无效')
    return float(value)


def same(a, b):
    return abs(number(a) - number(b)) < 1e-9


def displayed_number(value):
    try:
        return number(value)
    except ValueError:
        return None


def version(row, result, api):
    source = json.loads(row['sub_questions_json'])
    cleaned = api.clean_sub_questions(source)
    scope = result.get('grading_scope') or {}
    ids = scope.get('graded_question_ids')
    if not isinstance(ids, list) or len(ids) != len(cleaned) or set(ids) != {q['id'] for q in cleaned}:
        raise ValueError('仅评分部分小题或范围不明，需逐点复核')
    questions, current_scope = api.resolve_grading_scope(cleaned, graded_question_ids=ids)
    if not same(scope.get('scoped_max_score'), current_scope['scoped_max_score']):
        raise ValueError('评分范围满分不一致')
    reason = api.grading_core.subjective_version_state(result.get('subjective_version_evidence'), assignment_question=row['question'], assignment_standard_answer=row['standard_answer'], source_questions=source, scoring_questions=questions, graded_question_ids=ids, scoped_max_score=scope['scoped_max_score'], grading_context=result.get('grading_context'), dual_review=result.get('dual_review'))
    if reason:
        raise ValueError('评分内容版本不同或缺少版本证据，需核实后复核')
    return questions, scope


def evaluate(row, api, conn=None):
    result = json.loads(row['result_json'] or '{}')
    dual = result.get('dual_review') or {}
    if row['state'] != 'completed':
        raise ValueError('尚未完成评分')
    if row['exam_id'] and row['exam_assignment_id'] != row['assignment_id']:
        raise ValueError('答卷正式考试归属异常，需核实')
    if row['confirmed_at']:
        raise ValueError('已有确认成绩')
    if (result.get('mode') != 'vision-model' or result.get('blocked') or result.get('simulation_model')
            or result.get('teacher_reconfirmation_required') or result.get('photos_discarded_unconfirmed')
            or result.get('photos_deleted') or result.get('unmapped_answers') or result.get('requires_question_mapping')
            or row['uncertain_model_call'] or api.result_has_unresolved_answer_status(result)):
        raise ValueError('存在评分、图片或题号异常，需人工复核')
    if row['privacy_status'] not in api.PRIVACY_STATUS_APPROVED or not row['ai_privacy_confirmed']:
        raise ValueError('缺少有效隐私确认')
    photos = json.loads(row['photos_json'])
    paths = api._submission_photo_paths(photos)
    if not photos or len(paths) != len(photos) or not all(p.is_file() for p in paths):
        raise ValueError('答卷图片缺失，需人工核实')
    for photo, path in zip(photos, paths):
        if path.stat().st_size > 40 * 1024 * 1024:
            raise ValueError('图片过大，需人工核实')
        if photo.get('sha256') and hashlib.sha256(path.read_bytes()).hexdigest() != photo['sha256']:
            raise ValueError('评分图片校验不一致，需人工复核')
    if dual.get('status') != 'complete' or dual.get('errors'):
        raise ValueError('双模型未完整成功')
    models = dual.get('models') or {}
    if any(models.get(s, {}).get('status') != 'ok' for s in ('A', 'B')):
        raise ValueError('模型状态缺失或失败')
    if not models['A'].get('input_hash') or models['A'].get('input_hash') != models['B'].get('input_hash'):
        raise ValueError('两模型评分输入不一致或无法验证')
    questions, scope = version(row, result, api)
    expected = {(str(q['id']), str(p['id'])): p for q in questions for p in q['criteria']}
    if not expected:
        raise ValueError('缺少评分点')
    results = dual.get('results') or {}
    a, b = results.get('A') or {}, results.get('B') or {}
    if any(m.get('mode') != 'vision-model' or m.get('simulation_model') for m in (a,b)):
        raise ValueError('模型结果来源无效或为模拟结果')
    for model in (a, b, result):
        subs = model.get('sub_questions') or []
        if (model.get('blocked') or api.result_has_unresolved_answer_status(model) or len(subs) != len(questions)
                or {q.get('sub_question_id') for q in subs} != {q['id'] for q in questions}):
            raise ValueError('小题结果不完整或需人工确认')
    comparison = api.compare_dual_review_results(a, b)
    if (comparison['risk_level'] not in ('NO_CONFLICT', 'LOW')
            or (dual.get('comparison') or {}).get('risk_level') not in ('NO_CONFLICT', 'LOW')):
        raise ValueError('双评有分歧或证据异常，需复核')
    points = []
    for side in (a, b):
        raw = [(q['sub_question_id'], p['criterion_id']) for q in side['sub_questions'] for p in q.get('criteria', [])]
        mapped = api._dual_point_rows(side)
        if len(raw) != len(set(raw)) or set(mapped) != set(expected):
            raise ValueError('评分点缺失、重复或无法对应')
        points.append(mapped)
    decisions = []
    for key, criterion in expected.items():
        pa, pb = points[0][key], points[1][key]
        decision = pa.get('decision')
        if decision not in ('full', 'none') or decision != pb.get('decision'):
            raise ValueError('评分点判断不同或不确定，需复核')
        expected_score = number(criterion['score']) if decision == 'full' else 0
        if not same(pa.get('suggested_score'), expected_score) or not same(pb.get('suggested_score'), expected_score):
            raise ValueError('评分点分数不同或与细则不一致')
        decisions.append(dict(question_id=key[0], rubric_point_id=key[1], decision=decision))
    # Reuse the existing capped point calculation; these are AI consensus inputs,
    # not invented teacher judgments. Persist them under consensus_points only.
    calculated = api.teacher_deterministic_score(questions, decisions)
    for model in (a, b, result):
        if not same(model.get('suggested_total'), calculated['final_total']) or not same(model.get('max_total'), scope['scoped_max_score']):
            raise ValueError('总分不一致或不能从评分点核实')
        subs = {q['sub_question_id']: q for q in model['sub_questions']}
        for q in calculated['question_subtotals']:
            if not same(subs[q['question_id']].get('suggested_total'), q['subtotal']) or not same(subs[q['question_id']].get('max_total'), q['max_score']):
                raise ValueError('小题给分不同或分值无效，需复核')
    return {**calculated, 'score_source':'ai_consensus_batch_adopted_v1', 'grading_scope':scope, 'consensus_points':decisions}


def proof(row):
    fields = ['id','assignment_id','exam_id','state','result_json','photos_json','privacy_status','ai_privacy_confirmed','uncertain_model_call','question','standard_answer','sub_questions_json','criteria_json']
    return hashlib.sha256(json.dumps([row[k] for k in fields], ensure_ascii=False).encode()).hexdigest()


def rows(conn, assignment_id, exam_id, batch_id=''):
    if not conn.execute('SELECT 1 FROM assignments WHERE id=?', (assignment_id,)).fetchone():
        raise ValueError('考试不存在或不可访问')
    if exam_id and not conn.execute('SELECT 1 FROM grading_exams WHERE id=? AND assignment_id=?', (exam_id, assignment_id)).fetchone():
        raise ValueError('正式考试不属于当前题目来源')
    if batch_id and not conn.execute('SELECT 1 FROM grading_batches WHERE id=? AND assignment_id=? AND (?=\'\' OR exam_id=?)',(batch_id,assignment_id,exam_id,exam_id)).fetchone():
        raise ValueError('批次不属于当前考试')
    return conn.execute('SELECT s.*,a.question,a.standard_answer,a.criteria_json,a.sub_questions_json,e.assignment_id AS exam_assignment_id,b.status AS batch_status,json_extract(b.manifest_json,\'$.grading_stop_requested\') AS grading_stopped FROM submissions s JOIN assignments a ON a.id=s.assignment_id LEFT JOIN grading_exams e ON e.id=s.exam_id LEFT JOIN grading_batches b ON b.id=s.batch_id WHERE s.assignment_id=? AND (?=\'\' OR s.exam_id=?) AND (?=\'\' OR s.batch_id=?) ORDER BY CASE WHEN ?<>\'\' THEN s.batch_sequence END,s.created_at,s.id LIMIT 500', (assignment_id, exam_id, exam_id,batch_id,batch_id,batch_id)).fetchall()


def model_display_issue(model, error):
    """Fixed display labels only; never expose a provider response or error message."""
    if model.get('finish_reason') == 'content_filter' or (isinstance(model.get('finish_reason'),list) and 'content_filter' in model['finish_reason']):
        return '服务商内容过滤，未获得完整评分；请核对，勿反复重试'
    if model.get('status') == 'error' or error:
        return {'timeout':'模型请求超时，需核对是否已计费',
                'network':'模型连接异常，需核对请求结果',
                'auth_or_permission':'模型认证或权限异常',
                'provider_transient':'服务商暂时不可用或限流',
                'request_or_response_validation':'模型返回结果校验失败',
                'invalid_provider_response':'未获得可识别的模型结果'}.get(error.get('error_category'), '模型调用未完整成功')
    return ''


def listing(conn, assignment_id, exam_id, actor, api, batch_id=''):
    papers, proofs = [], {}
    for row in rows(conn, assignment_id, exam_id, batch_id):
        try:
            result = json.loads(row['result_json'] or '{}')
        except ValueError:
            result = {}
        result = result if isinstance(result,dict) else {}
        dual = result.get('dual_review') if isinstance(result.get('dual_review'),dict) else {}
        raw_results = dual.get('results') if isinstance(dual.get('results'),dict) else {}
        results = {s:raw_results.get(s) if isinstance(raw_results.get(s),dict) else {} for s in ('A','B')}
        if not dual:
            results['A'] = result
        try:
            evaluate(row, api, conn)
            state, reason = 'adoptable', '双模型总分、各小题及评分点一致，门禁通过'
            proofs[row['id']] = {'proof':proof(row), 'exam_id':row['exam_id'] or ''}
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            state = 'confirmed' if row['confirmed_at'] and not result.get('teacher_reconfirmation_required') else 'processing' if row['state'] in ('queued','grading') else 'review'
            reason = str(exc) if isinstance(exc, ValueError) else '评分数据不完整，需人工复核'
            if state == 'confirmed' and not current_adoption(result, row):
                state, reason = 'review', '保留历史采纳成绩；当前题干或评分标准已变化，不能作为当前版本成绩'
            if row['state'] == 'queued' and row['grading_stopped']:
                state, reason = 'stopped', '已停止（未开始）'
        subs = []
        fresh_comparison = api.compare_dual_review_results(results.get('A') or {}, results.get('B') or {})
        fresh_points = fresh_comparison.get('point_comparisons', [])
        for q in result.get('sub_questions') or []:
            if not isinstance(q,dict):
                continue
            sides = [next((p for p in (results.get(s) or {}).get('sub_questions', []) if p.get('sub_question_id') == q.get('sub_question_id')), {}) for s in ('A','B')]
            conflict = any(p.get('question_id') == q.get('sub_question_id') and (p.get('decision_conflict') or p.get('score_conflict')) for p in fresh_points)
            subs.append({'id':q.get('sub_question_id'), 'max_score':displayed_number(q.get('max_total')), 'a':displayed_number(sides[0].get('suggested_total')), 'b':displayed_number(sides[1].get('suggested_total')), 'blank_candidate':q.get('answer_status')=='unanswered_candidate', 'blank_confirmed':q.get('answer_status')=='blank_confirmed', 'blank_auto':q.get('blank_confirmation_method')=='automatic_blank_zero', 'a_status':sides[0].get('answer_status') or 'missing', 'b_status':sides[1].get('answer_status') or 'missing', 'point_conflict':conflict})
        models = dual.get('models') if isinstance(dual.get('models'),dict) else {}
        errors = dual.get('errors') if isinstance(dual.get('errors'),dict) else {}
        issues = {s:model_display_issue(models.get(s) if isinstance(models.get(s),dict) else {}, errors.get(s) if isinstance(errors.get(s),dict) else {}) for s in ('A','B')}
        progress = ('recovery' if result.get('mode') in ('rules-demo','vision-model-error','processing-error') else 'grading' if row['state']=='grading' else 'stopped' if row['state']=='queued' and row['grading_stopped'] else 'paused' if row['state']=='queued' and row['batch_status']=='paused' else 'queued' if row['state']=='queued' else 'recovery' if row['state']=='recovery_pending' else 'failed' if row['state']=='failed' else state)
        if result.get('mode')=='rules-demo':
            issues['A']='尚未进行真实模型评分'
        elif not dual and result.get('mode') in ('vision-model-error','processing-error'):
            issues['A']='尚未获得有效模型评分'
        progress_reasons = {'grading':'正在等待模型评分返回', 'stopped':'已停止后续批改，尚未开始的答卷不会自动评分', 'paused':'批次已暂停，需先处理前序异常答卷或上传任务', 'queued':'等待前序答卷完成，尚未开始评分', 'recovery':'上次处理结果未能确认，需核对计费后人工恢复', 'failed':'处理失败，请到任务恢复查看，勿重复上传整批'}
        if progress in progress_reasons:
            reason = '本份未进行AI评分，需配置模型并确认原位恢复' if result.get('mode')=='rules-demo' else progress_reasons[progress]
        scope = result.get('grading_scope') if isinstance(result.get('grading_scope'),dict) else {}
        expected_ids = set(scope.get('graded_question_ids') or [q['id'] for q in subs])
        complete = {s:bool(subs) and not issues[s] and {q.get('sub_question_id') for q in results[s].get('sub_questions',[]) if isinstance(q,dict)}==expected_ids and all(q[s.lower()+'_status'] in ('answered','blank_confirmed') and q[s.lower()] is not None for q in subs) for s in ('A','B')}
        evidence_only = False
        if (dual.get('status') == 'complete' and not dual.get('errors')
                and all(models.get(s, {}).get('status') == 'ok' for s in ('A', 'B'))
                and set(fresh_comparison.get('review_reasons') or []) == {'evidence_difference'}
                and all(complete.values()) and not result.get('blocked')
                and not result.get('teacher_reconfirmation_required')
                and not result.get('unmapped_answers') and not result.get('photos_discarded_unconfirmed')):
            try:
                version(row, result, api)
                evidence_only = True
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        names = {s: {'deepseek':'DeepSeek','mimo':'MiMo','qwen':'Qwen'}.get((dual.get('models') or {}).get(s,{}).get('provider'), (dual.get('models') or {}).get(s,{}).get('label') or '模型'+s) for s in ('A','B')}
        papers.append({'id':row['id'], 'exam_id':row['exam_id'] or '', 'sequence':row['batch_sequence'], 'label':row['marking_code'] or '答卷 '+row['id'][:6], 'state':state, 'progress_state':progress, 'reason':reason, 'model_issues':issues, 'totals_complete':complete, 'a':displayed_number(results['A'].get('suggested_total')), 'b':displayed_number(results['B'].get('suggested_total')), 'names':names, 'max_score':displayed_number(result.get('max_total')), 'questions':subs, 'evidence_only':evidence_only, 'confirmation_method':result.get('confirmation_method'), 'final_total':displayed_number(row['final_total'])})
    return {'papers':papers, 'preview_token':TOKENS.dumps({'assignment_id':assignment_id, 'exam_id':exam_id, 'batch_id':batch_id, 'actor':actor, 'papers':proofs})}


def adopt(conn, body, actor, api):
    if body.get('confirmed') is not True:
        raise ValueError('请明确确认采纳所选 AI 共识成绩及可能存在共同误判的风险')
    try:
        token = TOKENS.loads(body.get('preview_token',''), max_age=600)
    except (BadSignature, TypeError):
        raise ValueError('列表已失效，请刷新并重新确认')
    if any(token.get(k,'') != body.get(k,'') for k in ('assignment_id','exam_id','batch_id')) or token['actor'] != actor:
        raise ValueError('预览不属于当前考试或操作人')
    selected = body.get('items')
    if not isinstance(selected, list) or not selected or len(selected) > 500:
        raise ValueError('请选择可采纳答卷')
    ids = [item.get('id') for item in selected if isinstance(item, dict)]
    if len(ids) != len(selected) or any(not isinstance(v,str) for v in ids) or len(set(ids)) != len(ids):
        raise ValueError('选择格式无效或答卷重复')
    conn.execute('BEGIN IMMEDIATE')
    current = {r['id']:r for r in rows(conn, token['assignment_id'], token['exam_id'],token.get('batch_id',''))}
    pending, reused = [], 0
    for item in selected:
        row = current.get(item['id']); preview = token['papers'].get(item['id'])
        if row is None or not preview or preview['exam_id'] != item.get('exam_id') or str(row['exam_id'] or '') != preview['exam_id']:
            raise ValueError('答卷考试归属或选择已变化，请刷新')
        result = json.loads(row['result_json'])
        if row['confirmed_at']:
            saved = result.get('batch_adoption') or {}
            if result.get('confirmation_method') != METHOD or saved.get('source_fingerprint') != preview['proof'] or saved.get('confirmed_at') != row['confirmed_at'] or saved.get('final_total') != row['final_total']:
                raise ValueError('已有其他确认记录，不能覆盖')
            version(row, result, api)
            reused += 1
            continue
        if proof(row) != preview['proof']:
            raise ValueError('评分、图片或考试内容已变化，请重新核对')
        score = evaluate(row, api, conn)
        pending.append((row, result, score, preview['proof']))
    for row, result, score, fingerprint in pending:
        timestamp = api.utc_now()
        result['confirmation_method'] = METHOD
        result['batch_adoption'] = {**score, 'source_fingerprint':fingerprint, 'confirmed_at':timestamp, 'actor':actor, 'policy_version':'exact_point_consensus_v1'}
        conn.execute('UPDATE submissions SET result_json=?,final_total=?,confirmed_at=?,teacher_note=? WHERE id=?', (json.dumps(result, ensure_ascii=False), score['final_total'], timestamp, '教师批量采纳 AI 共识，未逐点评阅', row['id']))
        api.append_audit_event(conn, 'submission_batch_adopted', 'submission', row['id'], {'actor':actor, 'exam_id':row['exam_id'], 'final_total':score['final_total'], 'source_fingerprint':fingerprint, 'confirmation_method':METHOD})
    return {'adopted':len(pending), 'reused':reused}


def confirmed_final(result):
    """Read adopted score metadata without claiming teacher point decisions."""
    if result.get('confirmation_method') == METHOD:
        value = result.get('batch_adoption')
        return value if isinstance(value, dict) and value.get('policy_version') == 'exact_point_consensus_v1' else {}
    return None


def current_adoption(result, assignment):
    """Do not interpret adopted history using a changed current exam."""
    if result.get('confirmation_method') != METHOD:
        return True
    try:
        evidence = result['subjective_version_evidence']
        return (bool(confirmed_final(result)) and evidence['assignment_question'] == assignment['question']
                and evidence['assignment_standard_answer'] == assignment['standard_answer']
                and evidence['source_sub_questions'] == json.loads(assignment['sub_questions_json']))
    except (KeyError, TypeError, ValueError, IndexError):
        return False
