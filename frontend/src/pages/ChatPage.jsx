import React, { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, fmtNum } from '../api.js'

export default function ChatPage({ health, go }) {
  const [messages, setMessages] = useState([])
  const [pending, setPending] = useState(null)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [tokens, setTokens] = useState(0)
  const bottom = useRef(null)

  useEffect(() => {
    api.get('/api/chat')
      .then((d) => {
        setMessages(d.messages || [])
        setPending(d.interrupt || null)
      })
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, sending, pending])

  const apply = (d) => {
    setMessages(d.messages || [])
    setPending(d.interrupt || null)
    if (d.tokens) setTokens(d.tokens)
  }

  const send = async () => {
    const text = input.trim()
    if (!text || sending || pending) return
    setError('')
    setSending(true)
    setInput('')
    setMessages((m) => [...m, { role: 'user', content: text }])
    try {
      apply(await api.post('/api/chat', { text }))
    } catch (e) {
      setError(e.message)
      setMessages((m) => m.slice(0, -1))
      setInput(text)
    } finally {
      setSending(false)
    }
  }

  const resume = async (approved) => {
    setError('')
    setSending(true)
    try {
      apply(await api.post('/api/chat/resume', { approved }))
    } catch (e) {
      setError(e.message)
    } finally {
      setSending(false)
    }
  }

  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const ready = health?.model_ready
  const disabled = !ready || sending || !!pending

  return (
    <div className="chat-wrap">
      {!ready && (
        <div className="banner warn">
          还没配好模型。
          <button className="link-btn" onClick={() => go('config')}>去配置 API Key</button>
        </div>
      )}

      <div className="chat-scroll">
        {messages.length === 0 && !sending && (
          <div className="chat-empty">
            <div className="chat-empty-mark" />
            <h2>直接说一句话</h2>
            <p>
              它能翻工作区、查资料、做事。涉及发邮件、跑命令这类动作时，
              会在这里停下来等你批准。
            </p>
            <div className="examples">
              <button onClick={() => setInput('工作区里现在有什么？帮我看看该先处理哪件事')}>工作区里现在有什么？</button>
              <button onClick={() => setInput('把 inbox 里的东西整理成一份周报')}>整理 inbox 成周报</button>
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="msg-role">{m.role === 'user' ? '你' : 'dots'}</div>
            <div className="msg-body">
              {m.role === 'assistant' ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
              ) : (
                <div className="plain">{m.content}</div>
              )}
            </div>
          </div>
        ))}

        {sending && (
          <div className="msg assistant">
            <div className="msg-role">dots</div>
            <div className="msg-body">
              <div className="typing"><span /><span /><span /></div>
            </div>
          </div>
        )}
        <div ref={bottom} />
      </div>

      {pending && (
        <div className="approval-card">
          <div className="approval-head">
            <span className="chip amber">等你批准</span>
            <span className="mono">{pending.action}</span>
          </div>
          <pre>{JSON.stringify(pending.args || {}, null, 2)}</pre>
          <div className="approval-actions">
            <button className="btn primary" disabled={sending} onClick={() => resume(true)}>批准</button>
            <button className="btn" disabled={sending} onClick={() => resume(false)}>拒绝</button>
          </div>
        </div>
      )}

      {error && <div className="banner danger">{error}</div>}

      <div className="composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKey}
          placeholder={ready ? '输入消息，Enter 发送，Shift+Enter 换行' : '先在配置里填好模型和 API Key'}
          disabled={disabled}
          rows={3}
        />
        <div className="composer-foot">
          <span className="micro">
            {tokens > 0 ? `上一轮 ${fmtNum(tokens)} token` : '对话上下文保存在本机'}
          </span>
          <button className="btn primary" onClick={send} disabled={disabled || !input.trim()}>
            发送
          </button>
        </div>
      </div>
    </div>
  )
}
