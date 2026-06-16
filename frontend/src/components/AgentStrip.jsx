import { CheckIcon } from './StatusBadge.jsx'

function AgentChip({ agent, state, active, pinned, onPin }) {
  const isWorking = state === 'working'
  const isDone = state === 'done'
  const isError = state === 'error'
  const canPin = agent.id !== 'orchestrator'

  return (
    <button
      onClick={() => canPin && onPin?.(pinned ? null : agent.id)}
      className="group relative flex flex-shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-all"
      style={{
        background: active
          ? `color-mix(in srgb, ${agent.color} 10%, #fff)`
          : 'var(--bg-elev)',
        border: `1.5px solid ${active ? agent.color : 'var(--border)'}`,
        color: active || isWorking ? agent.color : isDone ? 'var(--ok)' : 'var(--text-muted)',
        cursor: canPin ? 'pointer' : 'default',
        boxShadow: active
          ? `0 0 0 3px color-mix(in srgb, ${agent.color} 12%, transparent), 0 2px 6px color-mix(in srgb, ${agent.color} 10%, transparent)`
          : '0 1px 2px rgba(0,0,0,0.03)',
      }}
      title={canPin ? (pinned ? 'Click to unpin → back to auto-routing' : `Pin to ${agent.label} — all queries go directly here`) : agent.role}
    >
      {agent.icon && <span className="text-sm leading-none">{agent.icon}</span>}
      <span className="max-w-[80px] truncate">{agent.label}</span>

      {isWorking && (
        <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full" style={{ background: agent.color }} />
      )}
      {isDone && (
        <span style={{ color: 'var(--ok)' }}>
          <CheckIcon size={10} />
        </span>
      )}
      {isError && (
        <span className="text-[10px] font-bold" style={{ color: 'var(--danger)' }}>!</span>
      )}

      {pinned && (
        <span
          className="absolute -right-0.5 -top-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full text-[7px] font-bold"
          style={{
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: '#fff',
            boxShadow: '0 1px 3px rgba(0,0,0,0.15)',
          }}
        >
          P
        </span>
      )}
    </button>
  )
}

function RoutingBadge({ pinnedAgent, pinnedLabel, onClear }) {
  if (pinnedAgent) {
    return (
      <div className="mr-1 flex flex-shrink-0 items-center gap-1">
        <button
          onClick={() => onClear(null)}
          className="flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider"
          style={{
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: '#fff',
            boxShadow: '0 1px 6px color-mix(in srgb, var(--accent) 25%, transparent)',
          }}
          title="Click to clear pin and return to auto-routing"
        >
          <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor">
            <path d="M4 0L5 3H8L5.5 5L6.5 8L4 6L1.5 8L2.5 5L0 3H3L4 0Z" />
          </svg>
          Pinned → {pinnedLabel}
          <span style={{ opacity: 0.7 }}>✕</span>
        </button>
      </div>
    )
  }

  return (
    <div
      className="mr-1 flex flex-shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1"
      style={{ background: 'var(--pill-bg)' }}
      title="LLM router picks the best agent per query. Click any agent to pin."
    >
      <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="var(--text-faint)" strokeWidth="1.2">
        <circle cx="5" cy="5" r="3.5" />
        <path d="M5 2v3l2 1.5" />
      </svg>
      <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
        Auto-route
      </span>
    </div>
  )
}

export default function AgentStrip({ agents, status, activeAgent, pinnedAgent, onPin }) {
  if (agents.length === 0) return null

  const pinnedLabel = pinnedAgent && agents.find((a) => a.id === pinnedAgent)?.label

  const instruction = pinnedAgent
    ? `Pin agent · all queries go to ${pinnedLabel}`
    : 'Auto-Route · router picks the best agent'

  return (
    <div
      className="flex flex-shrink-0 items-center gap-2 overflow-x-auto px-5 py-2.5"
      style={{ background: 'var(--bg-elev)', borderBottom: '1px solid var(--border)' }}
    >
      <div className="mr-0.5 flex flex-shrink-0 flex-col gap-0.5">
        <RoutingBadge pinnedAgent={pinnedAgent} pinnedLabel={pinnedLabel} onClear={onPin} />
        <span className="text-[10px] leading-tight" style={{ color: 'var(--text-faint)' }}>
          {instruction}
        </span>
      </div>
      <span className="h-4 w-px flex-shrink-0" style={{ background: 'var(--border)' }} />
      {agents.map((agent) => (
        <AgentChip
          key={agent.id}
          agent={agent}
          state={status[agent.id] || 'idle'}
          active={activeAgent === agent.id}
          pinned={pinnedAgent === agent.id}
          onPin={onPin}
        />
      ))}
    </div>
  )
}
