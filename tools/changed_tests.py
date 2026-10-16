"""Run affected pytest packages, or individual changed test files.

Canonical source: firmfooting/.github tools/changed_tests.py.
Vendored copies deliberately work offline and have no third-party dependencies.
This is a local feedback command; full CI and pre-push gates remain mandatory.
"""

# ruff: noqa: S603, S607, T201, INP001, RUF100

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FULL_OPTIONS = ("--cov", "--check-finding-reachability")


@dataclass(frozen=True)
class Suite:
    name: str
    cwd: str
    paths: tuple[str, ...]
    tests: tuple[str, ...]
    command: tuple[str, ...]
    serial_args: tuple[str, ...]
    depends_on: tuple[str, ...]
    always: bool


def strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected an array of strings")
    return tuple(value)


def relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"Expected a repository-relative POSIX path: {value!r}")
    return str(path)


def load_suites(path: Path) -> list[Suite]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not data.get("suites"):
        raise ValueError("Expected version 1 and a non-empty suites array")
    suites = []
    for entry in data["suites"]:
        suite = Suite(
            name=entry["name"],
            cwd=relative(entry.get("cwd", ".")),
            paths=tuple(relative(p) for p in strings(entry.get("paths", []))),
            tests=tuple(relative(p) for p in strings(entry.get("tests", []))),
            command=strings(entry["command"]),
            serial_args=strings(entry.get("serial_args", [])),
            depends_on=strings(entry.get("depends_on", [])),
            always=entry.get("always", False),
        )
        if not suite.name or not suite.command or not isinstance(suite.always, bool):
            raise ValueError("Each suite needs a name, a command and a boolean always flag")
        suites.append(suite)
    names = {suite.name for suite in suites}
    if len(names) != len(suites):
        raise ValueError("Suite names must be unique")
    for suite in suites:
        if set(suite.depends_on) - names:
            raise ValueError(f"Unknown dependency in {suite.name}")
    return suites


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)


def changed_files(root: Path, base: str) -> list[str]:
    commit = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")
    ancestor = git(root, "merge-base", "HEAD", os.fsdecode(commit).strip()).decode().strip()
    # Separate diffs retain staged edits even when the worktree undoes them.
    outputs = [
        git(root, "diff", "--name-only", "--no-renames", "-z", ancestor, "HEAD", "--"),
        git(root, "diff", "--name-only", "--no-renames", "-z", "--cached", "--"),
        git(root, "diff", "--name-only", "--no-renames", "-z", "--"),
        git(root, "ls-files", "--others", "--exclude-standard", "-z"),
    ]
    return sorted({os.fsdecode(p) for output in outputs for p in output.split(b"\0") if p})


def under(path: str, prefix: str) -> bool:
    return prefix in (".", path) or path.startswith(prefix + "/")


def test_file(root: Path, path: str, suite: Suite) -> str | None:
    local = PurePosixPath(path).relative_to(suite.cwd) if suite.cwd != "." else PurePosixPath(path)
    name = local.name
    if (
        name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
        and any(under(str(local), p) for p in suite.tests)
        and (root / path).is_file()
    ):
        return str(local)
    return None


def select(root: Path, suites: list[Suite], paths: list[str], full: bool) -> dict[str, list[str]]:
    whole = {suite.name for suite in suites if suite.always or full}
    files: dict[str, set[str]] = {}
    for path in paths:
        owners = [suite for suite in suites if any(under(path, p) for p in suite.paths)]
        if not owners:
            whole.update(suite.name for suite in suites)
            break
        for suite in owners:
            # A second consumer may own a path outside its own working directory.
            local = test_file(root, path, suite) if under(path, suite.cwd) else None
            if local:
                files.setdefault(suite.name, set()).add(local)
            else:
                whole.add(suite.name)
    while True:
        dependants = {suite.name for suite in suites if whole.intersection(suite.depends_on)}
        if dependants <= whole:
            break
        whole.update(dependants)
    return {
        suite.name: list(suite.tests) if suite.name in whole else sorted(files[suite.name])
        for suite in suites
        if suite.name in whole or suite.name in files
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="HEAD",
        help="Compare the branch merge base plus all local edits",
    )
    parser.add_argument(
        "--file",
        action="append",
        help="Select using an explicit path instead of Git (repeatable)",
    )
    parser.add_argument("--full", action="store_true", help="Run every configured suite")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan as JSON without running tests",
    )
    parser.add_argument("--config", default=".github/changed-tests.json")
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest arguments after --",
    )
    args = parser.parse_args()
    try:
        root = Path(os.fsdecode(git(Path.cwd(), "rev-parse", "--show-toplevel")).strip())
        suites = load_suites(root / args.config)
        extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
        coverage_requested = any(arg.startswith(FULL_OPTIONS) for arg in extra)
        full = args.full or coverage_requested
        reason = "explicit full run" if full else "affected packages and changed test files"
        try:
            explicit = args.file
            paths = [relative(p) for p in explicit] if explicit else changed_files(root, args.base)
        except subprocess.CalledProcessError:
            paths, full, reason = [], True, "Git comparison failed; running all suites"
        selected = select(root, suites, paths, full)
        runs = [
            {
                "suite": s.name,
                "cwd": s.cwd,
                "targets": selected[s.name],
                "command": [
                    *s.command,
                    *(s.serial_args if selected[s.name] != list(s.tests) else ()),
                    *selected[s.name],
                    *extra,
                ],
            }
            for s in suites
            if s.name in selected
        ]
        if args.dry_run:
            print(json.dumps({"reason": reason, "changed": paths, "runs": runs}, indent=2))
            return 0
        print(reason, flush=True)
        status = 0
        for run in runs:
            print(f"[{run['suite']}] {json.dumps(run['command'])}", flush=True)
            try:
                code = subprocess.run(run["command"], cwd=root / run["cwd"], check=False).returncode
            except OSError as exc:
                print(str(exc), file=sys.stderr)
                code = 127
            if code and not status:
                status = code if code > 0 else 128 - code
        if not runs:
            message = "No changed files; no tests selected."
            print(message, "Use --base origin/main for a branch or --full.")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Cannot plan changed tests: {exc}", file=sys.stderr)
        return 2
    else:
        return status


if __name__ == "__main__":
    sys.exit(main())
