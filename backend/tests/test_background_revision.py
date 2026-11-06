"""Focused tests for segments.revision advancement during background operations.

Tests verify that background writers (translation job, review job,
consistency correction apply, and consistency correction rollback)
increment segments.revision when segment content or review state changes,
preventing stale editor drafts from overwriting background modifications.
All tests run against isolated mock sqlite databases in tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from backend.app.db import (
    RevisionConflictError,
    apply_correction_proposals,
    get_connection,
    init_db,
    rollback_correction_snapshot,
    update_chapter_segments,
    utc_now,
)
from backend.app.jobs import JobWorker


def _setup_isolated_book_and_segment(
    db_path: Path,
    book_id: str = "book_rev_test",
    chapter_id: str = "ch_rev_1",
    segment_id: str = "seg_rev_1",
    source: str = "테스트 문장입니다.",
    translation: str | None = None,
    review_data: str | None = None,
) -> None:
    """Helper to seed minimal database with one book, chapter, and segment."""
    init_db(db_path)
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO books (id, title, author, language, identifier, file_path, created_at, updated_at, story_bible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (book_id, "Revision Test Book", "Author", "ko", book_id, "/dummy/test.epub", now, now),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO chapters (
                id, book_id, manifest_id, spine_index, archive_path, href,
                title, media_type, raw_bytes, status, created_at, updated_at
            ) VALUES (?, ?, ?, 0, 'OEBPS/ch1.xhtml', 'ch1.xhtml', 'Chapter 1', 'application/xhtml+xml', ?, 'translated', ?, ?)
            """,
            (chapter_id, book_id, "item_1", b"<html/>", now, now),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO segments (
                id, chapter_id, book_id, seq_order, dom_path, node_type,
                original_source, source, translation, parent_tag, is_whitespace,
                review_data, revision
            ) VALUES (?, ?, ?, 1, 'body/p[1]', 'text', ?, ?, ?, 'p', 0, ?, 1)
            """,
            (segment_id, chapter_id, book_id, source, source, translation, review_data),
        )


def _get_segment_row(db_path: Path, segment_id: str) -> dict:
    """Fetch segment by id as dictionary."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, translation, review_data, revision FROM segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
        assert row is not None, f"Segment '{segment_id}' not found."
        return dict(row)


# ===========================================================================
# 1. Consistency Correction Apply Tests
# ===========================================================================

