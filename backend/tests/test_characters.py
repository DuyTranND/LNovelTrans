"""Automated tests for backwards-compatible structured book-scoped character profiles."""

from __future__ import annotations

import io
import uuid
import zipfile
import pytest
from fastapi.testclient import TestClient

from backend.app.db import (
    create_character,
    delete_character,
    find_character_by_source_term,
    get_character,
    get_connection,
    init_db,
    list_characters,
    update_character,
    create_glossary_item,
    list_glossary,
)
from backend.app.epub import EPUB_MIMETYPE
from backend.app.main import create_app


def create_minimal_test_epub() -> bytes:
    """Generate a valid minimal test EPUB."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zinfo_mime = zipfile.ZipInfo("mimetype")
        zinfo_mime.compress_type = zipfile.ZIP_STORED
        zinfo_mime.external_attr = 0o644 << 16
        zinfo_mime.extra = b""
        zf.writestr(zinfo_mime, EPUB_MIMETYPE)

        container_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>"""
        zf.writestr("META-INF/container.xml", container_xml)

        opf_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="pub-id">test-char-epub</dc:identifier>
        <dc:title>Test Korean Novel</dc:title>
        <dc:language>ko</dc:language>
        <dc:creator>Author</dc:creator>
    </metadata>
    <manifest>
        <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    </manifest>
    <spine>
        <itemref idref="ch1"/>
    </spine>
</package>"""
        zf.writestr("OEBPS/content.opf", opf_xml)

        ch1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
    <p>이진우는 그림자 군주였다. 진우는 차해인을 바라보았다.</p>
</body>
</html>""".encode("utf-8")
        zf.writestr("OEBPS/chapter1.xhtml", ch1_xml)

    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path)
    return TestClient(app)


def test_character_db_crud_and_validation(tmp_path):
    """Test direct DB functions for character profiles."""
    db_path = tmp_path / "test_char.db"
    init_db(db_path)

    book_id = "book_1"
    with get_connection(db_path) as conn:
        conn.execute("INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, 'dummy1.epub', 'now', 'now')", (book_id, "Book 1"))
        conn.execute("INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, 'dummy2.epub', 'now', 'now')", ("book_2", "Book 2"))
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ('chap_1', 'book_1', 'ch1', 0, 'OEBPS/ch1.xhtml', 'ch1.xhtml', 'Chapter 1', 'application/xhtml+xml', b'', 'untranslated', 'now', 'now')
        )

    # Validation: empty source or target
    with pytest.raises(ValueError, match="Tên nguồn"):
        create_character(db_path, book_id=book_id, canonical_source="", canonical_target="Lee Jin-woo")

    with pytest.raises(ValueError, match="Tên dịch"):
        create_character(db_path, book_id=book_id, canonical_source="이진우", canonical_target="")

    # Create character
    char1 = create_character(
        db_path,
        book_id=book_id,
        canonical_source="이진우",
        canonical_target="Lee Jin-woo",
        source_aliases=["진우", "그림자 군주"],
        gender="male",
        speech_style="Lạnh lùng",
        relationships="Nhân vật chính",
        first_occurrence_chapter_id="chap_1",
        first_occurrence_chapter_index=0,
        locked=False,
    )
    assert char1["id"] is not None
    assert char1["canonical_source"] == "이진우"
    assert char1["canonical_target"] == "Lee Jin-woo"
    assert char1["source_aliases"] == ["진우", "그림자 군주"]
    assert char1["gender"] == "male"
    assert char1["locked"] is False

    # Duplicate canonical_source in same book should raise ValueError
    with pytest.raises(ValueError, match="đã tồn tại"):
        create_character(
            db_path,
            book_id=book_id,
            canonical_source=" 이진우 ",
            canonical_target="Sung Jin-woo",
        )

    # Same source in DIFFERENT book is allowed
    char_other_book = create_character(
        db_path,
        book_id="book_2",
        canonical_source="이진우",
        canonical_target="Lee Jin-woo",
    )
    assert char_other_book["book_id"] == "book_2"

    # Update character
    updated = update_character(
        db_path,
        char1["id"],
        canonical_target="Sung Jin-woo",
        gender="MALE",  # will normalize to lowercase
        locked=True,
    )
    assert updated["canonical_target"] == "Sung Jin-woo"
    assert updated["gender"] == "male"
    assert updated["locked"] is True

    # Attempting substantive update to locked character without unlocking should fail
    with pytest.raises(ValueError, match="Không thể chỉnh sửa nhân vật đã khóa"):
        update_character(
            db_path,
            char1["id"],
            canonical_target="Should Fail",
        )

    # Unlocking and updating in same call or explicitly setting locked=False succeeds
    unlocked = update_character(
        db_path,
        char1["id"],
        canonical_target="Sung Jin-woo (Final)",
        locked=False,
    )
    assert unlocked["canonical_target"] == "Sung Jin-woo (Final)"
    assert unlocked["locked"] is False

    # Locked delete protection
    update_character(db_path, char1["id"], locked=True)
    with pytest.raises(ValueError, match="Không thể xóa nhân vật đã khóa"):
        delete_character(db_path, char1["id"])

    # Unlock and delete
    update_character(db_path, char1["id"], locked=False)
    del_res = delete_character(db_path, char1["id"])
    assert del_res is True
    assert get_character(db_path, char1["id"]) is None


