import { useEffect, useRef, useState } from 'react'

const ACCENT = 'var(--accent)'

export default function InputRow({ running, onRun, debate, onSetDebate, turns, onSetTurns }) {
  const [value, setValue] = useState('')
  const wasRunning = useRef(false)

  useEffect(() => {
    if (wasRunning.current && !running) setValue('')
    wasRunning.current = running
  }, [running])

  const submit = (e) => {
    e?.preventDefault?.()
    if (running || !value.trim()) return
    onRun(value)
  }

  // In the debate textarea, plain Enter inserts a newline; ⌘/Ctrl+Enter submits.
  const onKeyDown = (e) => {
    if (debate && (e.metaKey || e.ctrlKey) && e.key === 'Enter') submit(e)
  }

  return (
    <div
      className="glass flex flex-shrink-0 flex-col gap-2.5 px-5 py-3"
      style={{
        borderTop: `1px solid ${debate ? `color-mix(in srgb, ${ACCENT} 45%, var(--border))` : 'var(--border)'}`,
        background: debate ? `color-mix(in srgb, ${ACCENT} 5%, transparent)` : undefined,
      }}
    >
      {/* ── Mode switch ──────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2">
        <div
          className="flex rounded-lg p-0.5 text-xs font-semibold"
          style={{ background: 'var(--bg-elev-2)', border: '1px solid var(--border)' }}
        >
          {[
            ['review', 'Review'],
            ['debate', '⚖ Debate'],
          ].map(([m, label]) => {
            const isDebateSeg = m === 'debate'
            const active = isDebateSeg === !!debate
            return (
              <button
                key={m}
                type="button"
                disabled={running}
                onClick={() => !running && onSetDebate(isDebateSeg)}
                className="rounded-md px-3 py-1 transition-colors"
                style={{
                  background: active ? (isDebateSeg ? ACCENT : 'var(--bg-elev)') : 'transparent',
                  color: active ? (isDebateSeg ? '#fff' : 'var(--text)') : 'var(--text-faint)',
                  cursor: running ? 'not-allowed' : 'pointer',
                  boxShadow: active && !isDebateSeg ? '0 1px 3px rgba(0,0,0,0.06)' : 'none',
                }}
              >
                {label}
              </button>
            )
          })}
        </div>

        {debate && (
          <div className="flex flex-wrap items-center gap-2 text-[11px]" style={{ color: 'var(--text-muted)' }}>
            <span>
              The full panel debates your design →{' '}
              <strong style={{ color: ACCENT }}>one structured report</strong>
            </span>
            <span style={{ color: 'var(--text-faint)' }}>·</span>
            <span>Turns</span>
            <div className="flex gap-1">
              {[3, 4, 5, 6].map((t) => (
                <button
                  key={t}
                  type="button"
                  disabled={running}
                  onClick={() => onSetTurns(t)}
                  className="mono rounded px-1.5 py-0.5 font-semibold transition-colors"
                  style={{
                    background: turns === t ? `color-mix(in srgb, ${ACCENT} 16%, transparent)` : 'transparent',
                    color: turns === t ? ACCENT : 'var(--text-faint)',
                    border: `1px solid ${turns === t ? `color-mix(in srgb, ${ACCENT} 35%, var(--border))` : 'transparent'}`,
                    cursor: running ? 'not-allowed' : 'pointer',
                  }}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* ── Input + submit ───────────────────────────────────────────── */}
      <form onSubmit={submit} className="flex items-end gap-3">
        {debate ? (
          <textarea
            value={value}
            disabled={running}
            rows={2}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Paste a design, RFC, or proposal — the specialist panel will debate it  (⌘/Ctrl+Enter to start)…"
            className="mono flex-1 resize-none rounded-xl px-4 py-2.5 text-sm leading-relaxed outline-none disabled:opacity-50"
            style={{
              background: 'var(--bg)',
              border: `1px solid color-mix(in srgb, ${ACCENT} 30%, var(--border))`,
              color: 'var(--text)',
              minHeight: 58,
              maxHeight: 170,
            }}
          />
        ) : (
          <input
            type="text"
            value={value}
            disabled={running}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Ask the team: design, security, performance, testing, devops, or code review…"
            className="mono flex-1 rounded-xl px-4 py-2.5 text-sm outline-none disabled:opacity-50"
            style={{
              background: 'var(--bg)',
              border: '1px solid var(--border)',
              color: 'var(--text)',
              boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.04)',
            }}
          />
        )}
        <button
          type="submit"
          disabled={running || !value.trim()}
          className="flex flex-shrink-0 items-center gap-1.5 rounded-xl px-5 py-2.5 text-sm font-bold disabled:cursor-not-allowed disabled:opacity-40"
          style={{
            background: debate ? ACCENT : 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: '#fff',
            boxShadow: running ? 'none' : `0 2px 12px color-mix(in srgb, ${ACCENT} 30%, transparent)`,
          }}
        >
          {running
            ? debate
              ? 'Debating…'
              : 'Running…'
            : debate
              ? '⚖ Start Debate'
              : 'Run'}
        </button>
      </form>
    </div>
  )
}
