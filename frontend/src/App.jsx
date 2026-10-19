import React, { useEffect, useState } from 'react'
import ChatPage from './pages/ChatPage.jsx'
import ActivityPage from './pages/ActivityPage.jsx'
import ConfigPage from './pages/ConfigPage.jsx'
import SkillsPage from './pages/SkillsPage.jsx'
import { api } from './api.js'

const NAV = [
  { key: 'chat', label: '聊天', hint: '和它说话' },
  { key: 'activity', label: '活动', hint: '目标 / 审批 / 流水' },
  { key: 'config', label: '配置', hint: '模型 / 事件源 / 规则' },
  { key: 'skills', label: '技能', hint: '它会积累什么' },
]

function routeName() {
  const h = (window.location.hash || '#/chat').replace(/^#\/?/, '')
  return NAV.some((n) => n.key === h) ? h : 'chat'
}

export default function App() {
  const [route, setRoute] = useState(routeName)
  const [health, setHealth] = useState(null)
  const [fatal, setFatal] = useState('')

  useEffect(() => {
    const onHash = () => setRoute(routeName())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const h = await api.get('/api/health')
        if (alive) setHealth(h)
      } catch (e) {
        if (alive) setFatal(e.message)
      }
    }
    load()
    const id = setInterval(() => {
      if (!document.hidden) load()
    }, 8000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  const go = (key) => {
    window.location.hash = `#/${key}`
    setRoute(key)
  }

  const quit = async () => {
    if (!window.confirm('退出 dots？后台任务会停在当前这一步，下次启动接着跑。')) return
    try {
      await api.post('/api/shutdown')
      setFatal('已退出。可以关闭这个页面了。')
    } catch (e) {
      setFatal(e.message)
    }
  }

  const ready = health?.model_ready
  const current = NAV.find((n) => n.key === route)

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" />
          <div>
            <div className="brand-name">dots</div>
            <div className="brand-sub">主动式智能体</div>
          </div>
        </div>

        <nav className="nav">
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item ${route === item.key ? 'active' : ''}`}
              onClick={() => go(item.key)}
            >
              <span className="nav-label">{item.label}</span>
              <span className="nav-hint">{item.hint}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="status-line">
            <span className={`dot ${ready ? 'ok' : 'warn'}`} />
            <span>{ready ? '模型已就绪' : health?.config_error ? '配置待修复' : '等待配置'}</span>
          </div>
          <div className="micro">v{health?.version || '…'} · 只监听本机</div>
          <button className="ghost-btn" onClick={quit}>退出</button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <div className="eyebrow">dots / {current.key}</div>
            <h1>{current.label}</h1>
          </div>
          <div className="topbar-right">
            {health?.dry_run && <span className="chip warn">dry-run</span>}
            {health?.chat_pending && <span className="chip amber">有动作待批</span>}
            {!ready && !health?.config_error && <span className="chip warn">未配置模型</span>}
            {health?.config_error && <span className="chip danger">配置错误</span>}
          </div>
        </header>

        {fatal && <div className="banner danger">{fatal}</div>}
        {health?.config_error && route !== 'config' && (
          <div className="banner warn">
            {health.config_error}
            <button className="link-btn" onClick={() => go('config')}>去配置</button>
          </div>
        )}

        <div className="page">
          {route === 'chat' && <ChatPage health={health} go={go} />}
          {route === 'activity' && <ActivityPage />}
          {route === 'config' && <ConfigPage onSaved={() => go('config')} />}
          {route === 'skills' && <SkillsPage />}
        </div>
      </main>
    </div>
  )
}
