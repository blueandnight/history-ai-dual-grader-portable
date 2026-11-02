import { afterEach, expect, it, vi } from 'vitest'
import { exportHref, fetchSubmissions, finalLabel, score } from './history'
afterEach(() => vi.unstubAllGlobals())
it('preserves scoped denominators, zero and decimal scores without recomputing', () => {
  expect(score(0, 8)).toBe('0/8');expect(score(1.5, 4)).toBe('1.5/4')
  expect(score(null, 8)).toBe('—');expect(score(NaN, 8)).toBe('—')
  const s = { id: 'x', state: 'completed', suggested_total: 4, final_total: 0, max_total: 30, grading_scope: { scoped_max_score: 8 }, confirmed_at: 'synthetic' }
  expect(finalLabel(s)).toBe('0/8')
  expect(finalLabel({ ...s, confirmed_at: null })).toBe('尚未确认')
  expect(finalLabel({ ...s, teacher_reconfirmation_required: true })).toBe('待重新确认')
})
it('uses existing export gates and rejects invalid IDs before fetching', async () => {
  vi.stubGlobal('fetch', vi.fn())
  await expect(fetchSubmissions('bad&extra=yes')).rejects.toThrow()
  expect(fetch).not.toHaveBeenCalled()
  expect(exportHref('a&b', false)).toBe('/api/export.csv?assignment_id=a%26b')
  expect(exportHref('abc', true)).toBe('/api/export-anonymous.csv?assignment_id=abc')
})
