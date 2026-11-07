"""Validate teacher-edited import structure without touching existing exams."""
import copy
import re


def normalize_majors(majors, originals=None):
    if not isinstance(majors, list) or len(majors) > 100:
        raise ValueError('大题结构无效或数量超过100')
    legacy = {(str(m.get('label', '')), str(q.get('question_id', '')))
              for m in (originals or []) for q in m.get('sub_questions', [])}
    result, seen, parents = [], set(), set()
    for major in majors:
        parent = str(major.get('label', '')).strip()
        if (not re.fullmatch(r'[1-9]\d{0,2}', parent) and not any(p == parent for p, _ in legacy)) or parent in parents:
            raise ValueError('大题编号必须唯一且为1至999的整数')
        parents.add(parent)
        subs = major.get('sub_questions')
        if not isinstance(subs, list) or not subs:
            raise ValueError('每道大题至少保留一个小题')
        item = dict(label=parent, prompt=str(major.get('prompt', '')), sub_questions=[])
        for sub in subs:
            qid = str(sub.get('question_id', '')).strip()
            if (not re.fullmatch(re.escape(parent)+r'\.[1-9]\d{0,2}', qid) and (parent,qid) not in legacy) or qid in seen:
                raise ValueError('小题编号必须唯一，且以所属大题编号开头')
            seen.add(qid)
            kind = sub.get('question_type', 'subjective')
            if kind not in ('subjective', 'essay', 'explanation'):
                raise ValueError('不支持的主观题题型')
            item['sub_questions'].append(dict(copy.deepcopy(sub), question_id=qid,
                parent_question_id=parent, label=qid.split('.')[-1], question_type=kind))
        result.append(item)
    if len(seen) > 300:
        raise ValueError('小题数量超过300')
    return result