def test_correction_apply_advances_revision_and_blocks_stale_editor(tmp_path: Path):
    """Applying a consistency correction proposal must advance segments.revision

    and block any stale editor draft from overwriting the corrected content.
    """
    db_path = tmp_path / "test.db"
    book_id = "book_corr"
    ch_id = "ch_corr_1"
    seg_id = "seg_corr_1"
    run_id = "run_corr_1"
    prop_id = "prop_1"

    _setup_isolated_book_and_segment(
        db_path,
        book_id=book_id,
        chapter_id=ch_id,
        segment_id=seg_id,
        source="스타플렉스 사원증",
        translation="Thẻ nhân viên Starflex",
    )

    initial_seg = _get_segment_row(db_path, seg_id)
    assert initial_seg["revision"] == 1
    assert initial_seg["translation"] == "Thẻ nhân viên Starflex"

    # Editor reads chapter at revision 1
    stale_expected_revision = initial_seg["revision"]

    # Insert consistency audit run and proposal using actual schema
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_audit_runs (
                id, book_id, status, model, total_chapters, total_segments,
                processed_segments, failed_segments, skipped_segments,
                progress, is_stale, created_at, updated_at
            ) VALUES (?, ?, 'completed', 'gemini-3.5-flash-lite', 1, 1, 1, 0, 0, ?, 0, ?, ?)
            """,
            (run_id, book_id, json.dumps({"coverage_status": "full"}), now, now),
        )
        conn.execute(
            """
            INSERT INTO consistency_correction_proposals (
                id, run_id, book_id, chapter_id, segment_id, source_term, canonical_target,
                old_text, new_text, category, confidence, classification, reason, is_selected,
                status, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, '스타플렉스', 'StarFlex Chuẩn',
                'Thẻ nhân viên Starflex', 'Thẻ nhân viên StarFlex Chuẩn', 'term', 'high', 'auto_safe',
                'term correction', 1, 'pending', ?, ?
            )
            """,
            (prop_id, run_id, book_id, ch_id, seg_id, now, now),
        )

    # Background operation: apply correction proposal
    result = apply_correction_proposals(db_path, run_id, book_id, [prop_id])
    assert result["applied_count"] == 1

    # Verify background update advanced revision to 2 and updated translation
    updated_seg = _get_segment_row(db_path, seg_id)
    assert updated_seg["revision"] == 2
    assert updated_seg["translation"] == "Thẻ nhân viên StarFlex Chuẩn"

    # Editor attempts to save using stale revision 1
    with pytest.raises(RevisionConflictError) as exc_info:
        update_chapter_segments(
            db_path=db_path,
            chapter_id=ch_id,
            segment_updates=[
                {
                    "id": seg_id,
                    "translation": "Bản dịch cũ của editor ghi đè",
                    "expected_revision": stale_expected_revision,
                }
            ],
        )

    conflict = exc_info.value.conflicts[0]
    assert conflict["segment_id"] == seg_id
    assert conflict["expected_revision"] == 1
    assert conflict["current_revision"] == 2

    # Retain corrected background content
    retained_seg = _get_segment_row(db_path, seg_id)
    assert retained_seg["translation"] == "Thẻ nhân viên StarFlex Chuẩn"
    assert retained_seg["revision"] == 2


def test_correction_apply_noop_does_not_advance_revision(tmp_path: Path):
    """Re-applying an already applied or identical proposal must not increment revision."""
    db_path = tmp_path / "test_noop.db"
    book_id = "book_noop"
    ch_id = "ch_noop_1"
    seg_id = "seg_noop_1"
    run_id = "run_noop_1"
    prop_id = "prop_noop_1"

    # Segment already has the target translation
    _setup_isolated_book_and_segment(
        db_path,
        book_id=book_id,
        chapter_id=ch_id,
        segment_id=seg_id,
        source="용어",
        translation="Đã chuẩn hóa từ trước",
    )

    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_audit_runs (
                id, book_id, status, model, total_chapters, total_segments,
                processed_segments, failed_segments, skipped_segments,
                progress, is_stale, created_at, updated_at
            ) VALUES (?, ?, 'completed', 'gemini-3.5-flash-lite', 1, 1, 1, 0, 0, ?, 0, ?, ?)
            """,
            (run_id, book_id, json.dumps({"coverage_status": "full"}), now, now),
        )
        conn.execute(
            """
            INSERT INTO consistency_correction_proposals (
                id, run_id, book_id, chapter_id, segment_id, source_term, canonical_target,
                old_text, new_text, category, confidence, classification, reason, is_selected,
                status, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, '용어', 'Đã chuẩn hóa từ trước',
                'Khác', 'Đã chuẩn hóa từ trước', 'term', 'high', 'auto_safe',
                'noop test', 1, 'pending', ?, ?
            )
            """,
            (prop_id, run_id, book_id, ch_id, seg_id, now, now),
        )

    seg_before = _get_segment_row(db_path, seg_id)
    assert seg_before["revision"] == 1

    # Apply idempotent proposal
    result = apply_correction_proposals(db_path, run_id, book_id, [prop_id])
    assert result["applied_count"] == 0
    assert result["already_applied_count"] == 1

    # Revision must not have been incremented
    seg_after = _get_segment_row(db_path, seg_id)
    assert seg_after["revision"] == 1
    assert seg_after["translation"] == "Đã chuẩn hóa từ trước"


