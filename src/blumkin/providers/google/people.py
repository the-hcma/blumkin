"""Google People search, shaped like ``blumkin.skills.people``.

Two sources, merged: the signed-in user's own contacts, and (on Workspace) the
domain directory. A consumer Gmail account has no directory, so that half is
allowed to fail without sinking the lookup.

Fail-closed semantics match the Microsoft provider exactly: zero matches raise
``LookupError``, several return ``ambiguous: true`` with the full candidate list
and no winner, and exactly one sets ``person``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import PEOPLE_SCOPES, get_credentials, persisted_granted_scopes
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.people import _DEFAULT_TOP, _MAX_TOP, _dedupe_matches

_READ_MASK = "emailAddresses,names,organizations"
# People API caps searchContacts / searchDirectoryPeople pageSize at 30, below the
# shared --top ceiling of 50. Clamp rather than reject, so --top means the same
# thing on both providers instead of erroring where Graph succeeds.
_DIRECTORY_SCOPE = "https://www.googleapis.com/auth/directory.readonly"
_MAX_PAGE_SIZE = 30


async def people_resolve(
    *,
    name: str | None = None,
    email: str | None = None,
    top: int = _DEFAULT_TOP,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    query_name = (name or "").strip() or None
    query_email = (email or "").strip() or None
    if query_name is None and query_email is None:
        raise ValueError("provide --name and/or --email")
    if top < 1:
        raise ValueError("--top must be >= 1")
    if top > _MAX_TOP:
        raise ValueError(f"--top must be <= {_MAX_TOP}")

    # Email is the more specific key, so search on it when present: a top-N cut on
    # a fuzzy name could otherwise hide the exact address.
    search = query_email or query_name
    assert search is not None
    cfg = config or load_config()
    # Read granted scopes before get_credentials may refresh and rewrite the token file.
    granted_directory_scope = _has_directory_scope(cfg)
    service = _people_service(cfg)
    matches = _merge_by_any_address(
        [
            _person_to_dict(result)
            for result in (
                *_search_contacts(service, search, min(top, _MAX_PAGE_SIZE)),
                *_search_directory(
                    service,
                    search,
                    min(top, _MAX_PAGE_SIZE),
                    granted_directory_scope=granted_directory_scope,
                ),
            )
        ]
    )
    if query_email is not None:
        needle = query_email.casefold()
        matches = [
            person
            for person in matches
            if needle == (person.get("email") or "").casefold()
            or any(needle == addr.casefold() for addr in person.get("emails") or [])
        ]
        # A merged row keeps whichever source came first as its primary, and
        # contacts are merged before the directory - so resolving a work address
        # could hand back the personal one the contact card flags as primary, and
        # the caller may then mail it. The address that was asked for is the
        # address to answer with; keep the API's own casing for it.
        for person in matches:
            canonical = next(
                (addr for addr in person.get("emails") or [] if addr.casefold() == needle),
                None,
            )
            if canonical is not None:
                person["email"] = canonical
    if query_name is not None and query_email is not None:
        name_needle = query_name.casefold()
        matches = [
            person
            for person in matches
            if name_needle in (person.get("display_name") or "").casefold()
        ]
    if not matches:
        label = query_name or query_email
        raise LookupError(f"no people match for {label!r}")
    ambiguous = len(matches) != 1
    if not ambiguous and not matches[0].get("email"):
        label = query_name or query_email
        raise LookupError(f"no email on people match for {label!r}")
    return {
        "ambiguous": ambiguous,
        "matches": matches,
        "person": None if ambiguous else matches[0],
        "query": {"email": query_email, "name": query_name},
    }


def _merge_by_any_address(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe on any shared address, then fall back to the shared identity keys.

    Contacts and the directory legitimately disagree about which address is
    primary for the same human - a contact often flags a personal address while
    the directory profile flags the work one - so keying on the primary alone
    would return one person twice and report them as ambiguous.
    """
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for person in people:
        addresses = {str(a).casefold() for a in (person.get("emails") or []) if a}
        primary = person.get("email")
        if primary:
            addresses.add(str(primary).casefold())
        hit = next((seen[a] for a in addresses if a in seen), None)
        if hit is not None:
            # Same human from a second source: keep the richer row's missing fields.
            existing = out[hit]
            merged = {*(existing.get("emails") or []), *(person.get("emails") or [])}
            existing["emails"] = sorted(merged)
            for key in ("company", "display_name", "job_title"):
                existing[key] = existing.get(key) or person.get(key)
            for addr in addresses:
                seen[addr] = hit
            continue
        if addresses:
            for addr in addresses:
                seen[addr] = len(out)
            out.append(person)
        else:
            out.append(person)
    # Rows without any address still need the shared name/UPN dedupe.
    return _dedupe_matches(out)


