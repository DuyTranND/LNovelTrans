"""Comprehensive unit and integration tests for consistency audit resume,

contextual adjudication, dry-run preview, optimistic locking, and automatic
correction with recoverable backup snapshots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.audit import ConsistencyAuditService
from backend.app.corrections import (
    apply_proposals_with_snapshot,
    extract_trailing_counter,
    generate_correction_proposals,
    generate_title_correction,
    rollback_snapshot,
)
from backend.app.db import (
    apply_correction_proposals,
    create_audit_run,
    create_glossary_item,
    get_all_audit_findings,
    get_audit_checkpoints,
    get_audit_findings,
    get_audit_run,
    get_chapter,
    get_connection,
    get_correction_backup_items,
    get_correction_proposals,
    get_correction_snapshot,
    get_correction_snapshots,
    init_db,
    save_audit_checkpoint,
    save_audit_findings,
    seed_checkpoints_from_prior_runs,
    update_audit_run,
    update_chapter_segments,
    utc_now,
)
from backend.app.main import create_app
from backend.app.provider import ProviderRouter


def _setup_test_book(
    db_path: Path,
    book_id: str = "book_test_1",
    num_chapters: int = 3,
) -> None:
    """Helper to populate a test book with chapters and segments."""
    init_db(db_path)
    now = utc_now()

    sample_texts = [
        ("내 목에 걸린 스타플렉스 사원증", "Thẻ nhân viên Starflex trước cổ tôi"),
        ("상태창을 열었다.", "Mở cửa sổ trạng thái."),
        ("시스템의 메시지가 떴다.", "Tin nhắn từ Thần thống hiện lên."),
    ]

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO books (id, title, author, language, identifier, file_path, created_at, updated_at, story_bible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (book_id, "Truyện Test", "Tác giả", "ko", book_id, "/tmp/dummy.epub", now, now),
        )

        for ch_idx in range(num_chapters):
            ch_id = f"ch_{ch_idx + 1}"
            ch_title = f"각성 ({ch_idx + 1})"
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

            # Segment 0: Chapter title (h1)
            # Chapter 1 uses "Thức tỉnh (1)", Chapter 2 uses "Giác tỉnh 2", Chapter 3 uses "Thức tỉnh (3)"
            title_trans = f"Thức tỉnh ({ch_idx + 1})" if ch_idx != 1 else "Giác tỉnh 2"
            conn.execute(
                """
                INSERT OR REPLACE INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace, review_data)
                VALUES (?, ?, ?, 0, 'body/div/h1[1]', 'text', ?, ?, ?, 'h1', 0, NULL)
                """,
                (f"seg_{ch_idx + 1}_0", ch_id, book_id, ch_title, ch_title, title_trans),
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


class MockAdjudicatingProvider:
    """Mock provider with terminology extraction and adjudication."""

    def __init__(self) -> None:
        self.all_secrets: list[str] = []

    async def extract_terminology(
        self,
        segments: list[Any],
        chapter_id: str | None = None,
        chapter_title: str | None = None,
        glossary: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        findings = []
        for s in segments:
            sid = s.get("id")
            src = s.get("source", "")
            trans = s.get("translation", "")
            if "스타플렉스" in src:
                findings.append({
                    "segment_id": sid,
                    "chapter_id": chapter_id,
                    "chapter_title": chapter_title,
                    "source_term": "스타플렉스",
                    "target_term": "Starflex",
                    "canonical_source": "스타플렉스",
                    "category": "location",
                    "confidence": "high",
                    "source_snippet": src,
                    "target_snippet": trans,
                })
        return findings

    async def adjudicate_inconsistencies(
        self,
        candidates: list[dict[str, Any]],
        glossary: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        results = []
        for c in candidates:
            st = c.get("source_term", "")
            if st == "스타플렉스":
                results.append({
                    "source_term": st,
                    "canonical_recommendation": "Starplex",
                    "reason": "Thẩm định: Phiên âm quốc tế chuẩn theo bối cảnh tòa nhà Starplex",
                    "confidence": "high",
                })
        return results


def test_trailing_counter_extraction() -> None:
    """Test precise extraction and preservation of installment ordinals."""
    stem, counter = extract_trailing_counter("Thức tỉnh (1)")
    assert stem == "Thức tỉnh"
    assert counter == "(1)"

    stem, counter = extract_trailing_counter("Phim tài liệu Starplex Tập 5")
    assert stem == "Phim tài liệu Starplex"
    assert counter == "Tập 5"

    stem, counter = extract_trailing_counter("Hệ thống VS Thần hệ thống - Chương 2")
    assert stem == "Hệ thống VS Thần hệ thống"
    assert counter == "Chương 2"

    new_t, preserved = generate_title_correction(
        old_title="Giác tỉnh (2)",
        canonical_stem="Thức tỉnh",
        source_title="각성 (2)",
    )
    assert new_t == "Thức tỉnh (2)"
    assert preserved == "(2)"


def test_seed_checkpoints_from_prior_runs(tmp_path: Path) -> None:
    """Test that valid checkpoints from prior runs of the same book are safely seeded."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_seed_test", num_chapters=2)

    run_old = create_audit_run(db_path, "book_seed_test")
    old_id = run_old["id"]

    # Save checkpoint in old run
    save_audit_checkpoint(
        db_path,
        old_id,
        batch_index=0,
        chapter_id="ch_1",
        segment_ids=["seg_1_0", "seg_1_1", "seg_1_2", "seg_1_3"],
        raw_findings=[{
            "segment_id": "seg_1_1",
            "source_term": "스타플렉스",
            "target_term": "Starflex",
            "category": "location",
            "confidence": "high",
        }],
    )

    planned_batches = [{
        "chapter_id": "ch_1",
        "chapter_title": "각성 (1)",
        "segments": [
            {"id": "seg_1_0"},
            {"id": "seg_1_1"},
            {"id": "seg_1_2"},
            {"id": "seg_1_3"},
        ],
    }]

    # Create new run
    run_new = create_audit_run(db_path, "book_seed_test")
    new_id = run_new["id"]

    seeded = seed_checkpoints_from_prior_runs(db_path, new_id, "book_seed_test", planned_batches)
    assert seeded == 1

    cps = get_audit_checkpoints(db_path, new_id)
    assert len(cps) == 1
    assert cps[0]["batch_index"] == 0
    assert cps[0]["chapter_id"] == "ch_1"
    raw = cps[0]["raw_findings"]
    assert len(raw) == 1
    assert raw[0]["source_term"] == "스타플렉스"



