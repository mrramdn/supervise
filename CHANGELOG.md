# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.6] - 2026-11-08

### Fixed

- Fingerprints refuse sparse checkouts and tracked files marked `assume-unchanged` or `skip-worktree`. Those
  index flags could hide an edit after verification and review, allowing `accept` to approve different content.
  The checkout and real index are left unchanged.
- A `gates` value that is not an array of tables is refused with exit 2 instead of raising a traceback before
  `start`. Missing gates and an empty array still produce the NO TESTS label.

## [1.0.5] - 2026-11-08

### Fixed

- In Claude Code's default permission mode, the skill's way of recording a plan asked for permission every time,
  even with `Bash(supervisectl *)` allowed: a Bash heredoc that holds JSON trips Claude Code's check for a brace
  next to a quote. The same went for transcribed and self-review findings. The skill now has the model write the
  JSON with the Write tool to `.supervise/plan.json` or `.supervise/findings.json` and pass that path. The CLI
  removes such a file once it is recorded, so the next run's Write doesn't hit a file the session hasn't read.
  `.supervise/` stays out of the tree fingerprint and the reviewed diff, and stdin (`-`) still works.

### Changed

- `supervisectl subtask done 1` means `s1`; models tend to write the number.
- The skill says to check that the plugin's agents loaded by reading the Agent tool's list, not by calling an
  agent, and that `review record` waits for the reviewer's hook itself, so it needs no `sleep` first.
- The skill says how to pick `model` and `effort` for the agents when the user's instructions ask for a choice: an
  implementer's fit its subtask, and the reviewer never gets a weaker model than the one that wrote the change.
  Without such instructions both agents still run on the session's model and effort, as before.

## [1.0.4] - 2026-11-07

### Added

- A plugin icon, `.claude-plugin/icon.png` (1024 px), for the plugin directory. Its SVG source is
  `.github/brand/icon.svg`.

### Fixed

- Ordinary `supervisectl` calls were denied when their text contained the word "override", such as
  `supervisectl start "let users override the theme"` or a plan, finding, or commit message that used it: the
  PreToolUse hook matched the word anywhere. It now denies `override` only as the subcommand, the way it has
  matched `abort` since 1.0.3.

### Changed

- The skill's `compatibility` field names Claude Code next to git and Python 3.11+ on Linux or macOS: supervise
  relies on the plugin's hooks and its `bin/` CLI, so the skill does not work outside Claude Code. The frontmatter
  drops `disable-model-invocation: false`, which is the default.

## [1.0.3] - 2026-11-07

### Fixed

- A stuck run could start over with fresh fix rounds: `supervisectl abort` followed by `start` reset the fix-round
  cap, and the new run took the aborted run's changes as its baseline, so `accept` and `commit` could carry
  changes no review saw. The PreToolUse hook now denies `abort` to the model, as it does `override`; you drop a
  run by typing `! supervisectl abort --reason "..."`. The escalation and open-run messages say so, and `abort`
  lists the files changed since the run started.

## [1.0.2] - 2026-09-28

### Changed

- The README says where `supervisectl` is on PATH: only in Claude's Bash calls inside Claude Code, not in your own
  shell, and the install path changes with every plugin version. Run the CLI yourself from the Claude Code prompt
  with `!`, as in `! supervisectl status`.

## [1.0.1] - 2026-09-28

### Fixed

- `subtask add --origin user` after a failed verify or a blocking review returned the run to IMPLEMENT without a
  fix round, so the fix-round cap could be skipped. It is now refused until `supervisectl fix` has counted the
  round; within the fix round, a new request from the user is added as before.
- A `supervise:code-reviewer` that started before `review begin`, such as a background reviewer of an older diff,
  counted as the review of the current tree. A new SubagentStart hook, matched to the reviewer only, records each
  start, and `review record` counts only reviewers that started and finished after `review begin`. A review begun
  under 1.0.0 needs `supervisectl review begin` again.
- Two runs started in the same second could sort in the wrong order, so `commit` and `report` without `--run`
  took the older run as the latest. Run ids now end in sub-second digits instead of random ones.

### Changed

