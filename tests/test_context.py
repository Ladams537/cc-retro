"""Context composition: carried cost is assigned to live items, scaled to measured usage."""

import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path

from cc_retro import context, pricing

M = "claude-opus-5-5"


def call(mid, read, w1=0, tools=()):
    usage = {"input_tokens": 0, "cache_read_input_tokens": read, "output_tokens": 1,
             "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": 0}}
    content = [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in tools]
    return {"type": "assistant", "message": {"id": mid, "model": M, "usage": usage, "content": content}}


def result(tid, text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


class Context(unittest.TestCase):
    def write(self, recs):
        d = Path(tempfile.mkdtemp()) / "p"
        d.mkdir()
        f = d / "s.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        return f

    def test_cost_follows_live_items_and_compaction(self):
        read = pricing.rates(M)[2] / 1e6
        f = self.write([
            {"type": "user", "message": {"content": "go"}},
            call("a", 10_000, tools=[("t1", "Read", {"file_path": "/x/big.py"})]),  # startup = 10k
            result("t1", "r" * 4000),  # ~1000 tok
            {"type": "attachment", "attachment": {"type": "prompt_snapshot", "x": "s" * 400_000}},  # not sent
            call("b", 11_000),  # carries the Read result (+ the tiny tool input), 1000 conv tokens
            call("c", 11_000),
            {"type": "system", "subtype": "compact_boundary"},
            {"type": "user", "message": {"content": "Base directory for this skill: /s/skills/claude-api\n" + "k" * 8000}},
            call("d", 12_000),  # after compaction only the skill is live
        ])
        comp = context.analyse([f])
        self.assertNotIn("mid-session attachments", comp.by_source)  # prompt_snapshot excluded
        conv_spend = (1000 + 1000 + 2000) * read  # calls b, c, d beyond the 10k startup
        self.assertAlmostEqual(comp.total, conv_spend, places=9)
        self.assertGreater(comp.by_source["Read"], 0.9 * 2000 * read)  # most of calls b and c
        self.assertAlmostEqual(comp.by_source["skill content"], 2000 * read, places=9)  # all of call d
        self.assertIn("skill: claude-api", [label for _, _, label, *_ in comp.top])

    def test_png_image_tokens(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1000, 600) + b"\x08\x02\x00\x00\x00"
        self.assertEqual(context.image_tokens(base64.b64encode(png).decode()), 800)  # 1000*600/750
        big = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 4000, 3000) + b"\x08\x02\x00\x00\x00"
        self.assertEqual(context.image_tokens(base64.b64encode(big).decode()), context.IMAGE_CAP)
        self.assertEqual(context.image_tokens("not-an-image"), context.IMAGE_DEFAULT)


if __name__ == "__main__":
    unittest.main()
