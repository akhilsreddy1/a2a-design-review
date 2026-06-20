import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useAgentStream } from './hooks/useAgentStream.js'
import AgentStrip from './components/AgentStrip.jsx'
import TopBar from './components/TopBar.jsx'
import StreamArea from './components/StreamArea.jsx'
import FlowTrack from './components/FlowTrack.jsx'
import InputRow from './components/InputRow.jsx'
import StatusPanel from './components/StatusPanel.jsx'
import QuestionTabs from './components/QuestionTabs.jsx'

let handoffSeq = 0

export default function App() {
  const [agents, setAgents] = useState([]) // [{id,label,role,color,icon,framework,model}]
  const [status, setStatus] = useState({}) // id -> idle|working|done|error
  const [handoffs, setHandoffs] = useState([]) // [{id,from,to,task,method,confidence,reason,n}]
  const [activeAgent, setActiveAgent] = useState(null)
  const [query, setQuery] = useState('')
  const [running, setRunning] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [errorMsg, setErrorMsg] = useState(null)
  const [pinnedAgent, setPinnedAgent] = useState(null) // null = auto-route
  const [debate, setDebate] = useState(false)          // structured multi-turn debate

  const [peerTokens, setPeerTokens] = useState({}) // spanId -> "accumulated text"
  const peerSpanIds = useRef(new Set())             // span IDs belonging to peer handoffs

  // Completed runs become tabs. activeTabId = which frozen run is being viewed
  // (null while a run is live / before any run).
  const [tabs, setTabs] = useState([]) // [{id,question,answer,handoffs,peerTokens,status,error,ts}]
  const [activeTabId, setActiveTabId] = useState(null)
  // Set true on finish; a post-commit effect does the actual snapshot so it
  // reads the FINAL handoffs/peerTokens/status/answer (not a stale mid-batch
  // read — the run's tail arrives as one React batch).
  const [pendingSnap, setPendingSnap] = useState(false)
  const snapshottedRef = useRef(false) // guards against double-snapshot per run

  // Append-only answer plumbing: tokens write straight to a single DOM node so
  // the panel is not re-rendered per character. answerBuffer mirrors the full
  // text so it survives re-registration AND can be snapshotted into a tab.
  const answerEl = useRef(null)
  const answerBuffer = useRef('')
  const scrollRef = useRef(null)
  const cleanupRef = useRef(null)
  const startRef = useRef(0)
  const sessionId = useRef(crypto.randomUUID())

  const agentById = useMemo(() => {
    const m = {}
    for (const a of agents) m[a.id] = a
    return m
  }, [agents])

  const registerAnswerEl = useCallback((el) => {
    if (!el) {
      answerEl.current = null
      return
    }
    el.textContent = answerBuffer.current
    answerEl.current = el
  }, [])

  const appendAnswer = useCallback((text) => {
    if (!text) return
    answerBuffer.current += text // authoritative copy (used for the tab snapshot)
    if (answerEl.current) answerEl.current.textContent += text
    const sc = scrollRef.current
    if (sc) sc.scrollTop = sc.scrollHeight
  }, [])

  // ── Fetch agent roster on load ──────────────────────────────────────────────
  useEffect(() => {
    let alive = true
    fetch('/api/agents')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data) => {
        if (!alive || !Array.isArray(data)) return
        setAgents(data)
        setStatus(Object.fromEntries(data.map((a) => [a.id, 'idle'])))
      })
      .catch((err) => alive && setErrorMsg(`Failed to load agents: ${err.message}`))
    return () => {
      alive = false
    }
  }, [])

  // ── Count-up timer ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!running) return
    const tick = () => setElapsed((Date.now() - startRef.current) / 1000)
    const id = setInterval(tick, 100)
    return () => clearInterval(id)
  }, [running])

  const finish = useCallback(() => {
    setRunning(false)
    setActiveAgent(null)
    if (cleanupRef.current) {
      cleanupRef.current()
      cleanupRef.current = null
    }
    setPendingSnap(true) // defer the snapshot to the post-commit effect below
  }, [])

  // Snapshot the just-finished run into a tab AFTER React has committed the
  // final events — so handoffs, peerTokens, status and the answer buffer all
  // reflect the run's end, not a stale read taken mid-batch.
  useEffect(() => {
    if (!pendingSnap) return
    setPendingSnap(false)
    if (snapshottedRef.current || !query) return
    snapshottedRef.current = true
    const snap = {
      id: crypto.randomUUID(),
      question: query,
      answer: answerBuffer.current,
      handoffs,
      peerTokens,
      status: { ...status },
      error: errorMsg || null,
      elapsed,
      ts: Date.now(),
    }
    setTabs((prev) => [...prev, snap])
    setActiveTabId(snap.id)
  }, [pendingSnap, query, handoffs, peerTokens, status, errorMsg, elapsed])

  // Consume the typed event stream (see observability/events.py).
  const onEvent = useCallback(
    (ev) => {
      const agent = ev.agent_name
      switch (ev.event_type) {
        case 'workflow_started':
          break

        case 'agent_handoff': {
          const id = `h${++handoffSeq}`
          setHandoffs((prev) => [
            ...prev,
            {
              id,
              from: ev.from_agent,
              to: ev.to_agent,
              task: ev.task,
              method: ev.method,
              confidence: ev.confidence,
              reason: ev.reason,
              n: prev.length + 1,
              spanId: ev.span_id,
              parentSpanId: ev.parent_span_id,
              toSpanId: ev.to_span_id || null,
            },
          ])
          // Peer consults AND debate reviewer turns render under their handoff
          // line, keyed by to_span_id — their tokens go to per-span panels, not
          // the main answer (the debate's final report streams to the main panel).
          if ((ev.method === 'peer' || ev.method === 'debate') && ev.to_span_id) {
            peerSpanIds.current.add(ev.to_span_id)
          }
          setActiveAgent(ev.to_agent)
          setStatus((s) => ({ ...s, [ev.to_agent]: 'working' }))
          break
        }

        case 'agent_started':
          if (agent) {
            setStatus((s) => ({ ...s, [agent]: 'working' }))
            setActiveAgent(agent)
          }
          break

        case 'token_stream': {
          const sid = ev.span_id
          if (sid && peerSpanIds.current.has(sid)) {
            setPeerTokens((prev) => ({ ...prev, [sid]: (prev[sid] || '') + (ev.text || '') }))
          } else {
            appendAnswer(ev.text || '')
          }
          if (agent) setActiveAgent(agent)
          break
        }

        case 'agent_completed':
          if (agent) setStatus((s) => ({ ...s, [agent]: 'done' }))
          break

        case 'agent_failed':
          if (agent) setStatus((s) => ({ ...s, [agent]: 'error' }))
          setErrorMsg(ev.error || 'Run failed')
          finish()
          break

        case 'workflow_completed':
          finish()
          break

        default:
          break
      }
    },
    [appendAnswer, finish],
  )

  const startStream = useAgentStream(onEvent)

  const resetState = useCallback(() => {
    if (cleanupRef.current) {
      cleanupRef.current()
      cleanupRef.current = null
    }
    answerBuffer.current = ''
    if (answerEl.current) answerEl.current.textContent = ''
    handoffSeq = 0
    snapshottedRef.current = false
    setPendingSnap(false)
    setHandoffs([])
    setPeerTokens({})
    peerSpanIds.current.clear()
    setActiveAgent(null)
    setElapsed(0)
    setErrorMsg(null)
    setRunning(false)
    setStatus((s) => Object.fromEntries(Object.keys(s).map((k) => [k, 'idle'])))
  }, [])

  const handleRun = useCallback(
    (q) => {
      const trimmed = q.trim()
      if (!trimmed || running) return
      resetState()
      setActiveTabId(null) // switch back to the live view
      setQuery(trimmed)
      startRef.current = Date.now()
      setElapsed(0)
      setRunning(true)
      const conversationId = crypto.randomUUID() // fresh per run → isolated SSE channel
      cleanupRef.current = startStream({
        query: trimmed,
        conversationId,
        sessionId: sessionId.current,
        pinnedAgent: debate ? null : pinnedAgent, // debate ignores routing/pin
        mode: debate ? 'debate' : 'route',
        turns: debate ? 5 : undefined,
      })
    },
    [running, resetState, startStream, pinnedAgent, debate],
  )

  const handleNewRun = useCallback(() => {
    resetState()
    setActiveTabId(null)
    setQuery('')
  }, [resetState])

  // ── View resolution: live run, or a frozen tab ──────────────────────────────
  const viewingTab = !running && activeTabId ? tabs.find((t) => t.id === activeTabId) : null
  const vHandoffs = viewingTab ? viewingTab.handoffs : handoffs
  const vPeerTokens = viewingTab ? viewingTab.peerTokens : peerTokens
  const vStatus = viewingTab ? viewingTab.status : status
  const vError = viewingTab ? viewingTab.error : errorMsg
  const vActiveAgent = viewingTab ? null : activeAgent
  const vQuery = viewingTab ? viewingTab.question : query
  const vElapsed = viewingTab ? viewingTab.elapsed || 0 : elapsed
  const frozenAnswer = viewingTab ? viewingTab.answer : null

  return (
    <div className="flex h-full" style={{ background: 'var(--bg)' }}>
      {/* Main content column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar query={vQuery} elapsed={vElapsed} running={running} onNewRun={handleNewRun} />
        <AgentStrip
          agents={agents}
          status={vStatus}
          activeAgent={vActiveAgent}
          pinnedAgent={pinnedAgent}
          onPin={setPinnedAgent}
        />
        <QuestionTabs
          tabs={tabs}
          activeId={activeTabId}
          onSelect={setActiveTabId}
          running={running}
          liveQuestion={query}
        />
        <StreamArea
          ref={scrollRef}
          handoffs={vHandoffs}
          peerTokens={vPeerTokens}
          status={vStatus}
          activeAgent={vActiveAgent}
          running={running && !viewingTab}
          agentById={agentById}
          registerAnswerEl={viewingTab ? undefined : registerAnswerEl}
          errorMsg={vError}
          frozenAnswer={frozenAnswer}
        />
        <FlowTrack blocks={vHandoffs} status={vStatus} activeAgent={vActiveAgent} agentById={agentById} />
        <InputRow
          running={running}
          onRun={handleRun}
          debate={debate}
          onToggleDebate={() => setDebate((d) => !d)}
        />
      </div>

      {/* Right status panel */}
      <StatusPanel
        agents={agents}
        status={vStatus}
        activeAgent={vActiveAgent}
        running={running}
        elapsed={vElapsed}
        handoffs={vHandoffs}
      />
    </div>
  )
}
