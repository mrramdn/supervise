"""Hook entry points (`supervisectl hook <event>`). Kept import-light: they run inside every matching tool call.

A hook never breaks the session: any error is logged and the tool call goes ahead.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REVIEWER = "supervise:code-reviewer"
MAX_MESSAGE = 200_000

OVERRIDE_REASON = (
    "`supervisectl override` is reserved for the user. Stop and tell the user why the run needs more fix rounds; "
    'they can type `! supervisectl override --max-fix-rounds N --reason "..."` themselves. '
    "Afterwards, run `supervisectl status` before continuing."
)


def _state_dir(start: str) -> Path | None:
    """Walk up from `start` to the repository root, looking for `.supervise/current`."""
    here = Path(start).resolve()
    for folder in (here, *here.parents):
        if (folder / ".supervise" / "current").is_file():
            return folder / ".supervise"
        if (folder / ".git").exists():
            return None
    return None


def _log(state_dir: Path | None, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n"
    try:
        if state_dir is None:
            raise OSError
        with (state_dir / "hook.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        sys.stderr.write(f"supervise hook: {line}")


def mentions_override(command: str) -> bool:
    flat = re.sub(r"[\"'\\]", "", command)
    return "supervisectl" in flat and re.search(r"\boverride\b", flat) is not None


def pre_tool_use(data: dict[str, Any]) -> None:
    if data.get("tool_name") != "Bash":
        return
    command = str((data.get("tool_input") or {}).get("command", ""))
    if mentions_override(command):
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": OVERRIDE_REASON,
        }
        print(json.dumps({"hookSpecificOutput": decision}))


def _handback(transcript: Any) -> str | None:
    """The message of the subagent's last SubagentHandback call, read from its transcript.

    Claude Code 2.1.280 can hand a subagent's report to the caller through that tool; the agent's final message
    (`last_assistant_message`) is then only a note about it, and the findings JSON lives in the handback.
    """
    if not isinstance(transcript, str) or not transcript:
        return None
    found = None
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "SubagentHandback" not in line:
                    continue
                try:
                    content = (json.loads(line).get("message") or {}).get("content")
                except (ValueError, AttributeError):
                    continue
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("name") == "SubagentHandback":
                        tool_input = block.get("input")
                        message = tool_input.get("message") if isinstance(tool_input, dict) else None
                        found = message if isinstance(message, str) else found
    except OSError:
        return None
    return found


def subagent_stop(data: dict[str, Any], state_dir: Path) -> None:
    """Append the stop to the active run's events; `review record` reads reviewer results from there.

    Stops without an `agent_type` are skipped. Claude Code sends them about every 30 seconds while a subagent
    runs, each with its own `agent_id` and no transcript; they are internal queries, not agents the model started.
    """
    if not data.get("agent_type"):
        return
    run_id = (state_dir / "current").read_text(encoding="utf-8").strip()
    run_dir = state_dir / "runs" / run_id
    if not run_id or not run_dir.is_dir():
        return
    event: dict[str, Any] = {
        "event": "subagent_stop",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_type": data.get("agent_type"),
        "agent_id": data.get("agent_id"),
        "session_id": data.get("session_id"),
    }
    if data.get("agent_type") == REVIEWER:
        final = str(data.get("last_assistant_message") or "")
        handback = _handback(data.get("agent_transcript_path"))
        event["message"] = (handback if handback is not None else final)[:MAX_MESSAGE]
        if handback is not None and handback != final:
            event["final_message"] = final[:MAX_MESSAGE]
        event["transcript"] = data.get("agent_transcript_path")
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode()
    fd = os.open(run_dir / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        os.close(fd)


def main(argv: list[str]) -> int:
    """`argv` is everything after `supervisectl`, so argv[0] == "hook"."""
    event = argv[1] if len(argv) > 1 else ""
    state_dir = None
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return 0
        if event == "pre-tool-use":
            pre_tool_use(data)
        elif event == "subagent-stop":
            state_dir = _state_dir(str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()))
            if state_dir is not None:
                subagent_stop(data, state_dir)
        else:
            _log(None, f"unknown hook event {event!r}")
    except Exception as exc:
        _log(state_dir, f"{event}: {type(exc).__name__}: {exc}")
    return 0
