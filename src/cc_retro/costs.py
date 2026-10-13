"""Where the money goes: spend split by what the tokens were, not by what went wrong.

Each transcript file is one context window. Every API call's cost splits into:

- output: tokens the model wrote.
- startup context: re-reading what was loaded before your first prompt (system prompt, tool
  schemas, skill and tool listings, CLAUDE.md files, MCP instructions) on every turn, plus writing it
  to cache once. Its size is the first call's context.
- conversation: reading everything accumulated since (your prompts, tool results, replies).
- new context: cache writes as the conversation grows (normal).
- cache rebuilds: a later call re-writing most of its context to cache because the cache was gone
  (idle past the TTL, compaction, a model switch). This is paid twice for the same tokens.

The startup context is also broken down by source from the transcript's attachment records, and
skills and deferred tools are attributed to the plugin or MCP server that brings them, next to how
often you actually used them. Token sizes of attachments are estimated at 4 chars per token.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import pricing
from .hooks import outcomes as hooks_outcomes

CHARS_PER_TOKEN = 4
REBUILD_SHARE = 0.5  # a later call writing more than half its context to cache is a rebuild
LONG_CONTEXT = 150_000


@dataclass
class Call:
    ts: datetime | None
    model: str
    inp: int
    read: int
    w5: int
    w1: int
    out: int

    @property
    def ctx(self) -> int:
        return self.inp + self.read + self.w5 + self.w1

    @property
    def writes(self) -> int:
        return self.w5 + self.w1


@dataclass
class Context:
    file: Path
    project: str
    calls: list[Call] = field(default_factory=list)
    startup: dict[str, int] = field(default_factory=dict)  # source -> estimated tokens
    compactions: list[datetime] = field(default_factory=list)
    used: Counter = field(default_factory=Counter)  # source -> tool/skill uses


@dataclass
class Breakdown:
    output: float = 0.0
    startup: float = 0.0
    conversation: float = 0.0
    new_context: float = 0.0
    rebuild: float = 0.0
    rebuild_causes: dict[str, float] = field(default_factory=lambda: defaultdict(float))  # cause -> $
    long_context: float = 0.0  # spend on calls whose context exceeded LONG_CONTEXT
    by_source: dict[str, float] = field(default_factory=lambda: defaultdict(float))  # startup source -> $
    source_tokens: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    used: Counter = field(default_factory=Counter)
    sessions_with: Counter = field(default_factory=Counter)
    per_context: list[tuple[str, float, int, int]] = field(default_factory=list)  # project, $, startup tokens, calls

    @property
    def total(self) -> float:
        return self.output + self.startup + self.conversation + self.new_context + self.rebuild


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


_MCP = re.compile(r"^mcp__(?:plugin_([^_]+)_)?([^_]+(?:-[^_]+)*)__")


def tool_source(name: str) -> str:
    """'mcp__plugin_vercel_vercel__list_projects' -> 'vercel (plugin)'; 'mcp__paper__x' -> 'paper (MCP)'."""
    m = _MCP.match(name)
    if not m:
        return "built-in tools"
    return f"{m.group(1)} (plugin)" if m.group(1) else f"{m.group(2)} (MCP)"


def skill_source(name: str) -> str:
    return f"{name.split(':', 1)[0]} (plugin)" if ":" in name else "standalone skills"


def _startup_sources(a: dict) -> dict[str, int]:
    """Estimated tokens per source for one startup attachment."""
    t = a.get("type")
    out: Counter = Counter()
    if t == "skill_listing":
        for line in str(a.get("content", "")).splitlines():
            name = line[2:].split(":", 2)
            key = skill_source(":".join(name[:2])) if len(name) > 2 else "standalone skills"
            out[key] += len(line)
    elif t in ("deferred_tools_delta", "deferred_tools_record"):
        lines = a.get("addedLines") or a.get("addedNames") or []
        for ln in lines:
            out[tool_source(str(ln).split()[0] if str(ln).split() else "")] += len(str(ln)) + 1
    elif t == "instructions":
        for f in a.get("files", []):
            out["CLAUDE.md files"] += len(f.get("content", ""))
    elif t == "mcp_instructions_delta":
        out["MCP server instructions"] += len(json.dumps(a))
    elif t == "agent_listing_delta":
        out["agent listing"] += len(json.dumps(a))
    return {k: v // CHARS_PER_TOKEN for k, v in out.items()}


def load_contexts(root: Path) -> list[Context]:
    out = []
    for f in sorted(root.glob("**/*.jsonl")):  # subagents live in <session>/subagents/, each its own context
        project = f.parent.name if f.parent.name != "subagents" else f.parent.parent.parent.name
        ctx = Context(f, project)
        seen: set[str] = set()
        started = False
        with f.open(errors="replace") as fh:
            lines = list(fh)
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "attachment" and not started:
                for k, v in _startup_sources(d.get("attachment") or {}).items():
                    ctx.startup[k] = ctx.startup.get(k, 0) + v
            elif t == "system" and d.get("subtype") == "compact_boundary":
                if (ts := _ts(d.get("timestamp"))):
                    ctx.compactions.append(ts)
            elif t == "assistant":
                m = d.get("message") or {}
                for blk in m.get("content") or []:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use":
                        name = blk.get("name", "")
                        if name == "Skill":
                            ctx.used[skill_source(str((blk.get("input") or {}).get("skill", "")))] += 1
                        else:
                            ctx.used[tool_source(name)] += 1
                mid, u, model = m.get("id"), m.get("usage"), m.get("model", "")
                if not mid or mid in seen or not u or model.startswith("<"):
                    continue
                seen.add(mid)
                started = True
                cc = u.get("cache_creation") or {}
                w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
                if w5 is None and w1 is None:
                    w5, w1 = u.get("cache_creation_input_tokens", 0), 0
                ctx.calls.append(Call(_ts(d.get("timestamp")), model, u.get("input_tokens", 0),
                                      u.get("cache_read_input_tokens", 0), w5 or 0, w1 or 0, u.get("output_tokens", 0)))
        if ctx.calls:
            out.append(ctx)
    return out


def _parts(c: Call) -> tuple[float, float, float, float] | None:
    """$ for (output, uncached input, cache read, cache write) of one call."""
    r = pricing.rates(c.model)
    if not r:
        return None
    inp, out, read = r
    return c.out * out / 1e6, c.inp * inp / 1e6, c.read * read / 1e6, (c.w5 * 1.25 + c.w1 * 2.0) * inp / 1e6


def rebuild_cause(prev: Call, c: Call, compactions: list[datetime]) -> str:
    if prev.model != c.model:
        return "model switch"
    if prev.ts and c.ts:
        if any(prev.ts <= t <= c.ts for t in compactions):
            return "compaction"
        gap = (c.ts - prev.ts).total_seconds()
        if gap > 3600:
            return "idle over 1h (cache expired)"
        if gap > 300 and c.w5 >= c.w1:
            return "idle over 5 min (5-min cache expired)"
    return "other (context changed near the start)"


def breakdown(contexts: list[Context]) -> Breakdown:
    b = Breakdown()
    for ctx in contexts:
        base = ctx.calls[0].ctx
        spent = 0.0
        read_price_sum = 0.0  # Σ read price per token across calls: what one startup token costs over the session
        for i, c in enumerate(ctx.calls):
            p = _parts(c)
            if not p:
                continue
            out_c, inp_c, read_c, write_c = p
            r = pricing.rates(c.model) or (0.0, 0.0, 0.0)
            spent += sum(p)
            b.output += out_c
            if c.ctx > LONG_CONTEXT:
                b.long_context += sum(p)
            if i == 0:
                b.startup += inp_c + read_c + write_c
                read_price_sum += r[0] * 2.0 / 1e6 if c.w1 else r[0] * 1.25 / 1e6
                continue
            carried = c.inp + c.read  # what this call re-reads; its cache writes are new tokens
            fixed = min(base, carried) / carried if carried else 0.0
            b.startup += (inp_c + read_c) * fixed
            b.conversation += (inp_c + read_c) * (1 - fixed)
            read_price_sum += r[2] / 1e6
            if c.writes > REBUILD_SHARE * c.ctx:
                b.rebuild += write_c
                b.rebuild_causes[rebuild_cause(ctx.calls[i - 1], c, ctx.compactions)] += write_c
            else:
                b.new_context += write_c
        for src, tok in ctx.startup.items():
            b.by_source[src] += tok * read_price_sum
            b.source_tokens[src].append(tok)
            b.sessions_with[src] += 1
        b.used.update(ctx.used)
        b.per_context.append((ctx.project, spent, base, len(ctx.calls)))
    return b


def _ranked(d: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(d.items(), key=lambda kv: -kv[1])


def by_source_with_residual(b: Breakdown) -> list[tuple[str, float]]:
    """Startup $ by source, plus what the attachments don't explain: the system prompt and built-in
    tool schemas, which transcripts don't record."""
    rest = b.startup - sum(b.by_source.values())
    return _ranked({**b.by_source, "system prompt + tool schemas (not recorded)": max(rest, 0.0)})


