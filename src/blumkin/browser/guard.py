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
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from blumkin.browser.cdp import (
    DEFAULT_ACTION_TIMEOUT_S,
    DEFAULT_CLICK_WAIT_S,
    FIELD_ROLES,
    INSPECT_ROLES,
    AccountPick,
    BrowserActionError,
    CellEdit,
    ControlInfo,
    DownloadedFile,
    FieldInspection,
    FieldReading,
    FieldTarget,
    FrameCapture,
    FrameInfo,
    OptionsListing,
    PageSettled,
    PageText,
    RawBrowserDriver,
    RequestEntry,
    UploadObservation,
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

#: Names a deny-list task (`allow_all_controls`) may not click unless it lists them by name in
#: `allowed_controls`: actions that destroy or copy what the form holds. They never apply to a
#: task that declares every control, which already chose each name.
DESTRUCTIVE_DENY_NAMES: tuple[str, ...] = (
    "delete",
    "discard",
    "duplicate",
    "remove",
    "void",
)

#: Longest value `edit_cell` types into a grid cell.
MAX_CELL_VALUE_CHARS = 80

#: Fixed letters or digits a control pattern must keep (a bare `*` would declare everything).
MIN_PATTERN_LITERAL = 4

#: Most `*` a control pattern may hold; every one is a place the page chooses the text.
MAX_PATTERN_STARS = 2


class SubmitBlockedError(BrowserActionError):
    """The guard refused an action that could finalize the task's form."""


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    """What a task may touch. `allowed_controls` are exact accessible names."""

    #: Deny-list mode (off by default): a link, tab, tree item, option or checkbox need not be
    #: declared, and everything that refuses a click still applies (the finalizing words,
    #: `extra_deny_names`, the destructive names, the element vetting). A button and a menu item
    #: stay individually declared.
    allow_all_controls: bool = False
    allowed_controls: frozenset[str] = frozenset()
    #: Strict mode: also swallow background writes (see `block_form_submission`).
    block_background_writes: bool = False
    #: Exact names of buttons that confirm a dialog's own rows; see `vet_control`.
    dialog_confirm: frozenset[str] = frozenset()
    #: A dialog confirm that is a submit control may be clicked in a popup found by its
    #: landmarks although the popup sits in the page's own form; see `vet_control`.
    dialog_confirm_in_form: bool = False
    #: Exact names of controls that together mark a popup the page does not flag as a dialog;
    #: see `BrowserTaskPolicy.dialog_landmarks`.
    dialog_landmarks: tuple[str, ...] | tuple[tuple[str, ...], ...] = ()
    #: Exact names of buttons the user declared as draft calculations (`Calculate`): a
    #: submit-type button of that name is spared the submit check and nothing else; see
    #: `vet_control`.
    draft_controls: frozenset[str] = frozenset()
    extra_deny_names: tuple[str, ...] = field(default=())


class GuardedDriver:
    """The `BrowserDriver` a task uses; wraps the raw driver with the submit guard."""

    def __init__(self, raw: RawBrowserDriver, policy: GuardPolicy) -> None:
        validate_control_patterns(policy.allowed_controls, policy.extra_deny_names)
        self._allow_all = policy.allow_all_controls
        self._allowed = frozenset(
            _exact(name) for name in policy.allowed_controls if "*" not in name
        )
        self._patterns = tuple(
            _pattern_regex(name) for name in sorted(policy.allowed_controls) if "*" in name
        )
        self._declared = tuple(sorted(set(policy.allowed_controls)))
        self._confirm_names = tuple(sorted(set(policy.dialog_confirm)))
        self._deny = _deny_stems((*DEFAULT_DENY_NAMES, *policy.extra_deny_names))
        self._destructive = _deny_stems(DESTRUCTIVE_DENY_NAMES)
        validate_dialog_confirm(policy.dialog_confirm, policy.extra_deny_names)
        landmark_sets = landmark_groups(policy.dialog_landmarks)
        for group in landmark_sets:
            validate_dialog_landmarks(group, policy.extra_deny_names)
        if landmark_sets and not policy.dialog_confirm:
            raise SubmitBlockedError(
                "Expected dialog_confirm names alongside dialog_landmarks, got none; "
                "the landmarks only locate the popup a dialog confirm is clicked in"
            )
        self._confirm = frozenset(_exact(name) for name in policy.dialog_confirm)
        self._confirm_in_form = policy.dialog_confirm_in_form
        self._draft_names = tuple(sorted(set(policy.draft_controls)))
        validate_draft_controls(policy.draft_controls, policy.extra_deny_names)
        self._draft = frozenset(_exact(name) for name in policy.draft_controls)
        self._raw = raw
        raw.block_form_submission(background_writes=policy.block_background_writes)
        setter = getattr(raw, "set_dialog_landmarks", None)
        if landmark_sets and setter is not None:
            setter(landmark_sets)

    def aria_snapshot(self) -> str:
        return self._raw.aria_snapshot()

    def check_click(
        self,
        name: str,
        role: str | None = None,
        *,
        confirm_ok: bool = True,
        declared_only: bool = False,
    ) -> None:
        """Raise `SubmitBlockedError` unless a click on `name` is allowed (no page access).

        A dialog confirm name passes only with `confirm_ok`; a download is never a dialog's
        confirm, so it takes `allowed_controls` names alone. `declared_only` also leaves out
        deny-list mode: the control must be listed in `allowed_controls` (a download, or a
        tab a flow switches through).
        """
        action = f"click {name!r}"
        if role is not None and role not in CLICKABLE_ROLES:
            raise self._refuse(action, f"the role {role!r} is not one a task may click")
        if _is_denied(name, self._deny):
            raise self._refuse(action, "its name is a finalizing action")
        # A draft name is a button's: a link, tab or menu item of that name is not covered.
        listed = _exact(name) in self._confirm or (
            _exact(name) in self._draft and role in (None, "button")
        )
        if not self._is_declared(name, role, declared_only=declared_only) and not (
            confirm_ok and listed
        ):
            raise self._refuse(
                action, f"the control is not declared by the task{self._declared_hint()}"
            )

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
        self._raw.checked_click(
            name, chosen, lambda info: self._vet_control(action, info, name, chosen), nth=nth
        )
        return chosen

    def close(self) -> None:
        self._raw.close()

    def dismiss(self) -> None:
        """Press Escape on the page to close an open dialog or menu; it types nothing."""
        self._raw.dismiss()

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
        chosen, action = self._locate(name, role, wait_s, confirm_ok=False, declared_only=True)
        return self._raw.checked_download(
            name,
            chosen,
            lambda info: self._vet_control(action, info),
            save_to=save_to,
            timeout_s=DEFAULT_ACTION_TIMEOUT_S,
        )

    def edit_cell(
        self, row: str, column: str, value: str | None = None, *, grid: int = 1
    ) -> CellEdit:
        """Read one cell of an Oracle JET data grid; with a `value`, select it, type and commit.

        The cell is vetted like a click on the element itself and the keys go only to the grid;
        the value is a short printable line (an empty one clears the cell), and a read clicks
        nothing. Enter is never pressed, so nothing is submitted.
        """
        action = f"edit the cell at row {row!r}, column {column!r}"
        if isinstance(grid, bool) or not isinstance(grid, int) or grid < 1:
            raise ValueError(f"Expected grid to be a positive whole number, got {grid!r}")
        if value is not None and not _is_cell_value(value):
            raise self._refuse(action, "the value must be at most 80 printable characters")
        if value is not None and _begins_with_denied(value, self._deny):
            raise self._refuse(action, "the value starts like a finalizing action")

        def vet_option(name: str) -> None:
            """Refuse a dropdown option whose text begins with a finalizing word."""
            if _begins_with_denied(name, self._deny):
                raise self._refuse(action, "the option starts like a finalizing action")

        return self._raw.edit_cell(
            row,
            column,
            value,
            grid,
            lambda info: self._vet_control(action, info),
            vet_option,
        )

    def fill(self, label: str, value: str, target: FieldTarget | None = None) -> str | None:
        """Fill a field; returns the value it holds afterwards (None for a password field)."""
        self._check_target(target)
        return self._raw.fill(label, value, target)

    def read_field(self, label: str, target: FieldTarget | None = None) -> FieldReading:
        """Read a field's value for a comparison; the same targeting rules as `fill`."""
        self._check_target(target)
        return self._raw.read_field(label, target)

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

    def mark_window(
        self, task: str, hosts: Sequence[str] = (), blocked: Sequence[str] = ()
    ) -> None:
        """Show the task's indicator on the page blumkin is attached to (on `hosts` only)."""
        self._raw.mark_window(task, hosts, blocked)

    def pick(
        self, text: str, role: str = "row", nth: int | None = None, prefix: bool = False
    ) -> None:
        """Choose the row or cell whose text is exactly `text`, such as a search result.

        With `prefix` its text only has to start with `text` (a list row that goes on with a
        date or an author); everything the element's own names say is then checked as well, so
        a finalizing word after the prefix refuses it too.

        A result of a search (a list-of-values dialog) is page data, so it cannot be declared
        in advance, and that is why `pick` is for rows and cells only: an option or a tree item
        is something `click` makes the task declare. It is refused when its text is a
        finalizing action and when the element is, holds or sits inside a control (a button or
        link given a row role, a row with a button of its own, a row inside a link), and the
        element is vetted like a click. `nth` (1-based, DOM order) narrows a repeated text. The
        dialog's own `OK` is still an ordinary declared click.
        """
        action = f"pick {text!r}"
        if role not in PICK_ROLES:
            raise self._refuse(action, f"the role {role!r} is not a row or a cell")
        if nth is not None and (isinstance(nth, bool) or not isinstance(nth, int) or nth < 1):
            raise ValueError(f"Expected nth to be a positive whole number, got {nth!r}")
        if _is_denied(text, self._deny):
            raise self._refuse(action, "its text is a finalizing action")

        def vet(info: ControlInfo) -> None:
            if prefix and not info.own_names_complete:
                raise self._refuse(action, "the element has more names than can be checked")
            if prefix and any(_is_denied(held, self._deny) for held in info.own_names):
                raise self._refuse(action, "its full text holds a finalizing action")
            if (
                info.holds_controls
                or info.tag in _PICK_CONTROL_TAGS
                or info.href
                or _is_button_element(info)
            ):
                raise self._refuse(
                    action,
                    "the element is, holds or sits inside a control; a control is clicked by "
                    "its own declared name",
                )
            self._vet_control(action, info, None, role)

        self._raw.checked_pick(text, role, vet, nth=nth, prefix=prefix)

    def pick_account(
        self,
        email: str,
        hosts: Sequence[str],
        blocked: Sequence[str],
        vet_names: Callable[[Sequence[str]], bool] | None = None,
    ) -> AccountPick:
        """Select the one account tile for ``email`` on an identity provider's picker.

        Not a control click: it works only on an identity-provider host that is not one of
        the task's own, never types, and reports instead of guessing; see the driver.
        """

        def clean(names: Sequence[str]) -> bool:
            lines = [line for name in names for line in (name, *name.splitlines())]
            return not any(_is_denied(line, self._deny) for line in lines)

        return self._raw.pick_account(email, hosts, blocked, clean)

    def press(self, label: str, key: str, target: FieldTarget | None = None) -> None:
        action = f"press {key!r} in {label!r}"
        if not _key_is_safe(key):
            raise self._refuse(action, "only navigation keys and single printable characters")
        self._check_target(target)

        def vet(info: ControlInfo) -> None:
            if _is_button_like(info):
                raise self._refuse(action, "the field is a button, not a text field")

        self._raw.press_field(label, key, target, vet)

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

    def settle_page(
        self, *, expect_heading: str | None = None, expect_wait_s: float | None = None
    ) -> PageSettled | None:
        """Wait for the page the last action started to settle and say which headings it shows.

        Reads and waits only; it clicks nothing. `expect_wait_s` shortens how long an expected
        heading is waited for (0 looks once). None when the driver cannot do it.
        """
        settle = getattr(self._raw, "settle_page", None)
        if settle is None:
            return None
        if expect_wait_s is None:
            return settle(expect_heading=expect_heading)  # type: ignore[no-any-return]
        return settle(  # type: ignore[no-any-return]
            expect_heading=expect_heading, expect_wait_s=expect_wait_s
        )

    def navigations(self) -> tuple[str, ...]:
        """The addresses the page moved through since `track_navigations` (none if unsupported)."""
        found = getattr(self._raw, "navigations", None)
        return found() if found is not None else ()

    def title(self) -> str:
        return self._raw.title()

    def track_navigations(self) -> None:
        """Start recording the redirect chain of the top-level page; it reads nothing else."""
        start = getattr(self._raw, "track_navigations", None)
        if start is not None:
            start()

    def select(
        self,
        label: str,
        option: str,
        target: FieldTarget | None = None,
        private: bool = False,
    ) -> str | None:
        """Choose an option in a dropdown; an option or field that finalizes a form is refused.

        With `private` the option (data a flow parameter supplied) is named in no refusal.
        """
        action = f"select an option in {label!r}" if private else f"select {option!r} in {label!r}"
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

    def inspect_field(self, label: str, target: FieldTarget | None = None) -> FieldInspection:
        """Facts about one element, never its value; read-only, so no control is vetted.

        The narrowing is checked like any other, except that the role may also be one of a
        button, link, cell or dialog (`INSPECT_ROLES`): nothing here clicks or types.
        """
        if target is not None and target.role is not None and target.role not in INSPECT_ROLES:
            raise ValueError(
                f"Expected a role of {', '.join(sorted(INSPECT_ROLES))}, got {target.role!r}"
            )
        self._check_target(None if target is None else replace(target, role=None))
        return self._raw.inspect_field(label, target)

    def list_requests(self) -> tuple[RequestEntry, ...]:
        """The resources the page loaded since its last navigation (timing only, never bodies)."""
        return self._raw.list_requests()

    def list_options(self, label: str, target: FieldTarget | None = None) -> OptionsListing:
        """List a dropdown's options without choosing any; the field is vetted like a select."""
        action = f"list the options of {label!r}"
        if _is_denied(label, self._deny):
            raise self._refuse(action, "its name is a finalizing action")
        if target is not None and target.role is not None and target.role not in _SELECT_ROLES:
            raise self._refuse(action, f"the role {target.role!r} is not one a dropdown has")
        self._check_target(target)
        return self._raw.list_options(
            label,
            target,
            lambda info: self._vet_control(action, info),
            lambda name: self._vet_field_name(action, name),
        )

    def upload(
        self, label: str, path: Path, target: FieldTarget | None = None
    ) -> UploadObservation:
        self._check_target(target)
        return self._raw.upload(label, path, target)

    def sign_in_pass(
        self,
        is_sign_in_host: Callable[[str], bool],
        is_task_host: Callable[[str], bool] | None = None,
    ) -> AbstractContextManager[None]:
        """Let identity-provider requests past the safety net while the user signs in by hand."""
        return self._raw.sign_in_pass(is_sign_in_host, is_task_host)

    def wait_ms(self, milliseconds: int) -> None:
        """Pause, with the page's events delivered (for example a redirect finishing)."""
        self._raw.wait_ms(milliseconds)

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
        self,
        name: str,
        role: str | None,
        wait_s: float,
        nth: int | None = None,
        *,
        confirm_ok: bool = True,
        declared_only: bool = False,
    ) -> tuple[str, str]:
        """Check the policy, then find `name` on the page: (the role it is in, the action)."""
        action = f"click {name!r}"
        self.check_click(name, role, confirm_ok=confirm_ok, declared_only=declared_only)
        roles = (role,) if role is not None else tuple(sorted(CLICKABLE_ROLES))
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
        resolved = next(iter(found))
        if role is None:
            self.check_click(name, resolved, confirm_ok=confirm_ok, declared_only=declared_only)
        return resolved, action

    def _declared_hint(self) -> str:
        """The task's declared control names and patterns, for a refusal that lists them."""
        if not self._declared and not self._confirm_names:
            if self._allow_all:
                return (
                    " (the task declares no controls; with allow_all_controls a button, a menu"
                    " item or a destructive name such as Delete still needs declaring)"
                )
            return " (the task declares no controls)"
        shown = ", ".join(repr(name) for name in self._declared[:_HINT_NAMES])
        more = (
            f" and {len(self._declared) - _HINT_NAMES} more"
            if len(self._declared) > _HINT_NAMES
            else ""
        )
        parts = []
        if self._declared:
            star = "; a `*` stands for any text" if self._patterns else ""
            parts.append(f"declared: {shown}{more}{star}")
        if self._confirm_names:
            parts.append(
                "dialog confirms: " + ", ".join(repr(name) for name in self._confirm_names)
            )
        return f" ({'; '.join(parts)})"

    def _is_declared(
        self, name: str, role: str | None = None, *, declared_only: bool = False
    ) -> bool:
        """Whether `name` may be clicked: declared exactly, by a declared pattern, or (in
        deny-list mode, unless `declared_only`) a link, tab, tree item, option or checkbox that
        is not a destructive name.

        A menu item is never reached by a pattern: each one stays individually declared, so
        `Dupl*` cannot authorize `Duplicate`. A button is never reached by deny-list mode. The
        finalizing-name and element checks that run beside this one still refuse a click that
        could finalize.
        """
        folded = _exact(name)
        if folded in self._allowed:
            return True
        if role in _MENU_ROLES:
            return False
        if len(folded) <= _MAX_PATTERN_NAME and any(
            rule.fullmatch(folded) for rule in self._patterns
        ):
            return True
        if not self._allow_all or declared_only:
            return False
        # With no role yet, the role the control is found in decides, right after the lookup.
        if role is not None and role not in _DENY_LIST_ROLES:
            return False
        return not _is_denied(name, self._destructive)

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

    def _vet_draft_control(self, action: str, info: ControlInfo) -> None:
        """A declared draft control must really be a button, and none of its own names (text,
        value, title, id, labels) may be a finalizing action: the page may have renamed the
        report's real Submit to the declared name."""
        # Only the two types a recalculation button has: a reset or an image input is not one.
        plain = info.tag in ("button", "input") and info.input_type in ("button", "submit")
        if not plain:
            raise self._refuse(action, "a draft control must be a button")
        if not info.own_names_complete:
            raise self._refuse(action, "the control has more names than can be checked")
        held = [text for text in info.own_names if _is_denied(text, self._deny)]
        if held:
            raise self._refuse(action, "a name this control also goes by is a finalizing action")

    def _vet_control(
        self, action: str, info: ControlInfo, name: str | None = None, role: str | None = None
    ) -> None:
        """Raise if the element about to be clicked could finalize a form.

        A name declared as a dialog confirm (and not as a plain control) is valid only inside a
        dialog; there it is spared the submit-control check when its form is the dialog's own.

        In deny-list mode a button element must be declared whatever role the page gives it
        (`<button role="link">` is still a button): the role only decides what the name check
        let through, not what the element is.
        """
        if (
            name is not None
            and self._allow_all
            and _is_button_element(info)
            and _exact(name) not in self._confirm
            and _exact(name) not in self._draft
            and not self._is_declared(name, role, declared_only=True)
        ):
            raise self._refuse(
                action,
                "the element is a button, and a button must be declared in allowed_controls "
                "whatever role the page gives it",
            )
        confirm = False
        if name is not None and _exact(name) in self._confirm:
            confirm = True
            if not self._is_declared(name, role, declared_only=True):
                # Declared only as a dialog confirm: valid in a dialog whose form is its own,
                # whatever type of button it is.
                if not info.in_dialog:
                    raise self._refuse(
                        action, "it is declared as a dialog confirm and is not in a dialog"
                    )
                # A popup found by its landmarks sits in the page's own form, so a button that
                # cannot submit anything (not a submit control) is judged by what the popup holds.
                in_form = (
                    self._confirm_in_form and info.form_method == "post" and not info.dialog_is_form
                )
                if not info.form_in_dialog and not (
                    info.dialog_landmark and (in_form or not _is_submit_control(info))
                ):
                    raise self._refuse(
                        action,
                        "it is declared as a dialog confirm and its form is not the dialog's",
                    )
            if info.in_dialog and not info.dialog_controls_complete:
                raise self._refuse(action, "the dialog holds more controls than can be checked")
            held = [text for text in info.dialog_controls if _is_denied(text, self._deny)]
            if info.in_dialog and held:
                # A dialog that also holds the report's own Submit is the whole report in a
                # modal wrapper, not a small dialog with rows of its own.
                raise self._refuse(action, "the dialog also holds a finalizing control")
        draft = name is not None and _exact(name) in self._draft
        if draft:
            self._vet_draft_control(action, info)
        reason = vet_control(
            info,
            dialog_confirm=confirm,
            draft_control=draft,
            in_form_ok=(
                self._confirm_in_form
                and info.dialog_landmark
                and info.form_method == "post"
                and not info.dialog_is_form
            ),
        )
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


