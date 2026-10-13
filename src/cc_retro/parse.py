"""Read Claude Code transcripts (~/.claude/projects/**/*.jsonl) into Session objects."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import pricing

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
IDLE_CAP_S = 300  # gaps longer than this don't count as active time
SLOW_TOOL_S = 120
INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}


@dataclass
class Friction:
    kind: str  # tool_error | denial | interrupt | correction | api_error | compaction | slow_tool | retry_loop
    ts: datetime | None
    tool: str | None
    signature: str  # normalized, for clustering
    snippet: str
    seconds: float = 0.0
    paths: list[str] = field(default_factory=list)  # where it happened: cwd + files the call touched
    session_id: str = ""
    project: str = ""


@dataclass
class Prompt:
    ts: datetime | None
    text: str
    prev: str  # the assistant's last visible text before this prompt (for classification)
    after_failure: bool  # an API error / usage limit / interrupt came just before


@dataclass
class Session:
    id: str
    project: str
    title: str = ""
    start: datetime | None = None
    end: datetime | None = None
    active_s: float = 0.0
    models: dict[str, int] = field(default_factory=dict)
    cost_est: float = 0.0
    cost_reported: float | None = None  # from cost-state records, when present
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0
    human_turns: int = 0
    lines_added: int = 0
    friction: list[Friction] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return self.cost_reported if self.cost_reported is not None else self.cost_est

    @property
    def cache_hit(self) -> float:
        t = self.tokens
        denom = t["input"] + t["cache_read"] + t["cache_write"]
        return t["cache_read"] / denom if denom else 0.0


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif isinstance(b.get("content"), (str, list)):
                    parts.append(_text(b["content"]))
        return "\n".join(parts)
    return ""


_NUM = re.compile(r"\d+")
_PATH = re.compile(r"(/[\w.@~+-]+){2,}/?")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
_URL = re.compile(r"https?://\S+")


def normalize(s: str, width: int = 90) -> str:
    """Collapse a message into a cluster key: drop paths, urls, ids, numbers."""
    first = next((ln.strip() for ln in s.splitlines() if ln.strip()), "")
    first = re.sub(r"</?tool_use_error>", "", first)
    first = _URL.sub("<url>", first)
    first = _PATH.sub("<path>", first)
    first = _HEX.sub("<id>", first)
    first = _NUM.sub("N", first)
    return first[:width]


_DENIAL = ("The user doesn't want to proceed", "Permission for this action was denied", "User rejected")
# A short prompt asking Claude to redo something. Corrections ("that's not what I meant") are
# semantic and are left to the opt-in LLM classifier: a regex for them scored 0/3 precision, 0/6 recall.
_RETRY = re.compile(r"\b(try again|retry|re-?run|please continue|continue from where|got into an issue|go again)\b", re.I)
RETRY_MAX_CHARS = 160
_NOT_HUMAN = ("<", "[Image", "[Request interrupted", "Base directory for this skill", "[Usage limit", "[Earlier usage", "Caveat:")


def _is_human(d: dict, txt: str) -> bool:
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin["kind"] == "human" and not txt.lstrip().startswith("<system-reminder>")
    if d.get("isMeta") or d.get("isCompactSummary"):
        return False
    return bool(txt.strip()) and not txt.lstrip().startswith(_NOT_HUMAN)


def _bash_key(inp: dict) -> str:
    return normalize(str(inp.get("command", "")), 60)


def load_session(path: Path, project_hint: str) -> Session | None:
    sess: Session | None = None
    seen_msgs: set[str] = set()
    pending: dict[str, tuple[str, dict, datetime | None]] = {}  # tool_use_id -> (name, input, ts)
    last_ts: datetime | None = None
    recent_errors: list[tuple[str, str]] = []  # (tool, key) of consecutive failures
    cwd = ""
    last_text = ""  # last visible assistant text
    failed = False  # infra failure since the last human prompt

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if sess is None and d.get("sessionId"):
                sess = Session(id=d["sessionId"], project=d.get("cwd") or project_hint)
            if sess is None:
                continue
            if d.get("cwd") and sess.project == project_hint:
                sess.project = d["cwd"]

            ts = _ts(d.get("timestamp"))
            cwd = d.get("cwd") or cwd
            if ts:
                sess.start = sess.start or ts
                sess.end = ts
                if last_ts:
                    sess.active_s += min(max((ts - last_ts).total_seconds(), 0), IDLE_CAP_S)
                last_ts = ts

            if t == "custom-title" and d.get("customTitle"):
                sess.title = d["customTitle"]
            elif t == "cost-state":
                sess.cost_reported = d.get("totalCostUSD")
                sess.lines_added = d.get("totalLinesAdded", sess.lines_added)
            elif t == "system":
                st = d.get("subtype")
                if st == "api_error":
                    failed = True
                    err = d.get("error") or d.get("content") or ""
                    _add(sess, cwd, None, {}, Friction("api_error", ts, None, normalize(json.dumps(err) if not isinstance(err, str) else err), str(err)[:300]))
                elif st == "compact_boundary":
                    _add(sess, cwd, None, {}, Friction("compaction", ts, None, "context compacted", ""))

            m = d.get("message")
            if not isinstance(m, dict):
                continue
            content = m.get("content")

            if t == "assistant":
                if d.get("isApiErrorMessage") or m.get("model") == "<synthetic>":
                    failed = True
                vis = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text") if isinstance(content, list) else ""
                if vis.strip():
                    last_text = vis.strip()
                mid = m.get("id") or d.get("requestId") or d.get("uuid")
                if mid not in seen_msgs:  # one API response is split across several lines
                    seen_msgs.add(mid)
                    u = m.get("usage") or {}
                    model = m.get("model") or "?"
                    if model != "<synthetic>":
                        sess.models[model] = sess.models.get(model, 0) + 1
                        sess.cost_est += pricing.cost(model, u) or 0.0
                        sess.tokens["input"] += u.get("input_tokens", 0)
                        sess.tokens["output"] += u.get("output_tokens", 0)
                        sess.tokens["cache_read"] += u.get("cache_read_input_tokens", 0)
                        sess.tokens["cache_write"] += u.get("cache_creation_input_tokens", 0)
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            name = b.get("name", "?")
                            sess.tool_calls[name] = sess.tool_calls.get(name, 0) + 1
                            pending[b.get("id", "")] = (name, b.get("input") or {}, ts)

            elif t == "user":
                if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    for b in content:
                        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                            continue
                        name, inp, t0 = pending.pop(b.get("tool_use_id", ""), ("?", {}, None))
                        body = _text(b.get("content"))
                        tur = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
                        if tur.get("timedOutAfterMs"):
                            _add(sess, cwd, name, inp, Friction("timeout", ts, name, f"{name}: hit the tool timeout", f"after {int(tur['timedOutAfterMs']) // 1000}s  {_describe(name, inp)}"))
                        elif t0 and ts and name not in INTERACTIVE_TOOLS and (ts - t0).total_seconds() > SLOW_TOOL_S:
                            # Wall time includes any permission-prompt wait; transcripts don't separate the two.
                            dur = (ts - t0).total_seconds()
                            _add(sess, cwd, name, inp, Friction("slow_tool", ts, name, f"{name}: {_first_word(inp) if name == 'Bash' else 'call'}", f"{dur:.0f}s  {_describe(name, inp)}", dur))
                        if not b.get("is_error"):
                            recent_errors.clear()
                            continue
                        if d.get("toolDenialKind") or body.startswith(_DENIAL):
                            _add(sess, cwd, name, inp, Friction("denial", ts, name, f"{name}: {d.get('toolDenialKind') or normalize(body, 60)}", _describe(name, inp)))
                            continue
                        sess.tool_errors += 1
                        sig = f"Bash: {bash_cause(str(inp.get('command', '')), body)}" if name == "Bash" else f"{name}: {normalize(body)}"
                        snippet = (_describe(name, inp) + "\n→ " + body.strip()[:400]).strip()
                        _add(sess, cwd, name, inp, Friction("tool_error", ts, name, sig, snippet))
                        # A loop is the *same* call failing the same way repeatedly, not any three failures.
                        key = (name, _bash_key(inp) if name == "Bash" else sig)
                        recent_errors = recent_errors + [key] if recent_errors and recent_errors[-1] == key else [key]
                        if len(recent_errors) == 3:  # flag once per streak
                            _add(sess, cwd, name, inp, Friction("retry_loop", ts, name, f"{name}: same call failed 3+ times in a row", snippet))
                    continue

                txt = _text(content)
                if txt.startswith("[Request interrupted"):
                    _add(sess, cwd, None, {}, Friction("interrupt", ts, None, normalize(txt, 50), last_text[-300:]))
                    failed = True
                elif _is_human(d, txt):
                    sess.human_turns += 1
                    sess.prompts.append(Prompt(ts, txt.strip(), last_text[-600:], failed))
                    if len(txt) <= RETRY_MAX_CHARS and _RETRY.search(txt):
                        why = "after an API error / limit" if failed else "after a failed attempt"
                        _add(sess, cwd, None, {}, Friction("retry", ts, None, f"User asked to retry {why}", f"{txt.strip()[:200]}\n← {last_text[-200:]}"))
                    failed = False

    if sess is None:
        return None
    for f in sess.friction:
        f.session_id, f.project = sess.id, sess.project
    return sess


def _add(sess: Session, cwd: str, name: str | None, inp: dict, f: Friction) -> None:
    f.paths = touched(cwd, name, inp)
    sess.friction.append(f)


_REL_FILE = re.compile(r"(?<![\w./~$-])((?:[\w@.-]+/)+[\w@.-]+\.[A-Za-z]{1,5})\b")  # src/lib/x.ts
_ABS_PATH = re.compile(r"(?<![\w.])(/(?:Users|home)/[^\s'\"`;|&<>()]+)")


def touched(cwd: str, name: str | None, inp: dict) -> list[str]:
    """Files/dirs a tool call was about, for placing a rule at the right CLAUDE.md.

    Falls back to the session cwd only when the call names nothing more specific; otherwise
    the project root would always be in the set and placement could never go below it.
    """
    out = []
    for k in ("file_path", "path", "notebook_path"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            out.append(v if v.startswith("/") else f"{cwd}/{v}")
    if name == "Bash":
        cmd = str(inp.get("command", ""))
        out += _ABS_PATH.findall(cmd)
        out += [f"{cwd}/{m}" for m in _REL_FILE.findall(cmd)] if cwd else []
    return out or ([cwd] if cwd else [])


_BASH_CAUSES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Blocked: (standalone )?sleep", re.I), "harness blocked a sleep"),
    (re.compile(r"<tool_use_error>"), "harness rejected the command"),
    (re.compile(r"^Exit code 7\b|Connection refused|ECONNREFUSED|Failed to connect", re.M), "connection refused (server not running)"),
    (re.compile(r"TLS handshake timeout|Could not resolve host|ETIMEDOUT|network is unreachable", re.I), "network error"),
    (re.compile(r"Executable doesn't exist|playwright install", re.I), "Playwright browser missing"),
    (re.compile(r"ERR_MODULE_NOT_FOUND|Cannot find module|ModuleNotFoundError|No module named", re.I), "module not found"),
    (re.compile(r"^Exit code 127\b|command not found|: not found\b|Unable to find application", re.M), "binary/app not found"),
    (re.compile(r"current working directory was deleted", re.I), "cwd was deleted"),
    (re.compile(r"Traceback \(most recent call last\)"), "Python traceback"),
    (re.compile(r"No such file or directory|cannot access|does not exist", re.I), "path not found"),
    (re.compile(r"error\[E\d+\]|error TS\d+|SyntaxError|TypeError|ReferenceError|^error:", re.M), "compile/type/runtime error"),
    (re.compile(r"\bFAIL\b|failed|failures?:|assert", re.I), "test or check failed"),
    (re.compile(r"Permission denied|EACCES|Operation not permitted", re.I), "OS permission denied"),
]
_PROBE = re.compile(r"^\s*(which|ls|cat|grep|test|\[|head|tail|sed -n|git (log|show|branch|remote)|command -v)\b")


def bash_cause(cmd: str, body: str) -> str:
    """Classify a failed Bash call by likely cause rather than exit code."""
    # Specific causes first; then multi-command probes (where the body is mostly stdout from
    # earlier commands and would trip the generic patterns); then the generic patterns.
    for rx, label in _BASH_CAUSES[:8]:
        if rx.search(body):
            return label
    segments = [s for s in re.split(r";|&&|\|\|", cmd.replace("\n", ";")) if s.strip()]
    if len(segments) > 1 and _PROBE.match(segments[-1].split("|")[0]):
        return "probe chain: last lookup came back empty"
    for rx, label in _BASH_CAUSES[8:]:
        if rx.search(body):
            return label
    if re.match(r"^\s*(grep|rg|diff|cmp|test|\[)\b", cmd) and re.match(r"^Exit code 1\s*$", body.strip()):
        return "grep/test found nothing (exit 1)"
    m = re.match(r"^Exit code (\d+)", body)
    return f"exit {m.group(1)} from {_first_word({'command': cmd})}" if m else normalize(body, 60)


_SKIP_WORDS ={"cd", "&&", "time", "env", "sudo", "S=", "SP="}


def _first_word(inp: dict) -> str:
    """The program a Bash command runs, e.g. 'xcodebuild', 'npx vitest', 'python3'."""
    cmd = str(inp.get("command", ""))
    for seg in re.split(r"&&|;|\|\|", cmd):
        words = [w for w in seg.split() if "=" not in w and w not in _SKIP_WORDS]
        if not words or words[0] == "cd":
            continue
        w0 = Path(words[0]).name
        if w0 in ("npx", "uv", "bunx", "pnpm", "npm", "cargo", "go") and len(words) > 1:
            return f"{w0} {words[1] if words[1] != 'run' or len(words) < 3 else 'run ' + Path(words[2]).name}"
        return w0
    return "?"


def _describe(name: str, inp: dict) -> str:
    if name == "Bash":
        return "$ " + str(inp.get("command", ""))[:200]
    for k in ("file_path", "path", "url", "query", "pattern"):
        if k in inp:
            return f"{name}({inp[k]})"[:200]
    return name


def load_all(root: Path = DEFAULT_ROOT, since: datetime | None = None) -> list[Session]:
    """One Session per session id; sidechain/subagent files merge into their parent."""
    by_id: dict[str, Session] = {}
    for path in sorted(root.glob("**/*.jsonl")):
        if since and datetime.fromtimestamp(path.stat().st_mtime).astimezone() < since:
            continue
        s = load_session(path, path.parent.name)
        if s is None or not (s.models or s.human_turns):
            continue
        if s.id in by_id:
            _merge(by_id[s.id], s)
        else:
            by_id[s.id] = s
    return sorted(by_id.values(), key=lambda s: s.start or datetime.min.astimezone())


def _merge(a: Session, b: Session) -> None:
    a.cost_est += b.cost_est
    for k, v in b.tokens.items():
        a.tokens[k] += v
    for k, v in b.tool_calls.items():
        a.tool_calls[k] = a.tool_calls.get(k, 0) + v
    for k, v in b.models.items():
        a.models[k] = a.models.get(k, 0) + v
    a.tool_errors += b.tool_errors
    a.friction += b.friction
    a.prompts += b.prompts
    a.title = a.title or b.title
