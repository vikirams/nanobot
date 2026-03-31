import { useState, useRef, useEffect, useCallback, memo } from 'react'
import { marked } from 'marked'

// In production the WebUI is served by the same aiohttp server — use relative
// URLs so the app works on any domain/IP without a build-time config.
// In local dev (Vite on :5173) set VITE_GATEWAY_URL=http://localhost:8080 in webui/.env.local
const GATEWAY = import.meta.env.VITE_GATEWAY_URL || ''
const REMOTE_AGENT_URL = import.meta.env.VITE_REMOTE_AGENT_URL || ''
const ACCOUNT_ID = import.meta.env.VITE_ACCOUNT_ID || 'TbZomqQGriXFmdvbrznx'
const USER_ID = import.meta.env.VITE_USER_ID || 'FhXfscdTfTFTgNPZRJUo'

const REMOTE_TOGGLE_KEY = 'nanobot_use_remote_agent'

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
 * gateway: base URL for export/download links (default GATEWAY).
 */
function renderMarkdown(src, sessionId, which = 'last', gateway = GATEWAY, resultsetId = null) {
  try {
    let html = marked.parse(src)
    // Wrap tables for horizontal scroll
    html = html
      .replace(/<table>/g, '<div class="table-wrap"><table>')
      .replace(/<\/table>/g, '</table></div>')

    // #download-csv sentinel: agent-emitted offer button → direct file URL.
    // If resultsetId is available (MCP-stored), proxy via /api/mcp/export.
    // Otherwise fall back to /export/latest (Agent Postgres).
    if (sessionId) {
      const exportUrl = resultsetId
        ? `${gateway}/api/mcp/export`
            + `?resultset_id=${encodeURIComponent(resultsetId)}`
            + `&account_id=${encodeURIComponent(ACCOUNT_ID)}`
            + `&user_id=${encodeURIComponent(USER_ID)}`
            + `&session_id=${encodeURIComponent(sessionId)}`
        : `${gateway}/export/latest`
            + `?session_id=${encodeURIComponent(sessionId)}`
            + `&account_id=${encodeURIComponent(ACCOUNT_ID)}`
            + `&user_id=${encodeURIComponent(USER_ID)}`
            + `&which=${encodeURIComponent(which)}`
      html = html.replace(
        /href="#download-csv"/gi,
        `href="${exportUrl}" class="download-btn" target="_blank" rel="noopener noreferrer"`,
      )
    }

    // Rewrite /download/ hrefs to full gateway URL
    html = html.replace(
      /href="(?:[a-z][a-z0-9+.-]*:\/*)?\/download\/([^"]+)"/gi,
      `href="${gateway}/download/$1" target="_blank" rel="noopener noreferrer" class="download-btn"`,
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
  const [useRemoteAgent, setUseRemoteAgent] = useState(() =>
    localStorage.getItem(REMOTE_TOGGLE_KEY) === '1'
  )
  const [slashCommands, setSlashCommands] = useState([])
  const [showSlashMenu, setShowSlashMenu] = useState(false)
  const [selectedSlashIndex, setSelectedSlashIndex] = useState(0)
  const [activeSlashCmd, setActiveSlashCmd] = useState(null) // schema of matched cmd, for arg hint
  // Latest MCP resultset_id for the current session — used to route #download-csv
  const [activeResultsetId, setActiveResultsetId] = useState(null)
  const gatewayUrl = useRemoteAgent ? REMOTE_AGENT_URL : GATEWAY

  const bottomRef = useRef(null)
  const textareaRef = useRef(null)
  const abortRef = useRef(null)
  const fileInputRef = useRef(null)

  // Persist sessionId whenever it changes; reset resultset state for new session
  useEffect(() => {
    localStorage.setItem('nanobot_session_id', sessionId)
    setActiveResultsetId(null)
  }, [sessionId])

  useEffect(() => {
    localStorage.setItem(REMOTE_TOGGLE_KEY, useRemoteAgent ? '1' : '0')
  }, [useRemoteAgent])

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
        `${gatewayUrl}/api/sessions?account_id=${encodeURIComponent(ACCOUNT_ID)}&user_id=${encodeURIComponent(USER_ID)}`
      )
      if (!res.ok) return
      setSessions(await res.json())
    } catch { /* network unavailable — silently ignore */ }
  }, [gatewayUrl])

  const fetchSlashCommands = useCallback(() => {
    fetch(
      `${gatewayUrl}/api/mcp/prompts?account_id=${encodeURIComponent(ACCOUNT_ID)}`
    )
      .then(r => {
        if (!r.ok) {
          console.error('[MCP prompts] HTTP error:', r.status, r.statusText)
          return { prompts: [] }
        }
        return r.json()
      })
      .then(data => {
        console.log('[MCP prompts] Loaded:', data.prompts?.length || 0)
        const prompts = (data.prompts || []).map(p =>
          p.name === 'discovery'
            ? { ...p, arguments: [{ name: 'query', description: 'What to search for', required: true }] }
            : p
        )
        setSlashCommands(prompts)
      })
      .catch(err => {
        console.error('[MCP prompts] Fetch error:', err)
      })
  }, [gatewayUrl])

  // On mount: load sidebar + restore current session history
  useEffect(() => {
    loadSessions()
    fetchSlashCommands()

    const savedId = localStorage.getItem('nanobot_session_id')
    if (!savedId) return
    fetch(
      `${gatewayUrl}/api/sessions/${encodeURIComponent(savedId)}/messages` +
      `?account_id=${encodeURIComponent(ACCOUNT_ID)}&user_id=${encodeURIComponent(USER_ID)}`
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
  }, [gatewayUrl]) // re-run when gateway changes so history uses correct backend

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
        `${gatewayUrl}/api/sessions/${encodeURIComponent(id)}/messages` +
        `?account_id=${encodeURIComponent(ACCOUNT_ID)}&user_id=${encodeURIComponent(USER_ID)}`
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
  }, [sessionId, gatewayUrl])

  // ── Delete a session ──────────────────────────────────────────────────────────

  const deleteSession = useCallback(async (id) => {
    try {
      await fetch(
        `${gatewayUrl}/api/sessions/${encodeURIComponent(id)}` +
        `?account_id=${encodeURIComponent(ACCOUNT_ID)}`,
        { method: 'DELETE' }
      )
    } catch { /* ignore network errors */ }
    // If deleting the active session, start fresh
    if (id === sessionId) {
      abortRef.current?.abort()
      setBusy(false)
      setMessages([])
      setInput('')
      setSessionId(genId())
    }
    setSessions(prev => prev.filter(s => s.session_id !== id))
  }, [sessionId, gatewayUrl])

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
      const res = await fetch(`${gatewayUrl}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, sessionId, accountId: ACCOUNT_ID, userId: USER_ID }),
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
            const finalPatch = { content: payload.content || streamedContent, streaming: false, progress: null }
            if (payload.response) finalPatch.structured = payload.response
            patch(finalPatch)
            streamedContent = ''
            // Capture active_resultset_id so #download-csv links route to MCP export
            if (payload.active_resultset_id) {
              setActiveResultsetId(payload.active_resultset_id)
            }
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
  }, [sessionId, busy, loadSessions, gatewayUrl])

  // ── Slash command parsing ─────────────────────────────────────────────────────
  // Positional / quoted-string only. Quoted strings strip outer quotes.
  // Single required slot → all tokens joined. Multiple slots → one token per slot.
  // Special case: bare URL in /enrich → maps to linkedin_url.

  const parseSlashCommand = useCallback((text) => {
    const slashless = text.slice(1)
    const spaceIdx = slashless.indexOf(' ')
    const cmdName = spaceIdx === -1 ? slashless : slashless.slice(0, spaceIdx)
    const argsStr = spaceIdx === -1 ? '' : slashless.slice(spaceIdx + 1).trim()

    if (!argsStr) return { cmd: '/' + cmdName, args: {} }

    // Tokenize — respects double-quoted strings
    const tokens = []
    let i = 0
    while (i < argsStr.length) {
      while (i < argsStr.length && argsStr[i] === ' ') i++
      if (i >= argsStr.length) break
      if (argsStr[i] === '"') {
        let j = i + 1
        while (j < argsStr.length && argsStr[j] !== '"') j++
        tokens.push(argsStr.slice(i + 1, j))
        i = j + 1
      } else {
        let j = i
        while (j < argsStr.length && argsStr[j] !== ' ') j++
        tokens.push(argsStr.slice(i, j))
        i = j
      }
    }

    const named = {}

    // Special case: bare URL in /enrich → linkedin_url
    if (cmdName === 'enrich' && tokens.length > 0 &&
        (tokens[0].startsWith('http') || tokens[0].startsWith('linkedin.com'))) {
      named.linkedin_url = tokens.shift()
    }

    // Map remaining tokens to schema arg names in order
    if (tokens.length > 0) {
      const cmdSchema = slashCommands.find(c => c.name === cmdName)
      const schemaArgNames = (cmdSchema?.arguments || []).map(a => a.name).filter(n => !(n in named))

      if (schemaArgNames.length === 1) {
        named[schemaArgNames[0]] = tokens.join(' ')
      } else if (schemaArgNames.length > 1) {
        schemaArgNames.forEach((slot, idx) => {
          if (idx < tokens.length) named[slot] = tokens[idx]
        })
        if (tokens.length > schemaArgNames.length) {
          const lastSlot = schemaArgNames[schemaArgNames.length - 1]
          named[lastSlot] += ' ' + tokens.slice(schemaArgNames.length).join(' ')
        }
      }
    }

    return { cmd: '/' + cmdName, args: named }
  }, [slashCommands])

  const sendSlashCommand = useCallback(async (cmd, args) => {
    if (busy) return

    console.log('[SendSlash] cmd:', cmd, 'args:', args)
    setBusy(true)
    setShowSlashMenu(false)

    const content = `${cmd} ${Object.entries(args).map(([k, v]) => `${k}=${v}`).join(' ')}`
    setMessages(prev => [...prev, { id: genId(), role: 'user', content }])

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
      const res = await fetch(`${gatewayUrl}/api/mcp/prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          prompt: cmd.replace(/^\//, ''), 
          args, 
          session_id: sessionId, 
          account_id: ACCOUNT_ID, 
          user_id: USER_ID 
        }),
        signal: abort.signal,
      })

      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }))
        throw new Error(err.error || `HTTP ${res.status}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let streamedContent = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buf += decoder.decode(value, { stream: true })
        const { events, remaining } = parseSSEBuffer(buf)
        buf = remaining

        for (const { type, payload } of events) {
          console.debug('[SSE slash]', type, payload)
          if (type === 'progress') {
            patch({ progress: payload.content })
          } else if (type === 'final') {
            const finalPatch = { content: payload.content, streaming: false, progress: null }
            if (payload.response) finalPatch.structured = payload.response
            if (payload.active_resultset_id) setActiveResultsetId(payload.active_resultset_id)
            patch(finalPatch)
          } else if (type === 'error') {
            patch({ content: payload.content, streaming: false, progress: null, error: true })
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        patch({ content: err.message, streaming: false, progress: null, error: true })
      }
    } finally {
      setBusy(false)
      loadSessions()
    }
  }, [sessionId, busy, loadSessions, gatewayUrl])

  // ── CSV file upload ──────────────────────────────────────────────────────────

  const handleFileUpload = useCallback(async (e) => {
    const file = e.target.files?.[0]
    if (!fileInputRef.current) return
    fileInputRef.current.value = ''   // reset so same file can be re-selected
    if (!file) return

    const formData = new FormData()
    formData.append('file', file)
    formData.append('account_id', ACCOUNT_ID)
    formData.append('user_id', USER_ID)

    try {
      const res = await fetch(`${gatewayUrl}/upload/csv`, { method: 'POST', body: formData })
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
  }, [gatewayUrl])

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
    
    // Check if it's a slash command
    if (content.startsWith('/') && slashCommands.length > 0) {
      const { cmd, args } = parseSlashCommand(content)
      console.log('[Send Slash] cmd:', cmd, 'args:', args)
      // Check if the command matches a known slash command
      const matchedCmd = slashCommands.find(c => c.name === cmd || c.name === cmd.replace(/^\//, ''))
      if (matchedCmd) {
        setInput('')
        setActiveSlashCmd(null)
        await sendSlashCommand(cmd, args)
        return
      }
    }

    setInput('')
    setActiveSlashCmd(null)
    await sendContent(content)
  }, [input, sendContent, slashCommands, parseSlashCommand, sendSlashCommand])

  // Handle input change for slash command autocomplete
  const handleInputChange = useCallback((e) => {
    const value = e.target.value
    setInput(value)

    if (!value.startsWith('/')) {
      setShowSlashMenu(false)
      setActiveSlashCmd(null)
      return
    }

    // If commands haven't loaded yet, retry the fetch
    if (slashCommands.length === 0) {
      fetchSlashCommands()
      return
    }

    const parts = value.slice(1).split(' ')
    const cmdPart = parts[0].toLowerCase()
    const hasArgs = parts.length > 1

    // Exact match with space → command selected, show arg hint instead of menu
    const exactMatch = slashCommands.find(c => c.name.toLowerCase() === cmdPart)
    if (exactMatch && hasArgs) {
      setShowSlashMenu(false)
      setActiveSlashCmd(exactMatch)
      return
    }

    setActiveSlashCmd(null)
    const matches = slashCommands.filter(c => c.name.toLowerCase().includes(cmdPart))
    if (matches.length > 0) {
      setShowSlashMenu(true)
      setSelectedSlashIndex(0)
    } else {
      setShowSlashMenu(false)
    }
  }, [slashCommands, fetchSlashCommands])

  // ── Stop an in-flight request ─────────────────────────────────────────────────

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
      if (showSlashMenu) {
        const filtered = slashCommands.filter(c => {
          if (!input.startsWith('/')) return false
          const cmdPart = input.slice(1).split(' ')[0].toLowerCase()
          return c.name.toLowerCase().includes(cmdPart)
        }).slice(0, 5)

        if (e.key === 'ArrowDown') {
          e.preventDefault()
          setSelectedSlashIndex(i => (i + 1) % filtered.length)
          return
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault()
          setSelectedSlashIndex(i => (i - 1 + filtered.length) % filtered.length)
          return
        }
        if (e.key === 'Escape') {
          e.preventDefault()
          setShowSlashMenu(false)
          return
        }
        if ((e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) && filtered[selectedSlashIndex]) {
          e.preventDefault()
          const cmd = filtered[selectedSlashIndex]
          setInput('/' + cmd.name + ' ')
          setShowSlashMenu(false)
          setActiveSlashCmd(cmd)
          return
        }
      }

      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        send()
      }
    },
    [send, showSlashMenu, slashCommands, input, selectedSlashIndex],
  )

  // ── Render ──────────────────────────────────────────────────────────────────

  const currentSessionTitle = sessions.find(s => s.session_id === sessionId)?.title || null

  const QUICK_SUGGESTIONS = [
    '/segments',
    'Check field fill rate on my contacts',
    'Fetch contacts from a segment',
    'Find companies in SaaS with 200–500 employees',
  ]

  return (
    <div className="app-shell">
      {/* ── Sidebar ── */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="brand">
            <span className="brand-icon">🐈</span>
            <span className="brand-name">nanobot</span>
          </div>
          <button
            className="btn-new"
            onClick={startNewSession}
            title="New conversation"
            aria-label="New conversation"
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M7 1v12M1 7h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
            New
          </button>
        </div>
        <div className="session-list">
          {sessions.length === 0 ? (
            <p className="session-empty">No conversations yet</p>
          ) : (
            sessions.map(s => (
              <div
                key={s.session_id}
                className={`session-item${s.session_id === sessionId ? ' session-item--active' : ''}`}
              >
                <button
                  className="session-item-body"
                  onClick={() => switchSession(s.session_id)}
                  title={s.title}
                >
                  <span className="session-title">{s.title}</span>
                  <span className="session-date">{formatSessionDate(s.updated_at)}</span>
                </button>
                <button
                  className="session-delete"
                  onClick={(e) => { e.stopPropagation(); deleteSession(s.session_id) }}
                  title="Delete conversation"
                  aria-label="Delete conversation"
                >
                  <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                    <path d="M1 1l10 10M11 1L1 11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                </button>
              </div>
            ))
          )}
        </div>
      </aside>

      {/* ── Main chat area ── */}
      <div className="main">
        <div className="layout">
          {/* ── Top bar ── */}
          <header className="topbar">
            <span className="topbar-title" title={currentSessionTitle || sessionId}>
              {currentSessionTitle || 'New conversation'}
            </span>
            <span className="topbar-spacer" />
            <label className="env-toggle" title={useRemoteAgent ? `Connected to ${REMOTE_AGENT_URL}` : 'Using local dev server'}>
              <input
                type="checkbox"
                checked={useRemoteAgent}
                onChange={(e) => setUseRemoteAgent(e.target.checked)}
                aria-label="Use online agent"
              />
              <span className="env-toggle-track">
                <span className="env-toggle-thumb" />
              </span>
              <span className="env-toggle-label">{useRemoteAgent ? 'Online' : 'Dev'}</span>
            </label>
          </header>

          {/* ── Messages ── */}
          <div className="messages-wrap" onClick={handleActionClick}>
            {messages.length === 0 ? (
              <div className="empty-state">
                <div className="empty-logo">🐈</div>
                <p className="empty-headline">How can I help?</p>
                <p className="empty-sub">Ask me to find contacts, analyse your data, or run a GTM workflow.</p>
                <div className="quick-suggestions">
                  {QUICK_SUGGESTIONS.map(s => (
                    <button key={s} className="quick-suggestion" onClick={() => {
                      setInput(s)
                      setTimeout(() => textareaRef.current?.focus(), 0)
                    }}>
                      {s}
                    </button>
                  ))}
                </div>
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
                return (
                <MessageBubble
                  key={msg.id}
                  msg={msg}
                  sessionId={sessionId}
                  datasetIndex={datasetIndex}
                  gatewayUrl={gatewayUrl}
                  onSendMessage={sendContent}
                  resultsetId={msg.resultsetId || activeResultsetId}
                />
              )
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
              onChange={handleInputChange}
              onKeyDown={onKeyDown}
              placeholder="Message nanobot… (type / for commands)"
              rows={1}
              disabled={busy}
              autoFocus
            />
            {activeSlashCmd && !showSlashMenu && (
              <div className="slash-arg-hint">
                <span className="slash-arg-hint-cmd">/{activeSlashCmd.name}</span>
                {(activeSlashCmd.arguments || []).map(a => (
                  <span
                    key={a.name}
                    className={`slash-arg-hint-token ${a.required ? 'required' : 'optional'}`}
                    title={a.description || a.name}
                  >
                    {a.required ? `<${a.name}>` : `[${a.name}]`}
                  </span>
                ))}
                {activeSlashCmd.arguments?.length === 0 && (
                  <span className="slash-arg-hint-nodesc">no arguments</span>
                )}
              </div>
            )}
            {showSlashMenu && slashCommands.length > 0 && (
              <div className="slash-menu">
                {slashCommands
                  .filter(c => {
                    if (!input.startsWith('/')) return false
                    const cmdPart = input.slice(1).split(' ')[0].toLowerCase()
                    return c.name.toLowerCase().includes(cmdPart)
                  })
                  .slice(0, 5)
                  .map((cmd, idx) => (
                    <button
                      key={cmd.name}
                      className={`slash-menu-item${idx === selectedSlashIndex ? ' slash-menu-item--selected' : ''}`}
                      onClick={() => {
                        // Pre-fill with "/cmdname " so user starts typing the first arg
                        setInput('/' + cmd.name + ' ')
                        setShowSlashMenu(false)
                        setActiveSlashCmd(cmd)
                        textareaRef.current?.focus()
                      }}
                    >
                      <div className="slash-cmd-header">
                        <span className="slash-cmd-name">/{cmd.name}</span>
                        <span className="slash-cmd-desc">{cmd.description}</span>
                      </div>
                      {cmd.arguments && cmd.arguments.length > 0 && (
                        <div className="slash-cmd-params">
                          {cmd.arguments.map(a => (
                            <div key={a.name} className="slash-param-row">
                              <span className={`slash-arg ${a.required ? 'slash-arg-required' : 'slash-arg-optional'}`}>
                                {a.name}{a.required ? '*' : ''}
                              </span>
                              {a.description && (
                                <span className="slash-param-desc">{a.description}</span>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </button>
                  ))}
              </div>
            )}
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

// ── Preview table — renders inline rows or fetches from /api/preview/latest ────
// Pass inlineRows+inlineTotal to render without a server fetch (e.g. when the
// structured response already contains the preview array).

function PreviewTable({ sessionId, which = 'last', gatewayUrl = GATEWAY, inlineRows, inlineTotal }) {
  const [fetchedData, setFetchedData] = useState(null)
  const [error, setError] = useState(null)

  // Only fetch from server when no inline data was provided
  useEffect(() => {
    if (inlineRows) return
    if (!sessionId) return
    const url =
      `${gatewayUrl}/api/preview/latest` +
      `?session_id=${encodeURIComponent(sessionId)}` +
      `&account_id=${encodeURIComponent(ACCOUNT_ID)}` +
      `&user_id=${encodeURIComponent(USER_ID)}` +
      `&which=${encodeURIComponent(which)}` +
      `&max_rows=20`
    fetch(url)
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setFetchedData)
      .catch(e => setError(String(e)))
  }, [sessionId, which, gatewayUrl, inlineRows])

  // Use inline data when available, fall back to server-fetched
  const rows = inlineRows ?? fetchedData?.rows
  const total = inlineTotal ?? fetchedData?.total ?? rows?.length
  const preview_rows = rows?.length

  if (!inlineRows && error) return <p className="preview-error">Preview unavailable: {error}</p>
  if (!inlineRows && !fetchedData) return <p className="preview-loading">⏳ Loading preview…</p>
  if (!rows || rows.length === 0) return <p className="preview-error">No records to preview.</p>

  // Derive columns from the flat row keys; skip null-only columns
  const columns = Object.keys(rows[0])
  const activeColumns = columns.filter(col =>
    rows.some(row => row[col] !== null && row[col] !== undefined && row[col] !== '')
  )

  return (
    <div className="preview-table-wrap">
      <p className="preview-meta">Showing {preview_rows} of {total} records</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>{activeColumns.map(c => <th key={c}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {activeColumns.map(c => (
                  <td key={c}>{row[c] ?? ''}</td>
                ))}
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

/** Extract clarifying_questions from ```clarify\n...\n``` block; returns null if none or parse error. */
function parseClarifyBlock(text) {
  if (typeof text !== 'string') return null
  const match = text.match(/```clarify\s*\n([\s\S]*?)\n```/)
  if (!match) return null
  try {
    const raw = match[1].trim()
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr) || arr.length === 0) return null
    return arr.filter(
      (q) =>
        q && typeof q.id === 'string' && typeof q.question === 'string' && Array.isArray(q.options) && q.options.length >= 2
    )
  } catch {
    return null
  }
}

/** Remove ```clarify...``` block from content so we don't show raw JSON. */
function stripClarifyBlock(text) {
  if (typeof text !== 'string') return text
  return text.replace(/```clarify\s*\n[\s\S]*?\n```/g, '').trim()
}

function ClarificationForm({ questions, onSend }) {
  const [selected, setSelected] = useState(() => ({}))
  const handleSelect = (id, option) => {
    setSelected((prev) => ({ ...prev, [id]: option }))
  }
  const allAnswered = questions.every((q) => selected[q.id] != null)
  const handleSubmit = () => {
    if (!allAnswered || !onSend) return
    const parts = questions.map((q) => `[${q.id}]: ${selected[q.id]}`)
    onSend(`Clarifications: ${parts.join('; ')}`)
  }
  return (
    <div className="clarify-form" role="form" aria-label="Clarifying questions">
      {questions.map((q) => (
        <div key={q.id} className="clarify-question">
          <p className="clarify-question-label">{q.question}</p>
          <div className="clarify-options">
            {q.options.map((opt, idx) => (
              <button
                key={idx}
                type="button"
                className={`clarify-opt ${selected[q.id] === opt ? 'clarify-opt--selected' : ''}`}
                onClick={() => handleSelect(q.id, opt)}
              >
                {opt}
                {q.recommended_index === idx && <span className="clarify-recommended"> (Recommended)</span>}
              </button>
            ))}
          </div>
        </div>
      ))}
      <button type="button" className="clarify-submit" onClick={handleSubmit} disabled={!allAnswered}>
        Submit answers
      </button>
    </div>
  )
}

// ── Structured JSON response renderer ──────────────────────────────────────────
// The WebUI is a test harness — it displays the raw JSON payload as-is.
// The production NextJS UI handles rich rendering of the same JSON.

function MetaActions({ actions, onSend }) {
  if (!actions || actions.length === 0) return null
  return (
    <div className="meta-actions">
      {actions.map((label, i) => (
        <button key={i} className="meta-action-btn" onClick={() => onSend && onSend(label)}>
          {label}
        </button>
      ))}
    </div>
  )
}

// Fields auto-added by execute.js to every response — not useful to display as raw JSON.
// They're already surfaced via the preview table, download button, and action buttons.
const EXECUTE_OPERATIONAL_FIELDS = new Set([
  'next_actions', 'stored', 'message', 'available_fields',
  'schema_summary', 'preview', 'status', 'pct_complete',
  'fetched', 'resultset_id', 'segmentId', 'segmentName',
  'returned', 'total', 'deduplicated', 'strategy',
  // Download card fields — surfaced via DownloadCard component
  'download_url', 'filename', 'row_count', 'column_count',
])

function DownloadCard({ url, filename, rowCount, columnCount }) {
  if (!url) return null
  const label = filename || 'download.csv'
  const meta = [
    rowCount    != null && `${Number(rowCount).toLocaleString()} rows`,
    columnCount != null && `${columnCount} columns`,
  ].filter(Boolean).join(' · ')
  return (
    <a
      href={url}
      className="download-card"
      target="_blank"
      rel="noopener noreferrer"
      download={label}
    >
      <span className="download-card-icon">⬇</span>
      <span className="download-card-body">
        <span className="download-card-name">{label}</span>
        {meta && <span className="download-card-meta">{meta}</span>}
      </span>
    </a>
  )
}

function StructuredResponse({ structured, sessionId, datasetIndex, gatewayUrl, resultsetId, onSendMessage }) {
  const { meta, text, error, ...dataPayload } = structured || {}
  const nextActions = meta?.next_actions || []

  // Strip operational MCP fields — surfaced via preview table / buttons / markdown
  const displayPayload = Object.fromEntries(
    Object.entries(dataPayload).filter(([k]) => !EXECUTE_OPERATIONAL_FIELDS.has(k))
  )
  const hasDisplayPayload = Object.keys(displayPayload).length > 0

  // Show preview table when execute.js stored result data, UNLESS the text
  // already contains a [Preview](#preview-last) sentinel (handled inline below).
  const hasResultData = !!(dataPayload.resultset_id ||
    (Array.isArray(dataPayload.preview) && dataPayload.preview.length > 0))
  const hasPreviewInText = typeof text === 'string' && PREVIEW_SENTINEL_RE.test(text)
  const showPreviewTable = hasResultData && !hasPreviewInText

  // When text contains the sentinel, split it and insert <PreviewTable> as a
  // React component — same approach MessageBubble uses in the non-structured path.
  const [textBefore, textAfter] = (() => {
    if (!hasPreviewInText) return [text, null]
    const match = text.match(/([\s\S]*?)\[Preview\]\(#preview-last\)([\s\S]*)/i)
    return match ? [match[1].trim(), match[2].trim()] : [text, null]
  })()

  return (
    <div className="structured-response">
      {/* Text — split at preview sentinel if present */}
      {hasPreviewInText ? (
        <>
          {textBefore && (
            <div className="msg-markdown"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(textBefore, sessionId, datasetIndex, gatewayUrl, resultsetId) }} />
          )}
          <PreviewTable
            sessionId={sessionId}
            which={String(datasetIndex)}
            gatewayUrl={gatewayUrl}
            inlineRows={Array.isArray(dataPayload.preview) && dataPayload.preview.length > 0 ? dataPayload.preview : undefined}
            inlineTotal={dataPayload.total}
          />
          {textAfter && (
            <div className="msg-markdown"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(textAfter, sessionId, datasetIndex, gatewayUrl, resultsetId) }} />
          )}
        </>
      ) : (
        text && (
          <div className="msg-markdown"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(text, sessionId, datasetIndex, gatewayUrl, resultsetId) }} />
        )
      )}
      {/* Preview table — shown when result data exists and no inline sentinel */}
      {showPreviewTable && (
        <PreviewTable
          sessionId={sessionId}
          which={String(datasetIndex)}
          gatewayUrl={gatewayUrl}
          inlineRows={Array.isArray(dataPayload.preview) && dataPayload.preview.length > 0 ? dataPayload.preview : undefined}
          inlineTotal={dataPayload.total}
        />
      )}
      {/* Download card — shown when MCP export returns a download_url */}
      {dataPayload.download_url && (
        <DownloadCard
          url={dataPayload.download_url}
          filename={dataPayload.filename}
          rowCount={dataPayload.row_count}
          columnCount={dataPayload.column_count}
        />
      )}
      {error && <p className="structured-error">{error}</p>}
      {hasDisplayPayload && (
        <pre className="json-payload">{JSON.stringify(displayPayload, null, 2)}</pre>
      )}
      <MetaActions actions={nextActions} onSend={onSendMessage} />
    </div>
  )
}

function MessageBubble({ msg, sessionId, datasetIndex = 'last', gatewayUrl = GATEWAY, onSendMessage, resultsetId = null }) {
  const { role, content, progress, streaming, error, structured } = msg
  const isUser = role === 'user'
  const contentRef = useRef(null)

  const clarifyQuestions = !isUser && typeof content === 'string' ? parseClarifyBlock(content) : null
  const contentWithoutClarify = clarifyQuestions ? stripClarifyBlock(content) : content

  // After dangerouslySetInnerHTML renders, wire up any preview sentinels
  const hasPreviewSentinel = !isUser && typeof contentWithoutClarify === 'string' && PREVIEW_SENTINEL_RE.test(contentWithoutClarify)

  const [beforeSentinel, afterSentinel] = (() => {
    if (!hasPreviewSentinel) return [contentWithoutClarify, null]
    const match = contentWithoutClarify.match(/([\s\S]*?)\[Preview\]\(#preview-last\)([\s\S]*)/i)
    return match ? [match[1].trim(), match[2].trim()] : [contentWithoutClarify, null]
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
        ) : structured && !streaming ? (
          // Structured JSON response — render blocks + next_actions
          <StructuredResponse
            structured={structured}
            sessionId={sessionId}
            datasetIndex={datasetIndex}
            gatewayUrl={gatewayUrl}
            resultsetId={resultsetId}
            onSendMessage={onSendMessage}
          />
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
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(beforeSentinel, sessionId, datasetIndex, gatewayUrl, resultsetId) }}
                  />
                )}
                <PreviewTable sessionId={sessionId} which={datasetIndex} gatewayUrl={gatewayUrl} />
                {afterSentinel && (
                  <div
                    className="msg-markdown"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(afterSentinel, sessionId, datasetIndex, gatewayUrl, resultsetId) }}
                  />
                )}
              </>
            ) : (
              <div
                ref={contentRef}
                className="msg-markdown"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(contentWithoutClarify, sessionId, datasetIndex, gatewayUrl, resultsetId) }}
              />
            )}
            {clarifyQuestions && clarifyQuestions.length > 0 && (
              <ClarificationForm questions={clarifyQuestions} onSend={onSendMessage} />
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
