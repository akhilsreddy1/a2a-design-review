// A horizontal bar of past questions. Each completed run becomes a tab; clicking
// one restores that frozen Q&A (handoff trail + peer panels + answer). While a
// run is in progress it shows as a live pill at the end of the bar; on completion
// it converts into a real, selectable tab.

function truncate(s, n = 32) {
  const t = (s || '').trim()
  return t.length > n ? `${t.slice(0, n).trimEnd()}…` : t
}

export default function QuestionTabs({ tabs, activeId, onSelect, running, liveQuestion }) {
  const items = running
    ? [...tabs, { id: 'live', question: liveQuestion, status: 'running' }]
    : tabs
  if (items.length === 0) return null

  const effectiveActive = running ? 'live' : activeId

  return (
    <div
      className="flex flex-shrink-0 items-center gap-1 overflow-x-auto px-5 py-1.5"
      style={{ background: 'var(--bg-elev-2)', borderBottom: '1px solid var(--border)' }}
    >
      <span
        className="mono mr-1.5 flex-shrink-0 text-[9px] font-semibold uppercase tracking-[0.14em]"
        style={{ color: 'var(--text-faint)' }}
      >
        Questions
      </span>
      {items.map((t, i) => {
        const active = effectiveActive === t.id
        const isLive = t.id === 'live'
        const isError = t.status === 'error'
        const disabled = running && !isLive
        return (
          <button
            key={t.id}
            onClick={() => !disabled && onSelect(t.id)}
            title={t.question}
            className="flex flex-shrink-0 items-center gap-1.5 rounded-lg px-2 py-1 text-[11px] font-medium transition-all"
            style={{
              cursor: disabled ? 'not-allowed' : 'pointer',
              background: active ? 'var(--bg-elev)' : 'transparent',
              border: `1px solid ${
                active ? 'color-mix(in srgb, var(--accent) 28%, var(--border))' : 'transparent'
              }`,
              color: active ? 'var(--text)' : 'var(--text-muted)',
              boxShadow: active
                ? '0 1px 5px color-mix(in srgb, var(--accent) 10%, rgba(0,0,0,0.04))'
                : 'none',
              opacity: disabled ? 0.5 : 1,
            }}
          >
            <span
              className="mono flex h-4 min-w-4 flex-shrink-0 items-center justify-center rounded px-1 text-[9px] font-semibold"
              style={{
                background: active
                  ? 'color-mix(in srgb, var(--accent) 12%, transparent)'
                  : 'var(--pill-bg)',
                color: active ? 'var(--accent)' : 'var(--text-faint)',
              }}
            >
              {i + 1}
            </span>
            {isLive && (
              <span
                className="h-1.5 w-1.5 animate-pulse-dot rounded-full"
                style={{ background: 'var(--accent)' }}
              />
            )}
            {isError && <span className="text-[10px] font-bold" style={{ color: 'var(--danger)' }}>!</span>}
            <span className="max-w-[180px] truncate">{truncate(t.question)}</span>
          </button>
        )
      })}
    </div>
  )
}
