"""What fills the conversation context, and what it costs to carry.

Everything added to a session (a tool result, your prompt, the model's reply and tool inputs) is
re-sent on every later call until the session ends or compacts. So an item's real cost is its size
times the input price of every call after it. This module assigns each call's conversation-context
cost (input-side spend beyond the startup context) to the items alive at that call:

- Item sizes are estimated (text at ~4 chars/token; images from their PNG header, capped at the
  ~1,600 tokens the API resizes to), then scaled per call so they sum to the call's measured
  conversation tokens. The shares come from content; the totals come from real usage.
- A compaction boundary clears the live set; everything after it is new.
"""

from __future__ import annotations

import base64
import json
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import pricing
from .costs import CHARS_PER_TOKEN

# Attachment records the app keeps for itself, not sent to the model. Checked against real usage:
# a prompt_snapshot "adds" ~43k tokens on paper while the next call grows ~7k.
NOT_SENT = {"prompt_snapshot", "hook_success", "credential_org", "remote_session_change", "session_context"}
SKILL_PREFIX = "Base directory for this skill:"
IMAGE_CAP = 1600
IMAGE_DEFAULT = 1500


@dataclass
class Item:
    source: str  # "Read", "mcp__x__screenshot", "your prompts", "assistant replies + tool inputs"
    label: str  # e.g. the file read or command run
    tokens: int
    added_at: int  # index of the first call that carries it
    cost: float = 0.0
    calls: int = 0


@dataclass
class Composition:
    by_source: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    tokens_by_source: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    count_by_source: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    top: list[tuple[float, str, str, int, int, str]] = field(default_factory=list)  # $, source, label, tokens, calls, project
    total: float = 0.0


def image_tokens(b64: str) -> int:
    try:
        head = base64.b64decode(b64[:64] + "=" * (-len(b64[:64]) % 4))
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            scale = min(1.0, 1568 / max(w, h))
            return min(IMAGE_CAP, int(w * scale * h * scale / 750))
    except (ValueError, struct.error):
        pass
    return IMAGE_DEFAULT


def _size(content) -> int:
    if isinstance(content, str):
        return len(content) // CHARS_PER_TOKEN
    if isinstance(content, list):
        n = 0
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "image":
                n += image_tokens(str((blk.get("source") or {}).get("data", "")))
            elif blk.get("type") == "text":
                n += len(blk.get("text", "")) // CHARS_PER_TOKEN
            else:
                n += len(json.dumps(blk)) // CHARS_PER_TOKEN
        return n
    return len(json.dumps(content)) // CHARS_PER_TOKEN if content else 0


def _label(name: str, inp: dict) -> str:
    for k in ("file_path", "path", "url", "command", "pattern", "query", "skill"):
        if inp.get(k):
            return f"{name}: {str(inp[k])[:90]}"
    return name


def analyse(files: list[Path], top_n: int = 15) -> Composition:
    comp = Composition()
    tops: list[tuple[float, str, str, int, int, str]] = []
    for f in files:
        items, calls_meta = _walk(f)
        if not calls_meta:
            continue
        base = calls_meta[0][0]
        live: list[Item] = []
        pending = iter(items)
        nxt = next(pending, None)
        for i, (ctx, price, reset) in enumerate(calls_meta):
            if reset:
                live = []
            while nxt is not None and nxt.added_at <= i:
                live.append(nxt)
                nxt = next(pending, None)
            if i == 0 or not live:
                continue
            conv_tokens = max(ctx - base, 0)
            est = sum(it.tokens for it in live)
            if not conv_tokens or not est:
                continue
            scale = conv_tokens / est
            for it in live:
                c = it.tokens * scale * price
                it.cost += c
                it.calls += 1
        project = f.parent.name if f.parent.name != "subagents" else f.parent.parent.parent.name
        for it in items:
            comp.by_source[it.source] += it.cost
            comp.tokens_by_source[it.source] += it.tokens
            comp.count_by_source[it.source] += 1
            comp.total += it.cost
            tops.append((it.cost, it.source, it.label, it.tokens, it.calls, project))
    comp.top = sorted(tops, reverse=True)[:top_n]
    return comp


