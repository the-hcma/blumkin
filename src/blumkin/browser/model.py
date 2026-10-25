"""Site model: a task's recorded interaction map, kept on disk (issue #485).

A model describes the structure of a website well enough that later runs can skip
exploring it: the pages (a URL template and a fingerprint of their landmark controls), the
controls on each page (a logical name, a kind, whether clicking it could finalize a form,
and ordered locator strategies) and the flows that chain them. It lives in
`<config dir>/browser-models/<task>/model.json`, with every saved version kept under
`history/`, in `0700` directories and `0600` files.

A model never holds anything the user typed, a cookie or any other personal data. That is
enforced by the schema itself: every object has a fixed set of keys, anything else is a
load error, every string is short and free of control and bidirectional characters, and a
URL template is limited to path characters, so a query string (where values live) cannot be
stored. The hosts must be concrete host names and can be checked against the task's
`allowed_hosts` when a model is saved.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import ensure_private_dir, validate_task_name, write_private


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


@dataclass(frozen=True, slots=True)
class FlowStep:
    """One step of a flow: an action on a control of a page, and the landmark expected next."""

    action: str
    control: str | None
    expect: str | None
    page: str | None


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
            "flows": [
                {
                    "name": flow.name,
                    "steps": [
                        {
                            "action": step.action,
                            "control": step.control,
                            "expect": step.expect,
                            "page": step.page,
                        }
                        for step in flow.steps
                    ],
                }
                for flow in self.flows
            ],
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


def list_versions(config_dir: Path, task: str) -> list[int]:
    """The saved versions of the task's model, oldest first."""
    validate_task_name(task)
    history = _task_dir(config_dir, task) / _HISTORY_DIRNAME
    if not history.is_dir():
        return []
    versions = []
    for entry in history.iterdir():
        match = _HISTORY_FILE.fullmatch(entry.name)
        if match:
            versions.append(int(match.group(1)))
    return sorted(versions)


def load_model(config_dir: Path, task: str) -> SiteModel | None:
    """The task's current model, or None when none was saved."""
    validate_task_name(task)
    path = _task_dir(config_dir, task) / _CURRENT_FILENAME
    if not _exists(path):
        return None
    return _read_model(path, task)


def load_version(config_dir: Path, task: str, version: int) -> SiteModel:
    """One saved version of the task's model."""
    validate_task_name(task)
    path = _task_dir(config_dir, task) / _HISTORY_DIRNAME / _history_name(version)
    if not _exists(path):
        raise SiteModelError(f"Expected version {version} of the {task!r} model to exist, got none")
    return _read_model(path, task)


def save_model(
    config_dir: Path,
    model: SiteModel,
    *,
    host_allowed: Callable[[str], bool] | None = None,
) -> SiteModel:
    """Validate and store `model` as the task's next version; returns it with that version."""
    validate_task_name(model.task)
    _parse(model.to_payload(), model.task)
    if host_allowed is not None:
        for host in model.hosts:
            if not host_allowed(host):
                raise SiteModelError(
                    f"Expected every host to be allowed for task {model.task!r}, got {host!r}"
                )
    root = ensure_private_dir(config_dir / _STORE_DIRNAME)
    task_dir = ensure_private_dir(root / model.task)
    history = ensure_private_dir(task_dir / _HISTORY_DIRNAME)
    with _locked(task_dir):
        previous = list_versions(config_dir, model.task)
        version = (previous[-1] if previous else 0) + 1
        stored = SiteModel(
            blumkin_version=model.blumkin_version,
            flows=model.flows,
            hosts=model.hosts,
            pages=model.pages,
            recorded_at=model.recorded_at,
            task=model.task,
            version=version,
        )
        text = json.dumps(stored.to_payload(), indent=2, sort_keys=True) + "\n"
        write_private(history / _history_name(version), text)
        write_private(task_dir / _CURRENT_FILENAME, text)
    return stored


_BIDI_AND_ZERO_WIDTH = re.compile("[؜​-‏‪-‮⁠⁦-⁩﻿]")
_CURRENT_FILENAME = "model.json"
_HISTORY_DIRNAME = "history"
_HISTORY_FILE = re.compile(r"model-(\d{4,})\.json")
_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_LOCK_FILENAME = ".lock"
_LOGICAL_NAME = re.compile(r"[a-z][a-z0-9_.\[\]-]{0,63}")
_MAX_CONTROLS = 500
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_FINGERPRINT = 200
_MAX_FLOWS = 50
_MAX_LOCATORS = 10
_MAX_PAGES = 200
_MAX_STEPS = 200
_MAX_TEXT = 200
_STORE_DIRNAME = "browser-models"
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_URL_TEMPLATE = re.compile(r"/[A-Za-z0-9._~/{}-]*")


def _at_most(items: Sequence[Any], limit: int, where: str) -> None:
    if len(items) > limit:
        raise SiteModelError(f"Expected at most {limit} items in {where}, got {len(items)}")


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


