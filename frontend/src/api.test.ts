import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { checkSession, getSession, isOperator, login, logout, request, setSession } from './api'
import { routeFromHash } from './navigation'

const origin = 'http://127.0.0.1:8765'
beforeEach(() => {
  setSession(null)
  vi.stubGlobal('window', { location: { origin }, dispatchEvent: vi.fn() })
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => vi.unstubAllGlobals())
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
describe('existing session contract', () => {
  it('does not announce expiry when an initial session check is unauthenticated', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ authenticated: false }, 401))
    await expect(checkSession()).rejects.toThrow()
    expect(getSession()).toBeNull()
    expect(window.dispatchEvent).not.toHaveBeenCalled()
  })
  it('checks session before granting a role and uses only same-origin cookies', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ required: true, authenticated: true, role: 'teacher', csrf_token: 'synthetic' }))
    await checkSession()
    expect(getSession()?.role).toBe('teacher')
    expect(isOperator(getSession())).toBe(false)
    expect(fetch).toHaveBeenCalledWith('/api/auth/session', expect.objectContaining({ credentials: 'same-origin', cache: 'no-store', redirect: 'error' }))
  })
  it('sends CSRF on writes and fails closed without it', async () => {
    setSession({ required: true, authenticated: true, role: 'teacher' })
    await expect(logout()).rejects.toThrow('重新登录')
    expect(fetch).not.toHaveBeenCalled()
    setSession({ required: true, authenticated: true, csrf_token: 'synthetic' })
    vi.mocked(fetch).mockResolvedValue(response({ ok: true }))
    await logout()
    expect(fetch).toHaveBeenCalledWith('/api/auth/logout', expect.objectContaining({ headers: expect.objectContaining({ 'X-CSRF-Token': 'synthetic' }) }))
    expect(getSession()).toBeNull()
  })
  it('login never grants access until session is independently checked', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response({ ok: true })).mockResolvedValueOnce(response({ authenticated: false }, 401))
    await expect(login('synthetic', 'fake-password')).rejects.toThrow()
    expect(getSession()).toBeNull()
  })
  it('clears an expired session without exposing provider/server text', async () => {
    setSession({ required: true, authenticated: true, csrf_token: 'synthetic' })
    vi.mocked(fetch).mockResolvedValue(response({ error: 'RAW_PRIVATE_SERVER_MESSAGE' }, 401))
    await expect(request('/api/assignments')).rejects.toThrow('登录已失效')
    expect(getSession()).toBeNull()
    expect(window.dispatchEvent).toHaveBeenCalled()
  })
  it('does not claim logout succeeded on network failure', async () => {
    const s = { required: true, authenticated: true, csrf_token: 'synthetic' }
    setSession(s); vi.mocked(fetch).mockRejectedValue(new Error('offline'))
    await expect(logout()).rejects.toThrow('offline')
    expect(getSession()).toEqual(s)
  })
  it('rejects cross-origin and invalid API paths', async () => {
    for (const path of ['https://bad.test/api', '//bad.test/api', '/static/x', '/api/\\bad']) await expect(request(path)).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })
  it('teacher hash manipulation cannot select admin navigation', () => {
    const teacher = { required: true, authenticated: true, role: 'teacher' as const }
    expect(routeFromHash('#/system', teacher)).toBe('system');expect(isOperator(teacher)).toBe(false)
    expect(routeFromHash('#/history', teacher)).toBe('history')
    expect(routeFromHash('#//bad.test', teacher)).toBe('home')
    expect(routeFromHash('#/system', { ...teacher, role: 'admin' })).toBe('system')
    expect(isOperator({ required: false, authenticated: true })).toBe(true)
  })
})

it('uses the existing same-origin and CSRF transport for objective link APIs',async()=>{
 setSession({required:true,authenticated:true,role:'teacher',csrf_token:'synthetic'})
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>({links:[]})}))
 await request('/objective/api/answer-links','POST',{exam_id:'synthetic'})
 expect(fetch).toHaveBeenCalledWith('/objective/api/answer-links',expect.objectContaining({credentials:'same-origin',headers:expect.objectContaining({'X-CSRF-Token':'synthetic'})}))
})
