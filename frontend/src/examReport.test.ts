import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { setSession } from './api'
import { fetchExamReport, fetchExamReportAI, reportMarkdown, reportNumber } from './examReport'
beforeEach(() => {
  vi.stubGlobal('window', { location: { origin: 'http://127.0.0.1:8765' } })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ report: { assignment_id: 'a'.repeat(12) } }))))
  setSession({ required: true, authenticated: true, csrf_token: 'SYNTHETIC' })
})
afterEach(() => vi.unstubAllGlobals())
it('reads local aggregates without an AI call and includes CSRF', async () => {
  await fetchExamReport('a'.repeat(12), 'e'.repeat(32))
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch).toHaveBeenCalledWith('/api/exam-report', expect.objectContaining({ headers: expect.objectContaining({ 'X-CSRF-Token': 'SYNTHETIC' }), body: JSON.stringify({ assignment_id: 'a'.repeat(12), exam_id: 'e'.repeat(32) }) }))
})
it('explicit AI action includes consent and objectives', async () => {
  await fetchExamReportAI('a'.repeat(12), '', '历史教学目标')
  expect(fetch).toHaveBeenCalledWith('/api/exam-report/ai', expect.objectContaining({ body: JSON.stringify({ assignment_id: 'a'.repeat(12), objectives: '历史教学目标', confirm_send: true }) }))
})
it('exports local report without AI and retains mixed-scope warnings and zero scores', () => {
  const text = reportMarkdown({ assignment_id: 'a'.repeat(12), assignment_name: '合成卷', average: 0, max_total: null, mixed_scopes: true, warnings: ['历史教师逐题终评缺失'], questions: [{ label: '1|x', average: 0 }] })
  expect(text).toContain('平均分：0'); expect(text).toContain('满分：—'); expect(text).toContain('历史教师逐题终评缺失'); expect(text).toContain('不同评分范围'); expect(text).toContain('1\\|x')
  expect(reportNumber(Infinity)).toBe('—'); expect(reportNumber(0)).toBe('0')
})
