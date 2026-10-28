"""Parse `[loop]` and `[cron.*]` from config.toml (issue #501)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import is_valid_task_name
from blumkin.loop.agent_task import fold_keyword
from blumkin.loop.types import AGENT_TASK, InstanceResolver, TriggerKind, TriggerSpec

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 30.0
DEFAULT_RUN_BUDGET_SECONDS = 600.0
_NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")
_TRIGGER_KEYS_CRON = frozenset(
    {"default_hours", "instance", "schedule", "task", "timezone", "working_days_only"}
)
_TRIGGER_KEYS_LOOP = frozenset(
    {
        "allowlist",
        "attachment_types",
        "instance",
        "instance_resolver",
        "kind",
        "profiles",
        "subject_tag",
        "task",
        "templates",
    }
)


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Top-level `[loop]` runner settings."""

    kill_switch: bool = False
    kill_switch_file: Path | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
    # Optional fallback recipient when a trigger has no allowlist (cron / manual).
    notice_to: str | None = None
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    run_budget_seconds: float = DEFAULT_RUN_BUDGET_SECONDS


@dataclass(frozen=True, slots=True)
class LoopSetup:
    """What the `blumkin loop` commands need: config dir, runner settings and triggers."""

    config_dir: Path
    loop: LoopConfig = field(default_factory=LoopConfig)
    triggers: dict[str, TriggerSpec] = field(default_factory=dict)

    def trigger(self, name: str) -> TriggerSpec:
        try:
            return self.triggers[name]
        except KeyError as exc:
            raise LoopTriggerNotFoundError(
                f"Expected a configured trigger named {name!r}, got none"
            ) from exc


class LoopConfigError(ValueError):
    """A `[loop]` / `[cron]` table is not acceptable."""


class LoopTriggerNotFoundError(LookupError):
    """No trigger with that name is configured."""


def parse_loop_setup(config_dir: Path, file_data: dict[str, Any]) -> LoopSetup:
    """Build a `LoopSetup` from a parsed config.toml mapping."""
    loop = _loop_config(file_data.get("loop"), config_dir)
    triggers: dict[str, TriggerSpec] = {}
    raw_loop = file_data.get("loop")
    if isinstance(raw_loop, dict):
        raw_triggers = raw_loop.get("triggers")
        if raw_triggers is not None:
            if not isinstance(raw_triggers, dict):
                raise LoopConfigError(
                    f"loop.triggers must be a table in config.toml, got {raw_triggers!r}"
                )
            for name, table in raw_triggers.items():
                spec = _loop_trigger(str(name), table)
                if spec.name in triggers:
                    raise LoopConfigError(
                        f"Expected unique trigger names, got {spec.name!r} more than once"
                    )
                triggers[spec.name] = spec
    raw_cron = file_data.get("cron")
    if raw_cron is not None:
        if not isinstance(raw_cron, dict):
            raise LoopConfigError(f"cron must be a table in config.toml, got {raw_cron!r}")
        for name, table in raw_cron.items():
            spec = _cron_trigger(str(name), table)
            if spec.name in triggers:
                raise LoopConfigError(
                    f"Expected unique trigger names across loop.triggers and cron, "
                    f"got {spec.name!r} more than once"
                )
            triggers[spec.name] = spec
    return LoopSetup(config_dir=config_dir, loop=loop, triggers=triggers)


def _check_name(kind: str, name: str) -> str:
    if not _NAME.fullmatch(name):
        raise LoopConfigError(
            f"Expected a {kind} name of 1-64 lowercase letters, digits, '-' or '_' "
            f"starting and ending with a letter or digit, got {name!r}"
        )
    return name


def _coerce_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise LoopConfigError(f"{label} must be a boolean in config.toml, got {value!r}")


