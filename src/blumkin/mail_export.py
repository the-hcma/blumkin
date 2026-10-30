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

import hashlib
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
#: Largest text file `receipts read` trusts next to a PDF (the `docs read` text limit).
MAX_TEXT_BYTES = 1_000_000
#: How long the browser may spend loading images and settling, in virtual milliseconds.
RENDER_TIME_BUDGET_MS = 15000
RENDER_TIMEOUT_SECONDS = 90

PdfReader = Callable[[Path], dict[str, Any]]
Renderer = Callable[[Path, Path], None]


class MailExportError(RuntimeError):
    """The message could not be exported (no body, name taken, render failed)."""


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
    page_images: list[int] = list(layers.get("page_images") or [0] * len(page_texts))
    images = int(layers.get("images") or sum(page_images))
    problems: list[str] = []
    blank = [
        index + 1
        for index, text in enumerate(page_texts)
        if not text.strip() and not (page_images[index] if index < len(page_images) else 0)
    ]
    if not page_texts:
        problems.append("PDF has no pages")
    if blank:
        problems.append(f"blank pages: {', '.join(str(number) for number in blank)}")
    if expected_images is not None and images < expected_images:
        problems.append(f"expected {expected_images} images in the PDF, found {images}")
    overlap: float | None = None
    if text_path is not None:
        try:
            wanted = _words(text_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            wanted = set()
            problems.append(f"text file could not be read ({type(exc).__name__})")
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


async def export_message(
    provider: Any,
    *,
    message_id: str,
    out_dir: Path,
    name: str | None = None,
    browser: str | None = None,
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
    # Removing a pixel can join the text around it into a new tag, so sanitize again after.
    cleaned, removed = strip_tracking_pixels(sanitize_html(body))
    cleaned = sanitize_html(cleaned)
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
    expected_images = count_loadable_images(cleaned)
    render = renderer or _chrome_renderer(browser)
    sidecar = {
        "expected_images": expected_images,
        "from": message.get("from_email"),
        "injection_warning": message.get("injection_warning"),
        "message_id": message.get("id") or message_id.strip(),
        "received": message.get("received"),
        "subject": message.get("subject"),
        "tracking_pixels_removed": removed,
    }
    payload: dict[str, Any] = {
        "injection_warning": message.get("injection_warning"),
        "json": None,
        "message_id": sidecar["message_id"],
        "pdf": None,
        "text": None,
        "tracking_pixels_removed": removed,
    }
    with tempfile.TemporaryDirectory(prefix="blumkin-mail-export-") as scratch:
        html_path = Path(scratch) / "message.html"
        pdf_path = Path(scratch) / "message.pdf"
        text_path = Path(scratch) / "message.txt"
        html_path.write_text(document, encoding="utf-8")
        render(html_path, pdf_path)
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise MailExportError("the browser produced no PDF")
        text_path.write_text(text + "\n", encoding="utf-8")
        check = check_pdf(
            pdf_path, text_path=text_path, expected_images=expected_images, pdf_reader=pdf_reader
        )
        payload["check"] = check
        if not check["ok"]:
            # Nothing is left behind, so the same --out and --name can be retried.
            return payload
        pdf_bytes = pdf_path.read_bytes()
    sidecar["pdf_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
    sidecar["text_sha256"] = hashlib.sha256((text + "\n").encode("utf-8")).hexdigest()
    written: list[Path] = []
    try:
        for suffix, data in (
            ("pdf", pdf_bytes),
            ("txt", (text + "\n").encode("utf-8")),
            ("json", (json.dumps(sidecar, indent=2, sort_keys=True) + "\n").encode("utf-8")),
        ):
            write_new_private_file(targets[suffix], data)
            written.append(targets[suffix])
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    payload.update(json=str(targets["json"]), pdf=str(targets["pdf"]), text=str(targets["txt"]))
    return payload


def count_loadable_images(html: str) -> int:
    """Images the render should show: body `<img>` tags, not comments or `cid:` inline parts."""
    body = _HEAD_BLOCK.sub(" ", _COMMENT.sub(" ", html))
    return sum(1 for tag in _IMG_TAG.findall(body) if not _CID_SRC.search(tag))


def html_to_text(raw: str) -> str:
    """Plain text of an HTML body, one line per block, entities decoded."""
    text = _DROPPED_BLOCKS.sub(" ", raw)
    text = _LINE_BREAKS.sub("\n", text)
    text = _TABLE_CELLS.sub(" ", text)
    text = _TAG.sub("", text)
    text = html_lib.unescape(text).replace("\u00a0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def read_bound_text(pdf_path: Path) -> str | None:
    """The `.txt` beside a PDF, only when `mail export` recorded it as this PDF's text.

    The `.json` beside them carries both SHA-256 hashes; a missing or different hash, a
    symlink, an oversized or unreadable file all mean "do not trust it" (None).
    """
    text_path = pdf_path.with_suffix(".txt")
    sidecar_path = pdf_path.with_suffix(".json")
    for path in (pdf_path, text_path, sidecar_path):
        if path.is_symlink() or not path.is_file():
            return None
    try:
        if text_path.stat().st_size > MAX_TEXT_BYTES:
            return None
        recorded = json.loads(sidecar_path.read_text(encoding="utf-8"))
        text_bytes = text_path.read_bytes()
        text = text_bytes.decode("utf-8")
        if recorded["text_sha256"] != hashlib.sha256(text_bytes).hexdigest() or recorded[
            "pdf_sha256"
        ] != _file_sha256(pdf_path):
            return None
    except OSError, UnicodeDecodeError, ValueError, KeyError, TypeError:
        return None
    return text if text.strip() else None


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
        page_images = [len(page.images) for page in pages]
        return {
            "images": sum(page_images),
            "page_images": page_images,
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


def sanitize_html(html: str) -> str:
    """Remove comments, scripts, frames, embeds and linked resources before rendering."""
    cleaned = _COMMENT.sub(" ", html)
    cleaned = _ACTIVE_BLOCKS.sub(" ", cleaned)
    cleaned = _drop_hidden(cleaned)
    return _LINKED_TAGS.sub(" ", cleaned)


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


_ACTIVE_BLOCKS = re.compile(
    r"<(script|iframe|object|embed|noscript)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE
)
_CID_SRC = re.compile(r"""src\s*=\s*["']?\s*cid:""", re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; font-src data:"
)
_DROPPED_BLOCKS = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_HIDDEN_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HIDDEN_OPEN = re.compile(
    r"<(div|span|p|td|font|a)\b[^>]*?\bstyle\s*=\s*(?:\"([^\"]*)\"|'([^']*)')[^>]*>",
    re.IGNORECASE,
)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_VISIBLE_AGAIN = re.compile(r"visibility\s*:\s*visible", re.IGNORECASE)
_HEAD_BLOCK = re.compile(r"<head\b.*?</head\s*>", re.DOTALL | re.IGNORECASE)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_LINE_BREAKS = re.compile(
    r"<(?:br|hr)\b[^>]*>"
    r"|</?(?:p|div|tr|li|ul|ol|h[1-6]|table|tbody|thead|blockquote|pre|section|article)\b[^>]*>",
    re.IGNORECASE,
)
_LINKED_TAGS = re.compile(r"<(?:link|base|meta)\b[^>]*>", re.IGNORECASE)
_TABLE_CELLS = re.compile(r"</?(?:td|th)\b[^>]*>", re.IGNORECASE)
_PIXEL_ATTR = r"""(?:^|[\s;"'])(?:{name})\s*[=:]\s*["']?\s*([0-9]+)(?:px)?\s*(?:["';\s/>]|$)"""
_TAG = re.compile(r"<[^>]*>")


def _as_document(fragment: str) -> str:
    # The policy comes first so nothing in the message can undo it: no scripts, no frames, and
    # only https or inline images, fonts and styles (no local files, no other hosts' markup).
    policy = f'<meta http-equiv="Content-Security-Policy" content="{_CONTENT_SECURITY_POLICY}">'
    return f'<!doctype html><meta charset="utf-8">{policy}{fragment}'


def _chrome_renderer(preferred: str | None) -> Renderer:
    browser = resolve_browser(preferred).path

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


def _drop_hidden(html: str) -> str:
    """Remove each element hidden by an inline style, with everything nested inside it.

    A hidden declaration counts only when it is the one that wins in that style attribute, and
    a `visibility:hidden` subtree that turns `visibility:visible` again is left alone: keeping
    some hidden text is safer than deleting an amount the page shows.
    """
    html = _HIDDEN_IMG.sub(lambda tag: " " if _style_hides(tag.group(0))[0] else tag.group(0), html)
    searched = 0
    while match := _HIDDEN_OPEN.search(html, searched):
        hides, only_visibility = _style_hides(match.group(0))
        if not hides:
            searched = match.end()
            continue
        same_tag = re.compile(rf"<(/?){match.group(1)}\b[^>]*>", re.IGNORECASE)
        depth = 1
        end = match.end()
        for tag in same_tag.finditer(html, match.end()):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                end = tag.end()
                break
        else:
            # Never closed: drop only the opening tag rather than the rest of the message.
            html = html[: match.start()] + html[match.end() :]
            searched = match.start()
            continue
        if only_visibility and _VISIBLE_AGAIN.search(html, match.end(), end):
            searched = match.end()
            continue
        html = html[: match.start()] + " " + html[end:]
        searched = match.start()
    return html


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _style_hides(tag: str) -> tuple[bool, bool]:
    """Whether the tag's style attribute ends up hiding it, and whether only visibility does."""
    found = _STYLE_ATTR.search(tag)
    if found is None:
        return False, False
    final: dict[str, tuple[str, bool]] = {}
    for declaration in (found.group(1) or found.group(2) or "").split(";"):
        name, _, value = declaration.partition(":")
        value = value.strip().lower()
        important = value.endswith("!important")
        value = value.removesuffix("!important").strip()
        key = name.strip().lower()
        if key and (key not in final or important or not final[key][1]):
            final[key] = (value, important)
    display = final.get("display", ("", False))[0] == "none"
    visibility = final.get("visibility", ("", False))[0] == "hidden"
    mso = final.get("mso-hide", ("", False))[0] == "all"
    opacity = final.get("opacity", ("", False))[0] in {"0", "0.0"}
    return display or visibility or mso or opacity, visibility and not (display or mso or opacity)


def _is_pixel(tag: str) -> bool:
    width = re.search(_PIXEL_ATTR.format(name="width"), tag, re.IGNORECASE)
    height = re.search(_PIXEL_ATTR.format(name="height"), tag, re.IGNORECASE)
    return bool(width and height and int(width.group(1)) <= 1 and int(height.group(1)) <= 1)


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())[:120].strip("-.")
    if not stem:
        raise ValueError("--name must contain letters or digits")
    return stem


def _words(text: str) -> set[str]:
    return set(re.findall(r"[\w$.,]{2,}", text.lower()))
