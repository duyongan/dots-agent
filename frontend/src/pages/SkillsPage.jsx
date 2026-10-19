import React, { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api.js'

const BLANK_BODY = `# 技能标题

## When to Use

什么时候用这份技能。

## Steps

1. 第一步
2. 第二步

## Pitfalls

容易踩的坑。
`

export default function SkillsPage() {
  const [meta, setMeta] = useState({ enabled: true, dir: 'skills', skills: [] })
  const [draft, setDraft] = useState(null)
  const [isNew, setIsNew] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)

  const load = async (select) => {
    try {
      const d = await api.get('/api/skills')
      setMeta(d)
      setError('')
      if (select) {
        const entry = d.skills.find((s) => s.name === select)
        if (entry) await open(entry.name)
      }
    } catch (e) {
      setError(e.message)
    }
  }

  const open = async (name) => {
    try {
      const s = await api.get(`/api/skills/${encodeURIComponent(name)}`)
      setDraft({ name: s.name, description: s.description, body: s.body })
      setIsNew(false)
      setNotice('')
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }

  const create = () => {
    setDraft({ name: '', description: '', body: BLANK_BODY })
    setIsNew(true)
    setNotice('')
    setError('')
  }

  const save = async () => {
    if (!draft) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      if (isNew) {
        await api.post('/api/skills', draft)
      } else {
        await api.put(`/api/skills/${encodeURIComponent(draft.name)}`, {
          description: draft.description,
          body: draft.body,
        })
      }
      setNotice('已保存。下一轮开跑时生效。')
      setIsNew(false)
      await load(draft.name)
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!draft || isNew) return
    if (!window.confirm(`删掉技能「${draft.name}」？文件会一起删。`)) return
    try {
      await api.del(`/api/skills/${encodeURIComponent(draft.name)}`)
      setDraft(null)
      setNotice('已删除。')
      await load()
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [])

  return (
    <div className="skills-layout">
      <aside className="panel skills-list">
        <div className="panel-head">
          <h2>技能库</h2>
          <button className="btn small" onClick={create} disabled={!meta.enabled}>新建</button>
        </div>
        <div className="micro dir">workspace / {meta.dir}</div>
        {!meta.enabled && (
          <div className="banner warn">技能功能已在配置里关掉。</div>
        )}
        {meta.skills.length === 0 && <div className="empty">还没有技能。它在干活时会自己攒。</div>}
        {meta.skills.map((s) => (
          <button
            key={s.name}
            className={`skill-item ${draft?.name === s.name && !isNew ? 'active' : ''}`}
            onClick={() => open(s.name)}
          >
            <span className="skill-name mono">{s.name}</span>
            <span className="skill-desc">{s.description}</span>
          </button>
        ))}
      </aside>

      <section className="panel skills-editor">
        {!draft && (
          <div className="empty big">
            选一份技能看正文，或者新建一份。
            <div className="micro" style={{ marginTop: 8 }}>
              描述会每轮进系统提示，上限 60 字符、句号结尾。
            </div>
          </div>
        )}

        {draft && (
          <>
            <div className="panel-head">
              <h2>{isNew ? '新建技能' : draft.name}</h2>
              <div className="row">
                {!isNew && <button className="btn danger" onClick={remove}>删除</button>}
                <button className="btn primary" onClick={save} disabled={saving}>
                  {saving ? '保存中…' : '保存'}
                </button>
              </div>
            </div>

            {error && <div className="banner danger">{error}</div>}
            {notice && <div className="banner ok">{notice}</div>}

            <div className="grid two">
              <label className="field">
                <span className="field-label">名字（小写连字符，保存后不能改）</span>
                <input value={draft.name} disabled={!isNew}
                       onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
              </label>
              <label className="field">
                <span className="field-label">描述（≤60 字符，句号结尾）</span>
                <input value={draft.description}
                       onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
              </label>
            </div>

            <div className="editor-split">
              <label className="field">
                <span className="field-label">正文（markdown）</span>
                <textarea className="editor" value={draft.body}
                          onChange={(e) => setDraft({ ...draft, body: e.target.value })} />
              </label>
              <div className="preview">
                <div className="field-label">预览</div>
                <div className="markdown">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.body}</ReactMarkdown>
                </div>
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
