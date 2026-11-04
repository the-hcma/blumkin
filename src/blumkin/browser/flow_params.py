"""Check the values a run supplies for a flow's declared parameters (issue #622).

A flow that declares `params` is run from named, typed inputs instead of from whatever the
caller binds, and every one is checked before anything is staged or the browser is touched:
each declared parameter must be supplied, nothing undeclared may be, and each value must be what
its kind says (a date as `YYYY-MM-DD`, one of the recorded choices, an existing regular file,
text without control characters). A refusal names the parameter and what was expected, never
the value it got: amounts, merchants and paths are the user's private data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from blumkin.browser.ledger import LedgerError
from blumkin.browser.model import Flow, FlowParam, ParamKind

#: Bindings the run adds itself (the ledger item being applied).
RESERVED_BINDINGS = frozenset({"item"})

MAX_TEXT_CHARS = 500

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def validate_params(flow: Flow, values: Mapping[str, str]) -> dict[str, str]:
    """The `values` of a run, checked against `flow.params`; a flow with none passes as is.

    Raises `LedgerError` listing every problem at once (missing, undeclared, malformed).
    """
    if not flow.params:
        return dict(values)
    declared = {param.name: param for param in flow.params}
    problems: list[str] = []
    missing = [name for name in declared if name not in values]
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    unknown = sorted(
        name for name in values if name not in declared and name not in RESERVED_BINDINGS
    )
    if unknown:
        problems.append(f"not parameters of this flow: {', '.join(unknown)}")
    for name, param in declared.items():
        if name in values:
            reason = _refusal(param, values[name])
            if reason is not None:
                problems.append(f"{name}: {reason}")
    if problems:
        raise LedgerError(
            f"Expected every parameter of flow {flow.name!r} to be supplied and valid "
            f"(values are not shown), got: {'; '.join(problems)}"
        )
    return dict(values)


def _refusal(param: FlowParam, value: str) -> str | None:
    """Why `value` is not a `param`, or None when it is."""
    if param.kind is ParamKind.CHOICE:
        if value in param.choices:
            return None
        return f"expected one of {', '.join(repr(choice) for choice in param.choices)}"
    if param.kind is ParamKind.DATE:
        if _ISO_DATE.fullmatch(value):
            try:
                date.fromisoformat(value)
            except ValueError:
                return "expected a real calendar date as YYYY-MM-DD"
            return None
        return "expected a date as YYYY-MM-DD"
    if param.kind is ParamKind.FILE:
        path = Path(value)
        if value and not path.is_symlink() and path.is_file():
            return None
        return "expected the path of an existing regular file (not a link)"
    if not value.strip() or len(value) > MAX_TEXT_CHARS or not value.isprintable():
        return f"expected 1 to {MAX_TEXT_CHARS} printable characters"
    return None