- The hooks run in exec form (`command` plus `args`), as the Claude Code hooks docs advise for a hook that names
  `${CLAUDE_PLUGIN_ROOT}`: no shell starts, and the plugin path needs no quoting. The README now asks for Claude
  Code 2.1.280 or newer, the oldest version supervise was tested on.
- The skill gives `start` and `verify` a Bash timeout above the gates' run time, and when Claude Code moves a call
  that outlasts it to the background, waits for it to finish before the next `supervisectl` command.
- CI runs lint and tests on Python 3.11 to 3.14, on Linux and macOS, and runs `claude plugin validate --strict`.
- `plugin.json` has a `homepage`, and the package classifiers say Production/Stable, Linux, and macOS.

## [1.0.0] - 2026-09-24

### Added

- `supervisectl commit -m MSG` commits exactly the accepted tree, only when the user asks. It refuses when the
  working tree changed since `accept`, when the message is not a Conventional Commit or carries AI attribution, and
  when the run was already committed. Repository hooks run as usual.
- `review record --findings <file|->` records the orchestrator's transcription of a reviewer reply that has no
  findings JSON. The review is marked transcribed and the reply is saved as `review-N-reply.md` next to the diff.
- Findings with `"kind": "acceptance"` (an acceptance criterion of the plan that is not met or not shown) block
  `accept` at any severity.
- The reviewer brief lists the gate results and the subtask notes.
- README and this changelog.

### Changed

- `review record` waits up to 15 seconds (`--wait S`) for the reviewer's SubagentStop event, which can arrive a few
  seconds after the reviewer's reply.
- After a clean review, the skill accepts and reports the non-blocking findings instead of fixing them. A `fix`
  after a clean review, to apply non-blocking suggestions when the user asks, uses no fix round.
- `init` takes the package runner from the `packageManager` field of `package.json` before looking at lockfiles.
- The code-reviewer agent's output instructions are stricter: every finding goes into one JSON block that ends
  the reply.

### Fixed

- Reviewer findings were lost when the reviewer handed its report back through a tool call: the hook now reads
  the handed-back report from the subagent transcript instead of the closing note.
- Only a `{"findings": [...]}` object that ends the reply is parsed, fenced or not, so a quoted example in prose is
  no longer read as a clean review.
- SubagentStop events without an agent type, which Claude Code sends while a subagent runs, are no longer
  recorded.

## [0.1.0] - 2026-09-23

### Added

- The `supervise` skill: a plan, implement, verify, review loop driven through the CLI.
- `supervisectl`, a standard-library Python CLI that keeps the run state in `.supervise/` and checks every phase
  change: `init`, `start`, `status`, `plan set`, `subtask add|done`, `verify`, `review begin|record`, `fix`,
  `accept`, `report`, `pause`, `resume`, `abort`, and `override`.
- Gate detection from `Makefile`, `package.json`, and `pyproject.toml`, with a baseline run at `start`.
- `accept` only when the last verify passed and a clean independent review ran on the same working-tree
  fingerprint as the current tree.
- NO TESTS label for projects without a test gate, a fix-round cap that escalates the run, and a
  `self`-review path limited to diffs of at most 10 lines.
- Agents `supervise:code-reviewer` (read-only) and `supervise:implementer`.
- Hooks: PreToolUse denies `supervisectl override` to the model; SubagentStop records finished reviewers as
  review evidence.
- End-to-end tests of the core rules, a commit message checker, and CI on Python 3.11.

[1.0.5]: https://github.com/mrramdn/supervise/compare/supervise--v1.0.4...supervise--v1.0.5
[1.0.4]: https://github.com/mrramdn/supervise/compare/supervise--v1.0.3...supervise--v1.0.4
[1.0.3]: https://github.com/mrramdn/supervise/compare/supervise--v1.0.2...supervise--v1.0.3
[1.0.2]: https://github.com/mrramdn/supervise/compare/supervise--v1.0.1...supervise--v1.0.2
[1.0.1]: https://github.com/mrramdn/supervise/compare/supervise--v1.0.0...supervise--v1.0.1
[1.0.0]: https://github.com/mrramdn/supervise/compare/supervise--v0.1.0...supervise--v1.0.0
[0.1.0]: https://github.com/mrramdn/supervise/releases/tag/supervise--v0.1.0
