"""Commit message rules for `supervisectl commit`: a Conventional Commits header and no AI attribution."""

from __future__ import annotations

import re
import unicodedata

TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
HEADER = re.compile(r"^(?:" + "|".join(TYPES) + r")(?:\([A-Za-z0-9][A-Za-z0-9._/-]*\))?!?: \S")
HINT = 'a Conventional Commits message without AI attribution, e.g. `-m "fix(api): handle an empty body"`'

_AI_TOOLS = r"claude|anthropic|openai|chatgpt|gpt-?\d[\w.-]*|copilot|codex|gemini|cursor|devin|aider|windsurf"
_AI_TOOL = r"(?:" + _AI_TOOLS + r")"
_AI_WORD = r"(?:" + _AI_TOOLS + r"|llms?|ai)"
_ATTRIBUTION = (
    # A trailer that credits an AI tool: "Co-Authored-By: Claude <noreply@anthropic.com>", "Assisted-by: Copilot".
    re.compile(r"^\s*[A-Za-z-]+-(?:by|with)\s*:.*\b" + _AI_TOOL + r"\b", re.I | re.M),
    # A credit line: "Generated with [Claude Code]", "written by an AI", "(made with Cursor)".
    re.compile(
        r"\b(?:generated|created|written|made|built|authored|assisted|co-written)\s+(?:with|by|using|via)\s+"
        r"(?:an?\s+|the\s+)?[\[(\"'*_]*" + _AI_WORD + r"\b",
        re.I,
    ),
    re.compile(r"@(?:anthropic|openai)\.com\b", re.I),
    re.compile("\N{ROBOT FACE}"),
)


def problem(message: str) -> str | None:
    """Why `message` may not be used for a commit, or None when it may."""
    if not message.strip():
        return "The message is empty."
    header = message.splitlines()[0]
    if not HEADER.match(header):
        return f"The first line is not a Conventional Commits header (type(scope): subject): {header!r}."
    folded = unicodedata.normalize("NFKC", message)
    for pattern in _ATTRIBUTION:
        found = pattern.search(folded)
        if found:
            return f"The message carries AI attribution: {found.group(0).strip()!r}."
    return None
