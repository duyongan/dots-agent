import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

const RULE_VALUES = ['allow', 'ask', 'deny']

function SecretField({ env, label, values, setValues, status }) {
  if (!env) return null
  const set = Boolean(status?.[env])
  return (
    <label className="field">
      <span className="field-label">
        {label}
        <span className={`dot ${set ? 'ok' : 'warn'}`} />
        <span className="micro">{set ? '已设置' : '未设置'}</span>
      </span>
      <input
        type="password"
        autoComplete="new-password"
        placeholder={set ? '已保存，留空则不修改' : '粘贴 API Key'}
        value={values[env] || ''}
        onChange={(e) => setValues({ ...values, [env]: e.target.value })}
      />
      {set && (
        <button type="button" className="link-btn"
                onClick={() => setValues({ ...values, [env]: '' })}>
          清空（保存后生效）
        </button>
      )}
    </label>
  )
}

export default function ConfigPage() {
  const [cfg, setCfg] = useState(null)
  const [presets, setPresets] = useState([])
  const [keys, setKeys] = useState({})
  const [ruleName, setRuleName] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)

  const load = async () => {
    try {
      setCfg(await api.get('/api/config'))
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
    api.get('/api/model-presets').then((d) => setPresets(d.presets || [])).catch(() => {})
  }, [])

  const set = (path, value) => {
    setCfg((prev) => {
      const next = structuredClone(prev)
      const parts = path.split('.')
      let node = next
      for (let i = 0; i < parts.length - 1; i += 1) node = node[parts[i]]
      node[parts[parts.length - 1]] = value
      return next
    })
  }

  const choosePreset = (env) => {
    const p = presets.find((x) => x.env === env)
    if (!p) return
    setCfg((prev) => ({
      ...prev,
      model: {
        ...prev.model,
        name: p.name,
        base_url: p.base_url,
        api_key_env: p.api_key_env,
      },
    }))
  }

  const setRule = (name, value) => {
    setCfg((prev) => ({
      ...prev,
      approval: {
        ...prev.approval,
        rules: { ...prev.approval.rules, [name]: value },
      },
    }))
  }

  const addRule = () => {
    const name = ruleName.trim()
    if (!name) return
    setRule(name, 'ask')
    setRuleName('')
  }

  const save = async () => {
    setSaving(true)
    setNotice('')
    setError('')
    try {
      const secrets = Object.fromEntries(
        Object.entries(keys).filter(([, v]) => v !== undefined && v !== null)
      )
      const res = await api.put('/api/config', { ...cfg, secrets })
      if (res.config) setCfg(res.config)
      setKeys({})
      if (res.config_error) {
        setError(res.config_error)
      } else {
        setNotice(res.reloaded_now
          ? '已保存并生效。'
          : '已保存。后台任务正在跑，结束后会自动用上新配置。')
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  if (!cfg) {
    return (
      <div className="stack">
        <div className="banner warn">
          配置读不出来：{error || '…'}
          <button className="link-btn" onClick={load}>重试</button>
        </div>
      </div>
    )
  }

  const presetEnv = presets.find(
    (p) => p.name === cfg.model.name && p.base_url === (cfg.model.base_url || '')
  )?.env || ''

  return (
    <div className="stack">
      {error && <div className="banner danger">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}

      <section className="panel">
        <div className="panel-head">
          <h2>模型</h2>
          <span className="micro">填完 Key 保存，聊天和后台任务就会启动</span>
        </div>
        <div className="grid two">
          <label className="field">
            <span className="field-label">服务商预设</span>
            <select value={presetEnv} onChange={(e) => choosePreset(e.target.value)}>
              <option value="">自定义</option>
              {presets.map((p) => (
                <option key={p.env} value={p.env}>{p.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">模型名</span>
            <input value={cfg.model.name} onChange={(e) => set('model.name', e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">Base URL（可空）</span>
            <input value={cfg.model.base_url} onChange={(e) => set('model.base_url', e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">API Key 环境变量名</span>
            <input value={cfg.model.api_key_env}
                   onChange={(e) => set('model.api_key_env', e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">max_tokens</span>
            <input value={cfg.model.max_tokens} onChange={(e) => set('model.max_tokens', e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">temperature</span>
            <input value={cfg.model.temperature} onChange={(e) => set('model.temperature', e.target.value)} />
          </label>
        </div>
        <div className="grid two">
          <SecretField
            env={cfg.model.api_key_env}
            label="模型 API Key"
            values={keys}
            setValues={setKeys}
            status={cfg.secrets.set}
          />
        </div>
        <div className="micro note">
          Key 写在本机 <span className="mono">{cfg.secrets_path}</span>，不写进 dot.toml。
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h2>运行</h2></div>
        <div className="grid three">
          <label className="field">
            <span className="field-label">心跳间隔（秒）</span>
            <input value={cfg.dot.tick_seconds} onChange={(e) => set('dot.tick_seconds', e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">工作区（只读）</span>
            <input value={cfg.dot.workspace} disabled />
          </label>
          <label className="field">
            <span className="field-label">数据库（只读）</span>
            <input value={cfg.dot.db} disabled />
          </label>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>事件源</h2>
          <span className="micro">没人说话时，靠这些唤醒它</span>
        </div>

        <div className="source-block">
          <label className="switch">
            <input type="checkbox" checked={cfg.sources.fs.enabled}
                   onChange={(e) => set('sources.fs.enabled', e.target.checked)} />
            <span>文件事件</span>
          </label>
          <div className="grid two">
            <label className="field">
              <span className="field-label">监听目录（逗号分隔，相对工作区）</span>
              <input value={(cfg.sources.fs.paths || []).join(', ')}
                     onChange={(e) => set('sources.fs.paths',
                       e.target.value.split(',').map((s) => s.trim()).filter(Boolean))} />
            </label>
            <label className="field">
              <span className="field-label">轮询间隔（秒）</span>
              <input value={cfg.sources.fs.interval}
                     onChange={(e) => set('sources.fs.interval', e.target.value)} />
            </label>
          </div>
        </div>

        <div className="source-block">
          <label className="switch">
            <input type="checkbox" checked={cfg.sources.imap.enabled}
                   onChange={(e) => set('sources.imap.enabled', e.target.checked)} />
            <span>IMAP 邮箱（事件 + 审批回复）</span>
          </label>
          <div className="grid two">
            <label className="field"><span className="field-label">服务器</span>
              <input value={cfg.sources.imap.host}
                     onChange={(e) => set('sources.imap.host', e.target.value)} /></label>
            <label className="field"><span className="field-label">端口</span>
              <input value={cfg.sources.imap.port}
                     onChange={(e) => set('sources.imap.port', e.target.value)} /></label>
            <label className="field"><span className="field-label">账号</span>
              <input value={cfg.sources.imap.user}
                     onChange={(e) => set('sources.imap.user', e.target.value)} /></label>
            <label className="field"><span className="field-label">文件夹</span>
              <input value={cfg.sources.imap.folder}
                     onChange={(e) => set('sources.imap.folder', e.target.value)} /></label>
            <SecretField env={cfg.sources.imap.password_env} label="邮箱密码"
                         values={keys} setValues={setKeys} status={cfg.secrets.set} />
            <label className="field"><span className="field-label">轮询间隔（秒）</span>
              <input value={cfg.sources.imap.interval}
                     onChange={(e) => set('sources.imap.interval', e.target.value)} /></label>
          </div>
          <label className="switch small">
            <input type="checkbox" checked={cfg.sources.imap.verify_tls}
                   onChange={(e) => set('sources.imap.verify_tls', e.target.checked)} />
            <span>验证 TLS 证书（自建服务器才关）</span>
          </label>
        </div>

        <div className="source-block">
          <label className="switch">
            <input type="checkbox" checked={cfg.sources.ical.enabled}
                   onChange={(e) => set('sources.ical.enabled', e.target.checked)} />
            <span>日历 (.ics)</span>
          </label>
          <div className="grid two">
            <label className="field"><span className="field-label">URL</span>
              <input value={cfg.sources.ical.url}
                     onChange={(e) => set('sources.ical.url', e.target.value)} /></label>
            <label className="field"><span className="field-label">轮询间隔（秒）</span>
              <input value={cfg.sources.ical.interval}
                     onChange={(e) => set('sources.ical.interval', e.target.value)} /></label>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>审批邮件（SMTP）</h2>
          <span className="micro">不配也能用，审批就在这个界面里点</span>
        </div>
        <div className="grid two">
          <label className="field"><span className="field-label">服务器</span>
            <input value={cfg.smtp.host} onChange={(e) => set('smtp.host', e.target.value)} /></label>
          <label className="field"><span className="field-label">端口</span>
            <input value={cfg.smtp.port} onChange={(e) => set('smtp.port', e.target.value)} /></label>
          <label className="field"><span className="field-label">账号</span>
            <input value={cfg.smtp.user} onChange={(e) => set('smtp.user', e.target.value)} /></label>
          <label className="field"><span className="field-label">发件人</span>
            <input value={cfg.smtp.sender} onChange={(e) => set('smtp.sender', e.target.value)} /></label>
          <label className="field"><span className="field-label">审批邮件发给谁</span>
            <input value={cfg.smtp.to} onChange={(e) => set('smtp.to', e.target.value)} /></label>
          <SecretField env={cfg.smtp.password_env} label="SMTP 密码"
                       values={keys} setValues={setKeys} status={cfg.secrets.set} />
        </div>
        <label className="switch small">
          <input type="checkbox" checked={cfg.smtp.use_tls}
                 onChange={(e) => set('smtp.use_tls', e.target.checked)} />
          <span>使用 TLS</span>
        </label>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>审批规则</h2>
          <span className="micro">没写到的动作一律 ask</span>
        </div>
        <div className="grid three">
          <label className="field"><span className="field-label">邮件主题前缀</span>
            <input value={cfg.approval.subject_prefix}
                   onChange={(e) => set('approval.subject_prefix', e.target.value)} /></label>
          <label className="field"><span className="field-label">超时（小时，0 = 永远等）</span>
            <input value={cfg.approval.timeout_hours}
                   onChange={(e) => set('approval.timeout_hours', e.target.value)} /></label>
        </div>
        <div className="rules">
          {Object.entries(cfg.approval.rules).map(([name, value]) => (
            <div className="rule-row" key={name}>
              <span className="mono">{name}</span>
              <select value={value} onChange={(e) => setRule(name, e.target.value)}>
                {RULE_VALUES.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </div>
          ))}
          <div className="rule-row">
            <input placeholder="动作名，如 send_email" value={ruleName}
                   onChange={(e) => setRuleName(e.target.value)} />
            <button className="btn" onClick={addRule}>添加规则</button>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h2>其他</h2></div>
        <div className="grid three">
          <label className="field"><span className="field-label">技能目录（相对工作区）</span>
            <input value={cfg.skills.dir} onChange={(e) => set('skills.dir', e.target.value)} /></label>
          <label className="field"><span className="field-label">最多交接次数</span>
            <input value={cfg.handoff.max_hops}
                   onChange={(e) => set('handoff.max_hops', e.target.value)} /></label>
          <label className="field"><span className="field-label">每日 token 上限（0 = 不限）</span>
            <input value={cfg.safety.daily_token_budget}
                   onChange={(e) => set('safety.daily_token_budget', e.target.value)} /></label>
        </div>
        <label className="switch small">
          <input type="checkbox" checked={cfg.skills.enabled}
                 onChange={(e) => set('skills.enabled', e.target.checked)} />
          <span>启用技能库</span>
        </label>
      </section>

      <div className="save-bar">
        <span className="micro">配置保存到 {cfg.path}</span>
        <button className="btn primary" onClick={save} disabled={saving}>
          {saving ? '保存中…' : '保存配置'}
        </button>
      </div>
    </div>
  )
}
