"""The Rust CI job runs only when Rust-related files change (#570).

`.github/ci/rust-changed` decides from the list of changed files; ci.yml feeds it the pull
request's files and runs the macOS job only when it says so. Other events (merge queue,
schedule, manual run, a push with no pull request) always run it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / ".github" / "ci" / "rust-changed"
_WORKFLOW = (_ROOT / ".github" / "workflows" / "ci.yml").read_text()


def _decide(files: list[str]) -> str:
    done = subprocess.run(
        ["bash", str(_SCRIPT)],
        input="\n".join(files) + ("\n" if files else ""),
        capture_output=True,
        check=False,
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


def test_the_script_is_an_executable_bash_script_without_an_extension() -> None:
    assert _SCRIPT.is_file() and _SCRIPT.stat().st_mode & 0o111
    assert _SCRIPT.read_text().splitlines()[0] == "#!/usr/bin/env bash"


def test_the_guard_job_publishes_rust_changed_from_the_pull_request_files() -> None:
    guard = _job("guard")
    assert "rust_changed: ${{ steps.rust-scope.outputs.rust_changed }}" in guard
    assert "id: rust-scope" in guard
    assert "pulls/$PR_NUMBER/files" in guard
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


def test_the_stale_not_a_required_check_comment_is_gone() -> None:
    assert "Not (yet) a required check" not in _WORKFLOW
    assert "Rust lint & test (blumkin-agent)" in _WORKFLOW
