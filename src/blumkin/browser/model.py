"""Site model: a task's recorded interaction map, kept on disk (issue #485).

A model describes the structure of a website well enough that later runs can skip
exploring it: the pages (a URL template and a fingerprint of their landmark controls), the
controls on each page (a logical name, a kind, whether clicking it could finalize a form,
and ordered locator strategies) and the flows that chain them. It lives in
`<config dir>/browser-models/<task>/model.json`, with every saved version kept under
`history/`, in `0700` directories and `0600` files.

A model is meant to hold structure only: nothing the user typed, no cookies and no other
personal data. The schema narrows what can be stored but cannot prove a free-text field is
innocent, so it combines a strict shape with heuristics: every object has a fixed set of
keys (anything else is a load error), every string is short, NFC-normalized and free of
control, separator and bidirectional characters, a URL template is a path of words and
`{placeholders}` (no query string, where values live, and no literal token or number
segments), labels and role names carry no `=` or quotes, and no text holds a run of six or
more digits. Hosts must be public-looking host names, and can be checked against the task's
`allowed_hosts` when a model is saved or loaded. Whoever records a model remains responsible
for reviewing it before it is stored.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import stat
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import ensure_private_dir, validate_task_name, write_private
from blumkin.config import BrowserSetup


@dataclass(frozen=True, slots=True)
class Control:
    kind: ControlKind
    locators: tuple[Locator, ...]
    name: str
    safety: ControlSafety


class ControlKind(StrEnum):
    BUTTON = "button"
    DATE = "date"
    FILE = "file"
    SELECT = "select"
    TEXT = "text"


class ControlSafety(StrEnum):
    """Whether clicking the control could submit or finalize a form (the guard still decides)."""

    FINALIZING = "finalizing"
    SAFE = "safe"


@dataclass(frozen=True, slots=True)
class Flow:
    name: str
    steps: tuple[FlowStep, ...]
    #: The inputs the flow takes; a flow with none keeps the older, unchecked bindings.
    params: tuple[FlowParam, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowParam:
    """One declared input of a flow: its name, what it must be and, for a choice, the options."""

    name: str
    kind: ParamKind
    #: The recorded options of a `choice` parameter (empty for every other kind).
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowStep:
    """One step of a flow: an action on a control of a page, and the landmark expected next."""

    action: str
    control: str | None
    expect: str | None
    page: str | None
    #: The flow parameter whose value a `fill` or `upload` step uses.
    bind: str | None = None
    #: A heading the page must show after the step (or, on a `handoff` step, once the human
    #: has acted); the run waits for it, bounded, and stops when it never shows.
    expect_heading: str | None = None
    #: What a `handoff` step tells the human to do before the run goes on.
    instruction: str | None = None
    #: A `cell` step's grid cell: the row and the column (1-based numbers, header texts, or
    #: `$name` for the value of a flow parameter), and the literal text to put in it (empty
    #: clears the cell). With neither a literal nor a `bind`, the step only reads the cell.
    column: str | None = None
    row: str | None = None
    value: str | None = None


@dataclass(frozen=True, slots=True)
class Locator:
    """One way to find a control; `value` is a label, an accessible name or a selector."""

    strategy: LocatorStrategy
    value: str


class LocatorStrategy(StrEnum):
    """How a locator finds its control, in the order a model normally lists them."""

    ROLE_NAME = "role_name"
    LABEL = "label"
    ATTRIBUTE = "attribute"
    CSS = "css"


class ParamKind(StrEnum):
    """What a flow parameter must be when a run supplies it."""

    CHOICE = "choice"
    DATE = "date"
    FILE = "file"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Page:
    controls: tuple[Control, ...]
    #: Sorted, unique `role:name` strings for the landmark controls of the page.
    fingerprint: tuple[str, ...]
    page_id: str
    #: A path with `{placeholders}`; never a query string or fragment (that is where values live).
    url_template: str


SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SiteModel:
    blumkin_version: str
    flows: tuple[Flow, ...]
    hosts: tuple[str, ...]
    pages: tuple[Page, ...]
    recorded_at: str
    task: str
    #: Assigned by `save_model`; 0 for a model that has not been saved.
    version: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "blumkin_version": self.blumkin_version,
            "flows": [_flow_payload(flow) for flow in self.flows],
            "hosts": list(self.hosts),
            "pages": [
                {
                    "controls": [
                        {
                            "kind": control.kind.value,
                            "locators": [
                                {"strategy": loc.strategy.value, "value": loc.value}
                                for loc in control.locators
                            ],
                            "name": control.name,
                            "safety": control.safety.value,
                        }
                        for control in page.controls
                    ],
                    "fingerprint": list(page.fingerprint),
                    "page_id": page.page_id,
                    "url_template": page.url_template,
                }
                for page in self.pages
            ],
            "recorded_at": self.recorded_at,
            "schema_version": SCHEMA_VERSION,
            "task": self.task,
            "version": self.version,
        }


class SiteModelError(ValueError):
    """A model that cannot be stored or loaded, with the expectation it broke."""


def _overlaps(where: Path, root: Path) -> bool:
    """Whether one path is the other or inside it; case is ignored, so a case-insensitive
    filesystem (macOS) cannot spell its way past the check."""
    a, b = Path(str(where).casefold()), Path(str(root).casefold())
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def default_store(config_dir: Path) -> Path:
    """Where models live unless `[browser] models_dir` says otherwise."""
    return config_dir / _STORE_DIRNAME


def resolve_store(config: BrowserSetup) -> Path:
    """The directory that holds the recorded models: `[browser] models_dir`, or the default.

    A configured location must be absolute (or start with `~/`) and resolves through any
    symlink it is given as. Its parent must exist; it is refused when it is the filesystem
    root, the home directory, or inside (or the same as) a folder a task may upload from or
    download into, and, when it already exists, unless it is a directory owned by this user
    that no one else can read or write (mode 0700). Inside it the store's own protections
    are unchanged: no symlinked task, `history` or model files, private modes, regular files.
    """
    configured = config.browser.models_dir
    if configured is None:
        return default_store(config.config_dir)
    text = os.fspath(configured)
    if not (text.startswith("/") or text.startswith("~/")):
        raise SiteModelError(
            f"Expected [browser] models_dir to be absolute or start with ~/, got {text!r}"
        )
    try:
        where = Path(text).expanduser().resolve()
        home = Path.home().resolve()
    except OSError, RuntimeError, ValueError:
        raise SiteModelError(
            "Expected [browser] models_dir to be a usable path, got one that cannot be resolved"
        ) from None
    if where in (Path(where.anchor), home) or str(where).casefold() == str(home).casefold():
        raise SiteModelError(
            f"Expected [browser] models_dir to be a folder of its own, got {str(where)!r}"
        )
    for policy in config.browser.tasks.values():
        for folder in (*policy.upload_dirs, *policy.download_dirs):
            root = Path(folder).expanduser().resolve()
            if _overlaps(where, root):
                raise SiteModelError(
                    "Expected [browser] models_dir apart from every upload_dirs and "
                    "download_dirs folder (not inside one, not around one), got an overlap"
                )
    try:
        state = config.config_dir.resolve()
    except OSError, RuntimeError, ValueError:
        raise SiteModelError("Expected the config directory to resolve, got a failure") from None
    folded_where, folded_state = Path(str(where).casefold()), Path(str(state).casefold())
    if folded_where == folded_state or folded_state.is_relative_to(folded_where):
        raise SiteModelError(
            "Expected [browser] models_dir to be a folder of its own, apart from the config "
            "directory that holds blumkin's other state"
        )
    if not where.parent.is_dir():
        raise SiteModelError(
            f"Expected the parent of [browser] models_dir to exist, got none for {str(where)!r}"
        )
    try:
        info = where.lstat()
    except FileNotFoundError:
        return where
    except OSError as exc:
        raise SiteModelError(
            f"Expected to inspect [browser] models_dir, got {type(exc).__name__}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise SiteModelError(f"Expected [browser] models_dir to be a directory, got {str(where)!r}")
    if info.st_uid != os.getuid():
        raise SiteModelError(
            f"Expected [browser] models_dir to be owned by you, got another owner: {str(where)!r}"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SiteModelError(
            f"Expected [browser] models_dir to be private (mode 0700), got "
            f"{stat.S_IMODE(info.st_mode):04o}: run `chmod 700 {shlex.quote(str(where))}`"
        )
    return where


def discard_draft(store: Path, task: str) -> bool:
    """Delete the task's in-progress draft; False when there was none."""
    validate_task_name(task)
    with _filesystem("discard the draft"):
        task_dir = _existing_dir(store, task)
        if task_dir is None:
            return False
        with _locked(task_dir):
            try:
                (task_dir / _DRAFT_FILENAME).unlink()
            except FileNotFoundError:
                return False
    return True


