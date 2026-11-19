# supervise

A Claude Code plugin for plan, implement, verify, and review runs where the rules are checked by code, not by the
prompt. A small bundled CLI, `supervisectl`, keeps the run state on disk and accepts the work only when the
project's own gates pass and an independent read-only review is clean, both for the exact working tree being
accepted.

## Why

supervise started as a markdown skill I used to run plan → implement → review → verify loops in Claude Code. Across five real runs, the rules written in that skill broke in ways a prompt can't prevent: work was accepted after a one-line edit, made after the review had passed, broke the parser; a review verdict was never saved; missing reviewer agents were silently swapped for a generic one; and a project with no tests at all was reported as passing. So the rules moved out of the prompt into a small CLI that Claude has to go through. It keeps the run state on disk and refuses to accept work unless the project's gates pass and an independent review is clean for the exact final tree.

## What it solves

| Problem with a prompt-only workflow | What `supervisectl` does instead |
|---|---|
| Work accepted without verification, or edited after the review passed | `accept` needs a passing verify and a clean review whose working-tree fingerprints both equal the current tree. Any edit after review means verify and review again. |
| Run state written by hand by the model, then wrong or lost | State lives in `.supervise/runs/<id>/state.json`, written only by the CLI. A run can be paused and resumed from another session. |
| A missing reviewer agent silently replaced by a generic one | A review counts only if a `supervise:code-reviewer` subagent started and finished after `review begin`, as recorded by hooks. Its findings are read from its own reply, not passed on by the orchestrator. |
| A project without tests reported as passing | Without a gate of kind `test`, `status`, `verify`, `accept`, and `report` say NO TESTS. |
| Fix loops with no end | Each fix after a failed verify or blocking review uses a round; at the cap the run escalates, and only the user can raise the cap or drop the run. |

Each row is covered by an end-to-end test in `tests/` that drives the real CLI in a temporary git repository.

## Status

Stable for daily use: 1.0.5, used on real projects in Claude Code. Every rule above has an end-to-end test, and CI
runs lint and the tests on Linux and macOS with Python 3.11 to 3.14. It is a one-person project, so expect changes
to follow Claude Code releases. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Quickstart

Requirements: Claude Code 2.1.280 or newer, git, and Python 3.11 or newer on Linux or macOS. The CLI uses the
standard library only; there is nothing else to install.

From GitHub:

```sh
claude plugin marketplace add mrramdn/supervise
claude plugin install supervise@supervise
```

From a local clone:

```sh
git clone https://github.com/mrramdn/supervise ~/src/supervise
claude plugin marketplace add ~/src/supervise
claude plugin install supervise@supervise
```

Then, in a Claude Code session inside a git repository:

```
/supervise:supervise add rate limiting to the upload endpoint
```

On the first run Claude calls `supervisectl init`, which detects the gates (commands such as `make test`,
`pnpm test`, `ruff check .`) from `Makefile`, `package.json`, and `pyproject.toml` and writes them to
`.supervise/config.toml`. Check that file before the run starts: the gates decide what "verified" means.

Every phase change is a `supervisectl` call. To stop Claude Code from asking permission for each one, allow
`Bash(supervisectl *)` in your settings.

`supervisectl` is on PATH only inside Claude Code: the plugin's `bin/` is added for Claude's Bash calls, not for
your own shell, and the install path changes with every plugin version. When you need the CLI yourself, type it in
the Claude Code prompt with `!`, as in `! supervisectl status`.

## How it works

There are two sides:

- **The skill, `/supervise`, is for you.** You give it a task, or `status`, `resume`, or `report`. It tells Claude
  how to run the loop: plan, implement, verify, review, then accept or fix.
- **The CLI, `supervisectl`, is for Claude.** Claude runs it through Bash at every phase change. It owns the state
  and refuses a step that breaks a rule (exit 2, state unchanged, with the rule and the next step in the message).
  You only use it yourself for human decisions: `override` and `abort`.

```
init -> start -> PLAN -> IMPLEMENT -> VERIFY -> REVIEW -> ACCEPT
                              ^          |         |
                              |          v         v
                              +------- FIX <-------+
```

