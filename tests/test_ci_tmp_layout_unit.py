"""CI wrappers run their tools through the repository-helpers temp layout (repository-helpers#832).

Each wrapper re-execs itself under ``scripts/dev/with-tmp-layout`` so pytest, uv and their caches
use ``$TMPDIR/<project>/<branch>/<tool>``. CI gets the helper from the ``setup-tmp-layout``
action; a host without a clone runs the wrapper unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS = _ROOT / ".github" / "workflows"
_ACTION = (
    "the-hcma/repository-helpers/actions/setup-tmp-layout@bfee0e1f2d682e83c6207a0d6a0f7feefa576fd8"
)

# wrapper path -> tools its layout is expected to be built from, outermost first
_WRAPPERS = {
    ".github/ci/python-static": ["uv"],
    ".github/ci/pytest-hermetic": ["uv", "pytest"],
    "test_packaging": ["uv"],
}


def _run(wrapper: str, tmp_path: Path, **extra: str | None) -> subprocess.CompletedProcess[str]:
    empty = tmp_path / "no-helpers"
    empty.mkdir(exist_ok=True)
    env = {
        "HOME": str(empty),
        "PATH": os.environ.get("PATH", ""),
        "REPOSITORY_HELPERS_DIR": str(empty),
        "TMPDIR": str(tmp_path),
    }
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(_ROOT / wrapper)],
        capture_output=True,
        check=False,
        cwd=_ROOT,
        env=env,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("wrapper", sorted(_WRAPPERS))
def test_ci_without_the_layout_helper_fails_with_a_pointer_to_the_action(
    wrapper: str, tmp_path: Path
) -> None:
    done = _run(wrapper, tmp_path, GITHUB_ACTIONS="true")
    assert done.returncode == 1
    assert "setup-tmp-layout" in done.stderr


@pytest.mark.parametrize("wrapper", sorted(_WRAPPERS))
def test_the_layout_helper_wraps_the_wrapper_in_its_tool_layouts(
    wrapper: str, tmp_path: Path
) -> None:
    helpers = tmp_path / "helpers"
    (helpers / "scripts" / "dev").mkdir(parents=True)
    fake = helpers / "scripts" / "dev" / "with-tmp-layout"
    # Record each tool and follow a nested layout call, but stop at the wrapper itself:
    # running it would need uv and the network.
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$1" >> "$TMPDIR/layout-tools"\n'
        "shift 2\n"
        f'if [[ "$1" == {fake} ]]; then exec "$@"; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    done = _run(wrapper, tmp_path, REPOSITORY_HELPERS_DIR=str(helpers))
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "layout-tools").read_text().split() == _WRAPPERS[wrapper]


@pytest.mark.parametrize("wrapper", [".github/ci/python-static", ".github/ci/pytest-hermetic"])
def test_a_host_without_the_layout_helper_runs_the_wrapper_unchanged(
    wrapper: str, tmp_path: Path
) -> None:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    uv = stub_dir / "uv"
    # A stand-in uv: log the call, answer the interpreter version probe, succeed.
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$TMPDIR/uv-calls"\n'
        'if [[ "$*" == *" -c "* ]]; then cut -d. -f1,2 .python-version; fi\n'
        "exit 0\n"
    )
    uv.chmod(0o755)
    path = f"{stub_dir}:{os.environ.get('PATH', '')}"
    done = _run(wrapper, tmp_path, GITHUB_ACTIONS=None, PATH=path)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "uv-calls").read_text()
    assert ("run pytest" if "pytest" in wrapper else "run ruff check") in calls
    assert not (tmp_path / "layout-tools").exists()


def test_the_pytest_wrapper_nests_the_pytest_layout_inside_the_uv_one() -> None:
    text = (_ROOT / ".github" / "ci" / "pytest-hermetic").read_text()
    assert '"$layout_bin" uv -- "$layout_bin" pytest -- bash "$0"' in text


@pytest.mark.parametrize("name", ["ci.yml", "cve-check.yml", "release-please.yml"])
def test_every_workflow_job_that_runs_uv_sets_up_the_layout_first(name: str) -> None:
    text = (_WORKFLOWS / name).read_text()
    jobs = re.split(r"\n  (?=[a-z][a-z0-9-]*:\n)", text.split("\njobs:\n", 1)[1])
    checked = 0
    for job in jobs:
        uv_runs = re.search(
            r"bash \.github/ci/(python-static|pytest-hermetic)"
            r"|run: \./test_packaging|\buv (sync|lock|build|run)\b",
            job,
        )
        if not uv_runs:
            continue
        checked += 1
        assert _ACTION in job, job.splitlines()[0]
        assert job.index(_ACTION) < uv_runs.start(), job.splitlines()[0]
    assert checked > 0
