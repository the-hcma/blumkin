"""`--body-type markdown` (the compose default) renders to an HTML body.

A plain-text body used to reach Gmail / Outlook as one collapsed line; the
default now renders Markdown to HTML so the message keeps its structure.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgraph.generated.models.body_type import BodyType

from blumkin.config import PreferencesConfig
from blumkin.providers.microsoft import MicrosoftWorkspaceProvider
from blumkin.skills.mail import (
    mail_draft,
    render_markdown_email,
    render_plain_text_email,
    resolve_mail_body,
)


def test_render_markdown_email_covers_the_common_constructs() -> None:
    html = render_markdown_email(
        "# Title\n\n"
        "Lead para with **bold** and a [link](https://example.com).\n\n"
        "1. first\n2. second\n\n"
        "- a\n- b\n"
    )
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert '<a href="https://example.com">link</a>' in html
    assert "<ol><li>first</li><li>second</li></ol>" in html
    assert "<ul><li>a</li><li>b</li></ul>" in html
    assert "\n" not in html  # single fragment, no stray newlines


def test_render_markdown_email_keeps_single_newlines_as_breaks() -> None:
    # A hand-typed multi-line note: the old --body-type text default turned each
    # \n into <br>; the markdown default must not silently reflow it to one line.
    html = render_markdown_email("Thanks!\nWill review by Friday.\n\nSecond para.")
    assert html == "<p>Thanks!<br>Will review by Friday.</p><p>Second para.</p>"


def test_render_markdown_email_nests_a_sublist_inside_its_parent_li() -> None:
    html = render_markdown_email("- a\n  - b\n- c")
    assert html == "<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>"


def test_render_markdown_email_escapes_html_and_unsafe_links() -> None:
    out = render_markdown_email("a <script> tag & a [click](javascript:evil) link")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    assert "javascript:" not in out  # unsafe scheme dropped; link renders as plain text
    assert "<a " not in out
    assert "click" in out


def test_render_plain_text_email_reflows_hard_wrapped_paragraphs() -> None:
    # Issue #304: a fixed-width word-wrapped plain-text body - a single newline
    # inside a paragraph is whitespace to fold away, not a line break to keep,
    # unlike render_markdown_email's hard_breaks=True.
    html = render_plain_text_email("Wanted to flag\nwhat is still open on my list.\n\nSecond para.")
    assert html == "<p>Wanted to flag what is still open on my list.</p><p>Second para.</p>"


def test_render_plain_text_email_does_not_interpret_markdown() -> None:
    # --body-type text is "send exactly what I typed": unlike render_markdown_email,
    # **bold**-looking syntax stays literal, only HTML-escaped.
    html = render_plain_text_email("a <script> tag & **not bold**")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html
    assert "<strong>" not in html
    assert "**not bold**" in html


def test_resolve_mail_body_markdown_is_the_default() -> None:
    content, label, graph_type = resolve_mail_body(body="one\n\ntwo")
    assert label == "html"
    assert graph_type == BodyType.Html
    assert content == "<p>one</p><p>two</p>"


def test_resolve_mail_body_text_still_passes_through() -> None:
    content, label, graph_type = resolve_mail_body(body="one\ntwo", body_type="text")
    assert (content, label, graph_type) == ("one\ntwo", "text", BodyType.Text)


def test_resolve_mail_body_omitted_type_follows_config_html_email_false() -> None:
    config = SimpleNamespace(preferences=PreferencesConfig(html_email=False))
    content, label, graph_type = resolve_mail_body(body="one\ntwo", config=config)  # type: ignore[arg-type]
    assert (content, label, graph_type) == ("one\ntwo", "text", BodyType.Text)


def test_resolve_mail_body_omitted_type_follows_config_html_email_true() -> None:
    config = SimpleNamespace(preferences=PreferencesConfig(html_email=True))
    content, label, graph_type = resolve_mail_body(body="one\n\ntwo", config=config)  # type: ignore[arg-type]
    assert (content, label, graph_type) == ("<p>one</p><p>two</p>", "html", BodyType.Html)


def test_resolve_mail_body_wraps_the_markdown_default_in_the_configured_font() -> None:
    """The wrap must also apply on the markdown default branch, not only --body-type html."""
    config = SimpleNamespace(preferences=PreferencesConfig(font_name="Calibri", font_size=11))
    content, label, graph_type = resolve_mail_body(body="hi", config=config)  # type: ignore[arg-type]
    assert (label, graph_type) == ("html", BodyType.Html)
    assert content == '<div style="font-family:Calibri;font-size:11pt"><p>hi</p></div>'


def test_resolve_mail_body_explicit_flag_beats_config_html_email_false() -> None:
    """An explicit --body-type always wins over preferences.html_email = false."""
    config = SimpleNamespace(preferences=PreferencesConfig(html_email=False))
    content, label, graph_type = resolve_mail_body(
        body="one\n\ntwo",
        body_type="markdown",
        config=config,  # type: ignore[arg-type]
    )
    assert (content, label, graph_type) == ("<p>one</p><p>two</p>", "html", BodyType.Html)


def test_resolve_mail_body_wraps_html_in_the_configured_font() -> None:
    config = SimpleNamespace(preferences=PreferencesConfig(font_name="Calibri", font_size=11))
    content, _label, _graph_type = resolve_mail_body(
        body="hi",
        body_type="html",
        config=config,  # type: ignore[arg-type]
    )
    assert content == '<div style="font-family:Calibri;font-size:11pt">hi</div>'


def test_resolve_mail_body_does_not_wrap_text_in_a_font_style() -> None:
    config = SimpleNamespace(preferences=PreferencesConfig(font_name="Calibri"))
    content, _label, _graph_type = resolve_mail_body(
        body="hi",
        body_type="text",
        config=config,  # type: ignore[arg-type]
    )
    assert content == "hi"


def test_resolve_mail_body_no_font_preference_leaves_html_unwrapped() -> None:
    config = SimpleNamespace(preferences=PreferencesConfig())
    content, _label, _graph_type = resolve_mail_body(
        body="hi",
        body_type="html",
        config=config,  # type: ignore[arg-type]
    )
    assert content == "hi"


def _client(monkeypatch, *, preferences: PreferencesConfig | None = None) -> MagicMock:
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=SimpleNamespace(id="d", subject="S"))
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    config = SimpleNamespace(client_id="x", default_tz="UTC", preferences=preferences)
    monkeypatch.setattr("blumkin.skills.mail.load_config", lambda: config)
    return client


def test_mail_draft_default_sends_markdown_as_html(monkeypatch) -> None:
    client = _client(monkeypatch)
    asyncio.run(mail_draft(to="a@b.com", subject="Asks", body="Two things:\n\n1. cliff\n2. window"))
    body = client.me.messages.post.await_args.args[0].body
    assert body.content_type == BodyType.Html
    assert body.content == "<p>Two things:</p><ol><li>cliff</li><li>window</li></ol>"


def test_mail_draft_rejects_an_unknown_body_type(monkeypatch) -> None:
    _client(monkeypatch)
    with pytest.raises(ValueError, match="--body-type"):
        asyncio.run(mail_draft(to="a@b.com", subject="S", body="hi", body_type="richtext"))


def test_mail_draft_omitted_type_sends_text_when_config_disables_html_email(monkeypatch) -> None:
    client = _client(monkeypatch, preferences=PreferencesConfig(html_email=False))
    asyncio.run(mail_draft(to="a@b.com", subject="Asks", body="plain please"))
    body = client.me.messages.post.await_args.args[0].body
    assert body.content_type == BodyType.Text
    assert body.content == "plain please"


def test_provider_wrapper_body_type_none_still_respects_config(monkeypatch) -> None:
    """dispatch drops a --body-type omitted on the CLI, calling the provider wrapper
    with no body_type kwarg at all - its own default must not shadow config, the
    way the CLI/MCP path (not the skill function called directly) actually runs."""
    client = _client(monkeypatch, preferences=PreferencesConfig(html_email=False))
    config = SimpleNamespace(
        client_id="x", default_tz="UTC", preferences=PreferencesConfig(html_email=False)
    )
    provider = MicrosoftWorkspaceProvider(config)  # type: ignore[arg-type]
    asyncio.run(provider.mail_draft(to="a@b.com", subject="Asks", body="plain please"))
    body = client.me.messages.post.await_args.args[0].body
    assert body.content_type == BodyType.Text
