export type Session = {
  required: boolean; authenticated: boolean; role?: 'admin' | 'teacher';
  username?: string; csrf_token?: string; configured?: boolean
  cloud_mode?: boolean
}
export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}
// Session and CSRF are memory-only. Never copy cookies/tokens to browser storage.
let current: Session | null = null
export function setSession(value: Session | null) { current = value }
export function getSession() { return current }
export function isOperator(s: Session | null) {
  return !!s?.authenticated && (s.required === false || s.role === 'admin')
}
export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (!(path.startsWith('/api/') || path.startsWith('/objective/api/')) || path.startsWith('//') || /[\\\r\n]/.test(path)) throw new Error('无效请求路径')
  const url = new URL(path, window.location.origin)
  if (url.origin !== window.location.origin) throw new Error('仅允许同源请求')
  const write = method !== 'GET' && method !== 'HEAD'
  const multipart = typeof FormData !== 'undefined' && body instanceof FormData
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (write) {
    if (!multipart) headers['Content-Type'] = 'application/json'
    if (path !== '/api/auth/login' && current?.required) {
      if (!current.authenticated || !current.csrf_token) throw new Error('请重新登录后再操作')
      headers['X-CSRF-Token'] = current.csrf_token
    }
  }
  const response = await fetch(url.pathname + url.search, {
    method, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers,
    ...(write ? { body: multipart ? body as FormData : JSON.stringify(body ?? {}) } : {}),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/auth/login') {
      const wasAuthenticated = current?.authenticated === true
      current = null
      if (wasAuthenticated) window.dispatchEvent(new Event('session-expired'))
    }
    // Never include raw response bodies, stack traces or credentials in errors.
    throw new ApiError(response.status, response.status === 401 ? '登录已失效，请重新登录' :
      response.status === 403 ? '没有此操作的权限，请刷新或重新登录' : '请求未完成，请稍后重试')
  }
  return data as T
}
export async function checkSession() {
  const s = await request<Session>('/api/auth/session')
  if (!s.authenticated) throw new ApiError(401, '请先登录')
  setSession(s); return s
}
export async function login(username: string, password: string) {
  await request('/api/auth/login', 'POST', { username, password })
  return checkSession()
}
export async function logout() {
  // Do not claim logout succeeded on network/CSRF failure.
  await request('/api/auth/logout', 'POST', {})
  setSession(null)
}
