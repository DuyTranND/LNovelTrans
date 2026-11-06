"""Targeted tests for secure multi-editor collaboration and optimistic concurrency.

Covers:
- Fail-closed anonymous access (401)
- Public health endpoint (/api/health)
- User authentication and session cookie issuance
- CSRF protection on state changes (403 on missing/invalid CSRF token)
- Role-based access control:
    - Viewer read-only (403 on any state change)
    - Editor allowed chapter/glossary edits, denied paid jobs/settings/uploads
    - Admin full access
- Per-segment numeric revision optimistic concurrency (CAS):
    - Rejection of stale expected_revision (409 Conflict)
    - Conflict metadata returned in response
    - Winner edit preserved
    - Atomic batch rollback on conflict (no partial writes)
    - Missing expected_revision rejection in collaboration mode (400)
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend.app.auth import hash_password, create_session_token, generate_csrf_token, SESSION_COOKIE_NAME
from backend.app.db import get_chapter, init_db, update_chapter_segments, get_connection
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
        <dc:identifier id="pub-id">test-collab-epub</dc:identifier>
        <dc:title>Collab Test Book</dc:title>
        <dc:language>ko</dc:language>
    </metadata>
    <manifest>
        <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
        <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    </manifest>
    <spine>
        <itemref idref="ch1"/>
        <itemref idref="ch2"/>
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

        zf.writestr(
            "OEBPS/ch2.xhtml",
            b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 2</title></head>
<body>
    <p id="p1">First sentence in chapter two.</p>
</body>
</html>""",
        )

    return buf.getvalue()


@pytest.fixture
def auth_env(monkeypatch):
    """Configure isolated environment with admin, editor, and viewer."""
    users = [
        {"username": "admin_user", "password_hash": hash_password("AdminPass123!"), "role": "admin"},
        {"username": "editor_user", "password_hash": hash_password("EditorPass123!"), "role": "editor"},
        {"username": "viewer_user", "password_hash": hash_password("ViewerPass123!"), "role": "viewer"},
    ]
    monkeypatch.setenv("SESSION_SECRET", "test_collaboration_secret_key_32bytes_long!")
    monkeypatch.setenv("AUTH_USERS", json.dumps(users))
    monkeypatch.setenv("AUTH_OVERRIDE_LOCAL_ONLY", "false")
    return users


def test_anonymous_access_fails_closed(tmp_path: Path, auth_env):
    """Default production configuration must fail closed (401) on sensitive endpoints."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    client = TestClient(app)

    # Health endpoint is public
    res_health = client.get("/api/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "ok"

    # All sensitive endpoints require authentication
    assert client.get("/api/books").status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/models").status_code == 401
    assert client.get("/api/chapters/some-chapter-id").status_code == 401
    assert client.patch("/api/chapters/some-chapter-id", json={"segments": []}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_login_logout_and_session_cookie(tmp_path: Path, auth_env):
    """Test login credentials verification, session cookie setting, and logout clearing."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    client = TestClient(app)

    # Invalid password -> 401
    bad_login = client.post("/api/auth/login", json={"username": "admin_user", "password": "WrongPassword"})
    assert bad_login.status_code == 401

    # Valid login -> 200, sets HttpOnly cookie, returns csrf_token
    good_login = client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    assert good_login.status_code == 200
    login_data = good_login.json()
    assert login_data["username"] == "admin_user"
    assert login_data["role"] == "admin"
    assert "csrf_token" in login_data
    assert SESSION_COOKIE_NAME in client.cookies

    # Authenticated /api/auth/me
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == 200
    assert me_res.json()["username"] == "admin_user"
    assert me_res.json()["role"] == "admin"

    # Logout
    logout_res = client.post("/api/auth/logout", headers={"X-CSRF-Token": login_data["csrf_token"]})
    assert logout_res.status_code == 200

    # Subsequent request is 401
    assert client.get("/api/auth/me").status_code == 401


