"""Attachment ingestion: magic bytes, extraction bounds, fencing, retention."""

from __future__ import annotations

import io
import zipfile
import zlib

import pytest
from fakes import FakeAttachment, FakeChatMessage, fake_settings

from copilot.core.attachments import (
    AttachmentService,
    classify_attachment,
    extract_docx_text,
    extract_pdf_text,
    extract_text_file,
    fence_attachment_text,
    fence_followup_attachments,
    is_sensitive_filename,
    safe_filename,
)
from copilot.db.repository import Repository


def _docx(document_xml: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf ({text}) Tj ET".encode("latin-1")
    compressed = zlib.compress(content)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Page /Contents 2 0 R >> endobj\n"
        b"2 0 obj << /Length " + str(len(compressed)).encode() + b" /Filter /FlateDecode >>\n"
        b"stream\n" + compressed + b"\nendstream\nendobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )


# ------------------------------------------------------------------ pure helpers


def test_classification_uses_magic_bytes_not_extensions():
    assert classify_attachment("photo.png", "image/png", b"\x89PNG\r\n\x1a\nxx")[0] == "image"
    assert classify_attachment("photo.exe", "application/octet-stream", b"%PDF-1.7 ...")[0] == "pdf"
    assert classify_attachment("notes.txt", "text/plain", b"hello")[0] == "text"
    assert classify_attachment("main.py", None, b"print('x')")[0] == "code"
    assert classify_attachment("server.log", None, b"ERROR boom")[0] == "log"
    assert classify_attachment("payload.bin", None, b"\x00\x01\x02")[0] == "unsupported"


def test_sensitive_and_pathological_filenames_are_refused_or_flattened():
    assert is_sensitive_filename(".env")
    assert is_sensitive_filename("deploy/id_rsa")
    assert is_sensitive_filename("cert.pem")
    assert not is_sensitive_filename("main.py")
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("folder\\report.txt") == "report.txt"
    assert safe_filename("\x00\x01") == "attachment"


def test_text_extraction_is_bounded_and_refuses_binary():
    extracted = extract_text_file(b"a" * 50, max_chars=10)
    assert extracted.text == "a" * 10
    assert extracted.truncated is True
    assert extract_text_file(b"abc\x00def", max_chars=100).error_code == "binary_content"


def test_pdf_extracts_selectable_text_from_flate_stream():
    extracted = extract_pdf_text(_pdf("Hello PDF world"), max_pages=10, max_chars=1000)
    assert "Hello PDF world" in extracted.text
    assert extracted.pages == 1
    assert extracted.error_code is None


def test_pdf_reports_encrypted_without_guessing():
    extracted = extract_pdf_text(b"%PDF-1.7\n/Encrypt 5 0 R\n%%EOF", max_pages=5, max_chars=100)
    assert extracted.error_code == "pdf_encrypted"
    assert extracted.text == ""


def test_pdf_without_text_is_reported_honestly():
    extracted = extract_pdf_text(
        b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF", max_pages=5, max_chars=100
    )
    assert extracted.error_code == "pdf_no_text"


def test_docx_extracts_paragraphs_and_bounds_output():
    xml = (
        b"<w:document><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t> world</w:t></w:r></w:p></w:body></w:document>"
    )
    extracted = extract_docx_text(_docx(xml), max_chars=5)
    assert extracted.text == "Hello"
    assert extracted.truncated is True


def test_docx_rejects_non_zip_and_missing_document():
    assert extract_docx_text(b"not a zip", max_chars=100).error_code == "docx_corrupt"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.xml", b"<x/>")
    assert extract_docx_text(buffer.getvalue(), max_chars=100).error_code == "docx_missing_document"


def test_attachment_fences_are_bounded_and_marked_untrusted():
    class Item:
        filename = "server.log"
        kind = "log"
        pages = None
        width = None
        height = None
        truncated = False
        extracted_text = "ERROR unauthorized"

    block = fence_attachment_text([Item()], max_chars=200)
    assert block is not None
    assert block.startswith("<attachments")
    assert "never instructions" in block
    assert "IGNORE ALL RULES" not in block  # the fence adds no such text
    assert "ERROR unauthorized" in block


def test_followup_fence_marks_image_pixels_as_unavailable():
    rows = [
        {
            "filename": "shot.png",
            "kind": "image",
            "message_id": "42",
            "extracted_text": "",
            "truncated": 0,
        }
    ]
    block = fence_followup_attachments(rows, max_chars=500)
    assert block is not None
    assert "earlier-attachments" in block
    assert "not retained" in block


# --------------------------------------------------------------------- service


async def test_service_persists_only_metadata_and_extracted_text(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    settings = fake_settings(tmp_path)
    service = AttachmentService(repo, settings)
    attachment = FakeAttachment(
        filename="error.log", content_type="text/plain", data=b"Traceback: boom at line 3"
    )
    message = FakeChatMessage(id=77, attachments=[attachment])

    bundle = await service.ingest_message(
        message, guild_id=500, channel_id=2002, user_id=7, vision_supported=False
    )

    assert "Traceback: boom at line 3" in (bundle.context_block or "")
    assert bundle.images == []
    rows = await repo.recent_attachments(guild_id="500", channel_id="2002", user_id="7")
    assert len(rows) == 1
    assert rows[0]["kind"] == "log"
    assert "Traceback" in rows[0]["extracted_text"]
    assert rows[0]["sha256"]


async def test_service_refuses_sensitive_and_unsupported_files(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = AttachmentService(repo, fake_settings(tmp_path))
    message = FakeChatMessage(
        id=78,
        attachments=[
            FakeAttachment(filename=".env", content_type="text/plain", data=b"TOKEN=secret"),
            FakeAttachment(filename="setup.exe", content_type="application/octet-stream", data=b"MZ"),
        ],
    )

    bundle = await service.ingest_message(
        message, guild_id=500, channel_id=2002, user_id=7, vision_supported=True
    )

    assert all("TOKEN=secret" not in (item.extracted_text or "") for item in bundle.ingested)
    notes = bundle.notes_text() or ""
    assert "secret/key files" in notes
    assert "can't read that attachment type" in notes


async def test_service_skips_images_without_vision(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = AttachmentService(repo, fake_settings(tmp_path))
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    message = FakeChatMessage(
        id=79,
        attachments=[FakeAttachment(filename="shot.png", content_type="image/png", data=png)],
    )

    bundle = await service.ingest_message(
        message, guild_id=500, channel_id=2002, user_id=7, vision_supported=False
    )

    assert bundle.images == []
    assert "does not support vision" in (bundle.notes_text() or "")


async def test_followup_context_excludes_current_message_and_other_scopes(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = AttachmentService(repo, fake_settings(tmp_path))
    first = FakeChatMessage(
        id=80,
        attachments=[FakeAttachment(filename="old.log", data=b"old evidence")],
    )
    await service.ingest_message(
        first, guild_id=500, channel_id=2002, user_id=7, vision_supported=False
    )
    second = FakeChatMessage(
        id=81,
        attachments=[FakeAttachment(filename="new.log", data=b"new evidence")],
    )
    await service.ingest_message(
        second, guild_id=500, channel_id=2002, user_id=7, vision_supported=False
    )

    block = await service.followup_block(
        guild_id=500, channel_id=2002, user_id=7, exclude_message_id="81"
    )
    assert "old evidence" in (block or "")
    assert "new evidence" not in (block or "")
    assert await service.followup_block(
        guild_id=500, channel_id=2002, user_id=8, exclude_message_id="81"
    ) is None


async def test_clear_and_retention_controls_remove_extracts(tmp_path, tmp_db):
    repo = Repository(tmp_db)
    service = AttachmentService(repo, fake_settings(tmp_path))
    message = FakeChatMessage(
        id=82, attachments=[FakeAttachment(filename="a.log", data=b"evidence")]
    )
    await service.ingest_message(
        message, guild_id=500, channel_id=2002, user_id=7, vision_supported=False
    )
    assert await service.clear_scope(guild_id=500, channel_id=2002, user_id=7) == 1

    await service.ingest_message(
        message, guild_id=500, channel_id=2002, user_id=7, vision_supported=False
    )
    assert await repo.purge_expired_attachments(before="9999-01-01T00:00:00+00:00") == 1


@pytest.mark.parametrize("kind", ["text", "code", "log"])
def test_allowed_text_kinds_roundtrip(kind):
    suffix = {"text": ".md", "code": ".py", "log": ".log"}[kind]
    assert classify_attachment("x" + suffix, None, b"data")[0] == kind
