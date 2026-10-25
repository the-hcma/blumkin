"""Helpers that check `blumkin ...` command lines found in docs against the real CLI."""

from __future__ import annotations

import re
import shlex

import click

from blumkin.cli import main


def command_lines(text: str, groups: tuple[str, ...]) -> list[str]:
    """`blumkin <group> ...` lines from fenced code blocks and inline code spans."""
    lines: list[str] = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL):
        joined = block.replace("\\\n", " ")
        lines.extend(
            line.strip() for line in joined.splitlines() if line.strip().startswith("blumkin ")
        )
    lines.extend(re.findall(r"`(blumkin [^`]+)`", text))
    return [line for line in lines if _first_word(line) in groups]


def unknown_parts(line: str) -> list[str]:
    """What in `line` is not a real command, or an option the command at that level lacks."""
    try:
        words = shlex.split(line)[1:]
    except ValueError:
        return [f"unparseable: {line}"]
    command: click.Command = main
    problems: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            break  # a bare `--` ends the options: what follows is a value, not a flag
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if name not in _known_options(command):
                problems.append(f"unknown option {name!r} for {command.name!r} in: {line}")
                index += 1
            else:
                index += 2 if _takes_value(command, name) and "=" not in word else 1
            continue
        if word.startswith(("-", "<", "$", "'", '"')):
            index += 1
            continue
        if isinstance(command, click.Group):
            nxt = command.commands.get(word)
            if nxt is None:
                problems.append(f"no command {word!r} in: {line}")
                return problems
            command = nxt
        index += 1
    return problems


def _first_word(line: str) -> str:
    parts = line.split()
    for part in parts[1:]:
        if not part.startswith("--"):
            return part
    return ""


def _known_options(command: click.Command) -> set[str]:
    known = {"--help", "--json"}
    for param in command.params:
        if isinstance(param, click.Option):
            known.update(param.opts)
            known.update(param.secondary_opts)
    return known


def _takes_value(command: click.Command, name: str) -> bool:
    return any(
        isinstance(param, click.Option) and name in param.opts and not param.is_flag
        for param in command.params
    )
