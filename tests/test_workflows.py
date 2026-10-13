"""Workflow grouping and coordination modes on synthetic sessions (no git calls: paths don't exist)."""

import json
import tempfile
import unittest
from pathlib import Path

from cc_retro import workflows

REPO = "/nowhere/app"


def rec(kind, ts, cwd=REPO, **kw):
    d = {"type": kind, "timestamp": f"2026-10-01T{ts}:00Z", "cwd": cwd}
    d.update(kw)
    return d


def prompt(ts, text, cwd=REPO):
    return rec("user", ts, cwd, message={"content": text})


def tool(ts, tid, name, inp, cwd=REPO):
    return rec("assistant", ts, cwd, message={"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]})


def result(ts, tid, text, err=False, cwd=REPO):
    return rec("user", ts, cwd, message={"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": err, "content": text}]})


class Workflows(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def write(self, name, recs, sub_of=None):
        d = self.root / "proj" / (f"{sub_of}/subagents" if sub_of else "")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    def run_group(self):
        return workflows.group(workflows.load(self.root), 2.0, person=lambda r: "me@x")

    def test_modes_commits_and_rework(self):
        # two of your sessions at once in the same repo (one in a worktree): human-parallel, sharing a file
        self.write("a", [prompt("10:00", "build import"),
                         tool("10:01", "e1", "Edit", {"file_path": f"{REPO}/lib/x.ts"}),
                         tool("10:02", "c1", "Bash", {"command": "git add -A && git commit -qm wip"}),
                         result("10:02", "c1", ""),
                         tool("10:03", "c2", "Bash", {"command": "git commit -m again"}),
                         result("10:03", "c2", "nothing to commit", err=True)])
        wt = f"{REPO}/.claude/worktrees/feat-1"
        self.write("b", [prompt("10:01", "fix tests", wt),
                         tool("10:02", "e2", "Edit", {"file_path": f"{wt}/lib/x.ts"}, wt),
                         tool("10:04", "c3", "Bash", {"command": "git merge feat"}, wt),
                         result("10:04", "c3", "CONFLICT (content): Merge conflict in lib/x.ts", wt),
                         tool("10:05", "c4", "Bash", {"command": "git commit -m merged"}, wt),
                         result("10:05", "c4", "[main 1a2b3c4] merged", cwd=wt)])
        # a scheduled run in the same repo and window: its own automated workflow
        self.write("s", [prompt("10:02", '<scheduled-task name="nightly">do it</scheduled-task>'), tool("10:03", "x", "Read", {})])
        # 5 hours later, a session that delegates to a subagent: a separate, delegated workflow
        self.write("c", [prompt("15:00", "plan the refactor"), tool("15:01", "t", "Agent", {"prompt": "go"})])
        self.write("sub1", [rec("assistant", "15:02", message={"content": []})], sub_of="c")
        wfs = {w.mode: w for w in self.run_group()}
        self.assertEqual(set(wfs), {"human-parallel", "automated", "delegated"})
        hp = wfs["human-parallel"]
        self.assertEqual((hp.sessions, hp.peak_parallel, hp.prompts), (2, 2, 2))
        self.assertEqual(hp.commits, 2)  # the quiet commit and the merge commit; the failed one doesn't count
        self.assertEqual((hp.shared_files, hp.conflicts), (1, 1))  # the worktree path folds into the repo
        self.assertEqual(wfs["delegated"].subagents, 1)
        self.assertEqual(wfs["automated"].prompts, 0)

    def test_export_has_no_paths_or_ids(self):
        self.write("a", [prompt("10:00", "secret plan"), tool("10:01", "e", "Edit", {"file_path": f"{REPO}/secret.ts"})])
        out = workflows.export(self.run_group())
        self.assertNotIn("secret", out)
        self.assertNotIn("/nowhere", out)
        self.assertEqual(json.loads(out)[0]["repo"], "app")


if __name__ == "__main__":
    unittest.main()
