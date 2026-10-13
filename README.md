# cc-retro

A retrospective for your Claude Code sessions. It reads the transcripts Claude Code already keeps in `~/.claude/projects` and tells you:

- **Where sessions got stuck:** failed tool calls clustered by *cause* (not exit code), retry streaks, permission denials, your interrupts and corrections, timeouts, and calls that were slow or waiting on approval.
- **What it cost:** list-price spend per session, project and day, cache hit rate, and roughly what each friction pattern cost in recovery turns. Estimates land within about 4% of the `cost-state` records in newer transcripts.
- **What to put in CLAUDE.md, and *which* CLAUDE.md:** each recurring pattern comes with a ready-made rule, placed in the CLAUDE.md of the **lowest directory that covers every place it happened**. That is the lowest common ancestor of the files involved, with worktrees folded into their repo. A lesson from one module stays in that module; one seen across two projects goes to their shared parent; only a pattern that spans your whole home directory reaches `~/.claude/CLAUDE.md`. Think of Jethro's courts in Exodus 18: only cases the lower courts can't handle go up to Moses. `--suggest` drafts rules for patterns the built-in library doesn't cover.

Everything runs locally with no dependencies (Python ≥ 3.10 stdlib), and the report is a single offline HTML file.

## Install

```sh
uv tool install git+https://github.com/Ladams537/cc-retro   # puts `cc-retro` in ~/.local/bin
uvx --from git+https://github.com/Ladams537/cc-retro cc-retro --open   # or run without installing
```

From a clone, `uv tool install .` (or `pip install .`). It reads Claude Code transcripts from `~/.claude/projects` (`--root` for another location). The `claude` CLI is only needed for the optional model-assisted features below.

## Quickstart

```sh
cc-retro --open                                   # HTML report for everything
cc-retro summary --since 7d                       # terminal summary
cc-retro cost                                     # where the money went
cc-retro report --classify                        # also find your corrections (Haiku via `claude -p`, cached)
cc-retro report --project myapp --suggest         # one project, with drafted rules (uses `claude -p`)
cc-retro report --redact -o share.html            # no snippets or titles, safe to share
cc-retro json | jq '.clusters[:5]'                # raw data
cc-retro upkeep                                   # propose CLAUDE.md maintenance (uses `claude -p`); edits nothing
```

## Privacy

Everything runs on your machine. cc-retro reads your transcripts and CLAUDE.md files, writes its caches and proposals under `~/.claude/cc-retro` and `~/.cache/cc-retro`, and makes no network requests. The HTML report loads nothing external.

