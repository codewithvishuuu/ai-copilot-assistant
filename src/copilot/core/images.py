"""Safe image handling for vision-capable models.

Defenses (see SECURITY.md):
- format is decided by magic bytes, not filename extensions;
- byte-size cap before and after download;
- pixel-count cap + Pillow's own decompression-bomb guard;
- images are re-encoded through Pillow to a sanitized copy (metadata such as
  EXIF/GPS is not carried over; oversized images are downscaled);
- every skipped image is reported — nothing is silently dropped.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field

from PIL import Image

from .collector import CollectedAttachment

log = logging.getLogger(__name__)

ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_PIXELS_DEFAULT = 40_000_000  # ~40 MP ceiling before scaling; Pillow guard set to match
MAX_DIMENSION_DEFAULT = 2048
MAX_OUTPUT_BYTES = 4_000_000
READ_TIMEOUT_S = 15.0

# Defense in depth: Pillow warns above MAX_IMAGE_PIXELS and raises above 2x.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS_DEFAULT


@dataclass
class PreparedImage:
    message_id: str
    filename: str
    data: bytes
    note: str | None = None


@dataclass
class ImagePrepResult:
    images: list[PreparedImage] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def sniff_image_format(data: bytes) -> str | None:
    """Detect the container format from magic bytes only."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def prepare_image_bytes(
    data: bytes,
    *,
    max_pixels: int = MAX_PIXELS_DEFAULT,
    max_dimension: int = MAX_DIMENSION_DEFAULT,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[bytes, str | None]:
    """Validate + re-encode one image into a sanitized copy.

    Returns ``(sanitized_bytes, note_or_None)``. Raises ``ValueError`` for
    anything unusable — the caller turns that into a skip note.
    """
    detected = sniff_image_format(data)
    if detected is None:
        raise ValueError("unrecognized image format")
    if detected not in ALLOWED_FORMATS:  # pragma: no cover - sniff only returns allowed formats
        raise ValueError("unsupported image format")

    try:
        image = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types for corrupt files
        raise ValueError("corrupt or unsupported image") from exc

    with image:
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("invalid image dimensions")
        if width * height > max_pixels:
            raise ValueError(f"image too large ({width}x{height} pixels)")

        note: str | None = None
        if max(width, height) > max_dimension:
            image.thumbnail((max_dimension, max_dimension))
            note = f"downscaled from {width}x{height}"

        if image.mode not in ("RGB", "RGBA", "L"):
            image = image.convert("RGB")

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        sanitized = output.getvalue()

        if len(sanitized) > max_output_bytes:
            output = io.BytesIO()
            rgb = image.convert("RGB") if image.mode != "RGB" else image
            rgb.save(output, format="JPEG", quality=85)
            sanitized = output.getvalue()
            note = (note + "; " if note else "") + "converted to JPEG to bound size"

    return sanitized, note


async def prepare_images(
    attachments: list[CollectedAttachment],
    *,
    max_images: int,
    max_bytes: int,
    read_timeout_s: float = READ_TIMEOUT_S,
) -> ImagePrepResult:
    """Download + sanitize up to ``max_images`` attachments. Never raises for one bad image."""
    result = ImagePrepResult()

    if len(attachments) > max_images:
        result.skipped.append(
            f"{len(attachments) - max_images} image(s) beyond the {max_images}-image limit were skipped"
        )
    considered = attachments[:max_images]

    for attachment in considered:
        label = f"image '{attachment.filename or 'unnamed'}' (message {attachment.message_id})"

        if attachment.size and attachment.size > max_bytes:
            result.skipped.append(
                f"{label} skipped: larger than {max_bytes // 1_000_000} MB"
            )
            continue

        raw = attachment.raw
        if raw is None or not hasattr(raw, "read"):
            result.skipped.append(f"{label} skipped: not downloadable")
            continue

        try:
            data = await asyncio.wait_for(raw.read(), timeout=read_timeout_s)
        except Exception as exc:  # noqa: BLE001 - any download failure is a skip
            result.skipped.append(f"{label} skipped: download failed ({type(exc).__name__})")
            continue

        if not isinstance(data, (bytes, bytearray)):
            result.skipped.append(f"{label} skipped: unexpected download result")
            continue
        data = bytes(data)

        if len(data) > max_bytes:
            result.skipped.append(
                f"{label} skipped: larger than {max_bytes // 1_000_000} MB"
            )
            continue

        try:
            sanitized, note = await asyncio.to_thread(prepare_image_bytes, data)
        except Exception as exc:  # noqa: BLE001 - corrupt/oversized files are skips
            result.skipped.append(f"{label} skipped: {exc}")
            continue

        result.images.append(
            PreparedImage(
                message_id=attachment.message_id,
                filename=attachment.filename or "image",
                data=sanitized,
                note=note,
            )
        )
        if note:
            result.notes.append(f"{label}: {note}")

    return result