def is_storable_label(text: str) -> bool:
    """Whether `text` may be stored as a label (a name, fingerprint entry or landmark)."""
    try:
        _text(text, "label", plain=True)
    except SiteModelError:
        return False
    return True


def list_versions(store: Path, task: str) -> list[int]:
    """The saved versions of the task's model, oldest first."""
    validate_task_name(task)
    with _filesystem("list the saved model versions"), _shared_lock(store, task):
        return _versions(store, task)


def load_draft(store: Path, task: str) -> SiteModel | None:
    """The task's in-progress exploration draft, or None when there is none."""
    validate_task_name(task)
    with _filesystem("read the draft"), _shared_lock(store, task):
        task_dir = _existing_dir(store, task)
        if task_dir is None or not _exists(task_dir / _DRAFT_FILENAME):
            return None
        return _read_model(task_dir / _DRAFT_FILENAME, task)


def load_model(
    store: Path,
    task: str,
    *,
    host_allowed: Callable[[str], bool] | None = None,
) -> SiteModel | None:
    """The task's current model, or None when none was saved.

    If the newest saved version is ahead of `model.json`, or `model.json` is missing (a save
    was interrupted between its two writes), the newest saved version is returned, and the
    next save repairs the file.
    """
    validate_task_name(task)
    with _filesystem("read the model"), _shared_lock(store, task):
        task_dir = _existing_dir(store, task)
        if task_dir is None:
            return None
        newest = _versions(store, task)
        if not _exists(task_dir / _CURRENT_FILENAME):
            if not newest:
                return None
            model = _newest_saved(task_dir, task, newest[-1])
        else:
            model = _read_model(task_dir / _CURRENT_FILENAME, task)
            if newest and model.version != newest[-1]:
                if model.version > newest[-1]:
                    raise SiteModelError(
                        f"Expected {_CURRENT_FILENAME} to be version {newest[-1]}, "
                        f"got {model.version}"
                    )
                model = _newest_saved(task_dir, task, newest[-1])
    _check_hosts(model, host_allowed)
    return model


