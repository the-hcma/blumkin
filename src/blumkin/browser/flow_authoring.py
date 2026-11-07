"""Author a flow from short written specs, without recording it click by click (issue #622).

`blumkin browser model flow add` takes parameters (`date:date`, `type:choice=Meals|Taxi`) and
steps (`click expense.add page=form`, `fill expense.amount bind=amount`,
`handoff instruction="Click OK, then Save" expect_heading="Report saved"`) and builds a `Flow`
from them. This module only parses; the model's own validation (`save_model`) is what decides
whether the flow is acceptable against the recorded pages and controls, so a spec cannot say
anything a recorded flow could not.
"""

from __future__ import annotations

import shlex

from blumkin.browser.model import Flow, FlowParam, FlowStep, ParamKind

#: Step keys a spec can set with `key=value`; every one is a `FlowStep` field.
STEP_KEYS = frozenset(
    {
        "bind",
        "column",
        "control",
        "expect",
        "expect_heading",
        "instruction",
        "match",
        "page",
        "role",
        "row",
        "starts_on",
        "value",
    }
)
#: The actions a flow step can have: what the ledger runner knows how to do.
ACTIONS = frozenset(
    {"cell", "click", "fill", "goto", "handoff", "pick", "press", "select", "upload", "verify"}
)
#: The most steps and parameters a spec may carry (the model's own limits are checked on save).
_MAX_PARAMS = 12
_MAX_STEPS = 200
#: Actions whose second word, when it has no `=`, is the control they act on.
_CONTROL_ACTIONS = frozenset({"click", "fill", "press", "select", "upload", "verify"})


class FlowSpecError(ValueError):
    """A parameter or step written in a form that cannot be read."""


def build_flow(name: str, params: list[str], steps: list[str]) -> Flow:
    """A `Flow` from its name, its parameter specs and its step specs, in order."""
    if not steps:
        raise FlowSpecError("Expected at least one --step, got none")
    if len(steps) > _MAX_STEPS or len(params) > _MAX_PARAMS:
        raise FlowSpecError(
            f"Expected at most {_MAX_STEPS} steps and {_MAX_PARAMS} parameters, got "
            f"{len(steps)} and {len(params)}"
        )
    return Flow(
        name=name,
        params=tuple(_param(spec) for spec in params),
        steps=tuple(_step(spec, number) for number, spec in enumerate(steps, start=1)),
    )


def _param(spec: str) -> FlowParam:
    name, sep, kind_text = spec.partition(":")
    kind_name, _, options = kind_text.partition("=")
    if not sep or not name.strip():
        raise FlowSpecError(f"Expected a parameter as NAME:KIND (or NAME:choice=A|B), got {spec!r}")
    try:
        kind = ParamKind(kind_name.strip())
    except ValueError:
        kinds = ", ".join(k.value for k in ParamKind)
        raise FlowSpecError(
            f"Expected a parameter kind of {kinds}, got {kind_name!r} in {spec!r}"
        ) from None
    choices = tuple(option.strip() for option in options.split("|") if option.strip())
    if kind is ParamKind.CHOICE and not choices:
        raise FlowSpecError(f"Expected options after choice= (A|B|C), got none in {spec!r}")
    if kind is not ParamKind.CHOICE and options:
        raise FlowSpecError(f"Expected options only for a choice parameter, got them in {spec!r}")
    return FlowParam(name=name.strip(), kind=kind, choices=choices)


def _step(spec: str, number: int) -> FlowStep:
    try:
        words = shlex.split(spec)
    except ValueError as exc:
        raise FlowSpecError(f"Expected step {number} to be quoted properly: {exc}") from None
    if not words:
        raise FlowSpecError(f"Expected step {number} to name an action, got nothing")
    action, rest = words[0].lower(), words[1:]
    if action not in ACTIONS:
        raise FlowSpecError(
            f"Expected step {number} to start with one of {', '.join(sorted(ACTIONS))}, "
            f"got {words[0]!r}"
        )
    fields: dict[str, str] = {}
    for word in rest:
        key, sep, value = word.partition("=")
        if sep and key in STEP_KEYS:
            if key in fields:
                raise FlowSpecError(f"Expected {key} once in step {number}, got it twice")
            if not value and not (action == "cell" and key == "value"):
                raise FlowSpecError(f"Expected {key}= in step {number} to have a value, got none")
            fields[key] = value
        elif not sep and action in _CONTROL_ACTIONS and "control" not in fields:
            fields["control"] = word
        else:
            keys = ", ".join(sorted(STEP_KEYS))
            raise FlowSpecError(
                f"Expected step {number} words as key=value with key one of {keys}, got {word!r}"
            )
    if action in _CONTROL_ACTIONS and "control" not in fields:
        raise FlowSpecError(f"Expected step {number} ({action}) to name the control it acts on")
    return FlowStep(
        action=action,
        bind=fields.get("bind"),
        column=fields.get("column"),
        control=fields.get("control"),
        expect=fields.get("expect"),
        expect_heading=fields.get("expect_heading"),
        instruction=fields.get("instruction"),
        match=fields.get("match"),
        page=fields.get("page"),
        role=fields.get("role"),
        row=fields.get("row"),
        starts_on=fields.get("starts_on"),
        value=fields.get("value"),
    )
