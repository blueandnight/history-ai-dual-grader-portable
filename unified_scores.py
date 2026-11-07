"""Read-only score projection. Never grades, confirms, or persists a total."""
import json
import math
import hashlib
from collections import Counter
from datetime import datetime
from flask import jsonify, request
from unified_exam import objective_content
from exam_report import _load_result, _question_key, _teacher_final
from grading_core import rubric_fingerprint


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def equal(a, b):
    return a is not None and b is not None and abs(a-b) < 1e-8


def timestamp(value):
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def objective(conn, row, current):
    side = dict(id=row['submission_id'], exam_id=row.get('source_exam_id',''), assessment_id=row['assessment_id'],
                job_id=row['job_id'], state=row['state'], page_count=row['page_count'],
                anonymous_code=row['anonymous_code'], created_at=row['created_at'],
                source_order=row['source_order'] if 'source_order' in row.keys() else 0,
                score=None, max_score=number(row['max_score']), valid=False,
                complete=False, reason='客观题待复核',
                source_fingerprint=row['source_fingerprint'], answer_version=row['answer_version'])
    key = json.loads(row['answer_key_json'])
    if not isinstance(key, list) or not key:
        raise ValueError('客观题快照无效')
    if hashlib.sha256(row['answer_key_json'].encode()).hexdigest() != row['answer_key_sha256']:
        raise ValueError('客观题快照校验失败')
    keys = {k['question_no']: k for k in key}
    if (sorted(keys) != list(range(1, len(key)+1))
            or any(k.get('original_question_id') != str(k['question_no'])
                   or k.get('correct_option') not in ('A', 'B', 'C', 'D') for k in key)):
        raise ValueError('客观题快照题号映射无效')
    maxima = [number(k['points']) for k in key]
    maximum = sum(maxima) if all(v is not None and v > 0 for v in maxima) else None
    answers = conn.execute('SELECT * FROM objective_answers WHERE submission_id=?', (row['submission_id'],)).fetchall()
    pending = conn.execute("SELECT 1 FROM objective_review_items WHERE submission_id=? AND state='open' LIMIT 1", (row['submission_id'],)).fetchone()
    job = conn.execute('SELECT assessment_id,mode FROM objective_jobs WHERE job_id=?', (row['job_id'],)).fetchone()
    side['mode'] = job['mode'] if job else ''
    score = number(row['score'])
    compatible = bool(job and job['assessment_id'] == row['assessment_id']) and len(keys) == len(key) and {a['question_no'] for a in answers} == set(keys)
    points = []
    for a in answers:
        k = keys.get(a['question_no'], {})
        value = number(a['awarded_points'])
        if (a['state'] not in ('auto', 'manual_blank', 'manual_answer') or value is None
                or not equal(number(a['max_points']), number(k.get('points')))
                or not 0 <= value <= a['max_points']):
            compatible = False
        else:
            points.append(value)
    if (row['state'] != 'completed' or pending or not compatible or score is None
            or maximum is None or not 0 <= score <= maximum
            or not equal(side['max_score'], maximum) or not equal(sum(points), score)):
        return side
    side.update(score=score, valid=True, reason='客观题已完成')
    if not current or row['source_fingerprint'] != current['source_fingerprint'] or key != current['answer_key']:
        side['reason'] = '客观题历史快照与当前考试版本不同，保留历史成绩'
    else:
        side.update(complete=True)
    return side


