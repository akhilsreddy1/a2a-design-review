export default function TopBar({ query, elapsed, running, onNewRun }) {
  return (
    <header
      className="glass flex flex-shrink-0 items-center gap-4 px-5 py-3"
      style={{ borderBottom: '1px solid var(--border)' }}
    >
      <div className="flex flex-shrink-0 items-center gap-2.5">
        <span
          className="flex h-8 w-8 items-center justify-center rounded-lg text-base"
          style={{
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: '#fff',
            boxShadow: '0 2px 8px color-mix(in srgb, var(--accent) 25%, transparent)',
          }}
        >
          A
        </span>
        <div>
          <span className="text-sm font-bold" style={{ color: 'var(--text)' }}>
            Design Review Agent
          </span>
          <div className="text-[10px]" style={{ color: 'var(--text-faint)' }}>
            Multi-Agent Collaboration
          </div>
        </div>
      </div>

      <div className="min-w-0 flex-1 border-l pl-4" style={{ borderColor: 'var(--border)' }}>
        <div className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
          Query
        </div>
        <div className="truncate text-sm" style={{ color: query ? 'var(--text)' : 'var(--text-faint)' }}>
          {query || 'No active run'}
        </div>
      </div>

      <div className="flex flex-shrink-0 items-center gap-3">
        <div className="flex items-center gap-1.5">
          <span
            className="h-2 w-2 rounded-full"
            style={{
              background: running
                ? 'linear-gradient(135deg, var(--accent), var(--ok))'
                : 'var(--border-strong)',
              boxShadow: running ? '0 0 6px color-mix(in srgb, var(--accent) 40%, transparent)' : 'none',
            }}
          />
          <span className="mono text-sm tabular-nums" style={{ color: 'var(--text-muted)' }}>
            {elapsed.toFixed(1)}s
          </span>
        </div>

        <button
          onClick={onNewRun}
          className="flex-shrink-0 rounded-lg px-3 py-1.5 text-xs font-semibold"
          style={{
            background: 'var(--bg-elev)',
            border: '1px solid var(--border)',
            color: 'var(--text-muted)',
            boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
          }}
        >
          New run
        </button>
      </div>
    </header>
  )
}
