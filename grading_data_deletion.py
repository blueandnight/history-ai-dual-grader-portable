"""Atomic answer-group deletion and exam reset; configuration is never deleted."""
import hashlib
import json
import re
from pathlib import Path

from batch_deletion import safe_directory, run_ids
from objective.job_deletion import cleanup


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scope(conn, aid, exam_id):
    if not isinstance(aid, str) or not re.fullmatch('[a-f0-9]{12}', aid):
        raise ValueError('考试来源编号无效')
    assignment = conn.execute('SELECT * FROM assignments WHERE id=?', (aid,)).fetchone()
    if not assignment:
        raise ValueError('考试不存在或已删除')
    exams = [r['id'] for r in conn.execute('SELECT id FROM grading_exams WHERE assignment_id=?', (aid,))]
    if exam_id is not None and (not isinstance(exam_id, str) or (exam_id and exam_id not in exams)):
        raise ValueError('阅卷记录不属于当前考试')
    subjects = [dict(r) for r in conn.execute('SELECT * FROM submissions WHERE assignment_id=? ORDER BY created_at,id', (aid,))
                if exam_id is None or (r['exam_id'] or '') == exam_id]
    objects = [dict(r) for r in conn.execute('SELECT s.*,a.source_exam_id FROM objective_submissions s JOIN objective_assessments a ON a.assessment_id=s.assessment_id ORDER BY s.created_at,s.submission_id')
               if r['source_exam_id'] in exams and (exam_id is None or r['source_exam_id'] == exam_id)]
    return assignment, exams, subjects, objects


def groups(conn, aid, exam_id, active_workers=()):
    _, _, subjects, objects = scope(conn, aid, exam_id)
    records = {'s:'+r['id']: ('subjective', r) for r in subjects}
    records.update({'o:'+r['submission_id']: ('objective', r) for r in objects})
    parent = {k:k for k in records}
    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k
    # Only proven same-upload origins form deletion groups. A manually linked
    # student's independently uploaded papers remain independently selectable.
    from shared_paper import verified
    for link in conn.execute("SELECT * FROM exam_answer_links WHERE method='same_upload_pages'"):
        s, o = 's:'+link['subjective_submission_id'], 'o:'+link['objective_submission_id']
        if s in records and o in records and records[s][1]['exam_id'] == records[o][1]['source_exam_id']:
            historical = dict(link, active=1, ended_at=None)
            try:
                proof=json.loads(link['reason'])
                hashes=proof['approved_pages']
                saved_origin=isinstance(hashes,list) and bool(hashes) and hashes==proof['objective_pages'] and all(isinstance(h,str) and re.fullmatch('[a-f0-9]{64}',h) for h in hashes)
            except (ValueError,KeyError,TypeError):
                saved_origin=False
            # Origin is immutable even after unlink or deliberate photo cleanup.
            # This groups deletion targets only; it never restores a score link.
            if link['exam_id']==records[s][1]['exam_id'] and (saved_origin or verified(conn, historical)):
                parent[find(o)] = find(s)
    result = {}
    for key, (side, row) in records.items():
        root = find(key)
        item = result.setdefault(root, dict(key=root, members=[], label='', subjective=0, objective=0, blocked=False, states=[]))
        item['members'].append(key);item[side] += 1
        item['label'] = item['label'] or row.get('marking_code') or row.get('student_label') or row.get('anonymous_code') or key[2:10]
        item['states'].append(row['state'])
        if side == 'subjective':
            item['blocked'] |= row['state'] == 'grading' or bool(conn.execute("SELECT 1 FROM task_queue WHERE submission_id=? AND state='running'", (row['id'],)).fetchone())
        else:
            item['blocked'] |= row['state'] in ('processing','queued') or row['job_id'] in active_workers
    return list(result.values())


