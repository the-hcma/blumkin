"""Tab-completion values read from the operator's own files (issue #538).

Each completer runs on every Tab press, so it reads local files only: no network, no prompt, no
output. Any failure (a missing or broken config, an unreadable store) completes nothing instead of
breaking the shell.
"""

from __future__ import annotations

import contextlib
import io

import click
from click.shell_completion import CompletionItem

from blumkin.config import config_dir, load_config, load_loop_setup
from blumkin.loop.agent_task import pending_items
from blumkin.tasks import load_tasks


def loop_trigger_names(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Configured loop and cron trigger names."""
    try:
        names = sorted(load_loop_setup().triggers)
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    return [CompletionItem(name) for name in names if name.startswith(incomplete)]


def task_names(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
    """Task template names from the config and active profile `tasks/` directories."""
    try:
        # Loading warns (on stderr) about malformed templates; Tab must stay silent.
        profile = ctx.find_root().params.get("profile")
        with contextlib.redirect_stderr(io.StringIO()):
            tasks = load_tasks(load_config(profile=profile if isinstance(profile, str) else None))
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    return [
        CompletionItem(task.name, help=task.title)
        for task in tasks
        if task.name.startswith(incomplete)
    ]


#: The types people give `drive upload --mime-type`; anything type/subtype is still accepted.
_COMMON_MIME_TYPES = (
    "application/json",
    "application/msword",
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    "image/webp",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "video/mp4",
)


def mime_types(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
    """Common MIME types for `--mime-type` (static: nothing is read or fetched)."""
    return [CompletionItem(kind) for kind in _COMMON_MIME_TYPES if kind.startswith(incomplete)]


def waiting_item_ids(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Ids of agent items waiting or handed back (`loop finish` can close either)."""
    try:
        items = pending_items(config_dir(), handed_back=True)
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    return [
        CompletionItem(item.id, help=f"template {item.template}")
        for item in items
        if item.id.startswith(incomplete)
    ]


# --- browser: task names, ledger instances, model pages, roles (the browser commands) ---------

#: Roles an element can be searched by in `--mask ROLE[:NAME]`: where a name or an amount shows.
_MASK_ROLES = (
    "cell",
    "columnheader",
    "gridcell",
    "heading",
    "img",
    "link",
    "listitem",
    "row",
    "rowheader",
    "textbox",
)

#: Roles an element can be searched by in `--within ROLE[:NAME]`: the containers people read.
_WITHIN_ROLES = (
    "alertdialog",
    "dialog",
    "form",
    "grid",
    "list",
    "listbox",
    "menu",
    "region",
    "table",
    "tablist",
    "tabpanel",
    "toolbar",
    "tree",
)
#: Where a `--folder` of `mail list` may point without asking the mailbox.
_MAIL_FOLDERS = ("inbox", "sentitems", "drafts", "deleteditems", "junkemail", "archive", "outbox")


def browser_roles(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """The roles `--role` takes for this browser command (read from the code, never the page)."""
    from blumkin.browser.cdp import FIELD_ROLES, INSPECT_ROLES
    from blumkin.browser.guard import CLICKABLE_ROLES, PICK_ROLES

    command = ctx.command.name or ""
    explore = ctx.parent is not None and ctx.parent.info_name == "explore"
    roles = (
        PICK_ROLES
        if command == "pick"
        else INSPECT_ROLES
        if command == "inspect"
        else CLICKABLE_ROLES
        if command in {"click", "download"} or explore
        else FIELD_ROLES
    )
    return [CompletionItem(role) for role in sorted(roles) if role.startswith(incomplete)]


def browser_task_names(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Browser task names: those with a `[browser.tasks.*]` policy and those with a profile."""
    from blumkin.browser.profiles import list_profiles as list_browser_profiles
    from blumkin.config import load_browser_setup

    try:
        setup = load_browser_setup()
        policies = set(setup.browser.tasks)
        profiles = {profile.task for profile in list_browser_profiles(setup.config_dir)}
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    items = []
    for name in sorted(policies | profiles):
        if not name.startswith(incomplete):
            continue
        both = name in policies and name in profiles
        help_text = (
            "profile and policy" if both else "policy only" if name in policies else "profile only"
        )
        items.append(CompletionItem(name, help=help_text))
    return items


def ledger_instances(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Ledger instances of the task already typed (`browser ledger status TASK <Tab>`)."""
    from blumkin.browser.ledger import list_instances

    task = ctx.params.get("task")
    if not isinstance(task, str):
        return []
    try:
        found = list_instances(config_dir(), task)
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    return [
        CompletionItem(name, help=str(phase))
        for name, phase in found
        if name.startswith(incomplete)
    ]


def mail_folders(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """The well-known mail folder names (static; a folder id or custom name is still accepted)."""
    return [CompletionItem(name) for name in _MAIL_FOLDERS if name.startswith(incomplete)]


def model_page_ids(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Page ids of the task's saved site model and exploration draft."""
    from blumkin.browser.model import load_draft, load_model, resolve_store
    from blumkin.config import load_browser_setup

    task = ctx.params.get("task")
    if not isinstance(task, str):
        return []
    try:
        store = resolve_store(load_browser_setup())
        ids: dict[str, str] = {}
        for label, model in (
            ("draft", load_draft(store, task)),
            ("model", load_model(store, task)),
        ):
            if model is not None:
                for page in model.pages:
                    ids.setdefault(page.page_id, label)
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    return [
        CompletionItem(page, help=label)
        for page, label in sorted(ids.items())
        if page.startswith(incomplete)
    ]


def mask_roles(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
    """The roles `--mask ROLE[:NAME]` takes: what a screenshot can cover (name: the page's)."""
    if ":" in incomplete:
        return []
    return [CompletionItem(role) for role in _MASK_ROLES if role.startswith(incomplete)]


def within_roles(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """The container roles `--within ROLE[:NAME]` takes (the name part is the page's own)."""
    if ":" in incomplete:
        return []
    return [CompletionItem(role) for role in _WITHIN_ROLES if role.startswith(incomplete)]


# --- accounts, time zones, addresses -------------------------------------------------------------


def contact_addresses(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Addresses from the operator's own `email-context.md` (a name never becomes one here)."""
    from blumkin.contacts import load_context

    try:
        profile = ctx.find_root().params.get("profile")
        with contextlib.redirect_stderr(io.StringIO()):
            contacts = load_context(
                load_config(profile=profile if isinstance(profile, str) else None)
            )
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    # `--to a@x.com,b` is a list: complete the last address and keep the ones before it.
    head, separator, last = incomplete.rpartition(",")
    wanted = last.strip().lower()
    return [
        CompletionItem(f"{head}{separator}{contact.email}", help=contact.name)
        for contact in contacts
        if contact.email and contact.email.lower().startswith(wanted)
    ]


def profile_names(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Account profile names, and their tags (`@work`, `google`, ...) where a tag is accepted.

    The global `--profile` and `mcp serve` / `mcp install` resolve a name or a tag; `loop init`
    compares what it is given with the configured names only, so a tag is not offered there.
    Only a value the selector resolver accepts for exactly one profile is offered, so completion
    never suggests a name or tag the command would refuse as ambiguous.
    """
    from blumkin.config import profile_selectors

    command = ctx.command_path.partition(" ")[2]  # `loop init`, without the program name
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            selectors = profile_selectors(tags=command not in _PROFILE_NAMES_ONLY)
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    items: list[CompletionItem] = []
    seen: set[str] = set()
    for value, name in selectors:
        if value in seen or not value.startswith(incomplete):
            continue
        seen.add(value)
        items.append(CompletionItem(value, help="profile" if value == name else f"tag of {name}"))
    return items


def time_zones(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
    """IANA time zone names from the system's zoneinfo (no network)."""
    import zoneinfo

    try:
        names = sorted(zoneinfo.available_timezones())
    except Exception:  # noqa: BLE001 - completion must never break the shell
        return []
    wanted = incomplete.lower()
    return [CompletionItem(name) for name in names if name.lower().startswith(wanted)]


# --- wiring --------------------------------------------------------------------------------------

#: Commands whose `--profile` is compared with the configured profile names, never with tags.
_PROFILE_NAMES_ONLY = frozenset({"loop init"})
_RECIPIENT_COMMANDS = (("mail", "draft"), ("mail", "forward"), ("mail", "reply"))

#: Where a completer is attached: (kind, parameter name) -> (completer, command-path prefixes).
#: An empty prefix list means any command. A parameter that is a `click.Choice` completes itself.
_WIRING: tuple[tuple[str, str, object, tuple[tuple[str, ...], ...]], ...] = (
    ("argument", "task", browser_task_names, (("browser",),)),
    ("argument", "instance", ledger_instances, (("browser",),)),
    ("argument", "page_id", model_page_ids, (("browser",),)),
    ("option", "--role", browser_roles, (("browser",),)),
    ("option", "--within", within_roles, (("browser",),)),
    ("option", "--mask", mask_roles, (("browser",),)),
    ("option", "--profile", profile_names, ()),
    ("option", "--tz", time_zones, ()),
    ("option", "--folder", mail_folders, (("mail", "list"),)),
    # Recipients only: `mail move --to` is a destination folder, not an address.
    ("option", "--to", contact_addresses, _RECIPIENT_COMMANDS),
    ("option", "--cc", contact_addresses, _RECIPIENT_COMMANDS),
    ("option", "--bcc", contact_addresses, _RECIPIENT_COMMANDS),
    ("option", "--with", contact_addresses, (("calendar",),)),
)


def wire(root: click.Group) -> None:
    """Attach the value completers to every matching parameter under `root`.

    Done in one place, by name and command path, so a new browser command with a `TASK` or a
    `--tz` completes without anyone remembering to ask. A parameter that already has a completer
    or is a `click.Choice` is left alone.
    """

    def walk(command: click.Command, path: tuple[str, ...]) -> None:
        if isinstance(command, click.Group):
            for name, child in command.commands.items():
                walk(child, (*path, name))
        for param in command.params:
            _attach(param, path)

    walk(root, ())
    # The global options sit on the root group itself.
    for param in root.params:
        _attach(param, ())


def _attach(param: click.Parameter, path: tuple[str, ...]) -> None:
    if isinstance(param.type, click.Choice) or getattr(param, "_custom_shell_complete", None):
        return
    kind = "argument" if isinstance(param, click.Argument) else "option"
    for want_kind, name, completer, prefixes in _WIRING:
        if kind != want_kind:
            continue
        matches = param.name == name if kind == "argument" else name in param.opts
        if not matches:
            continue
        if prefixes and not any(path[: len(prefix)] == prefix for prefix in prefixes):
            continue
        # Click's public hook is the `shell_complete=` constructor argument; the commands are
        # built by decorators, so the same attribute is set here once they all exist.
        param._custom_shell_complete = completer  # type: ignore[attr-defined]
        return
