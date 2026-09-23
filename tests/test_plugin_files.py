"""Checks `claude plugin validate --strict` does not make (spike S9): names, lengths, tools, model, versions."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from conftest import REPO

SKILL = REPO / "skills" / "supervise" / "SKILL.md"


def frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert match, f"{path} has no frontmatter"
    fields = dict(line.split(": ", 1) for line in match.group(1).splitlines())
    for key, value in fields.items():
        # A second ": " in a plain YAML scalar is a syntax error, which breaks loading the skill or agent.
        assert value.startswith(('"', "'")) or ": " not in value, f"{path}: quote the {key} value"
    return fields, match.group(2)


def test_skill_file() -> None:
    fields, body = frontmatter(SKILL)
    assert fields["name"] == SKILL.parent.name == "supervise"
    assert len(fields["description"]) <= 1024
    assert fields["disable-model-invocation"] == "false"
    assert len(SKILL.read_text(encoding="utf-8").splitlines()) < 150
    assert not re.search(r"\b(MUST|NEVER|CRITICAL)\b", body)
    assert not re.search(r"(/home/|/Users/|~/)", SKILL.read_text(encoding="utf-8"))
    assert "!`" not in body  # dynamic context lines break the skill when Bash is denied (spike S1)
    for link in re.findall(r"\]\(([^)]+)\)", body):
        assert (SKILL.parent / link).is_file(), link


def test_agents() -> None:
    expected_tools = {"implementer": "Read, Write, Edit, Bash, Grep, Glob", "code-reviewer": "Read, Grep, Glob"}
    for name, tools in expected_tools.items():
        fields, _ = frontmatter(REPO / "agents" / f"{name}.md")
        assert fields["name"] == name
        assert fields["tools"] == tools
        assert fields["model"] == "inherit"  # spike S4: inherit the session's model; no effort field
        assert "effort" not in fields


def test_hooks_call_the_bundled_cli() -> None:
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert sorted(hooks) == ["PreToolUse", "SubagentStop"]
    for entries in hooks.values():
        for entry in entries:
            for handler in entry["hooks"]:
                assert handler["command"].startswith('"${CLAUDE_PLUGIN_ROOT}/bin/supervisectl" hook ')


def test_versions_agree() -> None:
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    init = (REPO / "src" / "supervise" / "__init__.py").read_text(encoding="utf-8")
    assert plugin["name"] == marketplace["plugins"][0]["name"] == "supervise"
    assert plugin["version"] == pyproject["project"]["version"]
    assert f'__version__ = "{plugin["version"]}"' in init
