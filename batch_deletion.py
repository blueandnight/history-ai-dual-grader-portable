"""Permanent deletion of one subjective upload batch, never its shared exam."""
import hashlib
import json
from pathlib import Path
import re
import shutil


def run_ids(photos_json):
    photos=json.loads(photos_json or '[]')
    if not isinstance(photos,list):raise ValueError('照片记录无效，不能确定删除范围')
    result=set()
    for photo in photos:
        if not isinstance(photo,dict):raise ValueError('照片记录无效')
        provenance=photo.get('answer_region_provenance') or {}
        if not isinstance(provenance,dict):raise ValueError('裁切来源记录无效')
        run=provenance.get('alignment_run_id')
        if run:
            if not isinstance(run,str) or not re.fullmatch('[a-f0-9]{32}',run):raise ValueError('裁切来源编号无效')
            result.add(run)
    return result


def plan(conn,batch_id,assignment_id,exam_id):
    if not isinstance(batch_id,str) or not re.fullmatch('[a-f0-9]{12}',batch_id):raise ValueError('批次编号无效')
    b=conn.execute('SELECT * FROM grading_batches WHERE id=?',(batch_id,)).fetchone()
    if b is None:raise ValueError('批次不存在或已删除')
    if b['assignment_id']!=assignment_id or (b['exam_id'] or '')!=exam_id:raise ValueError('批次不属于当前考试')
    if exam_id:
        exam=conn.execute('SELECT assignment_id FROM grading_exams WHERE id=?',(exam_id,)).fetchone()
        if not exam or exam['assignment_id']!=assignment_id:raise ValueError('正式考试归属不一致')
    a=conn.execute('SELECT name FROM assignments WHERE id=?',(assignment_id,)).fetchone()
    if not a:raise ValueError('考试来源不存在')
    specs=[('exam_answer_identities',"side='subjective' AND submission_id IN (SELECT id FROM submissions WHERE batch_id=?)"),('exam_answer_links','subjective_submission_id IN (SELECT id FROM submissions WHERE batch_id=?)'),
           ('task_queue','submission_id IN (SELECT id FROM submissions WHERE batch_id=?)'),
           ('exam_marking_codes','batch_id=? OR submission_id IN (SELECT id FROM submissions WHERE batch_id=?)'),
           ('submissions','batch_id=?'),('grading_batches','id=?')]
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    specs=[(t,q) for t,q in specs if t in tables]
    rows={t:[dict(r) for r in conn.execute('SELECT * FROM '+t+' WHERE '+q+' ORDER BY rowid',[batch_id]*q.count('?'))] for t,q in specs}
    subjects=rows['submissions']
    if any(r['assignment_id']!=assignment_id or (r['exam_id'] or '')!=exam_id or not re.fullmatch('[a-f0-9]{12}',r['id']) for r in subjects):
        raise ValueError('批次答卷归属不一致，已拒绝删除')
    manifest=json.loads(b['manifest_json'] or '{}')
    if not isinstance(manifest,dict) or not isinstance(manifest.get('items',[]),list):raise ValueError('批次预约记录无效')
    items=manifest.get('items',[])
    if any(not isinstance(i,dict) for i in items):raise ValueError('批次预约项目无效')
    reserved={i.get('submission_id') for i in items}
    if len(reserved)!=len(items):raise ValueError('批次预约答卷重复')
    if any(not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{12}',sid) for sid in reserved):raise ValueError('预约答卷编号无效')
    ids={r['id'] for r in subjects}|reserved
    for sid in reserved:
        other=conn.execute('SELECT batch_id FROM submissions WHERE id=?',(sid,)).fetchone()
        if other and other['batch_id']!=batch_id:raise ValueError('预约答卷被其他批次引用，已拒绝删除')
    for r in subjects:run_ids(r['photos_json'])
    if any((r['batch_id'] not in (None,batch_id)) or (r['submission_id'] and r['submission_id'] not in ids) or (exam_id and r['exam_id']!=exam_id) for r in rows.get('exam_marking_codes',[])):
        raise ValueError('身份映射归属不一致，已拒绝删除')
    blocked=any(r['state']=='grading' for r in subjects) or any(r['state']=='running' for r in rows.get('task_queue',[]))
    fingerprint=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    photos=sum(len(json.loads(r['photos_json'] or '[]')) for r in subjects)
    preview=dict(batch_id=batch_id,assignment_id=assignment_id,exam_id=exam_id,name=a['name'],created_at=b['created_at'],
                 total=b['total_sequences'],submissions=len(subjects),confirmed=sum(bool(r['confirmed_at']) for r in subjects),
                 pending=sum(r['state'] in ('queued','recovery_pending') for r in subjects),photos=photos,
                 links=len(rows.get('exam_answer_links',[])),blocked=blocked,fingerprint=fingerprint)
    preview['unuploaded']=len(reserved-{r['id'] for r in subjects})
    return preview,rows,specs


def safe_directory(path,root):
    if path==root or not path.is_relative_to(root) or path.resolve()!=path:raise ValueError('文件路径越界或存在链接，已拒绝删除')
    if path.exists():
        if not path.is_dir():raise ValueError('答卷目录无效')
        if any(p.resolve()!=p for p in path.rglob('*')):raise ValueError('答卷文件存在链接，已拒绝删除')


def delete(connect,data_dir,body,*,audit,actor,resolve_path):
    conn=connect();directories=[];shared=0
    try:
        conn.execute('BEGIN IMMEDIATE')
        preview,rows,specs=plan(conn,body.get('batch_id'),body.get('assignment_id'),body.get('exam_id'))
        if body.get('confirmed') is not True or body.get('expected_fingerprint')!=preview['fingerprint']:raise ValueError('请重新预览删除范围并明确确认')
        if preview['blocked']:raise ValueError('本批仍有正在执行的评分任务，请待任务结束后删除')
        root=Path(data_dir).resolve();aid=preview['assignment_id'];runs=set()
        for row in rows['submissions']:
            directory=root/aid/'students'/row['id'];safe_directory(directory,root);directories.append(directory)
            for photo in json.loads(row['photos_json'] or '[]'):
                for key in ('path','original_path'):
                    if photo.get(key) and not resolve_path(photo[key]).is_relative_to(directory):raise ValueError('照片不属于本份答卷目录，已拒绝删除')
            runs.update(run_ids(row['photos_json']))
        manifest=json.loads(rows['grading_batches'][0]['manifest_json'] or '{}')
        for item in manifest.get('items',[]):
            directory=root/aid/'students'/item['submission_id'];safe_directory(directory,root)
            if directory not in directories:directories.append(directory)
        other_runs=set()
        for row in conn.execute("SELECT photos_json FROM submissions WHERE batch_id IS NULL OR batch_id<>?",(preview['batch_id'],)):
            other_photos=json.loads(row['photos_json'] or '[]')
            if not isinstance(other_photos,list) or any(not isinstance(p,dict) for p in other_photos):raise ValueError('其他答卷照片引用无法核实，已停止删除')
            if runs:other_runs.update(run_ids(row['photos_json']))
            for photo in other_photos:
                for key in ('path','original_path'):
                    if not photo.get(key):continue
                    try:path=resolve_path(photo[key])
                    except ValueError:continue
                    if any(path.is_relative_to(d) for d in directories):raise ValueError('本批照片仍被其他答卷引用，已阻止删除')
        for run in runs:
            if run in other_runs:shared+=1;continue
            directory=root/'student_auto_crop'/run;safe_directory(directory,root)
            if directory.exists():
                manifest=json.loads((directory/'run.json').read_text(encoding='utf-8'))
                if not isinstance(manifest,dict) or manifest.get('exam_id')!=preview['exam_id']:raise ValueError('裁切缓存归属不一致')
                directories.append(directory)
        for table,where in specs:conn.execute('DELETE FROM '+table+' WHERE '+where,[preview['batch_id']]*where.count('?'))
        audit(conn,'batch_deleted','grading_batch',preview['batch_id'],{'actor':actor,'assignment_id':aid,'exam_id':preview['exam_id'],'counts':{k:preview[k] for k in ('submissions','confirmed','pending','photos','links')},'shared_crop_runs_preserved':shared})
        conn.commit()
    except Exception:
        conn.rollback();raise
    finally:conn.close()
    warning=''
    for directory in directories:
        try:
            safe_directory(directory,root)
            if directory.exists():shutil.rmtree(directory)
        except (OSError,ValueError):warning='本批业务记录已删除，但部分文件清理失败，请管理员检查；不要重复删除。'
    return dict(ok=True,batch_id=preview['batch_id'],warning=warning,shared_crop_runs_preserved=shared)
