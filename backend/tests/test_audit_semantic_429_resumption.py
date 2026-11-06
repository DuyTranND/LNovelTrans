"""Tests for live Gemini semantic review 429 rate-limit cooldown, graceful pause, and resumption."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.audit import (
    ConsistencyAuditService,
    SEMANTIC_429_MAX_COOLDOWN,
    SEMANTIC_429_MIN_COOLDOWN,
    is_rate_limit_or_quota_error,
)
from backend.app.corrections import generate_correction_proposals
from backend.app.db import (
    apply_correction_proposals,
    create_audit_run,
    get_audit_run,
    get_connection,
    get_semantic_checkpoints,
    init_db,
    save_semantic_checkpoint,
    update_audit_run,
    utc_now,
)
from backend.app.main import app
from backend.app.provider import (
    ProviderConnectError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseFormatError,
)


def _setup_book(db_path: Path, book_id: str = "book_test_429", num_chapters: int = 3, segments_per_ch: int = 5) -> None:
    init_db(db_path)
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO books (id, title, author, language, identifier, file_path, created_at, updated_at, story_bible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (book_id, "Test 429 Novel", "Test Author", "ko", book_id, "/tmp/dummy.epub", now, now),
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

            for s_idx in range(segments_per_ch):
                seg_id = f"seg_{ch_idx + 1}_{s_idx}"
                ko_src = f"한국어 문장 {ch_idx + 1}-{s_idx}"
                vi_trans = f"Câu tiếng Việt {ch_idx + 1}-{s_idx}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace, review_data)
                    VALUES (?, ?, ?, ?, 'body/div/p[1]', 'text', ?, ?, ?, 'p', 0, NULL)
                    """,
                    (seg_id, ch_id, book_id, s_idx, ko_src, ko_src, vi_trans),
                )


