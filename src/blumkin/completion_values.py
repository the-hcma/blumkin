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
