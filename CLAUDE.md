# cc-retro: lessons learned

### `claude -p` classifier obeyed a prompt it was meant to label
* **Context:** Batch-labelling user prompts with `claude -p` returned an essay instead of JSON. One imperative item (a request to explain some code) was carried out, not classified.
* **Root Cause:** The instructions and the items shared one user message as plain text, so an imperative item read as a fresh request.
* **Fix:** Put the instructions in `--system-prompt`, send the items as a JSON array, and constrain the reply with `--json-schema` plus `--output-format json`; read the answer from `structured_output`. Reject a batch whose label count doesn't match the number of items. Bump `PROMPT_VERSION` whenever the instructions change, so stale cached labels are dropped.

### Placement can't go below the project root if cwd is always a path
* **Context:** The LCA placement test expected `lib/import/CLAUDE.md` but got the project root.
* **Root Cause:** Every friction event included the session cwd, which is an ancestor of every file it touched, so it always won the LCA.
* **Fix:** `touched()` falls back to cwd only when the call names no specific files.

### Don't tune detectors on the labels you score them with
Example phrases in the classifier prompt must not come from `eval/gold.local.json`, and a regex written after reading the labelled data is in-sample. Report it as such.

### A usage limit mid-pilot produces plausible-looking rows
* **Context:** The first rule pilot recorded 40 rows. 34 were cut off by "You've hit your session limit", including slow runs that graded as successes, because the grader only checks the fixture's final state.
* **Root Cause:** `claude -p` returns a normal JSON result (`is_error: true`, low cost) when the plan limit hits, and the runner treated it like any finished run.
* **Fix:** `run.py` halts on limit markers, moves the transcript to `results/transcripts/limit-hit/` and skips writing a row. Re-running resumes from clean rows. Always check `is_error` before scoring.

### Term mentions aren't evidence that a lesson is needed
* **Context:** The first `upkeep` run proposed moving 12 of 21 freshly placed entries up a level, citing hits for terms like `useEffect`, `vercel.json` and `cargo test` in other projects.
* **Root Cause:** Generic terms appear in every project that uses the same stack, and sessions that *maintain* CLAUDE.md files mention every entry's terms. Haiku also returned "Connection refused" as a "specific" symptom.
* **Fix:** Only move an entry up on a symptom (exact error string) found in tool output, in a session that also mentions one of the entry's terms. Exclude sessions that edit CLAUDE.md files in 2+ directories from move evidence (they can still keep an entry alive), and reject path-like symptoms (`_is_symptom`). Lines about CLAUDE.md or cc-retro count as usage only inside their own subtree; skipping them everywhere made cc-retro's own lessons permanently "unused". Don't trust the model to make symptoms specific; the co-occurrence filter does that deterministically.

### Tests under `tempfile` look like scratch paths
* **Context:** An upkeep test expected a move to `<tmp>/Projects/CLAUDE.md` and got `~/.claude/CLAUDE.md`, and the "no move" assertions passed for the wrong reason.
* **Root Cause:** macOS `mkdtemp` lives under `/var/folders`, which `analyze._TRANSIENT` and `upkeep._TMP` drop as scratch space.
* **Fix:** Patch both to `("/nonexistent",)` in tests that need real-looking paths.

### Hooks run the installed copy, not the checkout
* **Context:** Claude Code hooks (and any scheduled `cc-retro upkeep`) call `~/.local/bin/cc-retro`, installed with `uv tool install` (non-editable, so moving the checkout doesn't break them).
* **Fix:** After changing `src/` or `pyproject.toml`, run `uv tool install --force .` from the repo root, or the hooks keep running the old code.
* **Root Cause:** A non-editable uv tool install copies the package.
