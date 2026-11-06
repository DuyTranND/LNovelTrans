"""Tests for Trạm Dịch API (Milestone 2)."""

from __future__ import annotations

import io
import uuid
import zipfile
import pytest
from fastapi.testclient import TestClient

from backend.app.epub import parse_epub, EPUB_MIMETYPE
from backend.app.main import create_app
from backend.app.db import get_connection

# Attempt import from test_epub
try:
    from backend.tests.test_epub import build_test_epub_bytes
except ImportError:
    try:
        from tests.test_epub import build_test_epub_bytes
    except ImportError:
        build_test_epub_bytes = None


def create_minimal_test_epub() -> bytes:
    """Fallback valid test EPUB generator with CSS and 2 chapters."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Mimetype first uncompressed
        zinfo_mime = zipfile.ZipInfo("mimetype")
        zinfo_mime.compress_type = zipfile.ZIP_STORED
        zinfo_mime.external_attr = 0o644 << 16
        zinfo_mime.extra = b""
        zf.writestr(zinfo_mime, EPUB_MIMETYPE)

        # container.xml
        container_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>"""
        zf.writestr("META-INF/container.xml", container_xml)

        # content.opf
        opf_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="pub-id">test-epub-id</dc:identifier>
        <dc:title>Test Korean Book</dc:title>
        <dc:language>ko</dc:language>
        <dc:creator>Test Author</dc:creator>
    </metadata>
    <manifest>
        <item id="style" href="style.css" media-type="text/css"/>
        <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
        <item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    </manifest>
    <spine>
        <itemref idref="ch1"/>
        <itemref idref="ch2"/>
    </spine>
</package>"""
        zf.writestr("OEBPS/content.opf", opf_xml)

        # style.css
        zf.writestr("OEBPS/style.css", b"body { font-family: serif; color: #333; }")

        # chapter1.xhtml
        ch1_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title><link rel="stylesheet" href="style.css"/></head>
<body>
    <h1>Chapter 1</h1>
    <p>First paragraph in Korean.</p>
    <p>Second paragraph with <span class="highlight">inline text</span>.</p>
</body>
</html>"""
        zf.writestr("OEBPS/chapter1.xhtml", ch1_xml)

        # chapter2.xhtml
        ch2_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 2</title></head>
<body>
    <h1>Chapter 2</h1>
    <p>Third paragraph.</p>
