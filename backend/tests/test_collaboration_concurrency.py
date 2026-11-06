"""Concurrency, atomic validation, and session invalidation tests for multi-user collaboration.

Covers:
- Real two-thread/two-connection optimistic concurrency (CAS) race with barrier & bounded timeout:
  - Exactly one succeeds.
  - Exactly one raises RevisionConflictError (or HTTP 409).
  - Winner edit is preserved.
  - Failed batch causes no partial writes.
- Duplicate segment ID rejection atomicity (at DB level and HTTP API level).
- Cryptographically signed session invalidation upon user removal from current config (via Request and TestClient).
- Current configured role enforcement over token payload role (demotion to viewer prevents writes).
- Remote host access denial when local override is enabled (including header spoofing protection).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.app.auth import (
    SESSION_COOKIE_NAME,
    UserRole,
    create_session_token,
    generate_csrf_token,
    get_current_user_from_request,
    hash_password,
)
from backend.app.db import (
    RevisionConflictError,
    get_chapter,
    get_connection,
    init_db,
    update_chapter_segments,
    utc_now,
)
from backend.app.main import create_app
from backend.tests.test_collaboration import create_test_epub_bytes


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
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


def _seed_minimal_book_and_chapter(
    db_path: Path,
    book_id: str = "book_race",
    chapter_id: str = "ch_race",
) -> tuple[str, str, str]:
    """Directly seed a minimal book with one chapter and two segments at revision 1."""
    init_db(db_path)
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, 'race.epub', ?, ?)",
            (book_id, "Race Book", now, now),
        )
        conn.execute(
            """
            INSERT INTO chapters (
                id, book_id, manifest_id, spine_index, archive_path, href,
                title, media_type, raw_bytes, status, created_at, updated_at
            ) VALUES (?, ?, 'man_1', 1, 'text.xhtml', 'text.xhtml', 'Race Chapter', 'text/html', X'', 'untranslated', ?, ?)
            """,
            (chapter_id, book_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO segments (
                id, chapter_id, book_id, seq_order, dom_path, node_type,
                original_source, source, translation, parent_tag,
                is_whitespace, review_data, revision
            ) VALUES (
                'seg_1', ?, ?, 0, '/html/body/p[1]', 'text',
                'Original sentence 1', 'Original sentence 1', NULL, 'p',
                0, NULL, 1
            )
            """,
            (chapter_id, book_id),
        )
        conn.execute(
            """
            INSERT INTO segments (
                id, chapter_id, book_id, seq_order, dom_path, node_type,
                original_source, source, translation, parent_tag,
                is_whitespace, review_data, revision
            ) VALUES (
                'seg_2', ?, ?, 1, '/html/body/p[2]', 'text',
                'Original sentence 2', 'Original sentence 2', NULL, 'p',
                0, NULL, 1
            )
            """,
            (chapter_id, book_id),
        )
    return book_id, chapter_id, "seg_1"


# =============================================================================
# 1. Real two-thread/two-connection optimistic concurrency race
# =============================================================================


