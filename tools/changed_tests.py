"""Run affected pytest packages, or individual changed test files.

Canonical source: firmfooting/.github tools/changed_tests.py.
Vendored copies deliberately work offline and have no third-party dependencies.
This is a local feedback command; full CI and pre-push gates remain mandatory.

Routes are checked, not trusted. The same file doubles as a pytest plugin
(`-p changed_tests`) that records every repository file or directory a suite
opens, and the runner fails a run when a suite read a path that its
`paths`, `depends_on` or `reads` do not route to it. Python-level reads are
covered; a subprocess, a bare `Path.exists()` and the like are not.
"""

# ruff: noqa: S603, S607, T201, INP001, RUF100, ANN001, ANN201, ANN202, PTH100, PTH118, PTH206, SIM102

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FULL_OPTIONS = ("--cov", "--check-finding-reachability")
AUDIT_ENV = "CHANGED_TESTS_AUDIT"
SKIP_PARTS = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}


@dataclass(frozen=True)
class Suite:
    name: str
    cwd: str
    paths: tuple[str, ...]
    tests: tuple[str, ...]
    command: tuple[str, ...]
    serial_args: tuple[str, ...]
    depends_on: tuple[str, ...]
    reads: tuple[str, ...]
    scanners: tuple[str, ...]
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
            reads=tuple(relative(p) for p in strings(entry.get("reads", []))),
            scanners=tuple(relative(p) for p in strings(entry.get("scanners", []))),
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


def join(cwd: str, path: str) -> str:
    return path if cwd == "." else f"{cwd}/{path}"


def test_dirs(suite: Suite) -> tuple[str, ...]:
    return tuple(join(suite.cwd, t) for t in suite.tests)


