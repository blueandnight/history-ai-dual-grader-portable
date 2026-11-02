"""Execute the actual UI formatter; missing model output must not be invented."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class BatchScoreFormatTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required to execute browser formatting')
    def test_two_sides_scope_identity_and_missing_results(self):
        source = Path(__file__).resolve().parents[1] / 'static' / 'app.js'
        js = r'''
const fs = require('node:fs'), vm = require('node:vm');
const text = fs.readFileSync(process.argv[1], 'utf8');
const start = text.indexOf('function batchScoreSummary('), end = text.indexOf('function renderBatchRow(', start);
const escapeHtml = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const ctx = {escapeHtml}; vm.createContext(ctx); vm.runInContext(text.slice(start, end), ctx);
const names = {A:'DS', B:'MiMo'};
const q = (id, label, score) => ({sub_question_id:id, label, suggested_total:score, max_total:8});
const root = {suggested_total:4, max_total:8, sub_questions:[q('Q1','1',4)],
 dual_review:{results:{A:{suggested_total:4,sub_questions:[q('Q1','renamed',4)]},B:{suggested_total:2,sub_questions:[q('Q1','1',2)]}}}};
const show = r => ({total:ctx.batchScoreSummary(r,names), subs:ctx.batchSubScoreSummary(r,names)});
const missing = structuredClone(root);delete missing.dual_review.results.B;
const degraded = structuredClone(root);degraded.dual_review.status='degraded';
const zero = structuredClone(root);zero.dual_review.results.A.sub_questions[0].suggested_total=0;
zero.dual_review.results.B.sub_questions[0].suggested_total=0;
const hostile = structuredClone(root);hostile.sub_questions[0].label='<img src=x>';
const sameLabelWrongId = structuredClone(root);sameLabelWrongId.dual_review.results.B.sub_questions[0].sub_question_id='Q2';
console.log(JSON.stringify({root:show(root), missing:show(missing), degraded:show(degraded), zero:show(zero),hostile:show(hostile),
 wrongId:show(sameLabelWrongId), single:show({suggested_total:1.5,max_total:2,sub_questions:[{label:'1',suggested_total:1.5,max_total:2}]})}));
'''
        completed = subprocess.run(['node', '-e', js, str(source)], capture_output=True, text=True,
                                   encoding='utf-8', check=True, timeout=20)
        data = json.loads(completed.stdout)
        self.assertEqual(data['root']['total'], 'DS 4 · MiMo 2 / 8')
        self.assertIn('<th>DS</th><th>MiMo</th><th>差异</th>', data['root']['subs'])
        self.assertIn('<td>4 / 8</td><td>2 / 8</td>', data['root']['subs'])
        self.assertIn('相差 2 分', data['root']['subs'])
        self.assertIn('<td>4 / 8</td><td>— / 8</td>', data['missing']['subs'])
        self.assertEqual(data['missing']['total'], 'DS 4 · MiMo — / 8')
        self.assertIn('<td>4 / 8</td><td>— / 8</td>', data['wrongId']['subs'])
        self.assertEqual(data['degraded']['total'], '双评缺侧 · 建议分 — / 8')
        self.assertIn('<td>— / 8</td><td>— / 8</td>', data['degraded']['subs'])
        self.assertIn('<td>0 / 8</td><td>0 / 8</td>', data['zero']['subs'])
        self.assertIn('一致', data['zero']['subs'])
        self.assertEqual(data['single']['total'], '1.5 / 2')
        self.assertIn('&lt;img src=x&gt;', data['hostile']['subs'])
