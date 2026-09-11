"""Mail signature config parsing and body append."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from blumkin.config import BlumkinConfig, MailSignatureConfig, load_config
from blumkin.mail_signature_state import record_signature_state
from blumkin.providers.kind import ProviderKind
from blumkin.skills.mail import append_mail_signature, mail_draft, render_mail_signature


def test_mail_signature_defaults_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "abc"\n')
    cfg = load_config()
    assert cfg.mail_signature.enabled is False
    assert cfg.mail_signature.name == ""


def test_mail_signature_parses_nested_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[profiles.default]",
                'client_id = "abc"',
                "[profiles.default.mail.signature]",
                "enabled = true",
                'name = "Ada Example"',
                'affiliation = "Example Org"',
                'title = "Example Title"',
                'name_color = "#112233"',
                'title_color = "#445566"',
                "",
            ]
        )
    )
    cfg = load_config()
    assert cfg.mail_signature.enabled is True
    assert cfg.mail_signature.name == "Ada Example"
    assert cfg.mail_signature.affiliation == "Example Org"
    assert cfg.mail_signature.title == "Example Title"
    assert cfg.mail_signature.name_color == "#112233"
    assert cfg.mail_signature.title_color == "#445566"


def test_render_mail_signature_text() -> None:
    sig = MailSignatureConfig(enabled=True, name="Ada", title="Engineer", affiliation="Example Org")
    assert render_mail_signature(sig, body_type="text") == "Ada\nEngineer\nExample Org"


def test_render_mail_signature_html_escapes() -> None:
    sig = MailSignatureConfig(enabled=True, name="A <B>", title='T "x"', affiliation="Org")
    html = render_mail_signature(sig, body_type="html")
    assert "A &lt;B&gt;" in html
    # Python 3.14+ html.escape defaults to quote=True (text + attribute-safe).
    assert "T &quot;x&quot;" in html
    assert "Org" in html


def test_render_mail_signature_html_template_overrides() -> None:
    sig = MailSignatureConfig(
        enabled=True, name="Ada", html_template="<p>Custom</p>", title="ignored"
    )
    assert render_mail_signature(sig, body_type="html") == "<p>Custom</p>"


def test_append_mail_signature_html_separator_and_empty_body() -> None:
    cfg = BlumkinConfig(
        client_id="x",
        config_dir=Path("/tmp"),
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(enabled=True, name="Ada"),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="t",
        wo1162425_scopes=False,
    )
    assert append_mail_signature("", body_type="html", config=cfg) == (
        '<span style="color:#003366;font-weight:bold">Ada</span>'
    )
    assert append_mail_signature("<p>Hi</p>", body_type="html", config=cfg) == (
        '<p>Hi</p><br><br><span style="color:#003366;font-weight:bold">Ada</span>'
    )


def test_append_mail_signature_respects_opt_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\n'
        '[profiles.default.mail.signature]\nenabled = true\nname = "Ada"\n'
    )
    cfg = load_config()
    assert append_mail_signature("Hello", body_type="text", config=cfg) == "Hello\n\nAda"
    assert (
        append_mail_signature("Hello", body_type="text", config=cfg, no_signature=True) == "Hello"
    )


def test_append_mail_signature_disabled_is_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\n[profiles.default.mail.signature]\nname = "Ada"\n'
    )
    cfg = load_config()
    assert cfg.mail_signature.enabled is False
    assert append_mail_signature("Hello", body_type="text", config=cfg) == "Hello"


def test_append_mail_signature_stands_down_when_outlook_auto_signs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\n'
        '[profiles.default.mail.signature]\nenabled = true\nname = "Ada"\n'
    )
    cfg = load_config()
    assert append_mail_signature("Hello", body_type="text", config=cfg) == "Hello\n\nAda"
    record_signature_state(cfg, detected=True)
    assert append_mail_signature("Hello", body_type="text", config=cfg) == "Hello"
    # A negative probe result does not suppress.
    record_signature_state(cfg, detected=False)
    assert append_mail_signature("Hello", body_type="text", config=cfg) == "Hello\n\nAda"


def test_mail_draft_appends_signature_and_respects_opt_out(monkeypatch) -> None:
    draft = SimpleNamespace(id="draft-1", subject="Hi")
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=draft)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    cfg = BlumkinConfig(
        client_id="x",
        config_dir=Path("/tmp"),
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=None,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(enabled=True, name="Ada"),
        profile="default",
        provider=ProviderKind.MICROSOFT,
        tags=(),
        tenant_id="t",
        wo1162425_scopes=False,
    )

    asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello", body_type="text", config=cfg))
    posted = client.me.messages.post.await_args
    assert posted is not None
    # A text body is sent to Graph with CRLF so Outlook keeps the line breaks.
    assert posted.args[0].body.content == "Hello\r\n\r\nAda"

    asyncio.run(
        mail_draft(
            to="a@b.com",
            subject="Hi",
            body="Hello",
            body_type="text",
            config=cfg,
            no_signature=True,
        )
    )
    posted = client.me.messages.post.await_args
    assert posted is not None
    assert posted.args[0].body.content == "Hello"

    asyncio.run(
        mail_draft(
            to="a@b.com",
            subject="Hi",
            body="<p>Hi</p>",
            body_type="html",
            config=cfg,
        )
    )
    posted = client.me.messages.post.await_args
    assert posted is not None
    html_body = posted.args[0].body.content
    assert html_body.startswith("<p>Hi</p><br><br>")
    assert "Ada" in html_body