def test_two_thread_concurrent_revision_race_with_barrier(tmp_path: Path) -> None:
    """Real two-thread/two-connection same expected_revision race with barrier & bounded timeout.

    Guarantees:
    - Exactly one thread succeeds.
    - Exactly one thread raises RevisionConflictError.
    - Winner translation and revision increment are preserved in SQLite.
    - Failed batch results in no partial writes (the losing translation is not applied to any segment).
    """
    db_path = tmp_path / "race_test.db"
    book_id, chapter_id, _ = _seed_minimal_book_and_chapter(db_path)

    # Barrier synchronizes both threads right before entering update_chapter_segments
    barrier = threading.Barrier(2)
    results: list[tuple[int, dict[str, Any]]] = []
    errors: list[tuple[int, Exception]] = []

    def worker(worker_id: int, translation_prefix: str) -> None:
        try:
            # Wait at barrier with bounded timeout
            barrier.wait(timeout=5.0)
            res = update_chapter_segments(
                db_path=db_path,
                chapter_id=chapter_id,
                segment_updates=[
                    {"id": "seg_1", "expected_revision": 1, "translation": f"{translation_prefix} Seg 1"},
                    {"id": "seg_2", "expected_revision": 1, "translation": f"{translation_prefix} Seg 2"},
                ],
                enforce_revisions=True,
                actor=f"worker_{worker_id}",
            )
            results.append((worker_id, res))
        except Exception as exc:
            errors.append((worker_id, exc))

    t1 = threading.Thread(target=worker, args=(1, "Thread-1"))
    t2 = threading.Thread(target=worker, args=(2, "Thread-2"))

    t1.start()
    t2.start()

    # Bounded join timeout to guarantee test terminates
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert not t1.is_alive(), "Thread 1 did not complete within bounded timeout"
    assert not t2.is_alive(), "Thread 2 did not complete within bounded timeout"

    # Exactly one succeeds, exactly one fails
    assert len(results) == 1, f"Expected exactly 1 success, got {len(results)}"
    assert len(errors) == 1, f"Expected exactly 1 error, got {len(errors)}"

    winner_id, winner_result = results[0]
    loser_id, loser_error = errors[0]

    # Verify error is specifically RevisionConflictError
    assert isinstance(loser_error, RevisionConflictError), f"Expected RevisionConflictError, got {type(loser_error)}"
    assert len(loser_error.conflicts) > 0
    conflict = loser_error.conflicts[0]
    assert conflict["expected_revision"] == 1
    assert conflict["current_revision"] == 2

    # Verify winner was preserved in the database
    latest_chapter = get_chapter(db_path, chapter_id)
    assert latest_chapter is not None
    seg_map = {s["id"]: s for s in latest_chapter["segments"]}

    winner_prefix = f"Thread-{winner_id}"
    loser_prefix = f"Thread-{loser_id}"

    # Winner's updates fully applied
    assert seg_map["seg_1"]["translation"] == f"{winner_prefix} Seg 1"
    assert seg_map["seg_2"]["translation"] == f"{winner_prefix} Seg 2"
    assert seg_map["seg_1"]["revision"] == 2
    assert seg_map["seg_2"]["revision"] == 2

    # Loser's batch completely rolled back (no partial writes)
    assert loser_prefix not in (seg_map["seg_1"]["translation"] or "")
    assert loser_prefix not in (seg_map["seg_2"]["translation"] or "")