def test_csrf_protection_on_state_changes(tmp_path: Path, auth_env):
    """State changes (POST, PATCH, DELETE) with session cookie require valid X-CSRF-Token."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    client = TestClient(app)

    # Login as admin to upload a book first
    login_res = client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    assert login_res.status_code == 200
    csrf_token = login_res.json()["csrf_token"]

    # State change without CSRF token header -> 403
    upload_no_csrf = client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
    )
    assert upload_no_csrf.status_code == 403
    assert "CSRF" in upload_no_csrf.json()["detail"]

    # State change with invalid CSRF token header -> 403
    upload_bad_csrf = client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": "invalid_csrf_token_value"},
    )
    assert upload_bad_csrf.status_code == 403

    # State change with valid CSRF token header -> 201 Created
    upload_ok = client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert upload_ok.status_code == 201
    book_id = upload_ok.json()["id"]

    # Read-only GET request requires NO CSRF token
    books_res = client.get("/api/books")
    assert books_res.status_code == 200


def test_viewer_role_is_strictly_read_only(tmp_path: Path, auth_env):
    """Viewer can read data but cannot perform any state changes (403)."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    admin_client = TestClient(app)

    # Setup book via admin
    admin_login = admin_client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    admin_csrf = admin_login.json()["csrf_token"]
    upload_res = admin_client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": admin_csrf},
    )
    book_id = upload_res.json()["id"]

    # Login as viewer
    viewer_client = TestClient(app)
    viewer_login = viewer_client.post("/api/auth/login", json={"username": "viewer_user", "password": "ViewerPass123!"})
    assert viewer_login.status_code == 200
    viewer_csrf = viewer_login.json()["csrf_token"]

    # Viewer can read books, chapters, settings
    assert viewer_client.get("/api/books").status_code == 200
    assert viewer_client.get(f"/api/books/{book_id}/chapters").status_code == 200
    assert viewer_client.get("/api/settings").status_code == 200

    # Viewer denied on chapter patch
    chapters = viewer_client.get(f"/api/books/{book_id}/chapters").json()
    ch1_id = chapters[0]["id"]
    patch_res = viewer_client.patch(
        f"/api/chapters/{ch1_id}",
        json={"segments": [{"id": "any", "expected_revision": 1, "translation": "fail"}]},
        headers={"X-CSRF-Token": viewer_csrf},
    )
    assert patch_res.status_code == 403

    # Viewer denied on glossary creation
    gloss_res = viewer_client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "test", "target": "thử"},
        headers={"X-CSRF-Token": viewer_csrf},
    )
    assert gloss_res.status_code == 403

    # Viewer denied on paid Gemini job creation
    job_res = viewer_client.post(
        f"/api/books/{book_id}/jobs",
        json={"chapter_ids": [ch1_id]},
        headers={"X-CSRF-Token": viewer_csrf},
    )
    assert job_res.status_code == 403


def test_editor_role_permissions_and_restrictions(tmp_path: Path, auth_env):
    """Editor can edit chapters/glossary, but is denied paid jobs, settings, and book uploads."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    admin_client = TestClient(app)

    # Setup book via admin
    admin_login = admin_client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    admin_csrf = admin_login.json()["csrf_token"]
    upload_res = admin_client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": admin_csrf},
    )
    book_id = upload_res.json()["id"]

    # Login as editor
    editor_client = TestClient(app)
    editor_login = editor_client.post("/api/auth/login", json={"username": "editor_user", "password": "EditorPass123!"})
    assert editor_login.status_code == 200
    editor_csrf = editor_login.json()["csrf_token"]

    # Editor can read
    assert editor_client.get("/api/books").status_code == 200

    # Editor CAN edit chapters
    chapters = editor_client.get(f"/api/books/{book_id}/chapters").json()
    ch1_id = chapters[0]["id"]
    ch1_detail = editor_client.get(f"/api/chapters/{ch1_id}").json()
    seg1 = ch1_detail["segments"][0]
    assert seg1["revision"] == 1

    edit_res = editor_client.patch(
        f"/api/chapters/{ch1_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Bản dịch của editor"}]},
        headers={"X-CSRF-Token": editor_csrf},
    )
    assert edit_res.status_code == 200
    updated_seg = next(s for s in edit_res.json()["segments"] if s["id"] == seg1["id"])
    assert updated_seg["translation"] == "Bản dịch của editor"
    assert updated_seg["revision"] == 2

    # Editor CAN edit glossary
    gloss_res = editor_client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "Sword", "target": "Thanh kiếm"},
        headers={"X-CSRF-Token": editor_csrf},
    )
    assert gloss_res.status_code == 201

    # Editor CANNOT upload a book (Admin only)
    upload_attempt = editor_client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": editor_csrf},
    )
    assert upload_attempt.status_code == 403

    # Editor CANNOT change app settings (Admin only)
    settings_attempt = editor_client.patch(
        "/api/settings",
        json={"model": "gemini-3.8-flash-high"},
        headers={"X-CSRF-Token": editor_csrf},
    )
    assert settings_attempt.status_code == 403

    # Editor CANNOT launch paid Gemini jobs (Admin only)
    job_attempt = editor_client.post(
        f"/api/books/{book_id}/jobs",
        json={"chapter_ids": [ch1_id]},
        headers={"X-CSRF-Token": editor_csrf},
    )
    assert job_attempt.status_code == 403


def test_optimistic_concurrency_cas_and_winner_preserved(tmp_path: Path, auth_env):
    """Concurrent edits with stale revision are rejected (409) and the winner edit is preserved."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    admin_client = TestClient(app)

    # Setup book via admin
    admin_login = admin_client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    admin_csrf = admin_login.json()["csrf_token"]
    upload_res = admin_client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": admin_csrf},
    )
    book_id = upload_res.json()["id"]

    chapters = admin_client.get(f"/api/books/{book_id}/chapters").json()
    ch1_id = chapters[0]["id"]
    ch1_detail = admin_client.get(f"/api/chapters/{ch1_id}").json()
    seg1 = ch1_detail["segments"][0]
    assert seg1["revision"] == 1

    # Editor 1 client
    editor1 = TestClient(app)
    e1_login = editor1.post("/api/auth/login", json={"username": "editor_user", "password": "EditorPass123!"})
    e1_csrf = e1_login.json()["csrf_token"]

    # Editor 2 client (using admin for a second distinct editor session)
    editor2 = TestClient(app)
    e2_login = editor2.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    e2_csrf = e2_login.json()["csrf_token"]

    # Both Editor 1 and Editor 2 see seg1 at expected_revision = 1
    # Editor 1 saves first
    res1 = editor1.patch(
        f"/api/chapters/{ch1_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Editor 1 Winner Translation"}]},
        headers={"X-CSRF-Token": e1_csrf},
    )
    assert res1.status_code == 200
    assert res1.json()["segments"][0]["revision"] == 2

    # Editor 2 attempts to save with stale expected_revision = 1
    res2 = editor2.patch(
        f"/api/chapters/{ch1_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Editor 2 Stale Translation"}]},
        headers={"X-CSRF-Token": e2_csrf},
    )
    assert res2.status_code == 409
    conflict_data = res2.json()
    assert "conflicts" in conflict_data["detail"]
    conflict_item = conflict_data["detail"]["conflicts"][0]
    assert conflict_item["segment_id"] == seg1["id"]
    assert conflict_item["expected_revision"] == 1
    assert conflict_item["current_revision"] == 2
    assert conflict_item["current_translation"] == "Editor 1 Winner Translation"

    # Verify Winner edit remains preserved in database
    latest_ch = admin_client.get(f"/api/chapters/{ch1_id}").json()
    latest_seg1 = latest_ch["segments"][0]
    assert latest_seg1["translation"] == "Editor 1 Winner Translation"
    assert latest_seg1["revision"] == 2


