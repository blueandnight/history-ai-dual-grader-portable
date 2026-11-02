"""Small synthetic regressions for selectively absorbed safety fixes."""
from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class DatabaseVersionGuardTests(unittest.TestCase):
    def make_database(self, path, version):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, schema_version INTEGER, updated_at TEXT)')
            conn.execute('INSERT INTO schema_meta VALUES (1, ?, ?)', (version, 'synthetic'))
            for name in ('assignments', 'submissions', 'roster_students'):
                conn.execute(f'CREATE TABLE {name} (id TEXT PRIMARY KEY)')
            conn.execute('INSERT INTO assignments VALUES (?)', ('synthetic-preserved',))
            conn.execute('PRAGMA user_version=3')
            conn.commit()

    def test_newer_database_rejected_without_rewriting_or_migration_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / 'history_grading.sqlite3'
            self.make_database(db, app.DB_SCHEMA_VERSION + 1)
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            with patch.object(app, 'DATA_DIR', root), patch.object(app, 'DB_PATH', db), \
                 patch.object(app, '_backup_database_before_migration') as backup:
                with self.assertRaisesRegex(RuntimeError, '高于当前程序'):
                    app.get_db()
                backup.assert_not_called()
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
            # The rejected connection must be closed (also permits Windows cleanup).
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_newer_restore_rejected_without_changing_file(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'backup.sqlite3'
            self.make_database(db, app.DB_SCHEMA_VERSION + 1)
            before = db.read_bytes()
            with self.assertRaisesRegex(ValueError, '高于当前程序'):
                app.validate_restored_database(db)
            self.assertEqual(db.read_bytes(), before)

    def test_current_restore_accepted_and_missing_file_not_created(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'backup.sqlite3'
            self.make_database(db, app.DB_SCHEMA_VERSION)
            app.validate_restored_database(db)
            missing = Path(td) / 'missing.sqlite3'
            with self.assertRaises(ValueError):
                app.validate_restored_database(missing)
            self.assertFalse(missing.exists())


class DualSidePropagationTests(unittest.TestCase):
    def test_either_side_unresolved_or_blocked_propagates_without_changing_scores(self):
        questions = [{'id': 'Q1', 'label': '1', 'question': 'synthetic', 'standard_answer': 'synthetic',
                      'scoring_mode': 'weighted', 'max_score': 2,
                      'criteria': [{'id': 'C1', 'label': 'synthetic', 'score': 2}]}]
        configs = {s: {'provider': s, 'model': 'synthetic'} for s in ('A', 'B')}
        for problem_side, blocked, answer_status in [
            (None, False, 'answered'), ('B', True, 'answered'),
            ('B', False, 'question_unmatched'), ('A', True, 'answered')
        ]:
            with self.subTest(side=problem_side, status=answer_status):
                def model(_assignment, subs, _paths, _meta, config, _regions, diagnostics, **_kwargs):
                    problem = config['provider'] == problem_side
                    diagnostics['input_hash'] = 'identical-synthetic-crop'
                    return {'mode': 'vision-model', 'suggested_total': 0, 'max_total': 2,
                            'blocked': blocked and problem, 'needs_review': False,
                            'sub_questions': [{'sub_question_id': 'Q1', 'label': '1',
                                'answer_status': answer_status if problem else 'answered',
                                'suggested_total': 0, 'max_total': 2, 'criteria': [
                                    {'criterion_id': 'C1', 'decision': 'none', 'suggested_score': 0,
                                     'student_evidence': '', 'needs_review': False}]}]}
                with patch.object(app, 'load_dual_review_model_configs', return_value=configs), \
                     patch.object(app, '_grade_dual_answer_regions_for_side', side_effect=model) as boundary, \
                     patch.object(app, 'open_model_request', side_effect=AssertionError('Real provider forbidden')):
                    result = app.grade_submission_dual({'id': 'a'*12}, questions, '', [Path('synthetic.png')], [],
                        True, [{'invocation_scope': 'answer_region', 'sub_question_ids': ['Q1']}], True, None)
                self.assertEqual(boundary.call_count, 2)
                self.assertEqual(result['suggested_total'], 0)
                self.assertEqual(result['dual_review']['status'], 'complete')
                self.assertEqual(result['blocked'], problem_side is not None)
                if problem_side:
                    self.assertTrue(result['needs_review'])
                    self.assertTrue(any(f'{problem_side} 侧' in r for r in result['block_reasons']))

    def test_dual_only_configuration_still_blocks_bad_image_before_provider(self):
        previous = app.APP_PATHS
        try:
            with tempfile.TemporaryDirectory() as td:
                app.configure_paths(Path(td), Path(td) / 'program')
                photo = app.DATA_DIR / 'synthetic.jpg'
                photo.parent.mkdir(parents=True, exist_ok=True)
                photo.write_bytes(b'synthetic-not-read-by-provider')
                questions = [{'id': 'Q1', 'label': '1', 'question': 'synthetic', 'standard_answer': 'synthetic',
                              'max_score': 2, 'criteria': [{'id': 'C1', 'label': 'synthetic', 'score': 2}]}]
                with closing(app.get_db()) as conn:
                    conn.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)',
                        ('a'*12, 'synthetic', 'synthetic', 'synthetic', '[]', json.dumps(questions), app.utc_now()))
                    conn.execute('INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,privacy_status,ai_privacy_confirmed) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('b'*12, 'a'*12, 'synthetic', '', json.dumps([{'path': 'data/synthetic.jpg',
                            'quality': {'ok': False, 'issues': ['synthetic unreadable']}}]),
                         json.dumps({'dual_review_requested': True}), app.utc_now(), 'queued', 'no_identity_visible', 1))
                    conn.commit()
                with patch.object(app, 'model_configured', return_value=False), \
                     patch.object(app, 'grade_submission', side_effect=AssertionError('Quality gate bypassed')), \
                     patch.object(app, 'open_model_request', side_effect=AssertionError('Real provider forbidden')):
                    app.process_submission('b'*12)
                conn = app.get_db()
                try:
                    result = json.loads(conn.execute('SELECT result_json FROM submissions WHERE id=?', ('b'*12,)).fetchone()[0])
                finally:
                    conn.close()
                self.assertEqual(result['mode'], 'image-quality-error')
                self.assertTrue(result['blocked'])
        finally:
            app._install_paths(previous)
