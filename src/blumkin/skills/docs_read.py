"""Read local PDF, DOCX, and XLSX files into a stable JSON shape."""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path
from typing import Any

from docx import Document

from blumkin.config import BlumkinConfig


class DocsReadExtraMissingError(ValueError):
    """`docs read` needs an optional dependency group that is not installed."""


class DocsReadFileNotFoundError(FileNotFoundError):
    """The requested local file does not exist or is not a regular file."""


class DocsReadOcrUnavailableError(ValueError):
    """`--ocr` was requested, but the extra or a required system binary is missing."""


class DocsReadOversizeError(ValueError):
    """The input file or extracted output exceeds the local safety caps."""


class DocsReadUnsupportedFormatError(ValueError):
    """The file extension is not one of the supported local document formats."""


async def docs_read(
    *,
    config: BlumkinConfig,
    path: str,
    pages: str | None = None,
    sheet: str | None = None,
    ocr: bool = False,
) -> dict[str, Any]:
    """Read one local file already on disk; no provider, auth, or network calls."""
    del config
    file_path = Path(path).expanduser()
    _ensure_file(file_path)
    kind = file_path.suffix.lower()
    _validate_flags(kind, ocr=ocr, pages=pages, sheet=sheet)

    ocr_used = False
    if kind == ".docx":
        extracted_pages = _read_docx(file_path)
    elif kind == ".pdf":
        extracted_pages, ocr_used = _read_pdf(file_path, ocr=ocr, pages=pages)
    elif kind == ".xlsx":
        extracted_pages = _read_xlsx(file_path, sheet=sheet)
    else:
        raise DocsReadUnsupportedFormatError(
            f"unsupported file type {kind or '<none>'!r} (expected .pdf, .docx, or .xlsx)"
        )

    return {
        "kind": kind.removeprefix("."),
        "ocr_used": ocr_used,
        "ok": True,
        "pages": extracted_pages,
        "path": str(file_path.resolve()),
    }


def format_docs_read_human(payload: dict[str, Any]) -> list[str]:
    pages = payload.get("pages") or []
    lines = [
        f"Read {payload.get('path')!r} ({payload.get('kind')}); "
        f"{len(pages)} section(s); OCR used: {'yes' if payload.get('ocr_used') else 'no'}"
    ]
    for page in pages:
        heading = f"section {page.get('index')}"
        if page.get("sheet"):
            heading = f"sheet {page.get('sheet')!r}"
        lines.append("")
        lines.append(f"[{heading}]")
        text = str(page.get("text") or "").strip()
        lines.extend(text.splitlines() or ["(no extractable text)"])
        tables = page.get("tables") or []
        if tables:
            lines.append("")
            lines.append(f"tables: {len(tables)}")
            for table_index, table in enumerate(tables, start=1):
                lines.append(f"  table {table_index}:")
                for row in table:
                    lines.append("    " + " | ".join(row))
    return lines


_MAX_EXTRACTED_BYTES = 1_000_000
_MAX_FILE_BYTES = 25_000_000
_OCR_EXTRA_HINT = "docs read --ocr needs the ocr extra: uv tool install -e '.[ocr]'"
_POPPLER_HINT = "poppler not found on PATH, install via `brew install tesseract poppler`"
_TESSERACT_HINT = "tesseract not found on PATH, install via `brew install tesseract poppler`"


def _ensure_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise DocsReadFileNotFoundError(f"file not found: {path}")
    size = path.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise DocsReadOversizeError(
            f"file is larger than {_MAX_FILE_BYTES} bytes; choose a smaller file"
        )


def _import_ocr_modules() -> tuple[Any, Any, Any]:
    try:
        pdf2image = importlib.import_module("pdf2image")
        pdf2image_exceptions = importlib.import_module("pdf2image.exceptions")
        pytesseract = importlib.import_module("pytesseract")
    except ModuleNotFoundError as exc:
        raise DocsReadOcrUnavailableError(_OCR_EXTRA_HINT) from exc
    return pdf2image, pdf2image_exceptions, pytesseract


