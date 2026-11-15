"""Read-only projection of saved model suggestions for display and evaluation."""
from collections import Counter
from math import isfinite


def score_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isfinite(value) else None


def usable_question_score(question):
    value = score_number(question.get('suggested_total'))
    maximum = score_number(question.get('max_total'))
    if (value is None or maximum is None or not 0 <= value <= maximum or maximum <= 0
            or question.get('blocked') or question.get('block_reasons')
            or any(question.get(flag) is False for flag in ('image_readable', 'region_complete', 'question_matched'))
            or question.get('answer_status') not in ('answered', 'blank_confirmed')):
        return None
    return value


def model_scores(result, model, metadata=None):
    """Keep usable question scores and distinguish complete, partial, and missing."""
    if not isinstance(model, dict) or model.get('mode') != 'vision-model' or model.get('simulation_model') or result.get('simulation_model'):
        return {}, None, 'missing'
    if metadata is not None and not isinstance(metadata, dict):
        return {}, None, 'missing'
    if metadata is not None:
        finish = metadata.get('finish_reason')
        if metadata.get('status') != 'ok' or any(
            reason in ('length', 'content_filter', 'error')
            for reason in (finish if isinstance(finish, list) else [finish])
        ):
            return {}, None, 'missing'
    questions = model.get('sub_questions')
    raw_scope = result.get('grading_scope')
    scope = raw_scope if isinstance(raw_scope, dict) else {}
    if questions is None and metadata is None and not scope and not model.get('blocked'):
        # Older single-model records saved only the model total.
        total, maximum = map(score_number, (model.get('suggested_total'), model.get('max_total')))
        if total is not None and maximum is not None and 0 <= total <= maximum and maximum > 0:
            return {}, total, 'complete'
    if not isinstance(questions, list) or not questions or any(not isinstance(q, dict) for q in questions):
        return {}, None, 'missing'
    if model.get('requires_question_mapping') or model.get('unmapped_answers'):
        return {}, None, 'missing'
    if model.get('blocked') and not any(q.get('blocked') or q.get('block_reasons') for q in questions):
        return {}, None, 'missing'
    ids = [q.get('sub_question_id') for q in questions]
    expected = scope.get('graded_question_ids') or ids
    if not isinstance(expected, list) or any(not isinstance(qid, str) or not qid for qid in expected + ids):
        return {}, None, 'missing'
    counts = Counter(ids)
    scores, maxima = {}, {}
    for q, qid in zip(questions, ids):
        value = usable_question_score(q)
        if qid not in expected or counts[qid] != 1 or value is None:
            continue
        status = q.get('answer_status')
        if status == 'blank_confirmed':
            from blank_consensus import valid
            receipt = (result.get('dual_review') or {}).get('blank_resolution') or {}
            if value != 0 or qid not in (receipt.get('question_ids') or []) or not valid(result):
                continue
        scores[qid], maxima[qid] = value, q['max_total']
    if not scores:
        return {}, None, 'missing'
    scoped_max = score_number(scope.get('scoped_max_score', result.get('max_total')))
    complete = (
        not model.get('blocked')
        and len(expected) == len(scores) == len(questions) and set(expected) == scores.keys()
        and scoped_max is not None and abs(sum(maxima.values()) - scoped_max) < 1e-8
    )
    return scores, sum(scores.values()), 'complete' if complete else 'partial'


def saved_model_scores(result):
    dual = result.get('dual_review')
    if not isinstance(dual, dict):
        return {'A': model_scores(result, result)}
    results, models = dual.get('results'), dual.get('models')
    results = results if isinstance(results, dict) else {}
    models = models if isinstance(models, dict) else {}
    return {side: model_scores(result, results.get(side), models.get(side) or {}) for side in ('A', 'B')}


def primary_model_score(result, projected=None):
    dual = result.get('dual_review')
    side = dual.get('base_side') if isinstance(dual, dict) else 'A'
    scores = saved_model_scores(result) if projected is None else projected
    return scores.get(side or 'A', ({}, None, 'missing'))
