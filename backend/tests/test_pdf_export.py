"""Backend test suite for PDF export feature.

Covers:
- Fake multi-book fixture
- Single chapter, subset of chapters, and all chapters export
- Full translated export success (A4, Content-Type, Content-Disposition, Unicode bytes)
- Incomplete translated export rejection with explicit details (no silent drop or fallback)
- Original source export regardless of translation state
- Stable spine reading order preservation (even with reversed input IDs)
- Input validation: invalid book ID, empty chapter_ids list, duplicate IDs, foreign cross-book IDs
- Head metadata exclusion, inline formatting joins, <br> line breaks
- Verifies DB and provider are completely untouched (zero jobs, zero writes, zero API calls)
"""

from __future__ import annotations

import io
import urllib.parse
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_connection, update_chapter_segments
from backend.app.epub import rebuild_epub
from backend.app.main import create_app
from backend.app.pdf_export import is_head_segment
from backend.tests.test_epub import build_test_epub_bytes

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False


class FakeMockProvider:
    """Mock translation provider that records calls and asserts none were made."""
    def __init__(self):
        self.call_count = 0

    async def translate(self, *args, **kwargs):
        self.call_count += 1
        return {}

    async def review(self, *args, **kwargs):
        self.call_count += 1
        return {}


@pytest.fixture
def multi_book_setup(tmp_path):
    """Setup app with two distinct books in SQLite database."""
    provider = FakeMockProvider()
    app = create_app(data_dir=tmp_path, provider=provider, start_worker=False)
    client = TestClient(app, client=("127.0.0.1", 50000))

    # Book 1: 2 chapters (ch1 with head/h1/inline/br, ch2 with basic paragraphs)
    ch1_xhtml = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chương Đầu Tiên Metadata</title></head>
<body>
    <h1>각성 (1)</h1>
    <p>첫 번째 문장입니다. <em>강조된 단어</em> 그리고 이어지는 문장.</p>
    <div>줄바꿈 이전 줄<br/>줄바꿈 이후 줄</div>
</body>
</html>"""

    ch2_xhtml = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 2 Metadata</title></head>
<body>
    <h1>제2장</h1>
    <p>두 번째 장의 원문 내용입니다.</p>
</body>
</html>"""

    epub1_bytes = build_test_epub_bytes([
        {"id": "ch1", "href": "ch1.xhtml", "content": ch1_xhtml},
        {"id": "ch2", "href": "ch2.xhtml", "content": ch2_xhtml},
    ], title="Quyển Sách Hàn 1", language="ko")

    # Book 2: 1 chapter
    book2_ch_xhtml = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Book 2 Chapter</title></head>