def plan(conn, body, active_workers=()):
    aid, eid, clearing = body.get('assignment_id'), body.get('exam_id'), body.get('clear') is True
    if not clearing and eid is None:
        raise ValueError('请选择明确的阅卷记录；快速匿名使用空考试编号')
    assignment, exams, subjects, objects = scope(conn, aid, eid)
    all_groups = groups(conn, aid, eid, active_workers)
    keys = body.get('keys')
    assessment_ids=[]
    if clearing:
        selected = all_groups
    else:
        if not isinstance(keys,list) or not 1 <= len(keys) <= 500 or any(not isinstance(k,str) for k in keys) or len(set(keys)) != len(keys):
            raise ValueError('请选择1—500组答卷，不要重复选择')
        selected = [g for g in all_groups if g['key'] in keys]
        if len(selected) != len(keys):
            raise ValueError('答卷已变化或不属于当前考试，请重新选择')
    members = {k for g in selected for k in g['members']}
    subjects = [s for s in subjects if 's:'+s['id'] in members]
    objects = [o for o in objects if 'o:'+o['submission_id'] in members]
    sids, oids = [s['id'] for s in subjects], [o['submission_id'] for o in objects]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    specs = []
    def add(table, field, ids, extra=''):
        if ids and table in tables:
            specs.append((table, field+' IN ('+','.join('?' for _ in ids)+')'+extra, list(ids)))
    if 'exam_answer_links' in tables:
        add('exam_answer_links','subjective_submission_id',sids)
        add('exam_answer_links','objective_submission_id',oids)
    add('exam_answer_identities','submission_id',sids," AND side='subjective'")
    add('exam_answer_identities','submission_id',oids," AND side='objective'")
    for t in ('objective_review_items','objective_review_events','objective_answers','objective_submission_pages'):
        add(t,'submission_id',oids)
    add('task_queue','submission_id',sids)
    add('exam_marking_codes','submission_id',sids)
    add('submissions','id',sids)
    add('objective_submissions','submission_id',oids)
    batches = [dict(r) for r in conn.execute('SELECT * FROM grading_batches WHERE assignment_id=?', (aid,))
               if (clearing and (eid is None or (r['exam_id'] or '')==eid)) or r['id'] in {s['batch_id'] for s in subjects}]
    job_ids = {o['job_id'] for o in objects}
    if clearing:
        # Also remove empty/failed upload tasks and unused reserved marking codes.
        assessment_ids = [r[0] for r in conn.execute('SELECT assessment_id FROM objective_assessments WHERE source_exam_id IN ('+','.join('?' for _ in exams)+')',exams)] if exams else []
        if eid is not None:
            assessment_ids = [r[0] for r in conn.execute('SELECT assessment_id FROM objective_assessments WHERE source_exam_id=?',(eid,))]
        job_ids.update(r[0] for r in conn.execute('SELECT job_id,assessment_id FROM objective_jobs') if r[1] in assessment_ids)
        scoped_exams=exams if eid is None else [eid] if eid else []
        add('exam_answer_links','exam_id',scoped_exams)
        add('exam_answer_identities','exam_id',scoped_exams)
        reserved_ids=[]
        for b in batches:
            for item in json.loads(b['manifest_json'] or '{}').get('items',[]):
                sid=item.get('submission_id','')
                if not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{12}',sid):raise ValueError('预约答卷编号无效')
                reserved_ids.append(sid)
        add('task_queue','submission_id',sorted(set(reserved_ids)))
        add('objective_review_items','job_id', sorted(job_ids))
        add('objective_jobs','job_id',sorted(job_ids))
        if eid is None:
            add('exam_marking_codes','exam_id',exams)
        elif eid:
            add('exam_marking_codes','exam_id',[eid])
        add('grading_batches','id',[b['id'] for b in batches])
    rows = [(t,w,args,[dict(r) for r in conn.execute('SELECT * FROM '+t+' WHERE '+w+' ORDER BY rowid',args)]) for t,w,args in specs]
    # Include mutable parent records in the preview, not just child scores.
    jobs = [dict(r) for r in conn.execute('SELECT * FROM objective_jobs') if r['job_id'] in job_ids]
    blocked = any(g['blocked'] for g in selected) or any(j['job_id'] in active_workers or j['status']=='processing' for j in jobs)
    if clearing:
        blocked = any(s['state']=='grading' or conn.execute("SELECT 1 FROM task_queue WHERE submission_id=? AND state='running'",(s['id'],)).fetchone() for s in subjects)
        blocked = blocked or any(o['state']=='processing' for o in objects) or any(j['job_id'] in active_workers or j['status']=='processing' for j in jobs)
    pages = [r for t,_,_,rs in rows if t=='objective_submission_pages' for r in rs]
    # Clearing may cancel queued work under the worker locks; never active calls.
    preview = dict(assignment_id=aid,exam_id=eid,name=assignment['name'],clear=clearing,groups=len(selected),subjective=len(sids),objective=len(oids),batches=len(batches),jobs=len(jobs),
                   photos=sum(len(json.loads(s['photos_json'] or '[]')) for s in subjects)+len(pages),blocked=bool(blocked),
                   fingerprint=fingerprint(dict(rows=rows,batches=batches,jobs=jobs,groups=selected)))
    return dict(preview=preview,subjects=subjects,objects=objects,sids=sids,oids=oids,rows=rows,specs=specs,batches=batches,jobs=jobs,pages=pages,assessment_ids=assessment_ids)