def test_korean_boundary_lookup(tmp_path):
    """Test Hangul particle and boundary awareness to prevent false substring collisions."""
    db_path = tmp_path / "test_char_korean.db"
    init_db(db_path)

    book_id = "book_kr"
    with get_connection(db_path) as conn:
        conn.execute("INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, 'dummy_kr.epub', 'now', 'now')", (book_id, "Book KR"))
    create_character(
        db_path,
        book_id=book_id,
        canonical_source="이진우",
        canonical_target="Lee Jin-woo",
        source_aliases=["진우"],
        gender="male",
    )

    # 1. Exact match
    match1 = find_character_by_source_term(db_path, book_id, "이진우")
    assert match1 is not None
    assert match1["canonical_source"] == "이진우"

    # 2. Match via alias
    match2 = find_character_by_source_term(db_path, book_id, "진우")
    assert match2 is not None
    assert match2["canonical_source"] == "이진우"

    # 3. Match with Korean particle "이진우는"
    match3 = find_character_by_source_term(db_path, book_id, "이진우는")
    assert match3 is not None
    assert match3["canonical_source"] == "이진우"

    # 4. Anti-false-collision: "진" inside "이진우" should NOT match!
    # "진" is just a syllable inside "이진우", not a character headword.
    match_false = find_character_by_source_term(db_path, book_id, "진")
    assert match_false is None


def test_character_api_endpoints(client, tmp_path):
    """Test full REST API CRUD for characters."""
    # 1. Upload EPUB to get a valid book_id
    epub_bytes = create_minimal_test_epub()
    upload_res = client.post(
        "/api/books",
        files={"file": ("novel.epub", epub_bytes, "application/epub+zip")},
    )
    assert upload_res.status_code == 201
    book_id = upload_res.json()["id"]

    # 2. List characters (initially empty)
    list_res = client.get(f"/api/books/{book_id}/characters")
    assert list_res.status_code == 200
    assert list_res.json() == []

    # 3. Create character
    create_payload = {
        "canonical_source": "차해인",
        "canonical_target": "Cha Hae-in",
        "source_aliases": ["해인"],
        "gender": "female",
        "speech_style": "Lịch sự",
        "relationships": "Thợ săn cấp S",
        "locked": False,
    }
    create_res = client.post(f"/api/books/{book_id}/characters", json=create_payload)
    assert create_res.status_code == 201
    char_data = create_res.json()
    char_id = char_data["id"]
    assert char_data["canonical_source"] == "차해인"
    assert char_data["canonical_target"] == "Cha Hae-in"
    assert char_data["gender"] == "female"

    # 4. Create duplicate -> 409 Conflict
    dup_res = client.post(f"/api/books/{book_id}/characters", json=create_payload)
    assert dup_res.status_code == 409

    # 5. Get character by ID
    get_res = client.get(f"/api/characters/{char_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == char_id

    # 6. Lock character via PATCH
    lock_res = client.patch(f"/api/characters/{char_id}", json={"locked": True})
    assert lock_res.status_code == 200
    assert lock_res.json()["locked"] is True

    # 7. Edit locked character -> 400 Bad Request
    fail_edit_res = client.patch(
        f"/api/characters/{char_id}",
        json={"canonical_target": "Cha Hae In (Modified)"},
    )
    assert fail_edit_res.status_code == 400

    # 8. Unlock and edit
    ok_edit_res = client.patch(
        f"/api/characters/{char_id}",
        json={"canonical_target": "Cha Hae-in (Hunter)", "locked": False},
    )
    assert ok_edit_res.status_code == 200
    assert ok_edit_res.json()["canonical_target"] == "Cha Hae-in (Hunter)"

    # 9. Verify glossary compatibility: glossary operations work without interference
    gloss_create = client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "그림자 군주", "target": "Chúa Tể Bóng Tối", "locked": True},
    )
    assert gloss_create.status_code == 201

    gloss_list = client.get(f"/api/books/{book_id}/glossary")
    assert gloss_list.status_code == 200
    assert len(gloss_list.json()) == 1
    assert gloss_list.json()[0]["source"] == "그림자 군주"

    # 10. Delete character
    del_res = client.delete(f"/api/characters/{char_id}")
    assert del_res.status_code == 200
    assert del_res.json() == {"status": "deleted", "id": char_id}
