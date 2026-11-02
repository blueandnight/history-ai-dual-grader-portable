"""Pure, local aggregation for exam reports; this module never changes grading data."""
from __future__ import annotations

import copy
import json
import math
from typing import Any


def _load_result(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _score_value(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _question_key(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 1 and text[0].upper() == "Q" and text[1].isdigit():
        return text[1:]
    return text


def _teacher_final(result: dict[str, Any]) -> dict[str, Any]:
    if result.get('confirmation_method') == 'automatic_blank_zero':
        return result.get('automatic_blank_zero') or {}
    if result.get('confirmation_method') == 'teacher_total_confirmation':
        total = result.get('teacher_total_confirmation')
        return total if isinstance(total, dict) else {}
    from batch_adoption import confirmed_final
    adopted = confirmed_final(result)
    if adopted is not None:
        return adopted
    dual = result.get("dual_review")
    if isinstance(dual, dict) and isinstance(dual.get("teacher_final"), dict):
        return dual["teacher_final"]
    value = result.get("teacher_final")
    return value if isinstance(value, dict) else {}


def unified_exam_summary(conn, projection, pass_ratio=.6, excellent_ratio=.85):
    """Summarize the existing authoritative read projection; no new grade store."""
    import statistics
    if not 0 <= pass_ratio <= excellent_ratio <= 1:
        raise ValueError('及格率和优秀率阈值应在0—1之间，优秀阈值不得低于及格阈值')
    rows = projection['rows']; complete = [r for r in rows if r['complete']]
    scores = [r['total_score'] for r in complete]
    distribution = {name: 0 for name in ('未及格','及格','良好','优秀')}
    for r in complete:
        ratio = r['total_score']/r['total_max']
        distribution['优秀' if ratio >= excellent_ratio else '良好' if ratio >= (pass_ratio+excellent_ratio)/2 else '及格' if ratio >= pass_ratio else '未及格'] += 1
    objective, subjective, diagnostics = {}, {}, {}
    # Eligibility is evaluated by each engine's read projection, independently
    # of identity and the other side. Dedupe defensively when links are corrupt.
    eligible = {side: {r[side]['id']: r[side] for r in rows if r.get(side) and r[side].get('complete')}
                for side in ('objective', 'subjective')}
    for sid in eligible['objective']:
        for a in conn.execute('SELECT question_no,final_option,awarded_points,max_points FROM objective_answers WHERE submission_id=?', (sid,)):
            item = objective.setdefault(str(a['question_no']), {'count':0,'answered_count':0,'blank_count':0,'correct':0,'points':0,'maximum':0})
            item['count'] += 1
            item['answered_count'] += a['final_option'] in ('A','B','C','D')
            item['blank_count'] += a['final_option'] not in ('A','B','C','D')
            item['correct'] += a['awarded_points'] == a['max_points']
            item['points'] += a['awarded_points']; item['maximum'] += a['max_points']
    seen = set()
    for r in rows:
        side = r.get('subjective')
        if not side or side['id'] in seen:
            continue
        seen.add(side['id'])
        raw = conn.execute('SELECT result_json FROM submissions WHERE id=?', (side['id'],)).fetchone()
        result = _load_result(raw[0])
        if side.get('valid') and side.get('version_status') == 'verified' and result.get('confirmation_method') != 'teacher_total_confirmation':
            for q in _teacher_final(result).get('question_subtotals', []):
                score = _score_value(q.get('subtotal'))
                if score is not None:
                    subjective.setdefault(q['question_id'], []).append(score)
        # Exactly the same eligibility as the unified AI reference columns.
        from unified_scores import valid_ai_subscores
        source=conn.execute('SELECT a.* FROM assignments a JOIN submissions s ON s.assignment_id=a.id WHERE s.id=?',(side['id'],)).fetchone()
        try:
            saved=json.loads(source['sub_questions_json'] or '[]') if source else []
        except (ValueError,TypeError):
            saved=[]
        for name,values in valid_ai_subscores(result,source,saved,side.get('question_ids',[])).items():
            for qid,value in values.items():
                diagnostics.setdefault(qid+' / AI-'+name,[]).append(value)
    def side_summary(name):
        values = list(eligible[name].values())
        points = [v['score'] for v in values]
        maxima = [v['max_score'] for v in values]
        histogram = {name:0 for name in ('0–59%','60–69%','70–84%','85–100%')}
        for v in values:
            rate = v['score']/v['max_score']
            histogram['85–100%' if rate >= .85 else '70–84%' if rate >= .7 else '60–69%' if rate >= .6 else '0–59%'] += 1
        return dict(count=len(values), excluded_count=len({r[name]['id'] for r in rows if r.get(name)})-len(values),
                    pass_rate=sum(v['score']/v['max_score']>=pass_ratio for v in values)/len(values) if values else None, excellent_rate=sum(v['score']/v['max_score']>=excellent_ratio for v in values)/len(values) if values else None, mean=statistics.mean(points) if points else None, maximum=max(points) if points else None,
                    minimum=min(points) if points else None, distribution=histogram,
                    score_rate=sum(points)/sum(maxima) if sum(maxima) else None)
    return dict(scope='整卷仅统计完整正式成绩；两侧按各自有效评分独立统计，不要求实名或另一侧完成。版本不同、部分范围及待复核记录不混入当前整卷口径。',
                observed_rows=len(rows), complete_count=len(complete), incomplete_count=len(rows)-len(complete),
                participant_count=len(rows) if all(r.get('attribution_valid') for r in rows) else None,
                mean=statistics.mean(scores) if scores else None, median=statistics.median(scores) if scores else None,
                maximum=max(scores) if scores else None, minimum=min(scores) if scores else None,
                pass_ratio=pass_ratio, excellent_ratio=excellent_ratio,
                pass_rate=sum(r['total_score']/r['total_max']>=pass_ratio for r in complete)/len(complete) if complete else None,
                excellent_rate=sum(r['total_score']/r['total_max']>=excellent_ratio for r in complete)/len(complete) if complete else None,
                distribution=distribution, objective_summary=side_summary('objective'), subjective_summary=side_summary('subjective'),
                objective_questions={k:{**v,'correct_rate':v['correct']/v['count'],'mean':v['points']/v['count'],'score_rate':v['points']/v['maximum'] if v['maximum'] else None} for k,v in objective.items()},
                subjective_questions={k:{'count':len(v),'mean':statistics.mean(v)} for k,v in subjective.items()},
                ai_question_diagnostics={k:{'count':len(v),'mean':statistics.mean(v)} for k,v in diagnostics.items()},
                subjective_note='总分终评不生成教师确认的小题分；AI小题数据仅供诊断，不是教师确认的小题成绩')



def _scope_signature(result: dict[str, Any]) -> tuple[tuple[str, ...], float | None]:
    scope = result.get("grading_scope")
    scope = scope if isinstance(scope, dict) else {}
    ids = scope.get("graded_question_ids")
    if not isinstance(ids, list):
        ids = [
            sub.get("sub_question_id") or sub.get("question_id") or sub.get("label")
            for sub in result.get("sub_questions", []) if isinstance(sub, dict)
        ] if isinstance(result.get("sub_questions"), list) else []
    normalized = tuple(sorted({_question_key(value) for value in ids if _question_key(value)}))
    maximum = _score_value(scope.get("scoped_max_score"))
    if maximum is None:
        maximum = _score_value(result.get("max_total"))
    return normalized, maximum


def build_exam_report_data(
    conn: Any,
    assignment_id: str,
    exam_id: str = "",
    batch_id: str = "",
) -> dict[str, Any]:
    assignment = conn.execute(
        "SELECT * FROM assignments WHERE id=?", (assignment_id,)
    ).fetchone()
    if assignment is None:
        raise ValueError("考试不存在")
    clauses = ["assignment_id=?", "state='completed'"]
    params: list[Any] = [assignment_id]
    if exam_id:
        clauses.append("exam_id=?")
        params.append(exam_id)
    if batch_id:
        clauses.append("batch_id=?")
        params.append(batch_id)
    rows = conn.execute(
        "SELECT id, final_total, result_json, confirmed_at, batch_id, exam_id FROM submissions "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at", params
    ).fetchall()

    try:
        saved = json.loads(assignment["sub_questions_json"] or "[]")
    except (TypeError, ValueError):
        saved = []
    questions: list[dict[str, Any]] = []
    by_question_id: dict[str, dict[str, Any]] = {}
    for item in saved if isinstance(saved, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("sub_question_id") or item.get("id") or "").strip()
        qid = _question_key(item.get("sub_question_id") or item.get("id") or label)
        if not label or not qid or qid in by_question_id:
            continue
        target = {
            "label": label, "scores": [], "rates": [], "maxima": set(),
            "unanswered": 0, "criteria": {}, "teacher_scored": 0,
            "ai_scored": 0, "missing_score": 0,
        }
        questions.append(target)
        by_question_id[qid] = target

    total_scores: list[float] = []
    record_rates: list[float] = []
    scope_signatures: set[tuple[tuple[str, ...], float | None]] = set()
    confirmed = unconfirmed = blocked = simulation = reconfirmation = 0
    missing_teacher_totals = 0
    for row in rows:
        result = _load_result(row["result_json"])
        from batch_adoption import current_adoption
        if not current_adoption(result, assignment):
            reconfirmation += 1
            continue
        dual = result.get("dual_review")
        if result.get("blocked") is True or (isinstance(dual, dict) and dual.get("status") == "degraded"):
            blocked += 1
            continue
        if result.get("simulation_model") is True:
            simulation += 1
            continue
        if result.get("teacher_reconfirmation_required") is True:
            reconfirmation += 1
            continue

        is_confirmed = bool(row["confirmed_at"])
        teacher = _teacher_final(result) if is_confirmed else {}
        teacher_subtotals: dict[str, float] = {}
        teacher_points: dict[tuple[str, str], str] = {}
        if teacher:
            for item in teacher.get("question_subtotals", []) or []:
                if not isinstance(item, dict):
                    continue
                key = _question_key(item.get("question_id"))
                value = _score_value(item.get("subtotal"))
                if key and value is not None:
                    teacher_subtotals[key] = value
            for item in teacher.get("point_decisions", []) or []:
                if not isinstance(item, dict):
                    continue
                qid = _question_key(item.get("question_id"))
                cid = str(item.get("rubric_point_id") or "")
                decision = str(item.get("decision") or "")
                if qid and cid and decision in {"full", "none"}:
                    teacher_points[(qid, cid)] = decision

        if is_confirmed:
            confirmed += 1
            total = _score_value(row["final_total"])
            if total is None:
                total = _score_value(teacher.get("final_total"))
            if not teacher.get("question_subtotals"):
                missing_teacher_totals += 1
        else:
            unconfirmed += 1
            # Never promote a stale final_total to a teacher-confirmed result.
            total = _score_value(result.get("suggested_total"))
        if total is None:
            continue
        total_scores.append(total)
        signature = _scope_signature(result)
        scope_signatures.add(signature)
        scope_ids, scope_max = signature
        if scope_max is not None and scope_max > 0:
            record_rates.append(total / scope_max * 100)

        subs = result.get("sub_questions", [])
        for sub in subs if isinstance(subs, list) else []:
            if not isinstance(sub, dict):
                continue
            label = str(sub.get("label") or "").strip()
            qid = _question_key(sub.get("sub_question_id") or sub.get("question_id") or label)
            target = by_question_id.get(qid)
            if target is None:
                if not label or not qid:
                    continue
                target = {"label": label, "scores": [], "rates": [], "maxima": set(),
                          "unanswered": 0, "criteria": {}, "teacher_scored": 0,
                          "ai_scored": 0, "missing_score": 0}
                questions.append(target)
                by_question_id[qid] = target
            max_score = _score_value(sub.get("max_total"))
            if max_score is not None and max_score > 0:
                target["maxima"].add(round(max_score, 8))
            answer_status = str(sub.get("answer_status") or "")
            if answer_status == "unanswered_candidate" and not is_confirmed:
                target["unanswered"] += 1
                continue
            if answer_status in {"uncertain", "missing", "uncertain_candidate", "missing_candidate"} and not is_confirmed:
                target["missing_score"] += 1
                continue

            if is_confirmed:
                qscore = teacher_subtotals.get(qid)
                if qscore is not None:
                    target["scores"].append(qscore)
                    target["teacher_scored"] += 1
                    if max_score and max_score > 0:
                        target["rates"].append(qscore / max_score * 100)
                else:
                    target["missing_score"] += 1
            else:
                qscore = _score_value(sub.get("suggested_total"))
                if qscore is not None:
                    target["scores"].append(qscore)
                    target["ai_scored"] += 1
                    if max_score and max_score > 0:
                        target["rates"].append(qscore / max_score * 100)
                else:
                    target["missing_score"] += 1

            for criterion in sub.get("criteria", []) or []:
                if not isinstance(criterion, dict):
                    continue
                cid = str(criterion.get("criterion_id") or criterion.get("id") or "")
                if not cid:
                    continue
                entry = target["criteria"].setdefault(cid, {
                    "label": str(criterion.get("criterion") or criterion.get("label") or cid),
                    "max": _score_value(criterion.get("max_score")),
                    "attempts": 0, "scored": 0, "credited": 0, "missed": 0, "missing": 0,
                })
                entry["attempts"] += 1
                if is_confirmed:
                    decision = teacher_points.get((qid, cid))
                    if decision is None:
                        entry["missing"] += 1
                        continue
                    entry["scored"] += 1
                    if decision == "full":
                        entry["credited"] += 1
                    else:
                        entry["missed"] += 1
                else:
                    decision = str(criterion.get("decision") or "")
                    point_score = _score_value(criterion.get("suggested_score"))
                    if decision in {"full", "partial"}:
                        entry["scored"] += 1
                        entry["credited"] += 1
                    elif decision == "none":
                        entry["scored"] += 1
                        entry["missed"] += 1
                    elif decision in {"uncertain", "missing", "unresolved", "uncertain_candidate"}:
                        entry["missing"] += 1
                    elif point_score is not None:
                        entry["scored"] += 1
                        if point_score > 0:
                            entry["credited"] += 1
                        else:
                            entry["missed"] += 1
                    else:
                        entry["missing"] += 1

    questions = [item for item in questions if item["scores"] or item["unanswered"] or item["criteria"] or item["missing_score"]]
    count = len(total_scores)
    average = round(sum(total_scores) / count, 2) if count else None
    mixed_scopes = len(scope_signatures) > 1
    common_scope_max = None
    if len(scope_signatures) == 1:
        signature = next(iter(scope_signatures))
        if signature[1] is not None and signature[1] > 0:
            common_scope_max = signature[1]
    max_total = common_scope_max
    score_rate = round(sum(record_rates) / len(record_rates), 1) if record_rates else None
    distribution = {"0-59%": 0, "60-79%": 0, "80-89%": 0, "90-100%": 0}
    for rate in record_rates:
        if rate < 60:
            distribution["0-59%"] += 1
        elif rate < 80:
            distribution["60-79%"] += 1
        elif rate < 90:
            distribution["80-89%"] += 1
        else:
            distribution["90-100%"] += 1

    question_stats = []
    for item in questions:
        scores = item["scores"]
        answered = len(scores)
        avg = round(sum(scores) / answered, 2) if answered else None
        maxima = item["maxima"]
        question_max = next(iter(maxima)) if len(maxima) == 1 else None
        rate = round(sum(item["rates"]) / len(item["rates"]), 1) if item["rates"] else None
        criteria = []
        for criterion in item["criteria"].values():
            scored = criterion["scored"]
            criteria.append({**criterion, "rate": round(criterion["credited"] / scored * 100, 1) if scored else None})
        criteria.sort(key=lambda value: (value.get("rate") if value.get("rate") is not None else 999, value["label"]))
        question_stats.append({
            "label": item["label"], "max": question_max, "mixed_max": len(maxima) > 1,
            "answered": answered, "teacher_scored": item["teacher_scored"], "ai_scored": item["ai_scored"],
            "missing_score": item["missing_score"], "unanswered": item["unanswered"],
            "average": avg, "score_rate": rate, "criteria": criteria,
        })

    common_misses = [
        {"label": criterion["label"], "question": question["label"], "rate": criterion["rate"],
         "missed": criterion["missed"], "missing": criterion["missing"]}
        for question in question_stats for criterion in question["criteria"]
        if criterion["missed"] > 0
    ]
    common_misses.sort(key=lambda value: (-value["missed"], value["rate"] if value["rate"] is not None else 101))
    eligible_count = confirmed + unconfirmed
    warnings = []
    if any(_load_result(row['result_json']).get('confirmation_method') == 'teacher_batch_adoption' for row in rows):
        warnings.append('本报告含教师批量采纳的 AI 共识成绩；批量采纳不代表逐点评阅。')
    if unconfirmed:
        warnings.append("未确认记录按 AI 建议总分和逐题建议分统计，并单独标注数量。")
    if missing_teacher_totals:
        warnings.append("部分历史确认记录缺少逐题教师终评；这些记录不以 AI 分数填补，缺失数已标明。")
    if mixed_scopes:
        warnings.append("统计范围包含不同题目组合或满分，未提供统一满分；总得分率按各份合法分母计算。")
    if any(item["missing_score"] for item in question_stats):
        warnings.append("部分逐题分数缺失或状态不确定，未计为漏答或使用 AI 分数冒充教师终评。")
    if blocked or simulation or reconfirmation:
        warnings.append("blocked、simulation 和待教师重新确认的记录单列在覆盖人数中，不参与成绩统计。")
    if not warnings:
        warnings.append("统计仅使用可识别的教师确认分或未确认 AI 建议分；逐题评分来源已分别标注。")

    return {
        "assignment_id": assignment["id"], "assignment_name": assignment["name"],
        "exam_id": exam_id, "batch_id": batch_id,
        "student_count": count, "confirmed_count": confirmed, "unconfirmed_count": unconfirmed,
        "blocked_count": blocked, "simulation_count": simulation, "reconfirmation_count": reconfirmation,
        "coverage": {"total_count": len(rows), "included_count": count,
                      "confirmed_count": confirmed, "unconfirmed_count": unconfirmed,
                      "blocked_count": blocked, "simulation_count": simulation,
                      "reconfirmation_count": reconfirmation,
                      "missing_total_count": eligible_count - count},
        "max_total": max_total, "mixed_scopes": mixed_scopes,
        "scope": "scoped" if max_total is not None else "whole_or_mixed",
        "average": average, "score_rate": score_rate, "distribution": distribution,
        "questions": question_stats, "common_misses": common_misses[:12],
        "objective": {"available": False, "reason": "当前批次未接入客观题批改数据；本报告只统计主观题评分与评分点。"},
        "warnings": warnings,
    }


def build_ai_report_prompt(data: dict[str, Any], objectives: str) -> list[dict[str, str]]:
    """Pass report aggregates and teacher goals only, excluding record identities and IDs."""
    system = (
        "你是中学历史学科的教学评价分析助手。请基于用户提供的匿名聚合统计写一份“教学评一体化”试卷分析报告，"
        "不要重新评分，不要编造统计中没有的数字或学生信息。报告必须包含：数据口径、总体表现、教学目标达成情况、"
        "共性问题、分层教学建议、后续教学与评价建议。教学目标由用户提供；若用户没有提供，请明确说明只能按题目和评分点推断。"
        "所有结论都要能对应到统计中的题目、得分率或评分点命中率。只输出 Markdown，不要输出代码块。"
    )
    aggregate = {key: copy.deepcopy(data.get(key)) for key in (
        "assignment_name", "student_count", "confirmed_count", "unconfirmed_count", "blocked_count",
        "simulation_count", "reconfirmation_count", "coverage", "max_total", "mixed_scopes", "scope",
        "average", "score_rate", "distribution", "questions", "common_misses", "objective", "warnings",
    ) if key in data}
    # Remove rubric-internal identifiers while retaining aggregate labels and measures.
    for question in aggregate.get("questions", []) or []:
        if isinstance(question, dict):
            for criterion in question.get("criteria", []) or []:
                if isinstance(criterion, dict):
                    criterion.pop("id", None)
                    criterion.pop("criterion_id", None)
    user = (
        "学习目标（教师填写，可为空）：\n" + (str(objectives or "").strip() or "（未填写）") + "\n\n"
        "匿名聚合统计 JSON：\n" + json.dumps(aggregate, ensure_ascii=False, indent=2, allow_nan=False)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
