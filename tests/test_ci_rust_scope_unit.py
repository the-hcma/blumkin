"""The Rust CI job runs only when Rust-related files change (#570).

`.github/ci/rust-changed` decides from the list of changed files; ci.yml feeds it the pull
request's files and runs the macOS job only when it says so. Other events (merge queue,
schedule, manual run, a push with no pull request) always run it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / ".github" / "ci" / "rust-changed"
_WORKFLOW = (_ROOT / ".github" / "workflows" / "ci.yml").read_text()


def _decide(files: list[str], *, max_files: int | None = None) -> str:
    env = {"PATH": os.environ.get("PATH", "")}
    if max_files is not None:
        env["RUST_CHANGED_MAX_FILES"] = str(max_files)
    done = subprocess.run(
        ["bash", str(_SCRIPT)],
        input="\n".join(files) + ("\n" if files else ""),
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _job(name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", _WORKFLOW, re.M | re.S
    )
    assert match, name
    return match.group(1)


@pytest.mark.parametrize(
    "files",
    [
        ["rust-agent/src/main.rs"],
        ["README.md", "rust-agent/Cargo.toml"],
        ["rust-agent/Cargo.lock"],
        ["hatch_build.py"],
        [".github/ci/rust-static"],
        [".github/ci/rust-changed"],
        [".github/workflows/ci.yml"],
        ["pyproject.toml"],
        ["rust-toolchain.toml"],
        ["rust-toolchain"],
        [".cargo/config.toml"],
    ],
)
def test_a_change_that_can_affect_the_rust_build_runs_it(files: list[str]) -> None:
    assert _decide(files) == "true"


@pytest.mark.parametrize(
    "files",
    [
        ["docs/agent-integration.md"],
        ["src/blumkin/cli.py", "tests/test_cli_usage.py"],
        ["scripts/dev/blumkin", ".agents/skills/blumkin-dev/SKILL.md"],
        [".github/workflows/release-please.yml"],
        ["rust-agent-notes.md"],
    ],
)
def test_a_docs_python_or_script_change_does_not(files: list[str]) -> None:
    assert _decide(files) == "false"


def test_an_empty_or_unreadable_file_list_fails_safe_to_running_it() -> None:
    assert _decide([]) == "true"
    assert _decide(["", "", ""]) == "true"


def test_a_list_near_the_api_cap_fails_safe_because_it_may_be_truncated() -> None:
    """GitHub caps the files of a pull request or comparison at 3000 without an error."""
    big = [f"docs/page-{n}.md" for n in range(2900)]
    assert _decide(big) == "true"
    assert _decide(big[:2000]) == "false"


def test_the_caller_can_lower_the_trusted_list_length() -> None:
    """A comparison returns at most 300 files, so the merge queue sets a lower bound (#570)."""
    files = [f"docs/page-{n}.md" for n in range(260)]
    assert _decide(files) == "false"
    assert _decide(files, max_files=250) == "true"
    assert _decide(files[:100], max_files=250) == "false"


def test_the_merge_queue_comparison_uses_the_lower_bound() -> None:
    guard = _job("guard")
    step = guard.split("id: rust-scope", 1)[1]
    assert "RUST_CHANGED_MAX_FILES=250" in step
    # The pull-request listing keeps the script's default (its cap is 3000 files).
    assert step.count("RUST_CHANGED_MAX_FILES") == 1


def test_a_failing_or_skipped_guard_runs_the_rust_job_instead_of_skipping_it() -> None:
    """A skipped required check passes branch protection, so a broken guard must not skip it."""
    job = _job("rust-static")
    assert "always()" in job
    assert "needs.guard.result != 'success'" in job


def test_the_guard_job_publishes_rust_changed_from_the_pull_request_files() -> None:
    guard = _job("guard")
    assert "rust_changed: ${{ steps.rust-scope.outputs.rust_changed }}" in guard
    assert "id: rust-scope" in guard
    assert "pulls/$PR_NUMBER/files" in guard
    # A file renamed out of rust-agent/ shows up under its old name too.
    assert guard.count("previous_filename") >= 2
    assert ".github/ci/rust-changed" in guard
    # With neither a pull request nor a merge-queue entry (schedule, manual, push) it always runs.
    step = guard.split("id: rust-scope", 1)[1]
    assert re.search(r'else\n\s+echo "rust_changed=true" >> "\$GITHUB_OUTPUT"\n\s+exit 0', step)


def test_the_merge_queue_is_scoped_by_the_queue_entrys_own_diff() -> None:
    """The queue re-runs required checks; without this it would still build Rust every time."""
    guard = _job("guard")
    assert "github.event.merge_group.base_sha" in guard
    assert "github.event.merge_group.head_sha" in guard
    assert "compare/$BASE_SHA...$HEAD_SHA" in guard


def test_the_rust_job_is_gated_by_its_own_if_not_a_paths_filter() -> None:
    """A required check skipped by `if` counts as passing; a `paths:` filter would hang it."""
    job = _job("rust-static")
    assert "needs.guard.outputs.rust_changed == 'true'" in job
    assert "needs.guard.outputs.skip != 'true'" in job
    assert "paths:" not in _WORKFLOW.split("jobs:", 1)[0].split("on:", 1)[1]


def test_the_script_is_an_executable_bash_script_without_an_extension() -> None:
    assert _SCRIPT.is_file() and _SCRIPT.stat().st_mode & 0o111
    assert _SCRIPT.read_text().splitlines()[0] == "#!/usr/bin/env bash"


def test_the_stale_not_a_required_check_comment_is_gone() -> None:
    assert "Not (yet) a required check" not in _WORKFLOW
    assert "Rust lint & test (blumkin-agent)" in _WORKFLOW
