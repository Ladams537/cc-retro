"""cc-retro: find where your Claude Code sessions get stuck, and what to put in CLAUDE.md."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from . import analyze, classify, context, costs, handoff, hooks, report, split, upkeep, workflows
from .parse import DEFAULT_ROOT, load_all


def _since(s: str | None) -> datetime | None:
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([dhw])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}[unit]
        return datetime.now().astimezone() - delta
    return datetime.fromisoformat(s).astimezone()


def _load(args):
    sessions = load_all(Path(args.root).expanduser(), _since(args.since))
    if args.project:
        sessions = [s for s in sessions if args.project.lower() in s.project.lower()]
    if not sessions:
        sys.exit(f"No sessions found under {args.root}" + (f" matching '{args.project}'" if args.project else ""))
    if args.classify:
        classify.classify(sessions)
    return sessions


def cmd_report(args) -> None:
    sessions = _load(args)
    clusters = analyze.cluster(sessions)
    if args.suggest:
        _llm_remedies(clusters, args.model)
    html = report.render(sessions, clusters, analyze.totals(sessions), redact=args.redact, top=args.top)
    out = Path(args.out).expanduser()
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}  ({len(sessions)} sessions, {sum(len(c.events) for c in clusters)} friction events)")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


def cmd_summary(args) -> None:
    sessions = _load(args)
    t = analyze.totals(sessions)
    clusters = analyze.cluster(sessions)
    print(f"{t.sessions} sessions · ${t.cost:,.2f} · {t.active_h:.1f}h active · "
          f"{t.tool_calls:,} tool calls ({t.tool_errors / max(t.tool_calls, 1):.1%} failed) · cache hit {t.cache_hit:.0%}\n")
    print("Top friction:")
    for c in clusters[: args.top]:
        print(f"  {len(c.events):>3}× {len(c.sessions):>2}s  [{analyze.KIND_LABEL[c.kind]}] {c.signature}")
        if c.remedy:
            print(f"           → {c.remedy}\n             in {c.target}")
        elif c.advice:
            print(f"           ({c.advice})")
    print("\nSpend by project:")
    for p, v in list(t.by_project.items())[:8]:
        print(f"  ${v:>8,.2f}  {p}")


def cmd_json(args) -> None:
    sessions = _load(args)
    clusters = analyze.cluster(sessions)
    json.dump(
        {
            "sessions": [
                {"id": s.id, "project": s.project, "title": s.title, "start": s.start.isoformat() if s.start else None,
                 "cost": round(s.cost, 4), "active_s": round(s.active_s), "human_turns": s.human_turns,
                 "tool_calls": s.tool_calls, "tool_errors": s.tool_errors, "cache_hit": round(s.cache_hit, 4)}
                for s in sessions
            ],
            "clusters": [
                {"kind": c.kind, "signature": c.signature, "count": len(c.events), "sessions": len(c.sessions),
                 "remedy": c.remedy, "est_cost": round(c.est_cost, 4)}
                for c in clusters
            ],
        },
        sys.stdout,
        indent=2,
    )


def _llm_remedies(clusters: list[analyze.Cluster], model: str | None) -> None:
    """Ask headless Claude Code to draft rules for clusters the built-in library doesn't cover."""
    if not shutil.which("claude"):
        print("`claude` not on PATH; skipping --suggest", file=sys.stderr)
        return
    todo = [c for c in clusters[:25] if c.kind in ("tool_error", "retry_loop", "correction", "interrupt", "timeout")
            and (c.remedy is None or c.kind in ("correction", "interrupt"))][:10]
    if not todo:
        return
    blocks = []
    for i, c in enumerate(todo):
        ex = "\n".join(f"  - {e.snippet[:500]}" for e in c.events[:3])
        blocks.append(f"[{i}] kind={c.kind} count={len(c.events)} sessions={len(c.sessions)} signature={c.signature}\n{ex}")
    prompt = (
        "These are recurring friction patterns mined from my Claude Code transcripts. For each, write ONE concrete, "
        "imperative CLAUDE.md rule (max 30 words) that would have prevented it, or null if it's not preventable by "
        "instruction (e.g. a genuine bug in my code). For user corrections/interrupts, infer the underlying preference "
        "from the examples. Reply with only a JSON array of strings or nulls, one per item, in order.\n\n" + "\n\n".join(blocks)
    )
    cmd = ["claude", "-p", "--output-format", "text", "--tools", "", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    print(f"Asking Claude to draft rules for {len(todo)} clusters…", file=sys.stderr)
    try:
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300).stdout
        arr = json.loads(out[out.index("["): out.rindex("]") + 1])
    except (subprocess.SubprocessError, ValueError) as e:
        print(f"--suggest failed: {e}", file=sys.stderr)
        return
    for c, rule in zip(todo, arr):
        if isinstance(rule, str) and rule.strip():
            c.remedy = rule.strip() + " (drafted by Claude)"


