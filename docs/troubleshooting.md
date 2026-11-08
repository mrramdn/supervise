# Troubleshooting

When `supervisectl` refuses a step, it exits with code 2, changes nothing, and prints three things: the rule, the
reason, and the next step. Claude follows the `next:` line. This page lists the refusals you are most likely to
see, what caused them, and what to do.

| Exit code | Meaning |
|---|---|
| 0 | Done. |
| 1 | Negative result: a gate failed, the review blocks, or the run escalated. The state was updated. |
| 2 | Refused. The state is unchanged; the message says what to do next. |
| 3 | Error outside the rules, such as a failing git command or an unreadable state file. |

## Setup

**`supervise is not initialized in this repository`**
Run `/supervise:supervise <task>` and let Claude run `supervisectl init`, or run `supervisectl init` yourself.

**`the repository root is your home directory`**
Your home folder is a git repository, and supervise refuses to keep state for all of it. Open Claude Code inside
the project's own repository.

**Every `supervisectl` call asks for permission**
Allow `Bash(supervisectl *)` in your settings. See [Getting started](getting-started.md#allow-supervisectl-once).

**Claude asks for permission every time it records a plan or findings**
Passing JSON to a command in a heredoc makes Claude Code ask on every call, even with `Bash(supervisectl *)`
allowed. Since 1.0.5 the skill writes the JSON to `.supervise/plan.json` or `.supervise/findings.json` with the
Write tool and passes the path instead. If you still see this, update the plugin.

**Claude says the plugin's agents did not load**
After `start`, the skill reads the Agent tool's list of agent types, without calling an agent. It stops when
`supervise:code-reviewer` is not in the list, because only that agent's review counts. Check that the plugin is
enabled (`claude plugin list`), then start a new session.

**`/supervise` runs a different skill**
Another skill named `supervise` is installed, for example in `~/.claude/skills/`. Use the full name
`/supervise:supervise`.

**`supervisectl: command not found` in your own terminal**
Claude Code puts `supervisectl` on PATH only for its own Bash calls. Type commands in the Claude Code prompt with
the `!` prefix instead, as in `! supervisectl status`. You can also call the launcher by its path in the plugin
folder, but that path changes with every plugin version.

**`the supervise config is invalid`**
The next lines name the file and the problem. For example, `gates must be an array of tables ([[gates]])` means
`gates` is a plain value or a single `[gates]` table. Write one `[[gates]]` block per gate (see
[Configuration](configuration.md)). With no gates at all, leave `gates` out or write `gates = []`.

## The working tree

**`sparse checkout is not supported`**
In a sparse checkout, the files outside it are missing, so supervise cannot fingerprint the whole tree. Switch to a
full checkout with `git sparse-checkout disable`, then retry.

**`git index flags hide tracked files from verification`**
Some tracked files are marked `assume-unchanged` or `skip-worktree`, and the message lists up to ten of them. With
those flags, git can skip an edit to the file, so a change after the review could go unnoticed. `git ls-files -v`
shows them with a lowercase letter (assume-unchanged) or `S` (skip-worktree). Clear the flags, then retry:

```sh
git update-index --no-assume-unchanged -- <files>
git update-index --no-skip-worktree -- <files>
```

Both refusals come from any command that fingerprints the tree, such as `start`, `verify`, `review begin`, and
`accept`. The CLI does not change your checkout or index.

## Starting a run

**`run <id> is still open (...)`**
Only one run is open per repository. Finish it (`/supervise:supervise status` shows the next step), or drop it by
typing `! supervisectl abort --reason "..."` in the Claude Code prompt. Claude cannot drop it for you: a hook denies
`abort` to the model.

**The baseline already fails**
That is fine. A gate that fails at `start` passes at `verify` if it fails in exactly the same way. If the failure
is caused by the environment (a missing tool, a database that is not running), fix that first, or the gate stays
useless for this run.

## Verify

**`files changed after a failed verify without \`supervisectl fix\``**
The last verify failed and files changed since. A repair has to use a fix round: run `supervisectl fix`, then
verify.

**`the last review has blocking findings`**
After a blocking review, `verify` and `review begin` wait for `fix`.

**The verify fails because the tree changed while the gates ran**
A gate wrote files that git does not ignore, such as `__pycache__/`, coverage output, or a build folder. Add them
to `.gitignore` and verify again. This does not use a fix round.

**A gate fails with the same error before and after**
If the output points at the setup (for example `pytest: not found`), the gate cannot start at all, and a pass
"same as baseline" shows nothing. Fix the command in `.supervise/config.toml`; it applies from the next run.

**The verify takes longer than the Bash timeout**
Claude Code moves the call to the background, where it keeps running. The skill waits for it before the next
`supervisectl` command. Raise the gate's `timeout` in the config if the gate itself runs out of time.

## Review

**`no result from supervise:code-reviewer since \`review begin\``**
No reviewer finished after `review begin`, or a different agent ran instead (the message lists the ones it saw).
Run the Agent tool with `subagent_type: "supervise:code-reviewer"` and the brief from `review begin`.
`review record` waits up to 15 seconds for the reviewer's stop; `--wait S` changes that.

**`the supervise:code-reviewer that finished started before \`review begin\``**
That reviewer read an older diff, typically one that ran in the background while the code changed. Start a new
reviewer with the brief from the last `review begin`.

**`the supervise:code-reviewer reply has no findings JSON`**
The reviewer did not end its reply with `{"findings": [...]}`. Claude copies every finding from the reply, as
written, into `.supervise/findings.json` and runs `supervisectl review record --findings .supervise/findings.json`.
The review is marked transcribed and the reply is saved as `review-N-reply.md` so you can compare them.

**`files changed since \`review begin\``** or **`files changed since the review`**
Something edited the tree after the review started. Verify and review again. Keep files untouched from
`review begin` until `accept`.

**`self-review is only allowed for small diffs`**
Self-review covers at most 10 changed lines with no binary files since the last independent review. Use the
reviewer agent.

## Fix rounds

**`the last result is negative, so \`supervisectl fix\` comes first`**
A new subtask was added right after a failed verify or a blocking review. Run `fix` to count the round, then add
the subtask inside it.

**The run is escalated**
All fix rounds are used. Claude stops and reports what still fails. Decide whether to continue; if so, type in
the Claude Code prompt:

```
! supervisectl override --max-fix-rounds 5 --reason "why more rounds are worth it"
```

The new cap must be above the rounds already used. Claude cannot run this command: a hook denies it. To stop
instead, type `! supervisectl abort --reason "..."`.

**A hook denies Claude's `supervisectl` call**
The PreToolUse hook denies `override` and `abort` to the model. It matches them only as the subcommand, so
`supervisectl start "let users override the theme"` goes through. If an ordinary call that only mentions the word
`override` is denied, the plugin is older than 1.0.4: update it.

## Accept and commit

**`accept needs a passing verify and a recorded clean review of the current tree`**
Run the missing step. `supervisectl status` shows which one.

**`the commit message is not allowed`**
Use a Conventional Commit header, `type(scope): subject`, with one of `feat`, `fix`, `docs`, `style`,
`refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`, and no AI attribution.

**`files changed since accept`**
The tree no longer matches the accepted one. Put it back, or start a new run for the extra change.

**`nothing to commit`**
The accepted tree is already the tree of HEAD, for example after a run that changed nothing.

## Sessions

**The session ended in the middle of a run**
The state is on disk. In a new session, `/supervise:supervise resume` continues from the recorded phase. Ask Claude
to pause before you close a session on purpose.

**`another supervisectl command is running in this project`**
Only one command may change the state at a time, for example while a long `verify` runs. Wait for it to finish.

## Looking deeper

- `supervisectl status` shows the phase, whether the tree changed since the last verify or review, and the next step.
- `supervisectl report` shows the plan, gates, reviews, subagents, and the history of every phase change.
- `.supervise/runs/<id>/logs/` holds the full output of every gate run.
- `.supervise/runs/<id>/events.jsonl` holds the subagent starts and stops the hooks recorded.
- `.supervise/hook.log` holds errors from the subagent hooks, if any. A hook error never stops the session.
