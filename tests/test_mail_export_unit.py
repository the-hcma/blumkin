"""Render a mail body to a verified PDF plus text and JSON sidecars (issue #583)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from blumkin.mail_export import (
    MailExportError,
    check_pdf,
    count_loadable_images,
    export_message,
    html_to_text,
    read_bound_text,
    render_pdf_command,
    sanitize_html,
    strip_tracking_pixels,
)

_PIXEL = '<img src="https://t.example/p.gif" width="1" height="1">'
_LOGO = '<img src="https://cdn.example/logo.png" width="120">'
_HTML = f"<html><body><h1>Ride receipt</h1><p>Total $18.50</p>{_LOGO}{_PIXEL}</body></html>"


class _Provider:
    def __init__(self, body: str = _HTML, body_type: str = "html") -> None:
        self.body = body
        self.body_type = body_type
        self.asked: list[str] = []

    async def mail_get(self, *, message_id: str, body_type: str = "text") -> dict[str, Any]:
        self.asked.append(body_type)
        return {
            "message": {
                "body": self.body,
                "body_type": self.body_type,
                "from_email": "receipts@ride.example",
                "id": message_id,
                "injection_warning": None,
                "received": "2026-10-23T09:15:00Z",
                "subject": "Your Tuesday ride / receipt",
            }
        }


def _fake_renderer(text_layer: str, *, images: int = 1, pages: int = 1):
    def render(html_path: Path, pdf_path: Path) -> None:
        pdf_path.write_bytes(b"%PDF-fake")

    def read(pdf_path: Path) -> dict[str, Any]:
        return {"images": images, "page_texts": [text_layer, *([""] * (pages - 1))]}

    return render, read


def test_strip_tracking_pixels_drops_one_pixel_images_and_keeps_real_ones() -> None:
    cleaned, removed = strip_tracking_pixels(_HTML)

    assert removed == 1
    assert "p.gif" not in cleaned
    assert "logo.png" in cleaned


def test_strip_tracking_pixels_handles_style_sized_pixels() -> None:
    html = '<img src="https://t.example/x" style="width:1px;height:1px">'

    assert strip_tracking_pixels(html) == ("", 1)


def test_html_to_text_keeps_line_structure_and_decodes_entities() -> None:
    text = html_to_text("<p>Fare&nbsp;&amp; tip</p><p>Total<br>$18.50</p><style>x{}</style>")

    assert text.splitlines() == ["Fare & tip", "Total", "$18.50"]


def test_render_command_keeps_images_on_and_isolates_the_profile(tmp_path: Path) -> None:
    command = render_pdf_command(
        Path("/usr/bin/chrome"), tmp_path / "a.html", tmp_path / "a.pdf", tmp_path / "profile"
    )

    assert "--headless=new" in command
    assert any(arg.startswith("--virtual-time-budget=") for arg in command)
    assert any(arg.startswith("--user-data-dir=") for arg in command)
    assert not any("blink-settings=imagesEnabled=false" in arg for arg in command)
    assert any(arg.startswith("--print-to-pdf=") for arg in command)


def test_export_writes_pdf_text_and_json_side_by_side(tmp_path: Path) -> None:
    render, read = _fake_renderer("Ride receipt\nTotal $18.50")
    out = tmp_path / "mail-out"

    payload = asyncio.run(
        export_message(
            _Provider(), message_id="AAMk1", out_dir=out, renderer=render, pdf_reader=read
        )
    )

    pdf = Path(payload["pdf"])
    txt = Path(payload["text"])
    sidecar = Path(payload["json"])
    assert pdf.parent == txt.parent == sidecar.parent == out.resolve()
    assert pdf.stem == txt.stem == sidecar.stem == "2026-10-23_your-tuesday-ride-receipt"
    assert txt.read_text().splitlines() == ["Ride receipt", "Total $18.50"]
    meta = json.loads(sidecar.read_text())
    assert meta["message_id"] == "AAMk1"
    assert meta["from"] == "receipts@ride.example"
    assert meta["received"] == "2026-10-23T09:15:00Z"
    assert meta["subject"] == "Your Tuesday ride / receipt"
    assert meta["expected_images"] == 1
    assert meta["tracking_pixels_removed"] == 1
    assert payload["check"]["ok"] is True
    assert oct(pdf.stat().st_mode & 0o777) == "0o600"


def test_export_fails_loudly_when_the_images_are_missing(tmp_path: Path) -> None:
    render, read = _fake_renderer("Ride receipt\nTotal $18.50", images=0)

    payload = asyncio.run(
        export_message(
            _Provider(), message_id="m", out_dir=tmp_path, renderer=render, pdf_reader=read
        )
    )

    assert payload["check"]["ok"] is False
    assert any("image" in problem for problem in payload["check"]["problems"])
    assert payload["pdf"] is None
    assert list(tmp_path.iterdir()) == []


def test_a_failed_check_leaves_nothing_so_the_same_name_can_be_retried(tmp_path: Path) -> None:
    bad_render, bad_read = _fake_renderer("Ride receipt Total $18.50", images=0)
    good_render, good_read = _fake_renderer("Ride receipt Total $18.50", images=1)
    kwargs: dict[str, Any] = {"message_id": "m", "name": "ride", "out_dir": tmp_path}

    first = asyncio.run(
        export_message(_Provider(), renderer=bad_render, pdf_reader=bad_read, **kwargs)
    )
    second = asyncio.run(
        export_message(_Provider(), renderer=good_render, pdf_reader=good_read, **kwargs)
    )

    assert first["check"]["ok"] is False
    assert second["check"]["ok"] is True
    assert sorted(path.name for path in tmp_path.iterdir()) == ["ride.json", "ride.pdf", "ride.txt"]


def test_the_rendered_page_carries_a_policy_and_no_scripts_frames_or_links(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def render(html_path: Path, pdf_path: Path) -> None:
        seen.append(html_path.read_text())
        pdf_path.write_bytes(b"%PDF")

    body = (
        "<html><head><link rel=stylesheet href=https://x.example/a.css></head><body>"
        "<script>fetch('https://x.example')</script>"
        "<iframe src='file:///etc/passwd'></iframe><p>Total 5.00</p></body></html>"
    )
    _, read = _fake_renderer("Total 5.00", images=0)

    asyncio.run(
        export_message(
            _Provider(body=body), message_id="m", out_dir=tmp_path, renderer=render, pdf_reader=read
        )
    )

    page = seen[0]
    assert page.index("Content-Security-Policy") < page.index("Total 5.00")
    assert "default-src 'none'" in page
    for forbidden in ("<script", "<iframe", "<link", "passwd"):
        assert forbidden not in page


def test_images_in_comments_and_cid_parts_are_not_expected_in_the_pdf() -> None:
    html = (
        "<!--[if mso]><img src='https://x.example/mso.png'><![endif]-->"
        "<img src='cid:logo@123'><img src='https://x.example/real.png' width='80'>"
    )

    assert count_loadable_images(sanitize_html(html)) == 1


def test_an_image_only_page_is_not_blank(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")

    def read(_: Path) -> dict[str, Any]:
        return {"images": 1, "page_images": [0, 1], "page_texts": ["Total 5.00", ""]}

    result = check_pdf(pdf, text_path=None, expected_images=1, pdf_reader=read)

    assert result["blank_pages"] == []
    assert result["ok"] is True


def test_a_text_file_that_is_not_utf8_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    txt = tmp_path / "r.txt"
    txt.write_bytes(b"\xff\xfe\x00bad")

    def read(_: Path) -> dict[str, Any]:
        return {"images": 0, "page_texts": ["x"]}

    result = check_pdf(pdf, text_path=txt, expected_images=0, pdf_reader=read)

    assert result["ok"] is False
    assert "text file could not be read" in result["problems"][0]


def test_export_refuses_to_overwrite_an_existing_export(tmp_path: Path) -> None:
    render, read = _fake_renderer("Ride receipt Total $18.50")
    kwargs: dict[str, Any] = {
        "message_id": "m",
        "out_dir": tmp_path,
        "pdf_reader": read,
        "renderer": render,
    }
    asyncio.run(export_message(_Provider(), **kwargs))

    with pytest.raises(MailExportError, match="already exists"):
        asyncio.run(export_message(_Provider(), **kwargs))


def test_export_wraps_a_plain_text_body_so_it_still_renders(tmp_path: Path) -> None:
    seen: list[str] = []

    def render(html_path: Path, pdf_path: Path) -> None:
        seen.append(html_path.read_text())
        pdf_path.write_bytes(b"%PDF")

    _, read = _fake_renderer("Total 5.00")

    asyncio.run(
        export_message(
            _Provider(body="Total 5.00 <b>", body_type="text"),
            message_id="m",
            out_dir=tmp_path,
            renderer=render,
            pdf_reader=read,
        )
    )

    assert "&lt;b&gt;" in seen[0]
    assert "<pre" in seen[0]


def test_export_carries_an_injection_warning_without_stopping(tmp_path: Path) -> None:
    provider = _Provider()
    original = provider.mail_get

    async def with_warning(**kwargs: Any) -> dict[str, Any]:
        result = await original(**kwargs)
        result["message"]["injection_warning"] = {"matched": True, "findings": []}
        return result

    provider.mail_get = with_warning  # type: ignore[method-assign]
    render, read = _fake_renderer("Ride receipt Total $18.50")

    payload = asyncio.run(
        export_message(provider, message_id="m", out_dir=tmp_path, renderer=render, pdf_reader=read)
    )

    assert payload["injection_warning"]["matched"] is True
    assert json.loads(Path(payload["json"]).read_text())["injection_warning"]["matched"] is True


def test_export_with_a_message_without_a_body_is_an_error(tmp_path: Path) -> None:
    render, read = _fake_renderer("")

    with pytest.raises(MailExportError, match="no body"):
        asyncio.run(
            export_message(
                _Provider(body=""),
                message_id="m",
                out_dir=tmp_path,
                renderer=render,
                pdf_reader=read,
            )
        )


def test_check_pdf_flags_blank_pages_and_missing_text(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    txt = tmp_path / "r.txt"
    txt.write_text("Ride receipt\nTotal $18.50\nThank you for riding\n")

    def read(_: Path) -> dict[str, Any]:
        return {"images": 0, "page_texts": ["Ride receipt", ""]}

    result = check_pdf(pdf, text_path=txt, expected_images=0, pdf_reader=read)

    assert result["ok"] is False
    assert result["pages"] == 2
    assert result["blank_pages"] == [2]
    assert any("text" in problem for problem in result["problems"])


def test_check_pdf_passes_when_the_text_layer_matches(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    txt = tmp_path / "r.txt"
    txt.write_text("Ride receipt\nTotal $18.50\n")

    def read(_: Path) -> dict[str, Any]:
        return {"images": 2, "page_texts": ["Ride receipt Total $18.50"]}

    result = check_pdf(pdf, text_path=txt, expected_images=2, pdf_reader=read)

    assert result == {
        "blank_pages": [],
        "images": 2,
        "ok": True,
        "pages": 1,
        "problems": [],
        "text_overlap": 1.0,
    }


def test_check_pdf_reports_an_unreadable_pdf_as_a_problem(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"not a pdf")

    def read(_: Path) -> dict[str, Any]:
        raise ValueError("cannot read")

    result = check_pdf(pdf, text_path=None, expected_images=None, pdf_reader=read)

    assert result["ok"] is False
    assert "cannot read" in result["problems"][0]


@pytest.mark.skipif(shutil.which("google-chrome") is None, reason="needs Chrome")
def test_real_chrome_renders_a_text_layer_pdf(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    out = tmp_path / "out"
    payload = asyncio.run(
        export_message(
            _Provider(body="<html><body><h1>Ride receipt</h1><p>Total $18.50</p></body></html>"),
            message_id="m",
            out_dir=out,
        )
    )

    assert payload["check"]["ok"], payload["check"]
    assert (
        subprocess.run(["head", "-c", "4", payload["pdf"]], capture_output=True).stdout == b"%PDF"
    )


def _cli_payload(ok: bool) -> dict[str, Any]:
    return {
        "check": {
            "blank_pages": [],
            "images": 1,
            "ok": ok,
            "pages": 1,
            "problems": [] if ok else ["blank pages: 1"],
            "text_overlap": 1.0,
        },
        "injection_warning": None,
        "json": "/o/r.json",
        "message_id": "m",
        "pdf": "/o/r.pdf",
        "text": "/o/r.txt",
        "tracking_pixels_removed": 1,
    }


@pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
def test_mail_export_command_exits_1_when_the_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ok: bool, code: int
) -> None:
    from click.testing import CliRunner

    from blumkin import cli

    async def fake_export(*_: Any, **__: Any) -> dict[str, Any]:
        return _cli_payload(ok)

    monkeypatch.setattr(cli, "export_message", fake_export)
    monkeypatch.setattr(cli, "_workspace", lambda *_: object())

    result = CliRunner().invoke(
        cli.main, ["mail", "export", "--id", "m", "--out", str(tmp_path), "--json"]
    )

    assert result.exit_code == code, result.output
    assert json.loads(result.output)["ok"] is ok


def test_mail_export_command_reports_an_export_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from blumkin import cli

    async def fake_export(*_: Any, **__: Any) -> dict[str, Any]:
        raise MailExportError("r.pdf already exists")

    monkeypatch.setattr(cli, "export_message", fake_export)
    monkeypatch.setattr(cli, "_workspace", lambda *_: object())

    result = CliRunner().invoke(cli.main, ["mail", "export", "--id", "m", "--out", str(tmp_path)])

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_export_records_hashes_that_bind_the_text_to_the_pdf(tmp_path: Path) -> None:
    render, read = _fake_renderer("Ride receipt Total $18.50")

    payload = asyncio.run(
        export_message(
            _Provider(), message_id="m", out_dir=tmp_path, renderer=render, pdf_reader=read
        )
    )

    pdf = Path(payload["pdf"])
    assert read_bound_text(pdf) == "Ride receipt\nTotal $18.50\n"
    pdf.with_suffix(".txt").write_text("TOTAL $999.00\n")
    assert read_bound_text(pdf) is None


def test_elements_hidden_by_an_inline_style_leave_neither_the_page_nor_the_text() -> None:
    html = (
        '<div style="display:none">TOTAL $999.00</div>'
        '<span style="mso-hide: all">preheader</span><p>Total $18.50</p>'
        '<p style="font-size:0.9em">kept</p>'
    )

    cleaned = sanitize_html(html)

    assert html_to_text(cleaned).splitlines() == ["Total $18.50", "kept"]


def test_the_configured_browser_is_the_one_the_renderer_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from blumkin import mail_export

    asked: list[str | None] = []

    def fake_resolve(preferred: str | None) -> Any:
        asked.append(preferred)
        raise MailExportError("stop here")

    monkeypatch.setattr(mail_export, "resolve_browser", fake_resolve)

    with pytest.raises(MailExportError):
        asyncio.run(export_message(_Provider(), message_id="m", out_dir=tmp_path, browser="edge"))

    assert asked == ["edge"]


def test_mail_export_human_output_for_a_failed_check_says_nothing_was_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from blumkin import cli

    failed = _cli_payload(False)
    failed.update(json=None, pdf=None, text=None)

    async def fake_export(*_: Any, **__: Any) -> dict[str, Any]:
        return failed

    monkeypatch.setattr(cli, "export_message", fake_export)
    monkeypatch.setattr(cli, "_workspace", lambda *_: object())

    result = CliRunner().invoke(cli.main, ["mail", "export", "--id", "m", "--out", str(tmp_path)])

    assert result.exit_code == 1
    assert "Nothing was written" in result.output
    assert "problem: blank pages: 1" in result.output
    assert "Traceback" not in result.output


def test_inline_tags_keep_the_text_adjacent_and_table_cells_are_separated() -> None:
    text = html_to_text(
        "<p>Total $<span>18.50</span></p><table><tr><td>Tip</td><td>2.00</td></tr></table>"
    )

    assert text.splitlines() == ["Total $18.50", "Tip 2.00"]


def test_a_nested_hidden_element_is_removed_with_everything_inside_it() -> None:
    html = (
        '<div style="display:none"><div>preheader</div><p>TOTAL $999.00</p></div>'
        "<p>Total $18.50</p>"
    )

    assert html_to_text(sanitize_html(html)).splitlines() == ["Total $18.50"]


def test_an_unclosed_hidden_element_does_not_swallow_the_rest_of_the_message() -> None:
    html = '<span style="display:none">x<p>Total $18.50</p>'

    assert "Total $18.50" in html_to_text(sanitize_html(html))


def test_a_zero_font_size_wrapper_does_not_hide_the_visible_amount_inside() -> None:
    html = '<div style="font-size:0"><span style="font-size:16px">TOTAL USD 18.50</span></div>'

    assert "TOTAL USD 18.50" in html_to_text(sanitize_html(html))


def test_a_meta_refresh_in_the_message_is_removed() -> None:
    html = '<meta http-equiv="refresh" content="0;url=https://evil.example/"><p>Total 5.00</p>'

    cleaned = sanitize_html(html)

    assert "refresh" not in cleaned
    assert "Total 5.00" in cleaned


def test_mail_export_reports_a_missing_browser_with_the_export_failed_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from blumkin import cli
    from blumkin.browser.discovery import BrowserNotFoundError

    async def fake_export(*_: Any, **__: Any) -> dict[str, Any]:
        raise BrowserNotFoundError("Expected Chrome to be installed, found none")

    monkeypatch.setattr(cli, "export_message", fake_export)
    monkeypatch.setattr(cli, "_workspace", lambda *_: object())

    result = CliRunner().invoke(
        cli.main, ["mail", "export", "--id", "m", "--out", str(tmp_path), "--json"]
    )

    assert result.exit_code == 1
    assert "export_failed" in result.output
    assert "graph_error" not in result.output


def test_a_pixel_removal_cannot_rebuild_a_meta_refresh(tmp_path: Path) -> None:
    broken = (
        '<me<img width="1" height="1" src="x">ta http-equiv="refresh" '
        'content="0;url=https://x.example/"><p>Total 5.00</p>'
    )
    seen: list[str] = []

    def render(html_path: Path, pdf_path: Path) -> None:
        seen.append(html_path.read_text())
        pdf_path.write_bytes(b"%PDF")

    _, read = _fake_renderer("Total 5.00", images=0)

    asyncio.run(
        export_message(
            _Provider(body=broken),
            message_id="m",
            out_dir=tmp_path,
            renderer=render,
            pdf_reader=read,
        )
    )

    assert "refresh" not in seen[0]


def test_an_image_hidden_by_an_inline_style_is_not_expected_in_the_pdf() -> None:
    html = '<img src="https://x.example/a.png" style="display:none"><img src="https://x.example/b.png">'

    assert count_loadable_images(sanitize_html(html)) == 1


def test_a_later_declaration_that_shows_the_element_wins() -> None:
    html = '<div style="display:none;display:block">TOTAL $18.50</div>'

    assert "TOTAL $18.50" in html_to_text(sanitize_html(html))


def test_an_important_hidden_declaration_beats_a_later_plain_one() -> None:
    html = '<div style="display:none !important;display:block">hidden</div><p>kept</p>'

    assert html_to_text(sanitize_html(html)).splitlines() == ["kept"]


def test_a_visible_child_inside_a_visibility_hidden_parent_is_kept() -> None:
    html = (
        '<div style="visibility:hidden"><span style="visibility:visible">Total $18.50</span></div>'
    )

    assert "Total $18.50" in html_to_text(sanitize_html(html))


def test_br_and_hr_with_attributes_still_break_lines() -> None:
    text = html_to_text('Receipt<hr class="x">Total<br clear="all">$18.50')

    assert text.splitlines() == ["Receipt", "Total", "$18.50"]


def test_non_latin_receipt_text_is_compared_too(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    txt = tmp_path / "r.txt"
    txt.write_text("المجموع ١٨٫٥٠ ريال\n")

    def read(_: Path) -> dict[str, Any]:
        return {"images": 0, "page_texts": ["something else entirely"]}

    result = check_pdf(pdf, text_path=txt, expected_images=0, pdf_reader=read)

    assert result["ok"] is False


def test_a_fifo_named_like_the_json_does_not_hang_the_metadata_read(tmp_path: Path) -> None:
    import os

    from blumkin.mail_export import read_export_meta

    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    os.mkfifo(pdf.with_suffix(".json"))

    assert read_export_meta(pdf)["message_id"] is None
