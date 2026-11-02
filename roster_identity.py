"""Teacher-side roster matching. Never imported by the model/scoring pipeline."""
import uuid
from collections import Counter









def ensure_class(conn, name, now):
    name = str(name or "").strip()
    if not name or len(name) > 100:
        raise ValueError("请填写班级名称（最多100字）")
    row = conn.execute("SELECT id FROM roster_classes WHERE class_name=?", (name,)).fetchone()
    if row:
        return row[0]
    class_id = uuid.uuid4().hex
    conn.execute("INSERT INTO roster_classes (id,class_name,created_at) VALUES (?,?,?)", (class_id, name, now))
    return class_id


def find_class(conn, name):
    """Resolve displayed legacy classes without creating records on reads."""
    name = str(name or "").strip()
    row = conn.execute("SELECT id,class_name FROM roster_classes WHERE class_name=?", (name,)).fetchone()
    if row:
        return dict(row)
    if name and conn.execute("SELECT 1 FROM roster_students WHERE class_id IS NULL AND class_name=? LIMIT 1", (name,)).fetchone():
        return {"id": None, "class_name": name}
    return None


def class_students(conn, class_id=None, class_name="", *, enabled_only=True):
    """Expose existing legacy student_no as a read-time code, never rewrite IDs.

    A registered class can coexist with untouched legacy students of that exact
    class. Students registered to another class are never included by name.
    """
    if class_id:
        row = conn.execute("SELECT class_name FROM roster_classes WHERE id=?", (class_id,)).fetchone()
        if not row:
            return []
        class_name = row[0]
    if not class_name:
        return []
    return conn.execute(
        "SELECT s.*, CASE WHEN s.class_id IS NULL AND s.identity_code='' THEN s.student_no ELSE s.identity_code END AS effective_identity_code "
        "FROM roster_students s WHERE (s.class_id=? OR (s.class_id IS NULL AND s.class_name=?)) "
        + ("AND s.enabled=1 " if enabled_only else "") + "ORDER BY effective_identity_code,s.id",
        (class_id, class_name),
    ).fetchall()




def migrate(conn, ensure_column, now):
    conn.execute("CREATE TABLE IF NOT EXISTS roster_classes (id TEXT PRIMARY KEY,class_name TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)")
    ensure_column(conn, "roster_students", "class_id", "TEXT REFERENCES roster_classes(id)")
    ensure_column(conn, "roster_students", "identity_code", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "roster_students", "enabled", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "grading_exams", "class_id", "TEXT REFERENCES roster_classes(id)")
    ensure_column(conn, "exam_marking_codes", "identity_issue", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "exam_marking_codes", "source_identity_code", "TEXT NOT NULL DEFAULT ''")
    # Structural migration only. Legacy students keep NULL class_id and an
    # empty identity_code; reads and restarts must never infer new identity.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_roster_identity_code ON roster_students(class_id,identity_code) WHERE identity_code<>''")


def import_preview(conn, records):
    counts=Counter((r['class_name'],r['student_no'],r['student_name']) for r in records)
    accepted=[];duplicate=missing=conflict=existing=0
    for r in records:
        if not r['student_name']:missing+=1;continue
        if counts[(r['class_name'],r['student_no'],r['student_name'])]>1:duplicate+=1;continue
        candidates=conn.execute('SELECT student_name FROM roster_students WHERE class_name=? AND student_no=?',(r['class_name'],r['student_no'])).fetchall() if r['student_no'] else conn.execute('SELECT student_name FROM roster_students WHERE class_name=? AND student_no="" AND student_name=?',(r['class_name'],r['student_name'])).fetchall()
        if candidates:
            if len(candidates)>1 or candidates[0][0]!=r['student_name']:conflict+=1
            else:existing+=1
        else:accepted.append(r)
    return dict(new_count=len(accepted),duplicate_count=duplicate,missing_count=missing,conflict_count=conflict,existing_count=existing,records=accepted)


def save_student(conn, payload, now):
    student_id = str(payload.get("student_id") or "")
    name = str(payload.get("display_name") or "").strip()
    if not name or len(name) > 100:
        raise ValueError("请填写学生姓名（最多100字）")
    code = str(payload.get("student_no") or "").strip()
    if len(code)>100:raise ValueError("学号最多100字")
    old = conn.execute("SELECT * FROM roster_students WHERE id=?", (student_id,)).fetchone() if student_id else None
    if student_id and not old:
        raise ValueError("学生记录不存在")
    if old:
        class_id, class_name = old["class_id"], old["class_name"]
        if not class_id:
            # This is an explicit teacher edit, never a migration/read side effect.
            class_id = ensure_class(conn, class_name, now)
    else:
        class_name = str(payload.get("class_name") or "").strip()
        class_id = ensure_class(conn, class_name, now)
    if old:
        conn.execute("UPDATE roster_students SET class_id=?,student_no=?,student_name=?,enabled=? WHERE id=?", (class_id,code,name,int(payload.get('enabled',True)),student_id))
    else:
        student_id=uuid.uuid4().hex
        conn.execute("INSERT INTO roster_students (id,class_id,class_name,student_no,student_name,enabled,created_at) VALUES (?,?,?,?,?,1,?)",(student_id,class_id,class_name,code,name,now))
    return student_id
