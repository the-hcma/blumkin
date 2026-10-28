"""The help text module defines each epilog once (issue #547 review)."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_HELP = Path(__file__).resolve().parents[1] / "src" / "blumkin" / "help_text.py"


def test_every_constant_in_the_help_text_is_defined_once() -> None:
    tree = ast.parse(_HELP.read_text(encoding="utf-8"))
    names = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    repeated = {name: count for name, count in Counter(names).items() if count > 1}
    assert repeated == {}


def test_the_doctor_epilog_describes_the_repository_comparison() -> None:
    from blumkin import help_text

    assert "`repository` (JSON)" in help_text.DOCTOR_EPILOG
    assert "BLUMKIN_REPOSITORY" in help_text.DOCTOR_EPILOG
    assert "uv tool install -e PATH" in help_text.DOCTOR_EPILOG
