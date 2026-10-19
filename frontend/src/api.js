const TOKEN =
  document.querySelector('meta[name="dots-token"]')?.getAttribute('content') || ''

async function call(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(method !== 'GET' ? { 'X-Dots-Token': TOKEN } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const err = new Error(data?.error?.message || `HTTP ${res.status}`)
    err.code = data?.error?.code
    err.fields = data?.error?.fields || {}
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  get: (path) => call(path),
  post: (path, body = {}) => call(path, { method: 'POST', body }),
  put: (path, body = {}) => call(path, { method: 'PUT', body }),
  del: (path) => call(path, { method: 'DELETE' }),
}

export function fmtNum(n) {
  return Number(n || 0).toLocaleString('zh-CN')
}

export function ago(ts) {
  const d = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (d < 60) return `${d} 秒前`
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`
  return `${Math.floor(d / 86400)} 天前`
}
