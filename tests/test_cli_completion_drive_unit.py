"""Shell completion for the drive verbs that move files in and out (issue #555)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin.cli import main


def _lines(words: str) -> list[str]:
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
    return [line for line in result.output.splitlines() if line]


def _values(words: str) -> list[str]:
    return [line.split(",", 1)[-1] for line in _lines(words)]


def test_the_drive_verbs_complete_including_upload_and_download() -> None:
    verbs = _values("blumkin drive ")
    for verb in ("download", "upload", "export", "mkdir"):
        assert verb in verbs


def test_upload_completes_all_of_its_options() -> None:
    options = _values("blumkin drive upload --")
    for option in ("--file", "--folder", "--folder-id", "--mime-type", "--name", "--yes", "--json"):
        assert option in options


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("image/", ["image/gif", "image/jpeg", "image/png", "image/svg+xml", "image/webp"]),
        ("application/p", ["application/pdf"]),
        ("zzz", []),
    ],
)
def test_upload_mime_types_complete_from_the_common_ones(typed: str, expected: list[str]) -> None:
    assert _values(f"blumkin drive upload --mime-type {typed}") == expected


def test_upload_and_download_paths_are_left_to_the_shells_own_file_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "photo.png").write_bytes(b"x")
    assert _lines("blumkin drive upload --file ph")[0].startswith("file,")
    assert _lines("blumkin drive download --id 1a --out ph")[0].startswith("file,")
    assert _lines("blumkin drive export --id 1a --to ph")[0].startswith("file,")
