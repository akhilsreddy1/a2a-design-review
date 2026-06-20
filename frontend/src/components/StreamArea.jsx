import { forwardRef, useState } from 'react'
import { CheckIcon } from './StatusBadge.jsx'

function Dot({ color, icon }) {
  if (icon) return <span className="flex-shrink-0 text-sm leading-none">{icon}</span>
  return <span className="h-2 w-2 flex-shrink-0 rounded-full" style={{ background: color || 'var(--text-faint)' }} />
}

// Collapsible peer answer that sits under a peer handoff line.
function PeerAnswer({ text, agentLabel, color, icon, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  if (!text) return null

  const preview = text.slice(0, 120).replace(/\n/g, ' ').trim()
  const tint = color || 'var(--text-muted)'

  return (
    <div className="ml-7 mt-1.5 mb-0.5">
      <button
        onClick={() => setOpen((o) => !o)}
        className="group flex w-full items-start gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors"
        style={{
          background: open
            ? `color-mix(in srgb, ${tint} 8%, transparent)`
            : 'transparent',
          border: 'none',
          cursor: 'pointer',
        }}
      >
        <span
          className="mt-px flex-shrink-0 text-[10px]"
          style={{
            color: tint,
            display: 'inline-block',
            transform: open ? 'rotate(90deg)' : 'rotate(0)',
            transition: 'transform 0.15s',
          }}
        >
          ▶
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5 text-[11px] font-semibold" style={{ color: tint }}>
            {icon && <span className="text-xs leading-none">{icon}</span>}
            {agentLabel}
            <span
              className="mono rounded-full px-1.5 py-px text-[9px] font-normal"
              style={{
                background: `color-mix(in srgb, ${tint} 12%, transparent)`,
                color: `color-mix(in srgb, ${tint} 80%, var(--text-faint))`,
              }}
            >
              {text.length.toLocaleString()} chars
            </span>
          </span>
          {!open && (
            <span
              className="mt-0.5 block truncate text-[11px] leading-snug"
              style={{ color: 'var(--text-faint)' }}
            >
              {preview}…
            </span>
          )}
        </span>
      </button>
      {open && (
        <pre
          className="mono mt-1 whitespace-pre-wrap break-words rounded-lg px-3 py-2.5 text-[11px] leading-relaxed"
          style={{
            color: 'var(--text)',
            background: `color-mix(in srgb, ${tint} 4%, var(--bg))`,
            borderLeft: `2px solid ${tint}`,
            maxHeight: '280px',
            overflowY: 'auto',
          }}
        >
          {text}
        </pre>
      )}
    </div>
  )
}

// One compact line per handoff (not a separate card). All handoffs for a run
// live in the same response panel, stacked as a trail. Shows the routing
// decision (method · confidence) and the reason the agent was picked.
function HandoffLine({ handoff, agentById, status, activeAgent, peerText }) {
  const from = agentById[handoff.from]
  const to = agentById[handoff.to]
  const toState = status[handoff.to]
  const isActive = activeAgent === handoff.to && toState !== 'done'
  const conf = typeof handoff.confidence === 'number' ? handoff.confidence.toFixed(2) : null
  const isPeer = handoff.method === 'peer'
  const isDebate = handoff.method === 'debate'

  return (
    <div className="flex animate-fade-up flex-col gap-0.5 py-1">
      <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--text-muted)' }}>
        <span
          className="mono rounded px-1.5 py-0.5 text-[10px] font-semibold"
          style={{ background: 'var(--pill-bg)', color: 'var(--text-faint)' }}
        >
          {handoff.n}
        </span>
        <Dot color={from?.color} icon={from?.icon} />
        <span style={{ color: 'var(--text)' }}>{from?.label || handoff.from}</span>
        <span style={{ color: 'var(--text-faint)' }}>→</span>
        <Dot color={to?.color} icon={to?.icon} />
        <span className="font-semibold" style={{ color: isActive ? to?.color : 'var(--text)' }}>
          {to?.label || handoff.to}
        </span>
        {handoff.method && (
          <span
            className="mono rounded px-1.5 py-0.5 text-[10px]"
            style={{ background: 'var(--pill-bg)', color: 'var(--text-faint)' }}
          >
            {handoff.method}
            {conf ? ` · ${conf}` : ''}
          </span>
        )}
        {toState === 'done' && (
          <span style={{ color: 'var(--ok)' }}>
            <CheckIcon />
          </span>
        )}
        {toState === 'error' && <span style={{ color: 'var(--danger)' }}>!</span>}
        {isActive && <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full" style={{ background: to?.color }} />}
      </div>
      {handoff.reason && (
        <div className="pl-7 text-[11px] leading-snug" style={{ color: 'var(--text-faint)' }}>
          {handoff.reason}
        </div>
      )}
      {(isPeer || isDebate) && peerText && (
        <PeerAnswer
          text={peerText}
          agentLabel={to?.label || handoff.to}
          color={to?.color}
          icon={to?.icon}
          defaultOpen={isDebate}
        />
      )}
    </div>
  )
}

// A single response panel: the handoff trail on top, the streamed answer below.
const StreamArea = forwardRef(function StreamArea(
  { handoffs, peerTokens, status, activeAgent, running, agentById, registerAnswerEl, errorMsg, frozenAnswer },
  ref,
) {
  const isFrozen = frozenAnswer != null
  const hasRun = running || handoffs.length > 0 || isFrozen

  return (
    <div ref={ref} className="flex-1 overflow-y-auto px-6 py-5">
      {!hasRun && !errorMsg && (
        <div className="mt-20 text-center">
          <div
            className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl text-xl"
            style={{
              background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
              color: '#fff',
              boxShadow: '0 4px 16px color-mix(in srgb, var(--accent) 20%, transparent)',
            }}
          >
            A
          </div>
          <div className="text-sm font-semibold" style={{ color: 'var(--text-muted)' }}>
            Ask any design review question and it will be answered by a team of specialists.
          </div>
          <div className="mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
            The router picks the best specialist — the answer streams back live
          </div>
        </div>
      )}

      {hasRun && (
        <div
          className="animate-fade-up w-full rounded-2xl"
          style={{
            background: 'var(--bg-elev)',
            border: '1px solid var(--border)',
            boxShadow: '0 2px 12px rgba(0,0,0,0.04)',
          }}
        >
          {handoffs.length > 0 && (
            <div className="flex flex-col px-4 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
              {handoffs.map((h) => (
                <HandoffLine
                  key={h.id}
                  handoff={h}
                  agentById={agentById}
                  status={status}
                  activeAgent={activeAgent}
                  peerText={h.toSpanId ? (peerTokens || {})[h.toSpanId] : null}
                />
              ))}
            </div>
          )}

          <pre
            className="mono m-0 whitespace-pre-wrap break-words px-5 py-5 text-[13px] leading-relaxed"
            style={{ color: 'var(--text)' }}
          >
            {isFrozen ? (
              frozenAnswer
            ) : (
              <>
                <code ref={registerAnswerEl} />
                {running && (
                  <span className="animate-blink" style={{ color: 'var(--accent)' }}>
                    ▍
                  </span>
                )}
              </>
            )}
          </pre>
        </div>
      )}

      {errorMsg && (
        <div
          className="animate-fade-up mt-3 rounded-xl px-4 py-3 text-sm"
          style={{
            background: 'color-mix(in srgb, var(--danger) 6%, var(--bg-elev))',
            border: '1px solid color-mix(in srgb, var(--danger) 25%, transparent)',
            color: 'var(--danger)',
          }}
        >
          {errorMsg}
        </div>
      )}
    </div>
  )
})

export default StreamArea
