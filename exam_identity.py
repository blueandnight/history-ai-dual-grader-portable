"""Storage compatibility for retired identity-code tables; no active identity workflow."""
def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS exam_answer_identities (
        side TEXT NOT NULL, submission_id TEXT NOT NULL, exam_id TEXT NOT NULL,
        identity_code TEXT NOT NULL DEFAULT '' COLLATE BINARY, status TEXT NOT NULL,
        source TEXT NOT NULL, evidence_json TEXT NOT NULL, revision TEXT NOT NULL,
        updated_at TEXT NOT NULL, actor TEXT, PRIMARY KEY(side,submission_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_exam_identity_exact ON exam_answer_identities(exam_id,identity_code COLLATE BINARY)')
    conn.execute('''CREATE TABLE IF NOT EXISTS exam_identity_events (
        id TEXT PRIMARY KEY, exam_id TEXT NOT NULL, side TEXT NOT NULL,
        submission_id TEXT NOT NULL, old_json TEXT, new_json TEXT NOT NULL,
        actor TEXT, created_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS exam_identity_profiles (
        exam_id TEXT PRIMARY KEY, config_json TEXT NOT NULL, actor TEXT, updated_at TEXT NOT NULL)''')