def _coerce_float(value: Any, label: str, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LoopConfigError(f"{label} must be a number in config.toml, got {value!r}")
    number = float(value)
    if number < minimum:
        raise LoopConfigError(f"{label} must be >= {minimum} in config.toml, got {number}")
    return number


def _coerce_int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoopConfigError(f"{label} must be an integer in config.toml, got {value!r}")
    if value < minimum:
        raise LoopConfigError(f"{label} must be >= {minimum} in config.toml, got {value}")
    return value


def _cron_trigger(name: str, table: Any) -> TriggerSpec:
    name = _check_name("cron trigger", name)
    if not isinstance(table, dict):
        raise LoopConfigError(f"cron.{name} must be a table in config.toml, got {table!r}")
    unknown = sorted(set(table) - _TRIGGER_KEYS_CRON)
    if unknown:
        raise LoopConfigError(
            f"cron.{name} has unknown key(s) {', '.join(unknown)} in config.toml; "
            f"expected {', '.join(sorted(_TRIGGER_KEYS_CRON))}"
        )
    task = _require_string(table.get("task"), f"cron.{name}.task")
    if not is_valid_task_name(task):
        raise LoopConfigError(
            f"cron.{name}.task must be a valid task name in config.toml, got {task!r}"
        )
    instance = _require_string(table.get("instance"), f"cron.{name}.instance")
    if not is_valid_task_name(instance):
        raise LoopConfigError(
            f"cron.{name}.instance must be a valid instance name in config.toml, got {instance!r}"
        )
    schedule = _require_string(table.get("schedule"), f"cron.{name}.schedule")
    timezone = _require_string(table.get("timezone"), f"cron.{name}.timezone")
    working = table.get("working_days_only", False)
    hours_raw = table.get("default_hours")
    default_hours: float | None = None
    if hours_raw is not None:
        default_hours = _coerce_float(hours_raw, f"cron.{name}.default_hours", minimum=0.0)
        if not math.isfinite(default_hours):
            raise LoopConfigError(
                f"cron.{name}.default_hours must be a finite number in config.toml, "
                f"got {default_hours}"
            )
        if default_hours > 24:
            raise LoopConfigError(
                f"cron.{name}.default_hours must be <= 24 in config.toml, got {default_hours}"
            )
    spec = TriggerSpec(
        default_hours=default_hours,
        instance=instance,
        kind=TriggerKind.CRON,
        name=name,
        schedule=schedule,
        task=task,
        timezone=timezone,
        working_days_only=_coerce_bool(working, f"cron.{name}.working_days_only"),
    )
    from blumkin.loop.cron import CronError, validate_cron_trigger

    try:
        validate_cron_trigger(spec)
    except CronError as exc:
        raise LoopConfigError(str(exc)) from exc
    return spec


def _instance_resolver(value: Any, label: str) -> InstanceResolver:
    if value is None:
        return InstanceResolver.STATIC
    if not isinstance(value, str):
        raise LoopConfigError(
            f"{label}.instance_resolver must be a string in config.toml, got {value!r}"
        )
    try:
        return InstanceResolver(value.strip().lower())
    except ValueError as exc:
        raise LoopConfigError(
            f"{label}.instance_resolver must be one of "
            f"{', '.join(r.value for r in InstanceResolver)} in config.toml, got {value!r}"
        ) from exc


def _agent_templates(
    name: str, task: str, kind: TriggerKind, raw: Any
) -> tuple[tuple[str, str], ...]:
    """The subject-keyword to template table of an `agent` trigger (issues #506, #527)."""
    label = f"loop.triggers.{name}.templates"
    if task != AGENT_TASK:
        if raw is not None:
            raise LoopConfigError(
                f"{label} is only used with task = '{AGENT_TASK}' in config.toml; "
                f"remove it from a {task!r} trigger"
            )
        return ()
    if kind is not TriggerKind.LOOP:
        raise LoopConfigError(
            f"loop.triggers.{name}.kind must be 'loop' for task = '{AGENT_TASK}' in "
            f"config.toml, got {kind.value!r}"
        )
    if not isinstance(raw, dict) or not raw:
        raise LoopConfigError(
            f"{label} must be a non-empty table of subject keyword = template name "
            f"in config.toml, got {raw!r}"
        )
    pairs: dict[str, str] = {}
    for key, value in raw.items():
        keyword = fold_keyword(key) if isinstance(key, str) else None
        if keyword is None:
            raise LoopConfigError(
                f"{label} keyword must be one word of letters or digits in config.toml, got {key!r}"
            )
        if not isinstance(value, str) or not is_valid_task_name(value):
            raise LoopConfigError(
                f"{label}.{key} template must be a valid task name in config.toml, got {value!r}"
            )
        if keyword in pairs:
            raise LoopConfigError(f"{label} lists the keyword {keyword!r} twice in config.toml")
        pairs[keyword] = value
    return tuple(sorted(pairs.items()))


def _loop_config(raw: Any, config_dir: Path) -> LoopConfig:
    if raw is None:
        return LoopConfig()
    if not isinstance(raw, dict):
        raise LoopConfigError(f"[loop] must be a table in config.toml, got {raw!r}")
    unknown = sorted(
        set(raw)
        - {
            "kill_switch",
            "kill_switch_file",
            "max_retries",
            "notice_to",
            "retry_backoff_seconds",
            "run_budget_seconds",
            "triggers",
        }
    )
    if unknown:
        raise LoopConfigError(
            f"loop has unknown key(s) {', '.join(unknown)} in config.toml; "
            "expected kill_switch, kill_switch_file, max_retries, notice_to, "
            "retry_backoff_seconds, run_budget_seconds, triggers"
        )
    kill_file: Path | None = None
    raw_file = raw.get("kill_switch_file")
    if raw_file is not None:
        if not isinstance(raw_file, str) or not raw_file.strip():
            raise LoopConfigError(
                f"loop.kill_switch_file must be a non-empty string in config.toml, got {raw_file!r}"
            )
        text = raw_file.strip()
        kill_file = Path(text).expanduser() if text.startswith("~") else Path(text)
        if not kill_file.is_absolute():
            kill_file = config_dir / kill_file
    notice_to: str | None = None
    raw_notice = raw.get("notice_to")
    if raw_notice is not None:
        if not isinstance(raw_notice, str) or not raw_notice.strip():
            raise LoopConfigError(
                f"loop.notice_to must be a non-empty string in config.toml, got {raw_notice!r}"
            )
        notice_to = raw_notice.strip()
        from blumkin.loop.mail_trust import is_allowlist_email

        if not is_allowlist_email(notice_to):
            raise LoopConfigError(
                f"loop.notice_to must be an email address in config.toml, got {notice_to!r}"
            )
    return LoopConfig(
        kill_switch=_coerce_bool(raw.get("kill_switch", False), "loop.kill_switch"),
        kill_switch_file=kill_file,
        max_retries=_coerce_int(
            raw.get("max_retries", DEFAULT_MAX_RETRIES), "loop.max_retries", minimum=0
        ),
        notice_to=notice_to,
        retry_backoff_seconds=_coerce_float(
            raw.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS),
            "loop.retry_backoff_seconds",
            minimum=0.0,
        ),
        run_budget_seconds=_coerce_float(
            raw.get("run_budget_seconds", DEFAULT_RUN_BUDGET_SECONDS),
            "loop.run_budget_seconds",
            minimum=1.0,
        ),
    )


