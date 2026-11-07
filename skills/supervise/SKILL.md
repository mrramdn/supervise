---
name: supervise
description: Checkpointed coding workflow (plan, implement, verify with the project's gates, then an independent read-only review). The bundled supervisectl CLI checks every phase change and accepts only when the gates pass and the review is clean for the exact final working tree; state survives restarts. Use when asked to supervise a task, carry a multi-file change through verification and review, or resume or report a supervise run. Not for quick edits or questions.
argument-hint: "<task> | status | resume | report"
allowed-tools: Bash(supervisectl *)
license: MIT
compatibility: Needs Claude Code, because it relies on the plugin's hooks and its bin/ CLI. Also needs git and Python 3.11+ on Linux or macOS.
---

# supervise

You do the work; `supervisectl` keeps the run state and decides whether each phase change is allowed. It is on
PATH in Bash while the plugin is enabled. When it refuses (exit 2), it names the rule that failed and the next
step, and nothing was changed, so follow its output rather than working around it.

## Start or resume

1. Run `supervisectl status`.
   - An open run: continue from the phase it shows (`supervisectl resume` if it is paused). If the user now asks
     for a different task, ask whether to finish the open run first or drop it, which only they can do (below).
   - Not initialized: run `supervisectl init` and show the user the gates it detected. The gates decide what
     "verified" means, so let the user correct `.supervise/config.toml` before starting.
2. `supervisectl start "<task>"` runs the gates once to record the baseline: what already fails before you touch
   anything.
3. Check that the Agent tool's list of agent types includes `supervise:code-reviewer` (and
   `supervise:implementer`); reading the list is enough, without calling an agent. If it is missing, stop and tell
   the user the plugin's agents did not load. Do not substitute `general-purpose` or another agent: only
   a finished `supervise:code-reviewer` counts as a review, so a substitute would only waste a round.

If every `supervisectl` call asks for permission, suggest the user allow `Bash(supervisectl *)` in their
settings; the skill's own grant ends with the turn that invoked it.

## Checklist

Copy this into your task list and keep it current:

```
- [ ] supervisectl status (init + start, or resume)
- [ ] plan set
- [ ] implement; subtask done as each subtask lands
- [ ] verify
- [ ] review begin, supervise:code-reviewer, review record
- [ ] accept, or fix, then verify and review again
- [ ] report to the user; offer a commit, and commit only if they say yes
```

## Phases

Say one sentence of progress at every phase change.

**Plan.** Read what you need, then split the task into subtasks with acceptance criteria. Write the plan with the
Write tool to `.supervise/plan.json`, which stays out of the reviewed diff, as
`{"summary": "...", "subtasks": [{"title": "...", "acceptance": "..."}]}`, and record it:

```bash
supervisectl plan set .supervise/plan.json
```

The CLI removes the file once the plan is recorded. Don't pass JSON in a heredoc: Claude Code asks for permission
on every such command.

**Implement.** Do the work yourself. Spawn `supervise:implementer` only for a large subtask that is independent of
the rest, and give it the subtask, the files involved, and the acceptance criteria. Run the relevant tests as you
go. Mark progress with `supervisectl subtask done <id>`. If the user asks for something new mid-run, record it
with `supervisectl subtask add "<title>" --origin user`; that returns the run to implementing without using a
fix round. After a failed verify or a blocking review, run `supervisectl fix` first and add it in that round.

**Verify.** `supervisectl verify` runs every gate and compares failures with the baseline. It takes as long as the
project's checks, so give the Bash call a timeout above their run time. If the timeout runs out first, Claude Code
moves the command to the background, where it keeps running: wait for it to finish and read its result before the
next `supervisectl` command. The same holds for `start`, which runs the gates for the baseline. Verification is the
CLI's job; do not hand it to a subagent. A gate that already failed at baseline passes only if its output is
unchanged.

**Review.** `supervisectl review begin` writes the diff and prints a brief. Call the Agent tool with
`subagent_type: "supervise:code-reviewer"` and that brief; a reviewer started before this `review begin` read an
older diff and does not count. When it finishes, run `supervisectl review record` right away: it reads the
reviewer's findings from the hook, waiting up to 15 seconds for them itself, and decides which ones block. If it
refuses because the reply has no findings JSON, copy every finding from the reviewer's reply, as written, into
`.supervise/findings.json` as `{"findings": [...]}` and run
`supervisectl review record --findings .supervise/findings.json`; the report marks the review as transcribed and
keeps the reply. Do not act on findings before they are recorded. Keep your hands off the files from `review begin` until `accept`: any
edit changes the tree fingerprint and accept will refuse.
A diff of at most 10 lines since the last independent review may be reviewed by you instead:
write your `{"findings": [...]}` to `.supervise/findings.json` and run
`supervisectl review record --self --reason "<why>" --findings .supervise/findings.json`. The report lists every
self-review.

**Fix.** After a failed verify or a blocking review, run `supervisectl fix` before you change code. It counts the
fix round and lists what to repair. Then repair, `verify`, and review again.

**Accept.** `supervisectl accept` succeeds only when the last verify passed, the last review is clean, and both
saw exactly the current working tree. After a clean review, accept right away and list the non-blocking findings in
your report; each extra change costs another verify and review. Apply them only if the user asks: then
`supervisectl fix` (after a clean review it uses no fix round), change the code, `verify`, and review again.

**Commit.** Nothing is committed by default; the user may want to check the result first (in a browser, for a
visual change). After accepting, ask whether to commit. Only on a yes, run
`supervisectl commit -m "<type(scope): subject>"`: it commits exactly the accepted tree and refuses if files
changed since accept or the message is not a Conventional Commit. Add no AI attribution to the message.

## Escalation, pause, abort

- Escalated (the fix rounds ran out): stop and tell the user what still fails. Only they can grant more rounds, by
  typing `! supervisectl override --max-fix-rounds <N> --reason "<why>"` in the prompt; a hook refuses `override`
  from you. After they do, run `supervisectl status` before continuing.
- Before ending a session mid-run, `supervisectl pause`. Any later session continues with `supervisectl resume`.
- Only the user can drop a run, by typing `! supervisectl abort --reason "<why>"`; a hook refuses `abort` from you.
  A new run after an abort would start with fresh fix rounds and take the dropped run's changes as its baseline,
  where no review sees them.

## Finishing

- End with `supervisectl report` and summarize it for the user: what changed, the gate results, the review
  verdict with the non-blocking findings, and anything not done.
- If the run is labelled NO TESTS, say so plainly: passing gates without a test gate do not show the change works.
- Stay within the requested scope. List what you left out or could not finish rather than stretching the task.

Commands, exit codes, and file formats: [references/cli.md](references/cli.md).
