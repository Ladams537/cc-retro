import json
import tempfile
import unittest
from pathlib import Path

from cc_retro import analyze, pricing, report
from cc_retro.analyze import placement
from cc_retro.parse import bash_cause, load_all

USAGE = {"input_tokens": 10, "output_tokens": 1000, "cache_read_input_tokens": 100_000,
         "cache_creation_input_tokens": 0, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}


def ev(t, ts, **kw):
    return {"type": t, "sessionId": "s1", "cwd": "/x/Projects/demo", "timestamp": f"2026-10-01T10:{ts:02d}:00Z", **kw}


def assistant(ts, mid, *blocks):
    return ev("assistant", ts, message={"id": mid, "model": "claude-opus-5-5", "role": "assistant", "usage": USAGE, "content": list(blocks)})


def tool_use(i, name, **inp):
    return {"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp}


def result(ts, i, text, err=False, **kw):
    return ev("user", ts, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{i}", "content": text, "is_error": err}]}, **kw)


def human(ts, text):
    return ev("user", ts, origin={"kind": "human"}, message={"role": "user", "content": text})


TRANSCRIPT = [
    human(0, "build the thing"),
    # One API response split over two lines with identical usage: must be counted once.
    assistant(1, "m1", {"type": "thinking", "thinking": ""}),
    assistant(1, "m1", tool_use(1, "Bash", command="sleep 60")),
    result(2, 1, "<tool_use_error>Blocked: standalone sleep 60. Use Monitor.</tool_use_error>", err=True),
    assistant(3, "m2", tool_use(2, "Bash", command="cat a.txt; ls missing")),
    result(4, 2, "Exit code 1\nhello\nls: missing: No such file or directory", err=True),
    assistant(5, "m3", tool_use(3, "Bash", command="curl -s localhost:3000")),
    result(6, 3, "Exit code 7\n000", err=True),
    ev("user", 7, message={"role": "user", "content": "[Request interrupted by user for tool use]"}),
    human(8, "no, that's wrong — use the dev server"),
    # The same MCP call failing three times in a row is a loop.
    assistant(8, "m6", tool_use(6, "mcp__paper__get_guide")),
    result(8, 6, 'MCP server "paper" is not connected', err=True),
    assistant(8, "m7", tool_use(7, "mcp__paper__get_guide")),
    result(8, 7, 'MCP server "paper" is not connected', err=True),
    assistant(8, "m8", tool_use(8, "mcp__paper__get_guide")),
    result(8, 8, 'MCP server "paper" is not connected', err=True),
    ev("system", 9, subtype="api_error", error="529 Overloaded"),
    human(9, "Try again"),
    assistant(9, "m4", tool_use(4, "Edit", file_path="/x/Projects/demo/src/a.ts")),
    result(10, 4, "The user doesn't want to proceed with this tool use.", err=True, toolDenialKind="user-rejected"),
    assistant(11, "m5", tool_use(5, "Bash", command="npx vitest run")),
    result(15, 5, "ok"),
    ev("custom-title", 15, customTitle="Demo session"),
]


class RetroTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "-x-Projects-demo"
        d.mkdir()
        (d / "s1.jsonl").write_text("\n".join(json.dumps(e) for e in TRANSCRIPT))
        self.sessions = load_all(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_basics(self):
        (s,) = self.sessions
        self.assertEqual(s.title, "Demo session")
        self.assertEqual(s.human_turns, 3)
        self.assertEqual(sum(s.models.values()), 8)  # m1 deduped
        self.assertAlmostEqual(s.cost, 8 * pricing.cost("claude-opus-5-5", USAGE))
        self.assertEqual(s.tool_errors, 6)  # denial isn't a tool error
        self.assertTrue(s.prompts[2].after_failure)
        self.assertIn("dev server", s.prompts[1].text)

    def test_friction_kinds(self):
        kinds = sorted(f.kind for f in self.sessions[0].friction)
        # Three *different* Bash failures are not a loop; three identical MCP failures are.
        # Corrections need --classify (an LLM); without it the regex-free parser reports none.
        self.assertEqual(kinds, ["api_error", "denial", "interrupt", "retry", "retry_loop", "slow_tool"] + ["tool_error"] * 6)

    def test_bash_causes(self):
        sigs = {f.signature for f in self.sessions[0].friction if f.kind == "tool_error" and f.tool == "Bash"}
        self.assertEqual(sigs, {"Bash: harness blocked a sleep", "Bash: probe chain: last lookup came back empty",
                                "Bash: connection refused (server not running)"})
        self.assertEqual(bash_cause("grep foo x", "Exit code 1"), "grep/test found nothing (exit 1)")
        self.assertEqual(bash_cause("uv run x.py", "Exit code 1\nTraceback (most recent call last):"), "Python traceback")

    def test_clusters_and_report(self):
        clusters = analyze.cluster(self.sessions)
        by_sig = {c.signature: c for c in clusters}
        self.assertIn("Monitor", by_sig["Bash: harness blocked a sleep"].remedy)
        self.assertIsNone(by_sig["User asked to retry after an API error / limit"].remedy)
        self.assertIsNotNone(by_sig["User asked to retry after an API error / limit"].advice)
        html = report.render(self.sessions, clusters, analyze.totals(self.sessions))
        self.assertIn("Demo session", html)
        redacted = report.render(self.sessions, clusters, analyze.totals(self.sessions), redact=True)
        self.assertNotIn("Demo session", redacted)
        self.assertNotIn("dev server", redacted)
        self.assertIn('data-target="/x/Projects/demo/CLAUDE.md"', html)

    def test_pricing_prefix(self):
        self.assertEqual(pricing.rates("claude-opus-5-5")[0], 4.0)
        self.assertEqual(pricing.rates("claude-opus-5")[0], 5.0)
        self.assertIsNone(pricing.rates("gpt-9"))

    def test_placement(self):
        home = analyze.HOME
        p = f"{home}/Projects"
        # One module → that module's directory.
        self.assertEqual(placement([f"{p}/app/lib/import/x.ts", f"{p}/app/lib/import/y.ts"]), "~/Projects/app/lib/import/CLAUDE.md")
        # Two projects → their nearest shared parent, not global.
        self.assertEqual(placement([f"{p}/a/src/x.rs", f"{p}/b"]), "~/Projects/CLAUDE.md")
        # Worktrees fold into their repo; scratch paths don't count.
        self.assertEqual(placement([f"{p}/app/.claude/worktrees/ui-1a2b3c/lib/x.ts", "/private/tmp/scratch/a.py"]), "~/Projects/app/lib/CLAUDE.md")
        # Only something spanning the home dir is global.
        self.assertEqual(placement([home, f"{p}/a"]), analyze.GLOBAL)
        self.assertEqual(placement([]), analyze.GLOBAL)

    def test_short_project(self):
        self.assertEqual(analyze.short_project("/u/Projects/site/.claude/worktrees/ui-designs-b88b70"), "site ⎇ ui-designs")


if __name__ == "__main__":
    unittest.main()