def _import_openpyxl() -> Any:
    try:
        return importlib.import_module("openpyxl")
    except ModuleNotFoundError as exc:
        raise DocsReadExtraMissingError(
            "docs read needs the xlsx extra: uv tool install -e '.[xlsx]'"
        ) from exc


def _import_pdfplumber() -> Any:
    try:
        return importlib.import_module("pdfplumber")
    except ModuleNotFoundError as exc:
        raise DocsReadExtraMissingError(
            "docs read needs the pdf extra: uv tool install -e '.[pdf]'"
        ) from exc


def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _ocr_pdf_page(path: Path, *, page_index: int) -> str:
    _require_ocr_binaries()
    pdf2image, pdf2image_exceptions, pytesseract = _import_ocr_modules()
    poppler_errors = tuple(
        error
        for name in ("PDFInfoNotInstalledError", "PDFPageCountError", "PopplerNotInstalledError")
        if (error := getattr(pdf2image_exceptions, name, None)) is not None
    )
    try:
        images = pdf2image.convert_from_path(
            str(path),
            first_page=page_index,
            last_page=page_index,
            fmt="png",
            single_file=True,
        )
    except poppler_errors as exc:
        raise DocsReadOcrUnavailableError(_POPPLER_HINT) from exc
    if not images:
        return ""
    try:
        return str(pytesseract.image_to_string(images[0])).strip()
    except getattr(pytesseract, "TesseractNotFoundError", RuntimeError) as exc:
        raise DocsReadOcrUnavailableError(_TESSERACT_HINT) from exc


def _page_size_bytes(page: dict[str, Any]) -> int:
    size = len(str(page.get("text") or "").encode("utf-8"))
    if sheet := page.get("sheet"):
        size += len(str(sheet).encode("utf-8"))
    for table in page.get("tables") or []:
        for row in table:
            for cell in row:
                size += len(str(cell).encode("utf-8"))
    return size


def _parse_pages(raw: str | None, *, total_pages: int) -> list[int]:
    if raw is None:
        return list(range(1, total_pages + 1))
    selected: set[int] = set()
    chunks = [part.strip() for part in raw.split(",") if part.strip()]
    if not chunks:
        raise ValueError(f"invalid --pages value {raw!r} (expected 1-3 or 1,3,5)")
    for chunk in chunks:
        if "-" in chunk:
            start_text, end_text = [part.strip() for part in chunk.split("-", 1)]
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"invalid --pages value {raw!r} (expected 1-3 or 1,3,5)")
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                raise ValueError(f"invalid --pages value {raw!r} (expected 1-3 or 1,3,5)")
            selected.update(range(start, end + 1))
            continue
        if not chunk.isdigit() or int(chunk) < 1:
            raise ValueError(f"invalid --pages value {raw!r} (expected 1-3 or 1,3,5)")
        selected.add(int(chunk))
    pages = sorted(selected)
    too_high = next((page for page in pages if page > total_pages), None)
    if too_high is not None:
        raise ValueError(f"--pages selects page {too_high}, but this PDF has {total_pages} page(s)")
    return pages


def _read_docx(path: Path) -> list[dict[str, Any]]:
    try:
        document = Document(str(path))
    except Exception as exc:  # noqa: BLE001 - emit a clean usage error message
        raise ValueError(f"cannot read docx file {path}: {exc}") from exc
    paragraphs = [
        paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()
    ]
    tables = [table for table in (_table_rows(x) for x in document.tables) if table]
    page = {"index": 1, "tables": tables, "text": "\n\n".join(paragraphs)}
    _require_output_budget([page])
    return [page]


