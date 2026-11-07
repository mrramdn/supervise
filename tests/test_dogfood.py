"""One scenario per finding from dogfooding v0.1, each running the real CLI in a temporary git repository."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from conftest import BIN, REVIEWER, TEST_GATE, Project


def reached_review(project: Project, *, max_fix_rounds: int = 3) -> None:
    project.configure(TEST_GATE, max_fix_rounds=max_fix_rounds)
    project.start()
    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.ok("review", "begin")


def events_path(project: Project) -> Path:
    return project.state_path().parent / "events.jsonl"


def test_reviewer_reply_without_json_is_transcribed_instead_of_lost(project: Project) -> None:
    reached_review(project)
    prose = "One problem: app.txt line 1 says v2 but the task wants v3. That is a high severity bug."
    project.subagent_stop(REVIEWER, prose)
    refused = project.refused("review", "record")
    assert "no findings JSON" in refused.stderr
    assert "review record --findings .supervise/findings.json" in refused.stderr

    # The transcription goes through the file the message names; writing it leaves the tree fingerprint alone.
    transcript = {"findings": [{"file": "app.txt", "line": 1, "severity": "high", "summary": "says v2, not v3"}]}
    project.write(".supervise/findings.json", json.dumps(transcript))
    result = project.run("review", "record", "--findings", ".supervise/findings.json")
    assert result.returncode == 1, result.stderr  # the transcribed high finding blocks like any other
    assert "Read and removed .supervise/findings.json." in result.stdout
    assert not (project.root / ".supervise" / "findings.json").exists()
    assert "independent, findings transcribed by the orchestrator" in result.stdout
    review = project.state()["reviews"][-1]
    assert (review["reviewer"], review["transcribed"], review["blocking"]) == ("independent", True, 1)
    assert Path(review["reply"]).read_text(encoding="utf-8") == prose
    report = project.ok("report")
    assert "transcribed its findings" in report
    assert review["reply"] in report

    # A reply that has the JSON is read from the hook; a transcription is refused. The JSON may sit in a fence
    # with other keys first.
    project.ok("fix")
    project.write("app.txt", "ok v3\n")
    project.ok("verify")
    project.ok("review", "begin")
    project.subagent_stop(REVIEWER, 'Checked the diff.\n\n```json\n{"summary": "fine", "findings": []}\n```')
    assert "--findings is only for" in project.refused("review", "record", "--findings", "-", stdin="[]").stderr
    assert "clean, independent;" in project.ok("review", "record")


def test_findings_come_from_the_handback_not_from_a_quote_in_the_closing_note(project: Project) -> None:
    reached_review(project)
    note = 'I sent the review to your caller. The parser takes any `{"findings": []}` it sees first.'
    project.subagent_stop(REVIEWER, note)  # a quoted object in prose is not the findings block
    assert "no findings JSON" in project.refused("review", "record").stderr

    project.ok("review", "begin")
    finding = {"file": "app.txt", "line": 1, "severity": "medium", "summary": "unclear version"}
    report = f"I read the diff.\n\n```json\n{json.dumps({'findings': [finding]})}\n```"
    transcript = project.root.parent / "agent-reviewer.jsonl"
    entries = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SubagentHandback", "input": {}}]}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "SubagentHandback", "input": {"message": report}}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": note}]}},
    ]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries) + "not json SubagentHandback\n")
    payload = {
        "hook_event_name": "SubagentStop",
        "session_id": "session-1",
        "cwd": str(project.root),
        "agent_type": REVIEWER,
        "agent_id": "agent-2",
        "agent_transcript_path": str(transcript),
        "last_assistant_message": note,
    }
    project.subagent_start(REVIEWER, "agent-2")
    assert project.hook("SubagentStop", payload).returncode == 0
    out = project.ok("review", "record")
    assert "clean, independent; 0 blocking, 1 non-blocking" in out
    assert "app.txt:1: unclear version" in out
    stop = json.loads(events_path(project).read_text(encoding="utf-8").splitlines()[-1])
    assert (stop["message"], stop["final_message"]) == (report, note)


def test_review_record_waits_for_a_reviewer_stop_that_lands_late(project: Project) -> None:
    reached_review(project)
    for wait in ("nan", "inf", "-1"):
        assert "--wait needs" in project.refused("review", "record", "--wait", wait).stderr
    started = time.monotonic()
    assert f"no result from {REVIEWER}" in project.refused("review", "record", "--wait", "1").stderr
    assert time.monotonic() - started >= 1

    record = subprocess.Popen(
        [str(BIN), "review", "record", "--wait", "20"],
        cwd=project.root,
        env=project.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert record.stdout is not None
        assert "Waiting up to 20s" in record.stdout.readline()  # it is waiting before the stop exists
        project.reviewer_says([])  # the hand-back came first; the hook lands afterwards
        out, err = record.communicate(timeout=30)
    finally:
        record.kill()
    assert record.returncode == 0, err
    assert "clean, independent" in out
    assert project.state()["reviews"][-1]["state"] == "recorded"


def test_subagent_stops_without_an_agent_type_are_ignored(project: Project) -> None:
    reached_review(project)
    project.subagent_stop("", "")
    without_key = {"hook_event_name": "SubagentStop", "session_id": "s", "cwd": str(project.root), "agent_id": "a9"}
    assert project.hook("SubagentStop", without_key).returncode == 0
    assert not events_path(project).exists()

    # Event files written by v0.1 still hold such stops; readers skip them.
    old = {"event": "subagent_stop", "at": "2026-09-23T20:24:31+0700", "agent_type": "", "agent_id": "a4"}
    events_path(project).write_text(json.dumps(old) + "\n", encoding="utf-8")
    project.subagent_stop("general-purpose", "done")
    refused = project.refused("review", "record", "--wait", "0")
    assert "Seen instead: general-purpose." in refused.stderr

    project.reviewer_says([])
    project.ok("review", "record")
    run_id = project.run_id
    project.ok("accept")
    report = project.ok("report", "--run", run_id)
    subagents = report.split("## Subagents", 1)[1].split("## History", 1)[0].strip().splitlines()
    assert subagents == ["- general-purpose: 1 finished", f"- {REVIEWER}: 1 finished"]


def test_applying_non_blocking_suggestions_uses_no_fix_round(project: Project) -> None:
    reached_review(project, max_fix_rounds=0)
    project.reviewer_says([{"file": "app.txt", "line": 1, "severity": "low", "summary": "say which version"}])
    assert "clean" in project.ok("review", "record")

    out = project.ok("fix")  # with a cap of 0, a counted round would escalate the run
    assert "fix round unchanged (0/0)" in out
    assert "suggestion: low      app.txt:1: say which version" in out
    state = project.state()
    assert (state["status"], state["phase"], state["fix_round"], state["suggestion_rounds"]) == (
        "active",
        "FIX",
        0,
        1,
    )
    assert "fix round 0/0 (plus 1 for non-blocking suggestions, not counted)" in project.ok("status")

    project.write("app.txt", "ok v2 (version two)\n")
    project.ok("verify")
    project.review_clean()
    run_id = project.run_id
    assert "0/0 (plus 1 for non-blocking suggestions, not counted)" in project.ok("accept")
    assert "0/0 (plus 1 for non-blocking suggestions, not counted)" in project.ok("report", "--run", run_id)


def test_init_takes_the_runner_from_the_package_manager_field(project: Project) -> None:
    manifest = {"packageManager": "pnpm@9.12.0", "scripts": {"lint": "eslint .", "test": "vitest run"}}
    project.write("package.json", json.dumps(manifest))
    config = project.root / ".supervise" / "config.toml"
    for lockfile in (None, "yarn.lock"):  # no lockfile at all, then a stray one: the declared manager wins
        if lockfile:
            project.write(lockfile, "")
            config.unlink()
        out = project.ok("init")
        assert "pnpm run lint" in out, lockfile
        assert "pnpm run test" in out, lockfile
        assert "yarn" not in config.read_text(encoding="utf-8")


def test_commit_takes_exactly_the_accepted_tree_and_a_clean_message(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    project.write("app.txt", "ok v2\n")
    project.write("notes.txt", "new file\n")
    project.ok("verify")
    project.review_clean()
    assert "is still open" in project.refused("commit", "-m", "feat: v2").stderr
    run_id = project.run_id
    assert "Nothing is committed" in project.ok("accept")

    project.write("app.txt", "ok v3\n")
    changed = project.refused("commit", "-m", "feat: v2")
    assert "files changed since accept" in changed.stderr
    assert "app.txt" in changed.stderr
    project.write("app.txt", "ok v2\n")

    for message in (
        "update app.txt",
        "feat: v2\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        "feat: v2 (generated with Claude Code)",
        "feat: v2 \N{ROBOT FACE}",
    ):
        assert "commit message is not allowed" in project.refused("commit", "-m", message).stderr, message
    assert project.git("log", "--format=%s") == "chore: initial\n"

    assert "Committed" in project.ok("commit", "-m", "feat(app): v2 with notes")
    state = project.state(run_id)
    assert project.git("rev-parse", "HEAD^{tree}").strip() == state["accepted_tree"]
    assert state["commit"]["sha"] == project.git("rev-parse", "HEAD").strip()
    assert project.git("log", "-1", "--format=%B").strip() == "feat(app): v2 with notes"
    assert project.git("ls-tree", "-r", "--name-only", "HEAD").split() == ["app.txt", "notes.txt"]
    assert project.git("status", "--porcelain") == ""
    assert "already committed" in project.refused("commit", "-m", "feat: again").stderr
    assert "feat(app): v2 with notes" in project.ok("report", "--run", run_id)

    # A run accepted without changes has nothing to commit.
    project.ok("start", "no change")
    project.ok("plan", "set", "-", stdin='["look only"]')
    project.ok("verify")
    project.review_clean()
    project.ok("accept")
    assert "nothing to commit" in project.refused("commit", "-m", "chore: nothing").stderr


def test_unmet_acceptance_criterion_blocks_at_any_severity(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    project.write("app.txt", "ok v2\n")
    project.ok("subtask", "done", "s1", "--note", "checked by hand")
    project.ok("verify")
    brief = project.ok("review", "begin")
    assert "(note: checked by hand)" in brief
    assert "Gates at verify #1 (the only commands known to have run on this tree): test `grep -q ok app.txt` pass" in (
        brief
    )
    assert '"kind": "acceptance"' in brief

    unproven = {"file": "", "line": None, "severity": "medium", "kind": "acceptance", "summary": "s1 is not shown"}
    project.reviewer_says([unproven])
    result = project.run("review", "record")
    assert result.returncode == 1
    assert "BLOCKING medium   [acceptance] (no file): s1 is not shown" in result.stdout
    assert "blocking" in project.refused("accept").stderr
    project.ok("fix")
    assert project.state()["fix_round"] == 1  # a real repair, unlike applying suggestions


def test_commit_runs_the_repository_hooks_and_leaves_supervise_files_alone(project: Project) -> None:
    project.configure(TEST_GATE)
    project.git("add", "-f", ".supervise/config.toml")
    project.git("commit", "-qm", "chore: share the gates")
    project.start()
    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.review_clean()
    run_id = project.run_id
    project.ok("accept")
    tracked = project.refused("commit", "-m", "feat: v2")
    assert "git tracks files under .supervise/" in tracked.stderr
    assert ".supervise/config.toml" in tracked.stderr
    project.git("rm", "-q", "--cached", ".supervise/config.toml")
    project.git("commit", "-qm", "chore: stop sharing the gates")

    hooks = project.root / ".git" / "hooks"
    (hooks / "commit-msg").write_text('#!/bin/sh\n! grep -q wip "$1"\n', encoding="utf-8")
    (hooks / "commit-msg").chmod(0o755)
    rejected = project.run("commit", "-m", "feat: wip")
    assert rejected.returncode == 3
    assert "git commit failed" in rejected.stderr
    assert "commit" not in project.state(run_id)

    (hooks / "pre-commit").write_text("#!/bin/sh\necho hooked >> app.txt && git add app.txt\n", encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    changed = project.run("commit", "-m", "feat: v2")
    assert changed.returncode == 3
    assert "differs from the accepted tree (a git hook changed files)" in changed.stderr
    assert project.state(run_id)["commit"]["tree_matches"] is False
    assert "**differs from the accepted tree**" in project.ok("report", "--run", run_id)


def test_plan_file_in_the_state_dir_is_read_once_and_leaves_the_tree_alone(project: Project) -> None:
    # Smoke test, 8 Nov 2026: a heredoc holding JSON made Claude Code ask for permission on every call, so the skill
    # has the model write the plan to .supervise/plan.json. A plan the CLI can't read stays for the model to fix; a
    # recorded one is removed, so the next run's Write doesn't hit a file the session never read.
    project.configure(TEST_GATE)
    project.ok("start", "change app.txt")
    plan_file = project.root / ".supervise" / "plan.json"
    assert "`supervisectl plan set .supervise/plan.json`" in project.ok("status")

    project.write(".supervise/plan.json", '{"subtasks": [')
    assert "not valid JSON" in project.refused("plan", "set", ".supervise/plan.json").stderr
    assert plan_file.exists()

    project.write(".supervise/plan.json", json.dumps({"subtasks": [{"title": "edit app.txt"}, {"title": "check"}]}))
    out = project.ok("plan", "set", ".supervise/plan.json")
    assert "Read and removed .supervise/plan.json." in out
    assert not plan_file.exists()

    # A bare number names the subtask, as models tend to write it.
    assert "Subtask s1 done" in project.ok("subtask", "done", "1")
    assert "there is no subtask 3" in project.refused("subtask", "done", "3").stderr

    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.ok("review", "begin")
    # Written after `review begin`: the record below would be refused if the file counted as a change to the tree.
    project.write(".supervise/findings.json", '{"findings": []}')
    out = project.ok("review", "record", "--self", "--reason", "one line", "--findings", ".supervise/findings.json")
    assert "Read and removed .supervise/findings.json." in out
    assert "ACCEPTED run" in project.ok("accept")


def test_input_files_outside_the_state_dir_are_left_alone(project: Project) -> None:
    project.configure(TEST_GATE)
    project.ok("start", "change app.txt")
    project.write("plan.json", json.dumps(["edit app.txt"]))
    out = project.ok("plan", "set", "plan.json")
    assert "removed" not in out
    assert (project.root / "plan.json").exists()
