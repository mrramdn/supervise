# M0 spike findings

Date: 2026-09-23. Claude Code 2.1.280 on Linux x86_64. Nested sessions were mostly `claude -p --model haiku`
with the plugin loaded through `--plugin-dir` (S1 also through a local marketplace install; S4 also used sonnet
and claude-opus-5-5). Each spike was run once by one agent; the raw command output stayed in the session
scratchpad and is not committed, so the evidence below quotes the decisive lines.

## S1: `/supervise` through a local marketplace (partial)
- **Answer:** a local marketplace with `source: "./"` installs and the plugin loads in place from the source
  directory (edits apply at the next session; `installed_plugins.json` still points `installPath` at a stale
  cache copy). Bare `/<skill>` reaches `<plugin>:<skill>` only when nothing else owns the name.
- **Evidence:** without a collision, `/svspike hello-1b` expanded to `<command-name>/svspike:svspike</command-name>`.
  With the old `~/.claude/skills/supervise` present, `/supervise` loaded "Base directory for this skill:
  ~/.claude/skills/supervise" with no warning, while `/supervise:supervise` reached the plugin.
- **Impact:** `/supervise:supervise` is the canonical form, bare `/supervise` an alias. The old user skill must
  go before dogfooding, verified from the init skill list, not from the model's reply.

## S2: `bin/` on PATH, dynamic context, `allowed-tools` (confirmed, with caveats)
- **Answer:** `bin/` is on PATH for every Bash call (appended last, so an earlier `supervisectl` shadows it), but
  not for hook processes. The ``!`supervisectl status --brief || true` `` line runs, needs `allowed-tools` to pass
  its permission check, skips PreToolUse hooks, and gets `$ARGUMENTS`/`$0` substituted raw. `allowed-tools`
  pre-approves `supervisectl` (including `override`) for the invoking turn only.
- **Evidence:** a hook written as bare `supervisectl hook …` failed with "/bin/sh: line 1: supervisectl: command
  not found" and the tool call still ran (fail-open). `/supervise:argtest x $(supervisectl override …)` printed
  `HUMAN-CMD-EXECUTED override`. In turn 2 the same command got "Permission to use Bash has been denied".
- **Impact:** hooks.json calls `"${CLAUDE_PLUGIN_ROOT}/bin/supervisectl"` (the placeholder is substituted
  textually in SKILL.md too; `CLAUDE_PLUGIN_ROOT` is not in the Bash environment). Never put `$ARGUMENTS` in a
  dynamic-context line and keep `|| true` (a non-zero exit aborts the skill). Document adding
  `Bash(supervisectl *)` to project permissions, since the skill grant ends with the turn.

## S3: plugin agent names (confirmed)
- **Answer:** plugin agents register only as `supervise:implementer` / `supervise:code-reviewer` and coexist
  with project agents of the same bare name; unique names are not needed.
- **Evidence:** each name ran its own definition (markers `PI-4K8` vs `QI-7J2`); the reviewer's tool list sent
  to the model was exactly `['Read', 'Grep', 'Glob']`. `supervise:nope` returned "Agent type 'supervise:nope'
  not found. Available agents: …" with no fallback, but an omitted `subagent_type` silently ran
  `general-purpose`, and one haiku session handed the task to another live session with ListAgents + SendMessage.
- **Impact:** use scoped names everywhere (the Agent tool does not map bare names to plugin agents). While a run
  is active, deny Agent calls whose type is not the configured implementer/reviewer (including a missing type),
  and deny or record SendMessage. Compare the canonical `agent_type` from SubagentStart/Stop; the PreToolUse
  string is the raw model input.

## S4: subagent model and effort inheritance (confirmed)
- **Answer:** an agent without `model`/`effort` runs on the session's exact model and effort.
- **Evidence:** proxied request bodies and transcripts showed haiku → `claude-haiku-4-5-20251001`, sonnet →
  `claude-sonnet-5`, `claude-opus-5-5 --effort low` → `claude-opus-5-5` at low. The Agent tool's `model`
  parameter accepts only `sonnet|opus|haiku|fable` (`inherit` and full IDs gave InputValidationError) and has no
  effort parameter. `model: inherit` differs from omitting the field only when `CLAUDE_CODE_SUBAGENT_MODEL` is set.
- **Impact:** write `model: inherit` explicitly and omit `effort`. A profile must never pass `inherit` or a full
  model ID per invocation (omit the parameter instead), and cannot set per-agent effort.
