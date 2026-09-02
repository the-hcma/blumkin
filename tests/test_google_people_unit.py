"""Hermetic tests for Google people resolve."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_auth import GOOGLE_SCOPES
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind

_DIRECTORY_SCOPE = "https://www.googleapis.com/auth/directory.readonly"
_GOOGLE_PEOPLE = "blumkin.providers.google.people"


def _contact(name: str, *emails: str, title: str | None = None, company: str | None = None) -> dict:
    person: dict = {
        "names": [{"displayName": name}],
        "emailAddresses": [
            {"value": addr, "metadata": {"primary": i == 0}} for i, addr in enumerate(emails)
        ],
    }
    if title or company:
        person["organizations"] = [{"title": title, "name": company}]
    return {"person": person}


def test_people_resolve_returns_one_match(tmp_path: Path) -> None:
    service = _service(
        contacts=[_contact("Ada Lovelace", "ada@example.com", title="Fellow", company="BRK")]
    )
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada"))
    assert payload["ambiguous"] is False
    assert payload["person"]["email"] == "ada@example.com"
    assert payload["person"]["display_name"] == "Ada Lovelace"
    assert payload["person"]["job_title"] == "Fellow"
    assert payload["person"]["company"] == "BRK"
    assert payload["query"] == {"email": None, "name": "Ada"}


def test_people_resolve_is_ambiguous_with_several_matches(tmp_path: Path) -> None:
    """Fail closed like the Microsoft path: candidates listed, no winner picked."""
    service = _service(
        contacts=[
            _contact("Ada Lovelace", "ada@example.com"),
            _contact("Ada Byron", "byron@example.com"),
        ]
    )
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada"))
    assert payload["ambiguous"] is True
    assert payload["person"] is None
    assert len(payload["matches"]) == 2


def test_people_resolve_raises_when_nothing_matches(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(LookupError, match="no people match"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Nobody"))


def test_people_resolve_merges_directory_and_dedupes_by_email(tmp_path: Path) -> None:
    service = _service(
        contacts=[_contact("Ada Lovelace", "ada@example.com")],
        directory=[_contact("Ada Lovelace", "ada@example.com")["person"]],
    )
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada"))
    # Same person from both sources must not read as an ambiguous pair.
    assert payload["ambiguous"] is False
    assert payload["person"]["email"] == "ada@example.com"


def test_people_resolve_survives_a_consumer_account_without_a_directory(tmp_path: Path) -> None:
    """Personal Gmail has no domain directory; contacts alone must still resolve."""
    service = _service(contacts=[_contact("Ada Lovelace", "ada@example.com")])
    service.people.return_value.searchDirectoryPeople.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 403}), b"{}", uri="x"
    )
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada"))
    assert payload["person"]["email"] == "ada@example.com"


def test_people_resolve_filters_by_exact_email(tmp_path: Path) -> None:
    service = _service(
        contacts=[_contact("Ada Lovelace", "ada@example.com"), _contact("Bob", "bob@example.com")]
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(email="BOB@example.com")
        )
    assert payload["ambiguous"] is False
    assert payload["person"]["email"] == "bob@example.com"


def test_people_resolve_validates_arguments(tmp_path: Path) -> None:
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
    with _patched(_service()):
        with pytest.raises(ValueError, match="provide --name and/or --email"):
            asyncio.run(provider.people_resolve())
        with pytest.raises(ValueError, match=r"--top must be >= 1"):
            asyncio.run(provider.people_resolve(name="Ada", top=0))
        with pytest.raises(ValueError, match=r"--top must be <= 50"):
            asyncio.run(provider.people_resolve(name="Ada", top=51))


def test_google_scopes_include_people_reads() -> None:
    assert "https://www.googleapis.com/auth/contacts.readonly" in GOOGLE_SCOPES
    assert "https://www.googleapis.com/auth/directory.readonly" in GOOGLE_SCOPES


def _cfg(config_dir: Path, *, scopes: tuple[str, ...] = (_DIRECTORY_SCOPE,)) -> BlumkinConfig:
    _write_token(config_dir, *scopes)
    oauth = config_dir / "desktop-client.json"
    if not oauth.is_file():
        oauth.write_text(
            '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}'
        )
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_PEOPLE,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _write_token(config_dir: Path, *scopes: str) -> None:
    """Persist the granted-scope set the way google_auth does."""
    (config_dir / "google_token.json").write_text(json.dumps({"scopes": list(scopes)}))


def _service(
    *, contacts: list[dict] | None = None, directory: list[dict] | None = None
) -> MagicMock:
    service = MagicMock()
    people = service.people.return_value
    people.searchContacts.return_value.execute.return_value = {"results": contacts or []}
    people.searchDirectoryPeople.return_value.execute.return_value = {"people": directory or []}
    return service


def test_people_resolve_clamps_top_to_the_people_api_page_cap(tmp_path: Path) -> None:
    """--top 50 is legal on Graph; People caps pageSize at 30, so clamp rather than 400."""
    service = _service(contacts=[_contact("Ada Lovelace", "ada@example.com")])
    with _patched(service):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada", top=50))
    assert service.people.return_value.searchContacts.call_args.kwargs["pageSize"] == 30
    assert service.people.return_value.searchDirectoryPeople.call_args.kwargs["pageSize"] == 30


def test_people_resolve_propagates_a_transient_directory_failure(tmp_path: Path) -> None:
    """Swallowing a 429/5xx could turn two real candidates into one confident answer."""
    service = _service(contacts=[_contact("Ada Lovelace", "ada@example.com")])
    service.people.return_value.searchDirectoryPeople.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 429}), b"{}", uri="x"
    )
    with _patched(service), pytest.raises(HttpError):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada"))


def test_people_resolve_name_filter_applies_on_top_of_an_email_match(tmp_path: Path) -> None:
    service = _service(contacts=[_contact("Ada Lovelace", "ada@example.com")])
    with _patched(service):
        # Email hits, display name does not -> fail closed rather than return a winner.
        with pytest.raises(LookupError, match="no people match"):
            asyncio.run(
                GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(
                    name="Grace", email="ada@example.com"
                )
            )
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(
                name="Lovelace", email="ada@example.com"
            )
        )
    assert payload["person"]["email"] == "ada@example.com"


def test_people_resolve_refuses_a_unique_match_without_an_email(tmp_path: Path) -> None:
    """A single hit with no address is not something a caller can act on."""
    service = _service(contacts=[_contact("Ada Lovelace")])
    with _patched(service), pytest.raises(LookupError, match="no email on people match"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada"))


def test_people_resolve_propagates_403_when_the_directory_scope_is_missing(tmp_path: Path) -> None:
    """A stale token without directory.readonly must surface, not answer from contacts."""
    service = _service(contacts=[_contact("Ada Lovelace", "ada@example.com")])
    service.people.return_value.searchDirectoryPeople.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 403}), b"{}", uri="x"
    )
    stale = _cfg(tmp_path, scopes=("https://www.googleapis.com/auth/contacts.readonly",))
    with _patched(service), pytest.raises(HttpError):
        asyncio.run(GoogleWorkspaceProvider(stale).people_resolve(name="Ada"))


def test_people_resolve_merges_one_person_reported_under_two_primaries(tmp_path: Path) -> None:
    """Contacts and the directory disagree on which address is primary for one human."""
    contact = _contact("Ada Lovelace", "ada@personal.example", "ada@work.example")
    directory = _contact("Ada Lovelace", "ada@work.example", "ada@personal.example")["person"]
    service = _service(contacts=[contact], directory=[directory])
    with _patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(name="Ada"))
    # One human, so a confident answer - not a spurious "ambiguous, pick one".
    assert payload["ambiguous"] is False
    assert set(payload["person"]["emails"]) == {"ada@personal.example", "ada@work.example"}


def test_people_resolve_by_email_answers_with_the_address_that_was_asked_for(
    tmp_path: Path,
) -> None:
    """The merged row's primary comes from whichever source merged first.

    Contacts merge before the directory, so a contact card flagging a personal
    address as primary would otherwise win and `person.email` would hand back an
    address the caller never asked about - which an agent may then mail.
    """
    contact = _contact("Ada Lovelace", "ada@personal.example", "ada@work.example")
    directory = _contact("Ada Lovelace", "ada@work.example", "ada@personal.example")["person"]
    service = _service(contacts=[contact], directory=[directory])
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(email="ada@work.example")
        )
    assert payload["ambiguous"] is False
    assert payload["person"]["email"] == "ada@work.example"
    # Both addresses stay visible; only the answer is pinned to the query.
    assert set(payload["person"]["emails"]) == {"ada@personal.example", "ada@work.example"}


def test_people_resolve_by_email_is_case_insensitive_but_returns_api_casing(
    tmp_path: Path,
) -> None:
    contact = _contact("Ada Lovelace", "ada@personal.example", "Ada@Work.example")
    service = _service(contacts=[contact], directory=[])
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).people_resolve(email="ADA@work.EXAMPLE")
        )
    assert payload["person"]["email"] == "Ada@Work.example"
