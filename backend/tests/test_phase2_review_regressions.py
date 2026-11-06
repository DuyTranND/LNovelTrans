"""Targeted regression tests for Phase 2 review resumption and quality gate reconciliation.

Verifies:
1) previously reviewed row with review_data JSON [] is skipped on resume;
2) quality-gate-only pending row gets one review but no duplicate review on resume even if findings remain;
3) fixed warning after review is cleared while unresolved warning stays actionable and marked reviewed;
4) old review issue rows not re-reviewed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.db import (
    create_job,
    get_chapter,
    get_connection,
    init_db,
)
from backend.app.jobs import JobWorker, needs_llm_review
from backend.app.quality_gate import is_quality_finding_resolved


def _seed_book_with_translations(
    db_path: Path,
    book_id: str = "book_reg_p2",
    segments_data: list[dict] | None = None,
) -> str:
    """Helper to seed a test book with populated translation and review_data rows."""
    now = "2026-10-25T12:00:00Z"
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (book_id, "Regression Novel", "dummy.epub", now, now),
        )
        ch_id = f"ch_{book_id}"
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES (?, ?, 'ch1', 0, 'OEBPS/ch1.xhtml', 'ch1.xhtml', 'Chương 1', 'application/xhtml+xml', ?, 'untranslated', ?, ?)
            """,
            (ch_id, book_id, b"<html/>", now, now),
        )

        if not segments_data:
            segments_data = [
                {
                    "id": "s1",
                    "source": "제1장 시작",
                    "translation": "Chương 1 Bắt đầu",
                    "review_data": None,
                }
            ]

        for idx, seg in enumerate(segments_data):
            rdata_val = seg.get("review_data")
            if isinstance(rdata_val, (list, dict)):
                rdata_val = json.dumps(rdata_val, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, review_data, is_whitespace, parent_tag)
                VALUES (?, ?, ?, ?, ?, 'text', ?, ?, ?, ?, 0, 'p')
                """,
                (
                    seg["id"],
                    ch_id,
                    book_id,
                    idx,
                    f"/p[{idx+1}]",
                    seg["source"],
                    seg["source"],
                    seg.get("translation"),
                    rdata_val,
                ),
            )

    return ch_id


def _make_mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.all_secrets = None
    provider.api_key = "dummy_key"
    provider.is_model_supported.return_value = True
    provider.translate = AsyncMock(return_value={})
    provider.review = AsyncMock(return_value={"corrections": {}, "issues": []})
    provider.summarize = AsyncMock(return_value="Tóm tắt chương.")
    return provider


# =============================================================================
# Unit tests for needs_llm_review and is_quality_finding_resolved
# =============================================================================


def test_needs_llm_review_comprehensive_matrix():
    """Verify matrix of review_data values for needs_llm_review."""
    # 1. Untranslated / empty translation: never reviewed
    assert needs_llm_review({"translation": "", "review_data": None}) is False
    assert needs_llm_review({"translation": "   ", "review_data": None}) is False
    assert needs_llm_review({"translation": None, "review_data": None}) is False

    # 2. None review_data: needs review
    assert needs_llm_review({"translation": "Đã dịch", "review_data": None}) is True

    # 3. JSON [] or list []: completed review with no issues -> never reprocessed
    assert needs_llm_review({"translation": "Đã dịch", "review_data": []}) is False
    assert needs_llm_review({"translation": "Đã dịch", "review_data": "[]"}) is False

    # 4. Normal existing human/LLM issue lists: already reviewed -> never reprocessed
    assert needs_llm_review({
        "translation": "Đã dịch",
        "review_data": [{"id": "s1", "type": "style", "description": "Văn phong"}],
    }) is False
    assert needs_llm_review({
        "translation": "Đã dịch",
        "review_data": json.dumps([{"id": "s1", "type": "grammar", "description": "Lỗi ngữ pháp"}]),
    }) is False

    # 5. Pending quality gate finding (not yet marked reviewed): needs first review
    pending_qg = [{"type": "quality_gate", "category": "locked_character", "description": "Thiếu nhân vật"}]
    assert needs_llm_review({"translation": "Đã dịch", "review_data": pending_qg}) is True
    assert needs_llm_review({"translation": "Đã dịch", "review_data": json.dumps(pending_qg)}) is True

    # 6. Quality gate finding already reviewed (marked reviewed): completed review -> skipped on resume
    reviewed_qg = [{
        "type": "quality_gate",
        "category": "locked_character",
        "description": "Thiếu nhân vật",
        "reviewed": True,
    }]
    assert needs_llm_review({"translation": "Đã dịch", "review_data": reviewed_qg}) is False
    assert needs_llm_review({"translation": "Đã dịch", "review_data": json.dumps(reviewed_qg)}) is False

    # Also status == "reviewed" backwards-compatible check
    status_reviewed_qg = [{
        "type": "quality_gate",
        "category": "locked_character",
        "description": "Thiếu nhân vật",
        "status": "reviewed",
    }]
    assert needs_llm_review({"translation": "Đã dịch", "review_data": status_reviewed_qg}) is False

    # 7. Multiple findings where at least one is pending: needs review
    mixed_qg = [
        {"type": "quality_gate", "category": "locked_character", "reviewed": True},
        {"type": "quality_gate", "category": "length_anomaly"},  # pending
    ]
    assert needs_llm_review({"translation": "Đã dịch", "review_data": mixed_qg}) is True


def test_is_quality_finding_resolved_behavior():
    """Verify quality gate resolution evaluation for different categories."""
    # Locked character / glossary: resolved when expected_target is present in new translation
    qg_char = {
        "type": "quality_gate",
        "category": "locked_character",
        "expected_target": "Kim Chul Soo",
        "source_term": "김철수",
    }
    assert is_quality_finding_resolved(qg_char, "김철수", "Kim Chul Soo đã tới.") is True
    assert is_quality_finding_resolved(qg_char, "김철수", "Anh ta đã tới.") is False

    # Untranslated Hangul: resolved when >=4 Hangul run is gone
    qg_hangul = {"type": "quality_gate", "category": "untranslated_hangul"}
    assert is_quality_finding_resolved(qg_hangul, "원문", "Đoạn dịch tiếng Việt thuần") is True
    assert is_quality_finding_resolved(qg_hangul, "원문", "Đoạn dịch còn 안녕하세요 tiếng Hàn") is False

    # Empty output: resolved when non-empty
    qg_empty = {"type": "quality_gate", "category": "empty_output"}
    assert is_quality_finding_resolved(qg_empty, "Source", "Dịch xong") is True
    assert is_quality_finding_resolved(qg_empty, "Source", "   ") is False


# =============================================================================
# Targeted Test 1: Previously reviewed row with review_data JSON [] is skipped on resume
# =============================================================================

@pytest.mark.asyncio
async def test_previously_reviewed_row_with_empty_array_skipped_on_resume(tmp_path: Path):
    """Verify that a row with review_data '[]' is recognized as already reviewed and skipped on resume."""
    db_file = tmp_path / "test_skip_empty_array.db"
    init_db(db_file)

    # Seed segment with translation already populated and review_data = []
    ch_id = _seed_book_with_translations(
        db_file,
        book_id="book_skip_empty",
        segments_data=[
            {
                "id": "s1",
                "source": "안녕 세상아",
                "translation": "Xin chào thế giới",
                "review_data": [],  # historically empty array = completed review
            }
        ],
    )

    mock_provider = _make_mock_provider()
    worker = JobWorker(db_file, lambda: mock_provider)

    job = create_job(
        db_file,
        book_id="book_skip_empty",
        chapter_ids=[ch_id],
        params={"review": True},
    )

    done_job = await worker.step()
    assert done_job is not None
    assert done_job["status"] == "completed"

    # review() must NOT have been called because the segment was already reviewed with []
    assert mock_provider.review.call_count == 0


# =============================================================================
# Targeted Test 2: Quality-gate-only pending row gets one review but no duplicate review on resume
# =============================================================================

@pytest.mark.asyncio
async def test_quality_gate_only_pending_row_gets_one_review_and_no_duplicate_on_resume(tmp_path: Path):
    """Verify quality-gate-only row gets exactly 1 review pass, and resume does not trigger a duplicate review."""
    db_file = tmp_path / "test_qg_one_review.db"
    init_db(db_file)

    # Seed segment with pending quality gate finding (no 'reviewed' flag)
    pending_finding = {
        "type": "quality_gate",
        "category": "locked_character",
        "severity": "warning",
        "description": "Nhân vật đã khóa '김철수' chưa được dịch thành 'Kim Chul Soo'.",
        "source_term": "김철수",
        "expected_target": "Kim Chul Soo",
    }
    ch_id = _seed_book_with_translations(
        db_file,
        book_id="book_qg_one_review",
        segments_data=[
            {
                "id": "s1",
                "source": "김철수는 달렸다",
                "translation": "Anh ta đã chạy",  # Missing Kim Chul Soo
                "review_data": [pending_finding],
            }
        ],
    )

    mock_provider = _make_mock_provider()
    # Reviewer returns NO correction, so the warning remains unresolved
    mock_provider.review = AsyncMock(return_value={"corrections": {}, "issues": []})

    worker = JobWorker(db_file, lambda: mock_provider)

    # First pass: job with review=True
    job1 = create_job(
        db_file,
        book_id="book_qg_one_review",
        chapter_ids=[ch_id],
        params={"review": True},
    )

    done_job1 = await worker.step()
    assert done_job1 is not None
    assert done_job1["status"] == "completed"

    # Exactly 1 review call occurred
    assert mock_provider.review.call_count == 1

    # In DB, finding remains actionable, but is now marked reviewed: True
    chap = get_chapter(db_file, ch_id)
    seg = chap["segments"][0]
    r_data = seg["review_data"]
    assert isinstance(r_data, list)
    assert len(r_data) == 1
    assert r_data[0]["type"] == "quality_gate"
    assert r_data[0]["expected_target"] == "Kim Chul Soo"
    assert r_data[0]["reviewed"] is True

    # Second pass: simulating resume / retry job
    mock_provider.review.reset_mock()
    job2 = create_job(
        db_file,
        book_id="book_qg_one_review",
        chapter_ids=[ch_id],
        params={"review": True},
    )

    done_job2 = await worker.step()
    assert done_job2 is not None
    assert done_job2["status"] == "completed"

    # NO duplicate review call on resume even though quality gate findings remain!
    assert mock_provider.review.call_count == 0


# =============================================================================
# Targeted Test 3: Fixed warning cleared while unresolved warning stays actionable and marked reviewed
# =============================================================================

@pytest.mark.asyncio
async def test_fixed_warning_cleared_while_unresolved_warning_stays_actionable_and_marked_reviewed(tmp_path: Path):
    """Verify that after review, fixed warnings are removed, and unresolved warnings stay with reviewed=True."""
    db_file = tmp_path / "test_fixed_cleared.db"
    init_db(db_file)

    # Segment 1 has TWO quality gate warnings:
    # 1) Missing locked character "Kim Chul Soo"
    # 2) Missing locked glossary "Ma Kiếm"
    warning_char = {
        "type": "quality_gate",
        "category": "locked_character",
        "severity": "warning",
        "description": "Nhân vật đã khóa '김철수' chưa được dịch thành 'Kim Chul Soo'.",
        "source_term": "김철수",
        "expected_target": "Kim Chul Soo",
    }
    warning_gloss = {
        "type": "quality_gate",
        "category": "locked_glossary",
        "severity": "warning",
        "description": "Thuật ngữ đã khóa '마검' chưa được dịch thành 'Ma Kiếm'.",
        "source_term": "마검",
        "expected_target": "Ma Kiếm",
    }

    ch_id = _seed_book_with_translations(
        db_file,
        book_id="book_fixed_cleared",
        segments_data=[
            {
                "id": "s1",
                "source": "김철수는 마검을 들었다",
                "translation": "Hắn ta cầm thanh kiếm đen lên.",  # Missing BOTH targets
                "review_data": [warning_char, warning_gloss],
            }
        ],
    )

    mock_provider = _make_mock_provider()
    # Reviewer corrects Kim Chul Soo, but still omits Ma Kiếm:
    mock_provider.review = AsyncMock(
        return_value={
            "corrections": {"s1": "Kim Chul Soo cầm thanh kiếm đen lên."},
            "issues": [],
        }
    )

    worker = JobWorker(db_file, lambda: mock_provider)

    job = create_job(
        db_file,
        book_id="book_fixed_cleared",
        chapter_ids=[ch_id],
        params={"review": True},
    )

    done_job = await worker.step()
    assert done_job is not None
    assert done_job["status"] == "completed"

    # Check segment results in DB
    chap = get_chapter(db_file, ch_id)
    seg = chap["segments"][0]

    # Translation updated with correction
    assert "Kim Chul Soo" in seg["translation"]

    r_data = seg["review_data"]
    assert isinstance(r_data, list)

    # Warning for "Kim Chul Soo" was FIXED and must be CLEARED
    assert not any(item.get("expected_target") == "Kim Chul Soo" for item in r_data)

    # Warning for "Ma Kiếm" was NOT fixed and must STAY actionable with reviewed: True
    unresolved = [item for item in r_data if item.get("expected_target") == "Ma Kiếm"]
    assert len(unresolved) == 1
    assert unresolved[0]["category"] == "locked_glossary"
    assert unresolved[0]["severity"] == "warning"
    assert unresolved[0]["reviewed"] is True


# =============================================================================
# Targeted Test 4: Old review issue rows not re-reviewed
# =============================================================================

@pytest.mark.asyncio
async def test_old_review_issue_rows_not_rereviewed(tmp_path: Path):
    """Verify that rows with existing human or LLM review issue dictionaries are not re-reviewed."""
    db_file = tmp_path / "test_old_review_issues.db"
    init_db(db_file)

    # Seed segments with preexisting style and grammar issues from prior review
    ch_id = _seed_book_with_translations(
        db_file,
        book_id="book_old_issues",
        segments_data=[
            {
                "id": "s1",
                "source": "바람이 불었다",
                "translation": "Gió thổi nhè nhẹ qua đồi",
                "review_data": [{"id": "s1", "type": "style", "description": "Câu hơi dài dòng"}],
            },
            {
                "id": "s2",
                "source": "하늘이 푸르렀다",
                "translation": "Bầu trời trong xanh thăm thẳm",
                "review_data": [{"id": "s2", "type": "terminology", "description": "Lưu ý thuật ngữ"}],
            },
        ],
    )

    mock_provider = _make_mock_provider()
    worker = JobWorker(db_file, lambda: mock_provider)

    job = create_job(
        db_file,
        book_id="book_old_issues",
        chapter_ids=[ch_id],
        params={"review": True},
    )

    done_job = await worker.step()
    assert done_job is not None
    assert done_job["status"] == "completed"

    # review() must NOT have been called because both rows had prior review issues recorded
    assert mock_provider.review.call_count == 0


# =============================================================================
# Targeted Test 5: Review correction rollback re-evaluates gate on final saved text
# =============================================================================

@pytest.mark.asyncio
async def test_review_rollback_critical_defect_re_evaluates_gate_and_clears_stale_findings(tmp_path: Path):
    """Verify that when a reviewer correction introduces critical defects (e.g. untranslated Hangul),
    the correction is rolled back, quality gate is re-evaluated on final saved text,
    stale critical findings from the discarded correction are not stored, and
    prior unresolved warnings are preserved.
    """
    db_file = tmp_path / "test_rollback_gate.db"
    init_db(db_file)

    prior_warning_s1 = {
        "type": "quality_gate",
        "category": "locked_glossary",
        "severity": "warning",
        "description": "Thuật ngữ đã khóa '검' chưa được dịch thành 'Thần Kiếm'.",
        "source_term": "검",
        "expected_target": "Thần Kiếm",
    }
    prior_warning_s2 = {
        "type": "quality_gate",
        "category": "locked_character",
        "severity": "warning",
        "description": "Nhân vật đã khóa '이진우' chưa được dịch thành 'Lee Jin-woo'.",
        "source_term": "이진우",
        "expected_target": "Lee Jin-woo",
    }

    ch_id = _seed_book_with_translations(
        db_file,
        book_id="book_rollback_gate",
        segments_data=[
            {
                "id": "s1",
                "source": "김철수는 검을 들었다",
                "translation": "Kim Chul Soo cầm thanh kiếm lên.",  # Prior translation, missing 'Thần Kiếm'
                "review_data": [prior_warning_s1],
            },
            {
                "id": "s2",
                "source": "이진우가 달려왔다",
                "translation": "Hắn ta chạy tới.",  # Prior translation, missing 'Lee Jin-woo'
                "review_data": [prior_warning_s2],
            },
        ],
    )

    mock_provider = _make_mock_provider()
    # Reviewer output:
    # - s1 has a critical defect: introduces untranslated Hangul runs >= 4 (검을뽑았다)
    # - s2 has a clean, valid correction fixing 'Lee Jin-woo'
    mock_provider.review = AsyncMock(
        return_value={
            "corrections": {
                "s1": "Kim Chul Soo cầm kiếm lên 검을뽑았다 검을뽑았다.",
                "s2": "Lee Jin-woo chạy tới nhanh như chớp.",
            },
            "issues": [
                {"id": "s1", "type": "style", "description": "Ghi chú phong cách cho s1"},
            ],
        }
    )

    worker = JobWorker(db_file, lambda: mock_provider)

    job = create_job(
        db_file,
        book_id="book_rollback_gate",
        chapter_ids=[ch_id],
        params={"review": True},
    )

    done_job = await worker.step()
    assert done_job is not None
    assert done_job["status"] == "completed"

    chap = get_chapter(db_file, ch_id)
    seg1 = next(s for s in chap["segments"] if s["id"] == "s1")
    seg2 = next(s for s in chap["segments"] if s["id"] == "s2")

    # 1. s1 translation was rolled back to prior translation
    assert seg1["translation"] == "Kim Chul Soo cầm thanh kiếm lên."

    # 2. s1 review_data must NOT contain stale critical untranslated Hangul warning
    r_data_1 = seg1["review_data"]
    assert isinstance(r_data_1, list)
    hangul_findings = [f for f in r_data_1 if f.get("category") == "untranslated_hangul"]
    assert len(hangul_findings) == 0, f"Found stale untranslated_hangul finding on rolled-back segment: {r_data_1}"

    # 3. Prior unresolved warning for s1 (missing 'Thần Kiếm') must be preserved with reviewed=True
    unresolved_1 = [f for f in r_data_1 if f.get("expected_target") == "Thần Kiếm"]
    assert len(unresolved_1) == 1
    assert unresolved_1[0]["reviewed"] is True

    # 4. s2 valid correction was applied
    assert seg2["translation"] == "Lee Jin-woo chạy tới nhanh như chớp."

    # 5. s2 prior warning for 'Lee Jin-woo' was resolved and cleared
    r_data_2 = seg2["review_data"]
    resolved_char_findings = [f for f in r_data_2 if f.get("expected_target") == "Lee Jin-woo"]
    assert len(resolved_char_findings) == 0
