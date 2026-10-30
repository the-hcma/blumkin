"""The submit guard: a browser task fills in a form but never finalizes it (issue #467).

`GuardedDriver` is the only object a task gets. It has no way to submit a form and no way
to run arbitrary script. Clicks and key presses go through checks enforced here in code,
not left to the task's prompt, and the checks are deny-by-default:

- a control is clicked only if the task declared its exact name, and only with a short
  list of clickable roles;
- the element itself is inspected before it is clicked, on the same element handle, so the
  page cannot swap it in between: a submit control (judged by the browser's canonical
  `type`, so `<button type="bogus">` counts), a control with a `formaction`, a link that
  runs script and a click handler that submits a form are all refused;
- a control whose accessible name starts a finalizing word (submit, send, file, confirm,
  pay, ...) is refused, and the task can only add words to that list;
- only navigation keys and single printable characters may be pressed (never Enter,
  Space or a modifier combination), and never on a button-like element;
- while attached, the driver aborts every non-GET navigation, whatever triggered it: the
  backstop for a submit no static check can see.

Deny always wins over the task's allow list. A refusal never reaches the page and is
logged by control name only. The task gets this object through the CLI, which builds one
per command; it never holds the raw driver.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blumkin.browser.cdp import (
    DEFAULT_ACTION_TIMEOUT_S,
    DEFAULT_CLICK_WAIT_S,
    FIELD_ROLES,
    BrowserActionError,
    ControlInfo,
    DownloadedFile,
    FieldTarget,
    FrameCapture,
    FrameInfo,
    PageText,
    RawBrowserDriver,
    attach,
)
from blumkin.browser.profiles import BrowserProfile

_LOGGER = logging.getLogger(__name__)

DEFAULT_DENY_NAMES: tuple[str, ...] = (
    "approve",
    "authorize",
    "buy",
    "checkout",
    "complete",
    "confirm",
    "file",
    "finish",
    "pay",
    "place order",
    "publish",
    "register",
    "resend",
    "resubmit",
    "send",
    "submit",
    "transmit",
)


class SubmitBlockedError(BrowserActionError):
    """The guard refused an action that could finalize the task's form."""


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    """What a task may touch. `allowed_controls` are exact accessible names."""

    allowed_controls: frozenset[str] = frozenset()
    #: Strict mode: also swallow background writes (see `block_form_submission`).
    block_background_writes: bool = False
    extra_deny_names: tuple[str, ...] = field(default=())