def subjective_version(result, exam, source_questions, ids):
    """Validate recorded inputs, not a hash fabricated from today's assignment."""
    evidence = result.get('subjective_version_evidence')
    if not isinstance(evidence, dict) or evidence.get('version') != 1:
        return 'subjective_version_unverifiable'
    try:
        content = {k: v for k, v in evidence.items() if k != 'content_hash'}
        if evidence.get('content_hash') != rubric_fingerprint([content]):
            return 'subjective_version_unverifiable'
        historical = evidence['scoring_sub_questions']
        source = evidence['source_sub_questions']
        if not isinstance(source, list) or not isinstance(historical, list) or not historical:
            return 'subjective_version_unverifiable'
        snapshot_ids = [_question_key(q['id']) for q in historical]
        if len(set(snapshot_ids)) != len(snapshot_ids) or set(snapshot_ids) != {_question_key(v) for v in ids}:
            return 'subjective_version_unverifiable'
        digest = rubric_fingerprint(historical)
        if digest != evidence.get('rubric_hash') or digest != (result.get('grading_context') or {}).get('rubric_sha256'):
            return 'subjective_version_unverifiable'
        dual = result.get('dual_review')
        if isinstance(dual, dict) and digest != dual.get('rubric_hash'):
            return 'subjective_version_unverifiable'
        if (evidence['assignment_question'] != exam['question']
                or evidence['assignment_standard_answer'] != exam['standard_answer']
                or rubric_fingerprint(source) != rubric_fingerprint(source_questions)):
            return 'subjective_content_version_mismatch'
        return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'subjective_version_unverifiable'


def valid_ai_subscores(result, exam, source_questions, ids):
    """Shared read-only eligibility for AI references and diagnostic samples."""
    scores = {'A': {}, 'B': {}}
    if not isinstance(ids, list) or not ids or result.get('simulation_model'):
        return scores
    try:
        wanted = [_question_key(v) for v in ids]
        maxima = {_question_key(q['id']): number(q['max_score']) for q in source_questions}
        if (len(set(wanted)) != len(wanted) or any(k not in maxima or maxima[k] is None or maxima[k] <= 0 for k in wanted)
                or not equal(number((result.get('grading_scope') or {}).get('scoped_max_score')), sum(maxima[k] for k in wanted))
                or subjective_version(result, exam, source_questions, ids) is not None):
            return scores
    except (ValueError, TypeError, KeyError, AttributeError):
        return scores
    dual = result.get('dual_review') or {}
    if not isinstance(dual, dict):
        return scores
    models = dual.get('results') if dual else {'A': result}
    if not isinstance(models, dict):
        return scores
    for name in scores:
        model = models.get(name)
        if (not isinstance(model, dict) or model.get('mode') != 'vision-model' or model.get('simulation_model')
                or model.get('requires_question_mapping') or model.get('unmapped_answers')):
            continue
        if dual:
            metadata = (dual.get('models') or {}).get(name) if isinstance(dual.get('models'), dict) else None
            if not isinstance(metadata, dict) or metadata.get('status') != 'ok':
                continue
            finish = metadata.get('finish_reason')
            if any(v in ('length', 'content_filter', 'error') for v in (finish if isinstance(finish, list) else [finish])):
                continue
        questions = model.get('sub_questions')
        if not isinstance(questions, list) or any(not isinstance(q, dict) for q in questions):
            continue
        if model.get('blocked') and not any(q.get('blocked') or q.get('block_reasons') for q in questions):
            continue
        counts = Counter(_question_key(q.get('sub_question_id') or q.get('question_id')) for q in questions)
        for q in questions:
            qid = _question_key(q.get('sub_question_id') or q.get('question_id'))
            value = number(q.get('suggested_total'))
            if (qid not in wanted or counts[qid] != 1 or value is None or not 0 <= value <= maxima[qid]
                    or not equal(number(q.get('max_total')), maxima[qid]) or q.get('blocked') or q.get('block_reasons')
                    or any(q.get(k) is False for k in ('image_readable', 'region_complete', 'question_matched'))
                    or q.get('answer_status') not in ('answered', 'blank_confirmed')):
                continue
            if q.get('scoring_mode')=='holistic' and q['answer_status']=='answered':
                from holistic_scoring import valid_sub
                source=next((v for v in source_questions if _question_key(v['id'])==qid),{})
                if not valid_sub(q,source):continue
            if q['answer_status'] == 'blank_confirmed':
                from blank_consensus import valid
                receipt = dual.get('blank_resolution')
                if (value != 0 or not isinstance(receipt, dict) or not isinstance(receipt.get('question_ids'), list)
                        or not valid(result) or qid not in {_question_key(v) for v in receipt['question_ids']}):
                    continue
            scores[name][qid] = value
    return scores


