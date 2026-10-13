"""Cost breakdown on a hand-built transcript where every bucket's value is known."""

import json
import tempfile
import unittest
from pathlib import Path

from cc_retro import costs, pricing

M = "claude-opus-5-5"


def call(mid, ts, inp=0, read=0, w1=0, w5=0, out=0, model=M, tools=()):
    usage = {"input_tokens": inp, "cache_read_input_tokens": read, "output_tokens": out,
             "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": w5}}
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    return {"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": model, "usage": usage, "content": content}}


class Costs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "p").mkdir()
        lines = [
            {"type": "attachment", "attachment": {"type": "skill_listing",
             "content": "- vercel:deploy: " + "x" * 383 + "\n- mine: " + "y" * 192}},
            {"type": "attachment", "attachment": {"type": "deferred_tools_delta",
             "addedLines": ["mcp__plugin_vercel_vercel__list_projects", "mcp__paper__get_guide"]}},
            {"type": "attachment", "attachment": {"type": "instructions", "files": [{"content": "z" * 800}]}},
            call("a", "2026-10-01T10:00:00Z", w1=10_000, out=100),  # startup: 10k written once
            call("a", "2026-10-01T10:00:00Z", w1=10_000, out=100),  # same response split over two lines
            call("b", "2026-10-01T10:01:00Z", read=10_000, w1=2_000, out=200,
                 tools=[("Skill", {"skill": "vercel:deploy"}), ("Bash", {})]),  # normal growth
            call("c", "2026-10-01T12:30:00Z", read=0, w1=12_500, out=50),  # 2.5h idle: rebuild
            {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-01T12:40:00Z"},
            call("d", "2026-10-01T12:41:00Z", w1=4_000, out=50),  # after compaction: rebuild
            call("e", "2026-10-01T12:42:00Z", read=4_000, w1=200, out=10, model="claude-sonnet-5-5"),  # small write
        ]
        (self.root / "p" / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        self.ctx = costs.load_contexts(self.root)
        self.b = costs.breakdown(self.ctx)

    def test_buckets_add_up_to_list_price(self):
        calls = self.ctx[0].calls
        self.assertEqual(len(calls), 5)  # split response counted once
        expected = 0.0
        for c in calls:
            expected += pricing.cost(c.model, {"input_tokens": c.inp, "cache_read_input_tokens": c.read,
                                               "output_tokens": c.out, "cache_creation": {
                                                   "ephemeral_1h_input_tokens": c.w1, "ephemeral_5m_input_tokens": c.w5}})
        self.assertAlmostEqual(self.b.total, expected, places=9)

    def test_rebuilds_and_causes(self):
        inp = pricing.rates(M)[0]
        self.assertAlmostEqual(self.b.rebuild_causes["idle over 1h (cache expired)"], 12_500 * 2 * inp / 1e6)
        self.assertAlmostEqual(self.b.rebuild_causes["compaction"], 4_000 * 2 * inp / 1e6)
        self.assertAlmostEqual(self.b.new_context, 2_000 * 2 * inp / 1e6 + 200 * 2 * pricing.rates("claude-sonnet-5-5")[0] / 1e6)

    def test_startup_vs_conversation(self):
        read = pricing.rates(M)[2]
        # call b reads 10k, all of it startup (base = 10k); call e reads 4k < base, also all startup
        startup = 10_000 * 2 * pricing.rates(M)[0] / 1e6 + 10_000 * read / 1e6 + 4_000 * pricing.rates("claude-sonnet-5-5")[2] / 1e6
        self.assertAlmostEqual(self.b.startup, startup)
        self.assertAlmostEqual(self.b.conversation, 0.0)

    def test_sources_and_usage(self):
        st = self.ctx[0].startup
        self.assertEqual(st["vercel (plugin)"], (len("- vercel:deploy: " + "x" * 383) + len("mcp__plugin_vercel_vercel__list_projects") + 1) // 4)
        self.assertEqual(st["CLAUDE.md files"], 200)
        self.assertIn("paper (MCP)", st)
        self.assertEqual(self.b.used["vercel (plugin)"], 1)
        self.assertEqual(self.b.used["built-in tools"], 1)
        self.assertIn("system prompt + tool schemas (not recorded)", dict(costs.by_source_with_residual(self.b)))

    def test_tool_source(self):
        self.assertEqual(costs.tool_source("mcp__plugin_vercel_vercel__x"), "vercel (plugin)")
        self.assertEqual(costs.tool_source("mcp__claude-in-chrome__navigate"), "claude-in-chrome (MCP)")
        self.assertEqual(costs.tool_source("Bash"), "built-in tools")


if __name__ == "__main__":
    unittest.main()
