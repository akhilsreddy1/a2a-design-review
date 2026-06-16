import { CheckIcon } from './StatusBadge.jsx'

// Pipeline order = orchestrator, then each handoff target in arrival order.
function pipelineIds(blocks) {
  const ids = []
  for (const b of blocks) {
    if (!ids.includes(b.from)) ids.push(b.from)
    if (!ids.includes(b.to)) ids.push(b.to)
  }
  return ids
}

export default function FlowTrack({ blocks, status, activeAgent, agentById }) {
  const ids = pipelineIds(blocks)
  if (ids.length === 0) return null

  return (
    <div
      className="flex flex-shrink-0 items-center gap-2 overflow-x-auto px-5 py-3"
      style={{ background: 'var(--bg-elev)', borderTop: '1px solid var(--border)' }}
    >
      {ids.map((id, i) => {
        const agent = agentById[id]
        const label = agent?.label || id
        const color = agent?.color || 'var(--text-faint)'
        const state = status[id] || 'idle'
        const isCurrent = activeAgent === id && state !== 'done'
        const isDone = state === 'done'

        const border = isCurrent ? color : 'var(--border)'
        const text = isCurrent ? color : isDone ? 'var(--text-muted)' : 'var(--text-faint)'

        return (
          <div key={id} className="flex flex-shrink-0 items-center gap-2">
            <span
              className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold"
              style={{ border: `1px solid ${border}`, color: text, background: 'var(--bg)' }}
            >
              {agent?.icon && <span className="text-sm leading-none">{agent.icon}</span>}
              {isDone && (
                <span style={{ color: 'var(--ok)' }}>
                  <CheckIcon />
                </span>
              )}
              {label}
            </span>
            {i < ids.length - 1 && (
              <span className="connector h-0.5 w-8 rounded-full" style={{ '--fill': isDone ? '100%' : '0%' }} />
            )}
          </div>
        )
      })}
    </div>
  )
}
