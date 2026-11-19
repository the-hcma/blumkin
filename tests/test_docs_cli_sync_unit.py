"""The skills and guides stay in step with the CLI, and tab completion with its commands.

Three things drift when a command is added, renamed or loses an option, and nothing else notices:
the docs an agent reads, the commands those docs name, and the values the shell completes. These
checks fail the build instead of waiting for a person to remember (see `.agents/skills/`,
`docs/` and `completion_values.wire`).

- every command the CLI has is named in at least one skill, guide or the README;
- every `blumkin ...` command a skill or guide shows exists, with every `--option` it gives;
- every skill has the front matter a client needs, named like its folder;
- every completer is attached to something, and every attachment rule matches something.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import click
import pytest

from blumkin import completion_values
from blumkin.cli import main

_ROOT = Path(__file__).resolve().parent.parent
_DOCS = [
    *sorted((_ROOT / ".agents" / "skills").glob("*/SKILL.md")),
    *sorted((_ROOT / "docs").glob("*.md")),
    _ROOT / "README.md",
]
#: A command shown as `blumkin ...` in backticks, or a line of a fenced block that starts with it.
_INLINE = re.compile(r"`(blumkin(?: [^`\n]+)?)`")
_FENCE = re.compile(r"^```.*?^```", re.M | re.S)
_OPTION = re.compile(r"(?<![\w-])(--\w[\w-]*)")  # the whole token: `--top_typo` is not `--top`
#: Options click provides on every command without declaring them (`--version` is declared on
#: the program only, so it is accepted at the start of a command and nowhere after it).
_BUILT_IN = {"--help"}
_GLOBAL_BUILT_IN = {"--help", "--version"}
#: Shown on purpose as examples of what is *not* a command or option (nothing yet).
_IGNORED_SPANS: frozenset[str] = frozenset()


def _walk(
    command: click.Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    yield path, command
    if isinstance(command, click.Group):
        for name, child in command.commands.items():
            yield from _walk(child, (*path, name))


def _commands() -> list[tuple[str, ...]]:
    return [path for path, command in _walk(main) if path and not isinstance(command, click.Group)]


def _shown_commands() -> Iterator[tuple[str, str]]:
    """(file, command text) for every `blumkin ...` a document shows."""
    for doc in _DOCS:
        text = doc.read_text(encoding="utf-8")
        for span in _INLINE.findall(text):
            yield doc.name, span
        for block in _FENCE.findall(text):
            for line in block.splitlines():
                stripped = line.strip().removeprefix("$ ")
                if stripped.startswith("blumkin ") or stripped == "blumkin":
                    yield doc.name, stripped


def _resolve(tokens: list[str]) -> tuple[click.Command, tuple[str, ...], list[str]] | None:
    """The command a `blumkin` invocation names, its path, and the words left after it."""
    index = 0
    if tokens and tokens[0] == "-":  # `blumkin - TASK`: the window label, not a command
        return main, (), []
    while index < len(tokens) and tokens[index].startswith("-"):  # a global option first
        name = tokens[index].split("=")[0]
        if name in _GLOBAL_BUILT_IN:
            return main, (), []
        option = next(
            (p for p in main.params if isinstance(p, click.Option) and name in p.opts), None
        )
        if option is None:
            return None
        index += 1 if option.is_flag or "=" in tokens[index] else 2
    command: click.Command = main
    path: list[str] = []
    while (
        isinstance(command, click.Group)
        and index < len(tokens)
        and tokens[index] in command.commands
    ):
        command = command.commands[tokens[index]]
        path.append(tokens[index])
        index += 1
    return command, tuple(path), tokens[index:]


def _options_of(command: click.Command) -> set[str]:
    """The options a command takes itself: a global option typed after it is a usage error."""
    found = set(_BUILT_IN)
    for param in command.params:
        if isinstance(param, click.Option):
            found.update(param.opts)
            found.update(param.secondary_opts)
    return found


def test_the_docs_scan_finds_the_commands_it_is_meant_to_check() -> None:
    """A silent regex or path change must not turn the checks below into no-ops."""
    shown = list(_shown_commands())

    assert len(shown) > 300
    assert {"SKILL.md", "agent-integration.md", "browser-tasks.md", "README.md"} <= {
        name for name, _ in shown
    }


def test_every_command_the_cli_has_is_named_in_a_skill_or_guide() -> None:
    text = "\n".join(doc.read_text(encoding="utf-8") for doc in _DOCS)
    missing = [
        " ".join(path)
        for path in _commands()
        if not re.search(r"(?<![\w-])" + r"\s+".join(map(re.escape, path)) + r"(?![\w-])", text)
    ]

    assert missing == [], (
        "These commands are in no skill, guide or the README: name each where an agent or a "
        f"person would look for it (the skill for what it does, the guide for how): {missing}"
    )


def _problems_in(name: str, shown: str) -> list[str]:
    """What is wrong with one `blumkin ...` a document shows (empty when it is fine)."""
    if shown in _IGNORED_SPANS:
        return []
    text = re.split(r"\s+#\s", shown, maxsplit=1)[0]  # a trailing comment is prose
    tokens = text.split()[1:]
    if not tokens:
        return []
    resolved = _resolve(tokens)
    if resolved is None:
        return [f"{name}: `{shown}` starts with an option blumkin does not have"]
    command, path, rest = resolved
    if isinstance(command, click.Group):
        word = rest[0] if rest else ""
        # A concrete command word: lowercase, may hold digits and dashes, may be one character.
        # Placeholders (`TASK`, `<id>`, `[x]`) and values never look like this.
        if re.fullmatch(r"[a-z][a-z0-9_-]*", word) and word not in command.commands:
            where = " ".join(path) or "blumkin"
            return [f"{name}: `{shown}`: {word!r} is not a command of {where}"]
    known = _options_of(command)
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", " ".join(rest))  # a value is not an option
    return [
        f"{name}: `{shown}`: {option} is not an option of {' '.join(path)}"
        for option in _OPTION.findall(unquoted)
        if option not in known
    ]


def test_every_command_and_option_a_skill_or_guide_shows_exists() -> None:
    problems = [
        problem for name, shown in _shown_commands() for problem in _problems_in(name, shown)
    ]

    assert problems == [], "Docs name commands or options the CLI no longer has:\n" + "\n".join(
        sorted(set(problems))
    )


@pytest.mark.parametrize(
    "shown",
    [
        "blumkin browser removed2",  # digits
        "blumkin browser x",  # one character
        "blumkin browser remove_it",  # underscore
        "blumkin frobnicate now",
        "blumkin mail draft --bogus-flag",
        "blumkin browser click TASK Go --bogus",
        "blumkin mail inbox --top_typo 5",  # not `--top`
        "blumkin mail inbox --top-typo 5",
        "blumkin mail inbox --version",  # `--version` is the program's, not a command's
    ],
)
def test_the_docs_check_rejects_an_unknown_command_word_or_option(shown: str) -> None:
    assert _problems_in("x.md", shown) != []


@pytest.mark.parametrize(
    "shown",
    [
        "blumkin browser click TASK Go --role link",
        "blumkin browser TASK",  # a placeholder after a group is not a command word
        "blumkin browser <task>",
        "blumkin --profile work mail inbox --top 5",
        "blumkin mail draft --subject '--not-an-option'",
        "blumkin loop install-service   # systemd --user unit",
        "blumkin --help",
        "blumkin --version",
        "blumkin mail inbox --top 5",
        "blumkin mail inbox --top=5",
        "blumkin mail inbox --help",
    ],
)
def test_the_docs_check_accepts_real_commands_placeholders_and_quoted_values(shown: str) -> None:
    assert _problems_in("x.md", shown) == []


@pytest.mark.parametrize(
    "skill", sorted((_ROOT / ".agents" / "skills").glob("*/SKILL.md")), ids=lambda p: p.parent.name
)
def test_every_skill_has_front_matter_named_like_its_folder(skill: Path) -> None:
    text = skill.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)

    assert match is not None, f"{skill} has no front matter"
    front = match.group(1)
    assert re.search(rf"^name: {re.escape(skill.parent.name)}$", front, re.M)
    assert re.search(r"^description: ", front, re.M)


# --------------------------------------------------------------------------- completion


def test_every_completer_is_attached_to_something() -> None:
    used_by_cli = {
        getattr(param, "_custom_shell_complete", None)
        for _, command in _walk(main)
        for param in command.params
    }
    defined = {
        function
        for name, function in vars(completion_values).items()
        if callable(function)
        and getattr(function, "__module__", "") == completion_values.__name__
        and not name.startswith("_")
        and name not in {"wire"}
        and list(getattr(function, "__annotations__", {}))[:3] == ["ctx", "param", "incomplete"]
    }

    unused = sorted(function.__name__ for function in defined - used_by_cli)
    assert unused == [], f"These completers are attached to no parameter: {unused}"


def test_every_wiring_rule_matches_at_least_one_parameter() -> None:
    dead = []
    for kind, name, completer, prefixes in completion_values._WIRING:
        hits = 0
        for path, command in _walk(main):
            for param in command.params:
                same_kind = isinstance(param, click.Argument) == (kind == "argument")
                names = {param.name} if kind == "argument" else set(param.opts)
                if (
                    same_kind
                    and name in names
                    and (not prefixes or any(path[: len(prefix)] == prefix for prefix in prefixes))
                ):
                    hits += 1
        if hits == 0:
            dead.append(f"{kind} {name} -> {completer.__name__}")  # type: ignore[attr-defined]

    assert dead == [], f"These completion rules match nothing (renamed or removed?): {dead}"


# --------------------------------------------------------------------------- the skills registry


def _command_for(cli: list[str]) -> click.Command | None:
    command: click.Command = main
    for word in cli[1:]:  # cli[0] is the program name
        if not isinstance(command, click.Group) or word not in command.commands:
            return None
        command = command.commands[word]
    return command


def test_every_registered_skill_matches_its_cli_command_in_both_directions() -> None:
    """`blumkin skills list` and the MCP tools are built from the registry: an option the CLI
    gained (or lost) must show there too, or an agent is told the wrong arguments."""
    from blumkin.skills import SKILLS

    problems: list[str] = []
    for skill in SKILLS:
        command = _command_for(skill.cli)
        if command is None:
            problems.append(f"{skill.id}: no CLI command {' '.join(skill.cli)}")
            continue
        registered = {arg["name"] for arg in skill.args}
        # One option may have several spellings (`--teams` / `--no-teams`): the registry lists
        # whichever it likes, so compare option by option, not name by name.
        groups = [
            {name for name in (*param.opts, *param.secondary_opts) if name.startswith("--")}
            for param in command.params
            if isinstance(param, click.Option)
        ]
        groups = [group for group in groups if group != {"--json"}]
        positionals = {
            param.name.replace("_", "-")
            for param in command.params
            if isinstance(param, click.Argument) and param.name
        }
        only_cli = sorted(
            [min(group) for group in groups if not group & registered]
            + [name for name in positionals if name not in registered]
        )
        only_registry = sorted(
            name
            for name in registered
            if not any(name in group for group in groups) and name not in positionals
        )
        if only_cli or only_registry:
            problems.append(
                f"{skill.id}: in the CLI only {only_cli}, in the registry only {only_registry}"
            )

    assert problems == [], "The skills registry and the CLI disagree:\n" + "\n".join(problems)


def test_the_readme_says_what_every_wired_completion_completes() -> None:
    """Each value completer is explained where a person looks for it: the README's section."""
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Shell completion")
    section = (
        readme[start : readme.index("\n### ", start + 1)]
        if "\n### " in readme[start + 1 :]
        else readme[start:]
    )

    missing = []
    for kind, name, completer, _ in completion_values._WIRING:
        word = f"`{name.upper()}`" if kind == "argument" else f"`{name}`"
        if word not in section:
            missing.append(f"{word} ({completer.__name__})")  # type: ignore[attr-defined]

    assert missing == [], f"The README's Shell completion section does not mention: {missing}"
