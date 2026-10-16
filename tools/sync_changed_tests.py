"""Update or check the vendored runner in existing consumer checkouts."""

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repositories", nargs="+", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).with_name("changed_tests.py").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    status = 0
    for repo in args.repositories:
        config = repo / ".github/changed-tests.json"
        runner = repo / ".github/changed_tests.py"
        data = json.loads(config.read_text(encoding="utf-8"))
        if args.check:
            if runner.read_bytes() != source or data.get("runner_sha256") != digest:
                print(f"Runner differs: {repo}")
                status = 1
        else:
            runner.write_bytes(source)
            data["runner_sha256"] = digest
            config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