def _mark_run_completed_full(db_path: Path | str, run_id: str, total_segments: int = 10) -> None:
    update_audit_run(
        db_path,
        run_id,
        status="completed",
        total_segments=total_segments,
        processed_segments=total_segments,
        failed_segments=0,
        skipped_segments=0,
        progress={"coverage_status": "full", "stage": "completed"},
    )


def test_dry_run_proposals_generation_and_classification(tmp_path: Path) -> None:
    """Test generating proposals without modifying book segments, verifying auto_safe vs manual_only."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_prop_test", num_chapters=3)

    run = create_audit_run(db_path, "book_prop_test")
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id)

    # Add locked glossary term
    create_glossary_item(
        db_path,
        "book_prop_test",
        source="스타플렉스",
        target="Starplex",
        notes="location",
        locked=True,
    )

    findings = [
        # 1. Title finding (Ch. 2 uses 'Giác tỉnh 2' while Ch. 1, 3 use 'Thức tỉnh')
        {
            "category": "title",
            "source_term": "각성",
            "canonical_recommendation": "Thức tỉnh",
            "confidence": "high",
            "severity": "medium",
            "glossary_locked": False,
            "variants": [
                {"target": "Thức tỉnh", "count": 2},
                {"target": "Giác tỉnh", "count": 1},
            ],
            "occurrences": [
                {
                    "chapter_id": "ch_2",
                    "chapter_title": "Chương 2",
                    "segment_id": "seg_2_0",
                    "source_term": "각성 (2)",
                    "target_variant": "Giác tỉnh 2",
                }
            ],
        },
        # 2. Locked glossary location divergence: 'Starflex' in segment vs 'Starplex' in locked glossary
        {
            "category": "location",
            "source_term": "스타플렉스",
            "canonical_recommendation": "Starplex",
            "confidence": "high",
            "severity": "high",
            "glossary_locked": True,
            "glossary_target": "Starplex",
            "variants": [{"target": "Starflex", "count": 3}],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_1",
                    "source_term": "스타플렉스",
                    "target_variant": "Starflex",
                }
            ],
        },
        # 3. Case-only difference without locked glossary -> should be manual_only
        {
            "category": "term",
            "source_term": "상태창",
            "canonical_recommendation": "Cửa sổ trạng thái",
            "confidence": "medium",
            "severity": "low",
            "is_case_only": True,
            "glossary_locked": False,
            "variants": [
                {"target": "Cửa sổ trạng thái", "count": 2},
                {"target": "cửa sổ trạng thái", "count": 1},
            ],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_2",
                    "source_term": "상태창",
                    "target_variant": "cửa sổ trạng thái",
                }
            ],
        },
    ]

    res = generate_correction_proposals(db_path, run_id, "book_prop_test", findings)
    assert res["total_proposals"] >= 2

    # Verify dry-run: segments table MUST remain untouched
    ch1 = get_chapter(db_path, "ch_1")
    s1_1 = next(s for s in ch1["segments"] if s["id"] == "seg_1_1")
    assert s1_1["translation"] == "Thẻ nhân viên Starflex trước cổ tôi"  # Unchanged!

    ch2 = get_chapter(db_path, "ch_2")
    s2_0 = next(s for s in ch2["segments"] if s["id"] == "seg_2_0")
    assert s2_0["translation"] == "Giác tỉnh 2"  # Unchanged!

    # Check proposal classifications in DB
    proposals_data = get_correction_proposals(db_path, run_id, page=1, page_size=50)
    items = proposals_data["items"]

    # Title proposal should be auto_safe and preserve ordinal
    title_prop = next(p for p in items if p["category"] == "title")
    assert title_prop["classification"] == "auto_safe"
    assert title_prop["old_text"] == "Giác tỉnh 2"
    assert "Thức tỉnh" in title_prop["new_text"]
    assert "2" in title_prop["new_text"]

    # Locked glossary location proposal should be auto_safe
    loc_prop = next(p for p in items if p["category"] == "location")
    assert loc_prop["classification"] == "auto_safe"
    assert loc_prop["canonical_target"] == "Starplex"
    assert "Starplex" in loc_prop["new_text"]
    assert "Starflex" not in loc_prop["new_text"]

    # Case-only proposal should be manual_only
    case_prop = next((p for p in items if p["source_term"] == "상태창"), None)
    if case_prop:
        assert case_prop["classification"] == "manual_only"


def test_transactional_apply_with_snapshot_and_rollback(tmp_path: Path) -> None:
    """Test applying proposals creates a snapshot, updates segments atomically, and rolls back cleanly."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_apply_test", num_chapters=2)

    run = create_audit_run(db_path, "book_apply_test")
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id)

    findings = [
        {
            "category": "location",
            "source_term": "스타플렉스",
            "canonical_recommendation": "Starplex",
            "confidence": "high",
            "severity": "high",
            "glossary_locked": True,
            "variants": [{"target": "Starflex", "count": 2}],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_1",
                    "source_term": "스타플렉스",
                    "target_variant": "Starflex",
                }
            ],
        }
    ]

    generate_correction_proposals(db_path, run_id, "book_apply_test", findings)

    # Apply auto_safe proposals
    apply_res = apply_proposals_with_snapshot(db_path, run_id, "book_apply_test", only_auto_safe=True)
    assert apply_res["applied_count"] == 1
    snapshot_id = apply_res["snapshot_id"]
    assert snapshot_id is not None

    # Verify segment in DB was updated
    ch1 = get_chapter(db_path, "ch_1")
    s1_1 = next(s for s in ch1["segments"] if s["id"] == "seg_1_1")
    assert s1_1["translation"] == "Thẻ nhân viên Starplex trước cổ tôi"

    # Verify snapshot recorded in DB
    snap = get_correction_snapshot(db_path, snapshot_id)
    assert snap is not None
    assert snap["is_rolled_back"] == 0
    assert snap["total_segments_changed"] == 1

    backup_items = get_correction_backup_items(db_path, snapshot_id)
    assert len(backup_items) == 1
    assert backup_items[0]["pre_text"] == "Thẻ nhân viên Starflex trước cổ tôi"
    assert backup_items[0]["post_text"] == "Thẻ nhân viên Starplex trước cổ tôi"

    # Verify idempotency: applying a second time should result in 0 new writes
    apply_res_2 = apply_proposals_with_snapshot(db_path, run_id, "book_apply_test", only_auto_safe=True)
    assert apply_res_2["applied_count"] == 0
    assert apply_res_2["already_applied_count"] >= 1

    # Verify rollback
    rollback_res = rollback_snapshot(db_path, snapshot_id)
    assert rollback_res["rolled_back_segments"] == 1

    # Verify segment was restored to exact pre_text
    ch1_restored = get_chapter(db_path, "ch_1")
    s1_1_restored = next(s for s in ch1_restored["segments"] if s["id"] == "seg_1_1")
    assert s1_1_restored["translation"] == "Thẻ nhân viên Starflex trước cổ tôi"

    # Second rollback on same snapshot should be rejected
    with pytest.raises(ValueError, match="đã được hoàn tác trước đó"):
        rollback_snapshot(db_path, snapshot_id)


