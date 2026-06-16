import { CheckIcon } from './StatusBadge.jsx'

function StatusDot({ state, color }) {
  if (state === 'working')
    return <span className="h-2 w-2 animate-pulse-dot rounded-full" style={{ background: color }} />
  if (state === 'done')
    return (
      <span style={{ color: 'var(--ok)' }}>
        <CheckIcon size={10} />
      </span>
    )
  if (state === 'error')
    return <span className="h-2 w-2 rounded-full" style={{ background: 'var(--danger)' }} />
  return <span className="h-2 w-2 rounded-full" style={{ background: 'var(--border-strong)' }} />
}

function MiniAgentRow({ agent, state, active }) {
  return (
    <div
      className="flex items-center gap-2.5 rounded-xl px-2.5 py-2 transition-all"
      style={{
        background: active ? `color-mix(in srgb, ${agent.color} 6%, #fff)` : 'transparent',
        borderLeft: active ? `2px solid ${agent.color}` : '2px solid transparent',
      }}
    >
      <StatusDot state={state} color={agent.color} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          {agent.icon && <span className="text-xs leading-none">{agent.icon}</span>}
          <span
            className="truncate text-xs font-semibold"
            style={{ color: active ? agent.color : 'var(--text)' }}
          >
            {agent.label}
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5">
          {agent.framework && (
            <span
              className="mono rounded-full px-1.5 py-px text-[9px]"
              style={{ background: 'var(--pill-bg)', color: 'var(--text-faint)' }}
            >
              {agent.framework}
            </span>
          )}
          {agent.model && (
            <span className="mono truncate text-[9px]" style={{ color: 'var(--text-faint)' }}>
              {agent.model}
            </span>
          )}
        </div>
      </div>
      <span
        className="flex-shrink-0 rounded-full px-1.5 py-0.5 text-[8px] font-bold uppercase"
        style={{
          color:
            state === 'working' ? agent.color
            : state === 'done' ? 'var(--ok)'
            : state === 'error' ? 'var(--danger)'
            : 'var(--text-faint)',
          background:
            state === 'working' ? `color-mix(in srgb, ${agent.color} 10%, transparent)`
            : state === 'done' ? 'color-mix(in srgb, var(--ok) 10%, transparent)'
            : state === 'error' ? 'color-mix(in srgb, var(--danger) 10%, transparent)'
            : 'transparent',
        }}
      >
        {state}
      </span>
    </div>
  )
}

function StatCard({ value, label, gradient }) {
  return (
    <div
      className="rounded-xl px-2 py-2 text-center"
      style={{
        background: 'var(--bg)',
        boxShadow: 'inset 0 1px 2px rgba(0,0,0,0.03)',
      }}
    >
      <div className="text-base font-extrabold" style={gradient ? {
        background: gradient,
        WebkitBackgroundClip: 'text',
        WebkitTextFillColor: 'transparent',
      } : { color: 'var(--text-muted)' }}>
        {value}
      </div>
      <div className="text-[9px] font-semibold uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>
        {label}
      </div>
    </div>
  )
}

export default function StatusPanel({ agents, status, activeAgent, running, elapsed, handoffs }) {
  const workingCount = Object.values(status).filter((s) => s === 'working').length
  const doneCount = Object.values(status).filter((s) => s === 'done').length
  const peerCount = handoffs.filter((h) => h.method === 'peer').length

  return (
    <aside
      className="glass flex w-56 flex-shrink-0 flex-col overflow-y-auto"
      style={{ borderLeft: '1px solid var(--border)' }}
    >
      {/* Summary stats */}
      <div className="flex flex-col gap-3 px-3 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
            Dashboard
          </span>
          <span
            className="flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold"
            style={{
              background: running
                ? 'linear-gradient(135deg, var(--accent), var(--accent-2))'
                : 'var(--pill-bg)',
              color: running ? '#fff' : 'var(--text-faint)',
              boxShadow: running ? '0 1px 6px color-mix(in srgb, var(--accent) 25%, transparent)' : 'none',
            }}
          >
            {running && <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full" style={{ background: '#fff' }} />}
            {running ? 'Live' : 'Idle'}
          </span>
        </div>

        <div className="grid grid-cols-3 gap-1.5">
          <StatCard
            value={workingCount}
            label="Active"
            gradient="linear-gradient(135deg, var(--accent), var(--accent-2))"
          />
          <StatCard
            value={doneCount}
            label="Done"
            gradient={doneCount > 0 ? 'linear-gradient(135deg, var(--ok), #059669)' : null}
          />
          <StatCard value={peerCount} label="Peers" />
        </div>
      </div>

      {/* Agent roster */}
      <div className="flex-1 px-1.5 py-2">
        <div className="flex flex-col gap-0.5">
          {agents.map((agent) => (
            <MiniAgentRow
              key={agent.id}
              agent={agent}
              state={status[agent.id] || 'idle'}
              active={activeAgent === agent.id}
            />
          ))}
          {agents.length === 0 && (
            <p className="px-2 py-4 text-center text-xs" style={{ color: 'var(--text-faint)' }}>
              Loading agents…
            </p>
          )}
        </div>
      </div>

      {/* Footer */}
      <div
        className="flex items-center justify-between px-3 py-2.5"
        style={{ borderTop: '1px solid var(--border)' }}
      >
        <span className="text-[10px] font-semibold" style={{ color: 'var(--text-faint)' }}>
          {agents.length} agents
        </span>
        <span className="mono text-[10px] tabular-nums" style={{ color: 'var(--text-faint)' }}>
          {elapsed > 0 ? `${elapsed.toFixed(1)}s` : '—'}
        </span>
      </div>
    </aside>
  )
}
