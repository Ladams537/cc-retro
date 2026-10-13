"""A handoff note for continuing a session in a fresh one, built from its transcript with no model call.

Asking a long session for a summary after a break re-pays its whole context (the cache has
expired), which is the cost a fresh session is meant to avoid. The transcript already holds what a
new session needs: the goal, the recent prompts, the last summary the model gave, the files it
changed and read, the last commands and how they ended, and any open todos. This module writes
that out as markdown, small enough to paste (or to point a new session at with "Read <path>").
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from .parse import DEFAULT_ROOT, _is_human, _text

OUT_DIR = Path.home() / ".claude" / "cc-retro" / "handoffs"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_PASTE = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.S)
_NOISE = ("Shell cwd was reset", "[exited with code")


def _clean_prompt(txt: str) -> str:
    """Your words, with pasted blocks collapsed (they're context you gave, not the request)."""
    return " ".join(_PASTE.sub(lambda m: f"[pasted text, {len(m.group(0)):,} chars]", txt).split())


def _last_line(body: str) -> str:
    lines = [x for x in body.strip().splitlines() if x.strip() and not x.strip().startswith(_NOISE)]
    return lines[-1].strip()[:120] if lines else ""


def _commits(cwd: str, since: str | None) -> list[str]:
    if not cwd or not since:
        return []
    try:
        r = subprocess.run(["git", "-C", cwd, "log", f"--since={since}", "--format=%h %s", "-15"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return r.stdout.splitlines() if r.returncode == 0 else []


def latest_transcript(root: Path = DEFAULT_ROOT, project: str | None = None) -> Path | None:
    files = [f for f in root.glob("*/*.jsonl") if not project or project.lower() in f.parent.name.lower()]
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def find_transcript(session: str, root: Path = DEFAULT_ROOT) -> Path | None:
    hits = list(root.glob(f"*/{session}*.jsonl"))
    return hits[0] if len(hits) == 1 else None


def build(transcript: Path, recent: int = 5, max_summary: int = 1500) -> str:
    title, cwd, branch, first_ts, last_ts = "", "", "", None, None
    prompts: list[str] = []
    last_text = ""
    edited: dict[str, int] = {}  # path -> edits; insertion order is refreshed so the latest edit is last
    read: Counter = Counter()
    commands: list[tuple[str, str]] = []  # (command, outcome)
    pending: dict[str, str] = {}  # tool_use_id -> command
    todos: list[dict] = []
    with transcript.open(errors="replace") as fh:
        lines = list(fh)
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "custom-title":
            title = d.get("customTitle", title)
        if d.get("cwd"):
            cwd = d["cwd"]
        if d.get("gitBranch") and d["gitBranch"] != "HEAD":
            branch = d["gitBranch"]
        ts = d.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = m.get("content")
        if t == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    if isinstance(b, dict) and b.get("tool_use_id") in pending:
                        cmd = pending.pop(b["tool_use_id"])
                        body = _text(b.get("content"))
                        outcome = "failed" if b.get("is_error") else "ok"
                        tail = _last_line(body)
                        commands.append((cmd, f"{outcome}{': ' + tail if tail else ''}"))
                continue
            txt = _text(content)
            if txt and _is_human(d, _PASTE.sub("", txt).strip() or txt):  # a paste starts with "<", like system text
                prompts.append(_clean_prompt(txt))
        elif t == "assistant":
            txt = _text(content)
            if txt.strip():
                last_text = txt.strip()
            for b in content or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name", ""), b.get("input") or {}
                path = inp.get("file_path") or inp.get("notebook_path")
                if name in EDIT_TOOLS and path:
                    edited[path] = edited.pop(path, 0) + 1
                elif name == "Read" and path:
                    read[path] += 1
                elif name == "Bash" and inp.get("command"):
                    pending[b.get("id", "")] = " ".join(str(inp["command"]).split())[:160]
                elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    todos = inp["todos"]

    def rel(p: str) -> str:
        return p[len(cwd) + 1:] if cwd and p.startswith(cwd + "/") else p.replace(str(Path.home()), "~")

    def when(s: str | None) -> str:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M") if s else "?"
        except ValueError:
            return "?"

    out = [f"# Handoff: {title or (prompts[0][:60] if prompts else transcript.stem)}", "",
           f"Continuing session `{transcript.stem[:8]}` ({when(first_ts)} → {when(last_ts)}) in "
           f"`{cwd.replace(str(Path.home()), '~')}`" + (f" on branch `{branch}`" if branch else "") + ".",
           "Read this, check the files below as needed, and carry on. Don't redo finished work.", ""]
    if prompts:
        out += ["## Goal (how the session started)", "", f"> {prompts[0][:700]}", ""]
    if len(prompts) > 1:
        out += ["## Most recent requests", ""] + [f"- {p[:300]}" for p in prompts[-recent:] if p != prompts[0]] + [""]
    open_todos = [x for x in todos if x.get("status") != "completed"]
    if open_todos:
        out += ["## Open todos", ""] + [f"- [{x.get('status', '')}] {x.get('content', '')}" for x in open_todos] + [""]
    if last_text:
        s = last_text if len(last_text) <= max_summary else "…" + last_text[-max_summary:]
        out += ["## Where it left off (last message)", "", s, ""]
    commits = _commits(cwd, first_ts)
    if commits:
        out += ["## Commits made in this session (newest first)", ""] + [f"- {c}" for c in commits] + [""]
    if edited:
        latest = list(edited.items())[::-1][:25]
        out += ["## Files changed (most recent first)", ""] + [f"- `{rel(p)}`" + (f" ({n} edits)" if n > 1 else "")
                                                             for p, n in latest] + [""]
    if read:
        out += ["## Files read (most often)", ""] + [f"- `{rel(p)}`" for p, _ in read.most_common(10)] + [""]
    if commands:
        out += ["## Last commands", ""] + [f"- `{c}` → {o}" for c, o in commands[-8:]] + [""]
    return "\n".join(out).rstrip() + "\n"


def write(transcript: Path) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{transcript.stem}.md"
    path.write_text(build(transcript))
    return path