class GuardedDriver:
    """The `BrowserDriver` a task uses; wraps the raw driver with the submit guard."""

    def __init__(self, raw: RawBrowserDriver, policy: GuardPolicy) -> None:
        self._allowed = frozenset(_exact(name) for name in policy.allowed_controls)
        self._deny = _deny_stems((*DEFAULT_DENY_NAMES, *policy.extra_deny_names))
        self._raw = raw
        raw.block_form_submission(background_writes=policy.block_background_writes)

    def aria_snapshot(self) -> str:
        return self._raw.aria_snapshot()

    def check_click(self, name: str, role: str | None = None) -> None:
        """Raise `SubmitBlockedError` unless a click on `name` is allowed (no page access)."""
        action = f"click {name!r}"
        if role is not None and role not in _CLICKABLE_ROLES:
            raise self._refuse(action, f"the role {role!r} is not one a task may click")
        if _is_denied(name, self._deny):
            raise self._refuse(action, "its name is a finalizing action")
        if _exact(name) not in self._allowed:
            raise self._refuse(action, "the control is not declared by the task")

    def click(
        self,
        name: str,
        role: str | None = None,
        wait_s: float = DEFAULT_CLICK_WAIT_S,
        *,
        nth: int | None = None,
    ) -> str:
        """Click a declared control and return the role it was found in.

        With no `role`, the control is looked up by name across the clickable roles and must
        be in exactly one of them. The policy is checked before the page is touched, and the
        control is waited for up to `wait_s` seconds, so a wrong role fails in seconds.

        A name the page legitimately repeats is refused unless `nth` (1-based, DOM order) says
        which match is meant. That narrows the lookup only: the name must still be declared,
        a finalizing name is still refused, and the chosen element is vetted before the click.
        """
        if nth is not None and (isinstance(nth, bool) or not isinstance(nth, int) or nth < 1):
            raise ValueError(f"Expected nth to be a positive whole number, got {nth!r}")
        chosen, action = self._locate(name, role, wait_s, nth)
        self._raw.checked_click(name, chosen, lambda info: self._vet_control(action, info), nth=nth)
        return chosen

    def close(self) -> None:
        self._raw.close()

    def download(
        self,
        name: str,
        role: str | None = None,
        *,
        save_to: Path,
        wait_s: float = DEFAULT_CLICK_WAIT_S,
    ) -> DownloadedFile:
        """Click a declared control that starts a download and save the file to `save_to`.

        The same policy as `click`, found and vetted the same way; where the file may go and
        whether it may be kept are the caller's checks.
        """
        chosen, action = self._locate(name, role, wait_s)
        return self._raw.checked_download(
            name,
            chosen,
            lambda info: self._vet_control(action, info),
            save_to=save_to,
            timeout_s=DEFAULT_ACTION_TIMEOUT_S,
        )

    def fill(self, label: str, value: str, target: FieldTarget | None = None) -> str | None:
        """Fill a field; returns the value it holds afterwards (None for a password field)."""
        self._check_target(target)
        return self._raw.fill(label, value, target)

    def frame_aria_snapshot(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> str:
        return self._raw.frame_aria_snapshot(index, is_allowed, expect_host, wait_s=wait_s)

    def frame_list(
        self, is_allowed: Callable[[str], bool], *, wait_s: float = 0.0
    ) -> tuple[FrameInfo, ...]:
        return self._raw.frame_list(is_allowed, wait_s=wait_s)

    def goto(self, url: str) -> None:
        self._raw.goto(url)

    def press(self, label: str, key: str) -> None:
        action = f"press {key!r} in {label!r}"
        if not _key_is_safe(key):
            raise self._refuse(action, "only navigation keys and single printable characters")
        info = self._raw.describe_field(label)
        if _is_button_like(info):
            raise self._refuse(action, "the field is a button, not a text field")
        self._raw.raw_press(label, key)

    def read_frame_text(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> PageText:
        return self._raw.read_frame_text(index, is_allowed, expect_host, wait_s=wait_s)

    def read_text(self) -> PageText:
        return self._raw.read_text()

    def scroll_frame_text(
        self, is_allowed: Callable[[str], bool], *, max_steps: int, wait_s: float
    ) -> FrameCapture:
        return self._raw.scroll_frame_text(is_allowed, max_steps=max_steps, wait_s=wait_s)

    def title(self) -> str:
        return self._raw.title()

    def select(self, label: str, option: str, target: FieldTarget | None = None) -> str | None:
        """Choose an option in a dropdown; an option or field that finalizes a form is refused."""
        action = f"select {option!r} in {label!r}"
        if _is_denied(option, self._deny) or _is_denied(label, self._deny):
            raise self._refuse(action, "its name is a finalizing action")
        if target is not None and target.role is not None and target.role not in _SELECT_ROLES:
            raise self._refuse(action, f"the role {target.role!r} is not one a dropdown has")
        self._check_target(target)
        return self._raw.select(
            label,
            option,
            target,
            lambda info: self._vet_control(action, info),
            lambda name: self._vet_field_name(action, name),
        )

    def upload(self, label: str, path: Path, target: FieldTarget | None = None) -> None:
        self._check_target(target)
        self._raw.upload(label, path, target)

    @property
    def url(self) -> str:
        return self._raw.url

    def _check_target(self, target: FieldTarget | None) -> None:
        """Reject a malformed narrowing before the page is touched."""
        if target is None:
            return
        nth = target.nth
        if nth is not None and (isinstance(nth, bool) or not isinstance(nth, int) or nth < 1):
            raise ValueError(f"Expected nth to be a positive whole number, got {nth!r}")
        if target.frame_host is not None and target.frame is None:
            raise ValueError("Expected a frame number with a frame host, got none")
        if target.role is not None and target.role not in FIELD_ROLES:
            raise ValueError(
                f"Expected a field role ({', '.join(sorted(FIELD_ROLES))}), got {target.role!r}"
            )

    def _locate(
        self, name: str, role: str | None, wait_s: float, nth: int | None = None
    ) -> tuple[str, str]:
        """Check the policy, then find `name` on the page: (the role it is in, the action)."""
        action = f"click {name!r}"
        self.check_click(name, role)
        roles = (role,) if role is not None else tuple(sorted(_CLICKABLE_ROLES))
        found = self._raw.matching_roles(name, roles, wait_s, nth=nth)
        label = role if role is not None else "control"
        if not found:
            tried = "" if role is not None else f" in any of {', '.join(roles)}"
            raise BrowserActionError(
                f"Expected a {label} named {name!r} within {wait_s:g} seconds{tried}, got none"
            )
        if len(found) > 1:
            raise BrowserActionError(
                f"Expected {name!r} in one role, got {', '.join(found)}; pass --role to choose"
            )
        return next(iter(found)), action

    def _vet_field_name(self, action: str, name: str) -> None:
        """Refuse a field whose own accessible name is a finalizing action."""
        if _is_denied(name, self._deny):
            raise self._refuse(action, "the field's own name is a finalizing action")

    def _refuse(self, action: str, reason: str) -> SubmitBlockedError:
        _LOGGER.info("browser guard refused to %s: %s", action, reason)
        return SubmitBlockedError(
            f"Expected to {action}, got a refusal: {reason}. A browser task never submits; "
            "stop and hand the final step to the user"
        )

    def _vet_control(self, action: str, info: ControlInfo) -> None:
        """Raise if the element about to be clicked could finalize a form."""
        reason = vet_control(info)
        if reason is not None:
            raise self._refuse(action, reason)


def attach_guarded(
    profile: BrowserProfile,
    policy: GuardPolicy,
    *,
    attach_raw: Callable[..., RawBrowserDriver] = attach,
    timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
    **attach_kwargs: Any,
) -> GuardedDriver:
    """Attach to the task browser and return the guarded driver tasks must use."""
    raw = attach_raw(profile, timeout_s=timeout_s, **attach_kwargs)
    try:
        return GuardedDriver(raw, policy)
    except BaseException:
        raw.close()
        raise


_BUTTON_LIKE_INPUT_TYPES = frozenset({"button", "image", "reset", "submit"})
#: Roles a dropdown has; `select` never goes through a button, link or other clickable role.
_SELECT_ROLES = frozenset({"combobox", "listbox", "searchbox", "textbox"})

_CLICKABLE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "link",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "switch",
        "tab",
        "treeitem",
    }
)
_NAVIGATION_KEYS = frozenset(
    {
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "Backspace",
        "Delete",
        "End",
        "Escape",
        "Home",
        "PageDown",
        "PageUp",
        "Shift+Tab",
        "Tab",
    }
)
_SCRIPT_SCHEMES = ("data:", "javascript:", "vbscript:")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def is_finalizing_name(name: str, extra_deny: Sequence[str] = ()) -> bool:
    """Whether a control name starts a finalizing word (submit, send, pay, ...)."""
    return _is_denied(name, _deny_stems((*DEFAULT_DENY_NAMES, *extra_deny)))


