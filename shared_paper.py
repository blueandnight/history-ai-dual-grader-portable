"""Same-upload provenance, backed by the already approved outbound page hashes."""
import json

METHOD = 'same_upload_pages'


def approved_pages(conn, exam_id, submission_id):
    row = conn.execute('SELECT * FROM submissions WHERE id=?', (submission_id,)).fetchone()
    exam = conn.execute('SELECT assignment_id FROM grading_exams WHERE id=?', (exam_id,)).fetchone()
    if not row or not exam or row['exam_id'] != exam_id or row['assignment_id'] != exam['assignment_id']:
        raise ValueError('整卷来源不属于本考试')
    if not row['ai_privacy_confirmed'] or row['privacy_status'] not in ('pre_anonymized','redacted','no_identity_visible'):
        raise ValueError('整卷缺少有效隐私授权')
    photos = json.loads(row['photos_json'] or '[]')
    hashes = [p.get('approved_ai_page_sha256') for p in photos]
    if not hashes or not all(hashes) or any(p.get('image_role') == 'answer_region' for p in photos):
        raise ValueError('整卷需要已确认的完整外发页面，裁切小题不能用作客观题整卷来源')
    return hashes


def verified(conn, link):
    if not link or link['method'] != METHOD or not link['active'] or link['ended_at']:
        return False
    try:
        proof = json.loads(link['reason'])
        hashes = approved_pages(conn, link['exam_id'], link['subjective_submission_id'])
        pages = conn.execute('SELECT image_sha256 FROM objective_submission_pages WHERE submission_id=? ORDER BY page_no', (link['objective_submission_id'],)).fetchall()
        return proof['approved_pages'] == hashes and proof['objective_pages'] == [p[0] for p in pages] and len(pages) == len(hashes)
    except (ValueError, TypeError, KeyError):
        return False
