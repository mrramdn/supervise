"""Drive bin/supervisectl in throwaway git repositories. No model is involved: hook events are fed to the
hook commands from hooks/hooks.json exactly as Claude Code would run them."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "supervisectl"
REVIEWER = "supervise:code-reviewer"
TEST_GATE = ("test", "grep -q ok app.txt", "test")


def hook_command(event: str) -> str:
    """The single command hooks/hooks.json registers for `event`."""
    [entry] = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"][event]
    [handler] = entry["hooks"]
    return handler["command"]


class Project:
    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.root = root
        self.env = env

    def run(self, *args: str, stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BIN), *args],
            cwd=cwd or self.root,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def ok(self, *args: str, stdin: str | None = None) -> str:
        result = self.run(*args, stdin=stdin)
        assert result.returncode == 0, f"{args} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
        return result.stdout

    def refused(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run a command that must be refused: exit 2 and a byte-identical state file."""
        before = self.state_path().read_bytes() if self.has_run() else None
        result = self.run(*args, stdin=stdin)
        assert result.returncode == 2, f"{args} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
        assert "refused" in result.stderr
        if before is not None:
            assert self.state_path().read_bytes() == before
        return result

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],  # noqa: S607
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def configure(self, *gates: tuple[str, str, str], max_fix_rounds: int = 3) -> None:
        """`init`, then replace the detected gates with these (name, command, kind) triples."""
        self.ok("init")
        lines = [f"max_fix_rounds = {max_fix_rounds}", 'block_on_severity = "high"']
        for name, command, kind in gates:
            lines += ["", "[[gates]]", f"name = {json.dumps(name)}", f"command = {json.dumps(command)}"]
            lines.append(f"kind = {json.dumps(kind)}")
        (self.root / ".supervise" / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def has_run(self) -> bool:
        return (self.root / ".supervise" / "current").is_file()

    @property
    def run_id(self) -> str:
        return (self.root / ".supervise" / "current").read_text(encoding="utf-8").strip()

    def state_path(self, run_id: str | None = None) -> Path:
        return self.root / ".supervise" / "runs" / (run_id or self.run_id) / "state.json"

    def state(self, run_id: str | None = None) -> dict[str, Any]:
        return json.loads(self.state_path(run_id).read_text(encoding="utf-8"))

    def start(self, task: str = "change app.txt") -> str:
        out = self.ok("start", task)
        plan = {"summary": "one change", "subtasks": [{"title": "edit app.txt", "acceptance": "the test gate passes"}]}
        self.ok("plan", "set", "-", stdin=json.dumps(plan))
        return out

    def hook(self, event: str, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        """Run the hooks.json command for `event` through /bin/sh, as Claude Code does."""
        env = {**self.env, "CLAUDE_PLUGIN_ROOT": str(REPO), "CLAUDE_PROJECT_DIR": str(self.root)}
        return subprocess.run(
            ["/bin/sh", "-c", hook_command(event)],
            cwd=self.root,
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def subagent_start(self, agent_type: str, agent_id: str = "agent-1") -> None:
        payload = {
            "hook_event_name": "SubagentStart",
            "session_id": "session-1",
            "cwd": str(self.root),
            "agent_type": agent_type,
            "agent_id": agent_id,
        }
        result = self.hook("SubagentStart", payload)
        assert (result.returncode, result.stdout) == (0, ""), result.stderr

    def subagent_stop(self, agent_type: str, message: str, agent_id: str = "agent-1", *, started: bool = True) -> None:
        """A subagent that ends now. With `started`, it also starts now; else its start was sent earlier (or never)."""
        if started:
            self.subagent_start(agent_type, agent_id)
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "session-1",
            "cwd": str(self.root),
            "agent_type": agent_type,
            "agent_id": agent_id,
            "last_assistant_message": message,
        }
        result = self.hook("SubagentStop", payload)
        assert (result.returncode, result.stdout) == (0, ""), result.stderr

    def reviewer_says(self, findings: list[dict[str, Any]]) -> None:
        body = json.dumps({"findings": findings}, indent=2)
        self.subagent_stop(REVIEWER, f"I read the diff and the changed files.\n\n```json\n{body}\n```")

    def review_clean(self) -> None:
        self.ok("review", "begin")
        self.reviewer_says([])
        self.ok("review", "record")


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "project"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "GIT_"))}
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    project = Project(root, env)
    project.git("init", "-q", "-b", "main")
    project.write("app.txt", "ok\n")
    project.git("add", "-A")
    project.git("commit", "-q", "-m", "chore: initial")
    return project
