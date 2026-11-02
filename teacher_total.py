"""Explicit teacher total confirmation; never invents point/subquestion decisions."""
import hashlib
import json
import math
from grading_core import subjective_version_state


def digest(row):
    return hashlib.sha256(json.dumps([row[k] for k in ('id','exam_id','assignment_id','result_json','photos_json','question','standard_answer','sub_questions_json','confirmed_at')], ensure_ascii=False).encode()).hexdigest()


def read(conn, sid):
    row = conn.execute('SELECT s.*,a.question,a.standard_answer,a.sub_questions_json,a.import_metadata_json FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE s.id=?', (sid,)).fetchone()
    if row is None:
        raise ValueError('答卷不存在')
    return row


def prepare(conn, row, api):
    result = json.loads(row['result_json'] or '{}')
    if row['state'] not in ('completed', 'failed', 'recovery_pending') or result.get('simulation_model') or result.get('mode') == 'rules-demo':
        raise ValueError('任务尚未结束或是模拟结果，不能保存正式成绩')
    if conn.execute("SELECT 1 FROM task_queue WHERE submission_id=? AND state IN ('queued','running')", (row['id'],)).fetchone():
        raise ValueError('答卷任务仍在执行，请先停止并等待当前调用结束')
    if row['exam_id'] and not conn.execute('SELECT 1 FROM grading_exams WHERE id=? AND assignment_id=?', (row['exam_id'], row['assignment_id'])).fetchone():
        raise ValueError('考试归属不明')
    photos = json.loads(row['photos_json'] or '[]')
    if result.get('photos_discarded_unconfirmed') or not photos:
        raise ValueError('必要原图缺失，不能核实答卷')
    api.submission_photo_directory(row['assignment_id'], row['id'], photos)
    if any(not api.resolve_data_relative_path(p.get('original_path') or p.get('path', '')).is_file() for p in photos):
        raise ValueError('必要原图缺失，请恢复图片后确认')
    image_hashes = []
    for photo in photos:
        path = api.resolve_data_relative_path(photo.get('original_path') or photo.get('path',''))
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = photo.get('original_sha256') if photo.get('original_path') else photo.get('sha256')
        if expected and expected != actual:
            raise ValueError('原图内容与保存时的指纹不一致，请先恢复原图')
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError) as exc:
            raise ValueError('原图无法读取，不能核实本次成绩') from exc
        image_hashes.append(actual)
    source = json.loads(row['sub_questions_json'] or '[]')
    scope = result.get('grading_scope') or {}
    ids = scope.get('graded_question_ids')
    if not isinstance(ids, list) or not ids:
        raise ValueError('本次评分范围无法核实')
    questions, calculated = api.resolve_grading_scope(api.clean_sub_questions(source), graded_question_ids=ids)
    maximum = float(calculated['scoped_max_score'])
    if isinstance(scope.get('scoped_max_score'), bool) or not math.isfinite(maximum) or maximum <= 0 or maximum != scope.get('scoped_max_score'):
        raise ValueError('本次满分或范围与权威题目不一致')
    error = subjective_version_state(result.get('subjective_version_evidence'), assignment_question=row['question'], assignment_standard_answer=row['standard_answer'], source_questions=source, scoring_questions=questions, graded_question_ids=ids, scoped_max_score=maximum, grading_context=result.get('grading_context'), dual_review=result.get('dual_review'))
    if error:
        raise ValueError(error+'：评分版本证据缺失或与当前考试不一致')
    suggestions = {}
    results = (result.get('dual_review') or {}).get('results') or {'A': result}
    from exam_report import _question_key
    question_maxima = {_question_key(q['id']): q['max_score'] for q in questions}
    for name in ('A', 'B'):
        model = results.get(name) or {}
        values = model.get('sub_questions') or []
        scored_ids = [_question_key(q.get('sub_question_id') or q.get('question_id')) for q in values]
        wanted = [_question_key(v) for v in ids]
        valid = len(scored_ids)==len(set(scored_ids)) and set(scored_ids)==set(wanted) and not model.get('blocked') and model.get('mode') not in ('vision-model-error','processing-error')
        if result.get('dual_review') and ((result['dual_review'].get('models') or {}).get(name) or {}).get('status') != 'ok':
            valid = False
        scores = []
        for q in values:
            value = q.get('suggested_total')
            qmax = question_maxima.get(_question_key(q.get('sub_question_id') or q.get('question_id')), -1)
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= qmax or q.get('answer_status') in ('unanswered_candidate','missing','question_unmatched','image_unreadable','uncertain_candidate'):
                valid = False
            else:
                scores.append(value)
        total = sum(scores)
        suggestions[name] = total if valid and total <= maximum else None
    fingerprint = hashlib.sha256((digest(row)+json.dumps(image_hashes)).encode()).hexdigest()
    return dict(submission_id=row['id'], exam_id=row['exam_id'] or '', max_score=maximum, grading_scope=scope, suggestions=suggestions, expected_fingerprint=fingerprint, original_image_sha256s=image_hashes)


