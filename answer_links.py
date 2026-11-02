"""Explicit teacher-confirmed links; never joins or updates grading records."""
import json
import sqlite3
import re
import uuid
from flask import g, has_request_context, jsonify, request


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS exam_answer_links (
        id TEXT PRIMARY KEY, exam_id TEXT NOT NULL,
        objective_submission_id TEXT NOT NULL, subjective_submission_id TEXT NOT NULL,
        method TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT,
        confirmed_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        ended_at TEXT, ended_by TEXT, end_reason TEXT)""")
    for column in ('objective_submission_id','subjective_submission_id'):
        conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS idx_answer_links_{column} ON exam_answer_links(exam_id,{column}) WHERE active=1')


def register(app, provider, connect, audit, now):
    source_provider = provider
    if callable(source_provider):
        def provider():
            # allowed() loads access metadata before any score read transaction.
            # Reuse it during this request: opening another SQLite reader while
            # a writer waits for that transaction can otherwise deadlock.
            if not has_request_context():
                return source_provider()
            if not hasattr(g, 'answer_link_source_exams'):
                g.answer_link_source_exams = source_provider()
            return g.answer_link_source_exams
    def allowed(exam_id):
        if not isinstance(exam_id,str) or not re.fullmatch(r'[0-9a-f]{32}',exam_id):raise ValueError('正式考试编号无效')
        if not callable(provider):
            raise ValueError('当前独立服务未接入正式考试，不能推断答卷归属')
        if not any(e["exam_id"]==exam_id for e in provider()):
            raise LookupError("正式考试不存在或不可访问")

    def actor():
        return request.environ.get('history_grader.actor')

    def error(exc):
        return jsonify({'error':str(exc)}),403 if isinstance(exc,PermissionError) else 404 if isinstance(exc,LookupError) else 409 if isinstance(exc,sqlite3.IntegrityError) else 400

    def validate(conn,exam_id,obj_id,sub_id):
        if not isinstance(obj_id,str) or not isinstance(sub_id,str):raise ValueError('答卷编号格式无效')
        obj=conn.execute('SELECT s.*,a.source_exam_id FROM objective_submissions s JOIN objective_assessments a ON a.assessment_id=s.assessment_id WHERE s.submission_id=?',(obj_id,)).fetchone()
        sub=conn.execute('SELECT * FROM submissions WHERE id=?',(sub_id,)).fetchone()
        if not obj or not sub:raise LookupError('答卷不存在或不可访问')
        source=conn.execute('SELECT assignment_id FROM grading_exams WHERE id=?',(obj['source_exam_id'],)).fetchone()
        chosen=conn.execute('SELECT assignment_id FROM grading_exams WHERE id=?',(exam_id,)).fetchone()
        actual=conn.execute('SELECT assignment_id FROM grading_exams WHERE id=?',(sub['exam_id'],)).fetchone()
        if not source or not chosen or not actual or source[0]!=chosen[0] or actual[0]!=chosen[0]:raise ValueError('两份答卷必须属于同一考试来源；独立历史客观题不能推断归属')
        visible={e['exam_id'] for e in provider()}
        if obj['source_exam_id'] not in visible or sub['exam_id'] not in visible:raise PermissionError('答卷不属于可访问的考试范围')
        exam=conn.execute('SELECT assignment_id FROM grading_exams WHERE id=?',(exam_id,)).fetchone()
        if not exam or sub['assignment_id']!=exam['assignment_id']:raise ValueError('主观题作业与正式考试归属冲突')
        return obj,sub

    from unified_scores import register as register_scores
    register_scores(app, provider, connect, allowed, validate, error)

    @app.get('/api/answer-links')
    def list_links():
        exam_id=request.args.get('exam_id','')
        try:
            allowed(exam_id)
            with connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                from unified_scores import build
                projection=build(conn,exam_id,provider,validate,all_records=True)
                ids=projection['record_exam_ids'];slots=','.join('?' for _ in ids)
                links=[dict(r) for r in conn.execute('SELECT * FROM exam_answer_links WHERE exam_id IN ('+slots+') ORDER BY confirmed_at DESC,id',ids)]
                objects={r['objective']['id']:r['objective'] for r in projection['rows'] if r.get('objective')}
                subjects={r['subjective']['id']:r['subjective'] for r in projection['rows'] if r.get('subjective')}
                objects=[dict(submission_id=p['id'],anonymous_code=p.get('anonymous_code',p['id']),state=p.get('state',''),page_count=p.get('page_count',0),score=p['score'] if p['valid'] else None,max_score=p['max_score']) for p in objects.values()]
                subjects=[dict(id=r['id'],state=r['state'],page_count=r['page_count'],score=subjects[r['id']]['score'] if subjects[r['id']]['valid'] else None,max_score=subjects[r['id']]['max_score']) for r in conn.execute('SELECT id,state,json_array_length(photos_json) page_count FROM submissions WHERE exam_id IN ('+slots+') ORDER BY created_at,id',ids) if r['id'] in subjects]
            return jsonify({'exam_id':exam_id,'links':links,'objective_submissions':objects,'subjective_submissions':subjects})
        except (ValueError,LookupError,PermissionError) as exc:return error(exc)

    @app.post('/api/answer-links')
    def write_link():
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify({'error':'请求格式无效'}),400
        exam_id=body.get('exam_id','');action=body.get('action','link')
        try:
            allowed(exam_id)
            reason=str(body.get('reason') or ('教师解除关联' if action=='unlink' else '教师选择两份原卷，明确确认属于同一考生')).strip()
            if not reason or len(reason)>1000:raise ValueError('请填写可核实的关联依据或解除、更正原因（最多1000字）')
            with connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                conn.execute('BEGIN IMMEDIATE')
                old=None
                if action in ('unlink','replace'):
                    old=conn.execute('SELECT l.* FROM exam_answer_links l JOIN grading_exams e ON e.id=l.exam_id JOIN grading_exams chosen ON chosen.assignment_id=e.assignment_id WHERE l.id=? AND chosen.id=?',(body.get('link_id'),exam_id)).fetchone()
                    if not old:raise LookupError('关联不存在或不属于本考试')
                    allowed(old['exam_id'])
                    if not old['active']:
                        if action=='unlink':return jsonify({'link':dict(old),'reused':True})
                        raise ValueError('原关联已解除，请刷新后核实')
                elif action!='link':raise ValueError('未知关联操作')
                if action!='unlink':
                    if body.get('confirmed') is not True or body.get('originals_checked') is not True:raise ValueError('必须核对两边原始答卷并明确确认同一考生；信息不足时保持未关联')
                    obj_id=body.get('objective_submission_id');sub_id=body.get('subjective_submission_id')
                    validate(conn,exam_id,obj_id,sub_id)
                    conflicts=conn.execute('SELECT * FROM exam_answer_links WHERE active=1 AND (objective_submission_id=? OR subjective_submission_id=?)',(obj_id,sub_id)).fetchall()
                    if any(r['id']!=(old['id'] if old else '') and (r['objective_submission_id']!=obj_id or r['subjective_submission_id']!=sub_id) for r in conflicts):raise sqlite3.IntegrityError('答卷已关联其他答卷，请先解除或更正')
                    existing=conn.execute('SELECT * FROM exam_answer_links WHERE objective_submission_id=? AND subjective_submission_id=? AND active=1',(obj_id,sub_id)).fetchone()
                    if existing:
                        if old and old['id']!=existing['id']:raise ValueError('目标答卷已有其他有效关联')
                        return jsonify({'link':dict(existing),'reused':True})
                timestamp=now()
                if old:
                    conn.execute('UPDATE exam_answer_links SET active=0,ended_at=?,ended_by=?,end_reason=? WHERE id=?',(timestamp,actor(),reason,old['id']))
                if action=='unlink':
                    audit(conn,'answer_link_removed',details={'link_id':old['id'],'exam_id':exam_id,'actor':actor(),'reason':reason,'objective_submission_id':old['objective_submission_id'],'subjective_submission_id':old['subjective_submission_id']})
                    return jsonify({'link_id':old['id'],'active':False})
                link_id=uuid.uuid4().hex
                conn.execute('INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,actor,confirmed_at) VALUES(?,?,?,?,?,?,?,?)',(link_id,exam_id,obj_id,sub_id,'teacher_original_review',reason,actor(),timestamp))
                audit(conn,'answer_link_corrected' if old else 'answer_link_created',details={'link_id':link_id,'previous_link_id':old['id'] if old else None,'exam_id':exam_id,'objective_submission_id':obj_id,'subjective_submission_id':sub_id,'reason':reason,'actor':actor()})
                result=dict(conn.execute('SELECT * FROM exam_answer_links WHERE id=?',(link_id,)).fetchone())
            return jsonify({'link':result,'reused':False}),201
        except (ValueError,LookupError,PermissionError,sqlite3.IntegrityError) as exc:return error(exc)