def _has_directory_scope(cfg: BlumkinConfig) -> bool:
    """True when the *persisted* token was consented with directory.readonly.

    Uses ``persisted_granted_scopes`` rather than ``creds.scopes``: load rebuilds
    every credential with ``scopes=sorted(GOOGLE_SCOPES)``, and a silent refresh
    used to rewrite the file from ``to_json()`` with that full set even when the
    user never consented. Unreadable or absent means "assume not granted", so a
    directory 403 surfaces as missing_scope rather than being silently swallowed.
    """
    return _DIRECTORY_SCOPE in persisted_granted_scopes(cfg)


def _http_status(exc: HttpError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    value = getattr(resp, "status", None) if resp is not None else None
    try:
        return int(value) if value is not None else None
    except TypeError, ValueError:
        return None


def _people_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=PEOPLE_SCOPES)
    return build_api_service("people", "v1", creds=creds, config=cfg)


def _person_to_dict(result: Mapping[str, Any]) -> dict[str, Any]:
    person = result.get("person") if "person" in result else result
    person = person if isinstance(person, Mapping) else {}
    addresses = [
        str(item.get("value"))
        for item in person.get("emailAddresses") or []
        if isinstance(item, Mapping) and item.get("value")
    ]
    primary = next(
        (
            str(item["value"])
            for item in person.get("emailAddresses") or []
            if isinstance(item, Mapping)
            and item.get("value")
            and (item.get("metadata") or {}).get("primary")
        ),
        addresses[0] if addresses else None,
    )
    names = person.get("names") or []
    organizations = person.get("organizations") or []
    first_name = names[0] if names and isinstance(names[0], Mapping) else {}
    first_org = organizations[0] if organizations and isinstance(organizations[0], Mapping) else {}
    return {
        "company": first_org.get("name"),
        "display_name": first_name.get("displayName"),
        "email": primary,
        "emails": addresses,
        "job_title": first_org.get("title"),
        # Graph-only concept; kept so the --json shape matches across providers.
        "user_principal_name": None,
    }


def _search_contacts(service: Any, query: str, top: int) -> list[Mapping[str, Any]]:
    response = execute(
        service.people().searchContacts(query=query, pageSize=top, readMask=_READ_MASK)
    )
    return [item for item in (response.get("results") or []) if isinstance(item, Mapping)]


def _search_directory(
    service: Any, query: str, top: int, *, granted_directory_scope: bool
) -> list[Mapping[str, Any]]:
    """Domain directory results, or nothing at all.

    A consumer Gmail account has no directory and answers 403/404 here; that is a
    normal shape for this CLI's personal profile, not a failure worth surfacing,
    so the contacts half of the search still stands on its own.

    Everything else propagates. Swallowing a 429 or a 5xx would drop the directory
    silently and could turn two real candidates into one confident answer - turning
    a deliberately fail-closed resolver into a fail-open wrong one.
    """
    try:
        response = execute(
            service.people().searchDirectoryPeople(
                query=query,
                pageSize=top,
                readMask=_READ_MASK,
                sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
            )
        )
    except HttpError as exc:
        status = _http_status(exc)
        if status == 404 or (status == 403 and granted_directory_scope):
            return []
        # A 403 on a token that never carried directory.readonly is a scope problem,
        # not "this account has no directory". Propagating it lets the CLI report
        # missing_scope (exit 4) and the documented re-consent, instead of quietly
        # answering from contacts alone.
        raise
    return [item for item in (response.get("people") or []) if isinstance(item, Mapping)]
