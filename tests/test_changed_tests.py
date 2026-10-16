"""Exercise selection against real Git histories and subprocess exit codes."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "tools/changed_tests.py"


class ChangedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "Tests")
        for path in ["a/src/app.py", "a/tests/test_app.py", "b/tests/test_b.py"]:
            self.write(path, "# baseline\n")
        self.suites = [
            dict(
                name="a",
                cwd="a",
                paths=["a"],
                tests=["tests"],
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            ),
            dict(
                name="b",
                cwd="b",
                paths=["b"],
                tests=["tests"],
                depends_on=["a"],
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            ),
            dict(
                name="guard",
                cwd=".",
                paths=[],
                tests=[],
                always=True,
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            ),
        ]
        self.config()
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    def config(self):
        self.write(".github/changed-tests.json", json.dumps(dict(version=1, suites=self.suites)))

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(RUNNER), *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def plan(self, *args):
        result = self.run_tool("--dry-run", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {item["suite"]: item for item in json.loads(result.stdout)["runs"]}

    def test_no_changes_runs_only_guard(self):
        self.assertEqual(set(self.plan()), {"guard"})

    def test_source_runs_package_and_direct_dependants(self):
        self.write("a/src/app.py", "# changed\n")
        self.assertEqual(set(self.plan()), {"a", "b", "guard"})
        self.assertEqual(self.plan()["a"]["targets"], ["tests"])

    def test_changed_test_runs_only_that_file_and_guard(self):
        self.write("a/tests/test_app.py", "# changed\n")
        plan = self.plan()
        self.assertEqual(set(plan), {"a", "guard"})
        self.assertEqual(plan["a"]["targets"], ["tests/test_app.py"])

    def test_new_test_is_included(self):
        self.write("a/tests/test_new.py", "# new\n")
        self.assertEqual(self.plan()["a"]["targets"], ["tests/test_new.py"])

    def test_deleted_test_runs_its_suite_only(self):
        (self.root / "a/tests/test_app.py").unlink()
        self.assertEqual(set(self.plan()), {"a", "guard"})

    def test_renamed_test_includes_old_owner(self):
        self.git("mv", "a/tests/test_app.py", "b/tests/test_moved.py")
        self.assertEqual(set(self.plan()), {"a", "b", "guard"})

    def test_assets_and_helpers_expand_selection(self):
        for path in ["a/tests/conftest.py", "a/tests/helpers.py", "a/tests/snapshot.json"]:
            with self.subTest(path=path):
                plan = self.plan("--file", path)
                self.assertEqual(set(plan), {"a", "guard"})
                self.assertEqual(plan["a"]["targets"], ["tests"])
        for path in ["a/src/template.j2", "a/uv.lock"]:
            with self.subTest(path=path):
                self.assertEqual(set(self.plan("--file", path)), {"a", "b", "guard"})

    def test_unknown_path_and_root_configuration_run_everything(self):
        for path in ["pyproject.toml", "conftest.py", ".pre-commit-config.yaml", "docs/example.md"]:
            with self.subTest(path=path):
                self.assertEqual(set(self.plan("--file", path)), {"a", "b", "guard"})

    def test_missing_base_runs_everything(self):
        self.assertEqual(set(self.plan("--base", "not-a-ref")), {"a", "b", "guard"})

    def test_full_runs_everything(self):
        self.assertEqual(set(self.plan("--full")), {"a", "b", "guard"})

    def test_committed_branch_changes_compare_merge_base(self):
        base = self.git("rev-parse", "HEAD")
        self.write("a/src/app.py", "# committed\n")
        self.git("add", "a/src/app.py")
        self.git("commit", "-qm", "change")
        self.assertEqual(set(self.plan("--base", base)), {"a", "b", "guard"})

    def test_staged_change_is_not_hidden_by_worktree_reversion(self):
        self.write("a/src/app.py", "# staged\n")
        self.git("add", "a/src/app.py")
        self.write("a/src/app.py", "# baseline\n")
        self.assertIn("a", self.plan())

    def test_similarly_named_folder_is_not_a_match(self):
        self.assertEqual(set(self.plan("--file", "ab/test.py")), {"a", "b", "guard"})

    def test_dependencies_are_direct_not_transitive(self):
        self.suites.append(dict(name="c", cwd="c", paths=["c"], tests=["tests"], depends_on=["b"], command=["true"]))
        self.config()
        self.assertEqual(set(self.plan("--file", "a/src/app.py")), {"a", "b", "guard"})

    def test_dependencies_handle_cycles(self):
        self.suites[0]["depends_on"] = ["b"]
        self.config()
        self.assertEqual(set(self.plan("--file", "a/src/app.py")), {"a", "b", "guard"})

    def test_overlapping_ownership_runs_every_consumer(self):
        self.suites[1]["paths"].append("a")
        self.config()
        self.assertIn("b", self.plan("--file", "a/tests/test_app.py"))

    def test_importer_of_a_changed_test_module_runs_the_suite(self):
        self.write("a/tests/test_importer.py", "from test_app import helper\n")
        self.git("add", ".")
        self.git("commit", "-qm", "importer")
        self.write("a/tests/test_app.py", "# changed\n")
        self.assertEqual(self.plan()["a"]["targets"], ["tests"])
        self.write("a/tests/test_importer.py", "import os\n")
        self.assertEqual(self.plan("--file", "a/tests/test_app.py")["a"]["targets"], ["tests/test_app.py"])

    def test_scanners_accompany_a_changed_test(self):
        self.write("a/tests/test_scan.py", "# scanner\n")
        self.suites[0]["scanners"] = ["tests/test_scan.py", "tests/test_absent.py"]
        self.config()
        targets = self.plan("--file", "a/tests/test_app.py")["a"]["targets"]
        self.assertEqual(targets, ["tests/test_app.py", "tests/test_scan.py"])

    def test_reads_route_a_path_to_the_reader(self):
        self.suites[1]["reads"] = ["a/tests/data"]
        self.config()
        plan = self.plan("--file", "a/tests/data/x.json")
        self.assertEqual(set(plan), {"a", "b", "guard"})

    def test_filtered_run_with_no_tests_is_success(self):
        self.suites[0]["command"] = [sys.executable, "-c", "import sys; sys.exit(5)"]
        self.config()
        self.assertEqual(self.run_tool("--full", "--", "-k", "x").returncode, 0)

    def test_pin_mismatch_fails_verify(self):
        shutil.copy(RUNNER, self.root / ".github/changed_tests.py")
        digest = hashlib.sha256(RUNNER.read_bytes()).hexdigest()
        self.write(
            ".github/changed-tests.json",
            json.dumps(dict(version=1, runner_sha256=digest, suites=self.suites)),
        )
        verify = [sys.executable, str(self.root / ".github/changed_tests.py"), "--verify"]
        run = lambda: subprocess.run(verify, cwd=self.root, capture_output=True, check=False)  # noqa: E731
        self.assertEqual(run().returncode, 0)
        with (self.root / ".github/changed_tests.py").open("a") as handle:
            handle.write("# drift\n")
        self.assertEqual(run().returncode, 1)

    def test_audit_plugin_records_reads_and_scans_of_test_modules(self):
        self.write("a/src/data.txt", "x")
        script = (
            "import os, sys, types, pathlib\n"
            f"sys.path.insert(0, {str(RUNNER.parent)!r})\n"
            "import changed_tests as ct\n"
            f"root = {str(self.root)!r}\n"
            "ct.pytest_configure(types.SimpleNamespace(rootpath=pathlib.Path(root)))\n"
            "ct.pytest_runtest_logstart('n', ('a/tests/test_app.py', 0, 'n'))\n"
            "open(root + '/a/src/data.txt').read()\n"
            "open(root + '/b/tests/test_b.py').read()\n"
            "open(root + '/a/src/out.txt', 'w').write('w')\n"
            "os.listdir(root + '/a/src')\n"
            "ct.pytest_runtest_logfinish('n', None)\n"
            "ct.pytest_unconfigure(None)\n"
        )
        scratch = self.root / "audit"
        scratch.mkdir()
        env = {**os.environ, "CHANGED_TESTS_AUDIT": str(scratch), "CHANGED_TESTS_ROOT": str(self.root)}
        subprocess.run([sys.executable, "-c", script], env=env, check=True)
        data = json.loads(next(scratch.glob("*.json")).read_text())
        self.assertIn("a/src/data.txt", data["reads"])
        self.assertIn("a/src", data["reads"])
        self.assertNotIn("a/src/out.txt", data["reads"])
        self.assertIn(["a/tests/test_app.py", "b/tests/test_b.py"], data["during_test"])

    def test_route_gaps_are_reported(self):
        spec = importlib.util.spec_from_file_location("changed_tests", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        a, b = module.load_suites(self.root / ".github/changed-tests.json")[:2]
        suites = [a, b]
        self.assertEqual(module.violations(suites, b, ["a/src/app.py", "b/tests/x.py"], {}), [])
        self.assertEqual(module.violations(suites, a, ["b/src/app.py"], {}).__len__(), 1)
        self.assertEqual(module.violations(suites, b, ["a/tests/test_app.py"], {}).__len__(), 1)
        scans = {"a/tests/test_app.py": ["a/tests/test_z.py"]}
        self.assertEqual(len(module.violations(suites, a, [], scans)), 1)
        a = module.Suite(**{**a.__dict__, "scanners": ("tests/test_app.py",)})
        self.assertEqual(module.violations(suites, a, [], scans), [])

    def test_runner_parses_on_older_pythons(self):
        # Vendored copies run on whichever Python each repository's CI uses.
        import ast

        ast.parse(RUNNER.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_coverage_is_refused(self):
        for arg in ["--cov", "--cov-report=xml", "--check-finding-reachability"]:
            with self.subTest(arg=arg):
                self.assertEqual(self.run_tool("--dry-run", "--", arg).returncode, 2)

    def test_failure_and_no_tests_exit_codes_propagate(self):
        for status in [1, 4, 5]:
            with self.subTest(status=status):
                self.suites[0]["command"] = [
                    sys.executable,
                    "-c",
                    f"import sys; sys.exit({status})",
                ]
                self.config()
                self.assertEqual(self.run_tool("--full").returncode, status)

    def test_filenames_with_spaces_are_single_arguments(self):
        self.write("a/tests/test_space name.py", "# new\n")
        self.assertEqual(self.plan()["a"]["targets"], ["tests/test_space name.py"])

    def test_duplicate_names_and_unknown_dependencies_are_rejected(self):
        self.suites[1]["depends_on"] = ["typo"]
        self.config()
        self.assertNotEqual(self.run_tool("--dry-run").returncode, 0)
        self.suites[1]["depends_on"] = []
        self.suites[1]["name"] = "a"
        self.config()
        self.assertNotEqual(self.run_tool("--dry-run").returncode, 0)

    def test_path_escape_is_rejected(self):
        self.assertNotEqual(self.run_tool("--file", "../outside.py").returncode, 0)


if __name__ == "__main__":
    unittest.main()
