# Getting started

## Requirements

- Claude Code 2.1.280 or newer. The hooks use exec form, which older versions do not run correctly.
- git. supervise works inside a git repository and fingerprints the working tree with it.
- Python 3.11 or newer, on Linux or macOS. The CLI uses the standard library only; there is nothing to `pip install`.

## Install

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

Start a new Claude Code session afterwards. The plugin adds:

- the skill `/supervise:supervise`,
- the agents `supervise:code-reviewer` (read-only) and `supervise:implementer`,
- three hooks (PreToolUse, SubagentStart, SubagentStop),
- the `supervisectl` command, which Claude Code puts on PATH for Bash calls while the plugin is enabled.

### Allow `supervisectl` once

Claude runs `supervisectl` at every phase change. The skill's own permission lasts only for the turn that invoked
it, so later calls ask again. To stop that, allow the command in your settings, for example in
`.claude/settings.json` of the project or in `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["Bash(supervisectl *)"]
  }
}
```

This does not let Claude raise its own fix-round cap or drop a run: a hook refuses `supervisectl override` and
`supervisectl abort` from the model whatever the permissions say.

## Your first run

In a Claude Code session inside a git repository:

```
/supervise:supervise add rate limiting to the upload endpoint
```

Use the full name `/supervise:supervise`. The short `/supervise` also works unless another skill with the same name
is installed, for example one in `~/.claude/skills/`, in which case that one runs without a warning.

### 1. Init: check the gates

On the first run in a repository, Claude runs `supervisectl init`. It looks at `Makefile`, `package.json`, and
`pyproject.toml`, guesses the project's checks, and writes them to `.supervise/config.toml`:

```
Initialized supervise in /path/to/repo
Config: /path/to/repo/.supervise/config.toml (.supervise/ is excluded from git through .git/info/exclude)
Gates:
  lint       lint      make lint
  test       test      make test
Check the gates with the user and edit the config if they are wrong; each run copies them at start.
next: `supervisectl start "<task>"`
```

These gates decide what "verified" means for every run, so check them now. Claude will show them to you and wait.
If a check is missing, or a command is wrong, edit the file (see [Configuration](configuration.md)). If there is no
gate of kind `test`, every run is labelled NO TESTS.

### 2. Start: the baseline

`supervisectl start` runs every gate once, before anything changes:

```
Starting run 20261108-123544-3233: 2 baseline gate(s).
  running lint: make lint
  running test: make test
Run 20261108-123544-3233 started: add rate limiting to the upload endpoint
Baseline (before any change):
  pass              lint       make lint
  pass              test       make test
next: write the plan JSON to `.supervise/plan.json`, then `supervisectl plan set .supervise/plan.json`
```

A gate that already fails here is not blamed on the change later, as long as it fails the same way.

Claude then checks that the plugin's agents loaded. It reads the Agent tool's list of agent types; it does not call
an agent for this. If `supervise:code-reviewer` is missing, it stops and tells you.

### 3. Plan and implement

Claude splits the task into subtasks with acceptance criteria. It writes the plan with the Write tool to
`.supervise/plan.json` and records it. The CLI removes the file once the plan is recorded:

```
Plan recorded: 2 subtask(s).
  s1 Add a per-client limiter
  s2 Return 429 when the limit is hit
Read and removed .supervise/plan.json.
next: finish the changes, then `supervisectl verify`
```

`.supervise/` is not part of the reviewed diff, so the plan file never shows up in the review.

Then Claude does the work and marks each subtask done as it lands, with `supervisectl subtask done s1` (or just
`1`). For a large subtask that is independent of the rest, it may hand it to `supervise:implementer`.

If you ask for something extra in the middle of the run, Claude records it as a subtask that came from you.

### 4. Verify

`supervisectl verify` runs every gate again and records the fingerprint of the working tree. A failing gate means
a fix round:

```
Verify #1: 2 gate(s).
  running lint: make lint
  running test: make test
Verify #1: fail (lint pass, test fail)
  FAIL              test       make test (exit 2)
    | grep -q ok app.txt
    | make: *** [Makefile:5: test] Error 1
    full output: /path/to/repo/.supervise/runs/20261108-123544-3233/logs/verify-1-test.log
next: `supervisectl fix` before changing code, then repair what failed
```

```
Fix round 1/3.
  to fix: gate test failed: make test (log: /path/to/repo/.supervise/runs/20261108-123544-3233/logs/verify-1-test.log)
next: finish the changes, then `supervisectl verify`
```

### 5. Review

After a passing verify, `supervisectl review begin` writes the diff and a brief. Claude starts the read-only
`supervise:code-reviewer` agent with that brief. The hooks record the reviewer's start and its reply, and
`supervisectl review record` reads the findings from there, not from Claude. Claude runs it as soon as the reviewer
finishes. The command itself waits up to 15 seconds for the hook to record the reply, so no pause is needed first.

```
Review #1: clean, independent; 0 blocking, 1 non-blocking
  low      app.txt:2: No trailing punctuation on the greeting
next: `supervisectl accept`
```

Findings of severity `high` or above, and any acceptance criterion that is not met, block. A blocking review
means another fix round, then verify and review again.

### 6. Accept

`supervisectl accept` passes only when the last verify passed, the last review is clean, and both saw exactly the
current working tree. Any edit after the review is caught:

```
supervisectl: refused: files changed since the review
  The working tree no longer matches what was verified and reviewed. Changed: M app.txt.
  next: `supervisectl verify`, then `supervisectl review begin` and review again
```

On success:

```
ACCEPTED run 20261108-123544-3233: add rate limiting to the upload endpoint
Verify #2: pass (lint pass, test pass)
Review #1: clean, independent; 0 blocking, 1 non-blocking
Fix rounds used: 1/3
Full report: `supervisectl report`
Nothing is committed. Only if the user asks: `supervisectl commit -m "<conventional message>"`.
```

### 7. Report and commit

Claude ends with `supervisectl report`, a markdown summary of the plan, the gate results against the baseline,
every review and its findings, the subagents that ran, and the full history of the run.

Nothing is committed unless you say so. Check the result first (in a browser, for a visual change). If you want a
commit, tell Claude; it runs `supervisectl commit -m "feat(upload): add rate limiting"`, which commits exactly the
accepted tree and refuses a message that is not a Conventional Commit or that carries AI attribution.

## Everyday commands

| You type | What happens |
|---|---|
| `/supervise:supervise <task>` | Starts a run, or continues the open one. |
| `/supervise:supervise status` | Shows the open run, its phase, and the next step. |
| `/supervise:supervise resume` | Resumes a paused run, from this session or a new one. |
| `/supervise:supervise report` | Prints the report of the open or latest run. |
| `! supervisectl override --max-fix-rounds 5 --reason "..."` | Gives an escalated run more fix rounds. Only you can run this. |
| `! supervisectl abort --reason "..."` | Drops the open run and leaves the files as they are. Only you can run this. |

The state lives in `.supervise/` at the repository root, so a run survives a closed terminal. Before you end a
session in the middle of a run, ask Claude to pause it; any later session picks it up with `resume`.

Next: [Concepts](concepts.md) explains each rule in detail.
