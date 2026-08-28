"""Mail signature config parsing and body append."""

from __future__ import annotations

from pathlib import Path

from blumkin.config import BlumkinConfig, MailSignatureConfig, load_config
from blumkin.skills.mail import append_mail_signature, render_mail_signature


def test_mail_signature_defaults_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_CLIENT_ID", raising=False)
    (tmp_path / "config.toml").write_text('client_id = "abc"\n')
    cfg = load_config()
    assert cfg.mail_signature.enabled is False
    assert cfg.mail_signature.name == ""


def test_mail_signature_parses_nested_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BLUMKIN_CLIENT_ID", raising=False)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                'client_id = "abc"',
                "[mail.signature]",
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
        files_scopes=False,
        mail_signature=MailSignatureConfig(enabled=True, name="Ada"),
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
        'client_id = "abc"\n[mail.signature]\nenabled = true\nname = "Ada"\n'
    )
    cfg = load_config()
    assert append_mail_signature("Hello", body_type="text", config=cfg) == "Hello\n\nAda"
    assert (
        append_mail_signature("Hello", body_type="text", config=cfg, no_signature=True) == "Hello"
    )


def test_append_mail_signature_disabled_is_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('client_id = "abc"\n[mail.signature]\nname = "Ada"\n')
    cfg = load_config()
    assert cfg.mail_signature.enabled is False
    assert append_mail_signature("Hello", body_type="text", config=cfg) == "Hello"
