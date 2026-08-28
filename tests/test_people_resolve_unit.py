"""Unit tests for people resolve (Graph /me/people)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_SUCCESS, EXIT_USAGE
from blumkin.skills.people import (
    _person_to_dict,
    format_resolve_human,
    people_resolve,
)


def test_person_to_dict_picks_highest_scored_email() -> None:
    person = SimpleNamespace(
        company_name="Example Org",
        display_name="Ada Example",
        job_title="Engineer",
        scored_email_addresses=[
            SimpleNamespace(address="ada.alt@example.com", relevance_score=1.0),
            SimpleNamespace(address="ada@example.com", relevance_score=9.0),
        ],
        user_principal_name="ada@example.com",
    )
    mapped = _person_to_dict(person)
    assert mapped["email"] == "ada@example.com"
    assert mapped["emails"] == ["ada.alt@example.com", "ada@example.com"]
    assert mapped["display_name"] == "Ada Example"
    assert mapped["job_title"] == "Engineer"
    assert mapped["company"] == "Example Org"


def test_format_resolve_human_lists_candidates() -> None:
    lines = format_resolve_human(
        {
            "ambiguous": True,
            "matches": [
                {
                    "company": "Org",
                    "display_name": "Ada Example",
                    "email": "ada@example.com",
                    "job_title": "Engineer",
                },
                {
                    "company": None,
                    "display_name": "Ada Other",
                    "email": "ada.other@example.com",
                    "job_title": None,
                },
            ],
            "person": None,
            "query": {"email": None, "name": "Ada"},
        }
    )
    assert "ambiguous" in lines[0]
    assert "ada@example.com" in lines[1]
    assert "Ada Other" in lines[2]


def test_people_resolve_unique_match(monkeypatch) -> None:
    person = SimpleNamespace(
        company_name=None,
        display_name="Ada Example",
        job_title=None,
        scored_email_addresses=[
            SimpleNamespace(address="ada@example.com", relevance_score=5.0),
        ],
        user_principal_name="ada@example.com",
    )
    client = MagicMock()
    client.me.people.get = AsyncMock(return_value=SimpleNamespace(value=[person]))
    monkeypatch.setattr("blumkin.skills.people.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.people.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(people_resolve(name="Ada Example"))
    assert payload["ambiguous"] is False
    assert payload["person"]["email"] == "ada@example.com"
    assert len(payload["matches"]) == 1
    query = client.me.people.get.await_args.args[0].query_parameters
    assert query.search == '"Ada Example"'


def test_people_resolve_ambiguous_returns_candidates(monkeypatch) -> None:
    people = [
        SimpleNamespace(
            company_name=None,
            display_name="Ada Example",
            job_title=None,
            scored_email_addresses=[
                SimpleNamespace(address="ada@example.com", relevance_score=5.0),
            ],
            user_principal_name="ada@example.com",
        ),
        SimpleNamespace(
            company_name=None,
            display_name="Ada Other",
            job_title=None,
            scored_email_addresses=[
                SimpleNamespace(address="ada.other@example.com", relevance_score=4.0),
            ],
            user_principal_name="ada.other@example.com",
        ),
    ]
    client = MagicMock()
    client.me.people.get = AsyncMock(return_value=SimpleNamespace(value=people))
    monkeypatch.setattr("blumkin.skills.people.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.people.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(people_resolve(name="Ada"))
    assert payload["ambiguous"] is True
    assert payload["person"] is None
    assert [m["email"] for m in payload["matches"]] == [
        "ada@example.com",
        "ada.other@example.com",
    ]


def test_people_resolve_no_match_raises(monkeypatch) -> None:
    client = MagicMock()
    client.me.people.get = AsyncMock(return_value=SimpleNamespace(value=[]))
    monkeypatch.setattr("blumkin.skills.people.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.people.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    with pytest.raises(LookupError, match="no people match"):
        asyncio.run(people_resolve(name="Nobody"))


def test_people_resolve_email_filters_exact(monkeypatch) -> None:
    people = [
        SimpleNamespace(
            company_name=None,
            display_name="Ada Example",
            job_title=None,
            scored_email_addresses=[
                SimpleNamespace(address="ada@example.com", relevance_score=5.0),
            ],
            user_principal_name="ada@example.com",
        ),
        SimpleNamespace(
            company_name=None,
            display_name="Ada Other",
            job_title=None,
            scored_email_addresses=[
                SimpleNamespace(address="ada.other@example.com", relevance_score=4.0),
            ],
            user_principal_name="ada.other@example.com",
        ),
    ]
    client = MagicMock()
    client.me.people.get = AsyncMock(return_value=SimpleNamespace(value=people))
    monkeypatch.setattr("blumkin.skills.people.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.people.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(people_resolve(email="ADA@example.com"))
    assert payload["ambiguous"] is False
    assert payload["person"]["email"] == "ada@example.com"


def test_people_resolve_requires_query() -> None:
    with pytest.raises(ValueError, match="--name"):
        asyncio.run(people_resolve())


def test_cli_people_resolve_unique_exits_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.cli.people_resolve",
        AsyncMock(
            return_value={
                "ambiguous": False,
                "matches": [{"display_name": "Ada", "email": "ada@example.com"}],
                "person": {"display_name": "Ada", "email": "ada@example.com"},
                "query": {"email": None, "name": "Ada"},
            }
        ),
    )
    result = CliRunner().invoke(main, ["people", "resolve", "--name", "Ada", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert '"ambiguous": false' in result.stdout


def test_cli_people_resolve_ambiguous_exits_usage(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.cli.people_resolve",
        AsyncMock(
            return_value={
                "ambiguous": True,
                "matches": [
                    {"display_name": "Ada", "email": "ada@example.com"},
                    {"display_name": "Ada Other", "email": "ada.other@example.com"},
                ],
                "person": None,
                "query": {"email": None, "name": "Ada"},
            }
        ),
    )
    result = CliRunner().invoke(main, ["people", "resolve", "--name", "Ada", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert '"ambiguous": true' in result.stdout


def test_cli_people_resolve_missing_exits_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.cli.people_resolve",
        AsyncMock(side_effect=LookupError("no people match for 'Nobody'")),
    )
    result = CliRunner().invoke(main, ["people", "resolve", "--name", "Nobody", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert '"error": "not_found"' in result.stderr
