"""Copy exam configuration only; never copy grading or student records."""
import copy
import json
import shutil
import uuid
from contextlib import closing


def duplicate(app, assignment_id, name):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ValueError('请填写新考试名称（最多200字）')
    if not isinstance(assignment_id, str) or not assignment_id:
        raise ValueError('请选择原考试')
    new_assignment, new_exam = uuid.uuid4().hex[:12], uuid.uuid4().hex
    created_at = app.utc_now()
    new_dir = app.DATA_DIR / new_assignment
    asset_root = app.DATA_DIR / 'exam_assets' / new_exam
    warnings = []
    with closing(app.get_db()) as conn:
        try:
            conn.execute('BEGIN IMMEDIATE')
            original = conn.execute('SELECT * FROM assignments WHERE id=?', (assignment_id,)).fetchone()
            if not original:
                raise ValueError('原考试不存在或已删除')
            meta = copy.deepcopy(app._json_object_or_empty(original['import_metadata_json']))
            source = meta.get('source') if isinstance(meta.get('source'), dict) else {}
            old_layout = source.get('layout_source')
            replacements = {}
            refs = []
            if isinstance(old_layout, dict) and old_layout.get('stored_path'):
                try:
                    assets = app.persist_exam_pdf_assets(
                        portable_root=app.PORTABLE_ROOT, exam_id=new_exam,
                        source_pdf_path=app.resolve_data_relative_path(old_layout['stored_path']),
                        layout_source=old_layout, layout_reference=meta.get('layout_reference', {}),
                        reference_path_resolver=app.resolve_data_relative_path)
                    replacements[old_layout['stored_path']] = assets['layout_source']['stored_path']
                    replacements[assets['layout_source']['stored_path']] = assets['layout_source']['stored_path']
                    source['layout_source'] = assets['layout_source']
                    refs = assets['references']
                    meta.setdefault('layout_reference', {})['pages'] = refs
                except (ValueError, OSError):
                    source.pop('layout_source', None)
                    meta.pop('layout_reference', None)
                    warnings.append('原版式资源缺失或校验未通过，已复制试题与评分标准；自动裁切需重新配置。')
            elif old_layout:
                source.pop('layout_source', None)
                meta.pop('layout_reference', None)
                warnings.append('原考试没有可复制的有效版式资源，已复制试题与评分标准。')

            # Only declared import documents and validated PDF references may be
            # copied. No directory copy, uploads, thumbnails or grading data.
            def relocate(value):
                if isinstance(value, list):
                    return [relocate(v) for v in value]
                if not isinstance(value, dict):
                    return value
                result = {}
                for key, item in value.items():
                    if key == 'stored_path' and isinstance(item, str):
                        if item in replacements:
                            result[key] = replacements[item]
                        elif item.endswith('.docx'):
                            try:
                                path = app.resolve_data_relative_path(item)
                                if path.is_file():
                                    target = new_dir / 'imports' / ('source-' + str(len(replacements)) + '.docx')
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copyfile(path, target)
                                    replacements[item] = target.relative_to(app.PORTABLE_ROOT).as_posix()
                                    result[key] = replacements[item]
                            except (ValueError, OSError):
                                pass
                        # Unavailable source attachments do not affect saved
                        # questions. Never keep a path into the original exam.
                    else:
                        result[key] = relocate(item)
                return result
            if not refs:
                meta.pop('layout_reference', None)
            meta = relocate(meta)
            meta.pop('import_draft_id', None)
            if meta:
                meta.update(title=name.strip(), created_at=created_at)
            profile = None
            for row in conn.execute('SELECT id FROM grading_exams WHERE assignment_id=? ORDER BY (layout_profile_id IS NOT NULL) DESC,created_at DESC,id', (assignment_id,)):
                context = app._exam_layout_profile_context(conn, row['id'])
                if refs and context and context.get('layout_profile') and not any(context.get(k) for k in ('profile_invalid', 'profile_hash_mismatch', 'pdf_hash_mismatch')):
                    from exam_layout_profile_service import build_exam_layout_profile
                    profile = build_exam_layout_profile(
                        exam_id=new_exam, assignment_id=new_assignment,
                        source_pdf_sha256=context['source_pdf_sha256'], references=refs,
                        required_question_ids=context['subjective_question_ids'],
                        objective_question_ids=context['objective_question_ids'],
                        regions=context['layout_profile']['regions'], layout_profile_id=uuid.uuid4().hex,
                        created_at=created_at, updated_at=created_at)
                    break
            conn.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at,import_metadata_json) VALUES(?,?,?,?,?,?,?,?)',
                         (new_assignment,name.strip(),original['question'],original['standard_answer'],original['criteria_json'],original['sub_questions_json'],created_at,json.dumps(meta,ensure_ascii=False)))
            conn.execute('INSERT INTO grading_exams(id,assignment_id,created_at,layout_profile_id,layout_profile_json) VALUES(?,?,?,?,?)',
                         (new_exam,new_assignment,created_at,profile['layout_profile_id'] if profile else None,json.dumps(profile or {},ensure_ascii=False)))
            app.append_audit_event(conn,'exam_copied','grading_exam',new_exam,{'assignment_id':new_assignment,'source_assignment_id':assignment_id,'layout_copied':bool(profile)})
            conn.commit()
        except Exception:
            conn.rollback()
            # These paths contain freshly generated IDs and must remain in the
            # intended data directories; never clean any original exam files.
            for target, parent in ((new_dir,app.DATA_DIR),(asset_root,app.DATA_DIR/'exam_assets')):
                if target.resolve().parent == parent.resolve() and target.exists():
                    shutil.rmtree(target)
            raise
    return {'assignment':{'id':new_assignment,'name':name.strip()},'exam_id':new_exam,'layout_copied':bool(profile),'warnings':warnings}