def subjective(row, questions, exam, source_questions):
    result = _load_result(row['result_json'])
    teacher = _teacher_final(result)
    scope = result.get('grading_scope') or {}
    side = dict(id=row['id'], exam_id=row['exam_id'], score=None, max_score=number(scope.get('scoped_max_score')),
                valid=False, complete=False, question_ids=scope.get('graded_question_ids', []), reason='主观题待确认')
    if result.get('confirmation_method') == 'automatic_blank_zero':
        from blank_consensus import valid, digest
        record = result.get('automatic_blank_zero') or {}
        ids = scope.get('graded_question_ids') or []
        if (row['state'] != 'completed' or not timestamp(row['confirmed_at']) or row['final_total'] != 0
                or result.get('simulation_model') or result.get('teacher_reconfirmation_required')
                or result.get('blocked') or not valid(result)
                or record.get('method') != 'automatic_blank_zero' or record.get('final_total') != 0
                or record.get('confirmed_at') != row['confirmed_at'] or record.get('grading_scope') != scope
                or record.get('receipt_sha256') != digest(result['dual_review']['blank_resolution'])
                or set(result['dual_review']['blank_resolution']['question_ids']) != set(ids)
                or not equal(number(record.get('max_score')), side['max_score'])):
            side['reason'] = '自动空白计零记录无法核实'
            return side
        version = subjective_version(result, exam, source_questions, ids)
        side.update(score=0, valid=True, confirmation_method='automatic_blank_zero')
        if version or record.get('content_hash') != (result.get('subjective_version_evidence') or {}).get('content_hash'):
            side['reason'] = version or 'subjective_version_unverifiable'
        elif len(ids) != len(questions) or set(map(_question_key, ids)) != set(questions) or not equal(side['max_score'], sum(questions.values())):
            side['reason'] = '主观题仅部分范围评分'
        else:
            side.update(complete=True, version_status='verified', reason='双模型明确整卷空白，自动确认0分')
        return side
    total = result.get('teacher_total_confirmation')
    if result.get('confirmation_method') == 'teacher_total_confirmation' and isinstance(total, dict):
        score, maximum = number(row['final_total']), side['max_score']
        if (row['state'] != 'completed' or not timestamp(row['confirmed_at']) or result.get('simulation_model')
                or result.get('teacher_reconfirmation_required') or total.get('method') != 'teacher_total_confirmation'
                or total.get('source') not in ('A','B','teacher')
                or total.get('original_dual_sha256') != hashlib.sha256(json.dumps(result.get('dual_review'), sort_keys=True).encode()).hexdigest()
                or total.get('confirmed_at') != row['confirmed_at'] or total.get('grading_scope') != scope
                or not equal(number(total.get('max_score')), maximum) or not equal(number(total.get('final_total')), score)
                or score is None or maximum is None or not 0 <= score <= maximum):
            side['reason'] = '教师总分确认记录无效或需重新确认'
            return side
        side.update(score=score, valid=True, confirmation_method='teacher_total_confirmation')
        ids = scope.get('graded_question_ids') or []
        version = subjective_version(result, exam, source_questions, ids)
        if version or total.get('content_hash') != (result.get('subjective_version_evidence') or {}).get('content_hash'):
            side['reason'] = version or 'subjective_version_unverifiable'
        elif {_question_key(v) for v in ids} != set(questions) or len(ids) != len(questions) or not equal(maximum, sum(questions.values())):
            side.update(reason='主观题仅部分范围经过总分确认', version_status='verified')
        else:
            side.update(complete=True, reason='教师已确认最终总分（未逐点确认）', version_status='verified')
        return side
    override = row['manual_reviewed'] and result.get('mode') == 'vision-model'
    if (row['state'] != 'completed' or not timestamp(row['confirmed_at']) or not teacher
            or result.get('teacher_reconfirmation_required') or result.get('simulation_model')
            or teacher.get('teacher_review_completeness') == 'total_only'
            or (result.get('blocked') and not override)
            or result.get('mode') in ('vision-model-error', 'image-quality-error', 'rules-demo', 'processing-error', 'queued')
            or result.get('unmapped_answers')
            or any(q.get('answer_status') in ('uncertain', 'missing', 'uncertain_candidate', 'missing_candidate', 'question_unmatched', 'image_unreadable', 'unanswered_candidate') for q in result.get('sub_questions', []))):
        return side
    ids = scope.get('graded_question_ids')
    if not isinstance(ids, list) or not ids:
        side['reason'] = '主观题评分范围无法确认'
        return side
    ids = [_question_key(v) for v in ids]
    teacher_scope = teacher.get('grading_scope')
    if teacher_scope is not None and teacher_scope != scope:
        side['reason'] = '主观题终评范围已变化，需重新确认'
        return side
    subs = teacher.get('question_subtotals') or []
    subtotals = {_question_key(q.get('question_id')): number(q.get('subtotal')) for q in subs}
    maximum, score = side['max_score'], number(row['final_total'])
    if (len(set(ids)) != len(ids) or len(subtotals) != len(subs) or set(subtotals) != set(ids)
            or any(v is None or v < 0 for v in subtotals.values())
            or maximum is None or maximum <= 0 or score is None or not 0 <= score <= maximum
            or not equal(sum(v for v in subtotals.values() if v is not None), score)
            or not equal(number(teacher.get('final_total')), score)):
        side['reason'] = '主观题终评记录或评分范围不一致'
        return side
    side.update(score=score, valid=True, confirmation_method=result.get('confirmation_method') or 'teacher_point_review', reason='主观题仅部分评分或分值口径不同')
    version_error = subjective_version(result, exam, source_questions, ids)
    if version_error:
        side.update(reason=version_error + ('：主观题内容版本不同，保留历史终评' if version_error == 'subjective_content_version_mismatch' else '：缺少或无法核实当次评分版本证据，保留历史终评'), version_status=version_error)
        return side
    side['version_status'] = 'verified'
    recorded = {_question_key(q.get('sub_question_id') or q.get('question_id') or q.get('label')): number(q.get('max_total')) for q in result.get('sub_questions', [])}
    if (set(ids) == set(questions) and equal(maximum, sum(questions.values()))
            and all(equal(recorded.get(k), v) for k, v in questions.items())
            and all(subtotals[k] <= questions[k] for k in questions)):
        side.update(complete=True, reason='主观题已批量采纳 AI 共识' if result.get('confirmation_method') == 'teacher_batch_adoption' else '主观题已终评')
    return side


