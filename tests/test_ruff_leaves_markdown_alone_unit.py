"""`ruff format .` must not rewrite the repo's markdown (#798).

Ruff 0.17 formats the Python code blocks inside markdown files too. The examples in
`.agents/rules/` are written compact on purpose (an `class Example:` with two-space indents, no
blank lines between one-line definitions), so running `uv run ruff format .` (as AGENTS.md says)
modified `.agents/rules/lexicographic-code-organization.md` in every worktree. The markdown lint
checks the markdown a branch modifies, so the submit then failed on that file's own hand-wrapped
prose. CI only checks `src tests`, so nothing noticed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _ruff() -> list[str]:
    found = shutil.which("ruff")
    return [found] if found else [sys.executable, "-m", "ruff"]


def _ruff_version() -> tuple[int, ...]:
    done = subprocess.run([*_ruff(), "--version"], capture_output=True, text=True, check=True)
    return tuple(int(part) for part in done.stdout.split()[1].split(".")[:2])


def test_pyproject_excludes_markdown_from_ruff() -> None:
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "*.md" in config["tool"]["ruff"]["extend-exclude"]


def test_ruff_format_over_a_copy_of_the_repos_markdown_changes_none_of_it(tmp_path: Path) -> None:
    """The behaviour itself: run `ruff format .` over a copy of every tracked markdown file."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    tracked = [name for name in listed.split("\0") if name]  # `-z`: a name may hold a space
    assert tracked, "expected tracked markdown files"
    before: dict[Path, bytes] = {}
    for name in tracked:
        source = _ROOT / name
        assert source.is_file(), f"git lists {name} but it is not a file"
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        before[target] = target.read_bytes()
    shutil.copy(_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")

    done = subprocess.run(
        [*_ruff(), "format", "."], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, f"ruff did not run: {done.stdout}{done.stderr}"
    changed = [
        str(path.relative_to(tmp_path))
        for path, text in before.items()
        if path.read_bytes() != text
    ]
    assert changed == [], f"ruff format rewrote tracked markdown: {changed}"


def test_the_exclusion_is_what_protects_the_markdown(tmp_path: Path) -> None:
    """Without it ruff does change a markdown file's code block: the test above has teeth."""
    if _ruff_version() < (0, 17):  # the dev group pins >=0.17.4; older ruffs skip markdown
        pytest.skip("this ruff does not format markdown, so there is nothing to guard against")
    sample = tmp_path / "note.md"
    sample.write_text("# Note\n\n```python\nclass A:\n  def f(self): ...\n```\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")

    done = subprocess.run(
        [*_ruff(), "format", "."], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "    def f(self)" in sample.read_text(encoding="utf-8")  # reindented by ruff
