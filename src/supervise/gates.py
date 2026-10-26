"""Project config, gate detection for `init`, gate runs, and the baseline comparison."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from supervise.store import Refused

Gate = dict[str, Any]

KINDS = ("test", "lint", "typecheck", "build", "other")
SEVERITIES = ("info", "low", "medium", "high", "critical")
PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")
DEFAULT_TIMEOUT = 1800
DEFAULT_MAX_FIX_ROUNDS = 3
DEFAULT_BLOCK_ON = "high"
TAIL_LINES = 50
MAX_LINE = 500

# --- config ------------------------------------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    def bad(detail: str) -> Refused:
        return Refused("the supervise config is invalid", f"{path}: {detail}", f"fix {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise bad(str(exc)) from None
    max_rounds = data.get("max_fix_rounds", DEFAULT_MAX_FIX_ROUNDS)
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 0:
        raise bad("max_fix_rounds must be a whole number, 0 or more")
    block_on = data.get("block_on_severity", DEFAULT_BLOCK_ON)
    if block_on not in SEVERITIES:
        raise bad(f"block_on_severity must be one of {', '.join(SEVERITIES)}")
    raw_gates = data.get("gates", [])
    if not isinstance(raw_gates, list):
        raise bad("gates must be an array of tables ([[gates]])")
    gates: list[Gate] = []
    for i, raw in enumerate(raw_gates):
        if not isinstance(raw, dict):
            raise bad(f"gates[{i}] must be a table ([[gates]])")
        name, command = raw.get("name"), raw.get("command")
        kind, timeout = raw.get("kind", "other"), raw.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise bad(f"gates[{i}].name must be a short name (letters, digits, _ . -)")
        if any(g["name"] == name for g in gates):
            raise bad(f"gate name {name!r} is used twice")
        if not isinstance(command, str) or not command.strip():
            raise bad(f"gates[{i}].command must be a non-empty string")
        if kind not in KINDS:
            raise bad(f"gates[{i}].kind must be one of {', '.join(KINDS)}")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise bad(f"gates[{i}].timeout must be a positive number of seconds")
        gates.append({"name": name, "command": command, "kind": kind, "timeout": timeout})
    return {"max_fix_rounds": max_rounds, "block_on_severity": block_on, "gates": gates}


def render_config(gates: list[Gate]) -> str:
    lines = [
        "# supervise project config. Gates run in order: once at `start` (the baseline) and at every `verify`.",
        '# kind is one of test, lint, typecheck, build, other. Without a kind = "test" gate, runs are labelled',
        "# NO TESTS. Each run copies this file at `start`, so edits apply to the next run.",
        f"max_fix_rounds = {DEFAULT_MAX_FIX_ROUNDS}",
        f'block_on_severity = "{DEFAULT_BLOCK_ON}"  # review findings at or above this severity block accept',
    ]
    for gate in gates:
        lines += [
            "",
            "[[gates]]",
            f"name = {json.dumps(gate['name'])}",
            f"command = {json.dumps(gate['command'])}",
            f"kind = {json.dumps(gate['kind'])}",
            f"timeout = {gate.get('timeout', DEFAULT_TIMEOUT)}",
        ]
    if not gates:
        lines += [
            "",
            "# No gates were detected. Add one per check, for example:",
            "# [[gates]]",
            '# name = "test"',
            '# command = "make test"',
            '# kind = "test"',
        ]
    return "\n".join(lines) + "\n"


# --- detection ---------------------------------------------------------------------------------------------------


def _makefile_targets(root: Path) -> set[str]:
    for name in ("GNUmakefile", "makefile", "Makefile"):
        path = root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            return set(re.findall(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", text, re.M))
    return set()


def detect(root: Path) -> list[Gate]:
    """Guess gates from the Makefile, then package.json, then pyproject.toml; the first source of a kind wins."""
    found: dict[str, str] = {}

    targets = _makefile_targets(root)
    for kind in ("lint", "typecheck", "build", "test"):
        if kind in targets:
            found[kind] = f"make {kind}"
    if "test" not in found and "check" in targets:
        found["test"] = "make check"

    package = root / "package.json"
    if package.is_file():
        try:
            manifest = json.loads(package.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
        # The packageManager field ("pnpm@9.12.0", used by Corepack) names the tool; lockfiles are the fallback.
        declared = str(manifest.get("packageManager") or "").split("@", 1)[0].strip()
        runner = next(
            (
                pm
                for lock, pm in (
                    ("pnpm-lock.yaml", "pnpm"),
                    ("yarn.lock", "yarn"),
                    ("bun.lock", "bun"),
                    ("bun.lockb", "bun"),
                )
                if (root / lock).exists()
            ),
            "npm",
        )
        if declared in PACKAGE_MANAGERS:
            runner = declared
        for kind, names in (
            ("lint", ["lint"]),
            ("typecheck", ["typecheck", "type-check"]),
            ("build", ["build"]),
            ("test", ["test"]),
        ):
            script = next((n for n in names if isinstance(scripts.get(n), str)), None)
            if script is None or kind in found:
                continue
            if kind == "test" and "no test specified" in scripts[script]:
                continue
            found[kind] = f"{runner} run {script}"

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        try:
            tool = tomllib.loads(text).get("tool", {})
        except tomllib.TOMLDecodeError:
            tool = {}
        prefix = "uv run " if (root / "uv.lock").exists() else "poetry run " if (root / "poetry.lock").exists() else ""
        if "lint" not in found and "ruff" in tool:
            found["lint"] = f"{prefix}ruff check ."
        if "test" not in found and ("pytest" in tool or (root / "tests").is_dir() or "pytest" in text):
            found["test"] = f"{prefix}pytest" if prefix else "python3 -m pytest"

    order = ("lint", "typecheck", "build", "test")
    return [
        {"name": kind, "command": found[kind], "kind": kind, "timeout": DEFAULT_TIMEOUT}
        for kind in order
        if kind in found
    ]


# --- running -----------------------------------------------------------------------------------------------------

_NOISE = (
    (re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"), ""),
    (re.compile(r"\r"), ""),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"), "<timestamp>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:ns|us|µs|ms|s|sec|secs|seconds?|m|min|mins|minutes?|h)\b"), "<duration>"),
    (re.compile(r"\b0x[0-9a-fA-F]{6,}\b"), "0x<addr>"),
    (re.compile(r"\bpytest-\d+\b"), "pytest-<n>"),
)


def normalize(text: str) -> str:
    """Drop what changes between identical runs (durations, timestamps, addresses) before comparing output."""
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return "\n".join(line.rstrip() for line in text.splitlines())


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _run(root: Path, gate: Gate) -> tuple[int, str, float, bool]:
    started = time.monotonic()
    # Gates are shell commands from the project's own config, so shell=True is the point.
    proc = subprocess.Popen(  # noqa: S602
        gate["command"],
        shell=True,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def interrupted(signum: int, _frame: object) -> None:
        _kill_group(proc)
        raise SystemExit(128 + signum)

    handled = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.signal(sig, interrupted) for sig in handled}
    try:
        try:
            out, _ = proc.communicate(timeout=gate["timeout"])
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out, _ = proc.communicate()
            timed_out = True
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return proc.returncode, out.decode("utf-8", "replace"), time.monotonic() - started, timed_out


def run_gates(root: Path, gates: list[Gate], log_dir: Path, label: str, baseline: list[Gate] | None) -> list[Gate]:
    """Run every gate and classify it: pass, fail, or (verify only) baseline for an identical old failure."""
    log_dir.mkdir(parents=True, exist_ok=True)
    before = {g["name"]: g for g in baseline or []}
    results: list[Gate] = []
    for gate in gates:
        print(f"  running {gate['name']}: {gate['command']}", flush=True)
        code, output, seconds, timed_out = _run(root, gate)
        log = log_dir / f"{label}-{gate['name']}.log"
        log.write_text(output, encoding="utf-8")
        digest = hashlib.sha256(normalize(output).encode()).hexdigest()
        status = "pass" if code == 0 and not timed_out else "fail"
        old = before.get(gate["name"])
        unchanged = old is not None and old["exit_code"] == code and old["output_hash"] == digest
        if status == "fail" and unchanged and not timed_out and not old["timed_out"]:
            status = "baseline"
        results.append(
            {
                "name": gate["name"],
                "kind": gate["kind"],
                "command": gate["command"],
                "exit_code": code,
                "timed_out": timed_out,
                "seconds": round(seconds, 1),
                "status": status,
                "output_hash": digest,
                "tail": [line[:MAX_LINE] for line in output.splitlines()[-TAIL_LINES:]],
                "log": str(log),
            }
        )
    return results


def describe(result: Gate) -> str:
    """One line for a gate result."""
    tag = {"pass": "pass", "fail": "FAIL", "baseline": "same as baseline"}[result["status"]]
    extra = " (timed out)" if result["timed_out"] else f" (exit {result['exit_code']})"
    return f"{tag:<17} {result['name']:<10} {result['command']}{'' if result['status'] == 'pass' else extra}"
