"""Tests for manual edit history and CAS restore feature.

Covers:
- Actor recorded on manual edits (PATCH /api/chapters/{id})
- History exclusion on no-op updates and revision conflicts (atomic batch rollback)
- RBAC enforcement: Viewer and Editor cannot access edit history or restore
- CAS restore conflict rejection (409) when expected_revision does not match
- Successful restore recorded as new edit with incremented revision and audit record
- Active job blocking restore operations
- Pagination and segment filtering on edit history endpoint
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    generate_csrf_token,
    hash_password,
)
from backend.app.db import (
    get_chapter,
    get_connection,
    init_db,
    list_chapter_edit_history,
    restore_segment_history,
    update_chapter_segments,
)
from backend.app.epub import EPUB_MIMETYPE
from backend.app.main import create_app


def create_test_epub_bytes() -> bytes:
    """Generate a minimal valid 2-chapter EPUB for testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zinfo = zipfile.ZipInfo("mimetype")
        zinfo.compress_type = zipfile.ZIP_STORED
        zf.writestr(zinfo, EPUB_MIMETYPE)

        zf.writestr(
            "META-INF/container.xml",
            b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>""",
        )

        zf.writestr(
            "OEBPS/content.opf",
            b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="pub-id">test-edit-history-epub</dc:identifier>
        <dc:title>Edit History Test Book</dc:title>
        <dc:language>en</dc:language>
    </metadata>
    <manifest>
        <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    </manifest>
    <spine>
        <itemref idref="ch1"/>
    </spine>
</package>""",
        )

        zf.writestr(
            "OEBPS/ch1.xhtml",
            b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
    <p id="p1">First sentence in chapter one.</p>
    <p id="p2">Second sentence in chapter one.</p>
</body>
</html>""",
        )

    return buf.getvalue()


@pytest.fixture
def temp_dir():
    """Create an isolated temporary directory for test database and artifacts."""
    with tempfile.TemporaryDirectory(prefix="hist_test_") as td:
        yield Path(td)


@pytest.fixture
def auth_env(monkeypatch):
    """Configure isolated environment with fake signed users: admin, editor, viewer."""
    users = [
        {"username": "admin_user", "password_hash": hash_password("AdminPass123!"), "role": "admin"},
        {"username": "editor_user", "password_hash": hash_password("EditorPass123!"), "role": "editor"},
        {"username": "viewer_user", "password_hash": hash_password("ViewerPass123!"), "role": "viewer"},
    ]
    secret = "test_edit_history_secret_key_32bytes_long!"
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setenv("AUTH_USERS", json.dumps(users))
    monkeypatch.setenv("AUTH_OVERRIDE_LOCAL_ONLY", "false")
    return {"users": users, "secret": secret}


def _create_authenticated_client(app, username: str, role: str, secret: str) -> tuple[TestClient, str]:
    """Helper creating a TestClient with signed session cookie and valid CSRF token."""
    client = TestClient(app)
    token = create_session_token(username, role, secret=secret)
    client.cookies[SESSION_COOKIE_NAME] = token
    csrf = generate_csrf_token(token, secret=secret)
    return client, csrf


def _setup_book_and_chapter(temp_dir: Path, secret: str) -> tuple[Any, str, str, dict[str, Any]]:
    """Initialize app in temp_dir, upload test book as admin, return (app, book_id, chapter_id, seg1)."""
    app = create_app(data_dir=temp_dir, auth_override_local_only=False, start_worker=False)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    epub_bytes = create_test_epub_bytes()
    upload_res = admin_client.post(
        "/api/books",
        files={"file": ("test.epub", epub_bytes, "application/epub+zip")},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert upload_res.status_code == 201, upload_res.text
    book_id = upload_res.json()["id"]

    chapters = admin_client.get(f"/api/books/{book_id}/chapters").json()
    ch_id = chapters[0]["id"]
    ch_detail = admin_client.get(f"/api/chapters/{ch_id}").json()
    seg1 = ch_detail["segments"][0]
    return app, book_id, ch_id, seg1


def test_actor_recorded_on_patch(temp_dir: Path, auth_env):
    """Authenticated editor and admin usernames are accurately recorded as actor in edit history."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)

    editor_client, editor_csrf = _create_authenticated_client(app, "editor_user", "editor", secret)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    # 1. Editor updates segment
    res1 = editor_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Bản dịch của editor"}]},
        headers={"X-CSRF-Token": editor_csrf},
    )
    assert res1.status_code == 200

    # 2. Admin retrieves edit history
    hist_res = admin_client.get(f"/api/chapters/{ch_id}/edit-history")
    assert hist_res.status_code == 200
    data = hist_res.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["actor"] == "editor_user"
    assert item["actor_username"] == "editor_user"
    assert item["old_translation"] is None
    assert item["new_translation"] == "Bản dịch của editor"
    assert item["old_revision"] == 1
    assert item["new_revision"] == 2

    # 3. Admin updates segment to revision 3
    res2 = admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 2, "translation": "Bản dịch đã duyệt bởi admin"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert res2.status_code == 200

    hist_res2 = admin_client.get(f"/api/chapters/{ch_id}/edit-history")
    assert hist_res2.status_code == 200
    items2 = hist_res2.json()["items"]
    assert len(items2) == 2
    assert items2[0]["actor"] == "admin_user"
    assert items2[0]["old_translation"] == "Bản dịch của editor"
    assert items2[0]["new_translation"] == "Bản dịch đã duyệt bởi admin"
    assert items2[0]["old_revision"] == 2
    assert items2[0]["new_revision"] == 3


