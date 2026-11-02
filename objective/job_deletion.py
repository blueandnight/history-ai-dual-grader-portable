"""Permanent deletion of one objective upload task; shared keys/exams survive."""
import hashlib
import json
from pathlib import Path
import re
import shutil


def plan(conn,job_id,assessment_id,exam_id,*,worker_active=False):
    if not all(isinstance(v,str) for v in (job_id,assessment_id,exam_id)) or not re.fullmatch('[a-f0-9]{32}',job_id) or not re.fullmatch('[a-f0-9]{32}',assessment_id) or (exam_id and not re.fullmatch('[a-f0-9]{32}',exam_id)):
        raise ValueError('任务或考试编号无效')
    job=conn.execute('SELECT * FROM objective_jobs WHERE job_id=?',(job_id,)).fetchone()
    if job is None:raise LookupError('任务不存在或已删除')
    key=conn.execute('SELECT * FROM objective_assessments WHERE assessment_id=?',(assessment_id,)).fetchone()
    if not key or job['assessment_id']!=assessment_id or (key['source_exam_id'] or '')!=exam_id:raise ValueError('任务、答案键与考试归属不一致')
    sub='SELECT submission_id FROM objective_submissions WHERE job_id=?'
    specs=[('exam_answer_identities',f"side='objective' AND submission_id IN ({sub})"),('exam_answer_links',f'objective_submission_id IN ({sub})'),
           ('objective_review_items',f'job_id=? OR submission_id IN ({sub})'),
           ('objective_review_events',f'submission_id IN ({sub})'),
           ('objective_answers',f'submission_id IN ({sub})'),
           ('objective_submission_pages',f'submission_id IN ({sub})'),
           ('objective_submissions','job_id=?'),('objective_jobs','job_id=?')]
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    specs=[(t,q) for t,q in specs if t in tables]
    rows={t:[dict(r) for r in conn.execute('SELECT * FROM '+t+' WHERE '+q+' ORDER BY rowid',[job_id]*q.count('?'))] for t,q in specs}
    submissions=rows['objective_submissions'];ids={r['submission_id'] for r in submissions}
    if any(r['assessment_id']!=assessment_id or not re.fullmatch('[a-f0-9]{32}',r['submission_id']) for r in submissions):raise ValueError('任务答卷归属异常')
    if any(r['job_id']!=job_id or r['submission_id'] not in ids for r in rows.get('objective_review_items',[])):raise ValueError('任务复核记录归属异常')
    blocked=worker_active or job['status'] not in ('completed','needs_review','recoverable','failed') or any(r['state'] not in ('completed','needs_review','recoverable','failed') for r in submissions)
    fingerprint=hashlib.sha256(json.dumps({'rows':rows,'key':dict(key)},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    preview=dict(job_id=job_id,assessment_id=assessment_id,exam_id=exam_id,title=key['title'],created_at=job['created_at'],answer_version=key['answer_version'],
                 submissions=len(submissions),scored=sum(r['score'] is not None for r in submissions),photos=len(rows.get('objective_submission_pages',[])),
                 reviews=len(rows.get('objective_review_items',[]))+len(rows.get('objective_review_events',[])),links=len(rows.get('exam_answer_links',[])),blocked=bool(blocked),fingerprint=fingerprint)
    return preview,rows,specs


def safe_directory(path,root):
    if path==root or not path.is_relative_to(root) or path.resolve()!=path:raise ValueError('答卷文件路径越界或存在链接')
    if path.exists() and (not path.is_dir() or any(p.resolve()!=p for p in path.rglob('*'))):raise ValueError('答卷目录无效或存在链接')


def validate_deletion(conn,upload_root,body,job_id,*,worker_active=False):
    """Preflight both rows and file ownership without mutating either."""
    p,rows,specs=plan(conn,job_id,body.get('assessment_id'),body.get('exam_id'),worker_active=worker_active)
    if p['blocked']:raise ValueError('任务仍在排队或识别，请等待本组处理结束后删除')
    if body.get('confirmed') is not True or body.get('expected_fingerprint')!=p['fingerprint']:raise ValueError('删除范围已变化或尚未明确确认，请重新预览')
    root=Path(upload_root).resolve();directories=[]
    for row in rows['objective_submissions']:
        directory=root/p['assessment_id']/row['submission_id'];safe_directory(directory,root);directories.append(directory)
    for page in rows.get('objective_submission_pages',[]):
        path=Path(page['stored_path'])
        expected=root/p['assessment_id']/page['submission_id']
        if not path.is_absolute() or path.resolve()!=path or not path.is_relative_to(expected):raise ValueError('照片路径与任务归属不一致')
    for row in conn.execute('SELECT stored_path FROM objective_submission_pages WHERE submission_id NOT IN (SELECT submission_id FROM objective_submissions WHERE job_id=?)',(job_id,)):
        if any(Path(row['stored_path']).resolve().is_relative_to(d) for d in directories):raise ValueError('照片仍被其他任务引用，已拒绝删除')
    return p,rows,specs,directories,root


def apply_deletion(conn,p,specs,*,audit,actor):
    job_id=p['job_id']
    for table,where in specs:conn.execute('DELETE FROM '+table+' WHERE '+where,[job_id]*where.count('?'))
    audit(conn,'objective_job_deleted',job_id=job_id,details={'actor':actor,'assessment_id':p['assessment_id'],'source_exam_id':p['exam_id'],'counts':{k:p[k] for k in ('submissions','scored','photos','reviews','links')}})


def delete(conn,upload_root,body,job_id,*,audit,actor,worker_active=False):
    # Caller owns BEGIN IMMEDIATE and the engine's existing worker lock.
    p,_,specs,directories,root=validate_deletion(conn,upload_root,body,job_id,worker_active=worker_active)
    apply_deletion(conn,p,specs,audit=audit,actor=actor)
    return p,directories,root


def batch_items(items):
    if not isinstance(items,list) or not 1<=len(items)<=100 or any(not isinstance(i,dict) or set(i)!={'job_id','assessment_id','exam_id'} for i in items):
        raise ValueError('请选择1—100个已结束任务，并提供各任务的考试和答案键归属')
    if any(not isinstance(i['job_id'],str) for i in items) or len({i['job_id'] for i in items})!=len(items):
        raise ValueError('任务编号无效或重复选择')
    return sorted(items,key=lambda i:i['job_id'])


def batch_plan(conn,items,*,active_workers=()):
    previews=[plan(conn,i['job_id'],i['assessment_id'],i['exam_id'],worker_active=i['job_id'] in active_workers)[0] for i in batch_items(items)]
    fingerprint=hashlib.sha256(json.dumps(previews,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return dict(items=previews,jobs=len(previews),blocked=any(p['blocked'] for p in previews),fingerprint=fingerprint,
                **{k:sum(p[k] for p in previews) for k in ('submissions','scored','photos','reviews','links')})


def batch_delete(conn,upload_root,body,*,audit,actor,active_workers=()):
    """Preflight every task before any DELETE, in one caller-owned transaction."""
    p=batch_plan(conn,body.get('items'),active_workers=active_workers)
    if p['blocked']:
        raise ValueError('所选任务中仍有正在执行的任务，整批未删除，请刷新后重新选择')
    if body.get('confirmed') is not True or body.get('expected_fingerprint')!=p['fingerprint']:
        raise ValueError('批量删除范围已变化或尚未确认，整批未删除，请重新预览')
    prepared=[validate_deletion(conn,upload_root,dict(assessment_id=i['assessment_id'],exam_id=i['exam_id'],confirmed=True,expected_fingerprint=i['fingerprint']),i['job_id'],worker_active=i['job_id'] in active_workers) for i in p['items']]
    directories=[];root=Path(upload_root).resolve()
    for item,_,specs,paths,_ in prepared:
        apply_deletion(conn,item,specs,audit=audit,actor=actor)
        directories.extend(paths)
    audit(conn,'objective_jobs_batch_deleted',details={'actor':actor,'job_ids':[i['job_id'] for i in p['items']],
                                                     'counts':{k:p[k] for k in ('jobs','submissions','scored','photos','reviews','links')}})
    return p,directories,root


def cleanup(directories,root):
    warning=''
    for directory in directories:
        try:
            safe_directory(directory,root)
            if directory.exists():shutil.rmtree(directory)
        except (OSError,ValueError):warning='任务记录已删除，但部分图片清理失败，请管理员检查；不要重复提交删除。'
    return warning
