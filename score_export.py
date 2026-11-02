"""Small value-only grade exports. No formulas or inferred missing scores."""
import csv
import io
import zipfile
from xml.sax.saxutils import escape


METHODS = {'automatic_blank_zero':'明确空白自动0分','teacher_total_confirmation':'教师总分终评','teacher_batch_adoption':'教师批量采纳共识','teacher_point_review':'历史逐点终评'}


def rows(projection, title, include_incomplete=True, *, side='all'):
    if side not in ('all','objective','subjective'):
        raise ValueError('导出题型无效')
    if side != 'all':
        name='客观题' if side=='objective' else '主观题'
        data=[['考试ID','考试名称','匿名答卷编号','班级','姓名','座号/学号',name+'得分',name+'满分','答案键版本 / 确认方式','成绩范围状态','关联状态','答卷ID']]
        seen=set()
        for row in projection['rows']:
            record=row.get(side)
            if not record or record['id'] in seen or (not include_incomplete and not (record.get('valid') and record.get('complete'))):continue
            seen.add(record['id'])
            version=record.get('answer_version') if side=='objective' else METHODS.get(row['confirmation_method'],row['confirmation_method'])
            # Only reliable attribution may carry roster names; anonymous scores
            # are still exportable when association or the other engine is pending.
            data.append([projection['exam_id'],title,record.get('anonymous_code') or record['id'],
                         row['class_name'],row['student_name'],row['student_no'],record['score'] if record.get('valid') else None,
                         record['max_score'],version,record['reason'],row.get('association_status','未关联'),record['id']])
        return data
    data = [['考试ID','考试名称','匿名答卷编号','班级','姓名','座号/学号','客观题得分','客观题满分','主观题确认分','主观题满分','整卷总分','整卷满分','确认方式','完整性状态','答卷编号','关联状态','主观题AI-A参考分','主观题AI-B参考分','AI参考范围说明','主观题评分范围']]
    for r in projection['rows']:
        if not include_incomplete and not r['complete']:
            continue
        o,s=r['objective'],r['subjective']
        data.append([projection['exam_id'],title,r['anonymous_code'],r['class_name'],r['student_name'],r['student_no'],
                     o['score'] if o and o['valid'] else None,o['max_score'] if o else None,
                     s['score'] if s and s['valid'] else None,s['max_score'] if s else None,
                     r['total_score'],r['total_max'],METHODS.get(r['confirmation_method'],r['confirmation_method']),r['status'],r['anonymous_code'],r.get('association_status','未关联'),(s.get('ai_reference') or {}).get('A') if s else None,(s.get('ai_reference') or {}).get('B') if s else None,'；'.join(k+'：'+v for k,v in s.get('ai_reference_state',{}).items()) if s else '', '、'.join(str(v) for v in s.get('question_ids',[])) if s else ''])
    return data


def csv_bytes(data):
    output=io.StringIO(newline='');writer=csv.writer(output)
    for row in data:
        writer.writerow([("'"+v) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')) else v for v in row])
    return output.getvalue().encode('utf-8-sig')


def xlsx_bytes(data, extra_sheets=None, *, sheet_name='统一成绩'):
    def cell(v, col, row):
        label='';col+=1
        while col:col,n=divmod(col-1,26);label=chr(65+n)+label
        ref=label+str(row)
        if v is None:return '<c r="'+ref+'"/>'
        if type(v) in (float,int):return '<c r="'+ref+'"><v>'+str(v)+'</v></c>'
        return '<c r="'+ref+'" t="inlineStr"><is><t xml:space="preserve">'+escape(str(v))+'</t></is></c>'
    sheets=[(sheet_name,data)]+list(extra_sheets or [])
    def worksheet(values):
        return '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join('<row r="'+str(i)+'">'+''.join(cell(v,j,i) for j,v in enumerate(row))+'</row>' for i,row in enumerate(values,1))+'</sheetData></worksheet>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'+''.join('<Override PartName="/xl/worksheets/sheet'+str(i)+'.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))+'</Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        from xml.sax.saxutils import quoteattr
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join('<sheet name='+quoteattr(name)+' sheetId="'+str(i)+'" r:id="rId'+str(i)+'"/>' for i,(name,_) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join('<Relationship Id="rId'+str(i)+'" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet'+str(i)+'.xml"/>' for i in range(1,len(sheets)+1))+'</Relationships>')
        for i,(_,values) in enumerate(sheets,1):
            z.writestr('xl/worksheets/sheet'+str(i)+'.xml',worksheet(values))
    return output.getvalue()


def analysis_sheets(report, scope):
    """The same report projection is used on screen and in the workbook."""
    overview=[['指标','数值或说明'],['成绩表范围',scope],['分析范围',report['scope']],
              ['整卷完整成绩人数',report['complete_count']],['未完整记录数',report['incomplete_count']],
              ['整卷平均分',report['mean']],['整卷中位数',report['median']],['整卷最高分',report['maximum']],['整卷最低分',report['minimum']],
              ['及格率（0—1）',report['pass_rate']],['优秀率（0—1）',report['excellent_rate']],
              ['及格比例阈值',report['pass_ratio']],['优秀比例阈值',report['excellent_ratio']]]
    for key,label in [('objective_summary','客观题'),('subjective_summary','主观题确认')]:
        side=report[key]
        for field,text in [('count','有效样本'),('excluded_count','排除记录'),('mean','平均分'),('maximum','最高分'),('minimum','最低分'),('score_rate','得分率（0—1）')]:
            overview.append([label+text,side[field]])
        overview.extend([label+'分布 '+str(k),v] for k,v in side['distribution'].items())
    objective=[['题号','有效样本数','实际作答人数','空白人数','正确人数','正确率（0—1）','平均得分','得分率（0—1）']]
    objective.extend([k,v['count'],v['answered_count'],v['blank_count'],v['correct'],v['correct_rate'],v['mean'],v['score_rate']] for k,v in report['objective_questions'].items())
    subjective=[['说明',report['subjective_note']],['来源','题号 / 模型','对应来源样本数','平均得分']]
    for source,label in [('subjective_questions','教师确认小题'),('ai_question_diagnostics','AI诊断（非正式）')]:
        subjective.extend([label,k,v['count'],v['mean']] for k,v in report[source].items())
    return [('考试概览',overview),('客观题统计',objective),('主观题统计',subjective)]