def _read_pdf(path: Path, *, ocr: bool, pages: str | None) -> tuple[list[dict[str, Any]], bool]:
    pdfplumber = _import_pdfplumber()
    try:
        with pdfplumber.open(str(path)) as document:
            selected_pages = _parse_pages(pages, total_pages=len(document.pages))
            extracted_pages: list[dict[str, Any]] = []
            ocr_used = False
            for page_index in selected_pages:
                page = document.pages[page_index - 1]
                text = str(page.extract_text() or "").strip()
                if ocr and not text:
                    text = _ocr_pdf_page(path, page_index=page_index)
                    ocr_used = True
                extracted_page = {
                    "index": page_index,
                    "tables": [_table_rows(table) for table in page.extract_tables() or []],
                    "text": text,
                }
                extracted_pages.append(extracted_page)
    except DocsReadOcrUnavailableError, DocsReadOversizeError, ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - emit a clean usage error message
        raise ValueError(f"cannot read pdf file {path}: {exc}") from exc
    _require_output_budget(extracted_pages)
    return extracted_pages, ocr_used


def _read_xlsx(path: Path, *, sheet: str | None) -> list[dict[str, Any]]:
    openpyxl = _import_openpyxl()
    try:
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - emit a clean usage error message
        raise ValueError(f"cannot read xlsx file {path}: {exc}") from exc
    try:
        worksheet = _resolve_sheet(workbook, sheet)
        rows = []
        for row in worksheet.iter_rows(values_only=True):
            trimmed = _trim_row([_normalize_cell(cell) for cell in row])
            if trimmed:
                rows.append(trimmed)
        page = {
            "index": 1,
            "sheet": worksheet.title,
            "tables": [rows] if rows else [],
            "text": "\n".join("\t".join(row) for row in rows),
        }
    finally:
        workbook.close()
    _require_output_budget([page])
    return [page]


def _require_ocr_binaries() -> None:
    if shutil.which("tesseract") is None:
        raise DocsReadOcrUnavailableError(_TESSERACT_HINT)
    if shutil.which("pdfinfo") is None and shutil.which("pdftoppm") is None:
        raise DocsReadOcrUnavailableError(_POPPLER_HINT)


def _require_output_budget(pages: list[dict[str, Any]]) -> None:
    total = sum(_page_size_bytes(page) for page in pages)
    if total > _MAX_EXTRACTED_BYTES:
        raise DocsReadOversizeError(
            "extracted content exceeds "
            f"{_MAX_EXTRACTED_BYTES} bytes; narrow --pages or choose a smaller file"
        )


def _resolve_sheet(workbook: Any, sheet: str | None) -> Any:
    worksheets = list(workbook.worksheets)
    if not worksheets:
        raise ValueError("workbook has no worksheets")
    if sheet is None:
        return worksheets[0]
    wanted = sheet.strip()
    if not wanted:
        raise ValueError("--sheet must not be blank")
    if wanted.isdigit():
        index = int(wanted)
        if 1 <= index <= len(worksheets):
            return worksheets[index - 1]
        raise ValueError(
            f"--sheet index {index} is out of range for {len(worksheets)} worksheet(s)"
        )
    try:
        return workbook[wanted]
    except KeyError as exc:
        raise ValueError(
            f"worksheet {wanted!r} not found; available sheets: {workbook.sheetnames}"
        ) from exc


def _table_rows(table: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    if hasattr(table, "rows"):
        iterable = ([cell.text for cell in row.cells] for row in table.rows)
    else:
        iterable = table
    for row in iterable:
        trimmed = _trim_row([_normalize_cell(cell) for cell in row])
        if trimmed:
            rows.append(trimmed)
    return rows


def _trim_row(row: list[str]) -> list[str]:
    trimmed = list(row)
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return trimmed


def _validate_flags(kind: str, *, ocr: bool, pages: str | None, sheet: str | None) -> None:
    if kind == ".pdf":
        if sheet is not None:
            raise ValueError("--sheet is only valid for .xlsx files")
        return
    if ocr:
        raise ValueError("--ocr is only valid for .pdf files")
    if pages is not None:
        raise ValueError("--pages is only valid for .pdf files")
    if kind == ".docx" and sheet is not None:
        raise ValueError("--sheet is only valid for .xlsx files")
