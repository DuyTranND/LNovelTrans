"""Targeted regression tests for audit status correctness, gating of proposals,

stale error cleanup, and data preservation.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.audit import AuditManager, ConsistencyAuditService
from backend.app.corrections import (
    apply_proposals_with_snapshot,
    generate_correction_proposals,
    rollback_snapshot,
)
from backend.app.db import (
    create_audit_run,
    create_glossary_item,
    get_audit_checkpoints,
    get_audit_findings,
    get_audit_run,
    get_chapter,
    get_connection,
    get_correction_proposals,
    get_correction_snapshot,
    init_db,
    save_audit_checkpoint,
    save_audit_findings,
    update_audit_run,
    update_chapter_segments,
    utc_now,
)
from backend.app.main import create_app
from backend.app.provider import ProviderError, _BaseBatchProvider


def _setup_regression_book(db_path: Path | str, book_id: str, num_chapters: int = 2) -> None:
    init_db(db_path)
    now = utc_now()
    sample_texts = [
        ("스타플렉스에 도착했다.", "Đã đến Starflex."),
        ("시스템 창이 열렸다.", "Cửa sổ hệ thống mở ra."),
    ]

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO books (id, title, author, language, identifier, file_path, created_at, updated_at, story_bible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (book_id, "Sách Kiểm Thử", "Tác giả", "ko", book_id, "/tmp/dummy.epub", now, now),
        )

        for ch_idx in range(num_chapters):
            ch_id = f"ch_{ch_idx + 1}"
            ch_title = f"Chương {ch_idx + 1}"
            conn.execute(
                """
                INSERT OR REPLACE INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'translated', ?, ?)
                """,
                (
                    ch_id,
                    book_id,
                    f"item_{ch_idx + 1}",
                    ch_idx,
                    f"OEBPS/ch{ch_idx + 1}.xhtml",
                    f"ch{ch_idx + 1}.xhtml",
                    ch_title,
                    "application/xhtml+xml",
                    b"<html/>",
                    now,
                    now,
                ),
            )

            # Segment 0: title
            conn.execute(
                """
                INSERT OR REPLACE INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace, review_data)
                VALUES (?, ?, ?, 0, 'body/div/h1[1]', 'text', ?, ?, ?, 'h1', 0, NULL)
                """,
                (f"seg_{ch_idx + 1}_0", ch_id, book_id, ch_title, ch_title, ch_title),
            )

            for s_idx, (src, default_trans) in enumerate(sample_texts, start=1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace, review_data)
                    VALUES (?, ?, ?, ?, ?, 'text', ?, ?, ?, 'p', 0, NULL)
                    """,
                    (
                        f"seg_{ch_idx + 1}_{s_idx}",
                        ch_id,
                        book_id,
                        s_idx,
                        f"body/div/p[{s_idx}]",
                        src,
                        src,
                        default_trans,
                    ),
                )


