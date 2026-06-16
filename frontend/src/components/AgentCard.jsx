import StatusBadge from './StatusBadge.jsx'

// Status-display card. Active agent gets a 2px left border accent in its own
// color. Shows the agent icon + framework/model chips (from /api/agents).
export default function AgentCard({ agent, state, active, pinned, onPin }) {
  const accent = active ? agent.color : pinned ? agent.color : 'transparent'
  return (
    <div
      className="rounded-lg p-3 transition-colors"
      style={{
        background: 'var(--bg-elev-2)',
        border: '1px solid var(--border)',
        borderLeft: `2px solid ${accent}`,
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          {agent.icon ? (
            <span className="flex-shrink-0 text-base leading-none">{agent.icon}</span>
          ) : (
            <span className="h-2.5 w-2.5 flex-shrink-0 rounded-full" style={{ background: agent.color }} />
          )}
          <span className="truncate text-sm font-semibold" style={{ color: 'var(--text)' }}>
            {agent.label}
          </span>
        </div>
        <StatusBadge state={state} color={agent.color} />
      </div>

      <p className="mt-1.5 line-clamp-2 text-xs leading-snug" style={{ color: 'var(--text-muted)' }}>
        {agent.role}
      </p>

      {(agent.framework || agent.model) && (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {agent.framework && (
            <span
              className="mono rounded px-1.5 py-0.5 text-[10px]"
              style={{ background: 'var(--pill-bg)', color: 'var(--text-faint)' }}
            >
              {agent.framework}
            </span>
          )}
          {agent.model && (
            <span
              className="mono rounded px-1.5 py-0.5 text-[10px]"
              style={{ background: 'var(--pill-bg)', color: 'var(--text-faint)' }}
            >
              {agent.model}
            </span>
          )}
        </div>
      )}

      {onPin && agent.id !== 'orchestrator' && (
        <button
          onClick={() => onPin(pinned ? null : agent.id)}
          className="mt-2 w-full rounded px-2 py-1 text-[10px] font-semibold transition-colors"
          style={{
            background: pinned ? agent.color : 'var(--bg)',
            color: pinned ? 'var(--bg)' : 'var(--text-faint)',
            border: '1px solid var(--border)',
          }}
        >
          {pinned ? 'Pinned — click to unpin' : 'Pin to this agent'}
        </button>
      )}
    </div>
  )
}
