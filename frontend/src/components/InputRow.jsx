import { useEffect, useRef, useState } from 'react'

export default function InputRow({ running, onRun, debate, onToggleDebate }) {
  const [value, setValue] = useState('')
  const wasRunning = useRef(false)

  useEffect(() => {
    if (wasRunning.current && !running) {
      setValue('')
    }
    wasRunning.current = running
  }, [running])

  const submit = (e) => {
    e.preventDefault()
    if (running || !value.trim()) return
    onRun(value)
  }

  return (
    <form
      onSubmit={submit}
      className="glass flex flex-shrink-0 items-center gap-3 px-5 py-3"
      style={{ borderTop: '1px solid var(--border)' }}
    >
      <button
        type="button"
        onClick={() => !running && onToggleDebate?.()}
        disabled={running}
        title="Debate mode — a specialist panel debates a design over multiple turns and produces a structured report"
        className="flex flex-shrink-0 items-center gap-1.5 rounded-xl px-3 py-2.5 text-xs font-semibold transition-colors"
        style={{
          border: '1px solid',
          borderColor: debate ? 'var(--accent)' : 'var(--border)',
          background: debate ? 'color-mix(in srgb, var(--accent) 12%, transparent)' : 'var(--bg-elev)',
          color: debate ? 'var(--accent)' : 'var(--text-muted)',
          cursor: running ? 'not-allowed' : 'pointer',
          opacity: running ? 0.5 : 1,
        }}
      >
        <span className="text-sm leading-none">⚖</span>
        Debate{debate ? ' · panel' : ''}
      </button>
      <input
        type="text"
        value={value}
        disabled={running}
        onChange={(e) => setValue(e.target.value)}
        placeholder={
          debate
            ? 'Paste a design or proposal — a specialist panel will debate it and report…'
            : 'Ask the team: design, security, performance, testing, devops, or code review…'
        }
        className="mono flex-1 rounded-xl px-4 py-2.5 text-sm outline-none disabled:opacity-50"
        style={{
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          color: 'var(--text)',
          boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.04)',
        }}
      />
      <button
        type="submit"
        disabled={running || !value.trim()}
        className="rounded-xl px-5 py-2.5 text-sm font-bold disabled:cursor-not-allowed disabled:opacity-40"
        style={{
          background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
          color: '#fff',
          boxShadow: running ? 'none' : '0 2px 12px color-mix(in srgb, var(--accent) 30%, transparent)',
        }}
      >
        {running ? 'Running…' : debate ? 'Debate' : 'Run'}
      </button>
    </form>
  )
}
