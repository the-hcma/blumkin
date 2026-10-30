"""Render one mail message to a verified PDF with text and JSON sidecars (issue #583).

Expense sites want a receipt as a PDF, but many receipts are HTML mail with no attachment.
`export_message` prints the HTML body with a headless browser (images on, so the logo and the
route map survive), writes `<name>.pdf`, `<name>.txt` and `<name>.json` side by side, and
checks the PDF against the message so a broken render fails loudly instead of being found by
a person opening the file.

Tracking pixels are dropped before rendering and the render runs in a throwaway browser
profile, so remote images are fetched without cookies or the mailbox identity.

Receipt contents are private: nothing here logs a body, a vendor or an amount.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from blumkin.attachments import write_new_private_file
from blumkin.browser.discovery import resolve_browser

#: Share of the `.txt` words that must show up in the PDF text layer.
MIN_TEXT_OVERLAP = 0.8
#: How long the browser may spend loading images and settling, in virtual milliseconds.
RENDER_TIME_BUDGET_MS = 15000
RENDER_TIMEOUT_SECONDS = 90

PdfReader = Callable[[Path], dict[str, Any]]
Renderer = Callable[[Path, Path], None]


class MailExportError(RuntimeError):
    """The message could not be exported (no body, name taken, render failed)."""


def check_pdf(
    pdf_path: Path,
    *,
    text_path: Path | None,
    expected_images: int | None,
    pdf_reader: PdfReader | None = None,
) -> dict[str, Any]:
    """Compare a rendered PDF with its text sidecar and the images the message carried."""
    reader = pdf_reader or read_pdf_layers
    try:
        layers = reader(pdf_path)
    except Exception as exc:  # noqa: BLE001 - PDF libraries raise many types; a bad PDF is a finding
        return {
            "blank_pages": [],
            "images": 0,
            "ok": False,
            "pages": 0,
            "problems": [f"PDF could not be read ({type(exc).__name__}: {exc})"],
            "text_overlap": None,
        }
    page_texts: list[str] = list(layers.get("page_texts") or [])
    images = int(layers.get("images") or 0)
    problems: list[str] = []
    blank = [index + 1 for index, text in enumerate(page_texts) if not text.strip()]
    if not page_texts:
        problems.append("PDF has no pages")
    if blank:
        problems.append(f"blank pages: {', '.join(str(number) for number in blank)}")
    if expected_images is not None and images < expected_images:
        problems.append(f"expected {expected_images} images in the PDF, found {images}")
    overlap: float | None = None
    if text_path is not None:
        wanted = _words(text_path.read_text(encoding="utf-8"))
        if wanted:
            have = _words(" ".join(page_texts))
            overlap = round(len(wanted & have) / len(wanted), 2)
            if overlap < MIN_TEXT_OVERLAP:
                problems.append(
                    f"PDF text layer matches only {overlap:.0%} of the text file "
                    f"(expected at least {MIN_TEXT_OVERLAP:.0%})"
                )
    return {
        "blank_pages": blank,
        "images": images,
        "ok": not problems,
        "pages": len(page_texts),
        "problems": problems,
        "text_overlap": overlap,
    }


def check_exported_pdf(pdf_path: Path, *, pdf_reader: PdfReader | None = None) -> dict[str, Any]:
    """`check_pdf` for an export: the `.txt` and `.json` beside the PDF supply the rest."""
    text_path = pdf_path.with_suffix(".txt")
    expected: int | None = None
    try:
        expected = int(json.loads(pdf_path.with_suffix(".json").read_text())["expected_images"])
    except OSError, ValueError, KeyError, TypeError:
        expected = None
    return check_pdf(
        pdf_path,
        text_path=text_path if text_path.is_file() else None,
        expected_images=expected,
        pdf_reader=pdf_reader,
    )


async def export_message(
    provider: Any,
    *,
    message_id: str,
    out_dir: Path,
    name: str | None = None,
    pdf_reader: PdfReader | None = None,
    renderer: Renderer | None = None,
) -> dict[str, Any]:
    """Export one message as `<name>.pdf`, `<name>.txt` and `<name>.json` in `out_dir`."""
    if not message_id.strip():
        raise ValueError("--id is required")
    fetched = await provider.mail_get(message_id=message_id.strip(), body_type="html")
    message = fetched["message"]
    body = str(message.get("body") or "")
    if not body.strip():
        raise MailExportError(f"message {message_id.strip()} has no body to render")
    if message.get("body_type") == "text":
        body = (
            f'<pre style="white-space:pre-wrap;font-family:monospace">{html_lib.escape(body)}</pre>'
        )
    cleaned, removed = strip_tracking_pixels(body)
    document = _as_document(cleaned)
    stem = _safe_stem(name) if name else _default_stem(message)
    folder = out_dir.expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve()
    targets = {suffix: folder / f"{stem}.{suffix}" for suffix in ("pdf", "txt", "json")}
    taken = [path.name for path in targets.values() if path.exists() or path.is_symlink()]
    if taken:
        raise MailExportError(
            f"{', '.join(taken)} already exists in {folder}, refusing to overwrite"
        )
    text = html_to_text(cleaned)
    expected_images = len(_IMG_TAG.findall(cleaned))
    render = renderer or _chrome_renderer()
    with tempfile.TemporaryDirectory(prefix="blumkin-mail-export-") as scratch:
        html_path = Path(scratch) / "message.html"
        pdf_path = Path(scratch) / "message.pdf"
        html_path.write_text(document, encoding="utf-8")
        render(html_path, pdf_path)
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise MailExportError("the browser produced no PDF")
        pdf_bytes = pdf_path.read_bytes()
    write_new_private_file(targets["pdf"], pdf_bytes)
    write_new_private_file(targets["txt"], (text + "\n").encode("utf-8"))
    sidecar = {
        "expected_images": expected_images,
        "from": message.get("from_email"),
        "injection_warning": message.get("injection_warning"),
        "message_id": message.get("id") or message_id.strip(),
        "received": message.get("received"),
        "subject": message.get("subject"),
        "tracking_pixels_removed": removed,
    }
    write_new_private_file(
        targets["json"], (json.dumps(sidecar, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    check = check_pdf(
        targets["pdf"],
        text_path=targets["txt"],
        expected_images=expected_images,
        pdf_reader=pdf_reader,
    )
    return {
        "check": check,
        "injection_warning": message.get("injection_warning"),
        "json": str(targets["json"]),
        "message_id": sidecar["message_id"],
        "pdf": str(targets["pdf"]),
        "text": str(targets["txt"]),
        "tracking_pixels_removed": removed,
    }


def html_to_text(raw: str) -> str:
    """Plain text of an HTML body, one line per block, entities decoded."""
    text = _DROPPED_BLOCKS.sub(" ", raw)
    text = _LINE_BREAKS.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text).replace(" ", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def read_pdf_layers(pdf_path: Path) -> dict[str, Any]:
    """Per-page text and the number of images in a PDF (needs the pdf extra)."""
    try:
        import pdfplumber  # noqa: PLC0415 - optional extra
    except ImportError as exc:
        raise RuntimeError(
            "checking a PDF needs the pdf extra: uv tool install -e '.[pdf]'"
        ) from exc
    with pdfplumber.open(str(pdf_path)) as document:
        pages = list(document.pages)
        return {
            "images": sum(len(page.images) for page in pages),
            "page_texts": [page.extract_text() or "" for page in pages],
        }


def render_pdf_command(browser: Path, html_path: Path, pdf_path: Path, profile: Path) -> list[str]:
    """Headless browser command: images on, settled by a virtual-time budget, fresh profile."""
    return [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pdf-header-footer",
        f"--user-data-dir={profile}",
        f"--virtual-time-budget={RENDER_TIME_BUDGET_MS}",
        f"--print-to-pdf={pdf_path}",
        html_path.resolve().as_uri(),
    ]


def strip_tracking_pixels(html: str) -> tuple[str, int]:
    """Drop one-pixel images (open trackers); returns the HTML and how many were removed."""
    removed = 0

    def drop(match: re.Match[str]) -> str:
        nonlocal removed
        if _is_pixel(match.group(0)):
            removed += 1
            return ""
        return match.group(0)

    return _IMG_TAG.sub(drop, html), removed


_DROPPED_BLOCKS = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_LINE_BREAKS = re.compile(
    r"<br\s*/?>|</(?:p|div|tr|li|h[1-6]|table|blockquote|pre)\s*>", re.IGNORECASE
)
_PIXEL_ATTR = r"""(?:^|[\s;"'])(?:{name})\s*[=:]\s*["']?\s*([0-9]+)(?:px)?\s*(?:["';\s/>]|$)"""
_TAG = re.compile(r"<[^>]*>")


def _as_document(fragment: str) -> str:
    if re.search(r"<meta[^>]+charset", fragment, re.IGNORECASE):
        return fragment
    return f'<!doctype html><meta charset="utf-8">{fragment}'


def _chrome_renderer() -> Renderer:
    browser = resolve_browser(None).path

    def render(html_path: Path, pdf_path: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="blumkin-mail-profile-") as profile:
            command = render_pdf_command(browser, html_path, pdf_path, Path(profile))
            try:
                subprocess.run(
                    command,
                    capture_output=True,
                    check=True,
                    timeout=RENDER_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise MailExportError(
                    f"the browser did not finish within {RENDER_TIMEOUT_SECONDS} seconds"
                ) from exc
            except subprocess.CalledProcessError as exc:
                raise MailExportError(f"the browser exited with status {exc.returncode}") from exc

    return render


def _default_stem(message: dict[str, Any]) -> str:
    received = str(message.get("received") or "")[:10]
    day = received if re.fullmatch(r"\d{4}-\d{2}-\d{2}", received) else "undated"
    slug = re.sub(r"[^a-z0-9]+", "-", str(message.get("subject") or "").lower()).strip("-")[:60]
    return f"{day}_{slug.strip('-') or 'message'}"


def _is_pixel(tag: str) -> bool:
    width = re.search(_PIXEL_ATTR.format(name="width"), tag, re.IGNORECASE)
    height = re.search(_PIXEL_ATTR.format(name="height"), tag, re.IGNORECASE)
    return bool(width and height and int(width.group(1)) <= 1 and int(height.group(1)) <= 1)


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    if not stem:
        raise ValueError("--name must contain letters or digits")
    return stem


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9$.,]{2,}", text.lower()))