def load_version(
    store: Path,
    task: str,
    version: int,
    *,
    host_allowed: Callable[[str], bool] | None = None,
) -> SiteModel:
    """One saved version of the task's model."""
    validate_task_name(task)
    with _filesystem("read the model"), _shared_lock(store, task):
        history = _existing_dir(store, task, _HISTORY_DIRNAME)
        path = None if history is None else history / _history_name(version)
        if path is None or not _exists(path):
            raise SiteModelError(
                f"Expected version {version} of the {task!r} model to exist, got none"
            )
        model = _read_model(path, task)
    if model.version != version:
        raise SiteModelError(f"Expected version {version} in {path.name}, got {model.version}")
    _check_hosts(model, host_allowed)
    return model


def save_draft(store: Path, model: SiteModel) -> None:
    """Keep `model` as the task's in-progress draft (checked like a model, never a version)."""
    text = _draft_text(model)
    with _filesystem("store the draft"):
        root = ensure_private_dir(store)
        task_dir = ensure_private_dir(root / model.task)
        with _locked(task_dir):
            write_private(task_dir / _DRAFT_FILENAME, text)
            _fsync_dir(task_dir)


def save_model(
    store: Path,
    model: SiteModel,
    *,
    host_allowed: Callable[[str], bool] | None = None,
) -> SiteModel:
    """Validate and store `model` as the task's next version; returns it with that version."""
    if not isinstance(model.task, str):
        raise SiteModelError(f"Expected the task to be text, got {type(model.task).__name__}")
    validate_task_name(model.task)
    try:
        payload = model.to_payload()
    except (AttributeError, TypeError, ValueError) as exc:
        raise SiteModelError(f"Expected a well-formed model, got {type(exc).__name__}") from exc
    parsed = _parse(payload, model.task)
    _check_hosts(parsed, host_allowed)
    _check_size(_render(parsed))
    with _filesystem("store the model"):
        root = ensure_private_dir(store)
        task_dir = ensure_private_dir(root / model.task)
        history = ensure_private_dir(task_dir / _HISTORY_DIRNAME)
        with _locked(task_dir):
            previous = _versions(store, model.task)
            version = (previous[-1] if previous else 0) + 1
            stored = replace(parsed, version=version)
            text = _render(stored)
            _check_size(text)
            write_private(history / _history_name(version), text)
            _fsync_dir(history)
            write_private(task_dir / _CURRENT_FILENAME, text)
            _fsync_dir(task_dir)
            _fsync_dir(root)
            _fsync_dir(store.parent)
    return stored


def check_model(model: SiteModel, *, host_allowed: Callable[[str], bool] | None = None) -> None:
    """Raise `SiteModelError` unless `model` could be saved; nothing is written."""
    try:
        payload = model.to_payload()
    except (AttributeError, TypeError, ValueError) as exc:
        raise SiteModelError(f"Expected a well-formed model, got {type(exc).__name__}") from exc
    parsed = _parse(payload, model.task)
    _check_hosts(parsed, host_allowed)
    _check_size(_render(parsed))


def is_logical_name(value: str) -> bool:
    """Whether `value` is an acceptable page, control or action name in a stored model."""
    return _LOGICAL_NAME.fullmatch(value) is not None and not _looks_like_a_token(value)


def update_draft(
    store: Path,
    task: str,
    change: Callable[[SiteModel | None], SiteModel | None],
) -> SiteModel | None:
    """Change the task's draft under one lock, so concurrent updates cannot lose each other.

    `change` gets the draft, or the stored model when there is no draft yet, or None when
    there is neither, and returns the new draft (checked like a model) or None to leave
    things as they are. If it raises, or its result is not a valid model, the previous draft
    is untouched. `change` runs while the store lock is held, so it must not call other
    functions of this module that take that lock (`load_draft`, `save_model`, ...).
    """
    validate_task_name(task)
    with _filesystem("update the draft"):
        root = ensure_private_dir(store)
        task_dir = ensure_private_dir(root / task)
        with _locked(task_dir):
            draft_path = task_dir / _DRAFT_FILENAME
            model_path = task_dir / _CURRENT_FILENAME
            if _exists(draft_path):
                current = _read_model(draft_path, task)
            elif _exists(model_path):
                current = _read_model(model_path, task)
            else:
                current = None
            updated = change(current)
            if updated is None:
                return None
            text = _draft_text(updated)
            if updated.task != task:
                raise SiteModelError(
                    f"Expected a draft for task {task!r}, got {_shown(updated.task)}"
                )
            write_private(draft_path, text)
            _fsync_dir(task_dir)
            return updated


