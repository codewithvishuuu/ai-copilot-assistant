"""Image pipeline: magic-byte sniffing, caps, sanitized re-encode, honest skips."""

from __future__ import annotations

import io
import os

import pytest
from fakes import FakeAttachment
from PIL import Image, PngImagePlugin

from copilot.core.collector import CollectedAttachment
from copilot.core.images import prepare_image_bytes, prepare_images, sniff_image_format


def _png(width=4, height=4, color=(255, 0, 0)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(width=4, height=4):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (0, 255, 0)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _attachment(raw=None, *, filename=None, content_type="image/png", size=None, message_id="101"):
    if filename is None:
        filename = getattr(raw, "filename", "image.png") if raw is not None else "image.png"
    return CollectedAttachment(
        filename=filename,
        content_type=content_type,
        size=size if size is not None else (len(raw.data) if raw is not None else 100),
        url="https://cdn.invalid/image.png",
        message_id=message_id,
        raw=raw,
    )


# -------------------------------------------------------------------- sniffing


def test_sniff_uses_magic_bytes_only():
    assert sniff_image_format(_png()) == "PNG"
    assert sniff_image_format(_jpeg()) == "JPEG"
    assert sniff_image_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "WEBP"
    assert sniff_image_format(b"GIF89a-not-supported") is None
    assert sniff_image_format(b"\x89PNG") is None  # truncated magic
    assert sniff_image_format(b"") is None


# -------------------------------------------------------------- sanitize copy


def test_prepare_returns_clean_png_without_metadata():
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret-metadata")
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG", pnginfo=info)
    raw = buffer.getvalue()
    assert b"secret-metadata" in raw  # the original carries the metadata

    sanitized, note = prepare_image_bytes(raw)
    assert sanitized.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"secret-metadata" not in sanitized  # the re-encoded copy does not
    assert note is None


def test_prepare_downscales_oversized_dimensions():
    sanitized, note = prepare_image_bytes(_png(3000, 10))
    assert note == "downscaled from 3000x10"
    with Image.open(io.BytesIO(sanitized)) as image:
        assert max(image.size) <= 2048


def test_prepare_converts_jpeg_input_to_png():
    sanitized, note = prepare_image_bytes(_jpeg())
    assert sanitized.startswith(b"\x89PNG")
    assert note is None


def test_prepare_rejects_unrecognized_corrupt_and_huge():
    with pytest.raises(ValueError, match="unrecognized"):
        prepare_image_bytes(b"not an image at all")

    corrupt = b"\x89PNG\r\n\x1a\n" + b"garbage" * 10
    with pytest.raises(ValueError, match="corrupt"):
        prepare_image_bytes(corrupt)

    with pytest.raises(ValueError, match="too large"):
        prepare_image_bytes(_png(10, 10), max_pixels=50)


def test_prepare_falls_back_to_jpeg_when_png_exceeds_output_cap():
    noise = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    raw = buffer.getvalue()

    sanitized, note = prepare_image_bytes(raw, max_output_bytes=10_000)
    assert sanitized.startswith(b"\xff\xd8\xff")  # JPEG magic
    assert len(sanitized) <= 10_000
    assert "converted to JPEG" in (note or "")


def test_prepare_applies_exif_orientation_before_stripping_metadata():
    image = Image.new("RGB", (10, 20), (200, 30, 30))
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 degrees clockwise for display
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    sanitized, _note = prepare_image_bytes(buffer.getvalue())
    with Image.open(io.BytesIO(sanitized)) as stored:
        assert stored.size == (20, 10)


# -------------------------------------------------------------------- download


async def test_prepare_images_downloads_and_notes_downscales():
    raw = FakeAttachment(data=_png(3000, 10), filename="wide.png")
    result = await prepare_images([_attachment(raw)], max_images=4, max_bytes=6_000_000)

    assert result.skipped == []
    assert len(result.images) == 1
    prepared = result.images[0]
    assert prepared.message_id == "101"
    assert prepared.filename == "wide.png"
    assert prepared.data.startswith(b"\x89PNG")
    assert prepared.note and "downscaled" in prepared.note
    assert any("wide.png" in note for note in result.notes)


async def test_prepare_images_reports_every_skip_reason():
    oversized = _attachment(FakeAttachment(data=_png(), filename="huge.png", size=9_000_000), size=9_000_000)
    undownloadable = _attachment(None, filename="ghost.png")
    failing = _attachment(FakeAttachment(data=b"", read_error=RuntimeError("boom"), filename="fails.png"))
    corrupt = _attachment(FakeAttachment(data=b"\x89PNG\r\n\x1a\nbroken", filename="corrupt.png"))
    not_bytes = _attachment(FakeAttachment(data="text-not-bytes", filename="weird.png"))

    result = await prepare_images(
        [oversized, undownloadable, failing, corrupt, not_bytes],
        max_images=10,
        max_bytes=6_000_000,
    )

    assert result.images == []
    joined = " | ".join(result.skipped)
    assert "larger than 6 MB" in joined
    assert "not downloadable" in joined
    assert "download failed (RuntimeError)" in joined
    assert "corrupt or unsupported image" in joined
    assert "unexpected download result" in joined


async def test_prepare_images_enforces_max_images_limit():
    raws = [FakeAttachment(data=_png(), filename=f"{n}.png") for n in range(3)]
    attachments = [_attachment(raw, message_id=str(n)) for n, raw in enumerate(raws)]

    result = await prepare_images(attachments, max_images=2, max_bytes=6_000_000)

    assert len(result.images) == 2
    assert any("beyond the 2-image limit" in note for note in result.skipped)


async def test_prepare_images_times_out_slow_downloads():
    slow = _attachment(FakeAttachment(data=_png(), filename="slow.png", read_delay=0.3))
    result = await prepare_images([slow], max_images=1, max_bytes=6_000_000, read_timeout_s=0.05)

    assert result.images == []
    assert any("download failed (TimeoutError)" in note for note in result.skipped)


async def test_prepare_images_rechecks_size_after_download():
    lying = _attachment(FakeAttachment(data=b"x" * 3_000_000, filename="liar.png", size=10), size=10)
    result = await prepare_images([lying], max_images=1, max_bytes=2_000_000)

    assert result.images == []
    assert any("larger than 2 MB" in note for note in result.skipped)
