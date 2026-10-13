"""Claude Code hooks backed by cc-retro's findings. Each reads the hook's JSON from stdin.

idle-resume (UserPromptSubmit): returning to a large session after its prompt cache expired
re-caches the whole context. On the author's history that was 26% of spend: 30 returns, about $3
each. The first prompt after such a break is held once with the cost and options; sending it again
within HOLD_WINDOW goes through. Every failure lets the prompt through.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import pricing

STATE = Path.home() / ".claude" / "cc-retro" / "holds.json"
LOG = Path.home() / ".claude" / "cc-retro" / "holds.log.jsonl"  # hold/release events, for measuring the hook
MIN_TOKENS = int(os.environ.get("CC_RETRO_IDLE_MIN_TOKENS", "150000"))
HOLD_WINDOW = 600  # seconds: a resend within this goes through
TAIL_BYTES = 4_000_000


def last_call(transcript: Path) -> dict | None:
    """The newest API call in a transcript: context size, model, time, and the cache TTL in use."""
    size = transcript.stat().st_size
    with transcript.open("rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode(errors="replace").splitlines()
    ttl = None
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") or {}
        u = m.get("usage")
        if d.get("type") != "assistant" or not u or str(m.get("model", "")).startswith("<"):
            continue
        cc = u.get("cache_creation") or {}
        if ttl is None:
            ttl = 300 if cc.get("ephemeral_5m_input_tokens") and not cc.get("ephemeral_1h_input_tokens") else 3600
        ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
               + (u.get("cache_creation_input_tokens") or sum(v or 0 for v in cc.values())))
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")) if d.get("timestamp") else None
        return {"ctx": ctx, "model": m.get("model", ""), "ts": ts, "ttl": ttl}
    return None


def recache_cost(ctx: int, model: str, ttl: int) -> float:
    """Extra $ for re-writing the context to cache instead of reading it."""
    r = pricing.rates(model)
    if not r:
        return 0.0
    inp, _, read = r
    return ctx * (inp * (2.0 if ttl >= 3600 else 1.25) - read) / 1e6


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def idle_resume(event: dict, now: float | None = None) -> dict | None:
    """Hook output (a dict to print as JSON), or None to let the prompt through silently."""
    now = time.time() if now is None else now
    sid, path = event.get("session_id", ""), event.get("transcript_path", "")
    if not sid or not path or not Path(path).exists():
        return None
    holds = {k: v for k, v in _load().items() if now - v < HOLD_WINDOW}
    if sid in holds:  # the resend after a hold: let it through
        del holds[sid]
        _save(holds)
        _log({"event": "release", "ts": now, "session": sid})
        return None
    last = last_call(Path(path))
    if not last or not last["ts"] or last["ctx"] < MIN_TOKENS:
        return None
    idle = now - last["ts"].timestamp()
    if idle <= last["ttl"]:
        return None
    cost = recache_cost(last["ctx"], last["model"], last["ttl"])
    holds[sid] = now
    _save(holds)
    _log({"event": "hold", "ts": now, "session": sid, "transcript": path, "ctx": last["ctx"],
          "idle_h": round(idle / 3600, 2), "cost": round(cost, 4)})
    hours = idle / 3600
    prompt = str(event.get("prompt", ""))
    preview = prompt if len(prompt) <= 600 else prompt[:600] + "…"
    reason = (
        f"cc-retro: held this prompt. The session's {last['ctx'] / 1000:.0f}k-token context has been idle "
        f"{hours:.1f}h and its cache has expired, so continuing re-caches all of it (≈${cost:.2f} extra).\n"
        f"- Same task: send it again within {HOLD_WINDOW // 60} min to go ahead (or /compact first to shrink it).\n"
        f"- New task: start a new session, or /clear.\n"
        f"- Next time: before a break, ask for a short handoff note while the cache is warm, then start fresh with it.\n\n"
        f"Your prompt, to copy back:\n{preview}"
    )
    return {"decision": "block", "reason": reason}


def _log(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def outcomes(log: Path = LOG) -> list[dict]:
    """Each hold with what happened next:
    left: no resend in the window (new session or /clear): the re-cache was avoided.
    compacted: resent, and the next call's context was under half the held one (/compact first).
    continued: resent with the context intact: paid knowingly."""
    try:
        events = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    out = []
    for i, h in enumerate(events):
        if h.get("event") != "hold":
            continue
        rel = next((e for e in events[i + 1:] if e.get("event") == "release" and e["session"] == h["session"]
                    and e["ts"] - h["ts"] <= HOLD_WINDOW), None)
        if not rel:
            out.append({**h, "outcome": "left", "saved": h["cost"]})
            continue
        nxt = _next_call_ctx(Path(h["transcript"]), rel["ts"])
        if nxt is not None and nxt < h["ctx"] / 2:
            out.append({**h, "outcome": "compacted", "saved": h["cost"] * (1 - nxt / h["ctx"])})
        else:
            out.append({**h, "outcome": "continued", "saved": 0.0})
    return out


def _next_call_ctx(transcript: Path, after: float) -> int | None:
    try:
        lines = transcript.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m, ts = d.get("message") or {}, d.get("timestamp")
        if d.get("type") != "assistant" or not ts or not m.get("usage"):
            continue
        if datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() <= after:
            continue
        u = m["usage"]
        cc = u.get("cache_creation") or {}
        return (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                + (u.get("cache_creation_input_tokens") or sum(v or 0 for v in cc.values())))
    return None


def _save(holds: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(holds))


def main(name: str) -> None:
    try:
        event = json.load(sys.stdin)
        out = {"idle-resume": idle_resume}[name](event)
    except Exception:  # a broken hook must never block work
        return
    if out:
        print(json.dumps(out))
