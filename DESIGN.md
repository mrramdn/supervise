# Design

`supervise` is a Claude Code plugin with four parts: the `/supervise` skill (instructions), the `supervisectl` CLI
(state and rules), two agents (`supervise:implementer`, and the read-only `supervise:code-reviewer`), and two hooks.
The model does the work and calls the CLI at every phase change. The CLI owns the run state and refuses a change
that breaks a rule (exit 2, state untouched, with the rule and the next step in the message). The rules therefore
hold whether or not the model remembers them.

## How a run works

```
init -> start -> PLAN -> IMPLEMENT -> VERIFY -> REVIEW -> ACCEPT
                              ^          |         |
                              |          v         v
                              +------- FIX <-------+      (fix_round + 1 each time; escalated at the cap)
```

- `init` detects gates (Makefile, package.json, pyproject.toml) into `.supervise/config.toml` and adds `.supervise/`
  to `.git/info/exclude`. `start` copies the config into the run and runs the gates once as the baseline.
- **Fingerprint.** `git add -A` into a temporary copy of the index, then `git write-tree`. The result is the
  content of the working tree (untracked files included, ignored files and `.supervise/` excluded), independent of
  commits. `verify` and `review begin` record it; `accept` needs verify, review, and the current tree to match.
- **Gates.** A gate passes on exit 0. A gate that failed at baseline counts as "same as baseline" only with the same
  exit code and the same output once durations, timestamps, addresses, and ANSI codes are removed. Without a gate
  of kind `test`, output says NO TESTS.
- **Review evidence.** The SubagentStop hook appends each finished subagent to `runs/<id>/events.jsonl`, with the
  reviewer's final reply. `review record` accepts only a `supervise:code-reviewer` stop after `review begin` and
  parses the findings itself; `block_on_severity` (default `high`) decides what blocks. A finding with
  `"kind": "acceptance"` (an acceptance criterion not met or not shown) blocks at any severity.
- **Fix rounds.** `fix` after a failed verify or a blocking review uses a round. When all `max_fix_rounds` are used,
  the next `fix` escalates the run, and only `override` (typed by the user) continues it. The PreToolUse hook denies
  `override` to the model. `fix` after a clean review, to apply non-blocking suggestions, uses no round.
- **Commit.** Off by default. `commit -m` commits the latest accepted run only while the working tree still has the
  accepted fingerprint, with a Conventional Commits message and no AI attribution, through plain `git commit`.

## Decisions

- **Stdlib Python 3.11+, git, POSIX.** No dependencies for users. The code lives in `src/supervise/`; `bin/supervisectl`
  is a launcher that Claude Code puts on PATH for Bash calls. `hook` skips the CLI imports.
- **Findings come from the hook, not the orchestrator.** `review record` reads the reviewer's reply captured by the
  hook, so the orchestrator cannot drop or soften findings on the way. Only a self-review passes its own findings,
  with one exception: when a reviewer reply has no findings JSON, the orchestrator may transcribe it
  (`--findings`). The review is then marked transcribed and the reply is saved beside the diff, so the user can
  compare the two; refusing outright lost those findings in dogfooding.
- **`review record` waits up to 15 seconds for the reviewer's stop.** In dogfooding the Agent tool's hand-back
  reached the orchestrator a few seconds before the SubagentStop hook ran, so the first `review record` always
  failed. It polls without the state lock.
- **Stops without an agent type are dropped.** Claude Code 2.1.280 sends SubagentStop about every 30 seconds while a
  subagent runs, with an empty `agent_type`, a fresh `agent_id`, and no transcript. They are not agents the model
  started (likely internal progress queries), so the hook does not record them and readers skip old ones.
- **Non-blocking findings do not reopen the run.** The skill accepts after a clean review and reports the
  non-blocking findings; one extra review cost a reviewer spawn in dogfooding for a `low` finding. Applying them
  when the user asks is not a repair, so it costs no fix round.
