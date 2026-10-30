"""Turn a receipt photo into a PDF for expense sites that only take PDFs (issue #585)."""

from __future__ import annotations

import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from blumkin.attachments import write_new_private_file

#: A phone photo is far bigger than a page needs; this keeps the PDF small enough to upload.
MAX_SIDE_PIXELS = 3000
#: Photos bigger than this are refused before they are decoded (a decompression-bomb guard).
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_SOURCE_PIXELS = 50_000_000


class ReceiptPdfError(ValueError):
    """The photo cannot be converted or the destination is taken."""


def image_to_pdf(source: Path, dest: Path) -> Path:
    """Write `dest` (never replacing a file) as a one-page PDF of the photo at `source`."""
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ReceiptPdfError(f"Expected a readable image file, got {str(source)!r}") from exc
    if size > MAX_SOURCE_BYTES:
        raise ReceiptPdfError(
            f"Expected an image of at most {MAX_SOURCE_BYTES // (1024 * 1024)} MB, got {size} bytes"
        )
    image = _flat_rgb(source)
    image.thumbnail((MAX_SIDE_PIXELS, MAX_SIDE_PIXELS))
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150.0)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_new_private_file(dest, buffer.getvalue())
    except ValueError as exc:
        raise ReceiptPdfError(
            f"{dest.name} already exists in {dest.parent}, refusing to overwrite"
        ) from exc
    except OSError as exc:
        raise ReceiptPdfError(
            f"Expected a writable place for {str(dest)!r}, got {type(exc).__name__}"
        ) from exc
    return dest.resolve()


def _flat_rgb(source: Path) -> Image.Image:
    """The photo upright, on a white background where it is transparent, in RGB."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as opened:
                width, height = opened.size
                if width * height > MAX_SOURCE_PIXELS:
                    raise ReceiptPdfError(
                        f"Expected an image of at most {MAX_SOURCE_PIXELS:,} pixels, "
                        f"got {width * height:,}"
                    )
                # Shrink before anything copies the frame (turning it upright, converting its
                # mode): the draft only helps JPEG, the thumbnail covers every format.
                opened.draft("RGB", (MAX_SIDE_PIXELS, MAX_SIDE_PIXELS))
                opened.thumbnail((MAX_SIDE_PIXELS, MAX_SIDE_PIXELS))
                upright = ImageOps.exif_transpose(opened)
                if upright.mode in {"RGBA", "LA", "PA"} or "transparency" in upright.info:
                    rgba = upright.convert("RGBA")
                    flat = Image.new("RGB", rgba.size, (255, 255, 255))
                    flat.paste(rgba, mask=rgba.getchannel("A"))
                    return flat
                return upright.convert("RGB")
    except ReceiptPdfError:
        raise
    except (
        OSError,
        SyntaxError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ReceiptPdfError(f"Expected an image file, got {str(source)!r}") from exc
