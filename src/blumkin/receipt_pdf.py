"""Turn a receipt photo into a PDF for expense sites that only take PDFs (issue #585)."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from blumkin.attachments import write_new_private_file

#: A phone photo is far bigger than a page needs; this keeps the PDF small enough to upload.
MAX_SIDE_PIXELS = 3000


class ReceiptPdfError(ValueError):
    """The photo cannot be converted or the destination is taken."""


def image_to_pdf(source: Path, dest: Path) -> Path:
    """Write `dest` (never replacing a file) as a one-page PDF of the photo at `source`."""
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise ReceiptPdfError(f"Expected an image file, got {str(source)!r}") from exc
    image.thumbnail((MAX_SIDE_PIXELS, MAX_SIDE_PIXELS))
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150.0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_new_private_file(dest, buffer.getvalue())
    except ValueError as exc:
        raise ReceiptPdfError(
            f"{dest.name} already exists in {dest.parent}, refusing to overwrite"
        ) from exc
    return dest.resolve()