def test_two_client_concurrent_revision_race_via_http(tmp_path: Path, auth_env: list[dict[str, Any]]) -> None:
    """HTTP-level race with two TestClient instances submitting concurrent PATCH requests."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)

    # Admin sets up book
    admin_client = TestClient(app)
    admin_login = admin_client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    admin_csrf = admin_login.json()["csrf_token"]
    upload_res = admin_client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": admin_csrf},
    )
    book_id = upload_res.json()["id"]
    ch1_id = admin_client.get(f"/api/books/{book_id}/chapters").json()[0]["id"]
    seg1 = admin_client.get(f"/api/chapters/{ch1_id}").json()["segments"][0]
    assert seg1["revision"] == 1

    # Editor 1
    editor1 = TestClient(app)
    e1_login = editor1.post("/api/auth/login", json={"username": "editor_user", "password": "EditorPass123!"})
    e1_csrf = e1_login.json()["csrf_token"]

    # Editor 2 (admin account acting as second distinct editor session)
    editor2 = TestClient(app)
    e2_login = editor2.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    e2_csrf = e2_login.json()["csrf_token"]

    barrier = threading.Barrier(2)
    http_responses: list[tuple[int, int, dict[str, Any]]] = []

    def make_patch(client_id: int, client: TestClient, csrf: str, text: str) -> None:
        barrier.wait(timeout=5.0)
        resp = client.patch(
            f"/api/chapters/{ch1_id}",
            json={"segments": [{"id": seg1["id"], "expected_revision": 1, "translation": text}]},
            headers={"X-CSRF-Token": csrf},
        )
        http_responses.append((client_id, resp.status_code, resp.json()))

    th1 = threading.Thread(target=make_patch, args=(1, editor1, e1_csrf, "HTTP Winner 1"))
    th2 = threading.Thread(target=make_patch, args=(2, editor2, e2_csrf, "HTTP Stale 2"))

    th1.start()
    th2.start()
    th1.join(timeout=10.0)
    th2.join(timeout=10.0)

    status_codes = sorted([r[1] for r in http_responses])
    assert status_codes == [200, 409], f"Expected one 200 and one 409, got {status_codes}"

    # Verify conflict payload structure on the 409 response
    conflict_resp = next(r for r in http_responses if r[1] == 409)[2]
    assert "conflicts" in conflict_resp["detail"]
    assert conflict_resp["detail"]["conflicts"][0]["segment_id"] == seg1["id"]

    # Winner edit preserved in chapter
    final_seg = admin_client.get(f"/api/chapters/{ch1_id}").json()["segments"][0]
    assert final_seg["revision"] == 2
    assert final_seg["translation"] in ("HTTP Winner 1", "HTTP Stale 2")


# =============================================================================
# 2. Duplicate segment ID rejection atomicity
# =============================================================================


def test_duplicate_segment_id_rejection_atomicity_db(tmp_path: Path) -> None:
    """Duplicate segment IDs within a single batch must be rejected atomically without partial writes."""
    db_path = tmp_path / "duplicate_db.db"
    book_id, chapter_id, _ = _seed_minimal_book_and_chapter(db_path)

    # Attempt update batch with duplicate segment ID
    duplicate_batch = [
        {"id": "seg_1", "expected_revision": 1, "translation": "Valid first translation"},
        {"id": "seg_1", "expected_revision": 1, "translation": "Duplicate translation"},
    ]

    with pytest.raises(ValueError, match="Duplicate segment ID in one update batch"):
        update_chapter_segments(
            db_path=db_path,
            chapter_id=chapter_id,
            segment_updates=duplicate_batch,
            enforce_revisions=True,
        )

    # Verify atomicity: seg_1 remains unmutated at revision 1 with NULL translation
    ch = get_chapter(db_path, chapter_id)
    assert ch is not None
    seg1 = next(s for s in ch["segments"] if s["id"] == "seg_1")
    assert seg1["translation"] is None
    assert seg1["revision"] == 1

    # Also test mixed batch: seg_1 is valid, but seg_2 is duplicated
    mixed_duplicate_batch = [
        {"id": "seg_1", "expected_revision": 1, "translation": "Seg 1 valid change"},
        {"id": "seg_2", "expected_revision": 1, "translation": "Seg 2 first"},
        {"id": "seg_2", "expected_revision": 1, "translation": "Seg 2 duplicate"},
    ]

    with pytest.raises(ValueError, match="Duplicate segment ID in one update batch"):
        update_chapter_segments(
            db_path=db_path,
            chapter_id=chapter_id,
            segment_updates=mixed_duplicate_batch,
            enforce_revisions=True,
        )

    # Verify seg_1 was NOT partially applied
    ch_after = get_chapter(db_path, chapter_id)
    assert ch_after is not None
    seg1_after = next(s for s in ch_after["segments"] if s["id"] == "seg_1")
    assert seg1_after["translation"] is None
    assert seg1_after["revision"] == 1


def test_duplicate_segment_id_rejection_atomicity_http(tmp_path: Path, auth_env: list[dict[str, Any]]) -> None:
    """Duplicate segment ID in HTTP PATCH returns 400 Bad Request and modifies no segments."""
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    client = TestClient(app)

    login_res = client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPass123!"})
    csrf = login_res.json()["csrf_token"]

    upload_res = client.post(
        "/api/books",
        files={"file": ("sample.epub", create_test_epub_bytes(), "application/epub+zip")},
        headers={"X-CSRF-Token": csrf},
    )
    book_id = upload_res.json()["id"]
    ch1_id = client.get(f"/api/books/{book_id}/chapters").json()[0]["id"]
    seg1 = client.get(f"/api/chapters/{ch1_id}").json()["segments"][0]

    # PATCH with duplicate segment ID in batch
    patch_resp = client.patch(
        f"/api/chapters/{ch1_id}",
        json={
            "segments": [
                {"id": seg1["id"], "expected_revision": 1, "translation": "First draft"},
                {"id": seg1["id"], "expected_revision": 1, "translation": "Second duplicate"},
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert patch_resp.status_code == 400
    assert "Duplicate segment ID" in patch_resp.json()["detail"]

    # Verify seg1 remained untouched
    seg1_check = client.get(f"/api/chapters/{ch1_id}").json()["segments"][0]
    assert seg1_check["translation"] is None
    assert seg1_check["revision"] == 1


# =============================================================================
# 3. Signed session invalidation after user removal from config
# =============================================================================


def test_signed_session_rejected_after_user_removed_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cryptographically valid signed session is rejected (401) once the user is removed from config."""
    secret = "a_very_secure_test_session_secret_key_32bytes!"
    initial_users = [
        {"username": "admin_user", "password_hash": hash_password("Pass123!"), "role": "admin"},
        {"username": "departing_editor", "password_hash": hash_password("Pass123!"), "role": "editor"},
    ]
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setenv("AUTH_USERS", json.dumps(initial_users))
    monkeypatch.setenv("AUTH_OVERRIDE_LOCAL_ONLY", "false")

    # Issue signed session token for departing_editor
    signed_token = create_session_token("departing_editor", role="editor", secret=secret)
    assert signed_token is not None

    # Step 1: Verify token works via direct Request inspection while user exists
    req_active = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/books",
        "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}={signed_token}".encode("utf-8"))],
        "client": ("192.168.1.50", 50000),
    })
    user_active, token_active = get_current_user_from_request(req_active, secret=secret)
    assert user_active is not None
    assert user_active.username == "departing_editor"

    # Step 2: User is removed from current configuration
    updated_users = [
        {"username": "admin_user", "password_hash": hash_password("Pass123!"), "role": "admin"}
    ]
    monkeypatch.setenv("AUTH_USERS", json.dumps(updated_users))

    # Verification A: Direct Request inspection returns (None, None)
    req_removed = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/books",
        "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}={signed_token}".encode("utf-8"))],
        "client": ("192.168.1.50", 50000),
    })
    user_removed, token_removed = get_current_user_from_request(req_removed, secret=secret)
    assert user_removed is None, "Removed user must not be authenticated even with valid cryptographic signature"
    assert token_removed is None

    # Verification B: Via TestClient HTTP endpoint returns 401 Unauthorized
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, signed_token)

    resp = client.get("/api/books")
    assert resp.status_code == 401
    assert "đăng nhập" in resp.json()["detail"].lower()