def test_history_exclusion_on_noop_and_conflict(temp_dir: Path, auth_env):
    """No edit history is recorded for no-op changes or conflict-aborted batches."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    # Initially, edit history is empty
    hist_0 = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()
    assert hist_0["total"] == 0

    # 1. No-op update: send the exact current source and null translation
    noop_res = admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "source": seg1["source"]}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert noop_res.status_code == 200
    hist_after_noop = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()
    assert hist_after_noop["total"] == 0, "No-op update must not record history"

    # 2. Legitimate update to revision 2
    admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Hợp lệ"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["total"] == 1

    # Another no-op with same translation
    noop_res2 = admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 2, "translation": "Hợp lệ"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert noop_res2.status_code == 200
    assert admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["total"] == 1

    # 3. Conflict in batch: seg1 has stale revision
    conflict_res = admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Xung đột"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert conflict_res.status_code == 409
    assert admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["total"] == 1


def test_viewer_and_editor_cannot_access_or_restore(temp_dir: Path, auth_env):
    """Viewer and Editor are forbidden from GET edit-history and POST restore."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)

    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)
    editor_client, editor_csrf = _create_authenticated_client(app, "editor_user", "editor", secret)
    viewer_client, viewer_csrf = _create_authenticated_client(app, "viewer_user", "viewer", secret)
    anon_client = TestClient(app)

    # Create a history entry via admin
    patch_res = admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Bản dịch 1"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert patch_res.status_code == 200
    hist_list = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["items"]
    hist_id = hist_list[0]["id"]

    # Anonymous denied (401)
    assert anon_client.get(f"/api/chapters/{ch_id}/edit-history").status_code == 401
    assert anon_client.post(f"/api/chapters/{ch_id}/edit-history/{hist_id}/restore", json={"expected_revision": 2}).status_code == 401

    # Viewer denied (403)
    viewer_get = viewer_client.get(f"/api/chapters/{ch_id}/edit-history")
    assert viewer_get.status_code == 403

    viewer_restore = viewer_client.post(
        f"/api/chapters/{ch_id}/edit-history/{hist_id}/restore",
        json={"expected_revision": 2},
        headers={"X-CSRF-Token": viewer_csrf},
    )
    assert viewer_restore.status_code == 403

    # Editor denied on GET (403: explicitly enforced by endpoint check)
    editor_get = editor_client.get(f"/api/chapters/{ch_id}/edit-history")
    assert editor_get.status_code == 403

    # Editor denied on POST restore (403: rejected by middleware & endpoint)
    editor_restore = editor_client.post(
        f"/api/chapters/{ch_id}/edit-history/{hist_id}/restore",
        json={"expected_revision": 2},
        headers={"X-CSRF-Token": editor_csrf},
    )
    assert editor_restore.status_code == 403


