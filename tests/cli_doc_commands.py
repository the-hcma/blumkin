"""Helpers that check `blumkin ...` command lines found in docs against the real CLI."""

from __future__ import annotations

import re
import shlex

import click

from blumkin.cli import main

_GLOBAL_FLAGS_WITH_VALUE = frozenset({"--profile", "--tz"})


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
    """What in `line` is not a real command, or a flag the resolved command lacks."""
    try:
        words = shlex.split(line)[1:]
    except ValueError:
        return [f"unparseable: {line}"]
    command: click.Command = main
    index = 0
    while index < len(words) and words[index].startswith("--"):
        index += 2 if words[index] in _GLOBAL_FLAGS_WITH_VALUE else 1
    problems: list[str] = []
    while index < len(words) and isinstance(command, click.Group):
        word = words[index]
        if word.startswith(("-", "<", "$", "'", '"')):
            break
        nxt = command.commands.get(word)
        if nxt is None:
            problems.append(f"no command {word!r} in: {line}")
            return problems
        command, index = nxt, index + 1
    if isinstance(command, click.Group):
        return problems
    known = {"--help", "--json"}
    for param in command.params:
        if isinstance(param, click.Option):
            known.update(param.opts)
            known.update(param.secondary_opts)
    for word in words[index:]:
        if word == "--":
            break  # a bare `--` ends the options: what follows is a value, not a flag
        if word.startswith("--") and word.split("=", 1)[0] not in known:
            problems.append(f"unknown flag {word!r} for {command.name!r} in: {line}")
    return problems


def _first_word(line: str) -> str:
    parts = line.split()
    for part in parts[1:]:
        if not part.startswith("--"):
            return part
    return ""
