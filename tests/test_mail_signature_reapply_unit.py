"""Hermetic tests for `mail signature` and update-draft signature / quoted-thread handling."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from blumkin.cli import main
from blumkin.skills.mail import split_quoted_original

_OUTLOOK_REPLY = (
    "<html><body>my new text"
    '<hr tabindex="-1" style="display:inline-block; width:98%">'
    '<div id="divRplyFwdMsg" dir="ltr"><b>From:</b> Ada</div>'
    "<p>the original message</p></body></html>"
)


def test_split_quoted_original_keeps_the_outlook_separator_with_the_quote() -> None:
    head, quoted = split_quoted_original(_OUTLOOK_REPLY)
    assert head == "<html><body>my new text"
    # The <hr> belongs to the quote, so re-joining reproduces the original layout
    # instead of leaving a stray rule or doubling it.
    assert quoted.startswith("<hr")
    assert 'id="divRplyFwdMsg"' in quoted
    assert head + quoted == _OUTLOOK_REPLY


def test_split_quoted_original_handles_blockquote_and_plain_text() -> None:
    head, quoted = split_quoted_original("reply text<blockquote>old</blockquote>")
    assert head == "reply text"
    assert quoted == "<blockquote>old</blockquote>"

    head, quoted = split_quoted_original("reply\n-----Original Message-----\nold")
    assert head == "reply\n"
    assert quoted.startswith("-----Original Message-----")


def test_split_quoted_original_returns_empty_tail_when_unrecognised() -> None:
    assert split_quoted_original("just a body") == ("just a body", "")
    assert split_quoted_original("") == ("", "")


def _signature_config(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'client_id = "abc"\n'
        'tenant_id = "example.com"\n'
        'default_tz = "UTC"\n'
        "\n[mail.signature]\n"
        "enabled = true\n"
        'name = "Ada Lovelace"\n'
        'title = "Technical Fellow"\n'
    )


def test_mail_signature_renders_html_and_text(tmp_path: Path, monkeypatch) -> None:
    _signature_config(tmp_path)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))

    result = CliRunner().invoke(main, ["mail", "signature", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["enabled"] is True
    assert payload["body_type"] == "html"
    # HTML keeps the configured colours, which is the styling callers had to
    # hand-reconstruct from the source before this command existed.
    assert "Ada Lovelace" in payload["signature"]
    assert "color:" in payload["signature"]

    text = CliRunner().invoke(main, ["mail", "signature", "--body-type", "text", "--json"])
    rendered = json.loads(text.stdout)["signature"]
    assert "Ada Lovelace" in rendered
    assert "<span" not in rendered


def test_mail_signature_is_quiet_when_unconfigured(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text('client_id = "abc"\ntenant_id = "example.com"\n')
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["mail", "signature"])
    assert result.exit_code == 0
    assert "(no signature configured)" in result.stdout


def _update_draft_via_cli(tmp_path: Path, monkeypatch, args: list[str]) -> MagicMock:
    _signature_config(tmp_path)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    provider = MagicMock()

    async def _update(**kwargs):
        from blumkin.config import load_config
        from blumkin.skills.mail import mail_update_draft

        client = MagicMock()
        existing = MagicMock()
        existing.id = "d1"
        existing.is_draft = True
        existing.body.content = _OUTLOOK_REPLY
        existing.subject = "Re: Renewal"

        async def _get():
            return existing

        async def _patch(message):
            _patch.seen = message
            return existing

        client.me.messages.by_message_id.return_value.get = _get
        client.me.messages.by_message_id.return_value.patch = _patch
        with (
            patch("blumkin.skills.mail.create_graph_client", return_value=client),
            patch("blumkin.skills.mail._upload_attachments", return_value=[]),
        ):
            await mail_update_draft(config=load_config(), **kwargs)
        provider.sent_body = _patch.seen.body
        return {"draft": {}}

    provider.mail_update_draft.side_effect = _update
    with patch("blumkin.cli._workspace", return_value=provider):
        CliRunner().invoke(main, ["mail", "update-draft", *args])
    return provider


def test_update_draft_reapplies_the_signature_when_replacing_the_body(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _update_draft_via_cli(tmp_path, monkeypatch, ["--id", "d1", "--body", "fresh text"])
    assert "Ada Lovelace" in provider.sent_body.content


def test_update_draft_honours_no_signature(tmp_path: Path, monkeypatch) -> None:
    provider = _update_draft_via_cli(
        tmp_path, monkeypatch, ["--id", "d1", "--body", "fresh text", "--no-signature"]
    )
    assert "Ada Lovelace" not in provider.sent_body.content


def test_update_draft_keep_quoted_reattaches_the_thread_as_html(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _update_draft_via_cli(
        tmp_path, monkeypatch, ["--id", "d1", "--body", "fresh text", "--keep-quoted"]
    )
    content = provider.sent_body.content
    assert "fresh text" in content
    assert 'id="divRplyFwdMsg"' in content
    assert "<p>the original message</p>" in content
    # A plain-text half joined to a markup tail has to go out as HTML, or Graph
    # would render the quoted thread as literal angle brackets.
    assert provider.sent_body.content_type.name.lower() == "html"


def test_update_draft_without_keep_quoted_still_drops_the_thread(
    tmp_path: Path, monkeypatch
) -> None:
    provider = _update_draft_via_cli(tmp_path, monkeypatch, ["--id", "d1", "--body", "fresh text"])
    assert 'id="divRplyFwdMsg"' not in provider.sent_body.content
