"""Cluster friction across sessions and attach known remedies."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .parse import Friction, Session

KIND_LABEL = {
    "tool_error": "Tool error",
    "retry_loop": "Retry loop",
    "denial": "Permission denied",
    "interrupt": "User interrupt",
    "correction": "User correction",
    "retry": "User asked to retry",
    "api_error": "API error",
    "compaction": "Context compaction",
    "timeout": "Command timeout",
    "slow_tool": "Slow / awaiting approval",
}

# (kind or None, regex on signature) -> remedy. First match wins.
RULES: list[tuple[str | None, re.Pattern, str]] = [
    (None, re.compile(r"harness blocked a sleep|Blocked: .*sleep", re.I),
     "Never `sleep` to wait. Start long commands with `run_in_background: true`, or wait on a condition with an until-loop under Monitor."),
    (None, re.compile(r'MCP server "?([\w-]+)"? is not connected', re.I),
     "If an MCP server reports 'not connected', stop and ask the user to reconnect it (`/mcp`) instead of retrying its tools."),
    (None, re.compile(r"has not been read yet", re.I),
     "Read a file before you Write or Edit it, even when you are replacing it wholesale."),
    (None, re.compile(r"cannot run in a batch|ref map not initialized|not screenshotted yet", re.I),
     "Browser automation: take a screenshot / read_page first after every navigation, and don't put screenshot inside a batch."),
    (None, re.compile(r"(Forbidden|Not Found).*vercel|vercel.*(Forbidden|Not Found)", re.I),
     "Vercel API calls: resolve team and project scope (list_teams → list_projects) before calling, never guess IDs."),
    (None, re.compile(r"Port N is in use|EADDRINUSE|already running", re.I),
     "Check for an already-running dev server (`lsof -nP -iTCP -sTCP:LISTEN`) before starting another."),
    (None, re.compile(r"connection refused", re.I),
     "Before curling a local server, check it's up (`lsof -nP -iTCP:<port> -sTCP:LISTEN`) and start it in the background if not."),
    (None, re.compile(r"probe chain", re.I),
     "End exploratory `a; b; ls x` chains with `|| true` (or check each step) so a missing optional file doesn't read as a failure."),
    (None, re.compile(r"grep/test found nothing", re.I),
     "Use `grep … || true` when no match is an acceptable answer."),
    (None, re.compile(r"path not found", re.I),
     "Don't guess paths: Glob/ls the directory first. Add the project's key paths to CLAUDE.md."),
    (None, re.compile(r"Playwright browser missing", re.I),
     "Point Playwright at the already-installed browser via `executablePath` instead of downloading one."),
    (None, re.compile(r"cwd was deleted", re.I),
     "Use absolute paths and don't `cd` into directories that a later step deletes (e.g. worktrees)."),
    (None, re.compile(r"network error", re.I),
     "Transient network failure. Retry once with backoff; don't rewrite the approach."),
    (None, re.compile(r"binary/app not found|command not found|not found in \$?PATH", re.I),
     "A binary was missing from PATH. Record its absolute path (or the install command) in CLAUDE.md."),
    (None, re.compile(r"module not found|ModuleNotFoundError|Cannot find module", re.I),
     "Dependency missing at run time. Note the project's install/run command (uv run, npx, etc.) in CLAUDE.md, and keep scratch scripts inside a dir that has the deps."),
    (None, re.compile(r"String to replace not found|old_string", re.I),
     "Edit misses: re-Read the file region right before editing; it changed since the last read."),
    ("timeout", re.compile(r".*"),
     "Commands hit the tool timeout. Run builds/test suites with `run_in_background: true` and poll the output file."),
    ("slow_tool", re.compile(r"xcodebuild|cargo build|next build|npm run build|vitest|pytest|cargo test", re.I),
     "Long builds/tests block the turn. Run them in the background, or narrow them (single target / single test file)."),
    ("denial", re.compile(r"automode", re.I),
     "Auto mode blocked actions here. Add an explicit allow rule (`/permissions`) if they're routine, or ask first."),
    ("denial", re.compile(r".*"),
     "ADVICE:You rejected these calls. Consider a CLAUDE.md rule asking Claude to confirm before this kind of action."),
    ("interrupt", re.compile(r".*"),
     "ADVICE:You interrupted mid-task. Look at what preceded each one — usually a sign Claude went off-scope or was about to do something slow."),
    ("correction", re.compile(r".*"),
     "ADVICE:Your next message pushed back. Read the snippets: repeated themes are good CLAUDE.md rules."),
    ("retry", re.compile(r"API error|limit", re.I),
     "ADVICE:You had to re-prompt after an API error or usage limit. Not a CLAUDE.md problem, but it shows how often work stalls on infrastructure."),
    ("retry", re.compile(r".*"),
     "ADVICE:You had to ask for a redo after a failed attempt. The examples show what failed just before."),
    ("compaction", re.compile(r".*"),
     "ADVICE:Context filled up. Split long sessions by task, or keep a running PLAN.md so post-compaction work stays on track."),
    ("api_error", re.compile(r".*"),
     "ADVICE:API errors (overload/limits) — not actionable in CLAUDE.md, but they cost wall time."),
]


@dataclass
class Cluster:
    kind: str
    signature: str
    events: list[Friction] = field(default_factory=list)
    remedy: str | None = None  # a CLAUDE.md rule, addressed to Claude
    advice: str | None = None  # a note for the human reading the report
    est_cost: float = 0.0  # rough: events × avg cost per assistant turn in that session
    seconds: float = 0.0

    @property
    def sessions(self) -> set[str]:
        return {e.session_id for e in self.events}

    @property
    def projects(self) -> set[str]:
        return {e.project for e in self.events}

    rule_paths: list[str] = field(default_factory=list)  # every place this cluster's *rule* applies

    @property
    def target(self) -> str:
        return placement(self.rule_paths or [p for e in self.events for p in e.paths])

    @property
    def score(self) -> float:
        # Recurrence across sessions matters more than raw count: that's what a CLAUDE.md rule fixes.
        weight = {"correction": 3, "interrupt": 3, "retry": 2, "retry_loop": 2, "denial": 2, "timeout": 2}.get(self.kind, 1)
        return weight * len(self.events) * (1 + len(self.sessions)) + self.seconds / 600


def remedy_for(kind: str, signature: str) -> tuple[str | None, str | None]:
    """(CLAUDE.md rule, human advice) for a cluster; at most one is set."""
    for k, rx, text in RULES:
        if (k is None or k == kind) and rx.search(signature):
            return (None, text[7:]) if text.startswith("ADVICE:") else (text, None)
    return None, None


def cluster(sessions: list[Session]) -> list[Cluster]:
    per_turn = {s.id: s.cost / max(sum(s.models.values()), 1) for s in sessions}
    groups: dict[tuple[str, str], Cluster] = {}
    for s in sessions:
        for f in s.friction:
            sig = f.signature
            if f.kind in ("interrupt", "correction", "compaction", "api_error"):
                sig = KIND_LABEL[f.kind]
            key = (f.kind, sig)
            c = groups.get(key) or groups.setdefault(key, Cluster(f.kind, sig))
            c.events.append(f)
            c.seconds += f.seconds
            if f.kind in ("tool_error", "retry_loop", "denial", "correction", "interrupt", "retry"):
                c.est_cost += per_turn[s.id]
    out = list(groups.values())
    for c in out:
        c.remedy, c.advice = remedy_for(c.kind, c.signature)
    # One rule, one home: clusters sharing a remedy are placed at the ancestor of all of them.
    by_rule: dict[str, list[str]] = defaultdict(list)
    for c in out:
        if c.remedy:
            by_rule[c.remedy] += [p for e in c.events for p in e.paths]
    for c in out:
        if c.remedy:
            c.rule_paths = by_rule[c.remedy]
    out.sort(key=lambda c: c.score, reverse=True)
    return out


@dataclass
class Totals:
    sessions: int
    cost: float
    active_h: float
    tool_calls: int
    tool_errors: int
    human_turns: int
    cache_hit: float
    by_project: dict[str, float]
    by_day: dict[str, float]
    by_tool: dict[str, tuple[int, int]]  # calls, errors


def totals(sessions: list[Session]) -> Totals:
    by_project: dict[str, float] = defaultdict(float)
    by_day: dict[str, float] = defaultdict(float)
    by_tool: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    tok = defaultdict(int)
    for s in sessions:
        by_project[short_project(s.project)] += s.cost
        if s.start:
            by_day[s.start.astimezone().strftime("%Y-%m-%d")] += s.cost
        for k, v in s.tool_calls.items():
            by_tool[k][0] += v
        for f in s.friction:
            if f.kind == "tool_error" and f.tool:
                by_tool[f.tool][1] += 1
        for k, v in s.tokens.items():
            tok[k] += v
    denom = tok["input"] + tok["cache_read"] + tok["cache_write"]
    return Totals(
        sessions=len(sessions),
        cost=sum(s.cost for s in sessions),
        active_h=sum(s.active_s for s in sessions) / 3600,
        tool_calls=sum(sum(s.tool_calls.values()) for s in sessions),
        tool_errors=sum(s.tool_errors for s in sessions),
        human_turns=sum(s.human_turns for s in sessions),
        cache_hit=tok["cache_read"] / denom if denom else 0.0,
        by_project=dict(sorted(by_project.items(), key=lambda kv: -kv[1])),
        by_day=dict(sorted(by_day.items())),
        by_tool={k: (v[0], v[1]) for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1][0])},
    )


def short_project(path: str) -> str:
    """'/Users/x/Projects/site/.claude/worktrees/ui-b88b70' -> 'site ⎇ ui'."""
    m = re.search(r"([^/]+)/\.claude/worktrees/([^/]+?)(-[0-9a-f]{6})?$", path)
    if m:
        return f"{m.group(1)} ⎇ {m.group(2)}"
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[-2] not in ("Projects", "Users", "home", "src", "code"):
        return "/".join(parts[-2:])
    return parts[-1] if parts else path


HOME = os.path.expanduser("~")
GLOBAL = "~/.claude/CLAUDE.md"
_WORKTREE = re.compile(r"/\.claude/worktrees/[^/]+")
_TRANSIENT = ("/private/tmp", "/tmp/", "/var/folders", f"{HOME}/.claude/projects", f"{HOME}/Library/Caches")


def placement(paths: list[str]) -> str:
    """The CLAUDE.md a rule belongs in: the lowest common ancestor of everywhere it occurred.

    Like Jethro's courts (Exodus 18): a lesson is handled at the lowest level that has the
    context for it, and only rises when it spans several subtrees; root/global gets only
    what applies everywhere. Worktree paths fold into their repo, since worktrees are
    copies of it, and scratch/temp paths are ignored, since no CLAUDE.md lives there.
    """
    norm = []
    for p in paths:
        if not p or p.startswith(_TRANSIENT):
            continue
        p = _WORKTREE.sub("", p.rstrip("/"))
        if os.path.splitext(p)[1] or os.path.isfile(p):  # a file: its directory is the level
            p = os.path.dirname(p)
        norm.append(p)
    if not norm:
        return GLOBAL
    try:
        lca = os.path.commonpath(norm)
    except ValueError:
        return GLOBAL
    if (HOME + "/").startswith(lca.rstrip("/") + "/"):  # the home dir or above: genuinely global
        return GLOBAL
    return lca.replace(HOME, "~", 1) + "/CLAUDE.md"
