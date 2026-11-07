"""Additive project rubrics for open responses; never turns samples into mandatory answers."""
import re, math
VERSION='analytic-project-grading-v1'

def validate(config,maximum):
    from holistic_draft import validate as levels_validate
    projects=config.get('projects')
    if not isinstance(projects,list) or not projects:raise ValueError('请填写评价项目')
    output=[]
    for i,p in enumerate(projects):
        maximum_i=p.get('max_score');step=p.get('score_step')
        c=levels_validate(dict(levels=p.get('levels'),score_step=step,dimensions=p.get('label','')),maximum_i)
        if c['integration_status']!='ready':raise ValueError('项目标准或评分步长不完整')
        output.append(dict(id=p.get('id') or 'P'+str(i+1),label=p['label'],max_score=float(maximum_i),score_step=c['score_step'],levels=c['levels']))
    if len({p['id'] for p in output})!=len(output) or not math.isclose(sum(p['max_score'] for p in output),float(maximum)):raise ValueError('项目编号重复或项目满分合计不符')
    return dict(aggregation='sum_projects',projects=output,levels=[],dimensions='、'.join(p['label'] for p in output),dimension_definitions=[dict(id=p['id'],label=p['label']) for p in output],score_step=None,notes=config.get('notes',''),scoring_version=VERSION,integration_status='ready')

def extract(answer,maximum):
    # Explicitly named essay components repeated consistently across reference examples.
    components={}
    for label,score in re.findall(r'(?:^|\n)(所选角度|论题|论述|结论)[：:][^\n]*?[（(](\d+(?:\.\d+)?)\s*分[）)]',answer):
        components.setdefault(label,set()).add(float(score))
    projects=[]
    if len(components)>=3 and all(len(v)==1 for v in components.values()) and sum(next(iter(v)) for v in components.values())==maximum:
        for label,values in components.items():
            maximum_i=next(iter(values));projects.append(dict(id='P'+str(len(projects)+1),label=label,max_score=maximum_i,score_step=1,levels=[dict(id='range',min_score=0,max_score=maximum_i,description='依据原卷本项目要求评价'+label+'；参考示例不构成唯一答案。')]))
    else:
        # Only use the actual scoring section, never numbered sample prose.
        section=answer[answer.find('评分说明'): ] if '评分说明' in answer else ''
        theme=re.search(r'（1）展览主题[：:]?(.*?)（2）序言或结束语',section,re.S)
        chunks=[]
        if theme:
            choices=re.findall(r'[①②③]\s*(.*?)[（(](\d+)\s*分[）)]',theme.group(1),re.S)
            chunks.append(('展览主题',[(float(n),t.strip()) for t,n in choices]))
            rest=section[theme.end():]
            for block in re.split(r'[①②③]',rest):
                head=re.match(r'\s*(.*?)[（(](\d+)\s*分[）)]',block)
                if not head:continue
                scores=re.findall(r'(?:^|\n)\s*(\d+)\s*分[：:]\s*([^\n]+)',block)
                chunks.append((head.group(1).strip(),[(float(n),t.strip()) for n,t in scores]))
        for label,scores in chunks:
            if not scores:continue
            maximum_i=max(n for n,t in scores)
            projects.append(dict(id='P'+str(len(projects)+1),label=label,max_score=maximum_i,score_step=1,levels=[dict(id='S'+str(int(n)),min_score=n,max_score=n,description=t) for n,t in scores]))
    if projects:
        try:return validate(dict(aggregation='sum_projects',projects=projects,notes='从原卷项目分值提取，请核对；范例保留为非唯一参考。'),maximum)
        except (ValueError,TypeError):pass
    return None
