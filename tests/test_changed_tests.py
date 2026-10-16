"""Exercise selection against real Git histories and subprocess exit codes."""

import json
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

    def test_source_runs_package_and_transitive_dependants(self):
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

    def test_deleted_test_expands_to_dependants(self):
        (self.root / "a/tests/test_app.py").unlink()
        self.assertEqual(set(self.plan()), {"a", "b", "guard"})

    def test_renamed_test_includes_old_owner(self):
        self.git("mv", "a/tests/test_app.py", "b/tests/test_moved.py")
        self.assertEqual(set(self.plan()), {"a", "b", "guard"})

    def test_assets_and_helpers_expand_selection(self):
        for path in [
            "a/tests/conftest.py",
            "a/tests/helpers.py",
            "a/tests/snapshot.json",
            "a/src/template.j2",
            "a/uv.lock",
        ]:
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

    def test_transitive_closure_handles_cycles(self):
        self.suites[0]["depends_on"] = ["b"]
        self.config()
        self.assertEqual(set(self.plan("--file", "a/src/app.py")), {"a", "b", "guard"})

    def test_overlapping_ownership_runs_every_consumer(self):
        self.suites[1]["paths"].append("a")
        self.config()
        self.assertIn("b", self.plan("--file", "a/tests/test_app.py"))

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

    def test_coverage_forces_whole_suites(self):
        self.write("a/tests/test_app.py", "# changed\n")
        self.assertEqual(set(self.plan("--", "--cov")), {"a", "b", "guard"})


if __name__ == "__main__":
    unittest.main()
