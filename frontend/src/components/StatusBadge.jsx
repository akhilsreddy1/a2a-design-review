// Status badge: idle (muted) / working (pulsing dot in agent color) /
// done (check) / error (red). Agent color is passed in from /api/agents.
export default function StatusBadge({ state, color }) {
  if (state === 'working') {
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-semibold"
        style={{ color, background: 'color-mix(in srgb, ' + color + ' 16%, transparent)' }}
      >
        <span className="h-1.5 w-1.5 rounded-full animate-pulse-dot" style={{ background: color }} />
        working
      </span>
    )
  }
  if (state === 'done') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
        style={{ color: 'var(--ok)', background: 'color-mix(in srgb, var(--ok) 14%, transparent)' }}
      >
        <CheckIcon /> done
      </span>
    )
  }
  if (state === 'error') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
        style={{ color: 'var(--danger)', background: 'color-mix(in srgb, var(--danger) 16%, transparent)' }}
      >
        error
      </span>
    )
  }
  return (
    <span
      className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ color: 'var(--text-faint)', background: 'var(--pill-bg)' }}
    >
      idle
    </span>
  )
}

export function CheckIcon({ size = 10 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M3 8.5l3 3 7-7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
