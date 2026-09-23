"""supervisectl: keeps the state of a supervise run and refuses phase changes that break its rules.

Exit codes: 0 done, 1 negative result (a gate failed, the review blocks, the run escalated),
2 refused (a rule or usage error; state unchanged), 3 error (git failed, state unreadable).
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from supervise import __version__, hooks
from supervise.gates import SEVERITIES, describe, detect, load_config, render_config, run_gates
from supervise.store import Project, Refused, State, SuperviseError, git, now, read_events, write_atomic

REVIEWER = hooks.REVIEWER
SELF_REVIEW_MAX_LINES = 10
OPEN = ("active", "paused", "escalated")
NO_TESTS = 'NO TESTS: no gate of kind "test" is configured, so passing gates do not show that the change works.'
PLAN_HINT = 'pass {"summary": "...", "subtasks": [{"title": "...", "acceptance": "..."}]}'
SEVERITY_ALIASES = {
    "blocker": "critical",
    "error": "high",
    "major": "high",
    "warning": "medium",
    "warn": "medium",
    "minor": "low",
    "nit": "info",
    "note": "info",
    "suggestion": "info",
}
_FINDINGS_START = re.compile(r'\{\s*"findings"\s*:')


# --- small helpers -----------------------------------------------------------------------------------------------


def last(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return items[-1] if items else None


def record(state: State, event: str, note: str = "") -> None:
    state["history"].append(
        {"at": now(), "event": event, "phase": state["phase"], "status": state["status"], "note": note}
    )


def short_list(items: list[str], limit: int = 10) -> str:
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def next_step(state: State) -> str:
    status, phase = state["status"], state["phase"]
    if status == "paused":
        return "`supervisectl resume`"
    if status == "escalated":
        return (
            "stop and tell the user; only they can continue, by typing "
            '`! supervisectl override --max-fix-rounds N --reason "..."` (or ask whether to abort the run)'
        )
    if status in ("accepted", "aborted"):
        return '`supervisectl start "<task>"` for new work'
    verify, review = last(state["verifies"]), last(state["reviews"])
    if phase == "PLAN":
        return "write the plan, then `supervisectl plan set -` with the plan JSON on stdin"
    if phase in ("IMPLEMENT", "FIX"):
        return "finish the changes, then `supervisectl verify`"
    if phase == "VERIFY":
        if verify and verify["result"] == "pass":
            return f"`supervisectl review begin`, then run the {REVIEWER} agent with the brief it prints"
        if verify and not failed_gates(verify):
            return (
                "make the gates leave the working tree alone (e.g. ignore generated files), then `supervisectl verify`"
            )
        return "`supervisectl fix` before changing code, then repair what failed"
    if phase == "REVIEW" and review:
        if review["state"] != "recorded":
            return f"run the {REVIEWER} agent with the brief from `review begin`, then `supervisectl review record`"
        if review["verdict"] == "clean":
            return "`supervisectl accept`"
        return "`supervisectl fix`, then address the blocking findings"
    return "`supervisectl status`"


def failed_gates(verify: dict[str, Any]) -> list[dict[str, Any]]:
    return [g for g in verify["gates"] if g["status"] == "fail"]


def needs_fix(state: State) -> bool:
    """The last result is negative, so the next repair has to start with `supervisectl fix`."""
    verify, review = last(state["verifies"]), last(state["reviews"])
    if state["phase"] == "VERIFY":
        return bool(verify and failed_gates(verify))
    return bool(
        state["phase"] == "REVIEW" and review and review["state"] == "recorded" and review["verdict"] == "blocking"
    )


def open_run(project: Project, *, active: bool = True) -> State:
    """The run `current` points at. With `active`, refuse paused and escalated runs."""
    run_id = project.current_id()
    state = project.load(run_id) if run_id else None
    if state is None or state["status"] not in OPEN:
        raise Refused("there is no open supervise run", "", '`supervisectl start "<task>"`')
    if active and state["status"] == "paused":
        raise Refused(f"run {run_id} is paused", "", next_step(state))
    if active and state["status"] == "escalated":
        raise Refused(
            f"run {run_id} is escalated after {state['fix_round']} fix rounds",
            "Flow commands wait until the user raises the cap or drops the run.",
            next_step(state),
        )
    return state


def require_phase(state: State, command: str, *phases: str) -> None:
    if state["phase"] not in phases:
        raise Refused(
            f"`{command}` is not allowed in phase {state['phase']}",
            f"It is allowed in: {', '.join(phases)}.",
            next_step(state),
        )


def read_json_input(source: str, what: str, hint: str) -> Any:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise Refused(f"cannot read the {what}", str(exc), hint) from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise Refused(f"the {what} is not valid JSON", str(exc), hint) from None


def new_subtask(sid: str, title: str, acceptance: str, origin: str) -> dict[str, Any]:
    return {"id": sid, "title": title, "acceptance": acceptance, "origin": origin, "status": "open", "note": ""}


def normalize_finding(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {"summary": str(raw)}
    severity = str(raw.get("severity") or "").strip().lower()
    severity = SEVERITY_ALIASES.get(severity, severity)
    line = raw.get("line")
    return {
        "file": str(raw.get("file") or ""),
        "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
        # An unknown severity blocks rather than slipping through.
        "severity": severity if severity in SEVERITIES else "high",
        "summary": str(raw.get("summary") or raw.get("message") or "").strip(),
    }


def parse_findings(text: str) -> list[dict[str, Any]] | None:
    """The last `{"findings": [...]}` object in a reviewer reply, fenced or not; None when there is none."""
    decoder = json.JSONDecoder()
    for match in reversed(list(_FINDINGS_START.finditer(text))):
        try:
            data, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            return [normalize_finding(item) for item in data["findings"]]
    return None


def finding_line(finding: dict[str, Any]) -> str:
    where = finding["file"] + (f":{finding['line']}" if finding["line"] is not None else "")
    return f"{finding['severity']:<8} {where or '(no file)'}: {finding['summary']}"


def is_blocking(state: State, finding: dict[str, Any]) -> bool:
    return SEVERITIES.index(finding["severity"]) >= SEVERITIES.index(state["block_on_severity"])


def verify_line(verify: dict[str, Any]) -> str:
    gates = ", ".join(f"{g['name']} {g['status']}" for g in verify["gates"]) or "no gates"
    changed = "; the gates changed the working tree" if verify["problem"] else ""
    return f"Verify #{verify['n']}: {verify['result']} ({gates}{changed})"


def baseline_note(verify: dict[str, Any]) -> str | None:
    same = [g["name"] for g in verify["gates"] if g["status"] == "baseline"]
    if not same:
        return None
    return f"Note: {', '.join(same)} failed before the run and fail the same way now, so they say nothing about it."


def review_line(review: dict[str, Any]) -> str:
    if review["state"] == "begun":
        return f"Review #{review['n']}: waiting for the reviewer (diff: {review['diff']})"
    if review["state"] == "abandoned":
        return f"Review #{review['n']}: abandoned"
    who = "independent" if review["reviewer"] == "independent" else f"self-review ({review['reason']})"
    other = len(review["findings"]) - review["blocking"]
    return f"Review #{review['n']}: {review['verdict']}, {who}; {review['blocking']} blocking, {other} non-blocking"


def ensure_excluded(project: Project) -> bool:
    path = Path(git(project.root, "rev-parse", "--git-path", "info/exclude").strip())
    if not path.is_absolute():
        path = project.root / path
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(line.strip() in ("/.supervise/", ".supervise/", "/.supervise", ".supervise") for line in text.splitlines()):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}# supervise run state\n/.supervise/\n", encoding="utf-8")
    return True


def gate_lines(gates: list[dict[str, Any]]) -> list[str]:
    return [f"  {g['name']:<10} {g['kind']:<9} {g['command']}" for g in gates] or ["  (none)"]


def summary_lines(project: Project, state: State) -> list[str]:
    lines = [
        f"Run {state['run_id']}: {state['task']}",
        f"Status: {state['status']}, phase {state['phase']}, fix round {state['fix_round']}/{state['max_fix_rounds']}",
    ]
    if state["no_tests"]:
        lines.append(NO_TESTS)
    subtasks = state["subtasks"]
    if subtasks:
        done = sum(s["status"] == "done" for s in subtasks)
        lines.append(f"Subtasks: {done}/{len(subtasks)} done")
        for s in subtasks:
            origin = " (from the user)" if s["origin"] == "user" else ""
            lines.append(f"  [{'x' if s['status'] == 'done' else ' '}] {s['id']} {s['title']}{origin}")
    verify, review = last(state["verifies"]), last(state["reviews"])
    if verify:
        lines.append(verify_line(verify))
        if verify["problem"]:
            lines.append(f"  {verify['problem']}")
    if review:
        lines.append(review_line(review))
    step = next_step(state)
    if state["status"] == "active" and state["phase"] in ("VERIFY", "REVIEW") and verify:
        ref, label = (review, f"review #{review['n']}") if state["phase"] == "REVIEW" and review else (verify, "verify")
        same = project.fingerprint() == ref["tree"]
        lines.append(f"Working tree: {'unchanged' if same else 'CHANGED'} since {label}")
        if not same and not needs_fix(state):
            step = f"`supervisectl verify` (files changed since {label})"
    lines.append(f"next: {step}")
    return lines


# --- setup -------------------------------------------------------------------------------------------------------


def cmd_init(_args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("init"):
        created = not project.config_path.exists()
        if created:
            write_atomic(project.config_path, render_config(detect(project.root)))
        ensure_excluded(project)
        config = load_config(project.config_path)
    head = "Initialized" if created else "Already initialized; config left unchanged:"
    lines = [
        f"{head} supervise in {project.root}",
        f"Config: {project.config_path} (.supervise/ is excluded from git through .git/info/exclude)",
        "Gates:",
        *gate_lines(config["gates"]),
    ]
    if not any(g["kind"] == "test" for g in config["gates"]):
        lines.append(NO_TESTS)
    lines += [
        "Check the gates with the user and edit the config if they are wrong; each run copies them at start.",
        'next: `supervisectl start "<task>"`',
    ]
    print("\n".join(lines))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    task = args.task.strip()
    if not task:
        raise Refused("the task is empty", "", '`supervisectl start "<what to do>"`')
    project = Project.find()
    if not project.config_path.exists():
        raise Refused("supervise is not initialized in this repository", "", "`supervisectl init`")
    config = load_config(project.config_path)
    with project.lock("start"):
        open_id = project.current_id()
        if open_id:
            other = project.load(open_id)
            if other["status"] in OPEN:
                raise Refused(
                    f"run {open_id} is still open ({other['status']}, phase {other['phase']})",
                    f"Task: {other['task']}",
                    'finish it (`supervisectl status`), or `supervisectl abort --reason "..."` if the user drops it',
                )
        run_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        run_dir = project.run_dir(run_id)
        run_dir.mkdir(parents=True)
        tree, head = project.fingerprint(), project.head()
        dirty = head is None or tree != git(project.root, "rev-parse", f"{head}^{{tree}}").strip()
        print(f"Starting run {run_id}: {len(config['gates'])} baseline gate(s).", flush=True)
        baseline = run_gates(project.root, config["gates"], run_dir / "logs", "baseline", None)
        state: State = {
            "schema": 1,
            "run_id": run_id,
            "task": task,
            "status": "active",
            "phase": "PLAN",
            "created_at": now(),
            "updated_at": now(),
            "start": {"tree": tree, "head": head, "dirty": dirty},
            "gates": config["gates"],
            "block_on_severity": config["block_on_severity"],
            "max_fix_rounds": config["max_fix_rounds"],
            "fix_round": 0,
            "no_tests": not any(g["kind"] == "test" for g in config["gates"]),
            "baseline": baseline,
            "plan": None,
            "subtasks": [],
            "verifies": [],
            "reviews": [],
            "overrides": [],
            "history": [],
        }
        record(state, "start", task)
        project.save(state)
        project.set_current(run_id)
    lines = [f"Run {run_id} started: {task}", "Baseline (before any change):"]
    lines += [f"  {describe(g)}" for g in baseline] or ["  (no gates)"]
    if any(g["status"] == "fail" for g in baseline):
        lines.append("A gate that already fails counts as unchanged at verify only if its output stays identical.")
    if dirty:
        lines.append(
            "The working tree already differs from HEAD; those changes are part of the baseline, not the review."
        )
    if state["no_tests"]:
        lines.append(NO_TESTS)
    lines.append(f"next: {next_step(state)}")
    print("\n".join(lines))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    project = Project.find()
    if not project.config_path.exists():
        print(f"supervise is not initialized in {project.root}.\nnext: `supervisectl init`")
        return 0
    run_id = project.current_id()
    state = project.load(run_id) if run_id else None
    if state is None or state["status"] not in OPEN:
        lines = ["No open supervise run."]
        latest = project.latest_run_id()
        if latest:
            old = project.load(latest)
            lines.append(f"Last run: {latest} ({old['status']}): {old['task']}")
        lines.append('next: `supervisectl start "<task>"`')
        print("\n".join(lines))
        return 0
    print("\n".join(summary_lines(project, state)))
    return 0


# --- flow --------------------------------------------------------------------------------------------------------


def cmd_plan_set(args: argparse.Namespace) -> int:
    data = read_json_input(args.file, "plan", PLAN_HINT)
    if isinstance(data, list):
        data = {"subtasks": data}
    if not isinstance(data, dict) or not isinstance(data.get("subtasks"), list) or not data["subtasks"]:
        raise Refused("the plan needs a non-empty subtasks list", "", PLAN_HINT)
    subtasks = []
    for i, raw in enumerate(data["subtasks"], 1):
        item = {"title": raw} if isinstance(raw, str) else raw
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            raise Refused(f"subtask {i} has no title", "", PLAN_HINT)
        acceptance = str(item.get("acceptance") or "").strip()
        subtasks.append(new_subtask(f"s{i}", str(item["title"]).strip(), acceptance, "plan"))
    project = Project.find()
    with project.lock("plan set"):
        state = open_run(project)
        require_phase(state, "plan set", "PLAN")
        state["plan"] = {"summary": str(data.get("summary") or "").strip()}
        state["subtasks"] = subtasks
        state["phase"] = "IMPLEMENT"
        record(state, "plan", f"{len(subtasks)} subtasks")
        project.save(state)
    lines = [f"Plan recorded: {len(subtasks)} subtask(s).", *(f"  {s['id']} {s['title']}" for s in subtasks)]
    print("\n".join([*lines, f"next: {next_step(state)}"]))
    return 0


def cmd_subtask_add(args: argparse.Namespace) -> int:
    title = args.title.strip()
    if not title:
        raise Refused("the subtask title is empty", "", '`supervisectl subtask add "<title>"`')
    project = Project.find()
    with project.lock("subtask add"):
        state = open_run(project)
        require_phase(state, "subtask add", "IMPLEMENT", "FIX", "VERIFY", "REVIEW")
        back = state["phase"] in ("VERIFY", "REVIEW")
        if back and args.origin != "user":
            raise Refused(
                "new work after verify goes through `supervisectl fix`",
                "In VERIFY and REVIEW, `subtask add` is only for a new request from the user (--origin user).",
                "`supervisectl fix` (counts a fix round), or --origin user if the user asked for this",
            )
        sid = f"s{len(state['subtasks']) + 1}"
        state["subtasks"].append(new_subtask(sid, title, (args.acceptance or "").strip(), args.origin))
        if back:
            state["phase"] = "IMPLEMENT"
        note = f"{sid} ({args.origin}): {title}" + ("; back to IMPLEMENT, fix round unchanged" if back else "")
        record(state, "subtask add", note)
        project.save(state)
    back_note = " The run is back in IMPLEMENT; the fix round count is unchanged." if back else ""
    print(f"Added subtask {sid}: {title}.{back_note}\nnext: {next_step(state)}")
    return 0


def cmd_subtask_done(args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("subtask done"):
        state = open_run(project)
        require_phase(state, "subtask done", "IMPLEMENT", "FIX", "VERIFY", "REVIEW")
        subtask = next((s for s in state["subtasks"] if s["id"] == args.id), None)
        if subtask is None:
            known = ", ".join(s["id"] for s in state["subtasks"]) or "none"
            raise Refused(f"there is no subtask {args.id}", f"Known subtasks: {known}.", "`supervisectl status`")
        subtask["status"], subtask["note"] = "done", (args.note or "").strip()
        record(state, "subtask done", f"{subtask['id']}: {subtask['title']}")
        project.save(state)
    left = [s["id"] for s in state["subtasks"] if s["status"] != "done"]
    print(f"Subtask {subtask['id']} done. Open: {', '.join(left) or 'none'}.")
    return 0


def cmd_verify(_args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("verify"):
        state = open_run(project)
        require_phase(state, "verify", "IMPLEMENT", "FIX", "VERIFY", "REVIEW")
        previous, review = last(state["verifies"]), last(state["reviews"])
        tree = project.fingerprint()
        if state["phase"] == "VERIFY" and previous and failed_gates(previous) and tree != previous["tree"]:
            raise Refused(
                "files changed after a failed verify without `supervisectl fix`",
                "Each repair after a failed gate is a fix round.",
                "`supervisectl fix`, then `supervisectl verify`",
            )
        if state["phase"] == "REVIEW" and review and review["state"] == "recorded" and review["verdict"] == "blocking":
            raise Refused(
                "the last review has blocking findings",
                "They are repaired in a fix round, not by verifying again.",
                "`supervisectl fix`, then address the findings",
            )
        n = len(state["verifies"]) + 1
        print(f"Verify #{n}: {len(state['gates'])} gate(s).", flush=True)
        log_dir = project.run_dir(state["run_id"]) / "logs"
        results = run_gates(project.root, state["gates"], log_dir, f"verify-{n}", state["baseline"])
        after = project.fingerprint()
        problem = None
        if after != tree:
            changed = project.changed_files(tree, after)
            problem = f"the gates changed the working tree, so no single tree was verified: {short_list(changed)}"
        passed = problem is None and all(g["status"] != "fail" for g in results)
        if review and review["state"] == "begun":
            review["state"] = "abandoned"
        verify = {
            "n": n,
            "at": now(),
            "result": "pass" if passed else "fail",
            "tree": after,
            "gates": results,
            "problem": problem,
        }
        state["verifies"].append(verify)
        state["phase"] = "VERIFY"
        record(state, "verify", verify_line(verify))
        project.save(state)
    lines = [verify_line(verify)]
    for gate in results:
        if gate["status"] != "pass":
            lines.append(f"  {describe(gate)}")
        if gate["status"] == "fail":
            lines += [f"    | {line}" for line in gate["tail"][-20:]]
            lines.append(f"    full output: {gate['log']}")
    if problem:
        lines.append(f"  {problem}")
    lines += [note] if (note := baseline_note(verify)) else []
    if state["no_tests"]:
        lines.append(NO_TESTS)
    lines.append(f"next: {next_step(state)}")
    print("\n".join(lines))
    return 0 if passed else 1


def cmd_review_begin(_args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("review begin"):
        state = open_run(project)
        require_phase(state, "review begin", "VERIFY", "REVIEW")
        verify, review = last(state["verifies"]), last(state["reviews"])
        if state["phase"] == "REVIEW" and review and review["state"] == "recorded":
            raise Refused(f"review #{review['n']} is already recorded", "", next_step(state))
        if not verify or verify["result"] != "pass":
            raise Refused("review needs a passing verify first", "", next_step(state))
        tree = project.fingerprint()
        if tree != verify["tree"]:
            changed = project.changed_files(verify["tree"], tree)
            raise Refused(
                "files changed since the last verify", f"Changed: {short_list(changed)}.", "`supervisectl verify`"
            )
        if review and review["state"] == "begun":
            review["state"] = "abandoned"
        n = len(state["reviews"]) + 1
        run_dir = project.run_dir(state["run_id"])
        start = state["start"]["tree"]
        diff = run_dir / f"review-{n}.diff"
        project.write_diff(start, tree, diff)
        independent = [r for r in state["reviews"] if r["state"] == "recorded" and r["reviewer"] == "independent"]
        since = independent[-1]["tree"] if independent else start
        lines_since, binary_since = project.diff_size(since, tree)
        all_files, new_files = project.changed_files(start, tree), project.changed_files(since, tree)
        state["reviews"].append(
            {
                "n": n,
                "at": now(),
                "state": "begun",
                "tree": tree,
                "diff": str(diff),
                "events_offset": len(read_events(run_dir)),
                "lines_since": lines_since,
                "binary_since": binary_since,
            }
        )
        state["phase"] = "REVIEW"
        record(state, "review begin", f"#{n}: {len(all_files)} files changed since start")
        project.save(state)
    small = lines_since <= SELF_REVIEW_MAX_LINES and not binary_since
    lines = [
        f"Review #{n} begun. Diff since the run started ({len(all_files)} files): {diff}",
        f"Since the last independent review: {len(new_files)} files, {lines_since} lines"
        + (f", {binary_since} binary" if binary_since else ""),
        *(f"  {f}" for f in new_files[:20]),
        f"Self-review allowed: {'yes' if small else 'no'} (limit {SELF_REVIEW_MAX_LINES} lines, no binary files)",
        "",
        f"Brief for the {REVIEWER} agent:",
        f"  Task: {state['task']}",
    ]
    for s in state["subtasks"]:
        lines.append(f"  - {s['id']} {s['title']}" + (f" (acceptance: {s['acceptance']})" if s["acceptance"] else ""))
    lines.append(f"  Diff to review: {diff}")
    if independent:
        lines.append("  A previous review exists; check the files changed since then first, then the whole diff.")
    lines += ['  End the reply with the findings JSON: {"findings": [...]}.', "", f"next: {next_step(state)}"]
    print("\n".join(lines))
    return 0


def cmd_review_record(args: argparse.Namespace) -> int:
    reason = (args.reason or "").strip()
    if args.self_review and not reason:
        raise Refused("--self needs --reason", "", '`supervisectl review record --self --reason "<why>"`')
    self_findings: list[dict[str, Any]] = []
    if args.self_review and args.findings:
        data = read_json_input(args.findings, "findings", 'pass {"findings": [...]}')
        items = data.get("findings") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise Refused("the findings need a list", "", 'pass {"findings": [...]}')
        self_findings = [normalize_finding(item) for item in items]
    project = Project.find()
    with project.lock("review record"):
        state = open_run(project)
        require_phase(state, "review record", "REVIEW")
        review = last(state["reviews"])
        if not review or review["state"] != "begun":
            raise Refused("there is no pending review", "", next_step(state))
        if project.fingerprint() != review["tree"]:
            raise Refused(
                "files changed since `review begin`",
                "The reviewer saw a different tree.",
                "`supervisectl verify`, then `supervisectl review begin` again",
            )
        if args.self_review:
            if review["lines_since"] > SELF_REVIEW_MAX_LINES or review["binary_since"]:
                binary = f" and {review['binary_since']} binary file(s)" if review["binary_since"] else ""
                raise Refused(
                    "self-review is only allowed for small diffs",
                    f"{review['lines_since']} lines{binary} changed since the last independent review; "
                    f"the limit is {SELF_REVIEW_MAX_LINES} lines and no binary files.",
                    f"run the {REVIEWER} agent with the brief from `review begin`, then `supervisectl review record`",
                )
            findings, reviewer, agent_ids = self_findings, "self", []
        else:
            events = read_events(project.run_dir(state["run_id"]))[review["events_offset"] :]
            stops = [e for e in events if e.get("event") == "subagent_stop"]
            mine = [e for e in stops if e.get("agent_type") == REVIEWER]
            if not mine:
                seen = sorted({str(e.get("agent_type")) for e in stops})
                raise Refused(
                    f"no result from {REVIEWER} since `review begin`",
                    f"A review counts as independent only when a {REVIEWER} subagent finishes after `review begin`"
                    " (the SubagentStop hook records it)." + (f" Seen instead: {', '.join(seen)}." if seen else ""),
                    f'run the Agent tool with subagent_type "{REVIEWER}" and the brief from `review begin`, then '
                    f"this command again (a diff of at most {SELF_REVIEW_MAX_LINES} lines may use --self --reason)",
                )
            findings = []
            for event in mine:
                parsed = parse_findings(str(event.get("message") or ""))
                if parsed is None:
                    raise Refused(
                        f"the {REVIEWER} reply has no findings JSON",
                        f'Agent {event.get("agent_id")} ended without a {{"findings": [...]}} block.',
                        "`supervisectl review begin` again, then re-run the reviewer and ask it to end with the JSON",
                    )
                findings += parsed
            reviewer, agent_ids = "independent", [e.get("agent_id") for e in mine]
        blocking = [f for f in findings if is_blocking(state, f)]
        review.update(
            {
                "state": "recorded",
                "recorded_at": now(),
                "reviewer": reviewer,
                "agent_ids": agent_ids,
                "reason": reason if args.self_review else "",
                "findings": findings,
                "blocking": len(blocking),
                "verdict": "blocking" if blocking else "clean",
            }
        )
        record(state, "review record", review_line(review))
        project.save(state)
    lines = [review_line(review)]
    lines += [f"  BLOCKING {finding_line(f)}" for f in blocking]
    lines += [f"  {finding_line(f)}" for f in findings if not is_blocking(state, f)]
    lines.append(f"next: {next_step(state)}")
    print("\n".join(lines))
    return 1 if blocking else 0


def open_items(state: State) -> list[str]:
    """What the last negative result asks to repair."""
    verify, review = last(state["verifies"]), last(state["reviews"])
    items: list[str] = []
    if verify and verify["result"] == "fail":
        items += [f"gate {g['name']} failed: {g['command']} (log: {g['log']})" for g in failed_gates(verify)]
        if verify["problem"]:
            items.append(verify["problem"])
    if review and review["state"] == "recorded" and review["tree"] == (verify or {}).get("tree"):
        items += [finding_line(f) for f in review["findings"] if is_blocking(state, f)]
    return items


def cmd_fix(_args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("fix"):
        state = open_run(project)
        require_phase(state, "fix", "VERIFY", "REVIEW")
        items = open_items(state)
        if state["fix_round"] >= state["max_fix_rounds"]:
            state["status"] = "escalated"
            record(state, "escalate", f"all {state['max_fix_rounds']} fix rounds used")
            project.save(state)
            lines = [
                f"Run {state['run_id']} is escalated: all {state['max_fix_rounds']} fix rounds are used.",
                *(f"  still open: {item}" for item in items),
                "Stop here and tell the user what is still failing. Only the user can continue the run, by typing:",
                '  ! supervisectl override --max-fix-rounds <N> --reason "<why>"',
                'or they can drop it with `supervisectl abort --reason "..."`.',
            ]
            print("\n".join(lines))
            return 1
        review = last(state["reviews"])
        if review and review["state"] == "begun":
            review["state"] = "abandoned"
        state["fix_round"] += 1
        state["phase"] = "FIX"
        record(state, "fix", f"round {state['fix_round']}/{state['max_fix_rounds']}")
        project.save(state)
    lines = [f"Fix round {state['fix_round']}/{state['max_fix_rounds']}.", *(f"  to fix: {item}" for item in items)]
    print("\n".join([*lines, f"next: {next_step(state)}"]))
    return 0


def cmd_accept(_args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("accept"):
        state = open_run(project)
        verify, review = last(state["verifies"]), last(state["reviews"])
        if state["phase"] != "REVIEW" or not review or review["state"] != "recorded":
            raise Refused(
                "accept needs a passing verify and a recorded clean review of the current tree",
                f"The run is in phase {state['phase']}.",
                next_step(state),
            )
        if review["verdict"] != "clean":
            raise Refused(
                f"review #{review['n']} has blocking findings",
                f"{review['blocking']} finding(s) at or above {state['block_on_severity']}.",
                next_step(state),
            )
        if not verify or verify["result"] != "pass" or verify["tree"] != review["tree"]:
            raise Refused("the review does not match a passing verify", "", "`supervisectl verify`, then review again")
        tree = project.fingerprint()
        if tree != review["tree"]:
            changed = project.changed_files(review["tree"], tree)
            raise Refused(
                "files changed since the review",
                f"The working tree no longer matches what was verified and reviewed. Changed: {short_list(changed)}.",
                "`supervisectl verify`, then `supervisectl review begin` and review again",
            )
        state["status"], state["phase"] = "accepted", "ACCEPT"
        record(state, "accept", f"verify #{verify['n']}, review #{review['n']}")
        project.save(state)
        project.set_current(None)
    lines = [
        f"ACCEPTED run {state['run_id']}: {state['task']}",
        verify_line(verify),
        review_line(review),
        f"Fix rounds used: {state['fix_round']}/{state['max_fix_rounds']}",
    ]
    lines += [note] if (note := baseline_note(verify)) else []
    if state["no_tests"]:
        lines.append(NO_TESTS)
    left = [f"{s['id']} {s['title']}" for s in state["subtasks"] if s["status"] != "done"]
    if left:
        lines.append(f"Not marked done: {'; '.join(left)}")
    lines.append("Full report: `supervisectl report`")
    print("\n".join(lines))
    return 0


# --- control -----------------------------------------------------------------------------------------------------


def cmd_pause(args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("pause"):
        state = open_run(project, active=False)
        if state["status"] != "active":
            raise Refused(f"only an active run can be paused; this one is {state['status']}", "", next_step(state))
        state["status"] = "paused"
        record(state, "pause", (args.reason or "").strip())
        project.save(state)
    print(
        f"Run {state['run_id']} paused in phase {state['phase']}. Any session can continue it: `supervisectl resume`."
    )
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("resume"):
        state = open_run(project, active=False)
        if state["status"] != "paused":
            raise Refused(f"run {state['run_id']} is not paused (it is {state['status']})", "", next_step(state))
        state["status"] = "active"
        record(state, "resume")
        project.save(state)
    print("\n".join(["Resumed.", *summary_lines(project, state)]))
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    project = Project.find()
    with project.lock("abort"):
        state = open_run(project, active=False)
        state["status"] = "aborted"
        record(state, "abort", (args.reason or "").strip())
        project.save(state)
        project.set_current(None)
    print(f"Run {state['run_id']} aborted in phase {state['phase']}. The working tree was left as it is.")
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    reason = args.reason.strip()
    if not reason:
        raise Refused("override needs a reason", "", '`--reason "<why>"`')
    project = Project.find()
    with project.lock("override"):
        state = open_run(project, active=False)
        if args.max_fix_rounds <= state["fix_round"]:
            raise Refused(
                "the new cap must be above the fix rounds already used",
                f"{state['fix_round']} used; asked for {args.max_fix_rounds}.",
                f"--max-fix-rounds {state['fix_round'] + 1} or more",
            )
        old = state["max_fix_rounds"]
        state["max_fix_rounds"] = args.max_fix_rounds
        if state["status"] == "escalated":
            state["status"] = "active"
        state["overrides"].append({"at": now(), "from": old, "to": args.max_fix_rounds, "reason": reason})
        record(state, "override", f"max_fix_rounds {old} -> {args.max_fix_rounds}: {reason}")
        project.save(state)
    print(
        f"max_fix_rounds raised from {old} to {args.max_fix_rounds} for run {state['run_id']}; "
        f"status {state['status']}.\nnext: {next_step(state)}"
    )
    return 0


# --- report ------------------------------------------------------------------------------------------------------


def render_report(state: State, events: list[dict[str, Any]]) -> str:
    out = [
        f"# supervise report: run {state['run_id']}",
        "",
        f"- Task: {state['task']}",
        f"- Status: **{state['status']}** (phase {state['phase']})",
        f"- Started {state['created_at']}; last change {state['updated_at']}",
        f"- Fix rounds: {state['fix_round']}/{state['max_fix_rounds']}",
    ]
    if state["no_tests"]:
        out += ["", f"**{NO_TESTS}**"]
    if state["plan"] and state["plan"]["summary"]:
        out += ["", "## Plan", "", state["plan"]["summary"]]
    if state["subtasks"]:
        out += ["", "## Subtasks", ""]
        for s in state["subtasks"]:
            done = s["status"] == "done"
            extra = (f" (acceptance: {s['acceptance']})" if s["acceptance"] else "") + (
                " (added by the user)" if s["origin"] == "user" else ""
            )
            out.append(f"- [{'x' if done else ' '}] {s['id']} {s['title']}{extra}{'' if done else ' **not done**'}")
    verify = last(state["verifies"])
    out += ["", "## Gates", ""]
    if state["gates"]:
        latest = {g["name"]: g for g in verify["gates"]} if verify else {}
        base = {g["name"]: g for g in state["baseline"]}
        out += ["| gate | kind | command | baseline | last verify |", "|---|---|---|---|---|"]
        for gate in state["gates"]:
            b, v = base.get(gate["name"]), latest.get(gate["name"])
            cell_b = f"{b['status']} (exit {b['exit_code']})" if b else "-"
            cell_v = f"{v['status']} (exit {v['exit_code']})" if v else "not run"
            out.append(f"| {gate['name']} | {gate['kind']} | `{gate['command']}` | {cell_b} | {cell_v} |")
        for g in (verify or {}).get("gates", []):
            if g["status"] != "pass":
                out += ["", f"Last output of `{g['name']}` ({g['status']}):", "", "```", *g["tail"], "```"]
    else:
        out.append("No gates are configured.")
    if verify:
        out += ["", verify_line(verify)]
        out += [note] if (note := baseline_note(verify)) else []
    if state["reviews"]:
        out += ["", "## Reviews", ""]
        out += [f"- {review_line(r)}" for r in state["reviews"]]
        recorded = [r for r in state["reviews"] if r["state"] == "recorded"]
        if recorded and recorded[-1]["findings"]:
            out += ["", f"Findings of review #{recorded[-1]['n']}:", ""]
            out += [
                f"- {'**blocking** ' if is_blocking(state, f) else ''}{finding_line(f)}"
                for f in recorded[-1]["findings"]
            ]
    if state["overrides"]:
        out += ["", "## Overrides by the user", ""]
        out += [f"- {o['at']}: max_fix_rounds {o['from']} -> {o['to']}: {o['reason']}" for o in state["overrides"]]
    agents = Counter(str(e.get("agent_type")) for e in events if e.get("event") == "subagent_stop")
    if agents:
        out += ["", "## Subagents", ""]
        out += [f"- {name}: {count} finished" for name, count in sorted(agents.items())]
    out += ["", "## History", ""]
    out += [
        f"- {h['at']} {h['event']} ({h['phase']}, {h['status']})" + (f": {h['note']}" if h["note"] else "")
        for h in state["history"]
    ]
    return "\n".join(out)


def cmd_report(args: argparse.Namespace) -> int:
    project = Project.find()
    run_id = args.run or project.current_id() or project.latest_run_id()
    if not run_id or not (project.run_dir(run_id) / "state.json").is_file():
        raise Refused(
            "no such supervise run" if args.run else "there are no supervise runs here", "", "`supervisectl status`"
        )
    print(render_report(project.load(run_id), read_events(project.run_dir(run_id))))
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    return hooks.main(["hook", args.event])


# --- entry -------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supervisectl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"supervisectl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(group: Any, name: str, func: Any, text: str) -> argparse.ArgumentParser:
        p = group.add_parser(name, help=text, description=text)
        p.set_defaults(func=func)
        return p

    add(sub, "init", cmd_init, "detect gates, write .supervise/config.toml, exclude .supervise/ from git")
    add(sub, "start", cmd_start, "start a run and record the baseline gate results").add_argument("task")
    add(sub, "status", cmd_status, "show the open run and the next step")
    plan = sub.add_parser("plan", help="record the plan").add_subparsers(dest="plan_command", required=True)
    add(plan, "set", cmd_plan_set, "record the plan (JSON file, or - for stdin); PLAN -> IMPLEMENT").add_argument(
        "file"
    )
    subtask = sub.add_parser("subtask", help="add or finish subtasks").add_subparsers(dest="sub_command", required=True)
    p = add(subtask, "add", cmd_subtask_add, "add a subtask; --origin user sends VERIFY/REVIEW back to IMPLEMENT")
    p.add_argument("title")
    p.add_argument("--acceptance")
    p.add_argument("--origin", choices=("plan", "user"), default="plan")
    p = add(subtask, "done", cmd_subtask_done, "mark a subtask done")
    p.add_argument("id")
    p.add_argument("--note")
    add(sub, "verify", cmd_verify, "run the gates and compare failures with the baseline")
    review = sub.add_parser("review", help="begin or record a review").add_subparsers(
        dest="review_command", required=True
    )
    add(review, "begin", cmd_review_begin, "write the diff and the reviewer brief; VERIFY -> REVIEW")
    p = add(review, "record", cmd_review_record, "record the code-reviewer's findings (or a small self-review)")
    p.add_argument("--self", dest="self_review", action="store_true", help="orchestrator review of a small diff")
    p.add_argument("--reason", help="why a self-review is enough (required with --self)")
    p.add_argument("--findings", help="self-review findings JSON file, or - for stdin")
    add(sub, "fix", cmd_fix, "start a fix round after a failed verify or a review")
    add(sub, "accept", cmd_accept, "accept the run: passing verify and clean review of the current tree")
    add(sub, "report", cmd_report, "print the run report (markdown)").add_argument("--run", help="run id")
    add(sub, "pause", cmd_pause, "pause the run").add_argument("--reason")
    add(sub, "resume", cmd_resume, "resume a paused run")
    add(sub, "abort", cmd_abort, "drop the run").add_argument("--reason")
    p = add(sub, "override", cmd_override, "raise the fix round cap (for the user only)")
    p.add_argument("--max-fix-rounds", type=int, required=True)
    p.add_argument("--reason", required=True)
    add(sub, "hook", cmd_hook, "internal: hook entry point").add_argument("event")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Refused as exc:
        lines = [f"supervisectl: refused: {exc.rule}"]
        if exc.detail:
            lines.append(f"  {exc.detail}")
        if exc.next_step:
            lines.append(f"  next: {exc.next_step}")
        print("\n".join(lines), file=sys.stderr)
        return 2
    except SuperviseError as exc:
        print(f"supervisectl: error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
