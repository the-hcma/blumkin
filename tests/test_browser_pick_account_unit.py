"""The account-picker script run in a real Chrome page (opt-in: BLUMKIN_BROWSER_TESTS=1, #616)."""

from __future__ import annotations

import os

import pytest

from blumkin.browser.cdp import _PICK_ACCOUNT_JS

pytestmark = pytest.mark.skipif(
    os.environ.get("BLUMKIN_BROWSER_TESTS") != "1", reason="needs BLUMKIN_BROWSER_TESTS=1"
)

_PICKER = """
<div role=button data-test-id="ada@example.com"><div>Ada</div><div>ada@example.com</div></div>
<div role=button data-test-id="bob@example.com"><div>bob@example.com</div></div>
<div role=button id=other>Use another account</div>
"""


def _pick(html: str, email: str, *, url: str = "https://login.example.net/", **policy: object):
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    args = {"blocked": [], "email": email, "hosts": ["login.example.net"], **policy}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.route(
                "http*://**/*",
                lambda route: route.fulfill(body=html, content_type="text/html"),
            )
            page.goto(url)
            handle = page.evaluate_handle(_PICK_ACCOUNT_JS, args)
            status = handle.get_property("0").json_value()
            count = handle.get_property("2").json_value()
            tile = handle.get_property("1").as_element()
            return status, count, (tile.get_attribute("data-test-id") if tile else None)
        finally:
            browser.close()


def test_the_one_tile_for_the_account_is_found_whatever_the_case() -> None:
    assert _pick(_PICKER, "ada@example.com") == ("ok", 1, "ada@example.com")
    assert _pick(_PICKER, "ADA@Example.com") == ("ok", 1, "ada@example.com")


@pytest.mark.parametrize("email", ["carol@example.com", "ad@example.com", "a@example.com"])
def test_another_or_partial_address_matches_no_tile(email: str) -> None:
    assert _pick(_PICKER, email)[:2] == ("none", 0)


def test_a_longer_address_that_contains_the_account_is_not_a_match() -> None:
    page = '<div role=button data-test-id="xada@example.com">xada@example.com</div>'

    assert _pick(page, "ada@example.com")[:2] == ("none", 0)


def test_two_separate_tiles_for_the_same_account_are_ambiguous() -> None:
    page = _PICKER + '<div role=button aria-label="Sign in as ada@example.com">second</div>'

    assert _pick(page, "ada@example.com")[:2] == ("ambiguous", 2)


def test_a_tile_and_the_clickable_inside_it_count_once() -> None:
    page = '<div role=button><a href="#">ada@example.com</a></div>'

    assert _pick(page, "ada@example.com")[:2] == ("ok", 1)


@pytest.mark.parametrize(
    ("url", "policy"),
    [
        ("https://other.example.org/", {}),
        ("http://login.example.net/", {}),
        ("https://login.example.net/", {"blocked": ["login.example.net"]}),
        ("https://sub.login.example.net/", {}),
        ("https://login.example.net./", {"blocked": ["login.example.net"]}),
    ],
)
def test_the_wrong_origin_is_refused_whatever_the_page_shows(
    url: str, policy: dict[str, object]
) -> None:
    assert _pick(_PICKER, "ada@example.com", url=url, **policy)[:2] == ("wrong_origin", 0)


def test_a_wildcard_sign_in_host_matches_a_subdomain_only() -> None:
    policy = {"hosts": ["*.corp.example"]}

    assert _pick(_PICKER, "ada@example.com", url="https://sso.corp.example/", **policy)[0] == "ok"
    assert _pick(_PICKER, "ada@example.com", url="https://corp.example/", **policy)[0] == (
        "wrong_origin"
    )


@pytest.mark.parametrize(
    "field",
    ["<input type=password>", "<input type=text>", "<input type=email>", "<input type=tel>"],
)
def test_a_page_with_a_field_to_type_into_is_not_a_picker(field: str) -> None:
    assert _pick(_PICKER + field, "ada@example.com")[:2] == ("unsupported", 1)


def test_hidden_inputs_and_buttons_do_not_make_a_page_unsupported() -> None:
    page = _PICKER + '<input type=hidden name=ctx value=x><input type=submit value="Next">'

    assert _pick(page, "ada@example.com")[0] == "ok"