_BIDI_AND_ZERO_WIDTH = re.compile("[؜​-‏‪-‮⁠⁦-⁩﻿]")
_CURRENT_FILENAME = "model.json"
_DRAFT_FILENAME = "draft.json"
_HISTORY_DIRNAME = "history"
_HISTORY_FILE = re.compile(r"model-([0-9]{4,})\.json")
_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_LOCK_FILENAME = ".lock"
#: The steps a flow parameter can feed: a value typed into a field or a file chosen for upload.
_BOUND_ACTIONS = frozenset({"fill", "upload"})
_CELL = "cell"
_HANDOFF = "handoff"
#: The steps that can state a heading the page must show afterwards.
_HEADING_ACTIONS = frozenset({"cell", "click", "fill", "goto", "handoff", "press", "upload"})
_LOGICAL_NAME = re.compile(r"[a-z][a-z0-9_.\[\]-]{0,63}")
_MAX_CELL_REF = 80
_MAX_CELL_VALUE = 80
_MAX_CHOICES = 100
_MAX_CHOICE_CHARS = 80
_MAX_CONTROLS = 500
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_FINGERPRINT = 200
_MAX_FLOWS = 50
_MAX_HEADING = 100
_MAX_INSTRUCTION = 300
_MAX_LOCATORS = 10
_MAX_PAGES = 200
_MAX_PARAMS = 12
_MAX_STEPS = 200
_MAX_TEXT = 200
_STORE_DIRNAME = "browser-models"
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DIGIT_RUN = re.compile(r"\d{6,}")
_DIGIT_SEPARATORS = re.compile(r"(?<=\d)[ .,\-](?=\d)")
_PLAIN_FORBIDDEN = re.compile("[=\"'`@]")
_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")
_URL_SEGMENT = re.compile(
    r"(?=.{1,24}$)[A-Za-z]+[0-9]{0,2}(?:[-_][A-Za-z]+[0-9]{0,2})*|\{[a-z][a-z0-9_]{0,40}\}"
)


def _at_most(items: Sequence[Any], limit: int, where: str) -> None:
    if len(items) > limit:
        raise SiteModelError(f"Expected at most {limit} items in {where}, got {len(items)}")


def _check_hosts(model: SiteModel, host_allowed: Callable[[str], bool] | None) -> None:
    if host_allowed is None:
        return
    for host in model.hosts:
        if not host_allowed(host):
            raise SiteModelError(
                f"Expected every host to be allowed for task {model.task!r}, got {host!r}"
            )


def _check_size(text: str) -> None:
    size = len(text.encode("utf-8"))
    if size > _MAX_FILE_BYTES:
        raise SiteModelError(f"Expected at most {_MAX_FILE_BYTES} bytes, got {size}: too large")


def _control(data: Any, where: str) -> Control:
    data = _keys(data, where, {"kind", "locators", "name", "safety"})
    name = _text(data["name"], f"{where}.name", pattern=_LOGICAL_NAME, what="a logical name")
    locators = _list(data["locators"], f"{where}.locators")
    if not locators:
        raise SiteModelError(f"Expected at least one locator in {where}, got none")
    _at_most(locators, _MAX_LOCATORS, f"{where}.locators")
    return Control(
        kind=_enum(ControlKind, data["kind"], f"{where}.kind"),
        locators=tuple(_locator(item, f"{where}.locators[{n}]") for n, item in enumerate(locators)),
        name=name,
        safety=_enum(ControlSafety, data["safety"], f"{where}.safety"),
    )


def _draft_text(model: SiteModel) -> str:
    """The checked JSON text of a draft."""
    if not isinstance(model.task, str):
        raise SiteModelError(f"Expected the task to be text, got {type(model.task).__name__}")
    validate_task_name(model.task)
    try:
        payload = model.to_payload()
    except (AttributeError, TypeError, ValueError) as exc:
        raise SiteModelError(f"Expected a well-formed model, got {type(exc).__name__}") from exc
    text = _render(_parse(payload, model.task))
    _check_size(text)
    return text


def _enum[E: StrEnum](kind: type[E], value: Any, where: str) -> E:
    try:
        return kind(value)
    except ValueError:
        allowed = ", ".join(member.value for member in kind)
        raise SiteModelError(
            f"Expected {where} to be one of {allowed}, got {_shown(value)}"
        ) from None


