#!/usr/bin/env python3
"""Check commit messages: Conventional Commits header and no AI attribution.

Used by the local ``commit-msg`` git hook (``--msg-file``) and by CI
(``--range``). Exit status is 0 when every message passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence

TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
HEADER_RE = re.compile(
    r"^(?P<type>" + "|".join(TYPES) + r")(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?(?P<breaking>!)?: (?P<subject>\S.*)$"
)
MAX_HEADER = 100
# Messages git writes itself; they are not authored headers.
GIT_GENERATED_RE = re.compile(r'^(Merge (branch|pull request|remote-tracking branch|tag|commit) |Revert ")')
AUTOSQUASH_RE = re.compile(r"^(fixup|squash|amend)! ")
SCISSORS = "# ------------------------ >8 ------------------------"

AI_IDENTITY = (
    r"claude|anthropic|fable|opus|sonnet|haiku|openai|chatgpt|gpt-?\d|copilot|codex|gemini|"
    r"cursor|devin|aider|windsurf|\[bot\]|\bbot\b|\bai\b"
)
ATTRIBUTION_RES = (
    (re.compile(r"^co-authored-by:.*(" + AI_IDENTITY + ")", re.I | re.M), "AI co-author trailer"),
    (re.compile(r"^.*generated (with|by)\b.*(" + AI_IDENTITY + ")", re.I | re.M), '"Generated with/by" AI attribution'),
    (re.compile(r"\U0001f916"), "robot emoji attribution"),
    (re.compile(r"claude\.(com|ai)/(claude-)?code", re.I), "link to an AI coding tool"),
    (re.compile(r"noreply@anthropic\.com", re.I), "Anthropic noreply address"),
    (re.compile(r"^(assisted|written|authored)-by:.*(" + AI_IDENTITY + ")", re.I | re.M), "AI assistance trailer"),
    (re.compile(r"\bai[- ](generated|assisted)\b", re.I), "AI-generated/assisted label"),
)


def clean(message: str) -> str:
    """Drop what git's default cleanup would drop: scissors section and comment lines."""
    if SCISSORS in message:
        message = message.split(SCISSORS, 1)[0]
    lines = [line.rstrip() for line in message.splitlines() if not line.startswith("#")]
    return "\n".join(lines).strip("\n")


def check_message(message: str, *, allow_autosquash: bool = True) -> list[str]:
    """Return a list of problems; an empty list means the message is acceptable."""
    text = clean(message)
    if not text.strip():
        return ["empty commit message"]
    lines = text.splitlines()
    header = lines[0]
    problems: list[str] = []

    if not GIT_GENERATED_RE.match(header):
        subject = header
        if AUTOSQUASH_RE.match(subject):
            if not allow_autosquash:
                problems.append(f"autosquash commit must be squashed before merging: {header!r}")
            subject = AUTOSQUASH_RE.sub("", subject, count=1)
        if not HEADER_RE.match(subject):
            problems.append(
                f"header is not a Conventional Commit ('type(scope)!: subject', type in {', '.join(TYPES)}): {header!r}"
            )
        if len(header) > MAX_HEADER:
            problems.append(f"header is {len(header)} characters; the limit is {MAX_HEADER}")
        if len(lines) > 1 and lines[1].strip():
            problems.append("the second line must be blank (separate header and body)")

    for pattern, label in ATTRIBUTION_RES:
        match = pattern.search(text)
        if match:
            problems.append(f"AI attribution is not allowed ({label}): {match.group(0).strip()!r}")
    return problems


def messages_in_range(rev_range: str) -> Iterable[tuple[str, str]]:
    shas = subprocess.run(
        ["git", "rev-list", "--reverse", rev_range], check=True, capture_output=True, text=True
    ).stdout.split()
    for sha in shas:
        body = subprocess.run(
            ["git", "log", "-1", "--format=%B", sha], check=True, capture_output=True, text=True
        ).stdout
        yield sha[:12], body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--msg-file", help="commit message file (commit-msg hook argument)")
    source.add_argument("--range", dest="rev_range", help="git revision range to check, e.g. main..HEAD")
    source.add_argument("--message", help="check this message text")
    args = parser.parse_args(argv)

    if args.msg_file:
        with open(args.msg_file, encoding="utf-8") as fh:
            items: Iterable[tuple[str, str]] = [("message", fh.read())]
        allow_autosquash = True
    elif args.message is not None:
        items, allow_autosquash = [("message", args.message)], True
    else:
        items, allow_autosquash = messages_in_range(args.rev_range), False

    failed = 0
    for label, message in items:
        problems = check_message(message, allow_autosquash=allow_autosquash)
        if problems:
            failed += 1
            print(f"commit check failed for {label}:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