# ===========================================================================
# 2. Consistency Correction Rollback Tests
# ===========================================================================

def test_correction_rollback_advances_revision_and_blocks_stale_editor(tmp_path: Path):
    """Rolling back a correction snapshot must advance revision and block stale edits."""
    db_path = tmp_path / "test_rollback.db"
    book_id = "book_rb"
    ch_id = "ch_rb_1"
    seg_id = "seg_rb_1"
    run_id = "run_rb_1"
    prop_id = "prop_rb_1"

    _setup_isolated_book_and_segment(
        db_path,
        book_id=book_id,
        chapter_id=ch_id,
        segment_id=seg_id,
        source="원문",
        translation="Bản dịch gốc ban đầu",
    )

    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_audit_runs (
                id, book_id, status, model, total_chapters, total_segments,
                processed_segments, failed_segments, skipped_segments,
                progress, is_stale, created_at, updated_at
            ) VALUES (?, ?, 'completed', 'gemini-3.5-flash-lite', 1, 1, 1, 0, 0, ?, 0, ?, ?)
            """,
            (run_id, book_id, json.dumps({"coverage_status": "full"}), now, now),
        )
        conn.execute(
            """
            INSERT INTO consistency_correction_proposals (
                id, run_id, book_id, chapter_id, segment_id, source_term, canonical_target,
                old_text, new_text, category, confidence, classification, reason, is_selected,
                status, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, '원문', 'Bản dịch sau sửa',
                'Bản dịch gốc ban đầu', 'Bản dịch sau sửa', 'term', 'high', 'auto_safe',
                'test', 1, 'pending', ?, ?
            )
            """,
            (prop_id, run_id, book_id, ch_id, seg_id, now, now),
        )

    # 1. Apply correction -> revision advances 1 -> 2
    apply_res = apply_correction_proposals(db_path, run_id, book_id, [prop_id])
    snapshot_id = apply_res["snapshot_id"]
    assert snapshot_id is not None

    seg_applied = _get_segment_row(db_path, seg_id)
    assert seg_applied["revision"] == 2
    assert seg_applied["translation"] == "Bản dịch sau sửa"

    # Editor reads segment at revision 2
    stale_editor_revision = seg_applied["revision"]

    # 2. Rollback snapshot -> revision advances 2 -> 3
    rollback_res = rollback_correction_snapshot(db_path, snapshot_id)
    assert rollback_res["rolled_back_segments"] == 1
    with get_connection(db_path) as conn:
        snap_row = conn.execute(
            "SELECT is_rolled_back FROM consistency_correction_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        assert snap_row["is_rolled_back"] == 1

    seg_rolled_back = _get_segment_row(db_path, seg_id)
    assert seg_rolled_back["revision"] == 3
    assert seg_rolled_back["translation"] == "Bản dịch gốc ban đầu"

    # Editor attempts to save using stale revision 2
    with pytest.raises(RevisionConflictError) as exc_info:
        update_chapter_segments(
            db_path=db_path,
            chapter_id=ch_id,
            segment_updates=[
                {
                    "id": seg_id,
                    "translation": "Bản dịch của editor từ revision 2",
                    "expected_revision": stale_editor_revision,
                }
            ],
        )

    conflict = exc_info.value.conflicts[0]
    assert conflict["expected_revision"] == 2
    assert conflict["current_revision"] == 3

    # Retained rolled back content
    final_seg = _get_segment_row(db_path, seg_id)
    assert final_seg["translation"] == "Bản dịch gốc ban đầu"
    assert final_seg["revision"] == 3


def test_correction_rollback_noop_does_not_advance_revision(tmp_path: Path):
    """Rollback when translation is already equal to pre_text must not advance revision."""
    db_path = tmp_path / "test_rb_noop.db"
    book_id = "book_rb_noop"
    ch_id = "ch_rb_noop_1"
    seg_id = "seg_rb_noop_1"
    run_id = "run_rb_noop"
    snapshot_id = "snap_noop"

    # Pre-text and current text are identical
    _setup_isolated_book_and_segment(
        db_path,
        book_id=book_id,
        chapter_id=ch_id,
        segment_id=seg_id,
        source="원문",
        translation="Nội dung đã giống pre_text",
    )

    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_audit_runs (
                id, book_id, status, model, total_chapters, total_segments,
                processed_segments, failed_segments, skipped_segments,
                progress, is_stale, created_at, updated_at
            ) VALUES (?, ?, 'completed', 'gemini-3.5-flash-lite', 1, 1, 1, 0, 0, '{}', 0, ?, ?)
            """,
            (run_id, book_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO consistency_correction_snapshots (
                id, book_id, run_id, description, total_segments_changed, is_rolled_back, created_at
            ) VALUES (?, ?, ?, 'snapshot', 1, 0, ?)
            """,
            (snapshot_id, book_id, run_id, now),
        )
        conn.execute(
            """
            INSERT INTO consistency_correction_backup_items (
                id, snapshot_id, chapter_id, segment_id, pre_text, post_text, created_at
            ) VALUES ('item_1', ?, ?, ?, 'Nội dung đã giống pre_text', 'Nội dung khác', ?)
            """,
            (snapshot_id, ch_id, seg_id, now),
        )

    seg_before = _get_segment_row(db_path, seg_id)
    assert seg_before["revision"] == 1

    rollback_correction_snapshot(db_path, snapshot_id)

    seg_after = _get_segment_row(db_path, seg_id)
    assert seg_after["revision"] == 1
    assert seg_after["translation"] == "Nội dung đã giống pre_text"


# ===========================================================================
# 3. Background Translation & Review Job Worker Tests
# ===========================================================================

class MockTranslationProvider:
    """Mock LLM provider for translating and reviewing segments."""

    def __init__(self, translated_text: str = "Bản dịch từ JobWorker"):
        self.translated_text = translated_text
        self.reviewed_text = "Bản dịch đã qua reviewer LLM"

    async def translate(self, segments, context=None, **kwargs):
        return {s["id"]: self.translated_text for s in segments}

    async def review(self, segments, context=None, **kwargs):
        return {
            "issues": [],
            "corrections": {s["id"]: self.reviewed_text for s in segments},
        }

    async def summarize(self, segments, **kwargs):
        return "Tóm tắt chương test."


@pytest.mark.asyncio
async def test_job_translation_advances_revision_and_blocks_stale_editor(tmp_path: Path):
    """JobWorker translation phase must advance revision when populating translation."""
    db_path = tmp_path / "test_job_trans.db"
    book_id = "book_jt"
    ch_id = "ch_jt_1"
    seg_id = "seg_jt_1"
    job_id = "job_jt_1"

    # Segment starts untranslated
    _setup_isolated_book_and_segment(
        db_path,
        book_id=book_id,
        chapter_id=ch_id,
        segment_id=seg_id,
        source="안녕하세요 세계",
        translation=None,
    )

    seg_init = _get_segment_row(db_path, seg_id)
    assert seg_init["revision"] == 1
    assert seg_init["translation"] is None

    # Editor views chapter at revision 1
    stale_editor_revision = seg_init["revision"]

    # Create job in database and in-memory job_dict with chapter_ids and review: False
    trans_params = {
        "model": "gemini-3.5-flash-lite",
        "chapter_ids": [ch_id],
        "review": False,
        "max_segments": 1,
    }
    trans_progress = {"chapters": {ch_id: {"completed_segments": 0}}}
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, book_id, job_type, status, progress, params, created_at, updated_at)
            VALUES (?, ?, 'full', 'running', ?, ?, ?, ?)
            """,
            (
                job_id,
                book_id,
                json.dumps(trans_progress),
                json.dumps(trans_params),
                now,
                now,
            ),
        )

    provider = MockTranslationProvider(translated_text="Xin chào thế giới")
    worker = JobWorker(db_path, lambda: provider)

    job_dict = {
        "id": job_id,
        "book_id": book_id,
        "job_type": "full",
        "params": trans_params,
        "progress": trans_progress,
        "status": "running",
    }

    await worker._run_job(job_dict)

    # Verify background job updated translation and advanced revision
    seg_translated = _get_segment_row(db_path, seg_id)
    assert seg_translated["translation"] == "Xin chào thế giới"
    assert seg_translated["revision"] == 2

    # Editor tries to save draft with stale expected_revision 1
    with pytest.raises(RevisionConflictError) as exc_info:
        update_chapter_segments(
            db_path=db_path,
            chapter_id=ch_id,
            segment_updates=[
                {
                    "id": seg_id,
                    "translation": "Bản dịch tay đè lên",
                    "expected_revision": stale_editor_revision,
                }
            ],
        )

    conflict = exc_info.value.conflicts[0]
    assert conflict["expected_revision"] == 1
    assert conflict["current_revision"] == 2

    # Retain background translation
    retained_seg = _get_segment_row(db_path, seg_id)
    assert retained_seg["translation"] == "Xin chào thế giới"
    assert retained_seg["revision"] == 2