</body>
</html>"""
        zf.writestr("OEBPS/chapter2.xhtml", ch2_xml)

    return buf.getvalue()


@pytest.fixture
def sample_epub_bytes() -> bytes:
    if callable(build_test_epub_bytes):
        try:
            data = build_test_epub_bytes()
            if isinstance(data, bytes) and len(data) > 0:
                return data
        except Exception:
            pass
    return create_minimal_test_epub()


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path)
    return TestClient(app, client=("127.0.0.1", 50000))


def test_upload_and_spine_order(client, sample_epub_bytes, tmp_path):
    # Upload valid EPUB
    resp = client.post(
        "/api/books",
        files={"file": ("sample.epub", sample_epub_bytes, "application/epub+zip")},
    )
    assert resp.status_code == 201
    book = resp.json()
    book_id = book["id"]
    assert book["title"] is not None
    assert book["chapter_count"] >= 2

    # Verify original file stored by UUID in data_dir
    stored_path = tmp_path / f"{book_id}.epub"
    assert stored_path.exists()

    # Verify chapters order in spine
    resp_ch = client.get(f"/api/books/{book_id}/chapters")
    assert resp_ch.status_code == 200
    chapters = resp_ch.json()
    assert len(chapters) == book["chapter_count"]
    for idx, ch in enumerate(chapters):
        assert ch["spine_index"] == idx
        # Ensure chapter IDs are book-unique DB UUIDs distinct from EPUB manifest IDs
        assert ch["id"] != ch["manifest_id"]
        assert len(ch["id"]) >= 16


def test_chapter_editing_persisted_after_app_restart(client, sample_epub_bytes, tmp_path):
    # Upload book
    resp = client.post(
        "/api/books",
        files={"file": ("test.epub", sample_epub_bytes, "application/epub+zip")},
    )
    book_id = resp.json()["id"]

    # Get first chapter
    chapters = client.get(f"/api/books/{book_id}/chapters").json()
    ch1_id = chapters[0]["id"]

    # Get chapter details with safe preview
    ch1_detail = client.get(f"/api/chapters/{ch1_id}").json()
    segments = ch1_detail["segments"]
    assert len(segments) > 0
    seg1 = segments[0]

    # Patch segment with source edit and translation
    edited_source = "Sửa đổi câu gốc"
    translated_text = "Bản dịch tiếng Việt"
    patch_resp = client.patch(
        f"/api/chapters/{ch1_id}",
        json={
            "segments": [
                {
                    "id": seg1["id"],
                    "source": edited_source,
                    "translation": translated_text,
                }
            ]
        },
    )
    assert patch_resp.status_code == 200
    updated_segs = patch_resp.json()["segments"]
    found_seg = next(s for s in updated_segs if s["id"] == seg1["id"])
    assert found_seg["source"] == edited_source
    assert found_seg["translation"] == translated_text

    # Restart app pointing to same data_dir
    new_app = create_app(data_dir=tmp_path)
    new_client = TestClient(new_app, client=("127.0.0.1", 50000))

    # Verify edits persisted
    reloaded_ch = new_client.get(f"/api/chapters/{ch1_id}").json()
    reloaded_seg = next(s for s in reloaded_ch["segments"] if s["id"] == seg1["id"])
    assert reloaded_seg["source"] == edited_source
    assert reloaded_seg["translation"] == translated_text


def test_glossary_crud_and_locking(client, sample_epub_bytes):
    # Upload book
    resp = client.post(
        "/api/books",
        files={"file": ("book.epub", sample_epub_bytes, "application/epub+zip")},
    )
    book_id = resp.json()["id"]

    # Create glossary item
    create_resp = client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "철수", "target": "Triết Tú", "locked": False, "notes": "Main char"},
    )
    assert create_resp.status_code == 201
    item = create_resp.json()
    item_id = item["id"]
    assert item["locked"] is False

    # List glossary
    list_resp = client.get(f"/api/books/{book_id}/glossary")
    assert len(list_resp.json()) == 1

    # Lock term via PATCH
    patch_resp = client.patch(
        f"/api/glossary/{item_id}",
        json={"locked": True, "notes": "Locked main character"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["locked"] is True

    # Confirm locked state in list
    items = client.get(f"/api/books/{book_id}/glossary").json()
    assert items[0]["locked"] is True

    # Locked terms require explicit unlock before deletion.
    assert client.delete(f"/api/glossary/{item_id}").status_code == 400
    assert client.patch(f"/api/glossary/{item_id}", json={"locked": False}).status_code == 200
    # Delete term
    del_resp = client.delete(f"/api/glossary/{item_id}")
    assert del_resp.status_code == 200
    assert client.get(f"/api/books/{book_id}/glossary").json() == []


def test_export_source_edits_and_exact_resources(client, sample_epub_bytes):
    # Upload
    resp = client.post(
        "/api/books",
        files={"file": ("novel.epub", sample_epub_bytes, "application/epub+zip")},
    )
    book_id = resp.json()["id"]

    # Edit chapter
    chapters = client.get(f"/api/books/{book_id}/chapters").json()
    ch1_id = chapters[0]["id"]
    ch1_detail = client.get(f"/api/chapters/{ch1_id}").json()
    seg = ch1_detail["segments"][0]

    client.patch(
        f"/api/chapters/{ch1_id}",
        json={
            "segments": [
                {
                    "id": seg["id"],
                    "source": "수정된 원문",
                    "translation": "Bản dịch xuất bản",
                }
            ]
        },
    )

    # Export
    export_resp = client.get(f"/api/books/{book_id}/export")
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"] == "application/epub+zip"

    # Validate exported EPUB
    exported_bytes = export_resp.content
    reparsed = parse_epub(exported_bytes)

    # Mimetype uncompressed first check
    zf = zipfile.ZipFile(io.BytesIO(exported_bytes))
    first_entry = zf.infolist()[0]
    assert first_entry.filename == "mimetype"
    assert first_entry.compress_type == zipfile.ZIP_STORED

    # Verify translations applied to chapter text
    ch1_exported_bytes = reparsed.resources[reparsed.spine_chapters[0].archive_path]
    assert "Bản dịch xuất bản".encode("utf-8") in ch1_exported_bytes

    # Verify untouched resources (CSS or other files) preserved
    orig_parsed = parse_epub(sample_epub_bytes)
    for res_path, res_bytes in orig_parsed.resources.items():
        if res_path.endswith(".css") or res_path.endswith(".png"):
            assert res_path in reparsed.resources
            assert reparsed.resources[res_path] == res_bytes


def test_bad_upload_and_404_409(client, sample_epub_bytes, tmp_path):
    # Corrupt upload -> 400
    bad_resp = client.post(
        "/api/books",
        files={"file": ("corrupt.epub", b"not a zip file", "application/epub+zip")},
    )
    assert bad_resp.status_code == 400

    # Non-existent book/chapter 404
    assert client.get("/api/books/nonexistent").status_code == 404
    assert client.get("/api/chapters/nonexistent").status_code == 404
    assert client.patch("/api/chapters/nonexistent", json={"segments": []}).status_code == 404
    assert client.delete("/api/glossary/nonexistent").status_code == 404

    # Upload valid book
    resp = client.post(
        "/api/books",
        files={"file": ("ok.epub", sample_epub_bytes, "application/epub+zip")},
    )
    book_id = resp.json()["id"]
    ch_id = client.get(f"/api/books/{book_id}/chapters").json()[0]["id"]

    # Invalid segment ID -> 404
    bad_seg_resp = client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": "invalid-seg-id", "translation": "fail"}]},
    )
    assert bad_seg_resp.status_code == 404

    # 409 Conflict when job is running
    db_path = tmp_path / "translator.db"
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, book_id, chapter_id, job_type, status, created_at, updated_at)
            VALUES (?, ?, ?, 'translation', 'running', '2026-01-01', '2026-01-01')
            """,
            (uuid.uuid4().hex, book_id, ch_id),
        )

    conflict_resp = client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": []},
    )
    assert conflict_resp.status_code == 409