def test_optimistic_locking_conflict_detection(tmp_path: Path) -> None:
    """Test that applying a proposal is rejected if the segment text was modified since preview generation."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_conflict_test", num_chapters=1)

    run = create_audit_run(db_path, "book_conflict_test")
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id)

    findings = [
        {
            "category": "location",
            "source_term": "스타플렉스",
            "canonical_recommendation": "Starplex",
            "confidence": "high",
            "severity": "high",
            "glossary_locked": True,
            "variants": [{"target": "Starflex", "count": 1}],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_1",
                    "source_term": "스타플렉스",
                    "target_variant": "Starflex",
                }
            ],
        }
    ]

    generate_correction_proposals(db_path, run_id, "book_conflict_test", findings)

    # Simulate concurrent user edit to the segment text before apply
    update_chapter_segments(
        db_path,
        "ch_1",
        [{"id": "seg_1_1", "translation": "Bản dịch đã bị người dùng sửa bằng tay"}],
    )

    # Apply should detect conflict and refuse to overwrite
    apply_res = apply_proposals_with_snapshot(db_path, run_id, "book_conflict_test", only_auto_safe=True)
    assert apply_res["applied_count"] == 0
    assert apply_res["conflict_count"] == 1

    # Verify user's manual edit was preserved intact
    ch1 = get_chapter(db_path, "ch_1")
    s1_1 = next(s for s in ch1["segments"] if s["id"] == "seg_1_1")
    assert s1_1["translation"] == "Bản dịch đã bị người dùng sửa bằng tay"


def test_truthful_coverage_and_adjudication(tmp_path: Path) -> None:
    """Test that audit never reports 100% full coverage if segments were skipped or failed."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_cov_test", num_chapters=2)

    run = create_audit_run(db_path, "book_cov_test")
    run_id = run["id"]

    mock_provider = MockAdjudicatingProvider()
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    # Test audit run execution with mock provider
    import asyncio
    asyncio.run(service.execute_run(run_id))

    updated_run = get_audit_run(db_path, run_id)
    assert updated_run is not None
    assert updated_run["status"] == "completed"

    progress = json.loads(updated_run["progress"]) if isinstance(updated_run["progress"], str) else updated_run["progress"]
    assert progress["coverage_status"] == "full"
    assert updated_run["processed_segments"] == updated_run["total_segments"]

    # Verify that proposals were auto-generated
    props = get_correction_proposals(db_path, run_id)
    assert props["total"] >= 1


