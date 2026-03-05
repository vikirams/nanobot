import { useState, useRef, useEffect, useCallback, memo } from 'react'
import { marked } from 'marked'

const GATEWAY = import.meta.env.VITE_GATEWAY_URL || 'http://localhost:8080'
const ACCOUNT_ID = import.meta.env.VITE_ACCOUNT_ID || 'OmFHWEVhmrOvkYlhH2dx'

// Configure marked: GitHub-flavoured markdown, line breaks preserved
marked.use({ breaks: true, gfm: true })

/**
 * Render markdown to HTML with:
 * - #download-csv sentinel rewritten to a direct /export/latest one-click URL
 * - /download/ links rewritten to gateway URL
 * - #push-to-segment sentinel rewritten to a data-action button (intercepted by click handler)
 * - #push-to-webhook sentinel rewritten to a data-action button (intercepted by click handler)
 * - tables wrapped in a scrollable container
 * - crash-safe (returns escaped text on error)
 *
 * sessionId is passed so the export URL is scoped to the current session.
 * which is the 1-based dataset index so each result gets its own download link.
 */
function renderMarkdown(src, sessionId, which = 'last') {
  try {
    let html = marked.parse(src)
    // Wrap tables for horizontal scroll
    html = html
      .replace(/<table>/g, '<div class="table-wrap"><table>')
      .replace(/<\/table>/g, '</table></div>')

    // #download-csv sentinel: agent-emitted offer button → direct file URL
    if (sessionId) {
      const exportUrl = `${GATEWAY}/export/latest`
        + `?session_id=${encodeURIComponent(sessionId)}`
        + `&account_id=${encodeURIComponent(ACCOUNT_ID)}`
        + `&which=${encodeURIComponent(which)}`
      html = html.replace(
        /href="#download-csv"/gi,
        `href="${exportUrl}" class="download-btn" target="_blank" rel="noopener noreferrer"`,
      )
    }

    // Rewrite /download/ hrefs to full gateway URL
    html = html.replace(
      /href="(?:[a-z][a-z0-9+.-]*:\/*)?\/download\/([^"]+)"/gi,
      `href="${GATEWAY}/download/$1" target="_blank" rel="noopener noreferrer" class="download-btn"`,
    )

    // #push-to-segment sentinel → green action button (click intercepted in messages-wrap)
    html = html.replace(
      /href="#push-to-segment"/gi,
      `href="#" data-action="push-to-segment" class="action-btn action-btn--segment"`,
    )

    // #push-to-webhook sentinel → purple action button (click intercepted in messages-wrap)
    html = html.replace(
      /href="#push-to-webhook"/gi,
      `href="#" data-action="push-to-webhook" class="action-btn action-btn--webhook"`,
    )

    // #preview-last sentinel → placeholder div; MessageBubble will fetch + render the table
    html = html.replace(
      /<a\s[^>]*href="#preview-last"[^>]*>[\s\S]*?<\/a>/gi,
      `<div class="preview-sentinel" data-preview-sentinel data-session-id="${sessionId || ''}"></div>`,
    )

    return html
  } catch (e) {
    console.error('[renderMarkdown] parse error:', e)
    return `<pre>${src.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre>`
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function genId() {
  return crypto.randomUUID()
}

/**
 * Parse an SSE buffer into discrete events.
 */
function parseSSEBuffer(buffer) {
  const events = []
  const blocks = buffer.split('\n\n')
  const remaining = blocks.pop() ?? ''

  for (const block of blocks) {
    const trimmed = block.trim()
    if (!trimmed || trimmed.startsWith(':')) continue

    let eventType = null
    let dataStr = ''

    for (const line of trimmed.split('\n')) {
      if (line.startsWith('event: ')) eventType = line.slice(7).trim()
      else if (line.startsWith('data: ')) dataStr = line.slice(6)
    }

    if (eventType && dataStr) {
      try {
        events.push({ type: eventType, payload: JSON.parse(dataStr) })
      } catch {
        // skip malformed JSON
      }
    }
  }

  return { events, remaining }
}

/**
 * Format a SQLite timestamp (UTC "YYYY-MM-DD HH:MM:SS") into a human label.
 */
function formatSessionDate(dateStr) {
  if (!dateStr) return ''
  // SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS" (space, not T)
  const d = new Date(dateStr.replace(' ', 'T') + 'Z')
  if (isNaN(d)) return ''
  const now = new Date()
  const diffDays = Math.floor((now - d) / 86400000)
  if (diffDays === 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return d.toLocaleDateString([], { weekday: 'short' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  // sessionId persisted in localStorage so refreshing the page continues the same conversation
  const [sessionId, setSessionId] = useState(() =>
    localStorage.getItem('nanobot_session_id') || genId()
  )
  const [messages, setMessages] = useState([])
  const [sessions, setSessions] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)

  const bottomRef = useRef(null)
  const textareaRef = useRef(null)
  const abortRef = useRef(null)
  const fileInputRef = useRef(null)

  // Persist sessionId whenever it changes
  useEffect(() => {
    localStorage.setItem('nanobot_session_id', sessionId)
  }, [sessionId])

  // Scroll to bottom whenever messages change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 200) + 'px'
  }, [input])

  // ── Session list ─────────────────────────────────────────────────────────────

  const loadSessions = useCallback(async () => {
    try {
      const res = await fetch(
        `${GATEWAY}/api/sessions?account_id=${encodeURIComponent(ACCOUNT_ID)}`
      )
      if (!res.ok) return
      setSessions(await res.json())
    } catch { /* network unavailable — silently ignore */ }
  }, [])

  // On mount: load sidebar + restore current session history
  useEffect(() => {
    loadSessions()

    const savedId = localStorage.getItem('nanobot_session_id')
    if (!savedId) return
    fetch(
      `${GATEWAY}/api/sessions/${encodeURIComponent(savedId)}/messages` +
      `?account_id=${encodeURIComponent(ACCOUNT_ID)}`
    )
      .then(r => r.ok ? r.json() : [])
      .then(history => {
        if (history.length > 0) {
          setMessages(history.map(m => ({
            id: genId(), role: m.role, content: m.content || '', streaming: false,
          })))
        }
      })
      .catch(() => {})
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []) // intentionally only on mount

  // ── New session ──────────────────────────────────────────────────────────────

  const startNewSession = useCallback(() => {
    abortRef.current?.abort()
    setBusy(false)
    setMessages([])
    setInput('')
    setSessionId(genId())
    loadSessions()
    setTimeout(() => textareaRef.current?.focus(), 0)
  }, [loadSessions])

  // ── Switch to existing session ────────────────────────────────────────────────

  const switchSession = useCallback(async (id) => {
    if (id === sessionId) return
    abortRef.current?.abort()
    setBusy(false)
    setInput('')
    setSessionId(id)
    try {
      const res = await fetch(
        `${GATEWAY}/api/sessions/${encodeURIComponent(id)}/messages` +
        `?account_id=${encodeURIComponent(ACCOUNT_ID)}`
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const history = await res.json()
      setMessages(history.map(m => ({
        id: genId(), role: m.role, content: m.content || '', streaming: false,
      })))
    } catch {
      setMessages([])
    }
    setTimeout(() => textareaRef.current?.focus(), 0)
  }, [sessionId])

  // ── Core send logic ───────────────────────────────────────────────────────────

  const sendContent = useCallback(async (content) => {
    if (!content || busy) return

    setBusy(true)

    // User bubble
    setMessages(prev => [...prev, { id: genId(), role: 'user', content }])

    // Assistant placeholder
    const assistantId = genId()
    setMessages(prev => [
      ...prev,
      { id: assistantId, role: 'assistant', content: '', progress: null, streaming: true },
    ])

    const abort = new AbortController()
    abortRef.current = abort

    const patch = (fields) =>
      setMessages(prev => {
        const next = [...prev]
        const idx = next.findIndex(m => m.id === assistantId)
        if (idx !== -1) next[idx] = { ...next[idx], ...fields }
        return next
      })

    try {
      const res = await fetch(`${GATEWAY}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, sessionId, accountId: ACCOUNT_ID }),
        signal: abort.signal,
      })

      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }))
        throw new Error(err.error || `HTTP ${res.status}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      // Local accumulator for streaming tokens — avoids stale-closure issues with React state
      let streamedContent = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buf += decoder.decode(value, { stream: true })
        const { events, remaining } = parseSSEBuffer(buf)
        buf = remaining

        for (const { type, payload } of events) {
          console.debug('[SSE]', type, payload)
          if (type === 'token') {
            // Individual LLM token delta — append to streaming buffer
            streamedContent += payload.content
            patch({ content: streamedContent, streaming: true, progress: null })
          } else if (type === 'progress') {
            // Tool hint or interim update — only show if not already streaming tokens
            if (!streamedContent) {
              patch({ progress: payload.content })
            }
          } else if (type === 'final') {
            // Use server-assembled final (authoritative); fall back to accumulated tokens
            patch({ content: payload.content || streamedContent, streaming: false, progress: null })
            streamedContent = ''
          } else if (type === 'error') {
            patch({ content: payload.content, streaming: false, progress: null, error: true })
            streamedContent = ''
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        patch({ content: err.message, streaming: false, progress: null, error: true })
      }
    } finally {
      setBusy(false)
      loadSessions() // refresh sidebar after each completed turn
    }
  }, [sessionId, busy, loadSessions])

  // ── CSV file upload ──────────────────────────────────────────────────────────

  const handleFileUpload = useCallback(async (e) => {
    const file = e.target.files?.[0]
    if (!fileInputRef.current) return
    fileInputRef.current.value = ''   // reset so same file can be re-selected
    if (!file) return

    const formData = new FormData()
    formData.append('file', file)
    formData.append('account_id', ACCOUNT_ID)

    try {
      const res = await fetch(`${GATEWAY}/upload/csv`, { method: 'POST', body: formData })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const meta = await res.json()
      const { filename, row_count, domain_column, preview } = meta

      // Pre-fill the textarea with a ready-to-send enrichment message
      const domainInfo = domain_column
        ? `${row_count} domains from column "${domain_column}" (e.g. ${preview.slice(0, 3).join(', ')}…)`
        : `${row_count} rows (no domain column detected — please specify which column has domains)`

      setInput(
        `I uploaded a CSV file with ${domainInfo}. ` +
        `File saved as "${filename}". ` +
        `Find [specify contact titles, e.g. CTO, CEO, Head of Cloud] for each company.`
      )
      setTimeout(() => textareaRef.current?.focus(), 0)
    } catch (err) {
      console.error('[handleFileUpload] error:', err)
      setInput(`Failed to upload CSV: ${err.message}`)
    }
  }, [])

  // ── Export action click interceptor ─────────────────────────────────────────
  // Handles #push-to-segment and #push-to-webhook sentinel links rendered by renderMarkdown.
  // These are data-action anchors — prevent default navigation, pre-fill the textarea instead.

  const handleActionClick = useCallback((e) => {
    const link = e.target.closest('a[data-action]')
    if (!link) return
    e.preventDefault()
    const action = link.dataset.action
    if (action === 'push-to-segment') {
      setInput('Push the last discovery result to Segment.')
      setTimeout(() => textareaRef.current?.focus(), 0)
    } else if (action === 'push-to-webhook') {
      setInput('Push to webhook: ')
      setTimeout(() => textareaRef.current?.focus(), 0)
    }
  }, [])

  // ── Send from textarea ───────────────────────────────────────────────────────

  const send = useCallback(async () => {
    const content = input.trim()
    if (!content) return
    setInput('')
    await sendContent(content)
  }, [input, sendContent])

  // Stop an in-flight request
  const stop = useCallback(() => {
    abortRef.current?.abort()
    setBusy(false)
    setMessages(prev => {
      const next = [...prev]
      const last = next[next.length - 1]
      if (last?.role === 'assistant' && last.streaming) {
        next[next.length - 1] = { ...last, streaming: false }
      }
      return next
    })
  }, [])

  const onKeyDown = useCallback(
    (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        send()
      }
    },
    [send],
  )

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="app-shell">
      {/* ── Sidebar ── */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="brand-icon">🐈</span>
          <span className="brand-name">nanobot</span>
          <button
            className="btn-new-compact"
            onClick={startNewSession}
            title="New conversation"
            aria-label="New conversation"
          >
            ＋
          </button>
        </div>
        <div className="session-list">
          {sessions.length === 0 ? (
            <p className="session-empty">No conversations yet</p>
          ) : (
            sessions.map(s => (
              <button
                key={s.session_id}
                className={`session-item${s.session_id === sessionId ? ' session-item--active' : ''}`}
                onClick={() => switchSession(s.session_id)}
                title={s.title}
              >
                <span className="session-title">{s.title}</span>
                <span className="session-date">{formatSessionDate(s.updated_at)}</span>
              </button>
            ))
          )}
        </div>
      </aside>

      {/* ── Main chat area ── */}
      <div className="main">
        <div className="layout">
          {/* ── Top bar ── */}
          <header className="topbar">
            <span className="session-badge" title={`Session: ${sessionId}`}>
              {sessionId.slice(0, 8)}
            </span>
          </header>

          {/* ── Messages ── */}
          {/* onClick uses event delegation to intercept #push-to-segment / #push-to-webhook action anchors */}
          <div className="messages-wrap" onClick={handleActionClick}>
            {messages.length === 0 ? (
              <div className="empty-state">
                <div className="empty-icon">🐈</div>
                <h2>How can I help?</h2>
                <p>Ask me anything — I can search the web, run code, and more.</p>
              </div>
            ) : (
              messages.map((msg, idx) => {
                // Compute 1-based dataset index for this message's download/preview buttons.
                // Count how many discovery messages (those with #download-csv) have appeared
                // up to and including this one — so each result links to its own dataset.
                let datasetIndex = 'last'
                if (msg.role === 'assistant' && typeof msg.content === 'string' && msg.content.includes('#download-csv')) {
                  let count = 0
                  for (let i = 0; i <= idx; i++) {
                    const m = messages[i]
                    if (m.role === 'assistant' && typeof m.content === 'string' && m.content.includes('#download-csv')) {
                      count++
                    }
                  }
                  datasetIndex = count
                }
                return <MessageBubble key={msg.id} msg={msg} sessionId={sessionId} datasetIndex={datasetIndex} />
              })
            )}
            <div ref={bottomRef} className="scroll-anchor" />
          </div>

          {/* ── Input bar ── */}
          <div className="input-bar">
            {/* Hidden CSV file input */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,text/csv"
              style={{ display: 'none' }}
              onChange={handleFileUpload}
            />
            <button
              className="btn-attach"
              onClick={() => fileInputRef.current?.click()}
              title="Upload CSV with company domains"
              aria-label="Upload CSV"
              disabled={busy}
            >
              📎
            </button>
            <textarea
              ref={textareaRef}
              className="input-field"
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Message nanobot… (Enter to send, Shift+Enter for newline)"
              rows={1}
              disabled={busy}
              autoFocus
            />
            {busy ? (
              <button className="btn-stop" onClick={stop} title="Stop generating">
                ■
              </button>
            ) : (
              <button
                className="btn-send"
                onClick={send}
                disabled={!input.trim()}
                aria-label="Send"
                title="Send message"
              >
                ↑
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Preview table — fetched from /api/preview/latest and rendered client-side ──

function PreviewTable({ sessionId, which = 'last' }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!sessionId) return
    const url =
      `${GATEWAY}/api/preview/latest` +
      `?session_id=${encodeURIComponent(sessionId)}` +
      `&account_id=${encodeURIComponent(ACCOUNT_ID)}` +
      `&which=${encodeURIComponent(which)}` +
      `&max_rows=20`
    fetch(url)
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData)
      .catch(e => setError(String(e)))
  }, [sessionId, which])

  if (error) return <p className="preview-error">Preview unavailable: {error}</p>
  if (!data) return <p className="preview-loading">⏳ Loading preview…</p>

  const { columns, rows, total, preview_rows } = data
  return (
    <div className="preview-table-wrap">
      <p className="preview-meta">Showing {preview_rows} of {total} total records</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>{columns.map(c => <th key={c}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {columns.map(c => <td key={c}>{row[c] ?? ''}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Message bubble ─────────────────────────────────────────────────────────────

const PREVIEW_SENTINEL_RE = /\[Preview\]\(#preview-last\)/i

function MessageBubble({ msg, sessionId, datasetIndex = 'last' }) {
  const { role, content, progress, streaming, error } = msg
  const isUser = role === 'user'
  const contentRef = useRef(null)

  // After dangerouslySetInnerHTML renders, wire up any preview sentinels
  // (the div with data-preview-sentinel injected by renderMarkdown).
  // We replace the placeholder with a React portal root — but since portals
  // are complex here, we rely on the PREVIEW_SENTINEL_RE check to render
  // PreviewTable as a React sibling instead.
  const hasPreviewSentinel = !isUser && typeof content === 'string' && PREVIEW_SENTINEL_RE.test(content)

  // Split content at the sentinel so we can inject the React table component inline
  const [beforeSentinel, afterSentinel] = (() => {
    if (!hasPreviewSentinel) return [content, null]
    const match = content.match(/([\s\S]*?)\[Preview\]\(#preview-last\)([\s\S]*)/i)
    return match ? [match[1].trim(), match[2].trim()] : [content, null]
  })()

  return (
    <div className={`msg msg--${role}${error ? ' msg--error' : ''}`}>
      <div className="msg-avatar" aria-hidden="true">
        {isUser ? '👤' : '🐈'}
      </div>

      <div className="msg-body">
        {isUser ? (
          <p className="msg-text">{content}</p>
        ) : streaming && !content ? (
          <div className="msg-thinking">
            {progress ? (
              <span className="msg-progress">{progress}</span>
            ) : (
              <TypingDots />
            )}
          </div>
        ) : (
          <>
            {progress && streaming && (
              <div className="msg-progress-bar">{progress}</div>
            )}
            {hasPreviewSentinel ? (
              <>
                {beforeSentinel && (
                  <div
                    ref={contentRef}
                    className="msg-markdown"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(beforeSentinel, sessionId, datasetIndex) }}
                  />
                )}
                <PreviewTable sessionId={sessionId} which={datasetIndex} />
                {afterSentinel && (
                  <div
                    className="msg-markdown"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(afterSentinel, sessionId, datasetIndex) }}
                  />
                )}
              </>
            ) : (
              <div
                ref={contentRef}
                className="msg-markdown"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(content, sessionId, datasetIndex) }}
              />
            )}
            {streaming && <span className="cursor" aria-hidden="true">▋</span>}
          </>
        )}
      </div>
    </div>
  )
}

function TypingDots() {
  return (
    <span className="typing-dots" aria-label="Thinking…">
      <span /><span /><span />
    </span>
  )
}