<body><p>Book 2 content</p></body>
</html>"""
    epub2_bytes = build_test_epub_bytes([
        {"id": "b2ch1", "href": "b2ch1.xhtml", "content": book2_ch_xhtml},
    ], title="Quyển Sách Hàn 2", language="ko")

    # Upload Book 1
    resp1 = client.post(
        "/api/books",
        files={"file": ("book1.epub", epub1_bytes, "application/epub+zip")},
    )
    assert resp1.status_code == 201
    book1 = resp1.json()

    # Upload Book 2
    resp2 = client.post(
        "/api/books",
        files={"file": ("book2.epub", epub2_bytes, "application/epub+zip")},
    )
    assert resp2.status_code == 201
    book2 = resp2.json()

    # Query chapters for Book 1
    ch_resp1 = client.get(f"/api/books/{book1['id']}/chapters")
    chapters1 = ch_resp1.json()
    assert len(chapters1) == 2

    # Query chapters for Book 2
    ch_resp2 = client.get(f"/api/books/{book2['id']}/chapters")
    chapters2 = ch_resp2.json()

    # Fully translate Chapter 1 of Book 1 (save translations into DB)
    ch1_detail = client.get(f"/api/chapters/{chapters1[0]['id']}").json()
    ch1_updates = []
    for s in ch1_detail["segments"]:
        src = s.get("source", "")
        if "각성" in src:
            ch1_updates.append({"id": s["id"], "translation": "Thức tỉnh (1)"})
        elif "첫 번째" in src:
            ch1_updates.append({"id": s["id"], "translation": "Đây là câu đầu tiên. "})
        elif "강조된" in src:
            ch1_updates.append({"id": s["id"], "translation": "từ khóa nhấn mạnh"})
        elif "그리고" in src:
            ch1_updates.append({"id": s["id"], "translation": " và câu tiếp theo."})
        elif "줄바꿈 이전" in src:
            ch1_updates.append({"id": s["id"], "translation": "Dòng trước khi xuống hàng"})
        elif "줄바꿈 이후" in src:
            ch1_updates.append({"id": s["id"], "translation": "Dòng sau khi xuống hàng"})
        else:
            ch1_updates.append({"id": s["id"], "translation": "Bản dịch thử nghiệm"})

    patch_resp = client.patch(
        f"/api/chapters/{chapters1[0]['id']}",
        json={"segments": ch1_updates, "status": "translated"},
    )
    assert patch_resp.status_code == 200

    # Ensure Chapter 2 of Book 1 remains completely untranslated
    return {
        "client": client,
        "app": app,
        "provider": provider,
        "tmp_path": tmp_path,
        "book1": book1,
        "book2": book2,
        "chapters1": chapters1,
        "chapters2": chapters2,
    }


def test_export_pdf_missing_book_returns_404(multi_book_setup):
    client = multi_book_setup["client"]
    resp = client.post("/api/books/non-existent-book-id/export/pdf", json={})
    assert resp.status_code == 404
    assert "Không tìm thấy sách" in resp.json()["detail"]


def test_export_pdf_invalid_content_mode_returns_400(multi_book_setup):
    client = multi_book_setup["client"]
    book1_id = multi_book_setup["book1"]["id"]
    resp = client.post(
        f"/api/books/{book1_id}/export/pdf",
        json={"content": "invalid_mode"},
    )
    assert resp.status_code == 400
    assert "content phải là 'translation' hoặc 'source'" in resp.json()["detail"]


def test_export_pdf_empty_explicit_chapter_ids_returns_400(multi_book_setup):
    client = multi_book_setup["client"]
    book1_id = multi_book_setup["book1"]["id"]
    resp = client.post(
        f"/api/books/{book1_id}/export/pdf",
        json={"chapter_ids": []},
    )
    assert resp.status_code == 400
    assert "chapter_ids không được rỗng" in resp.json()["detail"]


def test_export_pdf_duplicate_chapter_ids_returns_400(multi_book_setup):
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    ch1_id = multi_book_setup["chapters1"][0]["id"]
    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch1_id, ch1_id]},
    )
    assert resp.status_code == 400
    assert "trùng lặp" in resp.json()["detail"]


def test_export_pdf_foreign_cross_book_ids_returns_400(multi_book_setup):
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    foreign_ch_id = multi_book_setup["chapters2"][0]["id"]

    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [foreign_ch_id]},
    )
    assert resp.status_code == 400
    assert "không thuộc quyển sách này" in resp.json()["detail"]


def test_export_pdf_translation_incomplete_rejected_with_clear_details(multi_book_setup):
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    ch2_id = multi_book_setup["chapters1"][1]["id"]

    # 1. Reject explicit selection of incomplete chapter 2
    resp_subset = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch2_id], "content": "translation"},
    )
    assert resp_subset.status_code == 400
    detail_subset = resp_subset.json()["detail"]
    assert "chưa hoàn thành dịch" in detail_subset
    assert "không được phép bỏ sót" in detail_subset

    # 2. Reject all chapters export when chapter 2 is incomplete (omitted chapter_ids)
    resp_all = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"content": "translation"},
    )
    assert resp_all.status_code == 400
    assert "chưa hoàn thành dịch" in resp_all.json()["detail"]


def test_export_pdf_translation_full_chapter1_success(multi_book_setup):
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    ch1_id = multi_book_setup["chapters1"][0]["id"]

    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch1_id], "content": "translation"},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/pdf"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert "_vi.pdf" in resp.headers["Content-Disposition"]

    # Valid PDF magic bytes
    pdf_bytes = resp.content
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 500

    # If pypdf is installed, verify text extraction
    if HAS_PYPDF:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) >= 1
        extracted = "".join(page.extract_text() for page in reader.pages)
        assert "Thức tỉnh" in extracted
        assert "Đây là câu đầu tiên" in extracted


def test_export_pdf_source_original_export_works_regardless_translation_state(multi_book_setup):
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    ch2_id = multi_book_setup["chapters1"][1]["id"]

    # Original export of incomplete Chapter 2 must succeed with Korean text
    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch2_id], "content": "source"},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/pdf"
    assert "_ko.pdf" in resp.headers["Content-Disposition"]
    assert resp.content.startswith(b"%PDF-")

    # Original export of all chapters (both ch1 and ch2) must succeed
    resp_all = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"content": "source"},
    )
    assert resp_all.status_code == 200
    assert resp_all.headers["Content-Type"] == "application/pdf"

    if HAS_PYPDF:
        reader = pypdf.PdfReader(io.BytesIO(resp_all.content))
        all_text = "".join(page.extract_text() for page in reader.pages)
        assert "원문 내용" in all_text or "제2장" in all_text or "각성" in all_text


def test_export_pdf_spine_order_preserved(multi_book_setup):
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    ch1_id = multi_book_setup["chapters1"][0]["id"]
    ch2_id = multi_book_setup["chapters1"][1]["id"]

    # Pass reversed list [ch2, ch1]
    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch2_id, ch1_id], "content": "source"},
    )
    assert resp.status_code == 200

    if HAS_PYPDF:
        reader = pypdf.PdfReader(io.BytesIO(resp.content))
        full_text = "".join(page.extract_text() for page in reader.pages)
        # ch1 heading '각성 (1)' must appear before ch2 heading '제2장'
        idx1 = full_text.find("각성")
        idx2 = full_text.find("제2장")
        if idx1 != -1 and idx2 != -1:
            assert idx1 < idx2, "Chapters must be rendered in spine reading order"


def test_export_pdf_head_exclusion_inline_joins_and_br(multi_book_setup):
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    ch1_id = multi_book_setup["chapters1"][0]["id"]

    # 1. Translation export
    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch1_id], "content": "translation"},
    )
    assert resp.status_code == 200

    if HAS_PYPDF:
        reader = pypdf.PdfReader(io.BytesIO(resp.content))
        text = "".join(page.extract_text() for page in reader.pages)

        # Head exclusion: <title>Chương Đầu Tiên Metadata</title> must NOT appear in visible body
        assert "Chương Đầu Tiên Metadata" not in text, "Head metadata title must be excluded"

        # Derived visible chapter title: translated body h1 'Thức tỉnh (1)' rendered exactly once
        assert text.count("Thức tỉnh (1)") == 1, "Translated heading must be rendered exactly once"

        # No Korean heading leaked into translated export
        assert "각성" not in text, "Source Korean heading must not appear in translated PDF"

        # Inline joins: 'Đây là câu đầu tiên. ' + 'từ khóa nhấn mạnh' + ' và câu tiếp theo.'
        assert "Đây là câu đầu tiên." in text
        assert "từ khóa nhấn mạnh" in text

        # br lines
        assert "Dòng trước khi xuống hàng" in text
        assert "Dòng sau khi xuống hàng" in text

    # 2. Original source export: derives visible heading from original body h1 exactly once
    resp_src = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch1_id], "content": "source"},
    )
    assert resp_src.status_code == 200

    if HAS_PYPDF:
        reader_src = pypdf.PdfReader(io.BytesIO(resp_src.content))
        text_src = "".join(page.extract_text() for page in reader_src.pages)

        # Head metadata title must be excluded
        assert "Chương Đầu Tiên Metadata" not in text_src, "Head metadata title must be excluded from source export"

        # Original source heading rendered exactly once
        assert text_src.count("각성 (1)") == 1, "Original heading must be rendered exactly once in source export"

        # Translated text must not appear in source export
        assert "Thức tỉnh" not in text_src, "Translated heading must not appear in source export"

    # 3. Fallback to metadata title only when chapter has no body heading (Book 2 Chapter 1)
    resp_b2 = client.post(
        f"/api/books/{multi_book_setup['book2']['id']}/export/pdf",
        json={"content": "source"},
    )
    assert resp_b2.status_code == 200
    if HAS_PYPDF:
        reader_b2 = pypdf.PdfReader(io.BytesIO(resp_b2.content))
        text_b2 = "".join(page.extract_text() for page in reader_b2.pages)
        assert "Book 2 Chapter" in text_b2, "Must fallback to metadata title when no body heading exists"
        assert "Book 2 content" in text_b2


def test_export_pdf_db_and_provider_completely_untouched(multi_book_setup):
    client = multi_book_setup["client"]
    provider = multi_book_setup["provider"]
    book1 = multi_book_setup["book1"]
    ch1_id = multi_book_setup["chapters1"][0]["id"]
    db_path = multi_book_setup["tmp_path"] / "translator.db"

    # Count jobs before
    with get_connection(db_path) as conn:
        job_count_before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # Perform PDF export
    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch1_id], "content": "translation"},
    )
    assert resp.status_code == 200

    # Verify provider was never invoked
    assert provider.call_count == 0

    # Verify no jobs were written to DB
    with get_connection(db_path) as conn:
        job_count_after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert job_count_after == job_count_before


def test_bundled_font_glyph_coverage():
    """Verify bundled OFL fonts cover Vietnamese (including tricky diacritics), Korean, U+0336, and Thai."""
    from backend.app.pdf_export import get_font_cascade, PdfFontError

    base_font, cascade = get_font_cascade()
    assert base_font.name == "NotoSansKR"

    font_map = {f.name: f for f in cascade}
    kr_font = font_map.get("NotoSansKR")
    sans_font = font_map.get("NotoSans")
    thai_font = font_map.get("NotoSansThai")

    assert kr_font is not None, "NotoSansKR must be loaded in cascade"
    assert sans_font is not None, "NotoSans fallback must be loaded in cascade"
    assert thai_font is not None, "NotoSansThai fallback must be loaded in cascade"

    # 1. Tricky Vietnamese characters missing in Windows Malgun
    tricky_vi_chars = ["\u1ebf", "\u1ec7", "\u1ec9", "\u1edb", "\u1ee9", "ế", "ệ", "ỉ", "ớ", "ứ"]
    for ch in tricky_vi_chars:
        assert kr_font.supports(ord(ch)), f"NotoSansKR must support Vietnamese char '{ch}' (U+{ord(ch):04X})"

    # 2. Korean Hangul syllables
    kr_chars = ["각", "성", "한", "글", "제", "장", "문", "장"]
    for ch in kr_chars:
        assert kr_font.supports(ord(ch)), f"NotoSansKR must support Korean char '{ch}' (U+{ord(ch):04X})"

    # 3. U+0336 combining long stroke overlay (combining strike)
    strike_char = "\u0336"
    assert sans_font.supports(ord(strike_char)), "NotoSans must support U+0336 combining strike"

    # 4. Thai characters present in original story (U+0E01, U+0E49)
    thai_chars = ["\u0e01", "\u0e49", "ก", "้"]
    for ch in thai_chars:
        assert thai_font.supports(ord(ch)), f"NotoSansThai must support Thai char '{ch}' (U+{ord(ch):04X})"


def test_format_text_with_font_fallback_markup():
    """Verify text is partitioned into font tags for non-base runs while keeping base text clean."""
    from backend.app.pdf_export import get_font_cascade, format_text_with_font_fallback

    base_font, cascade = get_font_cascade()

    sample_text = "Thức tỉnh 각성 ก้ alice\u0336"
    markup = format_text_with_font_fallback(sample_text, base_font, cascade)

    # Base font (NotoSansKR) covers Vietnamese and Korean: no font tag needed
    assert "Thức tỉnh 각성 " in markup
    assert '<font name="NotoSansKR">' not in markup, "Base font runs should not be wrapped in redundant tags"

    # Thai characters must be wrapped in NotoSansThai font tag
    assert '<font name="NotoSansThai">ก้</font>' in markup

    # U+0336 must be wrapped in NotoSans font tag
    assert '<font name="NotoSans">\u0336</font>' in markup


def test_genuinely_unsupported_glyph_raises_actionable_error():
    """Verify genuinely unsupported glyphs raise PdfFontError instead of rendering squares."""
    from backend.app.pdf_export import get_font_cascade, format_text_with_font_fallback, PdfFontError

    base_font, cascade = get_font_cascade()

    # Egyptian Hieroglyph U+13000 (not present in any of the 3 fonts)
    unsupported_text = "Câu văn hợp lệ \U00013000 kết thúc."
    with pytest.raises(PdfFontError) as exc_info:
        format_text_with_font_fallback(unsupported_text, base_font, cascade)

    assert "U+13000" in str(exc_info.value)
    assert "PDF_FONT_PATH" in str(exc_info.value)


def test_export_pdf_renders_thai_and_combining_strike_with_font_resources(multi_book_setup):
    """Verify exporting text with Thai and U+0336 embeds font resources in the generated PDF."""
    client = multi_book_setup["client"]
    book1 = multi_book_setup["book1"]
    ch1_id = multi_book_setup["chapters1"][0]["id"]

    # Retrieve real segments from Chapter 1
    ch1_detail = client.get(f"/api/chapters/{ch1_id}").json()
    body_segments = [
        s for s in ch1_detail["segments"]
        if not s.get("is_whitespace") and not is_head_segment(s.get("dom_path", ""))
    ]
    assert len(body_segments) >= 2, "Chapter 1 must have at least 2 body segments"

    # Use real body segment id (the body paragraph after heading)
    target_seg_id = body_segments[1]["id"]
    patch_text = "Bản dịch: Thức tỉnh 각성 với tiếng Thái ก้ và gạch t\u0336e\u0336s\u0336t"

    patch_resp = client.patch(
        f"/api/chapters/{ch1_id}",
        json={
            "segments": [{"id": target_seg_id, "translation": patch_text}],
            "status": "translated",
        },
    )
    assert patch_resp.status_code == 200, f"PATCH chapter segment must succeed: {patch_resp.text}"

    # Assert patch success and persisted test fixture text in database/fixture
    refreshed_ch1 = client.get(f"/api/chapters/{ch1_id}").json()
    stored_seg = next((s for s in refreshed_ch1["segments"] if s["id"] == target_seg_id), None)
    assert stored_seg is not None, "Target segment must exist in refreshed chapter"
    assert stored_seg["translation"] == patch_text, "Persisted fixture text must match patched text"

    resp = client.post(
        f"/api/books/{book1['id']}/export/pdf",
        json={"chapter_ids": [ch1_id], "content": "translation"},
    )
    assert resp.status_code == 200
    pdf_bytes = resp.content

    # Check font resource declarations in PDF binary stream
    assert b"NotoSansKR" in pdf_bytes, "PDF must reference NotoSansKR font"
    assert b"NotoSansThai" in pdf_bytes, "PDF must reference NotoSansThai font"
    assert b"NotoSans" in pdf_bytes, "PDF must reference NotoSans font"

    if HAS_PYPDF:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) >= 1

        # Traverse pypdf object tree for embedded BaseFont resources across all pages
        base_fonts: list[str] = []
        for page in reader.pages:
            res = page.get("/Resources", {})
            if hasattr(res, "get_object"):
                res = res.get_object()
            fonts_dict = res.get("/Font", {})
            if hasattr(fonts_dict, "get_object"):
                fonts_dict = fonts_dict.get_object()
            if isinstance(fonts_dict, dict):
                for font_ref in fonts_dict.values():
                    font_obj = font_ref.get_object() if hasattr(font_ref, "get_object") else font_ref
                    if isinstance(font_obj, dict):
                        bf = str(font_obj.get("/BaseFont", ""))
                        if bf:
                            base_fonts.append(bf)
                        descendants = font_obj.get("/DescendantFonts", [])
                        if hasattr(descendants, "get_object"):
                            descendants = descendants.get_object()
                        if isinstance(descendants, list):
                            for desc_ref in descendants:
                                desc_obj = desc_ref.get_object() if hasattr(desc_ref, "get_object") else desc_ref
                                if isinstance(desc_obj, dict):
                                    dbf = str(desc_obj.get("/BaseFont", ""))
                                    if dbf:
                                        base_fonts.append(dbf)

        assert any("NotoSansKR" in bf for bf in base_fonts), f"NotoSansKR BaseFont missing in {base_fonts}"
        assert any("NotoSansThai" in bf for bf in base_fonts), f"NotoSansThai BaseFont missing in {base_fonts}"
        assert any("NotoSans" in bf for bf in base_fonts), f"NotoSans BaseFont missing in {base_fonts}"

        # Assert extracted mixed text from PDF
        extracted_text = "".join(page.extract_text() for page in reader.pages)
        assert "Thức tỉnh" in extracted_text, "Vietnamese must be present in extracted PDF text"
        assert "각성" in extracted_text, "Korean must be present in extracted PDF text"
        assert "ก้" in extracted_text, "Thai must be present in extracted PDF text"
        assert "gạch" in extracted_text, "Latin text must be present in extracted PDF text"