# =============================================================================
# 4. Signed session enforces demoted role from current config
# =============================================================================


def test_signed_session_enforces_current_demoted_role_not_token_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a user is demoted in config, requests using their old session enforce the demoted role."""
    secret = "a_very_secure_test_session_secret_key_32bytes!"
    initial_users = [
        {"username": "admin_user", "password_hash": hash_password("Pass123!"), "role": "admin"},
        {"username": "chameleon_user", "password_hash": hash_password("Pass123!"), "role": "editor"},
    ]
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setenv("AUTH_USERS", json.dumps(initial_users))
    monkeypatch.setenv("AUTH_OVERRIDE_LOCAL_ONLY", "false")

    # Token minted when role was 'editor'
    signed_token = create_session_token("chameleon_user", role="editor", secret=secret)
    csrf_token = generate_csrf_token(signed_token, secret=secret)

    # Demote user to 'viewer' in current configuration
    demoted_users = [
        {"username": "admin_user", "password_hash": hash_password("Pass123!"), "role": "admin"},
        {"username": "chameleon_user", "password_hash": hash_password("Pass123!"), "role": "viewer"},
    ]
    monkeypatch.setenv("AUTH_USERS", json.dumps(demoted_users))

    # Verification A: get_current_user_from_request resolves role to current VIEWER
    req = Request({
        "type": "http",
        "method": "PATCH",
        "path": "/api/chapters/some_ch",
        "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}={signed_token}".encode("utf-8"))],
        "client": ("192.168.1.50", 50000),
    })
    user, _ = get_current_user_from_request(req, secret=secret)
    assert user is not None
    assert user.role == UserRole.VIEWER, "Server must resolve user to current demoted role (viewer), not token role"

    # Verification B: HTTP TestClient enforces RBAC for demoted role
    app = create_app(data_dir=tmp_path, auth_override_local_only=False, start_worker=False)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, signed_token)

    # Viewer is permitted to read books (HTTP 200)
    read_resp = client.get("/api/books")
    assert read_resp.status_code == 200

    # Viewer is denied state changes (HTTP 403 Forbidden)
    write_resp = client.patch(
        "/api/chapters/non_existent_chapter",
        json={"segments": []},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert write_resp.status_code == 403
    assert "viewer" in write_resp.json()["detail"].lower()


# =============================================================================
# 5. Remote host denied when local override is enabled
# =============================================================================


def test_remote_host_denied_when_local_override_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When AUTH_OVERRIDE_LOCAL_ONLY=true, remote transport peers are denied with 401.

    Also verifies transport peer check cannot be bypassed with spoofed proxy headers.
    """
    monkeypatch.setenv("AUTH_OVERRIDE_LOCAL_ONLY", "true")
    app = create_app(data_dir=tmp_path, auth_override_local_only=True, start_worker=False)

    # 1. Remote host without authentication -> 401
    remote_client = TestClient(app, client=("198.51.100.25", 50000))
    resp_remote = remote_client.get("/api/books")
    assert resp_remote.status_code == 401
    assert "đăng nhập" in resp_remote.json()["detail"].lower()

    # 2. Remote host attempting loopback spoofing via Forwarded/X-Forwarded-* headers -> still 401
    resp_spoof = remote_client.get(
        "/api/books",
        headers={
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
            "Forwarded": "for=127.0.0.1",
            "Host": "127.0.0.1",
        },
    )
    assert resp_spoof.status_code == 401, "Spoofed loopback headers must not bypass direct transport peer check"

    # 3. Default Starlette TestClient (host 'testclient') is remote -> 401
    legacy_client = TestClient(app)
    resp_legacy = legacy_client.get("/api/books")
    assert resp_legacy.status_code == 401

    # 4. Legitimate loopback client -> 200 OK
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    resp_loopback = loopback_client.get("/api/books")
    assert resp_loopback.status_code == 200
