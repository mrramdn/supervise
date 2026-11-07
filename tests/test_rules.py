"""One scenario per core rule (PLAN §6), each running the real CLI in a temporary git repository."""

from __future__ import annotations

import json
import shlex
import subprocess
import time

import pytest

from conftest import BIN, REVIEWER, TEST_GATE, Project


def test_accept_needs_a_passing_verify_and_a_clean_review(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    assert "passing verify" in project.refused("accept").stderr

    project.write("app.txt", "bad\n")
    assert project.run("verify").returncode == 1
    project.refused("accept")

    project.ok("fix")
    project.write("app.txt", "ok, fixed\n")
    project.ok("verify")
    project.refused("accept")
    project.ok("review", "begin")
    project.refused("accept")

    project.reviewer_says([{"file": "app.txt", "line": 1, "severity": "high", "summary": "wrong wording"}])
    assert project.run("review", "record").returncode == 1
    assert "blocking" in project.refused("accept").stderr
    assert project.state()["status"] == "active"


def test_edit_after_review_refuses_accept(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.review_clean()

    project.write("app.txt", "ok v3\n")
    result = project.refused("accept")
    assert "files changed since the review" in result.stderr
    assert "app.txt" in result.stderr

    # A new untracked file counts too; committing does not (the fingerprint is the tree's content).
    project.write("app.txt", "ok v2\n")
    project.write("notes.txt", "later\n")
    assert "notes.txt" in project.refused("accept").stderr
    (project.root / "notes.txt").unlink()
    project.git("commit", "-qam", "feat: v2")

    run_id = project.run_id
    assert "ACCEPTED" in project.ok("accept")
    assert project.state(run_id)["status"] == "accepted"
    assert not project.has_run()


def test_commands_in_the_wrong_phase_exit_2_and_leave_state_alone(project: Project) -> None:
    project.configure(TEST_GATE)
    project.ok("start", "task")
    for args in (
        ["verify"],
        ["review", "begin"],
        ["review", "record"],
        ["fix"],
        ["accept"],
        ["subtask", "add", "more"],
        ["subtask", "done", "s1"],
        ["resume"],
    ):
        assert "next:" in project.refused(*args).stderr, args

    project.ok("plan", "set", "-", stdin=json.dumps(["edit app.txt"]))
    for args in (["plan", "set", "-"], ["review", "begin"], ["review", "record"], ["fix"], ["accept"]):
        project.refused(*args, stdin="[]")
    assert project.state()["phase"] == "IMPLEMENT"


def test_project_without_a_test_gate_is_labelled_no_tests(project: Project) -> None:
    assert "NO TESTS" in project.ok("init")  # nothing to detect in this repository
    project.configure(("lint", "true", "lint"))
    assert "NO TESTS" in project.start()
    project.write("app.txt", "ok v2\n")
    assert "NO TESTS" in project.ok("verify")
    assert "NO TESTS" in project.ok("status")
    project.review_clean()
    run_id = project.run_id
    assert "NO TESTS" in project.ok("accept")
    assert "NO TESTS" in project.ok("report", "--run", run_id)


def test_fix_cap_escalates_and_only_override_raises_it(project: Project) -> None:
    project.configure(TEST_GATE, max_fix_rounds=1)
    project.start()
    project.write("app.txt", "bad\n")
    assert project.run("verify").returncode == 1
    project.ok("fix")
    assert project.run("verify").returncode == 1

    escalated = project.run("fix")
    assert escalated.returncode == 1
    assert "escalated" in escalated.stdout
    assert "! supervisectl override" in escalated.stdout
    state = project.state()
    assert (state["status"], state["fix_round"]) == ("escalated", 1)
    project.refused("verify")
    project.refused("fix")
    project.refused("override", "--max-fix-rounds", "1", "--reason", "not above the rounds used")

    project.ok("override", "--max-fix-rounds", "3", "--reason", "one more attempt")
    state = project.state()
    assert (state["status"], state["max_fix_rounds"]) == ("active", 3)
    assert state["overrides"][0]["reason"] == "one more attempt"
    project.ok("fix")
    assert project.state()["fix_round"] == 2
    project.write("app.txt", "ok\n")
    project.ok("verify")


def test_hook_denies_override_from_the_model(project: Project) -> None:
    denied = [
        "supervisectl override --max-fix-rounds 9 --reason x",
        'cd sub && "${CLAUDE_PLUGIN_ROOT}/bin/supervisectl" override --max-fix-rounds 9 --reason x',
        "supervisectl ov''erride --max-fix-rounds 9 --reason x",
        "supervisectl status && supervisectl over\\ride --max-fix-rounds 9 --reason x",
        "supervisectl -- override --max-fix-rounds 9 --reason x",
        "supervisectl \\\n  override --max-fix-rounds 9 --reason x",
    ]
    finding = {"file": "app.txt", "line": 1, "severity": "low", "summary": "an env var cannot override the config"}
    allowed = [  # the word in a task, a subtask, a plan, a finding, a commit message, or a search is not the command
        "supervisectl status",
        "supervisectl review record",
        "ls",
        'supervisectl start "let users override the theme"',
        "supervisectl subtask add 'override the default timeout per project' --origin user",
        'supervisectl plan set - <<\'EOF\'\n{"subtasks": ["override the theme"]}\nEOF',
        f"supervisectl review record --findings - <<'EOF'\n{json.dumps({'findings': [finding]})}\nEOF",
        'supervisectl commit -m "feat: let users override the theme"',
        'supervisectl start "abort the upload or override its limit"',
        "supervisectl report | grep -c override",
    ]
    for command in denied + allowed:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        result = project.hook("PreToolUse", payload)
        assert result.returncode == 0, result.stderr
        if command in allowed:
            assert result.stdout == "", command
            continue
        assert result.stdout, f"the hook let {command!r} through"
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", command
        assert "reserved for the user" in decision["permissionDecisionReason"]
        assert "! supervisectl override" in decision["permissionDecisionReason"]


def test_hook_denies_abort_so_a_stuck_run_cannot_start_over(project: Project) -> None:
    # Escalated with a change no review saw. Abort plus start would give the work fresh fix rounds and make
    # other.txt part of the new run's baseline, so a later accept and commit would carry it unreviewed.
    project.configure(TEST_GATE, max_fix_rounds=1)
    project.start()
    project.write("app.txt", "bad\n")
    project.write("other.txt", "never reviewed\n")
    assert project.run("verify").returncode == 1
    project.ok("fix")
    assert project.run("verify").returncode == 1
    escalated = project.run("fix")
    assert escalated.returncode == 1

    denied = [
        'supervisectl abort --reason "start over"',
        'supervisectl abort && supervisectl start "change app.txt"',
        'cd docs && "${CLAUDE_PLUGIN_ROOT}/bin/supervisectl" ab""ort',
        "supervisectl -- abort",
    ]
    allowed = [  # the word in a task, a subtask, a plan, or a search is not the command
        'supervisectl start "abort the upload when the user cancels"',
        "supervisectl subtask add 'retry an aborted upload' --origin user",
        'supervisectl plan set - <<\'EOF\'\n{"subtasks": ["abort on timeout"]}\nEOF',
        "supervisectl report | grep -c abort",
    ]
    for command in denied + allowed:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        result = project.hook("PreToolUse", payload)
        assert result.returncode == 0, result.stderr
        if command in allowed:
            assert result.stdout == "", command
            continue
        assert result.stdout, f"the hook let {command!r} through"
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", command
        assert "reserved for the user" in decision["permissionDecisionReason"]
        assert "! supervisectl abort" in decision["permissionDecisionReason"]
    assert project.state()["status"] == "escalated"
    assert "! supervisectl abort" in escalated.stdout
    assert "! supervisectl abort" in project.ok("status")

    # The user's `! supervisectl abort` skips PreToolUse hooks, so they can still drop the run, and it says what
    # it leaves behind.
    aborted = project.ok("abort", "--reason", "dropped by the user")
    assert "would not be reviewed" in aborted
    assert "other.txt" in aborted
    assert not project.has_run()


def test_review_record_needs_the_code_reviewer_and_self_review_needs_a_small_diff(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    project.write("app.txt", "ok\n" + "".join(f"line {i}\n" for i in range(20)))
    project.ok("verify")
    project.ok("review", "begin")

    assert f"no result from {REVIEWER}" in project.refused("review", "record", "--wait", "0").stderr
    project.subagent_stop("general-purpose", '{"findings": []}')
    assert "Seen instead: general-purpose" in project.refused("review", "record", "--wait", "0").stderr
    self_review = project.refused("review", "record", "--self", "--reason", "looks fine")
    assert "only allowed for small diffs" in self_review.stderr
    project.subagent_stop(REVIEWER, "All good, nothing to report.")
    assert "no findings JSON" in project.refused("review", "record").stderr

    project.ok("review", "begin")  # a fresh review: only reviewer results from now on count
    project.reviewer_says([{"file": "app.txt", "line": 3, "severity": "low", "summary": "long file"}])
    assert "clean, independent" in project.ok("review", "record")

    # A small follow-up (at most 10 lines since the last independent review) may be self-reviewed.
    project.ok("subtask", "add", "fix a typo", "--origin", "user")
    project.write("app.txt", "ok\n" + "".join(f"line {i}\n" for i in range(19)) + "line 99\n")
    project.ok("verify")
    project.ok("review", "begin")
    findings = json.dumps({"findings": [{"file": "app.txt", "line": 21, "severity": "info", "summary": "fine"}]})
    out = project.ok("review", "record", "--self", "--reason", "two-line typo fix", "--findings", "-", stdin=findings)
    assert "self-review (two-line typo fix)" in out
    run_id = project.run_id
    project.ok("accept")
    report = project.ok("report", "--run", run_id)
    assert "self-review (two-line typo fix)" in report
    assert f"{REVIEWER}: 2 finished" in report


def test_pause_in_one_process_and_resume_in_another(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.ok("pause", "--reason", "end of the session")
    project.refused("review", "begin")
    before = project.state()
    assert before["status"] == "paused"

    # Another session, started from a subdirectory with a different environment.
    subdir = project.root / "docs"
    subdir.mkdir()
    env = {**project.env, "CLAUDE_CODE_SESSION_ID": "another-session"}
    resumed = subprocess.run([str(BIN), "resume"], cwd=subdir, env=env, capture_output=True, text=True, check=False)
    assert resumed.returncode == 0, resumed.stderr
    assert "phase VERIFY" in resumed.stdout
    after = project.state()
    assert after["status"] == "active"
    assert (after["phase"], after["verifies"], after["subtasks"]) == (
        before["phase"],
        before["verifies"],
        before["subtasks"],
    )
    project.ok("review", "begin")


def test_user_request_does_not_count_as_a_fix_round(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.review_clean()

    assert "goes through `supervisectl fix`" in project.refused("subtask", "add", "more work").stderr
    project.ok("subtask", "add", "also update the docs", "--origin", "user")
    state = project.state()
    assert (state["phase"], state["fix_round"]) == ("IMPLEMENT", 0)
    assert state["subtasks"][-1]["origin"] == "user"

    project.ok("verify")
    project.ok("review", "begin")
    project.reviewer_says([{"file": "app.txt", "line": 1, "severity": "high", "summary": "wrong wording"}])
    assert project.run("review", "record").returncode == 1
    project.ok("fix")  # repairing a blocking review is a fix round, for contrast
    assert project.state()["fix_round"] == 1


def test_user_request_cannot_skip_the_fix_round_of_a_negative_result(project: Project) -> None:
    project.configure(TEST_GATE, max_fix_rounds=1)
    project.start()
    project.write("app.txt", "bad\n")
    assert project.run("verify").returncode == 1
    refused = project.refused("subtask", "add", "retry", "--origin", "user")
    assert "`supervisectl fix` comes first" in refused.stderr

    project.ok("fix")
    project.ok("subtask", "add", "also update the docs", "--origin", "user")  # in the fix round it is fine
    assert (project.state()["phase"], project.state()["fix_round"]) == ("FIX", 1)
    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.ok("review", "begin")
    project.reviewer_says([{"file": "app.txt", "line": 1, "severity": "high", "summary": "wrong wording"}])
    assert project.run("review", "record").returncode == 1
    project.refused("subtask", "add", "address the review", "--origin", "user")

    assert project.run("fix").returncode == 1  # the cap still holds
    assert project.state()["status"] == "escalated"


def test_reviewer_started_before_review_begin_does_not_count(project: Project) -> None:
    project.configure(TEST_GATE)
    project.start()
    project.write("app.txt", "ok v2\n")
    project.ok("verify")
    project.ok("review", "begin")
    project.subagent_start(REVIEWER, "old-reviewer")  # reads review-1.diff in the background

    project.write("app.txt", "ok v3, never reviewed\n")
    project.ok("verify")
    project.ok("review", "begin")
    project.subagent_stop(REVIEWER, '{"findings": []}', "old-reviewer", started=False)
    refused = project.refused("review", "record", "--wait", "0")
    assert "started before `review begin`" in refused.stderr
    assert "old-reviewer" in refused.stderr
    project.refused("accept")

    project.reviewer_says([])
    assert "clean, independent" in project.ok("review", "record")
    assert project.state()["reviews"][-1]["agent_ids"] == ["agent-1"]


def test_start_in_home_is_refused(project: Project) -> None:
    env = {**project.env, "HOME": str(project.root)}
    for args in (["init"], ["start", "task"], ["status"]):
        result = subprocess.run([str(BIN), *args], cwd=project.root, env=env, capture_output=True, text=True)
        assert result.returncode == 2, args
        assert "home directory" in result.stderr
    assert not (project.root / ".supervise").exists()


def test_second_writer_is_refused_while_verify_runs_and_state_stays_valid(project: Project) -> None:
    marker = project.root.parent / "gate-started"
    slow = f"touch {shlex.quote(str(marker))} && sleep 2 && grep -q ok app.txt"
    project.configure(("test", slow, "test"))
    project.start()
    marker.unlink()
    subtasks = project.state()["subtasks"]

    verify = subprocess.Popen(
        [str(BIN), "verify"], cwd=project.root, env=project.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists():
            assert time.monotonic() < deadline, "the gate never started"
            time.sleep(0.05)
        for args in (["subtask", "add", "racing"], ["pause"], ["verify"]):
            result = project.run(*args)
            assert result.returncode == 2, args
            assert "another supervisectl command is running" in result.stderr
        assert project.run("status").returncode == 0  # readers do not need the lock
    finally:
        assert verify.wait(timeout=30) == 0

    state = project.state()
    assert len(state["verifies"]) == 1
    assert state["subtasks"] == subtasks
    assert state["status"] == "active"
    assert not [p.name for p in project.state_path().parent.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("change_output", [False, True])
def test_failing_baseline_gate_counts_as_unchanged_only_with_identical_output(
    project: Project, change_output: bool
) -> None:
    project.write("legacy.txt", "bad\n")
    project.write(
        "legacy_check.py",
        "import random, sys\n"
        "print('legacy:', open('legacy.txt').read().strip())\n"
        "print(f'1 failed in {random.random():.2f}s')\n"
        "sys.exit(0 if 'ok' in open('legacy.txt').read() else 1)\n",
    )
    project.git("add", "-A")
    project.git("commit", "-qm", "chore: legacy check")
    project.configure(TEST_GATE, ("legacy", "python3 legacy_check.py", "test"))
    assert "FAIL" in project.start()

    project.write("app.txt", "ok v2\n")
    if change_output:
        project.write("legacy.txt", "bad in a new way\n")
    result = project.run("verify")
    legacy = project.state()["verifies"][-1]["gates"][1]
    if change_output:
        assert (result.returncode, legacy["status"]) == (1, "fail")
    else:
        assert (result.returncode, legacy["status"]) == (0, "baseline"), result.stdout
        assert "legacy failed before the run and fail the same way now" in result.stdout


def test_gates_that_change_the_tree_fail_verify_without_costing_a_fix_round(project: Project) -> None:
    project.configure(("test", "grep -q ok app.txt && echo run >> build.log", "test"))
    project.start()
    project.write("app.txt", "ok v2\n")
    result = project.run("verify")
    assert result.returncode == 1
    assert "the gates changed the working tree" in result.stdout
    assert "build.log" in result.stdout
    project.refused("review", "begin")

    project.write(".gitignore", "build.log\n")
    project.ok("verify")
    assert project.state()["fix_round"] == 0