def advice(b: Breakdown) -> list[str]:
    """Concrete, ranked suggestions; only where the numbers support one."""
    tips = []
    for src, cost in _ranked(b.by_source):
        if src in ("CLAUDE.md files", "built-in tools", "agent listing", "MCP server instructions"):
            continue
        uses = b.used.get(src, 0)
        if cost >= 1.0 and uses <= b.sessions_with[src] * 0.1:
            tips.append(f"{src}: ~${cost:.2f} to carry its listing through {b.sessions_with[src]} sessions, "
                        f"used {uses} times. Disable it where you don't need it (per project in "
                        f".claude/settings.json `enabledPlugins`, or `/mcp`).")
    if b.total and b.rebuild / b.total > 0.05:
        cause, c = _ranked(b.rebuild_causes)[0]
        tips.append(f"Cache rebuilds cost ${b.rebuild:.2f} ({b.rebuild / b.total:.0%}); mostly '{cause}' (${c:.2f}). "
                    "Coming back to a session after a long break re-pays its whole context; for a new task, start "
                    "a fresh session instead.")
    if b.total and b.long_context / b.total > 0.3:
        tips.append(f"{b.long_context / b.total:.0%} of spend was on calls with over {LONG_CONTEXT // 1000}k tokens of "
                    "context. Split long sessions at task boundaries (/clear or a new session), and keep big "
                    "tool outputs out of context (narrow greps, head/tail).")
    return tips


