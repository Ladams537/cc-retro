# Roadmap

## Done: session splitting (`cc-retro split`)

`cc-retro split` replays each session as if it had been split at idle gaps and/or at prompts labelled as independent new tasks, and prices the difference. It charges each new session's opening cache write and a configurable handoff. On the author's history, splitting at idle gaps saves 35–42% depending on handoff size, while splitting at new tasks saves 6%. Still to do:
- Finer boundary signals: a new request right after a commit or a passing test run, or a change of working directory or file set.
- Measuring what a fresh session really has to re-read, instead of assuming a fixed handoff.

## Done: workflow linking (`cc-retro workflows`)

Sessions are grouped into workflows by repo and time. Each gets a coordination mode (solo, delegated, human-parallel, mixed, automated) and is measured on cost, active time, prompts per hour, concurrency, commits and rework signals. A `--json` export lets a team pool results. Not done yet:
- Linking across repos.
- Explicit spawn links between sessions (sessions started by another session).
- Rework measured as lines later reverted, rather than only shared files, conflicts and undo commands.

## Coming next: who coordinates

With workflows linked, compare three ways of coordinating parallel sessions:

1. a human dispatching and merging the sessions by hand,
2. a "PM" Claude instance, prompted ad hoc, that splits the goal, starts and monitors other sessions, and integrates their results,
3. a Claude instance trained or prompted specifically for PM work (a dedicated coordination prompt, skill or fine-tune).

Each is measured on the same four outcomes: **cost**, **wall time**, **rework** (edits later reverted or redone, duplicate work across sessions, merge conflicts) and **corrections** (human corrections and redirects, from `--classify`). Observational data from transcripts gives a first answer; controlled runs on fixed tasks, like `eval/rule_pilot`, give a fair one.

## Phase 2: automatic rule re-tests after model changes

Behaviour rules in CLAUDE.md are written for a particular model. A rule that works hides its own evidence, so transcripts can't tell whether a newer model still needs it. Today `cc-retro upkeep` queues these rules for a re-test when a new main model appears, and `eval/rule_pilot` runs the on/off comparison by hand for one rule.

Phase 2 automates this: when a model change is detected, generate a small fixture task for each queued rule, run it with the rule on and off on the new model, and propose keeping, rewording or archiving the rule based on the result, with its cost and confidence. Runs stay opt-in and budget-capped, since they spend real money.

## Opt-in, redacted aggregate statistics

Individually, each user sees only their own friction. Pooled, the same counts would show which patterns are common across users, which rules actually reduce them, and how costs shift between model versions.

The plan is a strictly opt-in export of aggregate numbers only: friction cluster signatures (normalised, no paths or snippets), counts, cost buckets, model names and rule outcomes. Nothing from prompts, file contents or paths. You see the exact payload before anything is sent, and nothing is sent by default. The format will be published so the redaction can be checked.
