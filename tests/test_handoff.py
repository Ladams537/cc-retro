"""Handoff note from a transcript: no model call, the right sections, recency order, noise removed."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cc_retro import handoff, hooks

T_LAST = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc).timestamp()  # the appended usage record's time


def tx(recs):
    d = Path(tempfile.mkdtemp()) / "proj"
    d.mkdir()
    f = d / "abcd1234-0000.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return f


def asst(*blocks, ts="2026-10-01T10:00:00Z"):
    return {"type": "assistant", "timestamp": ts, "cwd": "/w/app", "gitBranch": "feat/x", "message": {"content": list(blocks)}}


def user(content, ts="2026-10-01T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "cwd": "/w/app", "message": {"content": content}}


class Handoff(unittest.TestCase):
    def setUp(self):
        self.f = tx([
            {"type": "custom-title", "customTitle": "Import page"},
            user('<pasted_content id="1">' + "x" * 500 + '</pasted_content id="1"> Build the CSV import page'),
            asst({"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/w/app/a.ts"}},
                 {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "npm test"}}),
            user([{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "FAIL a.test.ts\nShell cwd was reset to /w"}]),
            asst({"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "/w/app/b.ts"}},
                 {"type": "tool_use", "id": "t4", "name": "Edit", "input": {"file_path": "/w/app/a.ts"}},
                 {"type": "tool_use", "id": "t5", "name": "TodoWrite", "input": {"todos": [
                     {"content": "wire upload", "status": "completed"}, {"content": "error states", "status": "pending"}]}}),
            user("now add validation"),
            asst({"type": "text", "text": "Validation added; tests pass. Next: error states."}, ts="2026-10-01T11:00:00Z"),
        ])

    def test_sections(self):
        with mock.patch.object(handoff, "_commits", return_value=["abc123 Add import page"]):
            note = handoff.build(self.f)
        self.assertIn("# Handoff: Import page", note)
        self.assertIn("on branch `feat/x`", note)
        self.assertIn("[pasted text, ", note)
        self.assertNotIn("xxxx", note)
        self.assertIn("Build the CSV import page", note)
        self.assertIn("- now add validation", note)
        self.assertIn("[pending] error states", note)
        self.assertNotIn("wire upload", note)
        self.assertIn("Next: error states.", note)
        self.assertIn("abc123 Add import page", note)
        self.assertLess(note.index("`a.ts` (2 edits)"), note.index("`b.ts`"))  # a.ts edited last
        self.assertIn("`npm test` → failed: FAIL a.test.ts", note)
        self.assertNotIn("Shell cwd", note)

    def test_hook_points_at_the_note(self):
        d = Path(tempfile.mkdtemp())
        rec = {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {"id": "m", "model": "claude-opus-5-5",
               "usage": {"input_tokens": 1, "cache_read_input_tokens": 400_000, "output_tokens": 1,
                         "cache_creation": {"ephemeral_1h_input_tokens": 10}}, "content": []}}
        self.f.write_text(self.f.read_text() + json.dumps(rec) + "\n")
        with mock.patch.object(hooks, "STATE", d / "h.json"), mock.patch.object(hooks, "LOG", d / "h.log"), \
                mock.patch.object(handoff, "OUT_DIR", d / "handoffs"), mock.patch.object(handoff, "_commits", return_value=[]), \
                mock.patch.object(hooks, "_copy", return_value=True) as copy:
            out = hooks.idle_resume({"session_id": "s", "transcript_path": str(self.f), "prompt": "hi"}, now=T_LAST + 3 * 3600)
        read = f"Read {d / 'handoffs' / (self.f.stem + '.md')} and continue from it."
        self.assertIn(read, out["reason"])
        copy.assert_called_once_with(f"{read}\n\nhi")  # one paste: the Read line, then the held prompt
        self.assertIn("Copied to your clipboard: the Read line and your prompt", out["reason"])
        self.assertTrue((d / "handoffs" / (self.f.stem + ".md")).exists())


if __name__ == "__main__":
    unittest.main()
