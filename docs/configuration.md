# Configuration

Each repository has one config file, `.supervise/config.toml`, written by `supervisectl init`. `init` also adds
`.supervise/` to `.git/info/exclude`, so the run state never shows up in `git status` or in a commit.

Each run copies the config at `start`. Changes apply to the next run, not the one that is open.

## The file

```toml
max_fix_rounds = 3
block_on_severity = "high"   # critical, high, medium, low, info

[[gates]]
name = "lint"
command = "make lint"
kind = "lint"                # test, lint, typecheck, build, other
timeout = 1800               # seconds

[[gates]]
name = "test"
command = "make test"
kind = "test"
timeout = 1800
```

| Key | Default | Meaning |
|---|---|---|
| `max_fix_rounds` | `3` | Fix rounds a run may use before it escalates to you. |
| `block_on_severity` | `"high"` | Review findings at or above this severity block `accept`. Acceptance findings block at any severity. |
| `gates` | detected | The checks that run at `start` and at every `verify`, in order. |

Each gate:

| Key | Meaning |
|---|---|
| `name` | A short label used in output, logs, and the report. |
| `command` | Run through `/bin/sh` from the repository root, with your environment. Passes on exit 0. |
| `kind` | `test`, `lint`, `typecheck`, `build`, or `other`. Without a `test` gate, runs are labelled NO TESTS. |
| `timeout` | Seconds before the gate and its child processes are stopped; a gate that times out fails. Default 1800. |

## What `init` detects

`init` reads three files, in this order. The first file that provides a kind wins, so a `Makefile` target beats a
`package.json` script for the same kind.

| Source | Detected gates |
|---|---|
| `Makefile` (or `GNUmakefile`, `makefile`) | Targets named `lint`, `typecheck`, `build`, `test` become `make <target>`. Without `test`, a `check` target becomes the test gate. |
| `package.json` | Scripts `lint`, `typecheck` (or `type-check`), `build`, `test`, run as `<pm> run <script>`. The package manager comes from the `packageManager` field, else the lockfile (`pnpm-lock.yaml`, `yarn.lock`, `bun.lock`), else npm. The default `"no test specified"` script is skipped. |
| `pyproject.toml` | `ruff check .` if `[tool.ruff]` exists. `pytest` if `[tool.pytest]` exists, a `tests/` folder exists, or pytest is mentioned. Prefixed with `uv run` when `uv.lock` exists, `poetry run` when `poetry.lock` exists; otherwise `python3 -m pytest`. |

Anything else, such as `go test ./...`, `cargo test`, or a script in `bin/`, has to be added by hand.

## Examples

### A Node project with pnpm

```toml
max_fix_rounds = 3
block_on_severity = "high"

[[gates]]
name = "lint"
command = "pnpm run lint"
kind = "lint"
timeout = 600

[[gates]]
name = "typecheck"
command = "pnpm run typecheck"
kind = "typecheck"
timeout = 600

[[gates]]
name = "test"
command = "pnpm run test"
kind = "test"
timeout = 1800
```

### A Go service

```toml
[[gates]]
name = "vet"
command = "go vet ./..."
kind = "lint"
timeout = 600

[[gates]]
name = "test"
command = "go test ./..."
kind = "test"
timeout = 1800
```

### A stricter review

Block on `medium` findings too, and allow only two fix rounds:

```toml
max_fix_rounds = 2
block_on_severity = "medium"
```

## Tips

- **Keep a test gate.** A run without one is labelled NO TESTS everywhere. Passing lint and build gates do not show
  that the change works.
- **Make gates leave the tree alone.** A gate that writes files git does not ignore (coverage reports,
  `__pycache__/`, build output) fails the verify, because no single tree was tested. Add those paths to
  `.gitignore`.
- **Make gates non-interactive.** A gate runs without a terminal. Use the CI form of a command, such as
  `vitest run` instead of `vitest`, or `CI=1`.
- **Set timeouts above the real run time.** Also give the Bash call that runs `verify` a timeout above the gates'
  total time. If it runs longer, Claude Code moves the call to the background, where it keeps running, and the skill
  waits for it to finish.
- **Watch what gates print.** Gate output is stored as is. A gate that prints secrets stores them under
  `.supervise/`.

## Files under `.supervise/`

```
.supervise/
  config.toml                  project config (copied into each run at start)
  current                      id of the open run
  lock                         write lock
  runs/<id>/state.json         run state and history
  runs/<id>/events.jsonl       subagent starts and stops recorded by the hooks
  runs/<id>/logs/              full gate output per baseline and verify
  runs/<id>/review-N.diff      the diff each review saw
  runs/<id>/review-N-reply.md  the reviewer's reply, for a transcribed review
```

Run ids start with the local date and time, so the folders sort in start order. Finished runs can be deleted:
supervise reads only the open run, the latest run (for `commit` and `report`), and a run you name with
`report --run ID`.
