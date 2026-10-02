"""Project discovery, run state on disk, the write lock, and working-tree fingerprints."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

State = dict[str, Any]

STATE_DIR = ".supervise"
EVENTS = "events.jsonl"


class Refused(Exception):
    """A command that breaks a rule. The CLI prints it and exits 2 without changing state."""

    def __init__(self, rule: str, detail: str = "", next_step: str = "") -> None:
        super().__init__(rule)
        self.rule = rule
        self.detail = detail
        self.next_step = next_step


class SuperviseError(Exception):
    """Something outside the rules went wrong (git failed, state unreadable)."""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SuperviseError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def write_atomic(path: Path, text: str) -> None:
    """Write to a temporary file in the same directory, fsync it, then rename it over `path`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    """Hook events of a run, one per line. Unreadable lines keep their slot so offsets stay stable."""
    path = run_dir / EVENTS
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        data = fh.read()
    events: list[dict[str, Any]] = []
    for line in data.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        events.append(event if isinstance(event, dict) else {"event": "unreadable"})
    return events


class Project:
    """A git repository with a `.supervise/` directory at its top level."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / STATE_DIR
        self.config_path = self.dir / "config.toml"
        self.runs = self.dir / "runs"
        self.current_file = self.dir / "current"

    @classmethod
    def find(cls, cwd: Path | None = None) -> Project:
        here = cwd or Path.cwd()
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here, capture_output=True, text=True)
        if proc.returncode != 0:
            raise Refused(
                "supervise needs a git repository",
                f"{here} is not inside one.",
                "run `git init` in the project, then `supervisectl init`",
            )
        root = Path(proc.stdout.strip()).resolve()
        if root == Path.home().resolve():
            raise Refused(
                "the repository root is your home directory",
                f"{root} is $HOME; supervise would fingerprint and diff everything under it.",
                "run supervise inside a project repository",
            )
        return cls(root)

    # --- runs ----------------------------------------------------------------------------------------------------

    def current_id(self) -> str | None:
        try:
            return self.current_file.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    def set_current(self, run_id: str | None) -> None:
        if run_id is None:
            self.current_file.unlink(missing_ok=True)
        else:
            write_atomic(self.current_file, run_id + "\n")

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def latest_run_id(self) -> str | None:
        if not self.runs.is_dir():
            return None
        ids = sorted(p.name for p in self.runs.iterdir() if (p / "state.json").is_file())
        return ids[-1] if ids else None

    def load(self, run_id: str) -> State:
        path = self.run_dir(run_id) / "state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SuperviseError(f"run {run_id} has no state file at {path}") from None
        except ValueError as exc:
            raise SuperviseError(f"{path} is not valid JSON: {exc}") from None
        if not isinstance(state, dict):
            raise SuperviseError(f"{path} does not hold a run state object")
        return state

    def save(self, state: State) -> None:
        state["updated_at"] = now()
        text = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
        write_atomic(self.run_dir(state["run_id"]) / "state.json", text)

    @contextmanager
    def lock(self, command: str) -> Iterator[None]:
        """Exclusive, non-blocking write lock for the whole `.supervise/` directory.

        A second writer is refused instead of waiting, so a long `verify` never hides a concurrent command.
        Readers (`status`, `report`) do not take it: state files are only ever replaced atomically.
        """
        self.dir.mkdir(exist_ok=True)
        fd = os.open(self.dir / "lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = os.pread(fd, 200, 0).decode("utf-8", "replace").strip() or "another process"
                raise Refused(
                    "another supervisectl command is running in this project",
                    f"the state lock is held by {holder}; nothing was changed.",
                    "wait for it to finish, then run this command again",
                ) from None
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"pid {os.getpid()} ({command})".encode(), 0)
            yield
        finally:
            os.close(fd)

    # --- working tree --------------------------------------------------------------------------------------------

    def fingerprint(self) -> str:
        """Tree id of the working tree as `git add -A` would stage it, without touching the real index.

        Ignored files and `.supervise/` are not part of it. Starting from a copy of the real index keeps
        git's stat cache, so only changed files are hashed.
        """
        sparse = subprocess.run(
            ["git", "config", "--bool", "--get", "core.sparseCheckout"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if sparse.returncode not in (0, 1):
            raise SuperviseError(f"cannot read sparse-checkout config: {sparse.stderr.strip()}")
        if sparse.stdout.strip() == "true":
            raise Refused(
                "sparse checkout is not supported",
                "Files outside the sparse checkout are absent, so this is not a complete working tree.",
                "use a full checkout (`git sparse-checkout disable`), then retry",
            )
        # These flags survive an index copy and make `git add -A` overlook real content changes.
        entries = git(self.root, "ls-files", "-v", "-z").split("\0")
        hidden = [entry[2:] for entry in entries if entry and (entry[0].islower() or entry[0] == "S")]
        if hidden:
            raise Refused(
                "git index flags hide tracked files from verification",
                f"assume-unchanged or skip-worktree: {', '.join(hidden[:10])}",
                "clear the relevant flags with `git update-index --no-assume-unchanged -- <files>` and/or "
                "`git update-index --no-skip-worktree -- <files>`, then retry",
            )
        index = Path(git(self.root, "rev-parse", "--git-path", "index").strip())
        if not index.is_absolute():
            index = self.root / index
        self.dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.dir, prefix="index-") as tmp:
            tmp_index = Path(tmp) / "index"
            if index.is_file():
                shutil.copyfile(index, tmp_index)
            env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
            git(self.root, "add", "-A", env=env)
            git(self.root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", STATE_DIR, env=env)
            return git(self.root, "write-tree", env=env).strip()

    def head(self) -> str | None:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=self.root, capture_output=True, text=True
        )
        return proc.stdout.strip() or None

    def changed_files(self, old: str, new: str) -> list[str]:
        out = git(self.root, "diff", "--no-ext-diff", "--name-status", old, new)
        return [line.replace("\t", " ") for line in out.splitlines() if line]

    def diff_size(self, old: str, new: str) -> tuple[int, int]:
        """Lines added plus removed between two trees, and the number of binary files changed."""
        lines = binary = 0
        for row in git(self.root, "diff", "--no-ext-diff", "--numstat", old, new).splitlines():
            added, removed, _ = row.split("\t", 2)
            if added == "-" or removed == "-":
                binary += 1
            else:
                lines += int(added) + int(removed)
        return lines, binary

    def write_diff(self, old: str, new: str, path: Path) -> None:
        path.write_text(git(self.root, "diff", "--no-ext-diff", "--no-color", old, new), encoding="utf-8")