def test_the_tile_really_is_clicked_by_the_driver_path() -> None:
    from playwright.sync_api import sync_playwright

    from blumkin.browser.discovery import resolve_browser

    page_html = _PICKER.replace(
        '<div role=button data-test-id="ada@example.com">',
        '<div role=button data-test-id="ada@example.com" onclick="document.title=\'picked\'">',
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(resolve_browser(None).path), headless=True, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page()
            page.route(
                "https://**/*", lambda r: r.fulfill(body=page_html, content_type="text/html")
            )
            page.goto("https://login.example.net/")
            handle = page.evaluate_handle(
                _PICK_ACCOUNT_JS,
                {"blocked": [], "email": "ada@example.com", "hosts": ["login.example.net"]},
            )
            tile = handle.get_property("1").as_element()
            assert tile is not None
            tile.click()

            assert page.title() == "picked"
        finally:
            browser.close()


def test_a_container_that_wraps_other_accounts_is_never_the_tile() -> None:
    page = (
        '<div role=button data-test-id="wrapper">'
        '<div role=button data-test-id="ada@example.com">ada@example.com</div>'
        '<div role=button data-test-id="bob@example.com">bob@example.com</div>'
        "</div>"
    )

    assert _pick(page, "ada@example.com") == ("ok", 1, "ada@example.com")


def test_a_wrapper_around_one_tile_and_a_neighbour_without_an_address_picks_the_tile() -> None:
    page = (
        "<div role=button data-test-id=outer>"
        '<div role=button data-test-id="ada@example.com">ada@example.com</div>'
        "<div role=button>Use another account</div></div>"
    )

    assert _pick(page, "ada@example.com") == ("ok", 1, "ada@example.com")


@pytest.mark.parametrize(
    "label",
    [
        "Remove ada@example.com",
        "Sign out ada@example.com",
        "Forget this account ada@example.com",
        "Allow access as ada@example.com",
        "Delete ada@example.com",
    ],
)
def test_an_element_that_names_the_address_but_does_something_else_is_not_a_tile(
    label: str,
) -> None:
    page = f"<button>{label}</button>"

    assert _pick(page, "ada@example.com")[:2] == ("none", 0)


def test_the_real_tile_wins_over_a_remove_button_for_the_same_address() -> None:
    page = (
        '<div role=button data-test-id="ada@example.com">ada@example.com</div>'
        "<button>Remove ada@example.com</button>"
    )

    assert _pick(page, "ada@example.com") == ("ok", 1, "ada@example.com")


def test_a_tile_that_links_off_the_sign_in_hosts_is_not_clicked() -> None:
    page = '<a href="https://evil.example.org/x" data-test-id="ada@example.com">ada@example.com</a>'

    assert _pick(page, "ada@example.com")[:2] == ("none", 0)


def test_a_tile_that_links_within_the_sign_in_host_is_found() -> None:
    page = '<a href="/pick?u=1" data-test-id="ada@example.com">ada@example.com</a>'

    assert _pick(page, "ada@example.com")[0] == "ok"


@pytest.mark.parametrize(
    ("shown", "profile"),
    [
        ("o'neil@corp.example", "neil@corp.example"),
        ("neil@corp.example.au", "neil@corp.example"),
        ("a!neil@corp.example", "neil@corp.example"),
        ("neil@corp.example-x.com", "neil@corp.example"),
    ],
)
def test_a_longer_address_that_contains_the_profile_address_is_not_a_match(
    shown: str, profile: str
) -> None:
    page = f'<div role=button data-test-id="{shown}">{shown}</div>'

    assert _pick(page, profile)[:2] == ("none", 0)


def test_the_address_may_end_a_sentence_or_sit_in_brackets() -> None:
    assert _pick("<button>Signed in as ada@example.com.</button>", "ada@example.com")[0] == "ok"
    assert _pick("<button>Ada (ada@example.com)</button>", "ada@example.com")[0] == "ok"


def test_a_unicode_lookalike_does_not_match_ascii_case_folding() -> None:
    # The Kelvin sign lower-cases to `k` under full Unicode rules; only ASCII is folded here.
    page = '<div role=button data-test-id="ada\u212a@example.com">ada\u212a@example.com</div>'

    assert _pick(page, "adak@example.com")[:2] == ("none", 0)


def test_an_input_hidden_by_css_does_not_make_the_page_unsupported() -> None:
    page = _PICKER + '<input type=text style="display:none">'

    assert _pick(page, "ada@example.com")[0] == "ok"


@pytest.mark.parametrize("name", ["éada@example.com", "ada@example.comé", "ada@example.coḿ"])
def test_a_unicode_neighbour_makes_a_different_address(name: str) -> None:
    page = f'<div role=button data-test-id="x">{name}</div>'

    assert _pick(page, "ada@example.com")[:2] == ("none", 0)


@pytest.mark.parametrize("name", ["Send code to ada@example.com", "Confirm ada@example.com"])
def test_a_button_that_does_something_else_with_the_address_is_not_a_tile(name: str) -> None:
    assert _pick(f"<button>{name}</button>", "ada@example.com")[:2] == ("none", 0)


def test_a_marked_child_of_a_rejected_button_is_not_a_way_around_it() -> None:
    page = (
        '<button>Allow access <span data-test-id="ada@example.com">ada@example.com</span></button>'
    )

    assert _pick(page, "ada@example.com")[:2] == ("none", 0)
    off_host = (
        '<a href="https://evil.example.org/"><span data-test-id="ada@example.com">x</span></a>'
    )
    assert _pick(off_host, "ada@example.com")[:2] == ("none", 0)


def test_a_label_reference_naming_a_forbidden_action_vetoes_the_tile() -> None:
    page = (
        "<span id=l>Remove ada@example.com</span>"
        '<button aria-labelledby=l data-test-id="ada@example.com">ada@example.com</button>'
    )

    assert _pick(page, "ada@example.com")[:2] == ("none", 0)


@pytest.mark.parametrize(
    "field",
    ["<textarea></textarea>", "<div contenteditable=true>x</div>", "<div role=textbox>x</div>"],
)
def test_any_visible_field_to_type_into_is_unsupported(field: str) -> None:
    page = _PICKER + field

    assert _pick(page, "ada@example.com")[0] == "unsupported"