#: Most declared names listed in a "not declared" refusal.
_HINT_NAMES = 10
#: Longest name a pattern is tried against (page-supplied names can be arbitrarily long).
_MAX_PATTERN_NAME = 300
#: The roles deny-list mode (`allow_all_controls`) opens up: navigation and selection controls.
#: A button is never one of them, so Save, Calculate and every other button stays declared by
#: name: a handler a script installed on a button can write in the background unseen.
_DENY_LIST_ROLES = frozenset({"checkbox", "link", "option", "tab", "treeitem"})
#: Menu items are never reached by a pattern: each stays individually declared.
_MENU_ROLES = frozenset({"menuitem", "menuitemcheckbox", "menuitemradio"})
#: Stands in for an ASCII `*` while a pattern is normalized, so a fullwidth `＊` (which NFKC turns
#: into `*`) stays literal text.
_WILDCARD = "\ue000"
_BUTTON_LIKE_INPUT_TYPES = frozenset({"button", "image", "reset", "submit"})
#: Roles a dropdown has; `select` never goes through a button, link or other clickable role.
_SELECT_ROLES = frozenset({"combobox", "listbox", "searchbox", "textbox"})

CLICKABLE_ROLES = frozenset(
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

#: What `pick` may choose: data in a result list, never a control. An option or a tree item
#: is something `click` makes the task declare, so neither is here.
PICK_ROLES = frozenset({"gridcell", "row"})
_PICK_CONTROL_TAGS = frozenset({"a", "button", "input", "select", "summary", "textarea"})
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


def is_declared_control(name: str, declared: Iterable[str], role: str | None = None) -> bool:
    """Whether `name` is one the task declared, by exact name or by a `*` pattern.

    The same rule as `browser click`: a menu item is never reached by a pattern (each stays
    individually declared) and a pattern only matches a name of up to `_MAX_PATTERN_NAME`
    characters. A finalizing name is never declared (see `validate_control_patterns`), and
    callers still check `is_finalizing_name` themselves.
    """
    wanted = _exact(name)
    names = tuple(declared)
    if any("*" not in item and _exact(item) == wanted for item in names):
        return True
    if role in _MENU_ROLES or len(wanted) > _MAX_PATTERN_NAME:
        return False
    return any("*" in item and _pattern_regex(item).fullmatch(wanted) for item in names)


def is_finalizing_name(name: str, extra_deny: Sequence[str] = ()) -> bool:
    """Whether a control name starts a finalizing word (submit, send, pay, ...)."""
    return _is_denied(name, _deny_stems((*DEFAULT_DENY_NAMES, *extra_deny)))


def validate_control_patterns(names: Iterable[str], extra_deny_names: Iterable[str] = ()) -> None:
    """Refuse a declared control pattern that is too loose or could name a finalizing action.

    A pattern is a declared name with `*` standing for any text (`Actions for *`). It must start
    with fixed text, hold at most `MAX_PATTERN_STARS` stars and keep at least
    `MIN_PATTERN_LITERAL` letters or digits of fixed text, and that fixed text must not itself
    be a finalizing word: `Submit *` is refused. Only an ASCII `*` is a wildcard. The name
    actually clicked is still checked against the finalizing words, so a pattern can never
    reach one.
    """
    deny = _deny_stems((*DEFAULT_DENY_NAMES, *extra_deny_names))
    for name in names:
        if "*" not in name:
            continue
        if _WILDCARD in name:
            raise SubmitBlockedError(
                f"Expected a control pattern without private-use characters, got {name!r}. "
                "A browser task never submits"
            )
        folded = _wild(name)
        if folded.startswith(_WILDCARD):
            raise SubmitBlockedError(
                f"Expected a control pattern that starts with fixed text, got {name!r}: the "
                "page would choose how the name begins. A browser task never submits"
            )
        if len(re.findall(f"{_WILDCARD}+", folded)) > MAX_PATTERN_STARS:
            raise SubmitBlockedError(
                f"Expected a control pattern with at most {MAX_PATTERN_STARS} `*`, got {name!r}. "
                "A browser task never submits"
            )
        literal = folded.replace(_WILDCARD, " ")
        solid = sum(char.isalnum() for char in literal)
        if solid < MIN_PATTERN_LITERAL:
            raise SubmitBlockedError(
                f"Expected a control pattern with at least {MIN_PATTERN_LITERAL} letters or "
                f"digits of fixed text, got {name!r}. A browser task never submits"
            )
        if _is_denied(literal, deny):
            raise SubmitBlockedError(
                f"Expected a control pattern that is not a finalizing action, got {name!r}. "
                "A browser task never submits"
            )


def validate_dialog_confirm(names: Iterable[str], extra_deny_names: Iterable[str] = ()) -> None:
    """Refuse a `dialog_confirm` that names a finalizing action (`Submit`, `Pay`, ...)."""
    deny = _deny_stems((*DEFAULT_DENY_NAMES, *extra_deny_names))
    finalizing = sorted(name for name in names if _is_denied(name, deny))
    if finalizing:
        raise SubmitBlockedError(
            "Expected dialog_confirm names that are not finalizing actions, got "
            f"{', '.join(repr(name) for name in finalizing)}. A browser task never submits; "
            "a confirm button can only close a dialog"
        )


def landmark_groups(value: Iterable[Any]) -> tuple[tuple[str, ...], ...]:
    """`dialog_landmarks` as groups of names, one group per popup.

    A flat list of names is one group, so a task written before there were several keeps
    working; a list of lists is one group each.
    """
    items = tuple(value)
    if not items:
        return ()
    if all(isinstance(item, str) for item in items):
        return (tuple(items),)
    if all(isinstance(group, (list, tuple)) and group for group in items) and all(
        isinstance(name, str) for group in items for name in group
    ):
        return tuple(tuple(group) for group in items)
    raise SubmitBlockedError(
        "Expected dialog_landmarks as a list of names or a list of non-empty lists of names, "
        f"got {items!r}"
    )


def validate_dialog_landmarks(names: Iterable[str], extra_deny_names: Iterable[str] = ()) -> None:
    """Refuse landmarks that are too few to mark a popup, a pattern, or a finalizing name.

    Names are compared without case, as the page is searched: `Add` and `add` are one landmark.
    """
    listed = [name for name in names]
    if not listed:
        return
    bad = [name for name in listed if not name.strip() or "*" in name]
    deny = _deny_stems((*DEFAULT_DENY_NAMES, *extra_deny_names))
    bad += [name for name in listed if name.strip() and _is_denied(name, deny)]
    if bad or len({_exact(name).casefold() for name in listed}) < 2:
        raise SubmitBlockedError(
            "Expected dialog_landmarks of at least two distinct exact names that are not "
            f"finalizing actions, got {', '.join(repr(name) for name in listed)}"
        )


def validate_draft_controls(names: Iterable[str], extra_deny_names: Iterable[str] = ()) -> None:
    """Refuse a `draft_controls` name that is finalizing (`Submit`, `Pay`, ...) or a pattern."""
    deny = _deny_stems((*DEFAULT_DENY_NAMES, *extra_deny_names))
    bad = sorted(
        name for name in names if "*" in name or not _exact(name) or _is_denied(name, deny)
    )
    if bad:
        raise SubmitBlockedError(
            "Expected draft_controls names that are exact and not finalizing actions, got "
            f"{', '.join(repr(name) for name in bad)}. A browser task never submits; a draft "
            "control may only recalculate what is on the page"
        )


def vet_control(
    info: ControlInfo,
    *,
    dialog_confirm: bool = False,
    draft_control: bool = False,
    in_form_ok: bool = False,
) -> str | None:
    """Why clicking this element could finalize a form, or None when nothing says it could.

    `dialog_confirm` spares a submit-type button the submit check, but only inside a dialog
    whose form, if any, lies inside that dialog: it then submits the dialog's own rows.
    `draft_control` spares it for a button the user declared a draft calculation, anywhere on
    the page. `in_form_ok` (the task opted in, the popup was found by its landmarks and its
    form posts, so the safety net swallows a submit) also spares a dialog confirm whose popup
    sits in the page's own form; a form that submits by GET is never spared. Every other check (a
    formaction override, a script link, a submitting handler) still applies to all of them.
    """
    confirmed = dialog_confirm and info.in_dialog and (info.form_in_dialog or in_form_ok)
    if _is_submit_control(info) and not (confirmed or draft_control):
        return "it is a submit control"
    if info.has_formaction:
        return "it carries a formaction override"
    if _runs_script(info.href):
        return "it is a link that runs script"
    if "submit" in info.inline_handler.casefold():
        return "its click handler submits a form"
    return None


def _begins_with_denied(text: str, stems: tuple[str, ...]) -> bool:
    """Whether `text` opens with a finalizing word (`Submit`, `Pay now`), not merely holds one.

    A data value typed into a cell, or a dropdown option, is not a control: `Regular Pay` is a
    time reporting code, while `Submit` or `Pay now` read as the action itself.
    """
    words = re.findall(r"[^\W_]+", _folded(text))
    if not words:
        return False
    spaced = " ".join(words)
    return any(
        spaced.startswith(stem) if " " in stem else words[0].startswith(stem) for stem in stems
    )


def _deny_stems(words: tuple[str, ...]) -> tuple[str, ...]:
    """Reduce each deny word to the form names are matched in: lower-case words joined by
    single spaces with punctuation removed, so `e-mail` matches the name `E-mail it`."""
    stems = (" ".join(re.findall(r"[^\W_]+", _folded(word))) for word in words)
    return tuple(stem for stem in stems if stem)


def _exact(text: str) -> str:
    """Normalize for the allow list: NFKC, no zero-width characters, collapsed spaces; the
    case is kept because the browser matches an accessible name exactly."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).split())


def _wild(pattern: str) -> str:
    """`_exact` for a declared pattern: each ASCII `*` becomes `_WILDCARD`, all else is text."""
    return _exact(pattern.replace("*", _WILDCARD))


def _folded(text: str) -> str:
    return _exact(text).casefold()


def _is_button_like(info: ControlInfo) -> bool:
    if info.tag == "button" or info.tag == "a":
        return True
    return info.tag == "input" and info.input_type in _BUTTON_LIKE_INPUT_TYPES


def _is_button_element(info: ControlInfo) -> bool:
    """A `<button>` or a button-type `<input>`; unlike `_is_button_like`, never a link."""
    if info.tag == "button":
        return True
    return info.tag == "input" and info.input_type in _BUTTON_LIKE_INPUT_TYPES


def _is_cell_value(value: str) -> bool:
    """A value fit to type into a grid cell: one short line of printable characters."""
    return len(value) <= MAX_CELL_VALUE_CHARS and all(ch.isprintable() for ch in value)


#: A word that follows `file` when it names a column or a field (`File Name or URL`), not the
#: action of filing a report.
_FILE_NOUNS = frozenset({"name", "names", "size", "type"})


def _is_denied(name: str, stems: tuple[str, ...]) -> bool:
    folded = _folded(name)
    words = re.findall(r"[^\W_]+", folded)
    # `File Name or URL` is a column header, not the filing action: only the built-in `file`
    # stem looks past `file` followed by a noun, so a task's own `File Type` deny name still
    # matches it.
    plain = [
        word
        for i, word in enumerate(words)
        if not (word == "file" and i + 1 < len(words) and words[i + 1] in _FILE_NOUNS)
    ]
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
        elif any(word.startswith(stem) for word in (plain if stem == "file" else words)):
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


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """A declared pattern as a regex: `*` is any text, everything else is exact."""
    collapsed = re.sub(f"{_WILDCARD}+", _WILDCARD, _wild(pattern))
    parts = (re.escape(part) for part in collapsed.split(_WILDCARD))
    return re.compile(".*".join(parts), re.DOTALL)


def _runs_script(href: str) -> bool:
    cleaned = "".join(href.split()).casefold()
    return cleaned.startswith(_SCRIPT_SCHEMES)
