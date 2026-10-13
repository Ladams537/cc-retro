"""Workflows: sessions that worked toward one thing, and how they were coordinated.

A workflow is the sessions in one repo (worktrees folded into it) whose activity overlaps or
follows within --gap hours. Each workflow gets a coordination mode:

- solo: one main session, no delegation.
- delegated: one main session that handed work to subagents (Agent/Task tool) or worktree agents.
- human-parallel: several main sessions you ran at the same time.
- mixed: several concurrent main sessions, with delegation as well.
- automated: a scheduled-task run (its first prompt is a `<scheduled-task …>` block); each is its own
  workflow, so routines don't read as you running agents in parallel.

Measured per workflow: cost (subagents included), wall time, active time, your prompts (attention),
concurrency, output (commits made by the workflow's own sessions: successful `git commit` calls,
including quiet ones), rework signals (a file edited in several sessions, merge
conflicts, reverts/resets), and interrupts. Every workflow carries a `person` (git user.email of the
repo, else $USER), so exports from several people can be pooled.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import pricing
from .parse import _is_human, _text

ACTIVE_GAP = timedelta(minutes=5)
_WT = re.compile(r"/\.(?:claude|codex)/worktrees/[^/]+")
_CONFLICT = re.compile(r"CONFLICT \(|Automatic merge failed|both modified:")
_UNDO = re.compile(r"\bgit\s+(?:revert|reset\s+--hard|checkout\s+--\s|restore\s)")
DELEGATE_TOOLS = ("Agent", "Task")
_COMMIT_CMD = re.compile(r"\bgit\b[^|;&]*\bcommit\b")
_COMMIT_OUT = re.compile(r"^\[[^\]\n]+ [0-9a-f]{7,}\]", re.M)  # "[main abc1234] message"


@dataclass
class Sess:
    id: str
    file: str
    repo: str
    cwd: str
    subagent: bool
    parent: str = ""
    start: datetime | None = None
    end: datetime | None = None
    active: list[tuple[datetime, datetime]] = field(default_factory=list)
    cost: float = 0.0
    prompts: int = 0
    interrupts: int = 0
    delegations: int = 0
    worktree: bool = False
    edited: set[str] = field(default_factory=set)
    conflicts: int = 0
    undos: int = 0
    commits: int = 0
    automated: bool = False


@dataclass
class Workflow:
    repo: str
    person: str
    mode: str
    start: datetime
    end: datetime
    sessions: int
    subagents: int
    cost: float
    active_h: float
    wall_h: float
    prompts: int
    interrupts: int
    peak_parallel: int
    avg_parallel: float
    commits: int
    shared_files: int
    conflicts: int
    undos: int
    session_ids: list[str] = field(default_factory=list)

    @property
    def cost_per_commit(self) -> float | None:
        return self.cost / self.commits if self.commits else None


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def repo_of(cwd: str) -> str:
    """The repo a directory belongs to: worktrees fold into their repo, else the git toplevel."""
    p = _WT.sub("", cwd.rstrip("/"))
    if p != cwd.rstrip("/"):
        return p
    try:
        r = subprocess.run(["git", "-C", p, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return _WT.sub("", r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return p


def load(root: Path) -> list[Sess]:
    out = []
    repo_cache: dict[str, str] = {}
    for f in sorted(root.glob("**/*.jsonl")):
        sub = f.parent.name == "subagents"
        s = Sess(f.stem, str(f), "", "", sub, parent=f.parent.parent.name if sub else "")
        seen: set[str] = set()
        commit_calls: set[str] = set()  # tool_use ids of Bash calls that ran git commit
        first_prompt = True
        last: datetime | None = None
        span_start: datetime | None = None
        with f.open(errors="replace") as fh:
            lines = list(fh)
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("cwd") and not s.cwd:
                s.cwd = d["cwd"]
                s.worktree = bool(_WT.search(d["cwd"]))
            ts = _ts(d.get("timestamp"))
            if ts:
                s.start = s.start or ts
                s.end = ts
                if last is None or ts - last > ACTIVE_GAP:
                    if span_start and last:
                        s.active.append((span_start, last))
                    span_start = ts
                last = ts
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            content = m.get("content")
            t = d.get("type")
            if t == "user":
                if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    body = _text(content)
                    s.conflicts += bool(_CONFLICT.search(body))
                    for b in content:
                        if isinstance(b, dict) and b.get("tool_use_id") in commit_calls and not b.get("is_error"):
                            s.commits += max(1, len(_COMMIT_OUT.findall(_text(b.get("content")))))
                    continue
                txt = _text(content)
                if first_prompt and txt.strip():
                    s.automated = txt.lstrip().startswith("<scheduled-task")
                    first_prompt = False
                if txt.startswith("[Request interrupted"):
                    s.interrupts += 1
                elif txt and not sub and _is_human(d, txt):
                    s.prompts += 1
            elif t == "assistant":
                for b in content or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name, inp = b.get("name", ""), b.get("input") or {}
                    if name in DELEGATE_TOOLS:
                        s.delegations += 1
                    elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and inp.get("file_path"):
                        s.edited.add(_WT.sub("", str(inp["file_path"])))
                    elif name == "Bash":
                        cmd = str(inp.get("command", ""))
                        s.undos += bool(_UNDO.search(cmd))
                        if _COMMIT_CMD.search(cmd):
                            commit_calls.add(b.get("id", ""))
                mid, u, model = m.get("id"), m.get("usage"), str(m.get("model", ""))
                if mid and u and mid not in seen and not model.startswith("<"):
                    seen.add(mid)
                    s.cost += pricing.cost(model, u) or 0.0
        if span_start and last:
            s.active.append((span_start, last))
        if not s.start or not s.cwd:
            continue
        s.repo = repo_cache.setdefault(s.cwd, repo_of(s.cwd))
        out.append(s)
    return out


def _person(repo: str) -> str:
    try:
        r = subprocess.run(["git", "-C", repo, "config", "user.email"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("USER", "unknown")


def _concurrency(spans: list[tuple[datetime, datetime]]) -> tuple[int, float]:
    """Peak and time-weighted average number of main sessions active at once (over time any was active)."""
    events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
    cur = peak = 0
    busy = weighted = 0.0
    prev = None
    for t, delta in events:
        if prev is not None and cur > 0:
            dt = (t - prev).total_seconds()
            busy += dt
            weighted += dt * cur
        cur += delta
        peak = max(peak, cur)
        prev = t
    return peak, (weighted / busy if busy else 1.0)


def group(sessions: list[Sess], gap_hours: float = 2.0, person=_person) -> list[Workflow]:
    mains = [s for s in sessions if not s.subagent and not s.automated]
    subs: dict[str, list[Sess]] = defaultdict(list)
    for s in sessions:
        if s.subagent:
            subs[s.parent].append(s)
    by_repo: dict[str, list[Sess]] = defaultdict(list)
    for s in mains:
        by_repo[s.repo].append(s)
    out = []
    automated: list[Workflow] = []
    for s in sessions:
        if s.automated and not s.subagent:
            out_auto = _measure(s.repo, [s], subs.get(s.id, []), person)
            out_auto.mode = "automated"
            by_repo.setdefault("\0auto", []).append(s)  # keep it out of human clustering
            automated.append(out_auto)
    for repo, ss in by_repo.items():
        if repo == "\0auto":
            continue
        ss.sort(key=lambda s: s.start or datetime.min)
        clusters: list[list[Sess]] = []
        for s in ss:
            if clusters and s.start and s.start - max(x.end or s.start for x in clusters[-1]) <= timedelta(hours=gap_hours):
                clusters[-1].append(s)
            else:
                clusters.append([s])
        for c in clusters:
            out.append(_measure(repo, c, [x for s in c for x in subs.get(s.id, [])], person))
    return sorted(out + automated, key=lambda w: w.start)


def _measure(repo, mains, subagents, person) -> Workflow:
    start, end = min(s.start for s in mains), max(s.end for s in mains + subagents)
    spans = [span for s in mains for span in s.active]
    peak, avg = _concurrency(spans)
    # Worktrees alone aren't delegation: the desktop app starts your own sessions in worktrees too.
    delegated = any(s.delegations for s in mains) or bool(subagents)
    multi = peak > 1
    mode = "mixed" if multi and delegated else "human-parallel" if multi else "delegated" if delegated else "solo"
    owners: dict[str, int] = defaultdict(int)
    for s in mains + subagents:
        for f in s.edited:
            owners[f] += 1
    union = sorted(spans)
    active = 0.0
    cur_a = cur_b = None
    for a, b in union:  # union of active spans, so parallel sessions don't double-count your time
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                active += (cur_b - cur_a).total_seconds()
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        active += (cur_b - cur_a).total_seconds()
    return Workflow(
        repo=repo, person=person(repo), mode=mode, start=start, end=end, sessions=len(mains), subagents=len(subagents),
        cost=sum(s.cost for s in mains + subagents), active_h=active / 3600, wall_h=(end - start).total_seconds() / 3600,
        prompts=sum(s.prompts for s in mains), interrupts=sum(s.interrupts for s in mains),
        peak_parallel=peak, avg_parallel=avg, commits=sum(s.commits for s in mains + subagents),
        shared_files=sum(1 for n in owners.values() if n > 1), conflicts=sum(s.conflicts for s in mains + subagents),
        undos=sum(s.undos for s in mains + subagents), session_ids=[s.id for s in mains])


def summary(wfs: list[Workflow], min_cost: float = 0.5) -> str:
    home = str(Path.home())
    lines = [f"{len(wfs)} workflows ({sum(w.sessions for w in wfs)} sessions, {sum(w.subagents for w in wfs)} subagents)", "",
             "By coordination mode:",
             f"  {'mode':15} {'n':>3} {'cost':>9} {'$/commit':>9} {'active h':>9} {'prompts/h':>9} {'peak ∥':>7} {'rework':>7}"]
    modes: dict[str, list[Workflow]] = defaultdict(list)
    for w in wfs:
        modes[w.mode].append(w)
    for mode in ("solo", "delegated", "human-parallel", "mixed", "automated"):
        g = modes.get(mode, [])
        if not g:
            continue
        cost, commits, active = sum(w.cost for w in g), sum(w.commits for w in g), sum(w.active_h for w in g)
        rework = sum(w.shared_files + w.conflicts + w.undos for w in g)
        lines.append(f"  {mode:15} {len(g):3} ${cost:8.2f} " + (f"${cost / commits:8.2f}" if commits else f"{'–':>9}")
                     + f" {active:9.1f} {sum(w.prompts for w in g) / active if active else 0:9.1f}"
                     f" {max(w.peak_parallel for w in g):7} {rework:7}")
    lines += ["", "Workflows (cost ≥ $%.2f):" % min_cost]
    for w in sorted(wfs, key=lambda w: -w.cost):
        if w.cost < min_cost:
            continue
        lines.append(f"  {w.start:%d %b %H:%M}  {w.mode:14} ${w.cost:7.2f}  {w.sessions} sess"
                     + (f" +{w.subagents} sub" if w.subagents else "")
                     + f"  {w.active_h:4.1f}h active / {w.wall_h:5.1f}h wall  {w.prompts:3} prompts  {w.commits:3} commits"
                     + (f"  rework: {w.shared_files} shared files, {w.conflicts} conflicts, {w.undos} undos"
                        if w.shared_files + w.conflicts + w.undos else "")
                     + f"  {w.repo.replace(home, '~')}")
    return "\n".join(lines)


def export(wfs: list[Workflow]) -> str:
    """JSON for pooling across people: no paths beyond the repo name, no prompts, no file names."""
    rows = []
    for w in wfs:
        d = asdict(w)
        d["repo"] = Path(w.repo).name
        d["start"], d["end"] = w.start.isoformat(), w.end.isoformat()
        del d["session_ids"]
        rows.append(d)
    return json.dumps(rows, indent=1)
