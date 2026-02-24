import { useState, useRef, useEffect, useCallback } from 'react'
import { marked } from 'marked'

const GATEWAY = import.meta.env.VITE_GATEWAY_URL || 'http://localhost:8080'

// Configure marked: GitHub-flavoured markdown, line breaks preserved
marked.use({ breaks: true, gfm: true })

/**
 * Render markdown to HTML with:
 * - /download/ links rewritten to gateway URL
 * - tables wrapped in a scrollable container
 * - crash-safe (returns escaped text on error)
 */
function renderMarkdown(src) {
  try {
    let html = marked.parse(src)
    // Wrap tables for horizontal scroll
    html = html
      .replace(/<table>/g, '<div class="table-wrap"><table>')
      .replace(/<\/table>/g, '</table></div>')
    // Rewrite /download/ hrefs to full gateway URL and force new tab.
    // Strip any protocol prefix the model may have added (sandbox:, file:///, etc.)
    // Matches: /download/f  |  sandbox:/download/f  |  file:///download/f
    html = html.replace(
      /href="(?:[a-z][a-z0-9+.-]*:\/*)?\/download\/([^"]+)"/gi,
      `href="${GATEWAY}/download/$1" target="_blank" rel="noopener noreferrer"`,
    )
    return html
  } catch (e) {
    console.error('[renderMarkdown] parse error:', e)
    // Fallback: render as escaped plain text
    return `<pre>${src.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre>`
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function genId() {
  return crypto.randomUUID()
}

/**
 * Parse an SSE buffer into discrete events.
 * Returns fully-formed events and whatever incomplete data remains in the buffer.
 */
function parseSSEBuffer(buffer) {
  const events = []
  const blocks = buffer.split('\n\n')
  const remaining = blocks.pop() ?? '' // last block may be incomplete

  for (const block of blocks) {
    const trimmed = block.trim()
    if (!trimmed || trimmed.startsWith(':')) continue // keepalive comment or empty

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

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [sessionId, setSessionId] = useState(genId)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)

  const bottomRef = useRef(null)
  const textareaRef = useRef(null)
  const abortRef = useRef(null)

  // Scroll to bottom whenever the message list changes
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Auto-resize textarea as the user types
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 200) + 'px'
  }, [input])

  // ── New session ─────────────────────────────────────────────────────────────

  const startNewSession = useCallback(() => {
    abortRef.current?.abort()
    setBusy(false)
    setMessages([])
    setInput('')
    setSessionId(genId())
    setTimeout(() => textareaRef.current?.focus(), 0)
  }, [])

  // ── Send message ────────────────────────────────────────────────────────────

  const send = useCallback(async () => {
    const content = input.trim()
    if (!content || busy) return

    setInput('')
    setBusy(true)

    // User bubble
    setMessages(prev => [...prev, { id: genId(), role: 'user', content }])

    // Assistant placeholder (streaming=true while we wait)
    const assistantId = genId()
    setMessages(prev => [
      ...prev,
      { id: assistantId, role: 'assistant', content: '', progress: null, streaming: true },
    ])

    const abort = new AbortController()
    abortRef.current = abort

    // Helper: patch the assistant bubble in place
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
        body: JSON.stringify({ content, session_id: sessionId }),
        signal: abort.signal,
      })

      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }))
        throw new Error(err.error || `HTTP ${res.status}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buf += decoder.decode(value, { stream: true })
        const { events, remaining } = parseSSEBuffer(buf)
        buf = remaining

        for (const { type, payload } of events) {
          console.debug('[SSE]', type, payload)
          if (type === 'progress') {
            // Show tool hints / interim status while waiting for final
            patch({ progress: payload.content })
          } else if (type === 'final') {
            patch({ content: payload.content, streaming: false, progress: null })
          } else if (type === 'error') {
            patch({ content: payload.content, streaming: false, progress: null, error: true })
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        patch({
          content: err.message,
          streaming: false,
          progress: null,
          error: true,
        })
      }
    } finally {
      setBusy(false)
    }
  }, [input, sessionId, busy])

  // Stop an in-flight request
  const stop = useCallback(() => {
    abortRef.current?.abort()
    setBusy(false)
    // Mark the last assistant bubble as no longer streaming
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
    <div className="layout">
      {/* ── Top bar ── */}
      <header className="topbar">
        <div className="topbar-brand">
          <span className="brand-icon">🐈</span>
          <span className="brand-name">nanobot</span>
          <span className="session-badge" title={`Session: ${sessionId}`}>
            {sessionId.slice(0, 8)}
          </span>
        </div>
        <button className="btn-new" onClick={startNewSession} title="Start a new conversation">
          ＋ New chat
        </button>
      </header>

      {/* ── Messages ── */}
      <div className="messages-wrap">
        {messages.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">🐈</div>
            <h2>How can I help?</h2>
            <p>Ask me anything — I can search the web, run code, and more.</p>
          </div>
        ) : (
          messages.map(msg => <MessageBubble key={msg.id} msg={msg} />)
        )}
        <div ref={bottomRef} className="scroll-anchor" />
      </div>

      {/* ── Input bar ── */}
      <div className="input-bar">
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
  )
}

// ── Message bubble ─────────────────────────────────────────────────────────────

function MessageBubble({ msg }) {
  const { role, content, progress, streaming, error } = msg
  const isUser = role === 'user'

  return (
    <div className={`msg msg--${role}${error ? ' msg--error' : ''}`}>
      <div className="msg-avatar" aria-hidden="true">
        {isUser ? '👤' : '🐈'}
      </div>

      <div className="msg-body">
        {isUser ? (
          <p className="msg-text">{content}</p>
        ) : streaming && !content ? (
          /* Waiting: show progress hint or typing dots */
          <div className="msg-thinking">
            {progress ? (
              <span className="msg-progress">{progress}</span>
            ) : (
              <TypingDots />
            )}
          </div>
        ) : (
          /* Response received */
          <>
            {progress && streaming && (
              <div className="msg-progress-bar">{progress}</div>
            )}
            <div
              className="msg-markdown"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
            />
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
