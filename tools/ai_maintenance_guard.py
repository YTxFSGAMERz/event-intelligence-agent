#!/usr/bin/env python3
"""Fail-closed policy gate for AI-generated maintenance diffs.

Only a small allowlist of app and test files may change.
This script never inspects or prints environment variables or secrets.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FILES = {
    "main.py",
    "opportunity_quality.py",
    "source_adapters.py",
    "page_verification.py",
    "dashboard/index.html",
}
MAX_FILES = 8
MAX_CHANGED_LINES = 700


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def report(message: str) -> None:
    print(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")


def main() -> int:
    status = run_git("status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode:
        report("Policy gate failed: could not inspect Git status.")
        return 1

    changed: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            report("Policy gate failed: malformed Git status output.")
            return 1
        state, path = line[:2], line[3:]
        if any(char not in (" ", "M") for char in state) or " -> " in path:
            report("Policy gate failed: only modifications to existing files are permitted.")
            return 1
        if not (path in ALLOWED_FILES or path.startswith("tests/")):
            report(f"Policy gate failed: change outside allowlist: {path}")
            return 1
        changed.append(path)

    changed = sorted(set(changed))
    if not changed:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write("changed=false\n")
        report("No source changes proposed; no pull request update is needed.")
        return 0

    if len(changed) > MAX_FILES:
        report(f"Policy gate failed: {len(changed)} files changed; limit is {MAX_FILES}.")
        return 1

    diff = run_git("diff", "--numstat", "HEAD", "--")
    if diff.returncode:
        report("Policy gate failed: could not inspect diff size.")
        return 1

    changed_lines = 0
    for line in diff.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            report("Policy gate failed: binary or unrecognized diff detected.")
            return 1
        added, removed, _path = parts
        if added == "-" or removed == "-":
            report("Policy gate failed: binary changes are not permitted.")
            return 1
        changed_lines += int(added) + int(removed)

    # Include newly created test files, which git diff --numstat omits.
    for path in changed:
        candidate = ROOT / path
        if candidate.is_file() and run_git("ls-files", "--error-unmatch", "--", path).returncode != 0:
            try:
                changed_lines += len(candidate.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeError):
                report(f"Policy gate failed: new test file is not readable text: {path}")
                return 1

    if changed_lines > MAX_CHANGED_LINES:
        report(
            f"Policy gate failed: {changed_lines} changed lines exceed "
            f"the {MAX_CHANGED_LINES}-line limit."
        )
        return 1

    whitespace = run_git("diff", "--check", "HEAD", "--")
    if whitespace.returncode:
        report("Policy gate failed: git diff --check detected whitespace errors.")
        return 1

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.write("changed=true\n")
    report(
        "Policy gate passed: "
        + str(len(changed))
        + " allowed file(s), "
        + str(changed_lines)
        + " changed line(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