def test_batch_rollback_on_conflict_no_partial_updates(tmp_path: Path, auth_env):
    """If one segment in a batch update conflicts, NO segments in the batch are applied (atomic CAS)."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    admin_client = TestClient(app)

    # Setup book via admin
    admin_login = admin_client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    admin_csrf = admin_login.json()["csrf_token"]
    upload_res = admin_client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": admin_csrf},
    )
    book_id = upload_res.json()["id"]

    chapters = admin_client.get(f"/api/books/{book_id}/chapters").json()
    ch1_id = chapters[0]["id"]
    ch1_detail = admin_client.get(f"/api/chapters/{ch1_id}").json()
    seg1 = ch1_detail["segments"][0]
    seg2 = ch1_detail["segments"][1]
    assert seg1["revision"] == 1
    assert seg2["revision"] == 1

    # First update: seg1 revision increments to 2
    admin_client.patch(
        f"/api/chapters/{ch1_id}",
        json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": "Seg 1 updated"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )

    # Now seg1 is at rev 2, seg2 is at rev 1
    # Submit batch: seg1 with stale rev 1, seg2 with valid rev 1
    batch_res = admin_client.patch(
        f"/api/chapters/{ch1_id}",
        json={
            "segments": [
                {"id": seg1["id"], "expected_revision": 1, "translation": "Seg 1 should fail"},
                {"id": seg2["id"], "expected_revision": 1, "translation": "Seg 2 should NOT be saved"},
            ]
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert batch_res.status_code == 409

    # Verify seg2 was NOT modified (atomic rollback)
    latest_ch = admin_client.get(f"/api/chapters/{ch1_id}").json()
    latest_seg2 = latest_ch["segments"][1]
    assert latest_seg2["translation"] is None
    assert latest_seg2["revision"] == 1


def test_missing_expected_revision_in_collaboration_mode(tmp_path: Path, auth_env):
    """In fail-closed collaboration mode, missing expected_revision is rejected with 400."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    admin_client = TestClient(app)

    admin_login = admin_client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    admin_csrf = admin_login.json()["csrf_token"]
    upload_res = admin_client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": admin_csrf},
    )
    book_id = upload_res.json()["id"]
    chapters = admin_client.get(f"/api/books/{book_id}/chapters").json()
    ch1_id = chapters[0]["id"]
    ch1_detail = admin_client.get(f"/api/chapters/{ch1_id}").json()
    seg1 = ch1_detail["segments"][0]

    # Omitting expected_revision in collaboration mode
    patch_missing_rev = admin_client.patch(
        f"/api/chapters/{ch1_id}",
        json={"segments": [{"id": seg1["id"], "translation": "No revision provided"}]},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert patch_missing_rev.status_code == 400
    assert "expected_revision" in patch_missing_rev.json()["detail"]