def _loop_trigger(name: str, table: Any) -> TriggerSpec:
    name = _check_name("loop trigger", name)
    if not isinstance(table, dict):
        raise LoopConfigError(f"loop.triggers.{name} must be a table in config.toml, got {table!r}")
    unknown = sorted(set(table) - _TRIGGER_KEYS_LOOP)
    if unknown:
        raise LoopConfigError(
            f"loop.triggers.{name} has unknown key(s) {', '.join(unknown)} in config.toml; "
            f"expected {', '.join(sorted(_TRIGGER_KEYS_LOOP))}"
        )
    task = _require_string(table.get("task"), f"loop.triggers.{name}.task")
    if not is_valid_task_name(task):
        raise LoopConfigError(
            f"loop.triggers.{name}.task must be a valid task name in config.toml, got {task!r}"
        )
    resolver = _instance_resolver(table.get("instance_resolver"), f"loop.triggers.{name}")
    instance = ""
    if resolver is InstanceResolver.STATIC:
        instance = _require_string(table.get("instance"), f"loop.triggers.{name}.instance")
        if not is_valid_task_name(instance):
            raise LoopConfigError(
                f"loop.triggers.{name}.instance must be a valid instance name in config.toml, "
                f"got {instance!r}"
            )
    elif "instance" in table:
        raise LoopConfigError(
            f"loop.triggers.{name}.instance is only used with instance_resolver = 'static'; "
            f"remove it when instance_resolver is {resolver.value!r}"
        )
    kind_raw = table.get("kind", "manual")
    if not isinstance(kind_raw, str):
        raise LoopConfigError(
            f"loop.triggers.{name}.kind must be a string in config.toml, got {kind_raw!r}"
        )
    try:
        kind = TriggerKind(kind_raw.strip().lower())
    except ValueError as exc:
        raise LoopConfigError(
            f"loop.triggers.{name}.kind must be one of "
            f"{', '.join(k.value for k in TriggerKind)} in config.toml, got {kind_raw!r}"
        ) from exc
    if kind is TriggerKind.CRON:
        raise LoopConfigError(
            f"loop.triggers.{name}.kind cannot be 'cron'; put schedules under [cron.{name}]"
        )
    allowlist = _coerce_string_list(table.get("allowlist"), f"loop.triggers.{name}.allowlist")
    attachment_types = _coerce_string_list(
        table.get("attachment_types"), f"loop.triggers.{name}.attachment_types"
    )
    profiles = _coerce_string_list(table.get("profiles"), f"loop.triggers.{name}.profiles")
    subject_raw = table.get("subject_tag")
    subject_tag: str | None = None
    if subject_raw is not None:
        subject_tag = _require_string(subject_raw, f"loop.triggers.{name}.subject_tag")
    templates = _agent_templates(name, task, kind, table.get("templates"))
    if kind is TriggerKind.LOOP:
        from blumkin.loop.mail_trust import is_allowlist_email

        if not allowlist:
            raise LoopConfigError(
                f"loop.triggers.{name}.allowlist must list at least one sender email "
                "when kind is loop"
            )
        bad = [entry for entry in allowlist if not is_allowlist_email(entry)]
        if bad:
            raise LoopConfigError(
                f"loop.triggers.{name}.allowlist entries must be complete email addresses, "
                f"got {bad!r}"
            )
    if resolver is InstanceResolver.SUBJECT_TRIP and kind is not TriggerKind.LOOP:
        raise LoopConfigError(
            f"loop.triggers.{name}.instance_resolver = 'subject_trip' reads a mail subject "
            "and requires kind = 'loop'"
        )
    if kind is not TriggerKind.LOOP and (
        allowlist or attachment_types or profiles or subject_tag is not None
    ):
        raise LoopConfigError(
            f"loop.triggers.{name} mail fields (allowlist, attachment_types, profiles, "
            "subject_tag) require kind = 'loop'"
        )
    return TriggerSpec(
        allowlist=tuple(allowlist),
        attachment_types=tuple(attachment_types),
        instance=instance,
        instance_resolver=resolver,
        kind=kind,
        name=name,
        profiles=tuple(profiles),
        subject_tag=subject_tag,
        task=task,
        templates=templates,
    )


def _coerce_string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LoopConfigError(f"{label} must be a list of strings in config.toml, got {value!r}")
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise LoopConfigError(f"{label} must be a list of non-empty strings in config.toml")
        out.append(entry.strip())
    return out


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoopConfigError(f"{label} must be a non-empty string in config.toml, got {value!r}")
    return value.strip()