def cmd_cost(args) -> None:
    ctxs = costs.load_contexts(Path(args.root).expanduser())
    if args.project:
        ctxs = [c for c in ctxs if args.project.lower() in c.project.lower()]
    b = costs.breakdown(ctxs)
    print(costs.summary(b))
    if args.context:
        print()
        print(context.summary(context.analyse([c.file for c in ctxs], args.top_items),
                              b.conversation + b.new_context + b.rebuild))


def cmd_split(args) -> None:
    sessions = split.load(Path(args.root).expanduser())
    if args.project:
        sessions = [s for s in sessions if args.project.lower() in s.project.lower()]
    labels = split.classify(sessions) if args.classify else None
    print(split.report(sessions, labels, args.idle_hours, args.handoff))


def cmd_handoff(args) -> None:
    root = Path(args.root).expanduser()
    t = handoff.find_transcript(args.session, root) if args.session else handoff.latest_transcript(root, args.project)
    if not t:
        sys.exit("No matching session" + (f" for '{args.session}' (give more of the id?)" if args.session else ""))
    path = handoff.write(t)
    if args.print:
        print(path.read_text())
    print(f"Handoff written to {str(path).replace(str(Path.home()), '~')}\n"
          f"Start a new session and send: Read {path} and continue from it.")


def cmd_workflows(args) -> None:
    wfs = workflows.group(workflows.load(Path(args.root).expanduser()), args.gap)
    if args.project:
        wfs = [w for w in wfs if args.project.lower() in w.repo.lower()]
    print(workflows.export(wfs) if args.json else workflows.summary(wfs))