def test_cas_restore_conflict(temp_dir: Path, auth_env):
    """Restore with stale expected_revision is rejected (409) and database remains untouched."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    # Rev 1 -> Rev 2
    admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Bước một"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )

    # Rev 2 -> Rev 3
    admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 2, "translation": "Bước hai"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )

    hist_entries = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["items"]
    # hist_entries[1] is the edit from None to "Bước một" (old_revision=1, new_revision=2)
    entry_to_restore = hist_entries[1]

    # Current revision is 3. Attempt restore claiming expected_revision = 2 (stale CAS)
    conflict_res = admin_client.post(
        f"/api/chapters/{ch_id}/edit-history/{entry_to_restore['id']}/restore",
        json={"expected_revision": 2},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert conflict_res.status_code == 409
    conflicts = conflict_res.json()["detail"]["conflicts"]
    assert conflicts[0]["expected_revision"] == 2
    assert conflicts[0]["current_revision"] == 3

    # Verify segment was untouched
    current_ch = admin_client.get(f"/api/chapters/{ch_id}").json()
    curr_seg = next(s for s in current_ch["segments"] if s["id"] == seg1["id"])
    assert curr_seg["revision"] == 3
    assert curr_seg["translation"] == "Bước hai"


def test_successful_restore_recorded(temp_dir: Path, auth_env):
    """Successful restore applies historical before-content as a new edit with incremented revision."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    # 1. Edit from untranslated (None) to "Bản dịch ban đầu" (revision 2)
    admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Bản dịch ban đầu"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )

    # 2. Edit to "Bản dịch bị lỗi" (revision 3)
    admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 2, "translation": "Bản dịch bị lỗi"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )

    hist_items = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["items"]
    assert len(hist_items) == 2
    # The entry recording the change TO "Bản dịch bị lỗi" has old_translation="Bản dịch ban đầu"
    entry_to_restore = hist_items[0]
    assert entry_to_restore["old_translation"] == "Bản dịch ban đầu"
    assert entry_to_restore["new_translation"] == "Bản dịch bị lỗi"

    # 3. Restore historical before-content ("Bản dịch ban đầu") using expected_revision = 3
    restore_res = admin_client.post(
        f"/api/chapters/{ch_id}/edit-history/{entry_to_restore['id']}/restore",
        json={"expected_revision": 3},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert restore_res.status_code == 200, restore_res.text
    res_json = restore_res.json()
    assert res_json["status"] == "ok"
    assert res_json["revision"] == 4

    # Verify segment in database
    refreshed_ch = admin_client.get(f"/api/chapters/{ch_id}").json()
    refreshed_seg = next(s for s in refreshed_ch["segments"] if s["id"] == seg1["id"])
    assert refreshed_seg["translation"] == "Bản dịch ban đầu"
    assert refreshed_seg["revision"] == 4

    # Verify new edit history record was created for the restoration
    hist_after = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["items"]
    assert len(hist_after) == 3
    newest_hist = hist_after[0]
    assert newest_hist["actor"] == "admin_user"
    assert newest_hist["old_translation"] == "Bản dịch bị lỗi"
    assert newest_hist["new_translation"] == "Bản dịch ban đầu"
    assert newest_hist["old_revision"] == 3
    assert newest_hist["new_revision"] == 4


def test_restore_blocked_when_job_active(temp_dir: Path, auth_env):
    """Restore is blocked (409) if an active job is running on the book."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    # Create history entry
    admin_client.patch(
        f"/api/chapters/{ch_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Tạm thời"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    hist_id = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["items"][0]["id"]

    # Insert fake running job directly into DB
    with get_connection(app.state.db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, book_id, chapter_id, job_type, status, created_at, updated_at)
            VALUES ('job-1', ?, ?, 'translation', 'running', '2026-11-06T00:00:00', '2026-11-06T00:00:00')
            """,
            (book_id, ch_id),
        )

    # Attempt restore while job is running -> 409
    res = admin_client.post(
        f"/api/chapters/{ch_id}/edit-history/{hist_id}/restore",
        json={"expected_revision": 2},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert res.status_code == 409
    assert "tác vụ đang chạy" in res.json()["detail"]


def test_pagination_and_segment_filter(temp_dir: Path, auth_env):
    """Pagination parameters and segment_id filtering work accurately."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    ch_detail = admin_client.get(f"/api/chapters/{ch_id}").json()
    seg2 = ch_detail["segments"][1]

    # Create 3 edits on seg1 and 2 edits on seg2
    admin_client.patch(f"/api/chapters/{ch_id}", json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "S1-A"}]}, headers={"X-CSRF-Token": admin_csrf})
    admin_client.patch(f"/api/chapters/{ch_id}", json={"segments": [{"id": seg2["id"], "expected_revision": 1, "translation": "S2-A"}]}, headers={"X-CSRF-Token": admin_csrf})
    admin_client.patch(f"/api/chapters/{ch_id}", json={"segments": [{"id": seg1["id"], "expected_revision": 2, "translation": "S1-B"}]}, headers={"X-CSRF-Token": admin_csrf})
    admin_client.patch(f"/api/chapters/{ch_id}", json={"segments": [{"id": seg2["id"], "expected_revision": 2, "translation": "S2-B"}]}, headers={"X-CSRF-Token": admin_csrf})
    admin_client.patch(f"/api/chapters/{ch_id}", json={"segments": [{"id": seg1["id"], "expected_revision": 3, "translation": "S1-C"}]}, headers={"X-CSRF-Token": admin_csrf})

    # Total entries for chapter = 5
    all_res = admin_client.get(f"/api/chapters/{ch_id}/edit-history?limit=10").json()
    assert all_res["total"] == 5
    assert len(all_res["items"]) == 5

    # Filter by seg1 only -> 3
    seg1_res = admin_client.get(f"/api/chapters/{ch_id}/edit-history?segment_id={seg1['id']}").json()
    assert seg1_res["total"] == 3
    assert all(item["segment_id"] == seg1["id"] for item in seg1_res["items"])

    # Filter by seg2 only -> 2
    seg2_res = admin_client.get(f"/api/chapters/{ch_id}/edit-history?segment_id={seg2['id']}").json()
    assert seg2_res["total"] == 2
    assert all(item["segment_id"] == seg2["id"] for item in seg2_res["items"])

    # Test limit and offset
    page1 = admin_client.get(f"/api/chapters/{ch_id}/edit-history?limit=2&offset=0").json()
    assert len(page1["items"]) == 2
    page2 = admin_client.get(f"/api/chapters/{ch_id}/edit-history?limit=2&offset=2").json()
    assert len(page2["items"]) == 2
    assert page1["items"][0]["id"] != page2["items"][0]["id"]


def test_restore_alt_route_and_noop_rejection(temp_dir: Path, auth_env):
    """Restore via alternative /restore body route works, and restoring already identical content is rejected (400)."""
    secret = auth_env["secret"]
    app, book_id, ch_id, seg1 = _setup_book_and_chapter(temp_dir, secret)
    admin_client, admin_csrf = _create_authenticated_client(app, "admin_user", "admin", secret)

    # Edit from None to "Original" (rev 2)
    admin_client.patch(f"/api/chapters/{ch_id}", json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Original"}]}, headers={"X-CSRF-Token": admin_csrf})
    # Edit from "Original" to "Changed" (rev 3)
    admin_client.patch(f"/api/chapters/{ch_id}", json={"segments": [{"id": seg1["id"], "expected_revision": 2, "translation": "Changed"}]}, headers={"X-CSRF-Token": admin_csrf})

    hist_items = admin_client.get(f"/api/chapters/{ch_id}/edit-history").json()["items"]
    entry_to_restore = hist_items[0]

    # 1. Restore using POST /api/chapters/{chapter_id}/edit-history/restore with body history_id
    alt_restore_res = admin_client.post(
        f"/api/chapters/{ch_id}/edit-history/restore",
        json={"history_id": entry_to_restore["id"], "expected_revision": 3},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert alt_restore_res.status_code == 200
    assert alt_restore_res.json()["revision"] == 4

    # 2. Attempting to restore the same before-content again when segment is already at that content -> 400
    noop_restore = admin_client.post(
        f"/api/chapters/{ch_id}/edit-history/restore",
        json={"history_id": entry_to_restore["id"], "expected_revision": 4},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert noop_restore.status_code == 400
    assert "đã ở trạng thái trước khi sửa" in noop_restore.json()["detail"]