The only data that leaves your machine goes through your own `claude -p` (Claude Code's headless mode, under your account), and only for these opt-in features:

| Feature | What is sent |
|---|---|
| `report --classify` | your prompts (first 600 characters), each with the end of the assistant message before it |
| `report --suggest` | the top friction clusters, with up to three 500-character snippets each (tool errors, or your prompts for corrections) |
| `upkeep` (not `--hook`) | the text of each new or changed CLAUDE.md section, once (results are cached) |

Without the `claude` CLI on `PATH` these features are skipped. The default report, `summary`, `json`, `cost` and both hooks never call it. Reports include snippets of your prompts and tool output; use `--redact` before sharing one.

## How friction is detected

| Kind | Signal in the transcript |
|---|---|
| Tool error | `tool_result.is_error`. Bash failures are classified by cause: harness-blocked sleep, connection refused, probe chain whose last lookup was empty, path not found, module not found, traceback, and so on. |
| Retry loop | The *same* call (same tool, same command or same error) failing 3+ times in a row |
| Permission denied | `toolDenialKind`, or a "user doesn't want to proceed" result |
| User interrupt | `[Request interrupted by user…]` |
| User retry | A short "try again / rerun / continue" prompt, tagged by whether an API error, usage limit or interrupt came just before |
| User correction | Only with `--classify`: Haiku labels each human prompt, given the assistant turn before it, as correction / redirect / other. The input is JSON data with a schema-constrained reply, so a prompt like "explain this code" is labelled, not obeyed. |
| Timeout / slow | `toolUseResult.timedOutAfterMs`, or more than 2 min between call and result. Transcripts don't separate execution time from permission-prompt wait. |
| API error / compaction | `system` records with subtype `api_error` / `compact_boundary` |

Clusters are ranked by `weight × count × (1 + sessions affected)`, because a problem that recurs across sessions is exactly what a CLAUDE.md rule fixes.

## How good are the detectors?

`eval/eval_prompts.py` scores the prompt-level detectors against hand labels kept in `eval/gold.local.json` (gitignored, since they come from your own prompts). On the author's 130 labelled prompts:

| Detector | Precision | Recall | Caveat |
|---|---|---|---|
| Retry (regex) | 4/4 | 4/4 | In-sample: the regex was written after reading these prompts. |
| Correction, old regex (removed) | 0/3 | 0/6 | This is why it was removed. |
| Correction, `--classify` | 3/8 – 4/5 | 3/6 – 4/6 | Only 6 positives, so runs differ by ±1–2. Several "false positives" are critiques of something Claude built, which the definition doesn't settle either way. |

The Bash cause classifier was designed by reading the same failures it is tested on. Its real test is sessions recorded *after* 2026-10-12.

## Where the money goes: `cc-retro cost`

```sh
cc-retro cost                 # all projects
cc-retro cost --project myapp
```

Splits list-price spend by what the tokens were, per context window (each transcript file):

| Bucket | What it is |
|---|---|
| Output | tokens the model wrote |
| Startup context | re-reading what was loaded before your first prompt (system prompt, tool schemas, skill and tool listings, CLAUDE.md, MCP instructions) on every turn, plus caching it once |
| Conversation context | re-reading everything accumulated since |
| New context | cache writes as the conversation grows |
| Cache rebuilds | a later call re-caching most of its context because the cache was gone: idle past the TTL, compaction, or a model switch. Paid twice for the same tokens |

The startup context is broken down by source from the transcript's attachment records, at about 4 characters per token. Skills and deferred tools are attributed to the plugin or MCP server that brings them, next to how often each was used. The system prompt and built-in tool schemas aren't recorded in transcripts, so they show as the remainder. The totals match the main report's estimate.

`cc-retro cost --context` breaks the conversation context down by what put it there: each tool's results, your prompts, skill bodies loaded mid-session, system reminders, and the model's own replies and tool inputs. Each item is charged the input price of every later call that re-sends it, until the session ends or compacts. Sizes are estimated (text at about 4 characters per token, images from their PNG header) and scaled per call to the measured token counts. Attachment records the app keeps for itself, like `prompt_snapshot`, are excluded: real usage shows they never reach the model. It also lists the most expensive single items.

On the author's history ($373), output was 15%, startup context 11% (plugins and MCP listings only about $7 of that), conversation context 37%, and **cache rebuilds 26%**. That was 30 returns to sessions with a median 406k-token context after a median 3.7 h break, about $2.93 each. 87% of spend was on calls with more than 150k tokens of context. The conversation context had no single heavy hitter: the model's own replies and tool inputs were 32%, Bash output 19%, system reminders 15% and browser screenshots 13%, and the costliest single item was about $4. Small items re-sent for hundreds of calls drive the cost, so it grows roughly with the square of session length.

## Should sessions have been split? `cc-retro split`

```sh
cc-retro split               # boundaries at idle gaps over 1h
cc-retro split --classify    # also at prompts Haiku labels "independent new task" (cached)
cc-retro split --handoff 30000
```

Context cost grows with the square of session length, so starting fresh at a task boundary drops the old conversation from every later call. For each strategy, `split` replays your sessions as if they'd been split there. After a split, each call's context is the startup context, plus a handoff (2k tokens by default), plus what came since. Its real input cost is scaled by that ratio, which keeps its real mix of cache reads and writes. Opening the new session pays one cache write, unless that call was already a full rebuild. Output is unchanged, and a compaction ends the comparison.

On the author's history ($382), splitting at idle gaps would have saved **42%**, still 35% with a 60k-token handoff. Splitting at new tasks alone saved 6%: the sessions were long, single projects with breaks, not task-hopping.

## Continue in a fresh session: `cc-retro handoff`

```sh
cc-retro handoff                 # the most recently active session
cc-retro handoff --session a1b2  # by id prefix
cc-retro handoff --project api --print
```

Writes `~/.claude/cc-retro/handoffs/<session>.md` from the transcript, **with no model call**. Asking a session for a summary after a break re-pays its whole context, which is the cost a fresh session avoids. The note has:
- the session title, directory and branch
- the goal (your first prompt, with pasted blocks collapsed)
- your recent requests and any open todos
- the last message (where it left off)
- commits made since the session started
- files changed, most recent first, and files read
- the last commands and how they ended

Start a new session and send `Read <path> and continue from it`. When the idle-resume hook holds a prompt, it writes this note and shows that line.

## Workflows and coordination: `cc-retro workflows`

```sh
cc-retro workflows            # grouped workflows and a comparison by coordination mode
cc-retro workflows --json     # export for pooling across a team: no prompts, paths or file names
```

A workflow is the sessions in one repo (worktrees fold into it) whose activity overlaps or follows within `--gap` hours (2 by default). Each workflow gets a coordination mode:

| Mode | Meaning |
|---|---|
| solo | one main session at a time, no delegation |
| delegated | a lead session handing work to subagents (Agent/Task) |
| human-parallel | you running several main sessions at once |
| mixed | several concurrent main sessions, with delegation as well |
| automated | a scheduled-task run; each is its own workflow, so routines don't count as your parallel work |

Measures for each workflow:
- **Cost,** subagents included.
- **Wall time** and **active time:** the union of active spans, so parallel sessions don't double-count your time.
- **Your prompts per active hour:** a proxy for how much attention it took.
- **Peak and average concurrency.**
- **Commits:** successful `git commit` calls in its own sessions, quiet ones included. Commits made outside these Claude sessions in the same window aren't credited.
- **Rework signals:** a file edited in more than one session, merge conflicts, and reverts or resets.

Each workflow carries a `person` (the repo's git `user.email`), so `--json` exports from several people can be combined to compare how a team coordinates.

## Hooks

Install cc-retro as a tool first (see [Install](#install)); hooks call `~/.local/bin/cc-retro`, not a checkout. Add to `~/.claude/settings.json`:

```json
"hooks": {
  "SessionStart":     [{"hooks": [{"type": "command", "command": "~/.local/bin/cc-retro upkeep --hook 2>/dev/null || true"}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "~/.local/bin/cc-retro hook idle-resume 2>/dev/null || true"}]}]
}
```

- **`upkeep --hook`** (SessionStart): shows one line when a new main model appears in your transcripts, until `cc-retro upkeep` runs.
- **`hook idle-resume`** (UserPromptSubmit): holds the first prompt sent to a session with over 150k tokens of context (`CC_RETRO_IDLE_MIN_TOKENS`) after its prompt cache has expired. You see the extra cost of re-caching it, your options (`/compact`, or a new session for a new task) and your prompt to copy back. Sending it again within 10 minutes goes through. This targets the largest cost bucket `cc-retro cost` found.

Both run in about 0.1 s, read only the transcript tail or new files, and let everything through on any error.

Friction clusters were also candidates for hooks: blocked sleeps, curling a server that isn't running, MCP servers that aren't connected. On the author's data each one cost under $1 in total, and the harness already blocks or teaches most of them, so they don't have hooks.

## Keeping CLAUDE.md files current: `cc-retro upkeep`

```sh
cc-retro upkeep          # writes ~/.claude/cc-retro/upkeep/<stamp>/proposal.md; edits nothing
cc-retro upkeep --apply  # carries out the latest proposal's archive/move items, if no file changed since
```

Every `##`/`###` leaf section in every CLAUDE.md under `~/.claude` and the git repo you run it from is an entry. To cover all your projects at once, pass `--roots ~/.claude:~/code` or set `CC_RETRO_ROOTS` (`:`-separated). `--no-llm` skips the Haiku labelling, but unlabelled entries then get only the path and dependency checks. Haiku labels each one once (cached by content hash in `~/.claude/cc-retro/entries.json`) as a **fact**, a **behaviour** rule or a **directive**, and lists its context terms, symptoms (exact error strings), paths and dependencies. Entries go stale in different ways, so they're checked differently:

| Check | Applies to | Evidence | Proposal |
|---|---|---|---|
| Code moved on | facts | every path it names is gone from the directory it governs, or no manifest there uses its dependency | archive to `~/.claude/knowledge-archive.md` |
| Dependency changed | facts | a dependency's version spec differs from the last run | review: re-verify the quirk |
| Model moved on | behaviour rules | your main model (most messages in the last 14 days) isn't the one the rule was written for, and the rule hasn't been tested on it | re-test with rule on vs off (`eval/rule_pilot`) |
| Lesson didn't work | all | one of its symptoms reappears in its subtree after the lesson was first written into any CLAUDE.md (from the transcripts' Edit/Write calls, else git, else first seen), excluding the session that wrote it. Split into sessions where the file loaded at startup (the lesson was ignored) and sessions started above its directory (it may never have loaded) | review: make it a hook, or move it up |
| Problem recurred elsewhere | all | one of its symptoms appears in tool output, alongside one of its terms, in 2+ sessions outside its subtree | move up to the lowest common ancestor |
| Unused | all | the entry is older than `--stale-days` (90) and none of its terms has come up in that time | review |
| Duplicate | all | two entries share most of their words | review: merge |

Behaviour rules are only re-tested, never judged from transcripts: a rule that works stops its own pattern from appearing, so absence proves nothing. Entries only move *up* on evidence. Moving down is left to placing new lessons, because shared terms like `useEffect` say nothing about where a lesson is needed. Sessions that edit CLAUDE.md files in two or more directories count as maintenance and never count as evidence for a move. Lines about CLAUDE.md files or cc-retro itself count as usage only inside their own subtree, so a project's own lessons stay visible. Bare paths (like `/var/folders`) are never accepted as symptoms.

`cc-retro upkeep --hook` is the SessionStart form. It scans only transcripts changed since the last check and prints one line when a new model appears, repeating until `upkeep` runs.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests   # stdlib only, no setup
```

CI runs the same command on Python 3.10–3.13. `eval/` holds the detector scoring script and the rule pilot (`eval/rule_pilot`), which runs real `claude -p` sessions and costs money. See [ROADMAP.md](ROADMAP.md) for what's next.

## License

MIT. See [LICENSE](LICENSE).