class MockIntermittentAuditProvider(_BaseBatchProvider):
    """Mock provider that succeeds on batch 0 and fails on batch 1."""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def all_secrets(self) -> list[str]:
        return ["SUPER_SECRET_KEY"]

    async def extract_terminology(self, segments: list[dict[str, str]], **kwargs: Any) -> list[dict[str, Any]]:
        self.call_count += 1
        if self.call_count == 1:
            # First batch succeeds
            return [
                {
                    "source_term": "스타플렉스",
                    "target_term": "Starflex",
                    "category": "location",
                    "confidence": "high",
                    "severity": "high",
                    "chapter_id": kwargs.get("chapter_id", "ch_1"),
                    "segment_id": segments[0]["id"] if segments else "seg_1_1",
                }
            ]
        else:
            # Subsequent batch fails with transient network/server error
            raise ProviderError("502 Bad Gateway: Intermittent network glitch")

    async def adjudicate_inconsistencies(self, candidates: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        return []


def test_partial_audit_persists_partial_status_and_stage_with_checkpoints_preserved(tmp_path: Path) -> None:
    """Critical bug fix verification: partial audits MUST NOT be marked 'completed'.
    
    Status must be 'partial', stage 'partial', error persisted, and completed checkpoints preserved.
    """
    db_path = tmp_path / "translator.db"
    book_id = "book_partial_test"
    _setup_regression_book(db_path, book_id, num_chapters=2)

    # Add locked glossary to generate divergence finding
    create_glossary_item(
        db_path,
        book_id,
        source="스타플렉스",
        target="Starplex",
        notes="location",
        locked=True,
    )

    run = create_audit_run(db_path, book_id, model="gemini-3.5-flash-lite")
    run_id = run["id"]

    mock_provider = MockIntermittentAuditProvider()
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    asyncio.run(service.execute_run(run_id))

    updated_run = get_audit_run(db_path, run_id)
    assert updated_run is not None

    # Status must NOT be 'completed'
    assert updated_run["status"] == "partial", f"Expected status 'partial' but got {updated_run['status']}"
    assert updated_run["progress"]["stage"] == "partial"
    assert updated_run["progress"]["coverage_status"] == "partial"
    assert updated_run["processed_segments"] > 0
    assert updated_run["failed_segments"] > 0
    assert updated_run["processed_segments"] + updated_run["failed_segments"] <= updated_run["total_segments"]

    # Error must be persisted
    assert updated_run["error"] is not None
    assert "một phần" in updated_run["error"].lower()
    assert "502 Bad Gateway" in updated_run["error"]

    # Checkpoints for batch 0 must be safely preserved
    checkpoints = get_audit_checkpoints(db_path, run_id)
    assert len(checkpoints) >= 1
    assert checkpoints[0]["batch_index"] == 0

    # No automatic correction proposals should be persisted for partial audits
    proposals = get_correction_proposals(db_path, run_id)
    assert proposals["total"] == 0


def test_running_and_partial_and_stale_audit_apply_rejected_with_no_data_mutation(tmp_path: Path) -> None:
    """Gating verification: active, partial, or stale audit runs cannot generate or apply proposals.
    
    Verifies that regardless of selected_ids or auto_safe flags, apply is rejected
    and NO data mutation occurs in the segments table, with no snapshots created.
    """
    db_path = tmp_path / "translator.db"
    book_id = "book_gate_test"
    _setup_regression_book(db_path, book_id, num_chapters=1)

    app = create_app(start_worker=False)
    app.state.db_path = db_path
    client = TestClient(app)

    # 1. Test Active (Running) run
    run = create_audit_run(db_path, book_id)
    run_id = run["id"]
    update_audit_run(db_path, run_id, status="running", total_segments=3, processed_segments=1)

    # Proposal generation on running run rejected
    resp_gen = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
    assert resp_gen.status_code == 400
    assert "đang chạy" in resp_gen.json()["detail"] or "running" in resp_gen.json()["detail"]

    # Apply on running run rejected
    resp_apply = client.post(
        f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply",
        json={"selected_ids": ["seg_1_1"], "only_auto_safe": True},
    )
    assert resp_apply.status_code == 400
    assert "đang chạy" in resp_apply.json()["detail"] or "running" in resp_apply.json()["detail"]

    # Direct DB apply also rejected
    with pytest.raises(ValueError, match="đang hoạt động"):
        apply_proposals_with_snapshot(db_path, run_id, book_id, only_auto_safe=True)

    # 2. Test Partial run
    update_audit_run(
        db_path,
        run_id,
        status="partial",
        total_segments=3,
        processed_segments=2,
        failed_segments=1,
        progress={"coverage_status": "partial", "stage": "partial"},
    )

    resp_apply_partial = client.post(
        f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply",
        json={"only_auto_safe": False},
    )
    assert resp_apply_partial.status_code == 400
    assert "chưa hoàn tất" in resp_apply_partial.json()["detail"] or "partial" in resp_apply_partial.json()["detail"]

    resp_gen_partial = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
    assert resp_gen_partial.status_code == 400

    with pytest.raises(ValueError, match="chưa hoàn tất"):
        apply_proposals_with_snapshot(db_path, run_id, book_id)

    # 3. Test Stale run
    update_audit_run(
        db_path,
        run_id,
        status="completed",
        total_segments=3,
        processed_segments=3,
        failed_segments=0,
        is_stale=True,
        progress={"coverage_status": "full", "stage": "completed"},
    )

    resp_apply_stale = client.post(
        f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply",
        json={"selected_ids": []},
    )
    assert resp_apply_stale.status_code == 400
    assert "đã cũ" in resp_apply_stale.json()["detail"] or "stale" in resp_apply_stale.json()["detail"]

    resp_gen_stale = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
    assert resp_gen_stale.status_code == 400
    assert "đã cũ" in resp_gen_stale.json()["detail"] or "stale" in resp_gen_stale.json()["detail"]

    with pytest.raises(ValueError, match="stale"):
        apply_proposals_with_snapshot(db_path, run_id, book_id, check_stale=True)

    # CRITICAL: Verify NO segments were mutated in database
    ch = get_chapter(db_path, "ch_1")
    s1_1 = next(s for s in ch["segments"] if s["id"] == "seg_1_1")
    assert s1_1["translation"] == "Đã đến Starflex."

    # Verify NO snapshot was created
    with get_connection(db_path) as conn:
        snap_count = conn.execute("SELECT count(*) as cnt FROM consistency_correction_snapshots").fetchone()["cnt"]
        assert snap_count == 0


def test_manual_preview_on_incomplete_run_shows_coverage_notice_and_blocks_apply(tmp_path: Path) -> None:
    """Manual preview can view incomplete run with clear coverage notice, but can_apply is False."""
    db_path = tmp_path / "translator.db"
    book_id = "book_preview_test"
    _setup_regression_book(db_path, book_id, num_chapters=1)

    app = create_app(start_worker=False)
    app.state.db_path = db_path
    client = TestClient(app)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]
    update_audit_run(
        db_path,
        run_id,
        status="partial",
        total_segments=10,
        processed_segments=6,
        failed_segments=4,
        progress={"coverage_status": "partial", "stage": "partial"},
    )

    # GET preview should succeed (HTTP 200) for human inspection
    resp = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
    assert resp.status_code == 200
    data = resp.json()

    # Must clearly mark incomplete and forbid apply
    assert data["is_complete"] is False
    assert data["can_apply"] is False
    assert data["coverage_notice"] is not None
    assert "chưa hoàn tất" in data["coverage_notice"]
    assert "KHÔNG THỂ áp dụng" in data["coverage_notice"]


