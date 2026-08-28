"""People directory resolve (Graph /me/people)."""

from __future__ import annotations

from typing import Any

from msgraph.generated.users.item.people.people_request_builder import PeopleRequestBuilder

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config

_DEFAULT_TOP = 10
_MAX_TOP = 50


def format_resolve_human(payload: dict[str, Any]) -> list[str]:
    query = payload.get("query") or {}
    bits = [
        f"{key}={value!r}"
        for key, value in (("name", query.get("name")), ("email", query.get("email")))
        if value
    ]
    label = ", ".join(bits) or "(empty)"
    matches = payload.get("matches") or []
    if payload.get("ambiguous"):
        lines = [
            f"People resolve ambiguous ({label}): {len(matches)} match(es) - "
            "confirm which person (or pass an exact email); do not guess."
        ]
    else:
        lines = [f"People resolve ({label}): 1 match"]
    for person in matches:
        email = person.get("email") or "(no email)"
        name = person.get("display_name") or "(no name)"
        title = person.get("job_title") or ""
        company = person.get("company") or ""
        detail = ", ".join(part for part in (title, company) if part)
        suffix = f" - {detail}" if detail else ""
        lines.append(f"  • {name} <{email}>{suffix}")
    return lines


async def people_resolve(
    *,
    name: str | None = None,
    email: str | None = None,
    top: int = _DEFAULT_TOP,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Resolve a display name or email via Graph people search.

    Fail-closed: zero matches raise ``LookupError``; more than one match returns
    ``ambiguous: true`` with the full candidate list (no winner). Exactly one match
    sets ``person`` and ``ambiguous: false``.
    """
    query_name = (name or "").strip() or None
    query_email = (email or "").strip() or None
    if query_name is None and query_email is None:
        raise ValueError("provide --name and/or --email")
    if top < 1:
        raise ValueError("--top must be >= 1")
    if top > _MAX_TOP:
        raise ValueError(f"--top must be <= {_MAX_TOP}")

    search = query_name or query_email
    assert search is not None
    cfg = config or load_config()
    client = create_graph_client(cfg)
    query = PeopleRequestBuilder.PeopleRequestBuilderGetQueryParameters(
        search=f'"{search}"',
        top=top,
        select=[
            "companyName",
            "displayName",
            "jobTitle",
            "scoredEmailAddresses",
            "userPrincipalName",
        ],
    )
    response = await client.me.people.get(request_config(query))
    raw = [] if response is None else (response.value or [])
    matches = _dedupe_matches([_person_to_dict(item) for item in raw])
    if query_email is not None:
        needle = query_email.casefold()
        matches = [
            person
            for person in matches
            if needle == (person.get("email") or "").casefold()
            or any(needle == addr.casefold() for addr in person.get("emails") or [])
        ]
    if not matches:
        label = query_name or query_email
        raise LookupError(f"no people match for {label!r}")
    ambiguous = len(matches) != 1
    return {
        "ambiguous": ambiguous,
        "matches": matches,
        "person": None if ambiguous else matches[0],
        "query": {"email": query_email, "name": query_name},
    }


def _dedupe_matches(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep first occurrence per primary email (casefold); drop rows with no email."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for person in people:
        email = person.get("email")
        if not email:
            continue
        key = str(email).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(person)
    return out


def _person_to_dict(person: Any) -> dict[str, Any]:
    scored = list(getattr(person, "scored_email_addresses", None) or [])
    emails: list[str] = []
    best: str | None = None
    best_score = float("-inf")
    for item in scored:
        address = getattr(item, "address", None)
        if not address:
            continue
        addr = str(address)
        emails.append(addr)
        score_raw = getattr(item, "relevance_score", None)
        score = float(score_raw) if score_raw is not None else 0.0
        if best is None or score > best_score:
            best = addr
            best_score = score
    if best is None:
        upn = getattr(person, "user_principal_name", None)
        if upn and "@" in str(upn):
            best = str(upn)
            emails = [best]
    return {
        "company": getattr(person, "company_name", None),
        "display_name": getattr(person, "display_name", None),
        "email": best,
        "emails": emails,
        "job_title": getattr(person, "job_title", None),
        "user_principal_name": getattr(person, "user_principal_name", None),
    }
