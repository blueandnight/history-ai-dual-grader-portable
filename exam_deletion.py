"""Explicit permanent deletion of one assignment and its linked exam records."""
import hashlib
import json
from pathlib import Path
import re
import shutil


def deletion_plan(conn, assignment_id):
    if not isinstance(assignment_id, str) or not re.fullmatch(r'[0-9a-f]{12}', assignment_id):
        raise ValueError('考试编号无效')
    assignment=conn.execute('SELECT * FROM assignments WHERE id=?',(assignment_id,)).fetchone()
    if assignment is None:raise ValueError('考试不存在')
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sub='SELECT id FROM submissions WHERE assignment_id=?'
    exams='SELECT id FROM grading_exams WHERE assignment_id=?'
    assessments=f'SELECT assessment_id FROM objective_assessments WHERE source_exam_id IN ({exams})'
    objects=f'SELECT submission_id FROM objective_submissions WHERE assessment_id IN ({assessments})'
    jobs=f'SELECT job_id FROM objective_jobs WHERE assessment_id IN ({assessments})'
    has_objective='objective_assessments' in tables and 'source_exam_id' in {r[1] for r in conn.execute('PRAGMA table_info(objective_assessments)')}
    specs=[]
    def add(table,where):
        if table in tables:specs.append((table,where))
    link_filter=f'exam_id IN ({exams}) OR subjective_submission_id IN ({sub})'
    if has_objective:
        link_filter+=f' OR objective_submission_id IN ({objects})'
    add('exam_answer_identities',f'exam_id IN ({exams})')
    add('exam_identity_profiles',f'exam_id IN ({exams})')
    add('exam_answer_links',link_filter)
    if has_objective:
        for table in ('objective_review_items','objective_review_events','objective_answers','objective_submission_pages'):
            add(table,f'submission_id IN ({objects})')
        add('objective_audit_events',f'job_id IN ({jobs}) OR submission_id IN ({objects})')
        add('objective_submissions',f'assessment_id IN ({assessments})')
        add('objective_jobs',f'assessment_id IN ({assessments})')
        add('objective_assessments',f'source_exam_id IN ({exams})')
    add('task_queue',f'submission_id IN ({sub})')
    add('exam_marking_codes',f'exam_id IN ({exams}) OR submission_id IN ({sub})')
    for table in ('privacy_templates','rubric_versions','grading_batches','submissions','grading_exams','assignments'):
        add(table,'id=?' if table=='assignments' else 'assignment_id=?')
    rows={}
    for table,where in specs:
        rows[table]=[dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY rowid',[assignment_id]*where.count('?'))]
    subjects=rows.get('submissions',[]); obj=rows.get('objective_submissions',[])
    # The main handler excludes in-flight work using ACTIVE_TASKS_CONDITION.
    # Pending IDs may still exist in memory; process_submission re-reads the
    # database under the same task gate and returns if the row was deleted.
    blocked=any(r['state']=='grading' for r in subjects)
    blocked=blocked or any(r['state']=='running' for r in rows.get('task_queue',[]))
    blocked=blocked or any(r['state'] in ('queued','processing','recoverable') for r in obj)
    blocked=blocked or any(r['status'] in ('queued','processing','recoverable') for r in rows.get('objective_jobs',[]))
    pending_subjective=sum(r['state'] in ('queued','recovery_pending') for r in subjects)
    unfinished_batches=sum(r['status'] in ('active','paused') for r in rows.get('grading_batches',[]))
    photos=sum(len(json.loads(r['photos_json'] or '[]')) for r in subjects)+len(rows.get('objective_submission_pages',[]))
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return dict(rows=rows,specs=specs,preview=dict(assignment_id=assignment_id,name=assignment['name'],fingerprint=digest,blocked=bool(blocked),pending_subjective=pending_subjective,unfinished_batches=unfinished_batches,exams=len(rows.get('grading_exams',[])),subjective=len(subjects),objective=len(obj),photos=photos,links=len(rows.get('exam_answer_links',[]))))


def delete_exam(connect,data_dir,payload,*,strict,audit,actor):
    aid=payload.get('assignment_id');conn=connect();directories=[]
    try:
        conn.execute('BEGIN IMMEDIATE')
        plan=deletion_plan(conn,aid);preview=plan['preview'];rows=plan['rows']
        if preview['blocked']:raise ValueError('仍有上传或批改任务未结束，请处理后再删除')
        if strict and (payload.get('confirmation_name')!=preview['name'] or payload.get('expected_fingerprint')!=preview['fingerprint']):
            raise ValueError('考试名称或删除范围已变化，请重新核对')
        root=Path(data_dir).resolve()
        exam_ids={r['id'] for r in rows.get('grading_exams',[])}
        assessment_ids={r['assessment_id'] for r in rows.get('objective_assessments',[])}
        if any(not re.fullmatch(r'[0-9a-f]{32}', value) for value in exam_ids|assessment_ids):
            raise ValueError('考试或答案键编号无效')
        directories=[root/aid]+[root/'objective'/'uploads'/value for value in sorted(assessment_ids)]
        directories += [root/'exam_assets'/value for value in sorted(exam_ids)]
        crop_root=root/'student_auto_crop'
        if crop_root.resolve()!=crop_root:raise ValueError('裁切缓存目录无效')
        if crop_root.exists():
            for directory in crop_root.iterdir():
                if not re.fullmatch(r'[0-9a-f]{32}',directory.name) or not directory.is_dir():continue
                if directory.resolve()!=directory:raise ValueError('裁切缓存路径无效')
                manifest=directory/'run.json'
                if manifest.is_file():
                    try:run=json.loads(manifest.read_text(encoding='utf-8'))
                    except (ValueError,OSError):continue
                    if isinstance(run,dict) and run.get('exam_id') in exam_ids:directories.append(directory)
        for directory in directories:
            if directory.resolve()!=directory or not directory.is_relative_to(root) or directory==root:
                raise ValueError('考试文件路径无效，已拒绝删除')
        # Never trust stored absolute page paths as deletion targets.
        for page in rows.get('objective_submission_pages',[]):
            path=Path(page['stored_path']).resolve()
            if not any(path.is_relative_to(d) for d in directories):
                raise ValueError('照片路径与考试目录不一致，已拒绝删除')
        for table,where in plan['specs']:
            conn.execute(f'DELETE FROM {table} WHERE {where}',[aid]*where.count('?'))
        audit(conn,'exam_deleted','assignment',aid,{'actor':actor,'counts':{k:preview[k] for k in ('exams','subjective','objective','photos','links')}})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:conn.close()
    warning=''
    for directory in directories:
        try:
            if directory.resolve()!=directory or not directory.is_relative_to(root):raise OSError('unsafe cleanup path')
            if directory.exists():shutil.rmtree(directory)
        except OSError:warning='考试业务记录已删除，但部分文件未能清理，请由管理员检查本机数据目录。'
    return dict(ok=True,assignment_id=aid,warning=warning,message=warning or '考试、主客观题答卷、成绩和关联已删除；备份、审计与共享资源保留')