def remove(api, body, *, active_workers=(), upload_root=None, actor='local'):
    """Caller holds the main-operation condition and objective worker lock."""
    conn = api.get_db();root=Path(api.DATA_DIR).resolve();directories=[]
    try:
        conn.execute('BEGIN IMMEDIATE')
        p = plan(conn,body,active_workers);preview=p['preview']
        if body.get('confirmed') is not True or body.get('expected_fingerprint') != preview['fingerprint']:
            raise ValueError('删除范围已变化，请重新预览并确认')
        if preview['blocked']:
            raise ValueError('所选答卷仍有正在执行的任务，请先停止并等待当前调用结束')
        runs=set();sids=set(p['sids']);oids=set(p['oids']);aid=body['assignment_id']
        for s in p['subjects']:
            photos=json.loads(s['photos_json'] or '[]')
            directory=api.submission_photo_directory(aid,s['id'],photos) or root/aid/'students'/s['id']
            safe_directory(directory,root);directories.append(directory)
            for photo in photos:
                for field in ('path','original_path'):
                    if photo.get(field) and not api.resolve_data_relative_path(photo[field]).is_relative_to(directory):
                        raise ValueError('照片不属于当前答卷，已停止删除')
            runs.update(run_ids(s['photos_json']))
        if body.get('clear') is True:
            for b in p['batches']:
                for item in json.loads(b['manifest_json'] or '{}').get('items',[]):
                    sid=item.get('submission_id','')
                    if not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{12}',sid):raise ValueError('预约答卷编号无效')
                    other=conn.execute('SELECT assignment_id,exam_id FROM submissions WHERE id=?',(sid,)).fetchone()
                    if other and sid not in sids:raise ValueError('预约答卷被其他阅卷记录引用')
                    directories.append(root/aid/'students'/sid)
        upload_root=Path(upload_root or root/'objective'/'uploads').resolve()
        if not upload_root.is_relative_to(root):raise ValueError('客观题上传目录不属于当前数据目录')
        if body.get('clear') is True:
            if body.get('exam_id') is None:
                directories.append(root/aid/'students')
            for assessment_id in p['assessment_ids']:
                if not re.fullmatch('[a-f0-9]{32}',assessment_id):raise ValueError('答案键编号无效')
                # This directory holds uploads only, never answer-key configuration.
                directories.append(upload_root/assessment_id)
        for o in p['objects']:
            if not all(re.fullmatch('[a-f0-9]{32}',o[k]) for k in ('assessment_id','submission_id')):raise ValueError('客观题编号无效')
            directory=upload_root/o['assessment_id']/o['submission_id'];safe_directory(directory,root);directories.append(directory)
        for page in p['pages']:
            target=Path(page['stored_path']);owner=next(o for o in p['objects'] if o['submission_id']==page['submission_id'])
            if not target.is_absolute() or target.resolve()!=target or not target.is_relative_to(upload_root/owner['assessment_id']/owner['submission_id']):raise ValueError('客观题照片路径与归属不一致')
        other_runs=set()
        for s in conn.execute('SELECT id,photos_json FROM submissions'):
            if s['id'] in sids:continue
            other_runs.update(run_ids(s['photos_json']))
            for photo in json.loads(s['photos_json'] or '[]'):
                for field in ('path','original_path'):
                    if photo.get(field) and any(api.resolve_data_relative_path(photo[field]).is_relative_to(d) for d in directories):raise ValueError('照片仍被未选答卷引用')
        if body.get('clear') is True:
            # Canceled previews can leave an alignment run without a submission.
            _,exam_ids,_,_=scope(conn,aid,body.get('exam_id'))
            selected_exams=set(exam_ids if body.get('exam_id') is None else [body.get('exam_id')])
            cache=root/'student_auto_crop'
            safe_directory(cache,root)
            if cache.exists():
                for directory in cache.iterdir():
                    if not re.fullmatch('[a-f0-9]{32}',directory.name) or not directory.is_dir():continue
                    safe_directory(directory,root)
                    manifest=directory/'run.json'
                    if not manifest.is_file():continue
                    try:run=json.loads(manifest.read_text(encoding='utf-8'))
                    except (ValueError,OSError):continue
                    if isinstance(run,dict) and run.get('exam_id') in selected_exams and directory.name not in other_runs:
                        runs.add(directory.name)
        for run in runs-other_runs:
            directory=root/'student_auto_crop'/run
            if directory.exists():
                manifest=json.loads((directory/'run.json').read_text(encoding='utf-8'))
                selected_exams=set(exam_ids if body.get('exam_id') is None else [body.get('exam_id')]) if body.get('clear') else {s['exam_id'] for s in p['subjects']}
                if manifest.get('exam_id') not in selected_exams:raise ValueError('裁切缓存归属不一致')
                directories.append(directory)
        for page in conn.execute('SELECT submission_id,stored_path FROM objective_submission_pages'):
            if page['submission_id'] not in oids and any(Path(page['stored_path']).resolve().is_relative_to(d) for d in directories):raise ValueError('图片仍被未选客观题引用')
        for d in directories:safe_directory(d,root)
        for table,where,args in p['specs']:conn.execute('DELETE FROM '+table+' WHERE '+where,args)
        if not body.get('clear'):
            for b in p['batches']:
                manifest=json.loads(b['manifest_json'] or '{}')
                for item in manifest.get('items',[]):
                    if item.get('submission_id') in sids:
                        item.update(upload_status='deleted',upload_error='教师已永久删除本份答卷')
                remaining=conn.execute('SELECT 1 FROM submissions WHERE batch_id=?',(b['id'],)).fetchone()
                if not remaining and all(i.get('upload_status') in ('deleted','skipped') for i in manifest.get('items',[])):
                    conn.execute('DELETE FROM grading_batches WHERE id=?',(b['id'],))
                else:
                    current=b['next_sequence'];deleted={int(i['sequence']) for i in manifest.get('items',[]) if i.get('upload_status')=='deleted'}
                    while current in deleted:current+=1
                    status='completed' if current>b['total_sequences'] else b['status']
                    pause=b['pause_sequence'];reason=b['pause_reason']
                    if current!=b['next_sequence'] and status!='completed':
                        status='paused';pause=current;reason='当前答卷已删除，可继续处理其余答卷'
                        manifest['grading_stop_requested']=True
                        conn.execute("UPDATE task_queue SET state='waiting_batch' WHERE state='queued' AND submission_id IN (SELECT id FROM submissions WHERE batch_id=?)",(b['id'],))
                    if status=='completed':pause=None;reason=''
                    conn.execute('UPDATE grading_batches SET manifest_json=?,next_sequence=?,status=?,pause_sequence=?,pause_reason=?,updated_at=? WHERE id=?', (json.dumps(manifest,ensure_ascii=False),current,status,pause,reason,api.utc_now(),b['id']))
            from objective.app import _refresh_job_summary
            for job in p['jobs']:
                jid=job['job_id'];left=conn.execute('SELECT COUNT(*),COALESCE(SUM(page_count),0) FROM objective_submissions WHERE job_id=?',(jid,)).fetchone()
                if not left[0]:conn.execute('DELETE FROM objective_jobs WHERE job_id=?',(jid,))
                else:
                    conn.execute('UPDATE objective_jobs SET total_submissions=?,total_files=? WHERE job_id=?',(left[0],left[1],jid));_refresh_job_summary(conn,jid)
        verified_remaining=None
        if body.get('clear'):
            # Check actual rows in the SAME transaction and database, not just
            # the pre-delete preview or the subjective count shown in the UI.
            _,_,subjects_left,objects_left=scope(conn,aid,body.get('exam_id'))
            residuals={}
            for table,where,args in p['specs']:
                residuals[table]=residuals.get(table,0)+conn.execute('SELECT COUNT(*) FROM '+table+' WHERE '+where,args).fetchone()[0]
            if subjects_left or objects_left or any(residuals.values()):
                raise ValueError('清空后仍有答卷、任务或身份关联残留，本次操作已整体回滚；请重新读取范围核对')
            verified_remaining=dict(subjective=0,objective=0,jobs=0,batches=0,identities=0,links=0)
        api.append_audit_event(conn,'exam_grading_data_cleared' if body.get('clear') else 'answer_groups_deleted','assignment',aid,
            dict(actor=actor,exam_id=body.get('exam_id'),counts={k:preview[k] for k in ('groups','subjective','objective','photos')},selected_keys=body.get('keys',[]),verified_remaining=verified_remaining))
        conn.commit()
    except Exception:
        conn.rollback();raise
    finally:conn.close()
    warning=cleanup(list(set(directories)),root)
    return dict(ok=True,**{k:preview[k] for k in ('groups','subjective','objective','photos')},warning=warning,verified_remaining=verified_remaining)
