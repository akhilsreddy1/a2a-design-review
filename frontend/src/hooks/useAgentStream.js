// Event-driven streaming against the bridge (api/server.py):
//   1. open an SSE subscription scoped to a conversation_id
//   2. once connected, POST /api/run to start the orchestration
//   3. typed events arrive on the SSE channel and are handed to onEvent
//
// The run is POSTed exactly once (guarded), even though EventSource may
// auto-reconnect on a transient drop. Nothing bypasses the bus: every event
// the UI renders was published by orchestration onto the central event bus.
export function useAgentStream(onEvent) {
  return ({ query, conversationId, sessionId, pinnedAgent }) => {
    const es = new EventSource(`/api/stream/${encodeURIComponent(conversationId)}`)
    let started = false

    es.onopen = () => {
      if (started) return // avoid a duplicate run on auto-reconnect
      started = true
      fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query,
          conversation_id: conversationId,
          session_id: sessionId,
          pinned_agent: pinnedAgent || null,
        }),
      })
        .then((r) => {
          if (!r.ok)
            onEvent({ event_type: 'agent_failed', agent_name: 'orchestrator', error: `Run failed (HTTP ${r.status})` })
        })
        .catch(() =>
          onEvent({ event_type: 'agent_failed', agent_name: 'orchestrator', error: 'Failed to start run' }),
        )
    }

    es.onmessage = (e) => {
      try {
        onEvent(JSON.parse(e.data))
      } catch {
        /* ignore keep-alive comments / malformed frames */
      }
    }

    es.onerror = () => {
      // EventSource auto-reconnects; only surface if we never connected.
      if (!started) {
        es.close()
        onEvent({ event_type: 'agent_failed', agent_name: 'orchestrator', error: 'Connection lost' })
      }
    }

    return () => es.close()
  }
}