def vet_control(info: ControlInfo) -> str | None:
    """Why clicking this element could finalize a form, or None when nothing says it could."""
    if _is_submit_control(info):
        return "it is a submit control"
    if info.has_formaction:
        return "it carries a formaction override"
    if _runs_script(info.href):
        return "it is a link that runs script"
    if "submit" in info.inline_handler.casefold():
        return "its click handler submits a form"
    return None


def _deny_stems(words: tuple[str, ...]) -> tuple[str, ...]:
    """Reduce each deny word to the form names are matched in: lower-case words joined by
    single spaces with punctuation removed, so `e-mail` matches the name `E-mail it`."""
    stems = (" ".join(re.findall(r"[^\W_]+", _folded(word))) for word in words)
    return tuple(stem for stem in stems if stem)


def _exact(text: str) -> str:
    """Normalize for the allow list: NFKC, no zero-width characters, collapsed spaces; the
    case is kept because the browser matches an accessible name exactly."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).split())


def _folded(text: str) -> str:
    return _exact(text).casefold()


def _is_button_like(info: ControlInfo) -> bool:
    if info.tag == "button" or info.tag == "a":
        return True
    return info.tag == "input" and info.input_type in _BUTTON_LIKE_INPUT_TYPES


def _is_denied(name: str, stems: tuple[str, ...]) -> bool:
    folded = _folded(name)
    words = re.findall(r"[^\W_]+", folded)
    spaced = " ".join(words)
    # The same words run together, with the offset where each word starts, so a deny word
    # written `e-mail` also catches `Email`.
    compact = "".join(words)
    starts, offset = [], 0
    for word in words:
        starts.append(offset)
        offset += len(word)
    for stem in stems:
        if " " in stem:
            if re.search(rf"(?<![^\W_]){re.escape(stem)}", spaced):
                return True
            glued = stem.replace(" ", "")
            if any(compact.startswith(glued, start) for start in starts):
                return True
        elif any(word.startswith(stem) for word in words):
            return True
    return False


def _is_submit_control(info: ControlInfo) -> bool:
    if info.tag == "input" and info.input_type in ("image", "submit"):
        return True
    return info.tag == "button" and info.input_type == "submit" and info.in_form


def _key_is_safe(key: str) -> bool:
    if key in _NAVIGATION_KEYS:
        return True
    return len(key) == 1 and key.isprintable() and not key.isspace()


def _runs_script(href: str) -> bool:
    cleaned = "".join(href.split()).casefold()
    return cleaned.startswith(_SCRIPT_SCHEMES)
