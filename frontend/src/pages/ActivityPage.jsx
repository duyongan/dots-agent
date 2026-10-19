import React, { useEffect, useState } from 'react'
import { api, ago, fmtNum } from '../api.js'

const STATUS_LABEL = {
  pending: '待批准',
  ready: '排队中',
  running: '执行中',
  interrupted: '等审批',
  review: '待复核',
  done: '完成',
  failed: '失败',
}

export default function ActivityPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [text, setText] = useState('')
  const [bot, setBot] = useState('dot')
  const [bots, setBots] = useState(['dot'])
  const [busy, setBusy] = useState('')

  const load = async () => {
    try {
      setData(await api.get('/api/dashboard'))
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
    api.get('/api/config')
      .then((c) => setBots(Array.isArray(c.bots) && c.bots.length ? c.bots : ['dot']))
      .catch(() => {})
    const id = setInterval(() => {
      if (!document.hidden) load()
    }, 4000)
    return () => clearInterval(id)
  }, [])

  const decide = async (token, decision) => {
    setBusy(token)
    try {
      await api.post(`/api/approvals/${encodeURIComponent(token)}`, { decision })
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  const addGoal = async (e) => {
    e.preventDefault()
    if (!text.trim()) return
    setBusy('goal')
    try {
      await api.post('/api/goals', { text: text.trim(), bot })
      setText('')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  const pending = data?.pending || []
  const goals = data?.goals || []
  const audit = data?.audit || []

  return (
    <div className="stack">
      {error && <div className="banner danger">{error}</div>}

      <section className="panel">
        <div className="panel-head">
          <h2>新建目标</h2>
          <span className="micro">交给后台，按心跳一轮轮推进</span>
        </div>
        <form className="goal-form" onSubmit={addGoal}>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="例如：把 inbox 里的材料整理成一份周报"
          />
          <select value={bot} onChange={(e) => setBot(e.target.value)}>
            {bots.map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
          <button className="btn primary" disabled={!text.trim() || busy === 'goal'}>创建</button>
        </form>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>待批准</h2>
          <span className="micro">{pending.length} 个动作等你决定</span>
        </div>
        {pending.length === 0 && <div className="empty">没有等着你的动作。</div>}
        <div className="approval-list">
          {pending.map((a) => (
            <div className="approval-card" key={a.token}>
              <div className="approval-head">
                <span className="chip amber">{a.token}</span>
                <span className="mono">{a.tool}</span>
                <span className="micro">目标 {a.goal_id} · {ago(a.created_at)}</span>
              </div>
              <pre>{JSON.stringify(a.args || {}, null, 2)}</pre>
              <div className="approval-actions">
                <button className="btn primary" disabled={busy === a.token}
                        onClick={() => decide(a.token, 'approved')}>批准</button>
                <button className="btn" disabled={busy === a.token}
                        onClick={() => decide(a.token, 'rejected')}>拒绝</button>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>目标</h2>
          <span className="micro">
            今日 {fmtNum(data?.cost?.today)} token · 累计 {fmtNum(data?.cost?.total)}
            {data?.cost?.budget ? ` · 上限 ${fmtNum(data.cost.budget)}` : ''}
          </span>
        </div>
        {goals.length === 0 && <div className="empty">还没有目标。</div>}
        {goals.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>ID</th><th>状态</th><th>bot</th><th>token</th><th>更新</th><th>目标 / 结论</th></tr>
              </thead>
              <tbody>
                {goals.map((g) => (
                  <tr key={g.id}>
                    <td className="mono">{g.id}</td>
                    <td><span className={`status ${g.status}`}>{STATUS_LABEL[g.status] || g.status}</span></td>
                    <td className="mono">{g.bot}</td>
                    <td className="mono">{fmtNum(g.tokens)}</td>
                    <td className="micro">{ago(g.updated_at)}</td>
                    <td>
                      <div>{g.text}</div>
                      {g.summary && <div className="sub">{g.summary}</div>}
                      {g.evidence && <div className="micro">证据：{g.evidence}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>活动流水</h2>
          <span className="micro">最近 {audit.length} 条</span>
        </div>
        {audit.length === 0 && <div className="empty">还没有活动。</div>}
        <div className="feed">
          {[...audit].reverse().map((r, i) => (
            <div className="feed-row" key={i}>
              <span className="mono time">{new Date(r.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</span>
              <span className="mono kind">{r.kind}</span>
              <span className="detail">{r.detail}</span>
              {r.tokens ? <span className="micro">{fmtNum(r.tokens)} tok</span> : null}
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
