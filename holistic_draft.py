"""Teacher-authored level rubric configuration, with conservative legacy handling."""
import math


def validate(config, maximum):
    if isinstance(config,dict) and config.get("aggregation")=="sum_projects":
        from analytic_draft import validate as validate_projects
        return validate_projects(config,maximum)
    def number(value):
        try:
            if isinstance(value, bool):
                raise ValueError('无效分值')
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('分档范围和满分必须填写有效数字') from exc
    maximum = number(maximum)
    if not math.isfinite(maximum) or maximum <= 0 or not isinstance(config, dict):
        raise ValueError('分档评分需要有效满分及评分标准')
    levels = config.get('levels')
    if not isinstance(levels, list) or not levels or len(levels) > 30:
        raise ValueError('请填写1至30个评分档次')
    cleaned = []
    for level in levels:
        if not isinstance(level, dict):
            raise ValueError('评分档次格式无效')
        lo, hi = number(level.get('min_score')), number(level.get('max_score'))
        text = str(level.get('description', '')).strip()
        if not text or not all(math.isfinite(x) for x in (lo, hi)) or not 0 <= lo <= hi <= maximum:
            raise ValueError('分档范围须在0至满分之间，且每档须有评价标准')
        candidate=1
        existing={str(v.get('id')) for v in levels if isinstance(v,dict) and v.get('id')}|{x['id'] for x in cleaned}
        while 'L'+str(candidate) in existing:candidate+=1
        cleaned.append(dict(id=str(level.get('id') or 'L'+str(candidate)),min_score=lo, max_score=hi, description=text))
    if len({x['id'] for x in cleaned})!=len(cleaned):raise ValueError('档次ID重复')
    step=config.get('score_step')
    if step is not None:
        step=number(step)
        if not math.isfinite(step) or step<=0:raise ValueError('评分步长必须是有限正数')
        from decimal import Decimal
        if any(Decimal(str(x[k]))%Decimal(str(step))!=0 for x in cleaned for k in ('min_score','max_score')):raise ValueError('档次边界不符合评分步长')
    ordered = sorted(cleaned, key=lambda x: x['min_score'])
    if ordered[0]['min_score'] != 0 or ordered[-1]['max_score'] != maximum:
        raise ValueError('分档范围须覆盖0分和满分')
    for a, b in zip(ordered, ordered[1:]):
        if b['min_score'] <= a['max_score']:
            raise ValueError('评分档次不能重叠')
        if b['min_score'] - a['max_score'] > (step or 1):
            raise ValueError('整数分档之间不能缺档')
    import re
    dimensions=str(config.get('dimensions','')).strip()
    definitions=config.get('dimension_definitions')
    if definitions is None:
        definitions=[dict(id='D'+str(i+1),label=text.strip()) for i,text in enumerate(re.split(r'[、；;\n]+',dimensions)) if text.strip()]
    if not isinstance(definitions,list) or len(definitions)>30 or any(not isinstance(d,dict) or not str(d.get('id','')).strip() or not str(d.get('label','')).strip() for d in definitions) or len({d['id'] for d in definitions})!=len(definitions):raise ValueError('评价维度ID或说明无效')
    return dict(levels=cleaned, dimensions=dimensions,dimension_definitions=definitions,score_step=step,
                notes=str(config.get('notes', '')).strip(), scoring_version='holistic-essay-grading-v1',
                integration_status='ready' if step is not None and definitions else 'awaiting_configuration')



def simulate(config, maximum, level_index, score):
    """Explicit simulated result only; never a formal grade or a provider call."""
    rubric = validate(config, maximum)
    if type(level_index) is not int or not 0 <= level_index < len(rubric['levels']):
        raise ValueError('无效档次')
    level = rubric['levels'][level_index]
    score = float(score)
    if not math.isfinite(score) or not level['min_score'] <= score <= level['max_score']:
        raise ValueError('模拟得分不在所选档次内')
    return dict(scoring_mode='holistic', level_index=level_index, suggested_total=score,
                max_total=maximum, level=level, simulation_model=True, blocked=True,
                summary='模拟分档结果，仅用于配置验收，不能作为正式成绩')


def require_supported(questions):
    from holistic_scoring import ready
    for question in questions:
        if question.get('scoring_mode')=='holistic':ready(question)