def confirm(conn, row, payload, api, actor):
    prepared = prepare(conn, row, api)
    if payload.get('exam_id') != (row['exam_id'] or '') or payload.get('expected_fingerprint') != prepared['expected_fingerprint']:
        raise ValueError('考试、答卷或评分内容已经变化，请重新读取')
    if payload.get('confirmed') is not True or not api.submission_photo_was_viewed(row['id']):
        raise ValueError('请查看原卷并明确确认本次最终总分')
    choice = payload.get('source')
    if choice not in ('A','B','teacher'):
        raise ValueError('请选择采用模型或教师自定')
    score = payload.get('final_total')
    if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= prepared['max_score']:
        raise ValueError('最终总分必须是0至本次满分之间的有限数值')
    if choice != 'teacher' and prepared['suggestions'][choice] != score:
        raise ValueError('该模型未提供完整有效总分，请使用教师自定总分')
    result = json.loads(row['result_json'] or '{}');when=api.utc_now()
    record = dict(method='teacher_total_confirmation', final_total=score, max_score=prepared['max_score'], grading_scope=prepared['grading_scope'], source=choice,
                  original_result_sha256=hashlib.sha256(row['result_json'].encode()).hexdigest(),
                  original_dual_sha256=hashlib.sha256(json.dumps(result.get('dual_review'), sort_keys=True).encode()).hexdigest(),
                  original_image_sha256s=prepared['original_image_sha256s'],
                  content_hash=result['subjective_version_evidence']['content_hash'], confirmed_at=when, actor=actor,
                  teacher_review_completeness='total_confirmation', question_subtotals=[])
    previous = result.get('teacher_total_confirmation')
    result['teacher_total_confirmation']=record;result['confirmation_method']='teacher_total_confirmation';result['teacher_reconfirmation_required']=False
    # Original model result, Risk, modes, blank flags and previous point final remain untouched.
    conn.execute('UPDATE submissions SET state=\'completed\',final_total=?,confirmed_at=?,result_json=? WHERE id=?', (score,when,json.dumps(result,ensure_ascii=False),row['id']))
    api.append_audit_event(conn,'teacher_total_confirmed','submission',row['id'],{'actor':actor,'previous_confirmation':previous,'previous_final_total':row['final_total'],'previous_confirmed_at':row['confirmed_at'],'confirmation':record})
    return record


def current_confirmation(row, assignment, api):
    """Compatibility exports may accept this explicit source, never a bare total."""
    from exam_report import _question_key
    from unified_scores import subjective
    try:
        result = json.loads(row['result_json'] or '{}')
        if result.get('confirmation_method') not in ('teacher_total_confirmation', 'automatic_blank_zero'):
            return False
        source = json.loads(assignment['sub_questions_json'] or '[]')
        questions = {_question_key(q['id']): float(q['max_score']) for q in source}
        side = subjective(row,questions,assignment,source)
        if not side['valid'] or side.get('version_status') != 'verified':
            return False
        return True
    except (ValueError,TypeError,KeyError):
        return False
