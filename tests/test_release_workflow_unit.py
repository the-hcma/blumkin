"""The forced PyPI publish is bound to the commit the operator inspected (release recovery)."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = (_ROOT / ".github" / "workflows" / "release-please.yml").read_text(encoding="utf-8")


def _job(name: str) -> str:
    """The text of one job: from its key to the next job key."""
    start = _WORKFLOW.index(f"\n  {name}:\n")
    following = [
        _WORKFLOW.index(f"\n  {other}:\n")
        for other in ("publish-pypi", "publish-npm", "build-dists")
        if other != name and _WORKFLOW.index(f"\n  {other}:\n") > start
    ]
    return _WORKFLOW[start : min(following) if following else len(_WORKFLOW)]


def test_the_dispatch_takes_an_optional_expected_sha() -> None:
    inputs = _WORKFLOW[_WORKFLOW.index("workflow_dispatch:") : _WORKFLOW.index("\npermissions:")]
    assert "      expected_sha:" in inputs
    assert "required: false" in inputs.split("expected_sha:", 1)[1]


def test_the_build_stops_when_main_is_not_the_inspected_commit_before_anything_is_built() -> None:
    build = _job("build-dists")
    guard = build.index("- name: Check main is the inspected commit")
    assert guard < build.index("- name: Install dependencies")
    assert guard < build.index("- name: Build distributions")
    step = build[guard : build.index("\n      - name:", guard + 1)]
    assert "if: github.event.inputs.expected_sha != ''" in step
    assert '"${GITHUB_SHA}" != "${EXPECTED_SHA}"' in step
    assert "exit 1" in step


def test_the_publish_job_cannot_run_when_the_guarded_build_failed() -> None:
    publish = _job("publish-pypi")
    assert "needs: build-dists" in publish


def test_the_recovery_steps_pass_the_inspected_commit() -> None:
    releasing = (_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
    assert '-f expected_sha="$(git rev-parse origin/main)"' in releasing
    assert "git fetch origin" in releasing
