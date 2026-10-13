"""Pilot: does cc-retro's timeout rule change agent behaviour, and at what cost?

Arms: `base` (neutral CLAUDE.md) vs `rule` (same + the exact remedy cc-retro emits).
Tasks: slow test suite / slow build (target: ~150 s commands) and fast twins (control:
does the rule add needless backgrounding/polling where nothing is slow?).

    PYTHONPATH=src python3 eval/rule_pilot/run.py --trials 5 --jobs 4 [--only tests_fast] [--budget 40]

Each run: fresh temp copy of the fixture → headless `claude -p` with only project settings
(no user CLAUDE.md, plugins, hooks or MCP) → grade outside the agent → move the transcript
out of ~/.claude/projects into results/. One JSON line per run in results/runs.jsonl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent / "src"))
from cc_retro.analyze import remedy_for  # noqa: E402

RULE = remedy_for("timeout", "Bash: hit the tool timeout")[0]
BASE_MD = "# Project notes\n\n- Python 3 standard library only; no dependencies.\n"
WORK = Path(tempfile.gettempdir()) / "ccr-pilot"
RESULTS = HERE / "results"
PROJECTS = Path.home() / ".claude" / "projects"

TASKS = {
    # name: (fixture, delay, protected file, prompt)
    "tests_slow": ("tests_task", "15", "test_inventory.py",
                   "The test suite is failing. Find and fix the bug in the code (not the tests), "
                   "then run the full test suite to confirm everything passes."),
    "tests_fast": ("tests_task", "0.05", "test_inventory.py",
                   "The test suite is failing. Find and fix the bug in the code (not the tests), "
                   "then run the full test suite to confirm everything passes."),
    "build_slow": ("build_task", "13", "build.py",
                   "`python3 build.py` is failing. Fix the root cause rather than changing the build "
                   "script, then run the full build to confirm it succeeds."),
    "build_fast": ("build_task", "0.05", "build.py",
                   "`python3 build.py` is failing. Fix the root cause rather than changing the build "
                   "script, then run the full build to confirm it succeeds."),
}
CLAUDE_FLAGS = [
    "--setting-sources", "project,local", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--disable-slash-commands", "--permission-mode", "acceptEdits",
    "--allowedTools", "Bash Read Edit Write Glob Grep",
    "--output-format", "json", "--max-budget-usd", "3",
]
GRADE = {
    "tests_task": "import time,unittest,sys; time.sleep=lambda *_: None; "
                  "r=unittest.main(module=None, argv=['x','discover','-q'], exit=False); sys.exit(not r.result.wasSuccessful())",
    "build_task": "import time,sys; time.sleep=lambda *_: None; import build; sys.exit(build.main())",
}

lock = threading.Lock()
spent = 0.0
halt = threading.Event()  # set on a usage limit: later runs would only record the limit, not behaviour
LIMIT_MARKERS = ("hit your session limit", "usage limit", "rate limit")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def setup(task: str, arm: str, run_id: str) -> tuple[Path, str]:
    fixture, delay, protected, _ = TASKS[task]
    d = WORK / run_id
    shutil.copytree(HERE / "fixtures" / fixture, d)
    for f in d.iterdir():
        f.write_text(f.read_text().replace("__DELAY__", delay))
    (d / "CLAUDE.md").write_text(BASE_MD + (f"- {RULE}\n" if arm == "rule" else ""))
    return d, sha(d / protected)


def behaviour(transcript: Path) -> dict:
    """What the agent did with commands: foreground vs background, timeouts, polling."""
    from cc_retro.parse import load_session

    b = {"bash": 0, "background": 0, "timeout_param": 0, "max_timeout_ms": 0, "polls": 0,
         "tool_timeouts": 0, "tool_errors": 0, "confirmed": False, "model": None}
    for line in transcript.open():
        d = json.loads(line)
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        for blk in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use":
                inp = blk.get("input") or {}
                if blk.get("name") == "Bash":
                    b["bash"] += 1
                    b["background"] += bool(inp.get("run_in_background"))
                    if inp.get("timeout"):
                        b["timeout_param"] += 1
                        b["max_timeout_ms"] = max(b["max_timeout_ms"], int(inp["timeout"]))
                    if ".output" in str(inp.get("command", "")):  # reading a background task's output file
                        b["polls"] += 1
                elif blk.get("name") in ("BashOutput", "TaskOutput", "Monitor"):
                    b["polls"] += 1
                elif blk.get("name") == "Read" and ".output" in str(inp.get("file_path", "")):
                    b["polls"] += 1
            if blk.get("type") == "tool_result":
                txt = json.dumps(blk.get("content"))
                if ("Ran 10 tests" in txt and "OK" in txt and "FAILED" not in txt) or "BUILD OK" in txt:
                    b["confirmed"] = True
        if d.get("type") == "assistant" and m.get("model") not in (None, "<synthetic>"):
            b["model"] = m["model"]
    s = load_session(transcript, "pilot")
    if s:
        b["tool_timeouts"] = sum(f.kind == "timeout" for f in s.friction)
        b["tool_errors"] = s.tool_errors
    return b


def run_one(task: str, arm: str, trial: int, budget: float) -> dict | None:
    global spent
    with lock:
        if spent >= budget or halt.is_set():
            return None
    run_id = f"{task}-{arm}-{trial}-{uuid.uuid4().hex[:6]}"
    d, protected_sha = setup(task, arm, run_id)
    fixture, _, protected, prompt = TASKS[task]
    t0 = time.time()
    try:
        p = subprocess.run(["claude", "-p", *CLAUDE_FLAGS, prompt], cwd=d, capture_output=True, text=True, timeout=1500)
        res = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, ValueError) as e:
        res = {"is_error": True, "result": f"harness: {e!r}"}
    wall = time.time() - t0
    graded = subprocess.run([sys.executable, "-c", GRADE[fixture]], cwd=d, capture_output=True, timeout=60)
    tampered = sha(d / protected) != protected_sha
    if res.get("is_error") and any(m in str(res.get("result", "")).lower() for m in LIMIT_MARKERS):
        # A limit mid-run truncates the transcript, so the row would look like a real (often failed) run.
        halt.set()
        print(f"usage limit hit in {run_id}; stopping. Re-run the same command to resume.", flush=True)
        sid = res.get("session_id")
        for h in PROJECTS.glob(f"*/{sid}.jsonl") if sid else []:
            (RESULTS / "transcripts" / "limit-hit").mkdir(parents=True, exist_ok=True)
            shutil.move(h, RESULTS / "transcripts" / "limit-hit" / f"{run_id}.jsonl")
            shutil.rmtree(h.parent, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)
        return None
    row = {
        "run_id": run_id, "task": task, "arm": arm, "trial": trial,
        "success": graded.returncode == 0 and not tampered, "tampered": tampered,
        "cost": res.get("total_cost_usd", 0.0), "turns": res.get("num_turns"), "wall_s": round(wall),
        "is_error": res.get("is_error"),
    }
    sid = res.get("session_id")
    hits = list(PROJECTS.glob(f"*/{sid}.jsonl")) if sid else []
    if hits:
        dest = RESULTS / "transcripts" / f"{run_id}.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(hits[0], dest)
        shutil.rmtree(hits[0].parent, ignore_errors=True)  # the per-cwd project dir is ours alone
        row.update(behaviour(dest))
    shutil.rmtree(d, ignore_errors=True)
    with lock:
        spent += row["cost"] or 0.0
        with (RESULTS / "runs.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"[${spent:6.2f}] {run_id:28} ok={row['success']!s:5} bg={row.get('background')} "
              f"tmo={row.get('tool_timeouts')} turns={row['turns']} ${row['cost']:.2f} {row['wall_s']}s", flush=True)
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", nargs="*", default=list(TASKS))
    ap.add_argument("--budget", type=float, default=40.0, help="stop launching runs past this spend (USD)")
    ap.add_argument("--seed", type=int, default=12)
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    done = set()
    if (RESULTS / "runs.jsonl").exists():  # resume: skip trials that already have a clean result
        for line in (RESULTS / "runs.jsonl").open():
            r = json.loads(line)
            if not r.get("is_error"):
                done.add((r["task"], r["arm"], r["trial"]))
    plan = [(t, arm, i) for t in a.only for arm in ("base", "rule") for i in range(a.trials)
            if (t, arm, i) not in done]
    random.Random(a.seed).shuffle(plan)  # interleave arms so drift (load, time of day) hits both
    print(f"rule under test: {RULE}\n{len(plan)} runs, {a.jobs} parallel, budget ${a.budget}", flush=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(lambda x: run_one(*x, a.budget), plan))


if __name__ == "__main__":
    main()
