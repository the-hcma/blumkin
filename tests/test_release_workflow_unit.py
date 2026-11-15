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


def test_a_forced_publish_skips_release_please_and_the_build_does_not_need_it_to_succeed() -> None:
    """Release Please fails when it would have to open a PR (#197); that must not stop a forced
    publish (#754)."""
    release_please = _job("release-please")
    head = release_please[: release_please.index("    steps:")]
    assert "if: ${{ github.event.inputs.force_publish_pypi != 'true' }}" in head
    build = _job("build-dists")
    condition = build[build.index("    if: >-") : build.index("    # Two legs")]
    assert "!cancelled()" in condition
    assert "needs.release-please.outputs.release_created == 'true'" in condition
    assert "github.event.inputs.force_publish_pypi == 'true'" in condition


def test_the_release_docs_say_a_forced_publish_needs_no_release_pr() -> None:
    releasing = (_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
    assert "A forced publish skips the Release Please job" in releasing


def test_the_publish_job_runs_on_a_forced_publish_although_release_please_was_skipped() -> None:
    """A skipped job anywhere in the needs chain skips a job whose `if` is implicit, even when
    its direct dependency succeeded (shown with a probe workflow on GitHub, #754), so the publish
    job must say so explicitly."""
    publish = _job("publish-pypi")
    head = publish[: publish.index("    runs-on:")]
    assert "needs: build-dists" in head
    assert "if: ${{ !cancelled() && needs.build-dists.result == 'success' }}" in head


def test_the_docs_name_the_commit_types_that_release_and_the_config_does_not_override_them() -> (
    None
):
    """The docs describe the Python release type's own section list (#755), which only holds while
    the config does not override it with `changelog-sections`; this pins the docs to that config,
    and the list itself was checked in Release Please's source and in practice (a docs-only commit
    made it propose a release), which a hermetic test cannot run."""
    import json

    config = json.loads((_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    assert "changelog-sections" not in config
    for package in config["packages"].values():
        assert "changelog-sections" not in package
    # The documented list is the Python release type's own: another type would use the generic
    # defaults (`docs` hidden, `deps` absent) and make the docs wrong.
    assert config["packages"]["."]["release-type"] == "python"
    releasing = " ".join((_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8").split())
    assert (
        "`feat`, `fix`, `perf`, `deps`, `revert` and `docs` (all but `feat` as a patch)"
        in releasing
    )
    assert "`style`, `chore`, `refactor`, `test`, `build` and `ci` never release" in releasing
    assert "A `docs:` commit therefore asks for a patch release on its own" in releasing