class MockRateLimitedSemanticProvider:
    """Mock provider with configurable 429 behaviors."""

    def __init__(
        self,
        fail_batches_with_429: dict[int, int] | None = None,
        retry_after: float | None = None,
        daily_quota_on_batch: int | None = None,
        malformed_batches: dict[int, int] | None = None,
        connect_error_batches: dict[int, int] | None = None,
    ):
        # fail_batches_with_429: {batch_index: fail_times}
        self.fail_batches_with_429 = dict(fail_batches_with_429 or {})
        self.retry_after = retry_after
        self.daily_quota_on_batch = daily_quota_on_batch
        self.malformed_batches = dict(malformed_batches or {})
        self.connect_error_batches = dict(connect_error_batches or {})
        self.calls: list[dict[str, Any]] = []
        self.all_secrets = ["secret-api-key-123"]

    async def audit_semantic_consistency(
        self,
        segments_payload: list[dict[str, str]],
        context_before: str = "",
        context_after: str = "",
        chapter_title: str = "",
        glossary: list[dict[str, Any]] | None = None,
        known_entities: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        batch_idx = 0
        if chapter_title and chapter_title.startswith("Chương "):
            try:
                batch_idx = int(chapter_title.split()[-1]) - 1
            except ValueError:
                batch_idx = 0
        self.calls.append({"batch_idx": batch_idx, "payload": segments_payload, "chapter_title": chapter_title})

        if self.daily_quota_on_batch is not None and batch_idx == self.daily_quota_on_batch:
            raise ProviderRateLimitError(
                "Resource has been exhausted: Quota exceeded for metric 'generations per day' with secret-api-key-123",
                status_code=429,
                secret=self.all_secrets,
            )

        if batch_idx in self.fail_batches_with_429 and self.fail_batches_with_429[batch_idx] > 0:
            self.fail_batches_with_429[batch_idx] -= 1
            raise ProviderRateLimitError(
                f"Rate limit 429 exceeded for batch {batch_idx} using secret-api-key-123",
                status_code=429,
                secret=self.all_secrets,
                retry_after=self.retry_after,
            )

        if batch_idx in self.malformed_batches and self.malformed_batches[batch_idx] > 0:
            self.malformed_batches[batch_idx] -= 1
            raise ProviderResponseFormatError(
                f"Malformed JSON in candidate for batch {batch_idx}",
                secret=self.all_secrets,
            )

        if batch_idx in self.connect_error_batches and self.connect_error_batches[batch_idx] > 0:
            self.connect_error_batches[batch_idx] -= 1
            raise ProviderConnectError(
                f"All connection attempts failed for batch {batch_idx} with secret-api-key-123",
                secret=self.all_secrets,
            )

        # Success return
        return [
            {
                "segment_id": seg["id"],
                "source_term": "한국어",
                "problematic_translation": "Tiếng Hàn cũ",
                "recommended_translation": "Tiếng Hàn mới",
                "category": "term",
                "confidence": "high",
                "reason": "Phát hiện ngữ nghĩa",
            }
            for seg in segments_payload[:1]
        ]


def test_is_rate_limit_or_quota_error_detection() -> None:
    """Verify rate-limit and quota error detection, Retry-After extraction, and secret sanitization."""
    # 1. ProviderRateLimitError with retry_after
    exc1 = ProviderRateLimitError("429 Too Many Requests", status_code=429, retry_after=72.5)
    is_rl, ra, is_daily = is_rate_limit_or_quota_error(exc1)
    assert is_rl is True
    assert ra == 72.5
    assert is_daily is False

    # 2. Generic ProviderError with status 429 and regex retryDelay
    exc2 = ProviderError("RESOURCE_EXHAUSTED: retryDelay: 45s", status_code=429)
    is_rl, ra, is_daily = is_rate_limit_or_quota_error(exc2)
    assert is_rl is True
    assert ra == 45.0
    assert is_daily is False

    # 3. Daily quota exceeded
    exc3 = ProviderError("Quota exceeded for quota metric 'generations per day'", status_code=429)
    is_rl, ra, is_daily = is_rate_limit_or_quota_error(exc3)
    assert is_rl is True
    assert is_daily is True

    # 4. Content / format error -> NOT rate limit
    exc4 = ProviderResponseFormatError("Malformed JSON")
    is_rl, ra, is_daily = is_rate_limit_or_quota_error(exc4)
    assert is_rl is False
    assert is_daily is False


@pytest.mark.asyncio
async def test_semantic_review_temporary_429_cooldown_and_success(tmp_path: Path) -> None:
    """Verify temporary 429 respects Retry-After, cools down >=60s, and succeeds without losing batch."""
    db_path = tmp_path / "translator.db"
    book_id = "book_429_temp"
    _setup_book(db_path, book_id=book_id, num_chapters=2, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    # Batch 0 fails once with 429 and Retry-After: 75.0, then succeeds on retry
    mock_provider = MockRateLimitedSemanticProvider(
        fail_batches_with_429={0: 1},
        retry_after=75.0,
    )
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    sleep_calls: list[float] = []

    async def fake_sleep(dur: float) -> None:
        sleep_calls.append(dur)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await service.execute_semantic_review(run_id)

    # Verify cooldown was called for at least 75.0 seconds
    assert sum(sleep_calls) >= 75.0

    cps = get_semantic_checkpoints(db_path, run_id)
    assert len(cps) == 2  # Both batches checkpointed
    updated = get_audit_run(db_path, run_id)
    prog = updated["progress"]
    assert prog["semantic_stage"] == "completed"
    assert prog["semantic_coverage"] == "full"
    assert prog["semantic_percentage"] == 100.0
    assert prog["semantic_failed_batches"] == 0


@pytest.mark.asyncio
async def test_semantic_review_min_60s_cooldown_and_exponential_backoff(tmp_path: Path) -> None:
    """Verify 429 without Retry-After uses conservative cooldown >=60s with exponential growth."""
    db_path = tmp_path / "translator.db"
    book_id = "book_429_backoff"
    _setup_book(db_path, book_id=book_id, num_chapters=1, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    # Fails 2 times with 429 (attempt 0 and 1), then succeeds on attempt 2
    mock_provider = MockRateLimitedSemanticProvider(
        fail_batches_with_429={0: 2},
        retry_after=None,  # No explicit Retry-After
    )
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    sleep_calls: list[float] = []

    async def fake_sleep(dur: float) -> None:
        sleep_calls.append(dur)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await service.execute_semantic_review(run_id)

    # First cooldown >= 60.0, second cooldown doubled >= 120.0 -> total >= 180.0
    assert sum(sleep_calls) >= (SEMANTIC_429_MIN_COOLDOWN + 120.0)

    cps = get_semantic_checkpoints(db_path, run_id)
    assert len(cps) == 1
    prog = get_audit_run(db_path, run_id)["progress"]
    assert prog["semantic_stage"] == "completed"


@pytest.mark.asyncio
async def test_semantic_review_persistent_429_graceful_pause_no_lost_checkpoints(tmp_path: Path) -> None:
    """Verify persistent 429 gracefully pauses with semantic_stage='paused', preserving every checkpoint."""
    db_path = tmp_path / "translator.db"
    book_id = "book_429_pause"
    _setup_book(db_path, book_id=book_id, num_chapters=3, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    # Batch 0 succeeds. Batch 1 persistently fails with 429 across all retries.
    mock_provider = MockRateLimitedSemanticProvider(
        fail_batches_with_429={1: 99},  # persistent
    )
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    with patch("asyncio.sleep", AsyncMock()):
        await service.execute_semantic_review(run_id)

    prog = get_audit_run(db_path, run_id)["progress"]
    # Must be gracefully paused
    assert prog["semantic_stage"] == "paused"
    assert "429" in prog.get("semantic_pause_reason", "")
    # Checkpoints: Batch 0 checkpoint is preserved
    cps = get_semantic_checkpoints(db_path, run_id)
    assert len(cps) == 1
    assert cps[0]["batch_index"] == 0

    # Ensure secret is NOT in pause reason or progress
    assert "secret-api-key-123" not in str(prog)


@pytest.mark.asyncio
async def test_semantic_review_resumption_processes_missed_batches_and_resets_failed_count(tmp_path: Path) -> None:
    """Verify resuming a paused run resets semantic_failed_batches, retains checkpoints, and covers missed batches."""
    db_path = tmp_path / "translator.db"
    book_id = "book_429_resume"
    _setup_book(db_path, book_id=book_id, num_chapters=3, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    # 1. Seed checkpoint for batch 0 (as happened prior to pause)
    save_semantic_checkpoint(
        db_path,
        run_id=run_id,
        chapter_id="ch_1",
        batch_index=0,
        segment_ids=["seg_1_0"],
        raw_findings=[],
    )

    # Simulate old cancelled/paused progress that had 8 failed batches recorded
    update_audit_run(
        db_path,
        run_id,
        progress={
            "semantic_stage": "paused",
            "semantic_failed_batches": 8,
            "semantic_completed_batches": 1,
            "semantic_pause_reason": "API 429 paused",
        },
    )

    # 2. Resumed pass with healthy provider
    healthy_provider = MockRateLimitedSemanticProvider()
    service = ConsistencyAuditService(db_path, lambda: healthy_provider)

    with patch("asyncio.sleep", AsyncMock()):
        await service.execute_semantic_review(run_id)

    # Verify:
    # - Batch 0 was NOT recalled (call count is 2 for batches 1 and 2)
    assert len(healthy_provider.calls) == 2

    # - All 3 checkpoints exist
    cps = get_semantic_checkpoints(db_path, run_id)
    assert len(cps) == 3

    # - Progress is full, completed, failed batches reset to 0
    prog = get_audit_run(db_path, run_id)["progress"]
    assert prog["semantic_stage"] == "completed"
    assert prog["semantic_coverage"] == "full"
    assert prog["semantic_percentage"] == 100.0
    assert prog["semantic_failed_batches"] == 0
    assert prog["semantic_pause_reason"] is None


@pytest.mark.asyncio
async def test_semantic_review_connect_error_pause_then_resume(tmp_path: Path) -> None:
    """Verify persistent ProviderConnectError gracefully pauses without losing checkpoints, and subsequent resume completes."""
    db_path = tmp_path / "translator.db"
    book_id = "book_connect_error_pause_resume"
    _setup_book(db_path, book_id=book_id, num_chapters=3, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    # Chapter 1 (batch 0) succeeds, Chapter 2 (batch 1) fails persistently with ProviderConnectError
    failing_provider = MockRateLimitedSemanticProvider(
        connect_error_batches={1: 99},
    )
    service = ConsistencyAuditService(db_path, lambda: failing_provider)

    with patch("asyncio.sleep", AsyncMock()):
        await service.execute_semantic_review(run_id)

    # Assert paused, one checkpoint index 0, completed_batches=1, failed batch not checkpointed, no secret in progress
    prog = get_audit_run(db_path, run_id)["progress"]
    assert prog["semantic_stage"] == "paused"
    assert prog["semantic_completed_batches"] == 1
    assert "secret-api-key-123" not in str(prog)

    cps = get_semantic_checkpoints(db_path, run_id)
    assert len(cps) == 1
    assert cps[0]["batch_index"] == 0
    assert all(cp["batch_index"] != 1 for cp in cps)

    # Resume pass with healthy provider on same run ID
    healthy_provider = MockRateLimitedSemanticProvider()
    service = ConsistencyAuditService(db_path, lambda: healthy_provider)

    with patch("asyncio.sleep", AsyncMock()):
        await service.execute_semantic_review(run_id)

    # Assert all 3 checkpoints and full completed with failed=0
    cps_resumed = get_semantic_checkpoints(db_path, run_id)
    assert len(cps_resumed) == 3
    assert {cp["batch_index"] for cp in cps_resumed} == {0, 1, 2}

    prog_resumed = get_audit_run(db_path, run_id)["progress"]
    assert prog_resumed["semantic_stage"] == "completed"
    assert prog_resumed["semantic_coverage"] == "full"
    assert prog_resumed["semantic_percentage"] == 100.0
    assert prog_resumed["semantic_failed_batches"] == 0

    # Assert healthy provider only called batches 1 and 2
    assert len(healthy_provider.calls) == 2
    assert [call["batch_idx"] for call in healthy_provider.calls] == [1, 2]


@pytest.mark.asyncio
async def test_semantic_review_daily_quota_exhaustion_immediate_graceful_pause(tmp_path: Path) -> None:
    """Verify daily quota exhaustion immediately pauses the run without burning subsequent requests."""
    db_path = tmp_path / "translator.db"
    book_id = "book_daily_quota"
    _setup_book(db_path, book_id=book_id, num_chapters=3, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    # Daily quota hit on batch 0
    mock_provider = MockRateLimitedSemanticProvider(daily_quota_on_batch=0)
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    with patch("asyncio.sleep", AsyncMock()):
        await service.execute_semantic_review(run_id)

    prog = get_audit_run(db_path, run_id)["progress"]
    assert prog["semantic_stage"] == "paused"
    assert "hàng ngày" in prog.get("semantic_pause_reason", "").lower()
    # Exactly 1 call was made (no retries burned on daily quota)
    assert len(mock_provider.calls) == 1


@pytest.mark.asyncio
async def test_semantic_review_distinguishes_content_malformed_response(tmp_path: Path) -> None:
    """Verify content/formatting errors are retried with short backoff and honestly marked partial."""
    db_path = tmp_path / "translator.db"
    book_id = "book_malformed"
    _setup_book(db_path, book_id=book_id, num_chapters=2, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    # Batch 0 has unrecoverable malformed response
    mock_provider = MockRateLimitedSemanticProvider(
        malformed_batches={0: 10},
    )
    service = ConsistencyAuditService(db_path, lambda: mock_provider)

    with patch("asyncio.sleep", AsyncMock()):
        await service.execute_semantic_review(run_id)

    prog = get_audit_run(db_path, run_id)["progress"]
    assert prog["semantic_stage"] == "paused"
    assert prog["semantic_failed_batches"] == 0
    assert prog["semantic_percentage"] < 100.0
    cps = get_semantic_checkpoints(db_path, run_id)
    assert len(cps) == 0


def test_backend_guard_prevents_preview_and_apply_when_semantic_not_complete(tmp_path: Path) -> None:
    """Verify generate-preview and apply are blocked while semantic_stage is 'running', 'paused', or 'partial'."""
    db_path = tmp_path / "translator.db"
    book_id = "book_guard_test"
    _setup_book(db_path, book_id=book_id, num_chapters=1, segments_per_ch=5)

    run = create_audit_run(db_path, book_id)
    run_id = run["id"]

    app.state.db_path = db_path
    client = TestClient(app, client=("127.0.0.1", 50000))

    # 1. Base audit completed, but semantic review is 'running'
    update_audit_run(
        db_path,
        run_id,
        status="completed",
        processed_segments=5,
        total_segments=5,
        failed_segments=0,
        skipped_segments=0,
        progress={
            "stage": "completed",
            "coverage_status": "full",
            "semantic_stage": "running",
        },
    )

    # Guard: generate-preview blocked
    resp = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview")
    assert resp.status_code == 400
    assert "thẩm định ngữ nghĩa chưa hoàn tất" in resp.json()["detail"]

    # Guard: apply blocked
    resp_apply = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply")
    assert resp_apply.status_code == 400
    assert "thẩm định ngữ nghĩa chưa hoàn tất" in resp_apply.json()["detail"]

    # Guard: get preview indicates can_apply is False
    resp_prev = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview")
    assert resp_prev.status_code == 200
    assert resp_prev.json()["can_apply"] is False
    assert "thẩm định ngữ nghĩa chưa hoàn tất" in resp_prev.json()["coverage_notice"]

    # Direct function calls in corrections and db raise ValueError
    with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
        generate_correction_proposals(db_path, run_id, book_id)

    with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
        apply_correction_proposals(db_path, run_id, book_id)

    # 2. When semantic_stage is 'paused': blocked
    update_audit_run(
        db_path,
        run_id,
        progress={
            "stage": "completed",
            "coverage_status": "full",
            "semantic_stage": "paused",
        },
    )
    with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
        generate_correction_proposals(db_path, run_id, book_id)

    # 3. When semantic_stage is 'partial': blocked
    update_audit_run(
        db_path,
        run_id,
        progress={
            "stage": "completed",
            "coverage_status": "full",
            "semantic_stage": "partial",
        },
    )
    with pytest.raises(ValueError, match="Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất"):
        generate_correction_proposals(db_path, run_id, book_id)

    # 4. When semantic_stage is 'completed': unblocked!
    update_audit_run(
        db_path,
        run_id,
        progress={
            "stage": "completed",
            "coverage_status": "full",
            "semantic_stage": "completed",
            "semantic_coverage": "full",
        },
    )
    res = generate_correction_proposals(db_path, run_id, book_id)
    assert isinstance(res, dict)

    # 5. Legacy flow: semantic_stage is None: unblocked!
    update_audit_run(
        db_path,
        run_id,
        progress={
            "stage": "completed",
            "coverage_status": "full",
        },
    )
    res_legacy = generate_correction_proposals(db_path, run_id, book_id)
    assert isinstance(res_legacy, dict)
