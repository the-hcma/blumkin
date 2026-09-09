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

from blumkin.skills.mail import mail_draft, render_markdown_email, resolve_mail_body


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


def test_resolve_mail_body_markdown_is_the_default() -> None:
    content, label, graph_type = resolve_mail_body(body="one\n\ntwo")
    assert label == "html"
    assert graph_type == BodyType.Html
    assert content == "<p>one</p><p>two</p>"


def test_resolve_mail_body_text_still_passes_through() -> None:
    content, label, graph_type = resolve_mail_body(body="one\ntwo", body_type="text")
    assert (content, label, graph_type) == ("one\ntwo", "text", BodyType.Text)


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=SimpleNamespace(id="d", subject="S"))
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
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
