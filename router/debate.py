"""DebateOrchestrator — hybrid structured design-review debate.

A sibling of :class:`router.orchestrator.RouterOrchestrator`: it reuses the same
``LiteLLMRegistry`` and the same event bus, and is opt-in via ``mode="debate"``.
Single-shot review keeps working unchanged.

**Hybrid design.** Deterministic code owns the *skeleton* — the three phases,
the turn budget, parallel reviewer invocation, event emission, transcript
capping, and anti-domination — while the LLM (`complete()`) is called only for
the *judgment* steps it is actually good at:

  - detecting genuine tensions from the opening positions,
  - framing the specific question that advances a tension,
  - judging whether a tension resolved / refined / entrenched,
  - synthesising the final structured report.

Every quote in the report is therefore grounded in a transcript the code
captured, not the model's memory. Reviewers are invoked **unchanged** over A2A;
a per-turn prompt asks them for a structured debate-response shape (stance /
score / what-would-change-my-mind) which the code parses leniently.

**UI mapping.** Each reviewer invocation emits a ``debate`` handoff and streams
that reviewer's content to its own collapsible panel; the synthesised report
streams to the main answer panel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from time import perf_counter

from clients.a2a_client import A2AClient
from common.deadline import DeadlineExceeded, deadline
from common.llm_client import complete
from common.peer_client import PEER_CALL_MARKER
from config import get_settings
from observability.context import ExecutionContext, bind_context
from observability.emitters import (
    emit_agent_completed,
    emit_agent_failed,
    emit_agent_started,
    emit_handoff,
    emit_token,
    emit_workflow_completed,
    emit_workflow_started,
)
from registry import LiteLLMRegistry

logger = logging.getLogger("multi_agent.debate")

ORCHESTRATOR_ID = "orchestrator"

# Default panel (mapped onto the existing registry). Each is a distinct lens.
DEFAULT_PANEL = ["security", "performance", "testing", "devops", "code_reviewer"]
DEFAULT_MAX_TURNS = 6
# Per reviewer/judge call: fresh wall-clock budget (no shared shrinking deadline).
PER_CALL_SECONDS = 150
# Cap each prior contribution fed into a prompt so inputs can't balloon.
MAX_PRIOR_CHARS = 1400
# Model for the orchestrator's own judgment calls (same gateway as the agents).
JUDGE_MODEL = get_settings().default_model


# ── parsing helpers ──────────────────────────────────────────────────────────
def _extract_json_array(text: str) -> list:
    """Pull the first JSON array out of an LLM response (lenient)."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        val = json.loads(m.group(0))
        return val if isinstance(val, list) else []
    except json.JSONDecodeError:
        return []


def _extract_json_object(text: str) -> dict:
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        val = json.loads(m.group(0))
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def _parse_score(text: str) -> int | None:
    m = re.search(r"SCORE:\s*(\d{1,2})", text or "")
    if m:
        try:
            return max(1, min(10, int(m.group(1))))
        except ValueError:
            return None
    return None


def _clip(t: str, n: int = MAX_PRIOR_CHARS) -> str:
    t = (t or "").strip()
    return t if len(t) <= n else t[:n] + " …[trimmed]"