def _existing_dir(store: Path, *parts: str) -> Path | None:
    """The store directory for the task (and `parts`), None if absent; a symlink is an error."""
    path = store
    for name in (None, *parts):
        path = path if name is None else path / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise SiteModelError(f"Expected {path.name} to be a real directory, got a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise SiteModelError(f"Expected {path.name} to be a directory, got another file type")
    return path


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


@contextlib.contextmanager
def _filesystem(action: str) -> Iterator[None]:
    """Turn a filesystem failure while doing `action` into a `SiteModelError`."""
    try:
        yield
    except OSError as exc:
        raise SiteModelError(f"Expected to {action}, got {type(exc).__name__}") from exc


def _flow(data: Any, where: str, pages: Mapping[str, Page]) -> Flow:
    data = _keys(data, where, {"name", "steps"}, optional={"params"})
    steps = _list(data["steps"], f"{where}.steps")
    _at_most(steps, _MAX_STEPS, f"{where}.steps")
    raw_params = _list(data.get("params", []), f"{where}.params")
    _at_most(raw_params, _MAX_PARAMS, f"{where}.params")
    params = tuple(_param(item, f"{where}.params[{n}]") for n, item in enumerate(raw_params))
    if len({param.name for param in params}) != len(params):
        raise SiteModelError(f"Expected {where}.params with distinct names, got a repeat")
    controls = {c.name for page in pages.values() for c in page.controls}
    return Flow(
        name=_text(data["name"], f"{where}.name", pattern=_LOGICAL_NAME, what="a logical name"),
        params=params,
        steps=tuple(
            _step(item, f"{where}.steps[{n}]", pages, controls, {p.name: p.kind for p in params})
            for n, item in enumerate(steps)
        ),
    )


def _flow_payload(flow: Flow) -> dict[str, Any]:
    """A flow as stored; `params` and `bind` appear only when used, so older models stay as is."""
    payload: dict[str, Any] = {
        "name": flow.name,
        "steps": [
            {
                "action": step.action,
                "control": step.control,
                "expect": step.expect,
                "page": step.page,
            }
            | ({"bind": step.bind} if step.bind is not None else {})
            | ({"expect_heading": step.expect_heading} if step.expect_heading is not None else {})
            | ({"instruction": step.instruction} if step.instruction is not None else {})
            | ({"row": step.row} if step.row is not None else {})
            | ({"column": step.column} if step.column is not None else {})
            | ({"value": step.value} if step.value is not None else {})
            for step in flow.steps
        ],
    }
    if flow.params:
        payload["params"] = [
            {"kind": param.kind.value, "name": param.name}
            | ({"choices": list(param.choices)} if param.choices else {})
            for param in flow.params
        ]
    return payload


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _history_name(version: int) -> str:
    return f"model-{version:04d}.json"


def _host(value: Any, where: str) -> str:
    text = _text(value, where, max_len=253)
    labels = text.split(".")
    if not _HOST.fullmatch(text) or len(labels) < 2 or labels[-1].isdigit():
        raise SiteModelError(
            f"Expected {where} to be a lowercase public-looking host name (at least two labels, "
            f"no scheme, port, path, user info or IP address), got {_shown(value)}"
        )
    return text


def _is_unprintable(char: str) -> bool:
    if 0xFE00 <= ord(char) <= 0xFE0F or 0xE0000 <= ord(char) <= 0xE01EF:
        return True  # variation selectors and tag characters can hide data
    category = unicodedata.category(char)
    return (
        category.startswith("C") or category in {"Zl", "Zp"} or (category == "Zs" and char != " ")
    )


def _keys(
    data: Any, where: str, allowed: set[str], *, optional: set[str] | None = None
) -> Mapping[str, Any]:
    """`data` as an object holding every key of `allowed` and, besides, only `optional` ones."""
    if not isinstance(data, dict):
        raise SiteModelError(f"Expected {where} to be an object, got {type(data).__name__}")
    extra = optional or set()
    for key in data:
        if key not in allowed and key not in extra:
            raise SiteModelError(
                f"Expected only {sorted(allowed | extra)} in {where}, "
                f"got unexpected key {_shown(key)}"
            )
    missing = sorted(allowed - set(data))
    if missing:
        raise SiteModelError(f"Expected {where} to have {missing}, got none of them")
    return data


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise SiteModelError(f"Expected {where} to be a list, got {type(value).__name__}")
    return value


def _locator(data: Any, where: str) -> Locator:
    data = _keys(data, where, {"strategy", "value"})
    strategy = _enum(LocatorStrategy, data["strategy"], f"{where}.strategy")
    selector = strategy in {LocatorStrategy.ATTRIBUTE, LocatorStrategy.CSS}
    return Locator(
        strategy=strategy, value=_text(data["value"], f"{where}.value", plain=not selector)
    )


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    fd = os.open(directory / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _looks_like_a_token(name: str) -> bool:
    """Whether a logical name reads like an identifier carrying data, not like words."""
    return sum(char.isdigit() for char in name) > 4 or any(
        len(run) > 16 for run in re.findall("[a-z]+", name)
    )


def _newest_saved(task_dir: Path, task: str, version: int) -> SiteModel:
    """The newest history entry, checked against its own name."""
    model = _read_model(task_dir / _HISTORY_DIRNAME / _history_name(version), task)
    if model.version != version:
        raise SiteModelError(
            f"Expected version {version} in {_history_name(version)}, got {model.version}"
        )
    return model


def _no_constants(name: str) -> Any:
    raise ValueError(f"unexpected constant {name}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _page(data: Any, where: str) -> Page:
    data = _keys(data, where, {"controls", "fingerprint", "page_id", "url_template"})
    controls = _list(data["controls"], f"{where}.controls")
    _at_most(controls, _MAX_CONTROLS, f"{where}.controls")
    fingerprint = _list(data["fingerprint"], f"{where}.fingerprint")
    if not fingerprint:
        raise SiteModelError(f"Expected at least one landmark in {where}.fingerprint, got none")
    _at_most(fingerprint, _MAX_FINGERPRINT, f"{where}.fingerprint")
    parsed = tuple(_control(item, f"{where}.controls[{n}]") for n, item in enumerate(controls))
    names = [control.name for control in parsed]
    if len(set(names)) != len(names):
        raise SiteModelError(f"Expected unique control names in {where}, got a duplicate")
    marks = [
        _text(item, f"{where}.fingerprint[{n}]", plain=True) for n, item in enumerate(fingerprint)
    ]
    return Page(
        controls=parsed,
        fingerprint=tuple(sorted(set(marks))),
        page_id=_text(
            data["page_id"], f"{where}.page_id", pattern=_LOGICAL_NAME, what="a logical name"
        ),
        url_template=_url_template(data["url_template"], f"{where}.url_template"),
    )


def _param(data: Any, where: str) -> FlowParam:
    data = _keys(data, where, {"kind", "name"}, optional={"choices"})
    kind = _enum(ParamKind, data["kind"], f"{where}.kind")
    raw = _list(data.get("choices", []), f"{where}.choices")
    _at_most(raw, _MAX_CHOICES, f"{where}.choices")
    if raw and kind is not ParamKind.CHOICE:
        raise SiteModelError(f"Expected {where}.choices only for a choice parameter, got some")
    if kind is ParamKind.CHOICE and not raw:
        raise SiteModelError(f"Expected {where}.choices to list the options, got none")
    choices = tuple(
        _text(item, f"{where}.choices[{n}]", max_len=_MAX_CHOICE_CHARS, plain=True)
        for n, item in enumerate(raw)
    )
    if len(set(choices)) != len(choices):
        raise SiteModelError(f"Expected {where}.choices without repeats, got a repeat")
    name = _text(data["name"], f"{where}.name", pattern=_LOGICAL_NAME, what="a logical name")
    if name == "item":
        raise SiteModelError(f"Expected {where}.name other than 'item' (the ledger item), got it")
    return FlowParam(choices=choices, kind=kind, name=name)


def _parse(payload: Any, task: str) -> SiteModel:
    data = _keys(
        payload,
        "the model",
        {
            "blumkin_version",
            "flows",
            "hosts",
            "pages",
            "recorded_at",
            "schema_version",
            "task",
            "version",
        },
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise SiteModelError(
            f"Expected schema_version {SCHEMA_VERSION}, got {_shown(data['schema_version'])} "
            "(this model needs a different blumkin version)"
        )
    if data["task"] != task:
        raise SiteModelError(f"Expected a model for task {task!r}, got {_shown(data['task'])}")
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise SiteModelError(
            f"Expected version to be a non-negative integer, got {_shown(version)}"
        )
    hosts = _list(data["hosts"], "hosts")
    if not hosts:
        raise SiteModelError("Expected at least one host, got none")
    page_items = _list(data["pages"], "pages")
    if not page_items:
        raise SiteModelError("Expected at least one page, got none")
    _at_most(page_items, _MAX_PAGES, "pages")
    pages = tuple(_page(item, f"pages[{n}]") for n, item in enumerate(page_items))
    ids = [page.page_id for page in pages]
    if len(set(ids)) != len(ids):
        raise SiteModelError("Expected unique page ids, got a duplicate")
    by_id = {page.page_id: page for page in pages}
    flow_items = _list(data["flows"], "flows")
    _at_most(flow_items, _MAX_FLOWS, "flows")
    recorded_at = _text(
        data["recorded_at"],
        "recorded_at",
        pattern=_TIMESTAMP,
        what="a UTC time",
        digits=True,
    )
    try:
        datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SiteModelError(
            f"Expected recorded_at to be a real UTC time, got {_shown(recorded_at)}"
        ) from None
    return SiteModel(
        blumkin_version=_text(
            data["blumkin_version"],
            "blumkin_version",
            max_len=40,
            pattern=_VERSION,
            what="a version string",
        ),
        flows=tuple(_flow(item, f"flows[{n}]", by_id) for n, item in enumerate(flow_items)),
        hosts=_unique_hosts(hosts),
        pages=pages,
        recorded_at=recorded_at,
        task=task,
        version=version,
    )


def _read_model(path: Path, task: str) -> SiteModel:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError as exc:
        raise SiteModelError(
            f"Expected a regular file at {path}, got {type(exc).__name__} (a symlink is refused)"
        ) from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise SiteModelError(f"Expected a regular file at {path}, got another kind of file")
        if info.st_size > _MAX_FILE_BYTES:
            raise SiteModelError(
                f"Expected at most {_MAX_FILE_BYTES} bytes, "
                f"got {info.st_size}: the model is too large"
            )
        raw = handle.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise SiteModelError(f"Expected at most {_MAX_FILE_BYTES} bytes: the model is too large")
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except (ValueError, RecursionError, MemoryError) as exc:
        raise SiteModelError(
            f"Expected valid JSON without duplicate keys in {path.name}, got a parse error"
        ) from exc
    return _parse(payload, task)


def _render(model: SiteModel) -> str:
    return json.dumps(model.to_payload(), indent=2, sort_keys=True) + "\n"


@contextlib.contextmanager
def _shared_lock(store: Path, task: str) -> Iterator[None]:
    """Hold the store's lock for reading, so a save cannot be seen half done."""
    task_dir = _existing_dir(store, task)
    if task_dir is None or not _exists(task_dir / _LOCK_FILENAME):
        yield
        return
    fd = os.open(task_dir / _LOCK_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def _shown(value: Any) -> str:
    """A short, safe rendering of a rejected value for an error message."""
    if value is None or isinstance(value, bool | int | float):
        return repr(value)[:40]
    if isinstance(value, str):
        return repr(value[:37] + ("..." if len(value) > 37 else ""))
    return f"a {type(value).__name__}"


def _step(
    data: Any,
    where: str,
    pages: Mapping[str, Page],
    controls: set[str],
    params: Mapping[str, ParamKind] | None = None,
) -> FlowStep:
    data = _keys(
        data,
        where,
        {"action", "control", "expect", "page"},
        optional={"bind", "column", "expect_heading", "instruction", "row", "value"},
    )
    declared = params or {}
    action = _text(data["action"], f"{where}.action", pattern=_LOGICAL_NAME, what="an action name")
    expect_heading = data.get("expect_heading")
    if expect_heading is not None:
        expect_heading = _text(
            expect_heading, f"{where}.expect_heading", max_len=_MAX_HEADING, plain=True
        )
        if not expect_heading.strip():
            raise SiteModelError(f"Expected {where}.expect_heading with some text, got blanks")
    instruction = data.get("instruction")
    if (action == _HANDOFF) != (instruction is not None):
        raise SiteModelError(
            f"Expected {where}.instruction on a handoff step and nowhere else, got "
            f"{'none' if action == _HANDOFF else 'one on ' + repr(action)}"
        )
    if instruction is not None:
        instruction = _text(
            instruction, f"{where}.instruction", max_len=_MAX_INSTRUCTION, plain=True
        )
        if (
            data["control"] is not None
            or data.get("bind") is not None
            or data["page"] is not None
            or data["expect"] is not None
        ):
            raise SiteModelError(
                f"Expected a handoff step without a control, a bind, a page or an expect in {where}"
            )
    if expect_heading is not None and action not in _HEADING_ACTIONS:
        raise SiteModelError(
            f"Expected {where}.expect_heading only on a click, press, fill, upload, goto or "
            f"handoff step, got it on {action!r}"
        )
    row, column, literal = _cell_keys(data, where, action, declared)
    bind = data.get("bind")
    if bind is not None:
        bind = _text(bind, f"{where}.bind", pattern=_LOGICAL_NAME, what="a logical name")
        if action not in _BOUND_ACTIONS and action != _CELL:
            raise SiteModelError(
                f"Expected {where}.bind only on a fill, upload or cell step, got it on {action!r}"
            )
        if action == _CELL and literal is not None:
            raise SiteModelError(f"Expected {where} with a bind or a value, not both")
        if bind not in declared:
            raise SiteModelError(
                f"Expected {where}.bind to name a parameter of the flow, got {_shown(bind)}"
            )
        # A file path is checked as one and never typed into a field; text never uploads.
        if action == _CELL and declared[bind] is ParamKind.FILE:
            raise SiteModelError(f"Expected {where}.bind of a non-file parameter, got a file one")
        if action != _CELL and (declared[bind] is ParamKind.FILE) != (action == "upload"):
            raise SiteModelError(
                f"Expected {where}.bind of a {'file' if action == 'upload' else 'non-file'} "
                f"parameter for a {action} step, got {_shown(bind)}"
            )
    elif declared and action in _BOUND_ACTIONS:
        raise SiteModelError(
            f"Expected {where} to bind a parameter (the flow declares params), got none"
        )
    control = data["control"]
    page = data["page"]
    if control is not None:
        control = _text(control, f"{where}.control", pattern=_LOGICAL_NAME, what="a logical name")
        if control not in controls:
            raise SiteModelError(
                f"Expected a control that exists in {where}, got {_shown(control)}"
            )
    if page is not None:
        page = _text(page, f"{where}.page", pattern=_LOGICAL_NAME, what="a logical name")
        if page not in pages:
            raise SiteModelError(f"Expected a page that exists in {where}, got {_shown(page)}")
        if control is not None and control not in {c.name for c in pages[page].controls}:
            raise SiteModelError(
                f"Expected a control that belongs to page {page} in {where}, got {_shown(control)}"
            )
    expect = data["expect"]
    if expect is not None:
        expect = _text(expect, f"{where}.expect", plain=True)
    return FlowStep(
        action=action,
        bind=bind,
        column=column,
        control=control,
        expect=expect,
        expect_heading=expect_heading,
        instruction=instruction,
        page=page,
        row=row,
        value=literal,
    )


def _cell_keys(
    data: Mapping[str, Any], where: str, action: str, declared: Mapping[str, ParamKind]
) -> tuple[str | None, str | None, str | None]:
    """The row, column and literal value of a `cell` step; none of them on any other step."""
    present = [key for key in ("row", "column", "value") if data.get(key) is not None]
    if action != _CELL:
        if present:
            raise SiteModelError(
                f"Expected {where}.{present[0]} only on a cell step, got it on {action!r}"
            )
        return None, None, None
    if data["control"] is not None:
        raise SiteModelError(f"Expected a cell step without a control in {where}")
    resolved: dict[str, str] = {}
    for key in ("row", "column"):
        raw = data.get(key)
        if raw is None:
            raise SiteModelError(f"Expected {where}.{key} on a cell step, got none")
        text = _text(raw, f"{where}.{key}", max_len=_MAX_CELL_REF, plain=True)
        if text.startswith("text:") and not text[5:].strip():
            raise SiteModelError(f"Expected {where}.{key} with header text after 'text:', got none")
        if text.startswith("$") and not text.startswith("$$"):
            name = text[1:]
            if declared.get(name) in (None, ParamKind.FILE):
                raise SiteModelError(
                    f"Expected {where}.{key} to name a non-file parameter of the flow after "
                    f"'$', got {_shown(text)}"
                )
        resolved[key] = text
    literal = data.get("value")
    if literal is not None:
        literal = (
            ""
            if literal == ""
            else _text(literal, f"{where}.value", max_len=_MAX_CELL_VALUE, plain=True)
        )
    return resolved["row"], resolved["column"], literal


def _task_dir(store: Path, task: str) -> Path:
    return store / task


def _text(
    value: Any,
    where: str,
    *,
    max_len: int = _MAX_TEXT,
    pattern: re.Pattern[str] | None = None,
    plain: bool = False,
    what: str = "text",
    digits: bool = False,
) -> str:
    if not isinstance(value, str) or not value:
        raise SiteModelError(f"Expected {where} to be non-empty text, got {_shown(value)}")
    if len(value) > max_len:
        raise SiteModelError(
            f"Expected {where} to be at most {max_len} characters, got {len(value)}"
        )
    if _BIDI_AND_ZERO_WIDTH.search(value) or any(_is_unprintable(char) for char in value):
        raise SiteModelError(
            f"Expected {where} without control, separator or bidirectional characters, "
            f"got {_shown(value)}"
        )
    if unicodedata.normalize("NFC", value) != value:
        raise SiteModelError(f"Expected {where} in NFC form, got {_shown(value)}")
    if not digits and _DIGIT_RUN.search(_DIGIT_SEPARATORS.sub("", value)):
        raise SiteModelError(
            f"Expected {where} without a run of six or more digits (it may be a typed value), "
            f"got {_shown(value)}"
        )
    if plain and _PLAIN_FORBIDDEN.search(value):
        raise SiteModelError(
            f"Expected {where} without '=' or quotes (it may be a typed value), got {_shown(value)}"
        )
    if pattern is not None and not pattern.fullmatch(value):
        raise SiteModelError(f"Expected {where} to be {what}, got {_shown(value)}")
    if pattern is _LOGICAL_NAME and _looks_like_a_token(value):
        raise SiteModelError(
            f"Expected {where} to be made of short words, got {_shown(value)} "
            "(it looks like a token)"
        )
    return value


def _unique_hosts(items: list[Any]) -> tuple[str, ...]:
    hosts = tuple(_host(item, f"hosts[{n}]") for n, item in enumerate(items))
    if len(set(hosts)) != len(hosts):
        raise SiteModelError("Expected unique hosts, got a duplicate")
    return hosts


def _url_template(value: Any, where: str) -> str:
    text = _text(value, where)
    segments = text.split("/")
    if (
        segments[0] != ""
        or len(segments) < 2
        or any(not _URL_SEGMENT.fullmatch(seg) for seg in segments[1:-1])
        or not (_URL_SEGMENT.fullmatch(segments[-1]) or (segments[-1] == "" and len(segments) > 2))
        and text != "/"
    ):
        raise SiteModelError(
            f"Expected {where} to be a path of words and {{placeholders}} without query, "
            f"fragment, user info, escapes or empty segments, got {_shown(value)}"
        )
    for segment in segments:
        if not segment.startswith("{") and sum(char.isupper() for char in segment) > 2:
            raise SiteModelError(
                f"Expected {where} segments to be plain words, got {_shown(segment)} "
                "(mixed case like this looks like a token)"
            )
    return text


def _versions(store: Path, task: str) -> list[int]:
    """The saved versions: regular files with the exact canonical name, each counted once."""
    history = _existing_dir(store, task, _HISTORY_DIRNAME)
    if history is None:
        return []
    versions: set[int] = set()
    for entry in history.iterdir():
        match = _HISTORY_FILE.fullmatch(entry.name)
        if match is None or entry.name != _history_name(int(match.group(1))):
            continue
        try:
            if not stat.S_ISREG(entry.lstat().st_mode):
                continue
        except FileNotFoundError:
            continue
        versions.add(int(match.group(1)))
    return sorted(versions)