- `start` runs the gates once as a baseline, so failures that were already there are not blamed on the change.
- `verify` runs the gates and records the fingerprint of the working tree (its git tree id, untracked files
  included, ignored files excluded).
- `review begin` writes the diff and a brief for the read-only `supervise:code-reviewer` agent (Read, Grep, Glob
  only). SubagentStart and SubagentStop hooks record the reviewer and its reply, and `review record` parses its
  findings. A reviewer started before `review begin` does not count. Findings at or above `high` (configurable),
  and any unmet acceptance criterion from the plan, block.
- `accept` passes only when verify and review ran on the current tree.
- `commit`, if you ask for it, commits exactly the accepted tree with a Conventional Commits message.

The plugin also ships `supervise:implementer` for large, independent subtasks, and a PreToolUse hook that denies
`supervisectl override` and `supervisectl abort` to the model. You type those with `!` in the prompt.

Design notes and the reasons behind each rule are in [DESIGN.md](DESIGN.md); the full CLI reference is
[skills/supervise/references/cli.md](skills/supervise/references/cli.md).

## Commands

Skill:

| Form | Does |
|---|---|
| `/supervise:supervise <task>` | Starts a run for the task, or continues the open one. |
| `/supervise:supervise status` | Shows the open run and the next step. |
| `/supervise:supervise resume` | Resumes a paused run, from this or another session. |
| `/supervise:supervise report` | Prints the report of the open or latest run. |

CLI (exit codes: 0 done, 1 negative result, 2 refused, 3 error):

| Command | Does |
|---|---|
| `init` | Detects gates and writes `.supervise/config.toml`. |
| `start "<task>"` | Runs the baseline and opens a run. |
| `status` | Shows the run, whether the tree changed since verify or review, and the next step. |
| `plan set <file\|->` | Records the plan and its subtasks. |
| `subtask add "<title>"` / `subtask done <id>` | Adds or completes a subtask. |
| `verify` | Runs the gates on the current tree. |
| `review begin` / `review record` | Starts a review and records the reviewer's findings. |
| `fix` | Starts a fix round, or escalates when the cap is reached. |
| `accept` | Accepts the run when verify and a clean review match the current tree. |
| `commit -m MSG` | Commits exactly the accepted tree, only when you ask. |
| `report [--run ID]` | Prints a markdown report of a run. |
| `pause` / `resume` | Pauses or resumes the open run. |
| `abort [--reason T]` | Yours only: drops the open run and lists the changes it leaves in the working tree. |
| `override --max-fix-rounds N --reason T` | Yours only: raises the fix cap and reopens an escalated run. |

## When to use something else

supervise is a thin layer of state, rules, and evidence around one task at a time. Other tools fit other needs:

- **[superpowers](https://github.com/obra/superpowers)** if you want a complete development methodology
  (brainstorming, planning, TDD, subagent-driven development, review) as a set of skills, or one that works
  across several agent harnesses.
- **[spec-kit](https://github.com/github/spec-kit)** if you want to start from a written specification and derive
  the plan and tasks from it.
- **Claude Code workflows (`ultracode`)** for large fan-out work that many subagents do in parallel within one
  session.
- **`/code-review`** if you only need a review of a diff or a pull request, without a run around it.
- **[stagent](https://github.com/jie-worldstatelabs/stagent)** if you want to define your own stages as a
  workflow state machine rather than use a fixed loop.

## Limitations

- Linux and macOS only. Needs git and Python 3.11+. Gates run through `/bin/sh` with your environment.
- Gate output is stored as is under `.supervise/`. Secrets that a gate prints are not redacted.
- The CLI guards against drift, not against a hostile agent: nothing stops a process from editing `.supervise/`
  by hand.
- Designed and tested with Opus 5.5 in Claude Code 2.1.280 to 2.1.283. Other models may follow the skill less
  well. The hooks use exec form, which older Claude Code versions do not run correctly.
- `/supervise:supervise` always works. The short `/supervise` can resolve to a different skill of the same name,
  such as one in `~/.claude/skills/`, without a warning.

## Development

```sh
make sync       # dev environment (ruff, pytest) through uv
make check      # lint and tests, as CI runs them
make validate   # claude plugin validate --strict, needs the claude CLI
```

## License

[MIT](LICENSE)