def is_test_name(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def imported_names(root: Path, suite: Suite) -> dict[str, set[str]]:
    """Map each module name a suite's Python files import to the files importing it."""
    found: dict[str, set[str]] = {}
    for directory in test_dirs(suite):
        for file in sorted((root / directory).rglob("*.py")):
            try:
                tree = ast.parse(file.read_bytes())
            except Exception:  # noqa: BLE001
                found.setdefault("*", set()).add(file.name)  # unreadable: assume imported
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [part for a in node.names for part in a.name.split(".")]
                elif isinstance(node, ast.ImportFrom):
                    names = [*(node.module or "").split("."), *(a.name for a in node.names)]
                else:
                    continue
                for name in names:
                    found.setdefault(name, set()).add(file.name)
    return found


def test_file(root: Path, path: str, suite: Suite, imports: dict[str, set[str]]) -> str | None:
    """Return the suite-relative path when a changed file is a test nothing else needs."""
    local = PurePosixPath(path).relative_to(suite.cwd) if suite.cwd != "." else PurePosixPath(path)
    name = local.name
    if (
        is_test_name(name)
        and any(under(path, d) for d in test_dirs(suite))
        and (root / path).is_file()
        # A test module another test imports can break its importer, so run them all.
        and not (imports.get(PurePosixPath(name).stem, set()) - {name})
        and "*" not in imports
    ):
        return str(local)
    return None


def select(root: Path, suites: list[Suite], paths: list[str], full: bool) -> dict[str, list[str]]:
    whole = {suite.name for suite in suites if suite.always or full}
    files: dict[str, set[str]] = {}
    imports: dict[str, dict[str, set[str]]] = {}
    for path in paths:
        owners = [suite for suite in suites if any(under(path, p) for p in suite.paths)]
        readers = [suite for suite in suites if any(under(path, p) for p in suite.reads)]
        if not owners and not readers:
            whole.update(suite.name for suite in suites)
            break
        whole.update(suite.name for suite in readers)
        for suite in owners:
            # A second consumer may own a path outside its own working directory.
            local = None
            if under(path, suite.cwd):
                if suite.name not in imports and any(under(path, d) for d in test_dirs(suite)):
                    imports[suite.name] = imported_names(root, suite)
                local = test_file(root, path, suite, imports.get(suite.name, {}))
            if local:
                files.setdefault(suite.name, set()).add(local)
                continue
            whole.add(suite.name)
            # Only source that is not a suite's own test may be read by another suite.
            if not any(under(path, d) for d in test_dirs(suite)):
                whole.update(s.name for s in suites if suite.name in s.depends_on)
    for suite in suites:
        if suite.name in files:
            # Tests that read other test modules must see every changed one.
            files[suite.name].update(
                s for s in suite.scanners if (root / join(suite.cwd, s)).is_file()
            )
    return {
        suite.name: list(suite.tests)
        if suite.name in whole
        else sorted(files[suite.name]) or list(suite.tests)
        for suite in suites
        if suite.name in whole or suite.name in files
    }


def violations(
    suites: list[Suite],
    suite: Suite,
    reads: list[str],
    scans: dict[str, list[str]],
):
    """Name each file `suite` opened that its declared routes do not select the suite for."""
    if suite.always:
        return []
    known = {join(suite.cwd, s) for s in suite.scanners}
    gaps: dict[str, list[str]] = {}
    for path in sorted(reads):
        owners = [s for s in suites if any(under(path, p) for p in s.paths)]
        if not owners or suite in owners or any(under(path, p) for p in suite.reads):
            continue
        if any(
            o.name in suite.depends_on and not any(under(path, d) for d in test_dirs(o))
            for o in owners
        ):
            continue
        gaps.setdefault(", ".join(o.name for o in owners), []).append(path)
    found = [
        f"{suite.name} reads {len(paths)} paths owned by {names} (e.g. {paths[0]}): "
        "add depends_on or reads"
        for names, paths in sorted(gaps.items())
    ]
    found += [
        f"{suite.name}: {reader} reads {len(others)} other test modules (e.g. {others[0]}): "
        "list it in scanners"
        for reader, others in sorted(scans.items())
        if reader not in known
    ]
    return found


def collect_audit(suites: list[Suite], suite: Suite, directory: Path) -> list[str]:
    reads: set[str] = set()
    scans: dict[str, set[str]] = {}
    for record in directory.glob("*.json"):
        data = json.loads(record.read_text(encoding="utf-8"))
        reads.update(data["reads"])
        for reader, other in data["during_test"]:
            if is_test_name(PurePosixPath(other).name):
                scans.setdefault(reader, set()).add(other)
    directories = test_dirs(suite)
    inside = {
        reader: sorted(o for o in others if o != reader and any(under(o, d) for d in directories))
        for reader, others in scans.items()
    }
    return violations(suites, suite, sorted(reads), {k: v for k, v in inside.items() if v})


def is_pytest(command: tuple[str, ...]) -> bool:
    return any(Path(part).name == "pytest" for part in command)


def verify(root: Path, config: Path) -> int:
    pin = json.loads(config.read_text(encoding="utf-8")).get("runner_sha256")
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    load_suites(config)
    if digest != pin:
        print(f"Vendored runner {digest} does not match the pinned {pin}; re-sync it.")
        return 1
    return 0


# pytest plugin half (`-p changed_tests`): records opened repository paths.
_state: dict = {"current": None}


def _hook_for(root: str, reads: set[str], during: set[tuple[str, str]]):
    prefix = root + os.sep

    def hook(event: str, args: tuple) -> None:
        if event not in ("open", "os.listdir", "os.scandir"):
            return
        target = args[0] if args else None
        if not isinstance(target, (str, bytes, os.PathLike)):
            return
        if event == "open" and len(args) > 2 and isinstance(args[2], int):
            if args[2] & (os.O_WRONLY | os.O_RDWR):
                return
        full = os.path.abspath(os.fsdecode(target))
        if not full.startswith(prefix) or SKIP_PARTS.intersection(full.split(os.sep)):
            return
        rel = full[len(prefix) :].replace(os.sep, "/")
        reads.add(rel)
        if _state["current"] and rel != _state["current"]:
            during.add((_state["current"], rel))

    return hook


def pytest_configure(config) -> None:
    directory, root = os.environ.get(AUDIT_ENV), os.environ.get("CHANGED_TESTS_ROOT")
    if not directory or not root:
        return
    _state.update(directory=directory, root=os.path.abspath(root), rootdir=str(config.rootpath))
    _state.update(reads=set(), during=set())
    sys.addaudithook(_hook_for(_state["root"], _state["reads"], _state["during"]))


def pytest_runtest_logstart(nodeid: str, location) -> None:
    root = _state.get("root")
    if root:
        absolute = os.path.abspath(os.path.join(_state["rootdir"], location[0]))
        _state["current"] = os.path.relpath(absolute, root).replace(os.sep, "/")
        if _state["current"].startswith(".."):
            _state["current"] = None


def pytest_runtest_logfinish(nodeid: str, location) -> None:
    _state["current"] = None


def pytest_unconfigure(config) -> None:
    if _state.get("directory"):
        data = {"reads": sorted(_state["reads"]), "during_test": sorted(_state["during"])}
        target = Path(_state["directory"]) / f"{os.getpid()}-{uuid.uuid4().hex}.json"
        target.write_text(json.dumps(data), encoding="utf-8")


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
    parser.add_argument("--no-audit", action="store_true", help="Skip the route read audit")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check this file's SHA-256 against the pin in the configuration",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest arguments after --",
    )
    args = parser.parse_args()
    try:
        root = Path(os.fsdecode(git(Path.cwd(), "rev-parse", "--show-toplevel")).strip())
        if args.verify:
            return verify(root, root / args.config)
        suites = load_suites(root / args.config)
        extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
        if any(arg.startswith(FULL_OPTIONS) for arg in extra):
            # Each suite runs in its own process and would overwrite the others' results.
            print(
                "Coverage and reachability need one process over the whole suite; "
                "use the repository's full-suite command.",
                file=sys.stderr,
            )
            return 2
        full = args.full
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
        by_name = {s.name: s for s in suites}
        for run in runs:
            print(f"[{run['suite']}] {json.dumps(run['command'])}", flush=True)
            suite = by_name[run["suite"]]
            audit = not args.no_audit and is_pytest(suite.command)
            with tempfile.TemporaryDirectory() as scratch:
                command, env = run["command"], dict(os.environ)
                if audit:
                    command = [*command, "-p", "changed_tests"]
                    env[AUDIT_ENV], env["CHANGED_TESTS_ROOT"] = scratch, str(root)
                    env["PYTHONPATH"] = os.pathsep.join(
                        [
                            str(Path(__file__).resolve().parent),
                            *filter(None, [env.get("PYTHONPATH")]),
                        ]
                    )
                try:
                    code = subprocess.run(
                        command, cwd=root / run["cwd"], env=env, check=False
                    ).returncode
                except OSError as exc:
                    print(str(exc), file=sys.stderr)
                    code = 127
                if code == 5 and extra:
                    code = 0  # extra args such as -k may legitimately filter a suite to nothing
                problems = collect_audit(suites, suite, Path(scratch)) if audit else []
            for problem in problems:
                print(f"Route gap: {problem}", file=sys.stderr)
            code = code or (1 if problems else 0)
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