def cmd_upkeep(args) -> None:
    root = Path(args.root).expanduser()
    if args.roots:
        upkeep.SEARCH_ROOTS = upkeep.parse_roots(args.roots)
    if args.hook:
        msg = upkeep.hook(root)
        if msg:
            print(json.dumps({"systemMessage": msg}))
        return
    if args.apply:
        d = upkeep.latest_proposal()
        if not d:
            sys.exit("No proposal yet; run `cc-retro upkeep` first.")
        entries = [e for f in upkeep.find_claude_mds() for e in upkeep.split_entries(f)]
        for f in upkeep.apply(d, entries):
            print(f"wrote {f.replace(upkeep.HOME, '~')}")
        return
    d, actions, notes = upkeep.run(root, args.stale_days, None if args.no_llm else (args.model or "haiku"))
    for n in notes:
        print(n)
    counts = {op: sum(a.op == op for a in actions) for op in ("recurred", "archive", "move", "retest", "flag")}
    print(", ".join(f"{v} {k}" for k, v in counts.items()))
    print(f"Proposal: {d / 'proposal.md'}\nNothing has been edited. Review it, then `cc-retro upkeep --apply`.")


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=str(DEFAULT_ROOT), help="transcripts dir (default ~/.claude/projects)")
    common.add_argument("--since", help="only sessions modified within e.g. 7d, 48h, 2w, or an ISO date")
    common.add_argument("--project", help="substring filter on project path")
    common.add_argument("--top", type=int, default=15, help="number of friction clusters to show")
    common.add_argument("--classify", action="store_true",
                        help="label your prompts with `claude -p` (Haiku) to find corrections; cached, ~$0.015 per 40 new prompts")
    p = argparse.ArgumentParser(prog="cc-retro", description=__doc__)
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("report", parents=[common], help="write an HTML report (default)")
    r.add_argument("-o", "--out", default="cc-retro.html")
    r.add_argument("--open", action="store_true", help="open in browser")
    r.add_argument("--redact", action="store_true", help="omit snippets and session titles (for sharing)")
    r.add_argument("--suggest", action="store_true", help="use `claude -p` to draft rules for unrecognised clusters")
    r.add_argument("--model", help="model for --suggest")
    r.set_defaults(fn=cmd_report)
    sub.add_parser("summary", parents=[common], help="print a terminal summary").set_defaults(fn=cmd_summary)
    sub.add_parser("json", parents=[common], help="dump sessions and clusters as JSON").set_defaults(fn=cmd_json)

    c = sub.add_parser("cost", help="split spend into output, startup context, conversation and cache rebuilds")
    c.add_argument("--root", default=str(DEFAULT_ROOT), help="transcripts dir (default ~/.claude/projects)")
    c.add_argument("--project", help="substring filter on project path")
    c.add_argument("--context", action="store_true", help="also break conversation context down by tool and item")
    c.add_argument("--top-items", type=int, default=15, help="with --context: most expensive single items to list")
    c.set_defaults(fn=cmd_cost)
    sp = sub.add_parser("split", help="estimate what splitting long sessions at task boundaries would save")
    sp.add_argument("--root", default=str(DEFAULT_ROOT), help="transcripts dir (default ~/.claude/projects)")
    sp.add_argument("--project", help="substring filter on project path")
    sp.add_argument("--classify", action="store_true", help="label new-task prompts with `claude -p` (Haiku, cached)")
    sp.add_argument("--idle-hours", type=float, default=1.0, help="a gap this long before a prompt is a boundary")
    sp.add_argument("--handoff", type=int, default=split.HANDOFF_TOKENS, help="tokens a fresh session needs to pick up")
    sp.set_defaults(fn=cmd_split)
    ho = sub.add_parser("handoff", help="write a handoff note for continuing a session in a fresh one (no model call)")
    ho.add_argument("--root", default=str(DEFAULT_ROOT), help="transcripts dir (default ~/.claude/projects)")
    ho.add_argument("--session", help="session id or prefix (default: the most recently active session)")
    ho.add_argument("--project", help="with no --session: the latest session whose project path contains this")
    ho.add_argument("--print", action="store_true", help="also print the note")
    ho.set_defaults(fn=cmd_handoff)
    wf = sub.add_parser("workflows", help="group sessions into workflows and compare coordination modes")
    wf.add_argument("--root", default=str(DEFAULT_ROOT), help="transcripts dir (default ~/.claude/projects)")
    wf.add_argument("--project", help="substring filter on repo path")
    wf.add_argument("--gap", type=float, default=2.0, help="hours between sessions that still count as one workflow")
    wf.add_argument("--json", action="store_true", help="export for pooling across people (no prompts, paths or file names)")
    wf.set_defaults(fn=cmd_workflows)
    h = sub.add_parser("hook", help="Claude Code hook entry points (read the hook JSON on stdin)")
    h.add_argument("name", choices=["idle-resume"])
    h.set_defaults(fn=lambda a: hooks.main(a.name))
    u = sub.add_parser("upkeep", help="propose CLAUDE.md maintenance: stale, misplaced, duplicate, due a re-test")
    u.add_argument("--root", default=str(DEFAULT_ROOT), help="transcripts dir (default ~/.claude/projects)")
    u.add_argument("--roots", help="where to look for CLAUDE.md files, separated by ':' "
                   "(default $CC_RETRO_ROOTS, else ~/.claude plus the current git repo)")
    u.add_argument("--stale-days", type=int, default=90, help="flag entries whose terms haven't come up for this long")
    u.add_argument("--model", help="model that annotates entries (default haiku)")
    u.add_argument("--no-llm", action="store_true", help="don't call `claude -p`; new entries get no kind/anchors, so most checks skip them")
    u.add_argument("--apply", action="store_true", help="carry out the latest proposal (archive and move only)")
    u.add_argument("--hook", action="store_true", help="SessionStart mode: one line if a new model appeared")
    u.set_defaults(fn=cmd_upkeep)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("report", "summary", "json", "cost", "split", "handoff", "workflows", "hook", "upkeep", "-h", "--help"):
        argv.insert(0, "report")
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