@pytest.mark.asyncio
async def test_job_reviewer_advances_revision_and_blocks_stale_editor(tmp_path: Path):
    """JobWorker review phase must advance revision when updating translation/review_data."""
    db_path = tmp_path / "test_job_rev.db"
    book_id = "book_jr"
    ch_id = "ch_jr_1"
    seg_id = "seg_jr_1"
    job_id = "job_jr_1"

    # Segment is already translated but has not completed LLM review
    _setup_isolated_book_and_segment(
        db_path,
        book_id=book_id,
        chapter_id=ch_id,
        segment_id=seg_id,
        source="검토 테스트",
        translation="Bản dịch sơ bộ",
        review_data=None,
    )

    seg_init = _get_segment_row(db_path, seg_id)
    assert seg_init["revision"] == 1

    # Editor reads segment at revision 1
    stale_editor_revision = seg_init["revision"]

    # Create job with review enabled in database and in-memory job_dict
    rev_params = {
        "model": "gemini-3.5-flash-lite",
        "chapter_ids": [ch_id],
        "review": True,
    }
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, book_id, job_type, status, progress, params, created_at, updated_at)
            VALUES (?, ?, 'full', 'running', '{}', ?, ?, ?)
            """,
            (
                job_id,
                book_id,
                json.dumps(rev_params),
                now,
                now,
            ),
        )

    provider = MockTranslationProvider()
    worker = JobWorker(db_path, lambda: provider)

    job_dict = {
        "id": job_id,
        "book_id": book_id,
        "job_type": "full",
        "params": rev_params,
        "progress": {},
        "status": "running",
    }

    await worker._run_job(job_dict)

    # Reviewer phase should have refined the translation and set review_data
    seg_reviewed = _get_segment_row(db_path, seg_id)
    assert seg_reviewed["translation"] == "Bản dịch đã qua reviewer LLM"
    assert seg_reviewed["revision"] == 2

    # Editor attempts to save draft using stale revision 1
    with pytest.raises(RevisionConflictError) as exc_info:
        update_chapter_segments(
            db_path=db_path,
            chapter_id=ch_id,
            segment_updates=[
                {
                    "id": seg_id,
                    "translation": "Bản dịch manual cũ",
                    "expected_revision": stale_editor_revision,
                }
            ],
        )

    conflict = exc_info.value.conflicts[0]
    assert conflict["expected_revision"] == 1
    assert conflict["current_revision"] == 2

    # Retain reviewer refined translation
    final_seg = _get_segment_row(db_path, seg_id)
    assert final_seg["translation"] == "Bản dịch đã qua reviewer LLM"
    assert final_seg["revision"] == 2
