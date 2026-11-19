"""Tab completion of the browser commands' values, accounts, time zones and addresses (#701 pass).

Every completer reads local files only and completes nothing, rather than failing, when they are
missing. `completion_values.wire` attaches them by name and command path, and the drift tests
below make sure a new command with a `TASK` or a `--tz` cannot be left without one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from blumkin import completion_values
from blumkin.browser.explore import ControlObservation, ModelDraft
from blumkin.browser.model import resolve_store, update_draft
from blumkin.browser.profiles import create_profile
from blumkin.cli import main
from blumkin.config import BrowserSetup, load_browser_setup


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_PROFILE", raising=False)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.work]",
                'client_id = "abc-123"',
                'provider = "microsoft"',
                'tags = ["@work", "m365"]',
                "",
                "[profiles.personal]",
                'provider = "google"',
                'tags = ["@personal"]',
                "",
                "[browser.tasks.expense-report]",
                'allowed_hosts = ["erp.example.com"]',
                "",
                "[browser.tasks.time-sheet]",
                'allowed_hosts = ["erp.example.com"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    create_profile(tmp_path, "expense-report")
    create_profile(tmp_path, "teams-transcript")  # a profile with no policy
    return tmp_path


def _complete(words: str) -> list[str]:
    result = CliRunner().invoke(
        main,
        [],
        env={
            "_BLUMKIN_COMPLETE": "bash_complete",
            "COMP_WORDS": words,
            "COMP_CWORD": str(len(words.split()) - (0 if words.endswith(" ") else 1)),
        },
        prog_name="blumkin",
    )
    assert result.exit_code == 0, result.output
    return [line.split(",", 1)[-1] for line in result.output.splitlines() if line]


# --------------------------------------------------------------------------- tasks


def test_a_browser_task_completes_from_the_policies_and_the_profiles(config: Path) -> None:
    names = _complete("blumkin browser click ")
    assert {"expense-report", "time-sheet", "teams-transcript"} <= set(names)


def test_a_browser_task_completes_only_matching_prefixes(config: Path) -> None:
    assert _complete("blumkin browser read ti") == ["time-sheet"]
    assert _complete("blumkin browser read zzz") == []


@pytest.mark.parametrize(
    "words",
    [
        "blumkin browser ledger status ",
        "blumkin browser explore observe ",
        "blumkin browser model check ",
    ],
)
def test_the_nested_browser_commands_complete_the_task_too(config: Path, words: str) -> None:
    assert "expense-report" in _complete(words)


def test_a_broken_config_completes_nothing_instead_of_failing(
    config: Path,
) -> None:
    (config / "config.toml").write_text("this is [not toml", encoding="utf-8")

    assert _complete("blumkin browser click ") == []


# --------------------------------------------------------------------------- instances and pages


def _begin_instance(config: Path, task: str, instance: str) -> None:
    result = CliRunner().invoke(main, ["browser", "ledger", "begin", task, instance])
    assert result.exit_code == 0, result.output


def test_a_ledger_instance_completes_from_the_task_already_typed(config: Path) -> None:
    _begin_instance(config, "expense-report", "trip-lisbon-2026")
    _begin_instance(config, "expense-report", "trip-nyc-2026")
    _begin_instance(config, "time-sheet", "2026-11-15")

    assert set(_complete("blumkin browser ledger status expense-report ")) >= {
        "trip-lisbon-2026",
        "trip-nyc-2026",
    }
    assert "2026-11-15" not in _complete("blumkin browser ledger status expense-report ")
    assert _complete("blumkin browser ledger status expense-report trip-l") == ["trip-lisbon-2026"]


def test_an_instance_completes_nothing_before_the_task_is_typed(config: Path) -> None:
    assert _complete("blumkin browser ledger status ") != []  # that is the task, not an instance
    assert (
        completion_values.ledger_instances(
            click.Context(click.Command("x")), click.Argument(["i"]), ""
        )
        == []
    )


def _draft_with_pages(config: Path, task: str, *pages: str) -> None:
    store = resolve_store(BrowserSetup(browser=load_browser_setup().browser, config_dir=config))

    def change(current: Any) -> Any:
        draft = ModelDraft(task=task, hosts=("erp.example.com",))
        for page in pages:
            draft.add_page(
                page,
                f"https://erp.example.com/{page}",
                (ControlObservation("button", "Add line"), ControlObservation("link", "Help")),
            )
        return draft.to_model(recorded_at="2026-11-19T00:00:00Z", blumkin_version="1.0.0")

    update_draft(store, task, change)


def test_a_model_page_id_completes_from_the_tasks_draft(config: Path) -> None:
    _draft_with_pages(config, "expense-report", "expense-list", "expense-item")

    assert set(_complete("blumkin browser model check expense-report ")) == {
        "expense-item",
        "expense-list",
    }
    assert _complete("blumkin browser explore forget expense-report expense-l") == ["expense-list"]


def test_a_page_id_completes_nothing_for_a_task_with_no_model(config: Path) -> None:
    assert _complete("blumkin browser model check time-sheet ") == []


# --------------------------------------------------------------------------- roles


def test_role_completion_depends_on_what_the_command_acts_on(config: Path) -> None:
    clickable = _complete("blumkin browser click expense-report --role ")
    fields = _complete("blumkin browser fill expense-report Amount 5 --role ")
    explored = _complete("blumkin browser explore click expense-report Help --role ")

    assert "link" in clickable and "menuitem" in clickable and "textbox" not in clickable
    assert "textbox" in fields and "combobox" in fields and "link" not in fields
    assert set(explored) == set(clickable)


def test_inspect_completes_the_wider_role_set_it_accepts(config: Path) -> None:
    roles = set(_complete("blumkin browser inspect expense-report Amount --role "))

    assert {"textbox", "button", "dialog", "link", "row", "cell"} <= roles


def test_pick_roles_complete_as_a_choice(config: Path) -> None:
    assert set(_complete("blumkin browser pick expense-report Boston --role ")) == {
        "cell",
        "gridcell",
        "row",
        "rowheader",
    }


def test_within_completes_the_container_roles_but_not_a_name(config: Path) -> None:
    roles = _complete("blumkin browser read expense-report --aria --within ")
    assert {"dialog", "menu", "grid"} <= set(roles)
    assert _complete("blumkin browser read expense-report --aria --within dia") == ["dialog"]
    assert _complete("blumkin browser read expense-report --aria --within dialog:Man") == []


# --------------------------------------------------------------------------- accounts and zones


def test_the_global_profile_completes_names_and_tags(config: Path) -> None:
    found = set(_complete("blumkin --profile "))

    assert {"work", "personal", "@work", "@personal", "m365"} <= found


def test_a_tag_that_only_differs_by_a_leading_at_or_case_is_not_offered_either(
    config: Path,
) -> None:
    """The resolver compares tags lowercased and without `@`: `@team` and `Team` collide."""
    text = (config / "config.toml").read_text(encoding="utf-8")
    (config / "config.toml").write_text(
        text
        + '\n[profiles.third]\nprovider = "google"\ntags = ["@team"]\n'
        + '\n[profiles.fourth]\nprovider = "google"\ntags = ["Team"]\n',
        encoding="utf-8",
    )

    found = set(_complete("blumkin --profile "))

    assert "@team" not in found and "Team" not in found
    assert {"third", "fourth"} <= found  # the names themselves are still fine


def test_a_tag_two_profiles_share_or_that_is_a_profile_name_is_not_offered(config: Path) -> None:
    """`blumkin --profile @personal` with two profiles tagged so is refused as ambiguous."""
    text = (config / "config.toml").read_text(encoding="utf-8")
    (config / "config.toml").write_text(
        text
        + '\n[profiles.second]\nprovider = "google"\n'
        + 'tags = ["@personal", "work", "@only-second"]\n',
        encoding="utf-8",
    )

    found = set(_complete("blumkin --profile "))

    # `second` is tagged `@personal` (as `personal` is) and `work` (the name of `work`): the
    # resolver refuses all of those as ambiguous, so only what it accepts is offered
    assert found == {"m365", "@only-second", "second"}
    assert _complete("blumkin --profile @pers") == []


@pytest.mark.parametrize(
    "words", ["blumkin mcp serve --profile ", "blumkin mcp install --profile "]
)
def test_mcp_profile_completes_names_and_tags_as_the_global_option_does(
    config: Path, words: str
) -> None:
    assert {"work", "personal", "@work", "@personal", "m365"} <= set(_complete(words))


def test_loop_init_profile_completes_names_only(config: Path) -> None:
    """`loop init` compares what it is given with the configured names: a tag would fail."""
    found = set(_complete("blumkin loop init --profile "))

    assert {"work", "personal"} <= found
    assert not any(value.startswith("@") or value == "m365" for value in found)


def test_a_time_zone_completes_for_the_global_and_the_command_option(config: Path) -> None:
    assert "America/New_York" in _complete("blumkin --tz America/New")
    assert "America/New_York" in _complete("blumkin calendar today --tz America/New")
    assert _complete("blumkin calendar today --tz zzzz") == []


def test_a_mail_folder_completes_the_well_known_names(config: Path) -> None:
    assert _complete("blumkin mail list --folder s") == ["sentitems"]
    assert "drive" not in _complete("blumkin mail list --folder ")
    assert _complete("blumkin drive list --folder ") == []  # a drive path is not a mail folder


def test_mail_recipients_and_calendar_attendees_complete_from_email_context(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_PROFILE", "work")  # two profiles are configured: one is chosen
    (config / "email-context.md").write_text(
        "| Name | Aliases | Email | Notes |\n|---|---|---|---|\n"
        "| Sam Rivera | sam | sam@example.com | peer |\n"
        "| Ada Lovelace | ada | ada@example.com | |\n",
        encoding="utf-8",
    )

    assert _complete("blumkin mail draft --to s") == ["sam@example.com"]
    assert _complete("blumkin calendar update --with a") == ["ada@example.com"]
    assert _complete("blumkin mail draft --cc ") != []
    # a comma-separated list: the last address completes and the earlier ones stay
    assert _complete("blumkin mail draft --to sam@example.com,a") == [
        "sam@example.com,ada@example.com"
    ]
    assert _complete("blumkin mail draft --to sam@example.com,zzz") == []
    # `mail move --to` is a destination folder, not a recipient
    assert _complete("blumkin mail move --to s") == []
    assert _complete("blumkin mail move --to ") == []


# --------------------------------------------------------------------------- the wiring


def _walk(command: click.Command, path: tuple[str, ...] = ()) -> Any:
    yield path, command
    if isinstance(command, click.Group):
        for name, child in command.commands.items():
            yield from _walk(child, (*path, name))


def test_every_browser_task_argument_has_a_completer() -> None:
    missing = [
        " ".join(path)
        for path, command in _walk(main)
        if path[:1] == ("browser",)
        for param in command.params
        if isinstance(param, click.Argument)
        and param.name == "task"
        and getattr(param, "_custom_shell_complete", None) is None
    ]
    assert missing == []


def test_every_instance_and_page_argument_under_browser_has_a_completer() -> None:
    missing = [
        f"{' '.join(path)} {param.name}"
        for path, command in _walk(main)
        if path[:1] == ("browser",)
        for param in command.params
        if isinstance(param, click.Argument)
        and param.name in {"instance", "page_id"}
        and getattr(param, "_custom_shell_complete", None) is None
    ]
    assert missing == []


def test_every_time_zone_and_profile_option_has_a_completer() -> None:
    missing = [
        f"{' '.join(path)} {param.opts[0]}"
        for path, command in _walk(main)
        for param in command.params
        if isinstance(param, click.Option)
        and ("--tz" in param.opts or "--profile" in param.opts)
        and getattr(param, "_custom_shell_complete", None) is None
    ]
    assert missing == []


def test_wiring_leaves_a_choice_and_an_existing_completer_alone() -> None:
    group = click.Group("g")

    @group.command()
    @click.option("--tz", type=click.Choice(["UTC", "EST"]))
    @click.option("--profile", shell_complete=lambda ctx, param, incomplete: [])
    def sub(tz: str, profile: str) -> None:  # pragma: no cover - never run
        pass

    own = group.commands["sub"].params[1]._custom_shell_complete  # type: ignore[attr-defined]
    completion_values.wire(group)

    tz_param, profile_param = group.commands["sub"].params
    assert getattr(tz_param, "_custom_shell_complete", None) is None  # a Choice completes itself
    assert profile_param._custom_shell_complete is own  # type: ignore[attr-defined]


def test_a_parameter_outside_its_command_path_is_not_wired() -> None:
    group = click.Group("g")

    @group.command()
    @click.option("--folder")
    def drive(folder: str) -> None:  # pragma: no cover - never run
        pass

    completion_values.wire(group)

    assert getattr(group.commands["drive"].params[0], "_custom_shell_complete", None) is None


def test_profile_selectors_returns_resolvable_values_with_the_profile_they_name(
    config: Path,
) -> None:
    from blumkin.config import profile_selectors

    names_only = profile_selectors(tags=False)
    with_tags = profile_selectors(tags=True)

    assert sorted(names_only) == [("personal", "personal"), ("work", "work")]
    assert ("@work", "work") in with_tags and ("m365", "work") in with_tags
    assert ("@personal", "personal") in with_tags


def test_profile_selectors_refuses_a_config_that_cannot_be_read(config: Path) -> None:
    from blumkin.config import ProviderConfigError, profile_selectors

    (config / "config.toml").write_text("[profiles]\n", encoding="utf-8")

    with pytest.raises(ProviderConfigError):
        profile_selectors(tags=True)
