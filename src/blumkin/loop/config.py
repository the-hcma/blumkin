"""Parse `[loop]` and `[cron.*]` from config.toml (issue #501)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import is_valid_task_name
from blumkin.loop.types import TriggerKind, TriggerSpec

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 30.0
DEFAULT_RUN_BUDGET_SECONDS = 600.0
_NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")
_TRIGGER_KEYS_CRON = frozenset({"instance", "schedule", "task", "timezone", "working_days_only"})
_TRIGGER_KEYS_LOOP = frozenset({"instance", "kind", "task"})


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Top-level `[loop]` runner settings."""

    kill_switch: bool = False
    kill_switch_file: Path | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
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
    return TriggerSpec(
        instance=instance,
        kind=TriggerKind.CRON,
        name=name,
        schedule=schedule,
        task=task,
        timezone=timezone,
        working_days_only=_coerce_bool(working, f"cron.{name}.working_days_only"),
    )


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
            "retry_backoff_seconds",
            "run_budget_seconds",
            "triggers",
        }
    )
    if unknown:
        raise LoopConfigError(
            f"loop has unknown key(s) {', '.join(unknown)} in config.toml; "
            "expected kill_switch, kill_switch_file, max_retries, retry_backoff_seconds, "
            "run_budget_seconds, triggers"
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
    return LoopConfig(
        kill_switch=_coerce_bool(raw.get("kill_switch", False), "loop.kill_switch"),
        kill_switch_file=kill_file,
        max_retries=_coerce_int(
            raw.get("max_retries", DEFAULT_MAX_RETRIES), "loop.max_retries", minimum=0
        ),
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
    instance = _require_string(table.get("instance"), f"loop.triggers.{name}.instance")
    if not is_valid_task_name(instance):
        raise LoopConfigError(
            f"loop.triggers.{name}.instance must be a valid instance name in config.toml, "
            f"got {instance!r}"
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
    return TriggerSpec(instance=instance, kind=kind, name=name, task=task)


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoopConfigError(f"{label} must be a non-empty string in config.toml, got {value!r}")
    return value.strip()