def _enum[E: StrEnum](kind: type[E], value: Any, where: str) -> E:
    try:
        return kind(value)
    except ValueError:
        allowed = ", ".join(member.value for member in kind)
        raise SiteModelError(
            f"Expected {where} to be one of {allowed}, got {_shown(value)}"
        ) from None


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _flow(data: Any, where: str, pages: Mapping[str, Page]) -> Flow:
    data = _keys(data, where, {"name", "steps"})
    steps = _list(data["steps"], f"{where}.steps")
    _at_most(steps, _MAX_STEPS, f"{where}.steps")
    controls = {c.name for page in pages.values() for c in page.controls}
    return Flow(
        name=_text(data["name"], f"{where}.name", pattern=_LOGICAL_NAME, what="a logical name"),
        steps=tuple(
            _step(item, f"{where}.steps[{n}]", pages, controls) for n, item in enumerate(steps)
        ),
    )


def _history_name(version: int) -> str:
    return f"model-{version:04d}.json"


def _host(value: Any, where: str) -> str:
    text = _text(value, where, max_len=253)
    if not _HOST.fullmatch(text):
        raise SiteModelError(
            f"Expected {where} to be a lowercase host name without scheme, port, path or "
            f"user info, got {_shown(value)}"
        )
    return text


def _keys(data: Any, where: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise SiteModelError(f"Expected {where} to be an object, got {type(data).__name__}")
    for key in data:
        if key not in allowed:
            raise SiteModelError(
                f"Expected only {sorted(allowed)} in {where}, got unexpected key {_shown(key)}"
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
    return Locator(
        strategy=_enum(LocatorStrategy, data["strategy"], f"{where}.strategy"),
        value=_text(data["value"], f"{where}.value"),
    )


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    fd = os.open(directory / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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
    marks = [_text(item, f"{where}.fingerprint[{n}]") for n, item in enumerate(fingerprint)]
    return Page(
        controls=parsed,
        fingerprint=tuple(sorted(set(marks))),
        page_id=_text(
            data["page_id"], f"{where}.page_id", pattern=_LOGICAL_NAME, what="a logical name"
        ),
        url_template=_text(
            data["url_template"],
            f"{where}.url_template",
            pattern=_URL_TEMPLATE,
            what="a path template without query, fragment, user info or escapes",
        ),
    )


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
    if data["schema_version"] != SCHEMA_VERSION:
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
    recorded_at = _text(data["recorded_at"], "recorded_at", pattern=_TIMESTAMP, what="a UTC time")
    try:
        datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SiteModelError(
            f"Expected recorded_at to be a real UTC time, got {_shown(recorded_at)}"
        ) from None
    return SiteModel(
        blumkin_version=_text(data["blumkin_version"], "blumkin_version", max_len=40),
        flows=tuple(_flow(item, f"flows[{n}]", by_id) for n, item in enumerate(flow_items)),
        hosts=tuple(_host(item, f"hosts[{n}]") for n, item in enumerate(hosts)),
        pages=pages,
        recorded_at=recorded_at,
        task=task,
        version=version,
    )


def _read_model(path: Path, task: str) -> SiteModel:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
        raw = handle.read()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SiteModelError(f"Expected valid JSON in {path.name}, got a parse error") from exc
    return _parse(payload, task)


def _shown(value: Any) -> str:
    """A short, safe rendering of a rejected value for an error message."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _step(data: Any, where: str, pages: Mapping[str, Page], controls: set[str]) -> FlowStep:
    data = _keys(data, where, {"action", "control", "expect", "page"})
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
    expect = data["expect"]
    if expect is not None:
        expect = _text(expect, f"{where}.expect")
    return FlowStep(
        action=_text(
            data["action"], f"{where}.action", pattern=_LOGICAL_NAME, what="an action name"
        ),
        control=control,
        expect=expect,
        page=page,
    )


def _task_dir(config_dir: Path, task: str) -> Path:
    return config_dir / _STORE_DIRNAME / task


def _text(
    value: Any,
    where: str,
    *,
    max_len: int = _MAX_TEXT,
    pattern: re.Pattern[str] | None = None,
    what: str = "text",
) -> str:
    if not isinstance(value, str) or not value:
        raise SiteModelError(f"Expected {where} to be non-empty text, got {_shown(value)}")
    if len(value) > max_len:
        raise SiteModelError(
            f"Expected {where} to be at most {max_len} characters, got {len(value)}"
        )
    if _BIDI_AND_ZERO_WIDTH.search(value) or any(
        unicodedata.category(char).startswith("C") for char in value
    ):
        raise SiteModelError(
            f"Expected {where} without control or bidirectional characters, got {_shown(value)}"
        )
    if pattern is not None and not pattern.fullmatch(value):
        raise SiteModelError(f"Expected {where} to be {what}, got {_shown(value)}")
    return value