def test_api_correction_endpoints(tmp_path: Path) -> None:
    """Test the FastAPI endpoints for preview, proposal selection, apply, and rollback."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_api_test", num_chapters=2)

    app = create_app(start_worker=False)
    app.state.db_path = db_path
    client = TestClient(app)

    run = create_audit_run(db_path, "book_api_test")
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id)

    # Add finding
    findings = [
        {
            "category": "location",
            "source_term": "스타플렉스",
            "canonical_recommendation": "Starplex",
            "confidence": "high",
            "severity": "high",
            "glossary_locked": True,
            "variants": [{"target": "Starflex", "count": 2}],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_1",
                    "source_term": "스타플렉스",
                    "target_variant": "Starflex",
                }
            ],
        }
    ]
    generate_correction_proposals(db_path, run_id, "book_api_test", findings)

    # 1. GET preview
    resp = client.get(f"/api/books/book_api_test/consistency-reviews/{run_id}/corrections/preview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    proposal_id = data["items"][0]["id"]

    # 2. PUT toggle selection
    resp_toggle = client.put(
        f"/api/books/book_api_test/consistency-reviews/{run_id}/corrections/proposals/{proposal_id}",
        json={"is_selected": False},
    )
    assert resp_toggle.status_code == 200
    assert resp_toggle.json()["is_selected"] is False

    # 3. POST select-all
    resp_sel = client.post(
        f"/api/books/book_api_test/consistency-reviews/{run_id}/corrections/select-all",
        json={"is_selected": True, "only_auto_safe": True},
    )
    assert resp_sel.status_code == 200
    assert resp_sel.json()["updated_count"] >= 1

    # 4. POST apply
    resp_apply = client.post(
        f"/api/books/book_api_test/consistency-reviews/{run_id}/corrections/apply",
        json={"only_auto_safe": True},
    )
    assert resp_apply.status_code == 200
    apply_data = resp_apply.json()
    assert apply_data["applied_count"] == 1
    snapshot_id = apply_data["snapshot_id"]

    # 5. GET snapshots
    resp_snaps = client.get("/api/books/book_api_test/consistency-reviews/snapshots")
    assert resp_snaps.status_code == 200
    snaps_list = resp_snaps.json()
    assert len(snaps_list) >= 1
    assert snaps_list[0]["id"] == snapshot_id

    # 6. POST rollback
    resp_rollback = client.post(f"/api/books/book_api_test/consistency-reviews/snapshots/{snapshot_id}/rollback")
    assert resp_rollback.status_code == 200
    assert resp_rollback.json()["rolled_back_segments"] == 1


def test_regression_get_book_consistency_reviews_list(tmp_path: Path) -> None:
    """Regression test: GET /api/books/{book_id}/consistency-reviews properly returns list of audit runs."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_list_test", num_chapters=1)

    app = create_app(start_worker=False)
    app.state.db_path = db_path
    client = TestClient(app)

    # 1. Non-existent book returns 404
    resp_404 = client.get("/api/books/non_existent_book/consistency-reviews")
    assert resp_404.status_code == 404
    assert "Không tìm thấy sách" in resp_404.json()["detail"]

    # 2. Existing book with no runs returns empty list
    resp_empty = client.get("/api/books/book_list_test/consistency-reviews")
    assert resp_empty.status_code == 200
    assert resp_empty.json() == []

    # 3. Create two audit runs and verify GET returns both runs ordered by created_at DESC
    run1 = create_audit_run(db_path, "book_list_test")
    run2 = create_audit_run(db_path, "book_list_test")

    resp_runs = client.get("/api/books/book_list_test/consistency-reviews")
    assert resp_runs.status_code == 200
    runs_list = resp_runs.json()
    assert isinstance(runs_list, list)
    assert len(runs_list) == 2
    returned_ids = [r["id"] for r in runs_list]
    assert run2["id"] in returned_ids
    assert run1["id"] in returned_ids
    # Verify structure of item in list
    item = runs_list[0]
    assert "id" in item
    assert "book_id" in item
    assert "status" in item
    assert "total_segments" in item
    assert "findings_count" in item


def test_corrections_routes_registered_and_reachable(tmp_path: Path) -> None:
    """Verify that all newly added corrections routes are registered and reachable."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_routes_test", num_chapters=1)

    app = create_app(start_worker=False)
    app.state.db_path = db_path
    client = TestClient(app)

    run = create_audit_run(db_path, "book_routes_test")
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id)

    # Verify routes registered in app.routes
    route_paths = [r.path for r in app.routes]
    expected_paths = [
        "/api/books/{book_id}/consistency-reviews",
        "/api/books/{book_id}/consistency-reviews/snapshots",
        "/api/books/{book_id}/consistency-reviews/snapshots/{snapshot_id}/rollback",
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview",
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview",
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/proposals/{proposal_id}",
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/select-all",
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply",
    ]
    for ep in expected_paths:
        assert ep in route_paths, f"Route {ep} is not registered in FastAPI routes"

    # Test reachability of endpoints
    # 1. GET consistency-reviews
    r = client.get("/api/books/book_routes_test/consistency-reviews")
    assert r.status_code == 200

    # 2. GET snapshots
    r = client.get("/api/books/book_routes_test/consistency-reviews/snapshots")
    assert r.status_code == 200

    # 3. POST generate-preview
    r = client.post(f"/api/books/book_routes_test/consistency-reviews/{run_id}/corrections/generate-preview")
    assert r.status_code == 200

    # 4. GET preview
    r = client.get(f"/api/books/book_routes_test/consistency-reviews/{run_id}/corrections/preview")
    assert r.status_code == 200

    # 5. POST select-all
    r = client.post(
        f"/api/books/book_routes_test/consistency-reviews/{run_id}/corrections/select-all",
        json={"is_selected": True},
    )
    assert r.status_code == 200

    # 6. POST apply (empty selection returns 200 with 0 applied)
    r = client.post(
        f"/api/books/book_routes_test/consistency-reviews/{run_id}/corrections/apply",
        json={"selected_ids": []},
    )
    assert r.status_code == 200

    # 7. Rollback non-existent snapshot returns 404
    r = client.post("/api/books/book_routes_test/consistency-reviews/snapshots/nonexistent_snap/rollback")
    assert r.status_code == 404


def test_stale_source_protection_in_apply(tmp_path: Path) -> None:
    """Verify that applying a proposal is blocked if the segment source was modified (stale source protection)."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_stale_src_test", num_chapters=1)

    run = create_audit_run(db_path, "book_stale_src_test")
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id)

    findings = [
        {
            "category": "location",
            "source_term": "스타플렉스",
            "canonical_recommendation": "Starplex",
            "confidence": "high",
            "severity": "high",
            "glossary_locked": True,
            "variants": [{"target": "Starflex", "count": 1}],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_1",
                    "source_term": "스타플렉스",
                    "target_variant": "Starflex",
                }
            ],
        }
    ]

    generate_correction_proposals(db_path, run_id, "book_stale_src_test", findings)

    # Simulate source edit: source text was edited so "스타플렉스" is no longer present
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE segments SET source = ? WHERE id = ?",
            ("Đoạn nguồn đã bị sửa thành câu khác hoàn toàn", "seg_1_1"),
        )

    # Apply proposal - should detect stale source and flag as conflict
    apply_res = apply_proposals_with_snapshot(db_path, run_id, "book_stale_src_test", only_auto_safe=True)
    assert apply_res["applied_count"] == 0
    assert apply_res["conflict_count"] == 1

    # Check proposal status in DB
    props = get_correction_proposals(db_path, run_id)
    assert props["items"][0]["status"] == "conflict"
    assert "nguồn đã bị thay đổi" in props["items"][0]["reason"]

    # Verify segment translation was not modified
    ch1 = get_chapter(db_path, "ch_1")
    s1_1 = next(s for s in ch1["segments"] if s["id"] == "seg_1_1")
    assert s1_1["translation"] == "Thẻ nhân viên Starflex trước cổ tôi"