def build(conn, exam_id, provider, validate, *, all_records=False):
    exam = conn.execute('SELECT a.* FROM grading_exams e JOIN assignments a ON a.id=e.assignment_id WHERE e.id=?', (exam_id,)).fetchone()
    if exam is None:
        raise LookupError('考试不存在或已删除')
    meta = _load_result(exam['import_metadata_json'])
    try:
        saved = json.loads(exam['sub_questions_json'] or '[]')
        if not isinstance(saved, list) or any(not isinstance(q, dict) for q in saved):
            raise ValueError('Invalid subjective configuration')
    except (ValueError, TypeError):
        saved = []
        meta = {}
    questions = {_question_key(q.get('sub_question_id') or q.get('id') or q.get('label')): number(q.get('max_score')) for q in saved}
    confirmed = meta.get('teacher_confirmation_status') == 'confirmed_by_teacher'
    config_valid = confirmed and len(questions) == len(saved) and all(questions) and all(v is not None and v > 0 for v in questions.values())
    has_objective, has_subjective = bool(meta.get('objective_questions')), bool(saved)
    try:
        current = objective_content(meta) if has_objective else None
    except (ValueError, LookupError):
        current = None
    ids=[r[0] for r in conn.execute('SELECT id FROM grading_exams WHERE assignment_id=?',(exam['id'],))] if all_records else [exam_id]
    if all_records:
        visible={e['exam_id'] for e in provider()}
        ids=[eid for eid in ids if eid in visible]
    slots=','.join('?' for _ in ids)
    objects={r['submission_id']:dict(r) for r in conn.execute('SELECT s.*,s.rowid AS source_order,a.source_exam_id,a.source_fingerprint,a.answer_version,a.answer_key_json,a.answer_key_sha256 FROM objective_submissions s JOIN objective_assessments a ON a.assessment_id=s.assessment_id WHERE a.source_exam_id IN ('+slots+')',ids)}
    subjects={r['id']:dict(r) for r in conn.execute('SELECT * FROM submissions WHERE exam_id IN ('+slots+')',ids)}
    links=[dict(r) for r in conn.execute('SELECT * FROM exam_answer_links WHERE exam_id IN ('+slots+') AND active=1 ORDER BY confirmed_at,id',ids)]
    # Include cross-exam conflicts in cardinality checks without exposing their records.
    active = conn.execute('SELECT objective_submission_id,subjective_submission_id FROM exam_answer_links WHERE active=1').fetchall()
    oc, sc = Counter(r[0] for r in active), Counter(r[1] for r in active)
    rows, used_o, used_s = [], set(), set()
    def append(o, s, link=None):
        reasons=[]
        if s and s['assignment_id']!=exam['id']:reasons.append('主观题考试来源不一致')
        if not link and ((o and oc[o['submission_id']]) or (s and sc[s['id']])):reasons.append('存在不属于本考试范围的有效关联，请核对关联')
        valid_link = False
        if link:
            try:
                validate(conn, exam_id, link['objective_submission_id'], link['subjective_submission_id'])
                if oc[link['objective_submission_id']] != 1 or sc[link['subjective_submission_id']] != 1 or not timestamp(link['confirmed_at']) or link['ended_at']:
                    raise ValueError('关联关系冲突')
                valid_link = True
            except (ValueError, LookupError):
                reasons.append('关联异常，请核对或解除关联')
        attribution_valid = not reasons and (valid_link or not (has_objective and has_subjective))
        sides = []
        for raw, calculate in ((o, lambda r: objective(conn, r, current)), (s, lambda r: subjective(r, questions, exam, saved))):
            try:
                sides.append(calculate(raw) if raw else None)
            except (ValueError, TypeError, KeyError, AttributeError):
                sides.append(dict(id=raw.get('submission_id', raw.get('id')), score=None, max_score=None, valid=False, complete=False, reason='评分记录格式无效'))
        obj, sub = sides
        if sub and s:
            result = _load_result(s['result_json'])
            scope_ids = sub.get('question_ids', [])
            eligible = valid_ai_subscores(result, exam, saved, scope_ids)
            wanted = {_question_key(v) for v in scope_ids} if isinstance(scope_ids, list) else set()
            refs = {name: sum(values.values()) if values else None for name, values in eligible.items()}
            ref_states = {name: '完整AI参考' if wanted and set(values) == wanted else '部分AI参考' if values else '无有效AI参考'
                          for name, values in eligible.items()}
            sub['ai_reference'], sub['ai_reference_state'] = refs, ref_states
        if not config_valid or (not has_objective and not has_subjective):
            reasons.append('当前考试题型或分值口径尚未完整确认')
        if has_objective and has_subjective and not link:
            reasons.append('未关联')
        for needed, side, missing in ((has_objective, obj, '客观题缺失'), (has_subjective, sub, '主观题缺失')):
            if needed and not side:
                reasons.append(missing)
            elif side and not side['complete']:
                reasons.append(side['reason'])
        if (obj and not has_objective) or (sub and not has_subjective):
            reasons.append('历史答卷题型与当前考试不一致')
        complete = not reasons and (valid_link or not (has_objective and has_subjective))
        roster=None
        # Historical explicit roster bindings are retained; retired codes neither
        # create a new binding nor block anonymous scoring/aggregation.
        if s:
            bound=conn.execute("SELECT r.* FROM roster_students r JOIN exam_marking_codes c ON c.roster_student_id=r.id WHERE c.exam_id=? AND c.submission_id=? AND c.binding_status='matched' AND c.identity_issue=''",(s['exam_id'],s['id'])).fetchall()
            if len(bound)==1 and all(not s[k] or not bound[0][k] or s[k]==bound[0][k] for k in ('student_name','student_no')):roster=dict(bound[0])
        rows.append(dict(link_id=link['id'] if link else None, attribution_valid=attribution_valid, association_status='已关联' if valid_link else '未关联',
                         student_name=roster['student_name'] if roster else (s['student_name'] or '') if s else '', class_name=roster['class_name'] if roster else (s['class_name'] or '') if s else '', student_no=roster['student_no'] if roster else (s['student_no'] or '') if s else '',
                         confirmation_method=sub.get('confirmation_method','') if sub else '',
                         anonymous_code=s['id'] if s else o['anonymous_code'] if o else '关联记录异常',
                         objective=obj, subjective=sub, complete=complete,
                         total_score=round(sum(x['score'] for x in sides if x), 8) if complete else None,
                         total_max=round(sum(x['max_score'] for x in sides if x), 8) if complete else None,
                         status='已完整确认' if complete else '；'.join(dict.fromkeys(reasons))))
    for link in links:
        o, s = objects.get(link['objective_submission_id']), subjects.get(link['subjective_submission_id'])
        append(o, s, link)
        used_o.add(link['objective_submission_id']); used_s.add(link['subjective_submission_id'])
    for oid, o in objects.items():
        if oid not in used_o:
            append(o, None)
    for sid, s in subjects.items():
        if sid not in used_s:
            append(None, s)
    return dict(exam_id=exam_id, assignment_id=exam['id'], record_exam_ids=ids, scope='assignment' if all_records else 'exam', rows=rows, has_objective=has_objective, has_subjective=has_subjective,
                objective_full_max=current['max_score'] if current else None,
                subjective_full_max=sum(questions.values()) if config_valid else None)