- **Hook events live beside the state, not in it.** `verify` holds the write lock while gates run, and a second
  writer is refused rather than queued (test 11). A hook must never wait for or lose to that lock, so it appends to
  `events.jsonl` under its own short `flock`. Readers (`status`, `report`) take no lock; state is replaced atomically.
- **Self-review is measured from the last independent review** (or the start of the run), so a chain of small
  self-reviews cannot replace an independent one.
- **Gates must leave the tree alone.** If the fingerprint changes during `verify`, the verify fails and names the
  files, because no single tree was tested. Fixing that (usually ignoring generated files) needs no fix round;
  repairing a failing gate does.
- **Loopholes closed by phase rules.** After a failed gate, `verify` is refused once files changed until `fix` runs.
  After a blocking review, `verify` and `review begin` are refused until `fix`. Without these, the fix cap could be
  skipped by verifying again.
- **Config is copied at `start`**, so editing `config.toml` mid-run cannot change the gates of that run.
- **Trust points.** `subtask add --origin user` is the model's claim that the user asked for more; the report
  labels such subtasks. The CLI does not stop a process from editing `.supervise/` by hand. It guards against drift,
  not against a hostile agent.

## Spike results used (details in `docs/spike-findings.md`)

- **S1.** No ``!`…` `` dynamic-context line in SKILL.md; the skill's first step is `supervisectl status`.
  `/supervise:supervise` is the form that always works while the old user skill exists.
- **S2.** `bin/` is on PATH for Bash but not for hooks, so hooks.json calls `"${CLAUDE_PLUGIN_ROOT}/bin/supervisectl"`.
  `allowed-tools` only covers the turn that invoked the skill; SKILL.md tells the user how to allow
  `Bash(supervisectl *)`.
- **S3, S5.** Agents are addressed as `supervise:*`. Review evidence is SubagentStop `agent_type` (canonical, no
  `plugin:` prefix). A user `!` command bypasses PreToolUse, so the hook blocks `override` for the model only, and
  there is no TTY check.
- **S4.** Agents set `model: inherit` and no `effort`.
- **S8.** The PreToolUse hook uses matcher `Bash` plus `if: "Bash(*supervisectl*)"`, so unrelated Bash calls start
  no process. It denies `override` whether or not a run is open. The SubagentStop hook has no shell prefilter: it
  walks up from the session's cwd to the repository root (sessions opened in a subdirectory), and subagent stops
  are rare.
- **S9.** `make validate` runs `claude plugin validate --strict` on `plugin.json` (skills, agents, hooks) and on the
  directory (marketplace). `tests/test_plugin_files.py` covers what `--strict` misses. The skill description was
  trimmed; `claude plugin details` projects about 276 always-on tokens for the plugin.

## Deviations from the plan

- The plan lists two hooks. S3 and S8 also suggested denying non-supervise Agent calls and guarding writes to
  `.supervise/`; v0.1 keeps two hooks. A substituted reviewer cannot pass `review record`, which covers the failure
  that mattered (a silent `general-purpose` reviewer).
- The plan names only `review record`; the reviewer's findings are taken from the hook event (see Decisions).
- The reviewer's reply is `last_assistant_message`, except when the subagent handed its report back through the
  `SubagentHandback` tool (seen in dogfooding on Claude Code 2.1.280): the final message is then only a note, so the
  hook takes the handback's message from the subagent transcript. Only a `{"findings": ...}` object that ends the
  reply counts; that note quoted `{"findings": []}` in its prose, which v0.1 would have read as a clean review.

## Checked on 2026-09-23

A nested `claude -p --model haiku --plugin-dir .` session (Claude Code 2.1.280) spawned `supervise:code-reviewer`
on a real run: the SubagentStop hook recorded `agent_type: supervise:code-reviewer` with the reply's findings JSON,
`review record` accepted it, and the PreToolUse hook denied the model's `supervisectl override` with the message
above. The same run showed two gate pitfalls now handled in the output: a test gate that cannot start (pytest
missing) fails the same way before and after, which `verify`, `accept`, and `report` now point out, and an
unignored `__pycache__/` makes the gates change the tree.