class DebateOrchestrator:
    """Runs a code-driven, LLM-judged structured design-review debate."""

    def __init__(
        self,
        *,
        registry: LiteLLMRegistry | None = None,
        a2a: A2AClient | None = None,
    ) -> None:
        self.registry = registry or LiteLLMRegistry()
        self.a2a = a2a or A2AClient()

    # ── reviewer invocation (unchanged agents, structured-shape prompt) ───────
    async def _invoke(
        self,
        ctx: ExecutionContext,
        reviewer: str,
        url: str,
        prompt: str,
        *,
        label: str,
    ) -> str:
        """Invoke one reviewer over A2A, stream its content to its own panel,
        and return the full text. Failures degrade gracefully (return "")."""
        turn_ctx = ctx.child(workflow_name=reviewer)
        await emit_handoff(
            ctx, from_agent=ORCHESTRATOR_ID, to_agent=reviewer,
            task=label, method="debate", reason=label, to_span_id=turn_ctx.span_id,
        )
        await emit_agent_started(turn_ctx, reviewer)
        # Marker → the agent's no-side-consult path; orchestrator drives all cross-examination.
        wrapped = f"{prompt}\n\n{PEER_CALL_MARKER}"
        chunks: list[str] = []
        final_artifact = ""
        saw = False
        t0 = perf_counter()
        try:
            async with deadline(PER_CALL_SECONDS):
                async for ev in self.a2a.stream_agent(url, wrapped, agent_name=reviewer, ctx=turn_ctx):
                    if ev.kind == "error":
                        await emit_agent_failed(turn_ctx, reviewer, error=ev.text or "stream error")
                        break
                    if ev.text and (ev.phase == "progress" or ev.kind == "token"):
                        saw = True
                        chunks.append(ev.text)
                        await emit_token(turn_ctx, reviewer, ev.text)
                    elif ev.kind == "artifact" and ev.text:
                        final_artifact = ev.text
        except DeadlineExceeded:
            logger.warning("debate.invoke_timeout reviewer=%s label=%s", reviewer, label)
        except Exception as exc:
            logger.exception("debate.invoke_failed reviewer=%s", reviewer)
            await emit_agent_failed(turn_ctx, reviewer, error=str(exc))
            return ""
        text = "".join(chunks)
        if not saw and final_artifact:
            text = final_artifact
            await emit_token(turn_ctx, reviewer, text)
        text = text.strip()
        await emit_agent_completed(
            turn_ctx, reviewer, summary=text[:300], latency_ms=(perf_counter() - t0) * 1000,
        )
        return text

    async def _judge(self, system: str, user: str, *, max_tokens: int = 900) -> str:
        try:
            async with deadline(PER_CALL_SECONDS):
                return await complete(model=JUDGE_MODEL, system=system, user=user, max_tokens=max_tokens)
        except Exception:
            logger.exception("debate.judge_failed")
            return ""

    # ── the debate ────────────────────────────────────────────────────────────
    async def run(
        self,
        ctx: ExecutionContext,
        design: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        panel: list[str] | None = None,
    ) -> None:
        with bind_context(ctx):
            t0 = perf_counter()
            await emit_workflow_started(ctx, query=design)

            try:
                cards = await self.registry.discover_cards()
            except Exception as exc:
                logger.exception("debate.discovery_failed")
                await emit_agent_failed(ctx, ORCHESTRATOR_ID, error=f"Discovery failed: {exc}")
                await emit_workflow_completed(ctx, summary=f"Discovery failed: {exc}"[:400])
                return
            by_name = {c.name: c for c in cards}
            reviewers = [a for a in (panel or DEFAULT_PANEL) if a in by_name] or list(by_name)
            if not reviewers:
                await emit_agent_failed(ctx, ORCHESTRATOR_ID, error="No reviewers available.")
                await emit_workflow_completed(ctx, summary="No reviewers available.")
                return
            max_turns = max(3, min(int(max_turns or DEFAULT_MAX_TURNS), 8))

            # Opening statement → main answer panel.
            await emit_token(
                ctx, ORCHESTRATOR_ID,
                f"# Structured Design-Review Debate\n\n"
                f"**Scope:** {design.strip()[:280]}{'…' if len(design) > 280 else ''}\n\n"
                f"**Panel:** {', '.join(reviewers)}  ·  **Budget:** {max_turns} turns\n\n"
                f"Beginning opening positions…\n\n---\n\n",
            )

            # ── Phase 1 — opening positions (parallel) ──────────────────────────
            opening_prompt = (
                "You are reviewing the design below from your specialty.\n\n"
                f"## Design under review\n{design}\n\n"
                "Give: your top 3 concerns, your top 1 strength, then a score 1-10 from your "
                "specialty's perspective with a one-sentence rationale, and any factual "
                "assumption that — if wrong — would change your view. Be specific and concise. "
                "End with a line exactly:\nSCORE: <n>"
            )
            opens = await asyncio.gather(*[
                self._invoke(ctx, r, by_name[r].url, opening_prompt, label="Opening position")
                for r in reviewers
            ])
            positions = {r: txt for r, txt in zip(reviewers, opens) if txt}
            scores: dict[str, list[int]] = {
                r: [s] for r, txt in positions.items() if (s := _parse_score(txt)) is not None
            }
            if len(positions) < 2:
                await emit_token(ctx, ORCHESTRATOR_ID, "\n_Not enough reviewers responded to debate._\n")
                await emit_workflow_completed(ctx, summary="Debate aborted — too few reviewers.", latency_ms=(perf_counter() - t0) * 1000)
                return

            # ── Tension detection (judge) ───────────────────────────────────────
            positions_blob = "\n\n".join(f"### {r}\n{_clip(t)}" for r, t in positions.items())
            tensions = await self._detect_tensions(design, positions_blob, list(positions))

            # ── Phase 2 — debate rounds (targeted, not round-robin) ─────────────
            rounds_log: list[dict] = []
            round_no = 1
            no_change = 0
            while round_no <= max_turns - 2 and any(t["status"] == "open" for t in tensions):
                round_no += 1
                tension = next(t for t in tensions if t["status"] == "open")
                a, b = self._pair_for(tension, reviewers)
                question = await self._frame_question(tension, positions, a, b)
                prior_block = (
                    f"### {a}'s position\n{_clip(positions.get(a, ''))}\n\n"
                    f"### {b}'s position\n{_clip(positions.get(b, ''))}"
                )
                resp = {}
                for who, opp in ((a, b), (b, a)):
                    p = (
                        f"## Design (excerpt)\n{design[:600]}\n\n"
                        f"## The open question\n{question}\n\n"
                        f"## Positions so far\n{prior_block}\n\n"
                        f"You are **{who}**. Respond to **{opp}** directly. Take a clear stance, "
                        f"cite the specific prior point you're answering, say what would change your "
                        f"mind, and give an updated score if it changed. End with:\nSCORE: <n>"
                    )
                    txt = await self._invoke(ctx, who, by_name[who].url, p, label=f"Round {round_no} · {tension['id']}")
                    resp[who] = txt
                    if (s := _parse_score(txt)) is not None:
                        scores.setdefault(who, []).append(s)
                tension["turns"] += 1
                prev = tension["status"]
                tension["status"] = await self._assess(tension, resp.get(a, ""), resp.get(b, ""))
                if tension["status"] == "open" and tension["turns"] >= 2:
                    tension["status"] = "entrenched"   # two turns, still no movement
                rounds_log.append({"round": round_no, "tension": tension["description"], "agents": [a, b], "responses": resp})
                no_change = no_change + 1 if tension["status"] == prev else 0
                if no_change >= 2:
                    break  # two consecutive turns with no state change

            # ── Phase 3 — convergence (parallel final stances) ──────────────────
            async def converge(r: str) -> tuple[str, str]:
                p = (
                    f"## Design (excerpt)\n{design[:600]}\n\n"
                    f"## Your opening position\n{_clip(positions.get(r, ''))}\n\n"
                    "The panel has debated this design. State: (1) what you learned that changed your "
                    "initial position, (2) what you still believe despite the counter-arguments, "
                    "(3) your FINAL score 1-10 with a one-sentence rationale, (4) your single top "
                    "recommended change to the design. End with:\nSCORE: <n>"
                )
                return r, await self._invoke(ctx, r, by_name[r].url, p, label="Final stance")

            finals = dict(await asyncio.gather(*[converge(r) for r in positions]))
            for r, txt in finals.items():
                if (s := _parse_score(txt)) is not None:
                    scores.setdefault(r, []).append(s)

            # ── Report synthesis (judge) → main answer panel ────────────────────
            report = await self._synthesize_report(
                design, reviewers, positions, tensions, rounds_log, finals, scores,
            )
            await emit_token(ctx, ORCHESTRATOR_ID, report or "\n_(report synthesis failed)_\n")

            resolved = sum(1 for t in tensions if t["status"] in ("resolved", "entrenched"))
            await emit_workflow_completed(
                ctx,
                summary=f"Debate complete — {len(tensions)} tensions ({resolved} settled), {round_no} turns.",
                latency_ms=(perf_counter() - t0) * 1000,
            )

    # ── judgment steps ────────────────────────────────────────────────────────
    async def _detect_tensions(self, design: str, positions_blob: str, ids: list[str]) -> list[dict]:
        raw = await self._judge(
            system=(
                "You analyse a panel of specialist design reviews and surface GENUINE tensions — "
                "points where reviewers materially disagree: different top concerns, scores ≥3 apart, "
                "or contradictory assumptions. Respond with ONLY a JSON array, no prose."
            ),
            user=(
                f"Design (excerpt): {design[:800]}\n\nOpening positions:\n{positions_blob}\n\n"
                "Return the 1-4 MOST consequential tensions as a JSON array; each item: "
                '{"description": "<one line>", "agents": ["<id>", "<id>"]}. '
                f"Use only these reviewer ids: {ids}. If no real tension exists, return []."
            ),
        )
        out = []
        for i, d in enumerate(_extract_json_array(raw)):
            agents = [a for a in (d.get("agents") or []) if a in ids][:2]
            if len(agents) == 2 and d.get("description"):
                out.append({"id": f"T{i+1}", "description": str(d["description"]), "agents": agents, "status": "open", "turns": 0})
        logger.info("debate.tensions detected=%d", len(out))
        return out

    @staticmethod
    def _pair_for(tension: dict, reviewers: list[str]) -> tuple[str, str]:
        a, b = (list(tension["agents"]) + reviewers)[:2]
        return a, b

    async def _frame_question(self, tension: dict, positions: dict, a: str, b: str) -> str:
        raw = await self._judge(
            system="You frame ONE sharp, specific question that forces two reviewers to engage a disagreement. Not 'share your thoughts.' Return only the question.",
            user=(
                f"Tension: {tension['description']}\n\n"
                f"{a}: {_clip(positions.get(a,''), 600)}\n\n{b}: {_clip(positions.get(b,''), 600)}\n\n"
                f"Write one specific question that {a} and {b} must each answer to advance this."
            ),
            max_tokens=200,
        )
        return raw.strip() or tension["description"]

    async def _assess(self, tension: dict, resp_a: str, resp_b: str) -> str:
        raw = await self._judge(
            system="You judge whether a debated tension is now resolved, refined, or entrenched. Reply with ONLY one word: resolved | refined | entrenched.",
            user=(
                f"Tension: {tension['description']}\n\nResponse A:\n{_clip(resp_a)}\n\nResponse B:\n{_clip(resp_b)}\n\n"
                "resolved = both converge or one concedes; refined = narrowed but persists; entrenched = no movement."
            ),
            max_tokens=10,
        )
        w = raw.strip().lower()
        return w if w in ("resolved", "refined", "entrenched") else ("refined" if w else tension["status"])

    async def _synthesize_report(
        self, design, reviewers, positions, tensions, rounds_log, finals, scores,
    ) -> str:
        def score_line(r):
            s = scores.get(r, [])
            return f"{s[0] if s else '?'} → {s[-1] if s else '?'}"
        roster = "\n".join(f"- {r}: opening→final score {score_line(r)}" for r in positions)
        tblob = "\n".join(f"- [{t['status']}] {t['id']}: {t['description']} ({' vs '.join(t['agents'])})" for t in tensions) or "(none surfaced)"
        rblob = "\n\n".join(
            f"Round {x['round']} on '{x['tension']}':\n" + "\n".join(f"  {who}: {_clip(txt, 700)}" for who, txt in x["responses"].items())
            for x in rounds_log
        ) or "(no debate rounds)"
        oblob = "\n\n".join(f"### {r}\n{_clip(t, 900)}" for r, t in positions.items())
        fblob = "\n\n".join(f"### {r}\n{_clip(t, 900)}" for r, t in finals.items() if t)
        return await self._judge(
            system=(
                "You are the moderator of a structured design-review debate. Synthesise the transcript "
                "into an HONEST Markdown report. NEVER speak for a reviewer — only paraphrase or quote what "
                "they actually said. NEVER hide a disagreement to make the report tidy; entrenched tensions "
                "are valuable. Ground every claim in the transcript."
            ),
            user=(
                f"# Transcript\n\n## Design\n{design}\n\n## Roster & scores\n{roster}\n\n"
                f"## Tensions (final status)\n{tblob}\n\n## Opening positions\n{oblob}\n\n"
                f"## Debate rounds\n{rblob}\n\n## Final stances\n{fblob}\n\n"
                "---\nWrite the report with EXACTLY these sections (use '(none surfaced)' if empty, never omit):\n"
                "## 1. Design under review\n## 2. Reviewers (name · specialty · opening→final score)\n"
                "## 3. Strong consensus\n## 4. Genuine disagreements (unresolved) — quote each side, do NOT pick a winner\n"
                "## 5. Stress tests (what was tested · pass/partial/fail · evidence)\n"
                "## 6. Recommended changes (P0/P1/P2 · who asked · risk if not made)\n"
                "## 7. Open questions\n## 8. Debate quality self-assessment (tensions resolved, who moved, any sycophancy, was the budget enough)"
            ),
            max_tokens=4000,
        )