def summary(b: Breakdown) -> str:
    t = b.total or 1.0
    rows = [("Output (what the model wrote)", b.output), ("Startup context, re-read every turn", b.startup),
            ("Conversation context, re-read every turn", b.conversation), ("New context written to cache", b.new_context),
            ("Cache rebuilds (paid twice)", b.rebuild)]
    lines = [f"Spend ${b.total:.2f} across {len(b.per_context)} context windows", ""]
    lines += [f"  {name:42} ${v:8.2f}  {v / t:5.1%}" for name, v in rows]
    if b.rebuild_causes:
        lines += ["", "Cache rebuilds by cause:"] + [f"  {k:42} ${v:8.2f}" for k, v in _ranked(b.rebuild_causes)]
    lines += ["", f"Calls over {LONG_CONTEXT // 1000}k context: ${b.long_context:.2f} ({b.long_context / t:.0%})", "",
              "Startup context by source (≈ tokens per session, $ to carry it, times used):"]
    for src, cost in by_source_with_residual(b):
        if src not in b.source_tokens:
            lines.append(f"  {src:44} ${cost:7.2f}")
            continue
        toks = sorted(b.source_tokens[src])
        lines.append(f"  {src[:34]:34} ≈{toks[len(toks) // 2]:6,} tok  ${cost:7.2f}  "
                     f"used {b.used.get(src, 0):4}× in {b.sessions_with[src]} sessions")
    holds = hooks_outcomes()
    if holds:
        n = Counter(h["outcome"] for h in holds)
        lines += ["", f"idle-resume hook: {len(holds)} holds; {n['left']} left (new session or /clear), "
                      f"{n['compacted']} compacted first, {n['continued']} continued anyway; "
                      f"≈${sum(h['saved'] for h in holds):.2f} of re-caching avoided"]
    tips = advice(b)
    if tips:
        lines += ["", "Suggestions:"] + [f"  - {x}" for x in tips]
    return "\n".join(lines)
