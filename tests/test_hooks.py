"""idle-resume: hold once after an expired cache on a big context, then let the resend through."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cc_retro import hooks

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc).timestamp()


def transcript(dirpath, ctx_read, w1=100, w5=0, model="claude-opus-5-5"):
    usage = {"input_tokens": 1, "cache_read_input_tokens": ctx_read, "output_tokens": 50,
             "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": w5}}
    rec = {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z",
           "message": {"id": "m", "model": model, "usage": usage, "content": []}}
    p = Path(dirpath) / "s.jsonl"
    p.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(rec) + "\n")
    return str(p)


class IdleResume(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ps = [mock.patch.object(hooks, "STATE", Path(self.dir) / "holds.json"),
                   mock.patch.object(hooks, "LOG", Path(self.dir) / "holds.log.jsonl"),
                   mock.patch.object(hooks, "_copy", return_value=False)]  # never touch the real clipboard
        self.copy = [p.start() for p in self.ps][-1]

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def ev(self, path, sid="s1"):
        return {"session_id": sid, "transcript_path": path, "prompt": "carry on with the import page"}

    def test_holds_once_then_lets_resend_through(self):
        path = transcript(self.dir, 400_000)
        out = hooks.idle_resume(self.ev(path), now=T0 + 3 * 3600)
        self.assertEqual(out["decision"], "block")
        self.assertIn("400k-token", out["reason"])
        self.assertIn("carry on with the import page", out["reason"])
        self.assertIn("$3.12", out["reason"])  # 400,101 tokens x ($8 write - $0.20 read) / M
        self.assertNotIn("clipboard", out["reason"])  # _copy failed: don't claim it
        self.assertIn("carry on with the import page", self.copy.call_args[0][0])
        self.assertIsNone(hooks.idle_resume(self.ev(path), now=T0 + 3 * 3600 + 60))  # resend goes through
        self.assertEqual(hooks.idle_resume(self.ev(path), now=T0 + 6 * 3600)["decision"], "block")  # next long break

    def test_small_or_recent_or_5m_cases(self):
        self.assertIsNone(hooks.idle_resume(self.ev(transcript(self.dir, 50_000)), now=T0 + 3 * 3600))  # small
        self.assertIsNone(hooks.idle_resume(self.ev(transcript(self.dir, 400_000)), now=T0 + 1800))  # within 1h TTL
        five = transcript(self.dir, 400_000, w1=0, w5=100)  # 5-min cache: 10 min idle already expired
        self.assertEqual(hooks.idle_resume(self.ev(five), now=T0 + 600)["decision"], "block")

    def test_never_blocks_on_bad_input(self):
        self.assertIsNone(hooks.idle_resume({"session_id": "x", "transcript_path": "/nope.jsonl"}))
        with mock.patch("sys.stdin") as stdin, mock.patch("builtins.print") as pr:
            stdin.read.return_value = "not json"
            hooks.main("idle-resume")
            pr.assert_not_called()

    def test_outcomes(self):
        big = transcript(self.dir, 400_000)
        hooks.idle_resume(self.ev(big, "left"), now=T0 + 3 * 3600)  # never resent
        hooks.idle_resume(self.ev(big, "cont"), now=T0 + 3 * 3600)
        hooks.idle_resume(self.ev(big, "cont"), now=T0 + 3 * 3600 + 30)  # resent, context intact
        # compacted: resend, and the next call after the release has a small context
        p = Path(self.dir) / "c.jsonl"
        p.write_text(Path(big).read_text())
        hooks.idle_resume(self.ev(str(p), "comp"), now=T0 + 3 * 3600)
        rel = T0 + 3 * 3600 + 60
        hooks.idle_resume(self.ev(str(p), "comp"), now=rel)
        after = {"type": "assistant", "timestamp": datetime.fromtimestamp(rel + 5, timezone.utc).isoformat(),
                 "message": {"id": "n", "model": "claude-opus-5-5", "content": [],
                             "usage": {"input_tokens": 1, "cache_read_input_tokens": 0, "output_tokens": 1,
                                       "cache_creation": {"ephemeral_1h_input_tokens": 40_000}}}}
        with p.open("a") as f:
            f.write(json.dumps(after) + "\n")
        got = {o["session"]: o for o in hooks.outcomes(hooks.LOG)}
        self.assertEqual(got["left"]["outcome"], "left")
        self.assertAlmostEqual(got["left"]["saved"], got["left"]["cost"])
        self.assertEqual(got["cont"]["outcome"], "continued")
        self.assertEqual(got["comp"]["outcome"], "compacted")
        self.assertGreater(got["comp"]["saved"], 0.8 * got["comp"]["cost"])


if __name__ == "__main__":
    unittest.main()
