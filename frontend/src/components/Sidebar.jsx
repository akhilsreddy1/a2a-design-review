import AgentCard from './AgentCard.jsx'

export default function Sidebar({ agents, status, activeAgent, pinnedAgent, onPin }) {
  return (
    <aside
      className="flex w-72 flex-shrink-0 flex-col overflow-y-auto p-4"
      style={{ background: 'var(--bg-elev)', borderRight: '1px solid var(--border)' }}
    >
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
          Agents
        </h2>
        <span
          className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
          style={{ background: 'var(--pill-bg)', color: 'var(--text-faint)' }}
        >
          {pinnedAgent ? `pinned: ${pinnedAgent}` : 'auto-route'}
        </span>
      </div>

      <div className="flex flex-col gap-2">
        {agents.map((agent) => (
          <AgentCard
            key={agent.id}
            agent={agent}
            state={status[agent.id] || 'idle'}
            active={activeAgent === agent.id}
            pinned={pinnedAgent === agent.id}
            onPin={onPin}
          />
        ))}
        {agents.length === 0 && (
          <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
            Loading agents…
          </p>
        )}
      </div>
    </aside>
  )
}