def _walk(f: Path) -> tuple[list[Item], list[tuple[int, float, bool]]]:
    """Items in order of appearance, and per API call: (context tokens, $ per context token, compacted before it)."""
    names: dict[str, tuple[str, dict]] = {}
    items: list[Item] = []
    calls: list[tuple[int, float, bool]] = []
    seen: set[str] = set()
    compacted = False
    with f.open(errors="replace") as fh:
        lines = list(fh)
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "system" and d.get("subtype") == "compact_boundary":
            compacted = True
            continue
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = m.get("content")
        at = len(calls)  # the next call is the first to carry anything added now
        if t == "assistant":
            out_size = 0
            for blk in content or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use":
                    names[blk.get("id", "")] = (blk.get("name", ""), blk.get("input") or {})
                    out_size += len(json.dumps(blk.get("input") or {})) // CHARS_PER_TOKEN
                elif blk.get("type") == "text":
                    out_size += len(blk.get("text", "")) // CHARS_PER_TOKEN
            if out_size:
                items.append(Item("assistant replies + tool inputs", "", out_size, at + 1))
            mid, u = m.get("id"), m.get("usage")
            if mid and u and mid not in seen and not str(m.get("model", "")).startswith("<"):
                seen.add(mid)
                r = pricing.rates(m.get("model", ""))
                cc = u.get("cache_creation") or {}
                w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
                if w5 is None and w1 is None:
                    w5, w1 = u.get("cache_creation_input_tokens", 0), 0
                inp, read = u.get("input_tokens", 0), u.get("cache_read_input_tokens", 0)
                ctx = inp + read + (w5 or 0) + (w1 or 0)
                spend = ((inp + (w5 or 0) * 1.25 + (w1 or 0) * 2.0) * r[0] + read * r[2]) / 1e6 if r else 0.0
                calls.append((ctx, spend / ctx if ctx else 0.0, compacted))
                compacted = False
        elif t == "user":
            if isinstance(content, str):
                items.append(_prompt_item(content, at))
                continue
            for blk in content or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_result":
                    name, inp = names.get(blk.get("tool_use_id", ""), ("unknown tool", {}))
                    items.append(Item(_group(name), _label(name, inp), _size(blk.get("content")), at))
                elif blk.get("type") == "text":
                    items.append(_prompt_item(blk.get("text", ""), at))
                elif blk.get("type") == "image":
                    items.append(Item("your prompts", "[image]", image_tokens(str((blk.get("source") or {}).get("data", ""))), at))
        elif t == "attachment" and calls:
            a = d.get("attachment") or {}
            if a.get("type") in NOT_SENT:
                continue
            items.append(Item("mid-session attachments", str(a.get("type", "")), len(json.dumps(a)) // CHARS_PER_TOKEN, at))
    return [it for it in items if it.tokens > 0], calls


def _prompt_item(text: str, at: int) -> Item:
    """A user-role text block: your prompt, or a skill's body the harness injected when it was invoked."""
    size = len(text) // CHARS_PER_TOKEN
    if text.startswith(SKILL_PREFIX):
        path = text[len(SKILL_PREFIX):].split("\n", 1)[0].strip().rstrip("/")
        return Item("skill content", f"skill: {path.rsplit('/', 1)[-1]}", size, at)
    return Item("your prompts", text[:80].replace("\n", " "), size, at)


def _group(name: str) -> str:
    """mcp__Claude_Browser__computer and mcp__claude-in-chrome__computer are the same kind of thing."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"{parts[1]}: {parts[-1]}" if len(parts) >= 3 else name
    return name


def summary(comp: Composition, conversation_total: float | None = None) -> str:
    lines = [f"Conversation context by source (${comp.total:.2f} attributed"
             + (f" of ${conversation_total:.2f} measured" if conversation_total else "") + "):"]
    rows = sorted(comp.by_source.items(), key=lambda kv: -kv[1])
    for src, cost in rows[:15]:
        n = comp.count_by_source[src]
        lines.append(f"  {src[:44]:44} ${cost:8.2f}  {cost / (comp.total or 1):5.1%}  "
                     f"{n:5}× avg ≈{comp.tokens_by_source[src] // max(n, 1):6,} tok")
    lines += ["", "Most expensive single items (carried until the session ended or compacted):"]
    for cost, src, label, tok, calls, _project in comp.top:
        lines.append(f"  ${cost:6.2f}  ≈{tok:7,} tok × {calls:4} calls  {label or src}"[:150])
    return "\n".join(lines)
