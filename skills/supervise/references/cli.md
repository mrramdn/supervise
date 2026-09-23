# supervisectl reference

Run every command from anywhere inside the project's git repository. State lives in `.supervise/` at the
repository root, which `init` adds to `.git/info/exclude`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done. |
| 1 | Negative result: a gate failed, the review has blocking findings, or the run escalated. State was updated. |
| 2 | Refused: the command breaks a rule or is used wrongly. State is unchanged; the message says what to do next. |
| 3 | Error outside the rules, such as a failing git command or an unreadable state file. |

## Commands

| Command | Allowed when | Effect |
|---|---|---|
| `init` | any time | Detects gates from `Makefile`, `package.json`, `pyproject.toml`; writes `.supervise/config.toml` if missing. |
| `start "<task>"` | no open run | Runs the gates as the baseline and opens a run in phase PLAN. |
| `status` | any time | Shows the open run, whether the tree changed since verify or review, and the next step. |
| `plan set <file\|->` | PLAN | Records the plan; moves to IMPLEMENT. |
| `subtask add "<title>" [--acceptance T] [--origin plan\|user]` | IMPLEMENT, FIX; VERIFY and REVIEW only with `--origin user` | Adds a subtask. From VERIFY or REVIEW it returns to IMPLEMENT without a fix round. |
| `subtask done <id> [--note T]` | after PLAN | Marks a subtask done. |
| `verify` | IMPLEMENT, FIX, VERIFY, REVIEW | Runs the gates; moves to VERIFY. Refused after a failed verify if files changed since (run `fix` first), and after a blocking review. |
| `review begin` | VERIFY with a passing verify of the current tree | Writes `runs/<id>/review-<n>.diff` and prints the reviewer brief; moves to REVIEW. |
| `review record` | REVIEW, after `review begin`, tree unchanged | Reads the findings of every `supervise:code-reviewer` that finished since `review begin`. |
| `review record --self --reason T [--findings <file\|->]` | as above, diff of at most 10 lines and no binary files since the last independent review | Records the orchestrator's own review. |
| `fix` | VERIFY, REVIEW | Starts a fix round and lists what to repair. When all rounds are used, the run becomes escalated instead (exit 1). |
| `accept` | REVIEW with a clean review | Accepts when the last verify passed and verify, review, and the current tree have the same fingerprint. |
| `report [--run ID]` | any time | Prints a markdown report of the open run, or the latest run. |
| `pause [--reason T]` / `resume` | active / paused run | Pauses or resumes the run; any process or session can resume. |
| `abort [--reason T]` | open run | Drops the run and leaves the working tree as it is. |
| `override --max-fix-rounds N --reason T` | open run, N above the rounds used | For the user only. Raises the cap and reactivates an escalated run. |
| `hook <event>` | from hooks | Internal. |

## Rules the CLI enforces

- **Fingerprint.** The fingerprint is the git tree id of the working tree as `git add -A` would stage it, built in
  a temporary index (ignored files and `.supervise/` excluded; committing does not change it). `verify` and
  `review begin` record it; `accept` requires both to equal the current one.
- **Baseline.** A gate that fails at `start` counts as "same as baseline" at `verify` only when its exit code and
  its output, with durations, timestamps, and addresses removed, are identical. A gate that changes the working
  tree fails the verify.
- **NO TESTS.** Without a gate of kind `test`, status, verify, accept, and report say NO TESTS.
- **Fix rounds.** Each `fix` uses one round, up to `max_fix_rounds`. `subtask add --origin user` does not use one.
- **Independent review.** `review record` needs a SubagentStop of `supervise:code-reviewer` after `review begin`;
  the hook stores its final reply and the CLI parses the last `{"findings": [...]}` object in it.
- **Blocking.** Findings at or above `block_on_severity` (default `high`) block accept. Unknown severities count
  as `high`.
- **One writer.** Commands that change state take a non-blocking lock; a second writer is refused (exit 2) while
  the first runs. `status` and `report` read without the lock.

## Files

```
.supervise/
  config.toml            project config (copied into each run at start)
  current                id of the open run
  lock                   write lock
  runs/<id>/state.json   run state and history
  runs/<id>/events.jsonl subagent stops recorded by the hook
  runs/<id>/logs/        full gate output per baseline and verify
  runs/<id>/review-N.diff
```

`config.toml`:

```toml
max_fix_rounds = 3
block_on_severity = "high"   # critical, high, medium, low, info

[[gates]]
name = "test"
command = "make test"
kind = "test"                # test, lint, typecheck, build, other
timeout = 1800               # seconds
```

Plan JSON (`plan set`): `{"summary": "...", "subtasks": [{"title": "...", "acceptance": "..."}]}`. A bare list of
titles also works.

Findings JSON (reviewer reply, `--findings`):
`{"findings": [{"file": "src/app.py", "line": 42, "severity": "high", "summary": "..."}]}`.

## Limits

- Gate output is stored as is (the last 50 lines in `state.json`, the full output under `logs/`); secrets that a
  gate prints are not redacted.
- The CLI guards against drift, not against a hostile agent: it cannot stop a process from editing
  `.supervise/` directly.
- Gates run through `/bin/sh` from the repository root with the caller's environment.