def register(app, provider, connect, allowed, validate, error):
    @app.get('/api/unified-scores/report')
    @app.get('/api/unified-scores/export.<kind>')
    def unified_output(kind=None):
        from flask import Response
        import score_export
        from exam_report import unified_exam_summary
        try:
            eid=request.args.get('exam_id',''); allowed(eid)
            with connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
                projection=build(conn,eid,provider,validate,all_records=request.args.get('records')=='all')
                if kind is None:
                    return jsonify(unified_exam_summary(conn,projection,float(request.args.get('pass_ratio','.6')),float(request.args.get('excellent_ratio','.85'))))
                if kind not in ('csv','xlsx') or request.args.get('scope','all') not in ('complete','all') or request.args.get('side','all') not in ('all','objective','subjective'):
                    raise ValueError('请选择完整正式成绩或异常核对清单')
                title=conn.execute('SELECT a.name FROM grading_exams e JOIN assignments a ON a.id=e.assignment_id WHERE e.id=?',(eid,)).fetchone()[0]
                data=score_export.rows(projection,title,request.args.get('scope','all')=='all',side=request.args.get('side','all'))
                if kind=='csv':
                    body=score_export.csv_bytes(data)
                else:
                    side=request.args.get('side','all')
                    sheets=score_export.analysis_sheets(unified_exam_summary(conn,projection,float(request.args.get('pass_ratio','.6')),float(request.args.get('excellent_ratio','.85'))),'全部可用结果' if request.args.get('scope','all')=='all' else '完整正式成绩')
                    if side!='all':
                        stats=unified_exam_summary(conn,projection,float(request.args.get('pass_ratio','.6')),float(request.args.get('excellent_ratio','.85')))[side+'_summary']
                        summary=[['指标','数值'],['有效答卷数',stats['count']],['排除答卷数',stats['excluded_count']],['平均分',stats['mean']],['最高分',stats['maximum']],['最低分',stats['minimum']],['得分率',stats['score_rate']],['及格率',stats['pass_rate']],['优秀率',stats['excellent_rate']]]+[[k,v] for k,v in stats['distribution'].items()]
                        sheets=[('单侧分析',summary)]+[s for s in sheets if s[0]==('客观题统计' if side=='objective' else '主观题统计')]
                    body=score_export.xlsx_bytes(data,sheets,sheet_name={'all':'统一成绩','objective':'客观题成绩','subjective':'主观题成绩'}[side])
                return Response(body,mimetype='text/csv' if kind=='csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="unified-'+eid+'.'+kind+'"','Cache-Control':'no-store'})
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)

    @app.get('/api/unified-scores')
    def unified_scores():
        exam_id = request.args.get('exam_id', '')
        try:
            allowed(exam_id)
            with connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                return jsonify(build(conn, exam_id, provider, validate,all_records=request.args.get('records')=='all'))
        except (ValueError, LookupError, PermissionError) as exc:
            return error(exc)
