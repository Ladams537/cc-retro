"""Upkeep on a synthetic tree: each kind of staleness is planted once and must be found exactly once.

Annotation is stubbed (no `claude -p`), so these test the checks, not Haiku's labelling.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cc_retro import analyze, upkeep

NOW = datetime.now(timezone.utc)


def rec(sid, cwd, days_ago=1, model=None, tool_result=None, edit=None, text=""):
    ts = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    d = {"sessionId": sid, "cwd": cwd, "timestamp": ts}
    if model:
        d |= {"type": "assistant", "message": {"model": model, "content": []}}
    if tool_result:
        d |= {"type": "user", "message": {"content": [{"type": "tool_result", "content": tool_result}]}}
    if edit:
        d |= {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": edit, "new_string": text}}]}}
    return json.dumps(d)


class Upkeep(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "Projects"
        self.app = self.proj / "app"
        self.other = self.proj / "other"
        (self.app / "lib").mkdir(parents=True)
        self.other.mkdir()
        (self.app / "lib" / "db.ts").write_text("")
        (self.app / "package.json").write_text(json.dumps({"dependencies": {"@tanstack/react-query": "^9.1.0"}}))
        (self.app / "CLAUDE.md").write_text(
            "# app\n\n"
            "### Query cache quirk\n* **Fix:** see `lib/db.ts`; react-query v9 dedupes.\n\n"
            "### Alembic downgrade breaks\n* **Fix:** run `alembic stamp head`.\n\n"
            "### Old helper\n* **Fix:** use `lib/gone.ts`.\n\n"
            "### Port 5433 clash\n* **Fix:** `docker compose restart db`.\n\n"
            "### Wait with Monitor\n* Never `sleep`; use `run_in_background`.\n")
        self.notes = {  # what Haiku would return, per heading
            "Query cache quirk": dict(kind="fact", anchors=["react-query"], symptoms=[], paths=["lib/db.ts"], deps=["react-query"]),
            "Alembic downgrade breaks": dict(kind="fact", anchors=["alembic"], symptoms=[], paths=[], deps=["alembic"]),
            "Old helper": dict(kind="fact", anchors=["gone.ts"], symptoms=[], paths=["lib/gone.ts"], deps=[]),
            "Port 5433 clash": dict(kind="fact", anchors=["docker compose"], symptoms=["Connection refused"], paths=[], deps=[]),
            "Wait with Monitor": dict(kind="behaviour", anchors=["run_in_background"], symptoms=[], paths=[], deps=[]),
        }
        self.tx = self.tmp / "transcripts" / "p"
        self.tx.mkdir(parents=True)
        self.state = self.tmp / "state"
        self.patches = [
            mock.patch.object(upkeep, "REGISTRY", self.state / "entries.json"),
            mock.patch.object(upkeep, "PROPOSALS", self.state / "upkeep"),
            mock.patch.object(upkeep, "ARCHIVE", self.state / "archive.md"),
            mock.patch.object(upkeep, "find_claude_mds", lambda: [self.app / "CLAUDE.md"]),
            mock.patch.object(upkeep, "annotate", self._annotate),
            mock.patch.object(upkeep, "_TMP", ("/nonexistent",)),
            mock.patch.object(analyze, "_TRANSIENT", ("/nonexistent",)),  # mkdtemp lives under /var/folders, normally ignored
        ]
        for p in self.patches:
            p.start()
        upkeep._INDEX.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _annotate(self, entries, registry, model="haiku"):
        for e in entries:
            for k, v in self.notes[e.heading].items():
                setattr(e, k, v)

    def write(self, name, *lines):
        (self.tx / f"{name}.jsonl").write_text("\n".join(lines) + "\n")

    def run_upkeep(self):
        _, actions, notes = upkeep.run(self.tmp / "transcripts", stale_days=90)
        return {(a.op, a.heading): a for a in actions}, notes

    def test_first_run_then_model_change(self):
        app = str(self.app)
        self.write("s1", rec("s1", app, 3, model="claude-old"), rec("s1", app, 3, tool_result="react-query run_in_background docker compose"))
        self.write("s2", rec("s2", app, 2, tool_result="react-query run_in_background docker compose"))
        acts, notes = self.run_upkeep()
        self.assertIn(("archive", "Alembic downgrade breaks"), acts)  # no manifest uses alembic
        self.assertIn(("archive", "Old helper"), acts)  # its only path is gone
        self.assertNotIn(("archive", "Query cache quirk"), acts)  # repo-relative path and dep both resolve
        self.assertFalse([k for k in acts if k[0] == "retest"])  # first run only sets the baseline
        self.assertTrue(any("First run" in n for n in notes))

        # a new main model shows up, and the dependency moves a major version
        self.write("s3", rec("s3", app, 0, model="claude-new"), rec("s3", app, 0, model="claude-new"))
        (self.app / "package.json").write_text(json.dumps({"dependencies": {"@tanstack/react-query": "^10.0.0"}}))
        acts, notes = self.run_upkeep()
        self.assertIn(("retest", "Wait with Monitor"), acts)
        self.assertNotIn(("retest", "Query cache quirk"), acts)  # facts don't depend on the model
        self.assertIn("^9.1.0 → ^10.0.0", acts[("flag", "Query cache quirk")].reason)
        self.assertTrue(any("claude-old → claude-new" in n for n in notes))

    def test_moves_up_only_on_real_recurrence(self):
        app, other = str(self.app), str(self.other)
        # the symptom elsewhere, but with no anchor in that session: any server being down. Not a recurrence.
        self.write("a", rec("a", other, tool_result="curl: (7) Connection refused"))
        self.write("b", rec("b", other, tool_result="curl: (7) Connection refused"))
        acts, _ = self.run_upkeep()
        self.assertNotIn(("move", "Port 5433 clash"), acts)

        # now with docker compose in those sessions: the problem really recurred in a sibling project
        self.write("a", rec("a", other, tool_result="docker compose up: Connection refused"))
        self.write("b", rec("b", other, tool_result="docker compose ps; Connection refused"))
        acts, _ = self.run_upkeep()
        self.assertEqual(acts[("move", "Port 5433 clash")].target, str(self.proj / "CLAUDE.md"))

    def test_maintenance_sessions_are_not_evidence(self):
        other = str(self.other)
        for sid in ("m1", "m2"):  # sessions tidying CLAUDE.md files across two projects
            self.write(sid, rec(sid, other, edit=f"{self.app}/CLAUDE.md"), rec(sid, other, edit=f"{other}/CLAUDE.md"),
                       rec(sid, other, tool_result="docker compose: Connection refused"))
        acts, _ = self.run_upkeep()
        self.assertNotIn(("move", "Port 5433 clash"), acts)

    def test_apply_refuses_after_drift_and_applies_otherwise(self):
        self.write("s", rec("s", str(self.app), model="m"))
        self.run_upkeep()
        prop = upkeep.latest_proposal()
        entries = upkeep.split_entries(self.app / "CLAUDE.md")
        md = self.app / "CLAUDE.md"
        original = md.read_text()
        md.write_text(original + "\n### Added by hand\nx\n")
        with self.assertRaises(SystemExit):
            upkeep.apply(prop, upkeep.split_entries(md))
        md.write_text(original)
        upkeep.apply(prop, entries)
        text = md.read_text()
        self.assertNotIn("Alembic downgrade breaks", text)
        self.assertIn("Query cache quirk", text)
        self.assertIn("Alembic downgrade breaks", (self.state / "archive.md").read_text())

    def test_hook_reminds_until_upkeep_runs(self):
        app = str(self.app)
        self.write("s1", rec("s1", app, model="claude-old"))
        self.assertIsNone(upkeep.hook(self.tmp / "transcripts"))  # no baseline yet
        self.run_upkeep()
        self.write("s2", rec("s2", app, model="claude-new"))
        self.assertIn("claude-new", upkeep.hook(self.tmp / "transcripts"))
        self.assertIn("claude-new", upkeep.hook(self.tmp / "transcripts"))  # still pending, nothing rescanned
        self.run_upkeep()
        self.assertIsNone(upkeep.hook(self.tmp / "transcripts"))

    def test_symptoms_must_be_error_strings_not_paths(self):
        self.assertFalse(upkeep._is_symptom("/var/folders"))
        self.assertFalse(upkeep._is_symptom("~/.docker/config.json"))
        self.assertFalse(upkeep._is_symptom("src/lib/x.ts"))
        self.assertTrue(upkeep._is_symptom("./bin/sh: Cannot open: Permission denied"))
        self.assertTrue(upkeep._is_symptom("docker-credential-desktop"))
        self.assertTrue(upkeep._is_symptom("No space left on device writing /tmp/x"))

    def _age_entries(self, days):
        reg = json.loads(upkeep.REGISTRY.read_text())
        old = (NOW - timedelta(days=days)).date().isoformat()
        for m in reg["entries"].values():
            m["first_seen"] = old
        upkeep.REGISTRY.write_text(json.dumps(reg))

    def test_unused_needs_age(self):
        self.write("s", rec("s", str(self.app), model="m"))
        acts, _ = self.run_upkeep()  # all entries brand new: none can be "unused" yet
        self.assertFalse([k for k in acts if k[0] == "flag" and "haven't come up" in acts[k].reason])
        self._age_entries(120)
        acts, _ = self.run_upkeep()
        self.assertIn("haven't come up", acts[("flag", "Wait with Monitor")].reason)

    def test_meta_lines_count_only_inside_their_subtree(self):
        app, other = str(self.app), str(self.other)
        # work on the app's own lessons, inside the app: keeps them alive
        self.write("a", rec("a", app, tool_result="CLAUDE.md says run_in_background"))
        self.write("b", rec("b", app, tool_result="cc-retro: run_in_background rule"))
        # the same meta talk from another project says nothing about the app's entry
        self.write("c", rec("c", other, tool_result="CLAUDE.md: gone.ts"), rec("c", other, tool_result="cc-retro gone.ts"))
        self.write("d", rec("d", other, tool_result="CLAUDE.md gone.ts"))
        self.run_upkeep()
        self._age_entries(120)
        acts, _ = self.run_upkeep()
        self.assertNotIn(("flag", "Wait with Monitor"), acts)
        self.assertIn(("archive", "Old helper"), acts)  # still caught by the path check
        reg = json.loads(upkeep.REGISTRY.read_text())
        self.assertIsNone(reg["entries"][f"{self.app}/CLAUDE.md#Old helper"].get("last_used"))

    def test_recurrence_after_the_lesson_was_written(self):
        app, proj, md = str(self.app), str(self.proj), f"{self.app}/CLAUDE.md"
        hit = "docker compose up: Connection refused"
        self.write("before", rec("before", app, 20, tool_result=hit))  # the original incident: not a repeat
        self.write("learn", rec("learn", app, 10, tool_result=hit),
                   rec("learn", app, 10, edit=md, text="### Port 5433 clash\n..."),
                   rec("learn", app, 9, tool_result=hit))  # re-checking in the session that wrote it: not a repeat
        acts, _ = self.run_upkeep()
        self.assertNotIn(("recurred", "Port 5433 clash"), acts)

        self.write("again", rec("again", app, 3, tool_result=hit))  # loaded at startup, still happened
        self.write("root", rec("root", proj, 2, tool_result=hit))  # started above app/: file may not have loaded
        acts, _ = self.run_upkeep()
        reason = acts[("recurred", "Port 5433 clash")].reason
        self.assertIn("2 sessions", reason)
        self.assertIn("1 with it loaded", reason)
        self.assertIn("1 started above", reason)


class SearchRoots(unittest.TestCase):
    def test_default_and_override(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("os.getcwd", return_value=d), \
                mock.patch.object(upkeep.subprocess, "run", return_value=mock.Mock(stdout="")):  # not in a git repo
            expected = (Path.home() / ".claude", Path(d))
            self.assertEqual(upkeep.parse_roots(None), expected)
            self.assertEqual(upkeep.parse_roots(""), expected)
        self.assertEqual(upkeep.parse_roots("~/a:/b"), (Path.home() / "a", Path("/b")))

    def test_find_uses_current_roots(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x").mkdir()
            (Path(d) / "x" / "CLAUDE.md").write_text("# x\n")
            with mock.patch.object(upkeep, "SEARCH_ROOTS", (Path(d),)):
                self.assertEqual(upkeep.find_claude_mds(), [Path(d) / "x" / "CLAUDE.md"])


if __name__ == "__main__":
    unittest.main()
