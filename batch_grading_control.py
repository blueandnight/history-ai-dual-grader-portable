"""Persist teacher stop requests in existing batch metadata, without deleting work."""
import json

STOP_REASON = '教师已停止剩余批改；当前已开始的答卷仍会处理并保存，可能继续产生费用，完成后不会启动下一份'


def stopped(batch):
    return bool(batch and json.loads(batch['manifest_json'] or '{}').get('grading_stop_requested'))


def confirmed_pause(conn, batch):
    if batch['status'] != 'paused' or stopped(batch) or batch['pause_sequence'] != batch['next_sequence']:
        return None
    return conn.execute(
        "SELECT s.id FROM submissions s JOIN task_queue q ON q.submission_id=s.id "
        "WHERE s.batch_id=? AND s.batch_sequence=? AND s.assignment_id=? "
        "AND COALESCE(s.exam_id,'')=COALESCE(?,'') AND s.state='completed' AND q.state='completed' "
        "AND s.confirmed_at IS NOT NULL AND s.confirmed_at<>'' AND s.final_total IS NOT NULL",
        (batch['id'], batch['next_sequence'], batch['assignment_id'], batch['exam_id']),
    ).fetchone()


def change(conn, batch_id, assignment_id, exam_id, action, now, audit, actor='local', expected_sequence=None):
    conn.execute('BEGIN IMMEDIATE')
    batch = conn.execute('SELECT * FROM grading_batches WHERE id=?', (batch_id,)).fetchone()
    if not batch or batch['assignment_id'] != assignment_id or str(batch['exam_id'] or '') != exam_id:
        raise ValueError('批次不属于当前考试，请刷新后核实')
    if action not in ('stop', 'resume', 'continue_confirmed'):
        raise ValueError('未知批改控制操作')
    manifest = json.loads(batch['manifest_json'] or '{}')
    running = conn.execute("SELECT COUNT(*) FROM submissions WHERE batch_id=? AND state='grading'", (batch_id,)).fetchone()[0]
    if action == 'stop':
        if batch['status'] not in ('active', 'paused'):
            raise ValueError('本批已经结束')
        manifest['grading_stop_requested'] = True
        conn.execute("UPDATE grading_batches SET manifest_json=?,status='paused',pause_sequence=next_sequence,pause_reason=?,updated_at=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), STOP_REASON, now(), batch_id))
        conn.execute("UPDATE task_queue SET state='waiting_batch',updated_at=? WHERE state='queued' AND submission_id IN (SELECT id FROM submissions WHERE batch_id=? AND state='queued')", (now(), batch_id))
    elif action == 'continue_confirmed':
        if type(expected_sequence) is not int or expected_sequence != batch['next_sequence']:
            raise ValueError('批次位置已变化，请刷新后重试')
        current = confirmed_pause(conn, batch)
        if not current or running:
            raise ValueError('当前暂停答卷尚未完成教师确认，或仍有正在评分的答卷')
        sequence = batch['next_sequence'] + 1
        status = 'completed' if sequence > batch['total_sequences'] else 'active'
        conn.execute(
            "UPDATE grading_batches SET next_sequence=?,status=?,pause_sequence=NULL,pause_reason='',updated_at=? WHERE id=?",
            (sequence, status, now(), batch_id),
        )
    else:
        if not stopped(batch):
            raise ValueError('本批没有教师停止请求；异常任务请使用原位重试或跳过')
        if running:
            raise ValueError('已开始的请求尚未结束，请刷新核实后再继续')
        manifest.pop('grading_stop_requested', None)
        current = conn.execute('SELECT state FROM submissions WHERE batch_id=? AND batch_sequence=?', (batch_id, batch['next_sequence'])).fetchone()
        needs_recovery = bool(current and current['state'] != 'queued')
        status = 'completed' if batch['next_sequence'] > batch['total_sequences'] else 'paused' if needs_recovery else 'active'
        conn.execute('UPDATE grading_batches SET manifest_json=?,status=?,pause_sequence=?,pause_reason=?,updated_at=? WHERE id=?', (json.dumps(manifest, ensure_ascii=False), status, batch['next_sequence'] if needs_recovery else None, '当前序号需人工核实后原位重试或确认跳过' if needs_recovery else '', now(), batch_id))
    details = {'exam_id':exam_id, 'assignment_id':assignment_id, 'running':running, 'actor':actor}
    if action == 'continue_confirmed':
        details.update(sequence=batch['next_sequence'], submission_id=current['id'])
    audit(conn, 'grading_batch_' + action + '_requested', 'grading_batch', batch_id, details)
    waiting = conn.execute("SELECT COUNT(*) FROM submissions WHERE batch_id=? AND state='queued'", (batch_id,)).fetchone()[0]
    return {'batch_id':batch_id, 'grading_stopped':action == 'stop', 'running':running, 'waiting':waiting}