- **Update 2026-11-08:** the Agent tool now has an `effort` parameter (low to max), so the orchestrator can set
  effort per call. See DESIGN.md, S4.

## S5: hook inputs and human-only commands (confirmed)
- **Answer:** PreToolUse sees `tool_name: "Agent"` with `tool_input.subagent_type`; SubagentStart/Stop carry
  `agent_type: "supervise:code-reviewer"` (no `plugin:` prefix) and `agent_id`, and SubagentStop adds
  `agent_transcript_path` and `last_assistant_message`. A user `!` command bypasses all tool hooks.
- **Evidence:** a PreToolUse deny beat `--allowedTools "Bash(supervisectl *)"` and `bypassPermissions`, also
  inside subagents. In a real interactive session, `! supervisectl override --max-fix-rounds 4 --reason test`
  ran (`HUMAN-CMD-EXECUTED override`) with no PreToolUse, PostToolUse or UserPromptSubmit event. `!` and model
  Bash calls both run with `CLAUDECODE=1` and no TTY. In `-p`, PostToolUse(Agent) returns `async_launched`
  before SubagentStart.
- **Impact:** the PreToolUse hook is the only enforcement point for `override`/`approve`, and it does not
  block the human. There is no TTY check in the CLI: it would reject the `!` path and a model can fake a TTY
  (this overrides S2's suggestion). Review evidence is the reviewer's SubagentStop by `agent_id` after
  `review begin`, and the spawn cap counts SubagentStart/Stop.

## S6: `claude plugin eval` (cancelled)
Dropped by the plan revision of 2026-09-23 (no eval, no benchmark).

## S7: `/code-review` as the reviewer (not needed)
The plan was simplified and this reviewer option was removed. Incidental result from S2: a slash command typed
by the user fires only UserPromptSubmit; only a Skill-tool call produces PreToolUse/PostToolUse(Skill).

## S8: hook overhead (partial)
- **Answer:** the prefilter `[ -e "$CLAUDE_PROJECT_DIR/.supervise/active" ] || exit 0` costs almost nothing over
  starting a shell, but "< 5 ms per hook" is not reachable: Claude Code adds its own dispatch cost to every
  command hook. Hooks run as `/bin/sh -c '<command>'` (dash on Debian), so the prefilter must stay POSIX.
- **Evidence:** micro p50 5.12 ms (prefilter) vs 5.07 ms (bare `sh -c :`). Inside Claude Code the prefilter
  measured p50 23.6 ms on PreToolUse and 8.6 ms on PostToolUse, so matcher `""` on both adds +52 ms to every tool
  call in every session. Exact matchers plus `if` started 0 hooks for unrelated Bash calls (+5 ms, noise) and
  still denied `supervisectl override`.
- **Impact:** ship only PreToolUse `Write|Edit|NotebookEdit` (no `if`: `Edit(...)` patterns skip Write calls),
  `Bash` with `if: "Bash(*supervisectl*)"` and `if: "Bash(*.supervise*)"`, `Agent|Task`, plus SubagentStart and
  SubagentStop. No catch-all PostToolUse, SessionStart or Stop hooks. Keep the `hook` code path import-light:
  an active-run hook costs about 140 ms because of Python startup.

## S9: `validate --strict` and SKILL.md frontmatter (confirmed)
- **Answer:** the planned frontmatter (`license`, `compatibility`, `argument-hint`, `allowed-tools`,
  `disable-model-invocation`) passes `claude plugin validate --strict`; nothing needs to move to plugin.json.
- **Evidence:** with a marketplace.json present, `validate <dir>` checks only the marketplace (`"contents": []`).
  `validate --strict .claude-plugin/plugin.json` validates skills, agents and hooks and reported broken YAML in
  SKILL.md and an agent, and invalid JSON in hooks.json. It does not flag unknown keys, a description over 1024
  characters, a name that differs from its folder, or bad `model`/`effort` values. The repo skeleton passes
  both forms.
- **Impact:** `make validate` must run both forms. The repo's own skill lint covers what `--strict` misses.
  `claude plugin details` only works as `claude --plugin-dir <dir> plugin details supervise`, and projected
  ~319 always-on tokens for the planned description, so the description needs trimming.

## Other observations
- Nested `claude` processes inherit the outer session's `CLAUDECODE` and messaging variables unless they are
  scrubbed; one spike session used them to message the orchestrating session.
- After a `!` command, Claude Code starts a model turn on its own (no UserPromptSubmit event). After a human
  `override`, the orchestrator should re-read `supervisectl status` before continuing.