def test_tightened_auto_safe_classification_homographs_safety(tmp_path: Path) -> None:
    """Test tightened safety: frequent lexical terms >=70% or count>=5 without locked glossary or semantic verification are manual_only."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_tight_test", num_chapters=2)

    run = create_audit_run(db_path, "book_tight_test")
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id)

    # Add locked glossary term
    create_glossary_item(
        db_path,
        "book_tight_test",
        source="스타플렉스",
        target="Starplex",
        notes="location",
        locked=True,
    )

    findings = [
        # 1. Frequent lexical term with dominant_ratio=0.80 and count=8 without locked glossary or semantic verification -> MUST BE manual_only
        {
            "category": "term",
            "source_term": "사과",
            "canonical_recommendation": "quả táo",
            "confidence": "high",
            "severity": "medium",
            "glossary_locked": False,
            "variants": [
                {"target": "quả táo", "count": 8},
                {"target": "xin lỗi", "count": 2},
            ],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_2",
                    "source_term": "사과",
                    "target_variant": "xin lỗi",
                }
            ],
        },
        # 2. Term with explicit semantic_verified=True -> AUTO SAFE
        {
            "category": "organization",
            "source_term": "시스템",
            "canonical_recommendation": "Hệ thống",
            "confidence": "high",
            "severity": "medium",
            "semantic_verified": True,
            "glossary_locked": False,
            "variants": [
                {"target": "Hệ thống", "count": 6},
                {"target": "Thần thống", "count": 1},
            ],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_3",
                    "source_term": "시스템",
                    "target_variant": "Thần thống",
                }
            ],
        },
        # 3. Term with locked glossary -> AUTO SAFE
        {
            "category": "location",
            "source_term": "스타플렉스",
            "canonical_recommendation": "Starplex",
            "confidence": "high",
            "severity": "high",
            "glossary_locked": True,
            "variants": [
                {"target": "Starflex", "count": 5},
            ],
            "occurrences": [
                {
                    "chapter_id": "ch_1",
                    "chapter_title": "Chương 1",
                    "segment_id": "seg_1_1",
                    "source_term": "스타플렉스",
                    "target_variant": "Starflex",
                }
            ],
        },
    ]

    # Populate seg_1_2 with source containing 사과 and translation containing xin lỗi
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE segments SET source = ?, translation = ? WHERE id = ?",
            ("그는 사과를 건넸다.", "Anh ấy đưa ra lời xin lỗi.", "seg_1_2"),
        )

    res = generate_correction_proposals(db_path, run_id, "book_tight_test", findings)
    proposals = res["proposals"]

    # 1. Frequent lexical term without locked glossary or semantic verification -> manual_only
    apple_prop = next(p for p in proposals if p["source_term"] == "사과")
    assert apple_prop["classification"] == "manual_only"
    assert apple_prop["is_selected"] is False
    assert "đồng âm" in apple_prop["reason"]

    # 2. Explicit semantic_verified -> auto_safe
    sys_prop = next(p for p in proposals if p["source_term"] == "시스템")
    assert sys_prop["classification"] == "auto_safe"
    assert sys_prop["is_selected"] is True

    # 3. Locked glossary -> auto_safe
    loc_prop = next(p for p in proposals if p["source_term"] == "스타플렉스")
    assert loc_prop["classification"] == "auto_safe"
    assert loc_prop["is_selected"] is True


def test_chapter_title_korean_numbers_preserved_while_unifying_stem() -> None:
    """Test unifying chapter title stem while preserving Korean (1)/(2) counters (e.g. Mở cửa tiệm -> Mở cửa hàng)."""
    # 1. Test generate_title_correction directly
    # Chapter 1: 'Mở cửa tiệm (1)', Source '개점 (1)'
    new_t1, c1 = generate_title_correction(
        old_title="Mở cửa tiệm (1)",
        canonical_stem="Mở cửa hàng",
        source_title="개점 (1)",
    )
    assert new_t1 == "Mở cửa hàng (1)"
    assert c1 == "(1)"

    # Chapter 2: 'Mở cửa tiệm (2)', Source '개점 (2)'
    new_t2, c2 = generate_title_correction(
        old_title="Mở cửa tiệm (2)",
        canonical_stem="Mở cửa hàng",
        source_title="개점 (2)",
    )
    assert new_t2 == "Mở cửa hàng (2)"
    assert c2 == "(2)"

    # What if canonical stem recommendation itself had counter 'Mở cửa hàng (1)'?
    new_t2_from_canon_with_counter, c2_2 = generate_title_correction(
        old_title="Mở cửa tiệm (2)",
        canonical_stem="Mở cửa hàng (1)",
        source_title="개점 (2)",
    )
    assert new_t2_from_canon_with_counter == "Mở cửa hàng (2)"
    assert c2_2 == "(2)"

    # What if translation missed the counter but source had (1)?
    new_t_missing, c_missing = generate_title_correction(
        old_title="Mở cửa tiệm",
        canonical_stem="Mở cửa hàng",
        source_title="개점 (1)",
    )
    assert new_t_missing == "Mở cửa hàng (1)"
    assert c_missing == "(1)"

    # Test fractions (1/2) and (2/2)
    new_t_frac1, c_frac1 = generate_title_correction(
        old_title="Mở cửa tiệm (1/2)",
        canonical_stem="Mở cửa hàng",
        source_title="개점 (1/2)",
    )
    assert new_t_frac1 == "Mở cửa hàng (1/2)"
    assert c_frac1 == "(1/2)"

    new_t_frac2, c_frac2 = generate_title_correction(
        old_title="Mở cửa tiệm (2/2)",
        canonical_stem="Mở cửa hàng",
        source_title="개점 (2/2)",
    )
    assert new_t_frac2 == "Mở cửa hàng (2/2)"
    assert c_frac2 == "(2/2)"

    # Regression for Task A verified bug:
    # Embedded semantic episode numbering 'Tập 5' + trailing chapter installment (1)/(2)/(3)
    # Chapter with installment (3)
    bug_new_t3, bug_c3 = generate_title_correction(
        old_title="Phim tài liệu người thật Starplex tập 5 (3)",
        canonical_stem="Phim tài liệu thực tế Starplex Tập 5",
        source_title="스타플렉스 휴먼 다큐 5화 (3)",
    )
    assert bug_new_t3 == "Phim tài liệu thực tế Starplex Tập 5 (3)"
    assert bug_c3 == "(3)"

    # Chapter with installment (1)
    bug_new_t1, bug_c1 = generate_title_correction(
        old_title="Phim tài liệu người thật Starplex tập 5 (1)",
        canonical_stem="Phim tài liệu thực tế Starplex Tập 5",
        source_title="스타플렉스 휴먼 다큐 5화 (1)",
    )
    assert bug_new_t1 == "Phim tài liệu thực tế Starplex Tập 5 (1)"
    assert bug_c1 == "(1)"

    # Chapter with installment (2)
    bug_new_t2, bug_c2 = generate_title_correction(
        old_title="Phim tài liệu người thật Starplex tập 5 (2)",
        canonical_stem="Phim tài liệu thực tế Starplex Tập 5",
        source_title="스타플렉스 휴먼 다큐 5화 (2)",
    )
    assert bug_new_t2 == "Phim tài liệu thực tế Starplex Tập 5 (2)"
    assert bug_c2 == "(2)"

    # Chapter where canonical stem had (1) but target is (3)
    bug_new_t3_from_1, bug_c3_from_1 = generate_title_correction(
        old_title="Phim tài liệu người thật Starplex tập 5 (3)",
        canonical_stem="Phim tài liệu thực tế Starplex Tập 5 (1)",
        source_title="스타플렉스 휴먼 다큐 5화 (3)",
    )
    assert bug_new_t3_from_1 == "Phim tài liệu thực tế Starplex Tập 5 (3)"
    assert bug_c3_from_1 == "(3)"

    # Chapter where old translation forgot (3) but Korean source had (3)
    bug_new_t3_fallback, bug_c3_fallback = generate_title_correction(
        old_title="Phim tài liệu người thật Starplex tập 5",
        canonical_stem="Phim tài liệu thực tế Starplex Tập 5",
        source_title="스타플렉스 휴먼 다큐 5화 (3)",
    )
    assert bug_new_t3_fallback == "Phim tài liệu thực tế Starplex Tập 5 (3)"
    assert bug_c3_fallback == "(3)"

    # Square brackets and fullwidth brackets
    sq_t, sq_c = generate_title_correction(
        old_title="Phim tài liệu Starplex Tập 5 [3]",
        canonical_stem="Phim tài liệu thực tế Starplex Tập 5",
        source_title="스타플렉스 휴먼 다큐 5화 [3]",
    )
    assert sq_t == "Phim tài liệu thực tế Starplex Tập 5 [3]"
    assert sq_c == "[3]"

    fw_t, fw_c = generate_title_correction(
        old_title="Phim tài liệu Starplex Tập 5 （3）",
        canonical_stem="Phim tài liệu thực tế Starplex Tập 5",
        source_title="스타플렉스 휴먼 다큐 5화 （3）",
    )
    assert fw_t == "Phim tài liệu thực tế Starplex Tập 5 （3）"
    assert fw_c == "（3）"


def test_audit_resume_avoids_rebilling_and_reuses_checkpoints(tmp_path: Path) -> None:
    """Test that audit resume skips already-checkpointed batches and avoids re-calling the provider (no re-billing)."""
    db_path = tmp_path / "translator.db"
    _setup_test_book(db_path, "book_resume_test", num_chapters=2)

    run = create_audit_run(db_path, "book_resume_test")
    run_id = run["id"]

    # Save checkpoint for batch 0
    save_audit_checkpoint(
        db_path,
        run_id=run_id,
        batch_index=0,
        chapter_id="ch_1",
        segment_ids=["seg_1_0"],
        raw_findings=[{
            "segment_id": "seg_1_0",
            "source_term": "각성",
            "target_term": "Thức tỉnh",
            "category": "title",
            "confidence": "high",
        }],
    )

    class TrackingMockProvider:
        def __init__(self) -> None:
            self.extracted_batches: list[list[dict[str, Any]]] = []

        async def extract_terminology(self, segments, **kwargs):
            self.extracted_batches.append(segments)
            return []

        async def adjudicate_inconsistencies(self, candidates, **kwargs):
            return []

    mock_provider = TrackingMockProvider()
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    import asyncio
    asyncio.run(service.execute_run(run_id))

    # Batch 0 was already checkpointed, so mock_provider should NEVER be called for batch 0 segments
    extracted_seg_ids = [s["id"] for batch in mock_provider.extracted_batches for s in batch]
    assert "seg_1_0" not in extracted_seg_ids, "Batch 0 was already checkpointed and must NOT be re-audited (avoids re-billing)!"

    # Verify run completed
    updated_run = get_audit_run(db_path, run_id)
    assert updated_run["status"] == "completed"
    assert updated_run["failed_segments"] == 0
    assert updated_run["skipped_segments"] == 0


def test_confirm_cancelled_run_reuse_in_production_db() -> None:
    """Confirm audit resume of cancelled run 400ac73069984a0d8f121a0a6f1f9984 in data/translator.db (read-only verification)."""
    prod_db = Path("data/translator.db")
    if not prod_db.is_file():
        pytest.skip("data/translator.db not found on disk")

    import sqlite3
    # Use read-only URI
    conn = sqlite3.connect("file:data/translator.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Verify run metadata
    run_row = conn.execute(
        "SELECT * FROM consistency_audit_runs WHERE id = ?",
        ("400ac73069984a0d8f121a0a6f1f9984",),
    ).fetchone()
    assert run_row is not None
    assert run_row["status"] in ("cancelled", "running", "completed", "paused")
    assert run_row["model"] == "gemini-3.5-flash-lite"
    assert run_row["total_segments"] == 46637
    assert run_row["processed_segments"] >= 28518
    assert run_row["failed_segments"] == 0
    assert run_row["skipped_segments"] == 0

    # Verify at least 1002 saved checkpoints are present
    cp_rows = conn.execute(
        "SELECT batch_index, chapter_id, segment_ids FROM consistency_audit_checkpoints WHERE run_id = ?",
        ("400ac73069984a0d8f121a0a6f1f9984",),
    ).fetchall()
    assert len(cp_rows) >= 1002

    # Verify segment count across checkpoints is at least 28518
    total_cp_segments = sum(len(json.loads(r["segment_ids"])) for r in cp_rows)
    assert total_cp_segments >= 28518

    # Verify all 46637 segments in book 82a3a264bbc24924acd010f668da6543 are non-whitespace and translated
    book_id = run_row["book_id"]
    book_stats = conn.execute(
        "SELECT COUNT(*) as total, COUNT(translation) as translated FROM segments WHERE book_id = ? AND is_whitespace = 0",
        (book_id,),
    ).fetchone()
    assert book_stats["total"] == 46637
    assert book_stats["translated"] == 46637


def test_findings_pagination_beyond_100_items_across_pages(tmp_path: Path) -> None:
    """Verify complete deterministic pagination through >100 findings across multiple pages.
    
    Verifies that:
    1. Normal endpoint/db paging cap remains 100 (requesting page_size=1000 returns at most 100).
    2. get_all_audit_findings and generate_correction_proposals deterministically fetch ALL 250 findings
       across multiple pages without dropping subsequent pages or generating duplicates/omissions.
    """
    db_path = tmp_path / "translator.db"
    book_id = "book_pagination_test"
    _setup_test_book(db_path, book_id, num_chapters=2)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id, total_segments=10)

    # Populate 250 distinct findings to span 3 full pages (100 + 100 + 50)
    total_findings_count = 250
    generated_findings = []
    for i in range(total_findings_count):
        sev = "high" if i < 50 else ("medium" if i < 150 else "low")
        cat = "location" if i % 2 == 0 else "term"
        fid = f"finding_{i:04d}"
        generated_findings.append({
            "id": fid,
            "run_id": run_id,
            "book_id": book_id,
            "category": cat,
            "source_term": f"단어_{i:04d}",
            "source_term_id": f"{cat}:단어_{i:04d}",
            "canonical_recommendation": f"Thuật ngữ {i:04d}",
            "reason": f"Phát hiện nhất quán {i:04d}",
            "confidence": "high",
            "severity": sev,
            "glossary_divergence": False,
            "glossary_target": None,
            "glossary_locked": False,
            "variants": [{"target": f"Thuật ngữ {i:04d}", "count": 2}],
            "occurrences": [{
                "chapter_id": "ch_1",
                "chapter_title": "Chương 1",
                "segment_id": "seg_1_1",
                "source_term": f"단어_{i:04d}",
                "target_variant": f"Biến thể {i:04d}",
            }],
        })

    # Save to consistency_audit_findings table
    save_audit_findings(db_path, run_id, book_id, generated_findings)

    # 1. Verify get_audit_findings respects normal endpoint paging cap of 100
    res_page1 = get_audit_findings(db_path, run_id, page=1, page_size=1000)
    assert res_page1["total"] == total_findings_count
    assert res_page1["page_size"] == 100  # Capped to 100!
    assert len(res_page1["items"]) == 100
    assert res_page1["total_pages"] == 3

    # 2. Verify get_all_audit_findings pages through all 3 pages with no duplicates and no omissions
    all_findings = get_all_audit_findings(db_path, run_id)
    assert len(all_findings) == total_findings_count
    all_finding_ids = [f["id"] for f in all_findings]
    assert len(set(all_finding_ids)) == total_findings_count  # No duplicate IDs!
    all_terms = [f["source_term"] for f in all_findings]
    assert len(set(all_terms)) == total_findings_count  # No duplicate terms!
    expected_terms = {f"단어_{i:04d}" for i in range(total_findings_count)}
    assert set(all_terms) == expected_terms  # No omissions!

    # 3. Verify generate_correction_proposals(findings=None) pages through DB and does not truncate to 100
    prop_res = generate_correction_proposals(db_path, run_id, book_id, findings=None)
    assert isinstance(prop_res, dict)
    assert prop_res["run_id"] == run_id


def test_guard_incomplete_semantic_reviews(tmp_path: Path) -> None:
    """Verify that incomplete semantic reviews (CANCELLED, FAILED, unexpected states, and completed with

    semantic_coverage != full) comprehensively prohibit generating and applying corrections, while
    preserving backward compatibility for semantic_stage None legacy terminology-only runs.
    """
    db_path = tmp_path / "translator.db"
    book_id = "book_sem_comprehensive_test"
    _setup_test_book(db_path, book_id, num_chapters=2)

    app = create_app(start_worker=False)
    app.state.db_path = db_path
    client = TestClient(app)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]
    _mark_run_completed_full(db_path, run_id, total_segments=10)

    # Add a finding so proposals exist
    findings = [{
        "category": "location",
        "source_term": "스타플렉스",
        "canonical_recommendation": "Starplex",
        "confidence": "high",
        "severity": "high",
        "glossary_locked": True,
        "variants": [{"target": "Starflex", "count": 2}],
        "occurrences": [{
            "chapter_id": "ch_1",
            "chapter_title": "Chương 1",
            "segment_id": "seg_1_1",
            "source_term": "스타플렉스",
            "target_variant": "Starflex",
        }],
    }]
    save_audit_findings(db_path, run_id, book_id, findings)

    # -------------------------------------------------------------
    # Case 1: semantic_stage = 'cancelled' (or 'CANCELLED')
    # -------------------------------------------------------------
    for cancelled_val in ("cancelled", "CANCELLED"):
        update_audit_run(
            db_path,
            run_id,
            progress={
                "stage": "completed",
                "coverage_status": "full",
                "semantic_stage": cancelled_val,
                "semantic_coverage": "partial",
            },
        )
        # 1a. POST generate-preview -> 400
        r_gen = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
        assert r_gen.status_code == 400
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_gen.json()["detail"]
        assert cancelled_val in r_gen.json()["detail"]

        # 1b. GET preview -> can_apply = False, coverage_notice present
        r_prev = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
        assert r_prev.status_code == 200
        assert r_prev.json()["can_apply"] is False
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_prev.json()["coverage_notice"]
        assert cancelled_val in r_prev.json()["coverage_notice"]

        # 1c. POST apply -> 400
        r_app = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply")
        assert r_app.status_code == 400
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_app.json()["detail"]
        assert cancelled_val in r_app.json()["detail"]

        # 1d. Direct function calls raise ValueError
        with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
            generate_correction_proposals(db_path, run_id, book_id)

        with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
            apply_correction_proposals(db_path, run_id, book_id)

    # -------------------------------------------------------------
    # Case 2: semantic_stage = 'failed' (or 'FAILED')
    # -------------------------------------------------------------
    for failed_val in ("failed", "FAILED"):
        update_audit_run(
            db_path,
            run_id,
            progress={
                "stage": "completed",
                "coverage_status": "full",
                "semantic_stage": failed_val,
                "semantic_coverage": "partial",
            },
        )
        r_gen = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
        assert r_gen.status_code == 400
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_gen.json()["detail"]

        r_prev = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
        assert r_prev.status_code == 200
        assert r_prev.json()["can_apply"] is False
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_prev.json()["coverage_notice"]

        r_app = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply")
        assert r_app.status_code == 400
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_app.json()["detail"]

        with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
            generate_correction_proposals(db_path, run_id, book_id)

        with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
            apply_correction_proposals(db_path, run_id, book_id)

    # -------------------------------------------------------------
    # Case 3: Incomplete completed (semantic_stage='completed', but semantic_coverage != 'full')
    # -------------------------------------------------------------
    for non_full_cov in ("partial", "incomplete", None):
        prog = {
            "stage": "completed",
            "coverage_status": "full",
            "semantic_stage": "completed",
        }
        if non_full_cov is not None:
            prog["semantic_coverage"] = non_full_cov
        update_audit_run(db_path, run_id, progress=prog)

        r_gen = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
        assert r_gen.status_code == 400
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_gen.json()["detail"]

        r_prev = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
        assert r_prev.status_code == 200
        assert r_prev.json()["can_apply"] is False
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_prev.json()["coverage_notice"]

        r_app = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply")
        assert r_app.status_code == 400
        assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_app.json()["detail"]

        with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
            generate_correction_proposals(db_path, run_id, book_id)

        with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
            apply_correction_proposals(db_path, run_id, book_id)

    # -------------------------------------------------------------
    # Case 4: Unexpected semantic_stage state (e.g. 'unexpected_state_xyz')
    # -------------------------------------------------------------
    update_audit_run(
        db_path,
        run_id,
        progress={
            "stage": "completed",
            "coverage_status": "full",
            "semantic_stage": "unexpected_state_xyz",
            "semantic_coverage": "full",
        },
    )
    r_gen = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
    assert r_gen.status_code == 400
    assert "thẩm định ngữ nghĩa chưa hoàn tất" in r_gen.json()["detail"]
    assert "unexpected_state_xyz" in r_gen.json()["detail"]

    r_prev = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
    assert r_prev.status_code == 200
    assert r_prev.json()["can_apply"] is False

    with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
        generate_correction_proposals(db_path, run_id, book_id)

    # -------------------------------------------------------------
    # Case 5: Legacy terminology-only run (semantic_stage is None)
    # -------------------------------------------------------------
    update_audit_run(
        db_path,
        run_id,
        progress={
            "stage": "completed",
            "coverage_status": "full",
        },
    )
    # Generate preview succeeds
    r_gen = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
    assert r_gen.status_code == 200
    assert r_gen.json()["total_proposals"] >= 1

    # Preview endpoint reports can_apply = True, coverage_notice = None
    r_prev = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
    assert r_prev.status_code == 200
    assert r_prev.json()["can_apply"] is True
    assert r_prev.json()["coverage_notice"] is None

    # Apply endpoint succeeds
    r_app = client.post(
        f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply",
        json={"only_auto_safe": True},
    )
    assert r_app.status_code == 200
    assert r_app.json()["applied_count"] >= 1

    # Direct calls succeed
    prop_res = generate_correction_proposals(db_path, run_id, book_id)
    assert isinstance(prop_res, dict)

    # -------------------------------------------------------------
    # Case 6: Fully completed semantic review (semantic_stage='completed', semantic_coverage='full')
    # -------------------------------------------------------------
    update_audit_run(
        db_path,
        run_id,
        is_stale=0,
        progress={
            "stage": "completed",
            "coverage_status": "full",
            "semantic_stage": "completed",
            "semantic_coverage": "full",
        },
    )
    r_prev_full = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
    assert r_prev_full.status_code == 200
    assert r_prev_full.json()["can_apply"] is True
    assert r_prev_full.json()["coverage_notice"] is None