def test_retry_behavior_clears_stale_error_and_allows_apply_upon_full_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry behavior: clearing stale error on retry/running, reusing checkpoints,

    achieving full coverage status only when all segments succeed, and allowing apply with snapshot.
    """
    monkeypatch.setattr(AuditManager, "start_audit", lambda self, run_id: None)
    db_path = tmp_path / "translator.db"
    book_id = "book_retry_test"
    _setup_regression_book(db_path, book_id, num_chapters=2)

    app = create_app(start_worker=False)
    app.state.db_path = db_path
    client = TestClient(app)

    # 1. Create run with a stale failure error
    run = create_audit_run(db_path, book_id, model="gemini-3.5-flash-lite")
    run_id = run["id"]
    update_audit_run(
        db_path,
        run_id,
        status="failed",
        total_segments=4,
        processed_segments=2,
        failed_segments=2,
        error="Old stale error from prior network disconnect",
    )

    failed_state = get_audit_run(db_path, run_id)
    assert failed_state["error"] == "Old stale error from prior network disconnect"

    # Add locked glossary divergence
    create_glossary_item(
        db_path,
        book_id,
        source="스타플렉스",
        target="Starplex",
        notes="location",
        locked=True,
    )

    # 2. Trigger retry endpoint: should queue and clean stale error
    retry_resp = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/retry")
    assert retry_resp.status_code == 202

    queued_state = get_audit_run(db_path, run_id)
    assert queued_state["status"] == "queued"
    assert queued_state["error"] is None, "Stale error must be cleared upon retry!"

    # 3. Execute retry with successful provider
    class FullySuccessfulProvider(_BaseBatchProvider):
        async def extract_terminology(self, segments, **kwargs):
            return [
                {
                    "source_term": "스타플렉스",
                    "target_term": "Starflex",
                    "category": "location",
                    "confidence": "high",
                    "severity": "high",
                    "chapter_id": kwargs.get("chapter_id", "ch_1"),
                    "segment_id": segments[0]["id"] if segments else "seg_1_1",
                }
            ]

        async def adjudicate_inconsistencies(self, candidates, **kwargs):
            return []

    service = ConsistencyAuditService(db_path, lambda: FullySuccessfulProvider())
    asyncio.run(service.execute_run(run_id))

    completed_state = get_audit_run(db_path, run_id)
    assert completed_state["status"] == "completed"
    assert completed_state["failed_segments"] == 0
    assert completed_state["skipped_segments"] == 0
    assert completed_state["processed_segments"] == completed_state["total_segments"]
    assert completed_state["progress"]["coverage_status"] == "full"
    assert completed_state["progress"]["stage"] == "completed"
    assert completed_state["error"] is None
    assert completed_state["model"] == "gemini-3.5-flash-lite"

    # 4. Now that the run is 100% complete and full coverage, proposals are auto-generated
    props = get_correction_proposals(db_path, run_id)
    assert props["total"] >= 1

    # 5. Apply proposals now succeeds and creates snapshot
    apply_resp = client.post(
        f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply",
        json={"only_auto_safe": True},
    )
    assert apply_resp.status_code == 200
    apply_data = apply_resp.json()
    assert apply_data["applied_count"] >= 1
    snapshot_id = apply_data["snapshot_id"]
    assert snapshot_id is not None

    # Verify snapshot can be rolled back cleanly
    rollback_resp = client.post(f"/api/books/{book_id}/consistency-reviews/snapshots/{snapshot_id}/rollback")
    assert rollback_resp.status_code == 200
    assert rollback_resp.json()["rolled_back_segments"] >= 1


def test_production_audit_run_untouched_read_only() -> None:
    """Verify that the production database data/translator.db and real audit run 400ac73069984a0d8f121a0a6f1f9984

    remain strictly read-only and unmutated.
    """
    prod_db = Path("data/translator.db")
    if not prod_db.is_file():
        pytest.skip("data/translator.db does not exist on disk")

    # Connect strictly read-only
    conn = sqlite3.connect("file:data/translator.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    run = conn.execute(
        "SELECT * FROM consistency_audit_runs WHERE id = ?",
        ("400ac73069984a0d8f121a0a6f1f9984",),
    ).fetchone()

    assert run is not None
    assert run["model"] == "gemini-3.5-flash-lite"
    assert run["total_segments"] == 46637
    assert run["processed_segments"] >= 28518
    assert run["failed_segments"] == 0
    assert run["skipped_segments"] == 0
    conn.close()
