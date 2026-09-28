# Concepts

supervise splits the work between two sides. Claude does the planning, the coding, and the reviewing through its
agents. `supervisectl` keeps the state of the run on disk and decides whether each step is allowed. When a step
breaks a rule, the CLI refuses it (exit 2), leaves the state as it was, and prints the rule and the next step. The
rules therefore hold whether or not the model remembers them.

## The run

```
init -> start -> PLAN -> IMPLEMENT -> VERIFY -> REVIEW -> ACCEPT
                              ^          |         |
                              |          v         v
                              +------- FIX <-------+
```

| Phase | What happens | Moves on with |
|---|---|---|
| PLAN | Claude reads the code and writes subtasks with acceptance criteria. | `plan set` |
| IMPLEMENT | Claude changes the code and marks subtasks done. | `verify` |
| VERIFY | The gates ran on the current tree. | `review begin` after a pass, `fix` after a failure |
| REVIEW | The read-only reviewer is looking at the diff. | `review record`, then `accept` or `fix` |
| FIX | A fix round is open to repair what failed. | `verify` |
| ACCEPT | The run is done. | `commit`, only if you ask |

A run is also in one of these states: active, paused (resumable from any session), escalated (out of fix rounds,
waiting for you), accepted, or aborted. One run is open per repository at a time.

## Gates and the baseline

A gate is a shell command that checks the project, such as `make test` or `pnpm run lint`. Each gate has a kind:
`test`, `lint`, `typecheck`, `build`, or `other`. A gate passes when it exits 0.

`start` runs every gate once before anything changes. This is the baseline. If a gate already fails there, the
failure is not blamed on the change: at `verify` it counts as "same as baseline" when it exits with the same code
and prints the same output, once durations, timestamps, memory addresses, and colour codes are removed. If it fails
differently, the verify fails.

A gate must leave the working tree alone. If the tree changes while the gates run, for example because a build
writes files that git does not ignore, the verify fails and names the files. Ignore the generated files and verify
again; that needs no fix round.

The config is copied into the run at `start`, so editing `.supervise/config.toml` in the middle of a run changes
the next run, not this one.

## NO TESTS

If no gate has kind `test`, `status`, `verify`, `accept`, and `report` all say NO TESTS. A lint or build gate that
passes does not show that the change works, and the label makes sure nobody reads a green run as tested.

## The fingerprint

The fingerprint is the content of the working tree: the git tree id you would get from `git add -A`, built in a
temporary index. It includes untracked files and excludes ignored files and `.supervise/`. It does not depend on
commits, so committing does not change it.

`verify` and `review begin` each record the fingerprint. `accept` needs both to equal the current one. Any edit after
the review, even one line, means verify and review again. This is the rule that stops "one small fix after the
review" from landing untested and unreviewed.

## Independent review

`review begin` writes the diff since the run started to `.supervise/runs/<id>/review-<n>.diff` and prints a brief:
the task, the subtasks and their acceptance criteria, and the gate results for this tree. Claude passes the brief to
the `supervise:code-reviewer` agent, which can only read files (Read, Grep, Glob).

A review counts as independent only when the hooks saw a `supervise:code-reviewer` start and finish after
`review begin`, matched by its agent id:

- A different agent, such as `general-purpose`, does not count, so a missing reviewer cannot be silently replaced.
- A reviewer that started before `review begin` read an older diff and does not count either.
- The findings come from the reviewer's own reply, captured by the SubagentStop hook. Claude cannot drop or soften
  them on the way.

The reviewer ends its reply with a JSON object, `{"findings": [...]}`, one entry per finding with a file, a line,
a severity, and a summary. If a reply has no such JSON, Claude may copy the findings into
`review record --findings -`. The review is then marked transcribed, and the reviewer's reply is saved next to the
diff so you can compare the two.

### Self-review

For a diff of at most 10 lines, with no binary files, since the last independent review, Claude may review the
change itself with `review record --self --reason "..."`. The size is measured from the last independent review, so
a chain of small self-reviews cannot replace a real one. The report lists every self-review.

### What blocks

- A finding at or above `block_on_severity` (default `high`). Severities are `critical`, `high`, `medium`, `low`,
  and `info`. An unknown severity counts as `high`.
- Any finding with `"kind": "acceptance"`: an acceptance criterion from the plan that is not met, or that nothing
  in the diff or the gate results shows. It blocks at any severity.

Non-blocking findings do not reopen the run. Claude accepts and lists them in its report. If you want them applied,
say so: `fix` after a clean review uses no fix round, and the change is verified and reviewed again.

## Fix rounds and escalation

Every `fix` after a failed verify or a blocking review uses one round, up to `max_fix_rounds` (default 3). The CLI
closes the ways around the cap:

- After a failed verify, changing files and verifying again is refused until `fix` runs.
- After a blocking review, `verify` and `review begin` are refused until `fix` runs.
- After either, adding a subtask is refused until `fix` has counted the round, even for a request from you.

When all rounds are used, the next `fix` escalates the run. Claude stops and tells you what still fails. Only you can
continue it, by typing the override in the prompt with the `!` prefix:

```
! supervisectl override --max-fix-rounds 5 --reason "flaky test fixed upstream, one more try"
```

A PreToolUse hook denies `supervisectl override` to the model. Commands you type with `!` do not pass through that
hook.

## Requests in the middle of a run

If you ask for more while Claude is implementing, it records the request with
`subtask add "<title>" --origin user`. From VERIFY or REVIEW this returns the run to IMPLEMENT without using a fix
round, since a new request is not a repair. The report labels these subtasks as coming from you, because the CLI
takes Claude's word for it.

## Commit

Nothing is committed by default. `commit -m MSG` works only for the latest accepted run, only while the working tree
still has the accepted fingerprint, and only once per run. It stages exactly that tree and runs a plain
`git commit`, so your git hooks run. The message must be a Conventional Commit (`type(scope): subject`) with no AI
attribution: no `Co-Authored-By` naming an AI tool, no "Generated with", no robot emoji.

## One writer at a time

Commands that change the state take a lock. A second writer is refused rather than queued, so two sessions cannot
interleave phase changes. `status` and `report` read without the lock. The hooks append to their own event file
under a short lock of their own, so a long `verify` never blocks them.

## What supervise does not do

- It guards against drift, not against a hostile agent. Nothing stops a process from editing `.supervise/` by hand.
- Gate output is stored as is, in `state.json` (the last 50 lines) and under `logs/`. Secrets that a gate prints are
  not redacted.
- It runs one task at a time. For many agents in parallel, or a full development methodology, see
  [When to use something else](../README.md#when-to-use-something-else).
