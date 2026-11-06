"""Targeted reproduction and regression tests for AiNiee features audit fixes.

Tests:
1. Retranslation resets stale review_data to NULL so Phase 2 review is not skipped.
2. Locked character target precision: boundary-aware matching prevents substring false positives/negatives.
3. Quality gate: divider dashes / symbols do not trigger false repetition loops.
4. Preset fallback to default and cache key invalidation on update.
5. Discovery concurrent run prevention and proposal double-review guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient

from backend.app.context import (
    build_translation_context,
    match_character_term,
    TranslationMemory,
)
from backend.app.db import (
    approve_discovery_proposal,
    create_character,
    create_discovery_proposal,
    create_discovery_run,
    create_job,
    create_prompt_preset,
    get_connection,
    get_default_prompt_preset,
    get_discovery_proposal,
    get_discovery_run,
    get_prompt_preset,
    init_db,
    reject_discovery_proposal,
    update_prompt_preset,
)
from backend.app.discovery import DiscoveryManager
from backend.app.jobs import JobWorker, needs_llm_review
from backend.app.main import create_app
from backend.app.memory import memory_key
from backend.app.quality_gate import (
    evaluate_batch_quality_gate,
    is_quality_finding_resolved,
)


def _seed_book_with_stale_review(db_path: Path, book_id: str = "book_audit_1") -> str:
    """Seed a test book with a segment that has stale review_data='[]'."""
    now = "2026-10-25T12:00:00Z"
    ch_id = f"ch_{book_id}"
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, 'dummy.epub', ?, ?)",
            (book_id, "Audit Test Novel", now, now),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES (?, ?, 'ch1', 0, 'OEBPS/ch1.xhtml', 'ch1.xhtml', 'Chương 1', 'application/xhtml+xml', ?, 'untranslated', ?, ?)
            """,
            (ch_id, book_id, b"<html/>", now, now),
        )
        # Segment s1 was previously reviewed (review_data = '[]'), but now has empty translation (needs retranslation)
        conn.execute(
            """
            INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, review_data, is_whitespace, parent_tag)
            VALUES ('s1', ?, ?, 0, '/p[1]', 'text', '김철수는 말했다.', '김철수는 말했다.', '', '[]', 0, 'p')
            """,
            (ch_id, book_id),
        )
    return ch_id


# =============================================================================
# 1. Stale review_data clearing on retranslation
# =============================================================================

@pytest.mark.asyncio
async def test_retranslation_clears_stale_review_data_and_triggers_phase2_review(tmp_path: Path):
    db_file = tmp_path / "test_stale_review.db"
    init_db(db_file)
    ch_id = _seed_book_with_stale_review(db_file, "book_stale_rev")

    # Verify initial state: s1 has review_data = '[]'
    with get_connection(db_file) as conn:
        row = conn.execute("SELECT translation, review_data FROM segments WHERE id = 's1'").fetchone()
        assert row["review_data"] == "[]"

    mock_provider = MagicMock()
    mock_provider.all_secrets = None
    mock_provider.api_key = "dummy_key"
    mock_provider.is_model_supported.return_value = True

    # Translation returned cleanly without quality findings
    mock_provider.translate = AsyncMock(return_value={"s1": "Kim Chul Soo đã nói."})
    mock_provider.review = AsyncMock(return_value={"corrections": {"s1": "Kim Chul Soo cất lời nói."}, "issues": []})
    mock_provider.summarize = AsyncMock(return_value="Tóm tắt.")

    worker = JobWorker(db_file, lambda: mock_provider)

    job = create_job(
        db_file,
        book_id="book_stale_rev",
        chapter_ids=[ch_id],
        params={"review": True},
    )

    done_job = await worker.step()
    assert done_job["status"] == "completed"

    # Verify that the review phase was NOT skipped!
    # mock_provider.review should have been called for s1
    assert mock_provider.review.await_count == 1
    with get_connection(db_file) as conn:
        row = conn.execute("SELECT translation, review_data FROM segments WHERE id = 's1'").fetchone()
        # The translation should have reviewer's correction applied
        assert row["translation"] == "Kim Chul Soo cất lời nói."
        assert row["review_data"] == "[]"


# =============================================================================
# 2. Locked target word boundary precision
# =============================================================================

def test_locked_target_word_boundary_precision():
    # Canonical target is "An"
    context = {
        "characters": [
            {
                "canonical_source": "안",
                "canonical_target": "An",
                "source_aliases": [],
                "locked": True,
            }
        ]
    }
    batch = [{"id": "s1", "source": "안은 길을 걸었다."}]

    # Translation has "an" ONLY inside other words: "đang", "hoang"
    trans_missing_target = {
        "s1": "Hắn đang đi trên con đường hoang vắng."
    }
    gate_res1 = evaluate_batch_quality_gate(batch, trans_missing_target, context=context)
    # MUST flag locked_character finding because "An" is NOT in translation!
    findings1 = [f for f in gate_res1.findings if f.category == "locked_character"]
    assert len(findings1) == 1
    assert findings1[0].expected_target == "An"

    # Translation contains actual character "An"
    trans_with_target = {
        "s1": "An đang đi trên con đường vắng."
    }
    gate_res2 = evaluate_batch_quality_gate(batch, trans_with_target, context=context)
    findings2 = [f for f in gate_res2.findings if f.category == "locked_character"]
    assert len(findings2) == 0

    # Also check resolution in is_quality_finding_resolved
    finding_dict = findings1[0].to_dict()
    assert not is_quality_finding_resolved(finding_dict, "안은 길을 걸었다.", "Hắn đang hoang mang.")
    assert is_quality_finding_resolved(finding_dict, "안은 길을 걸었다.", "An cất bước đi.")

    # Punctuation / bracket boundary matching
    assert match_character_term("Gặp (An) ở trường", "(An)") is True
    assert match_character_term("Gặp An ở trường", "(An)") is False
    assert match_character_term("Thủ lĩnh [Vua Bóng Đêm]", "[Vua Bóng Đêm]") is True


# =============================================================================
# 3. Quality Gate divider line no false repetition
# =============================================================================

def test_quality_gate_divider_dashes_no_false_repetition():
    batch = [
        {"id": "s1", "source": "----------------"},
        {"id": "s2", "source": "* * * * * * * *"},
    ]
    translations = {
        "s1": "----------------",
        "s2": "* * * * * * * *",
    }
    gate_res = evaluate_batch_quality_gate(batch, translations)
    # Dividers should NOT be flagged as hallucination loops
    hallucinations = [f for f in gate_res.findings if f.category == "hallucination"]
    assert len(hallucinations) == 0
    assert gate_res.needs_retry is False


# =============================================================================
# 4. Preset fallback to default & cache key invalidation
# =============================================================================

@pytest.mark.asyncio
async def test_job_worker_preset_fallback_and_cache_invalidation(tmp_path: Path):
    db_file = tmp_path / "test_preset_fallback.db"
    init_db(db_file)
    now = "2026-10-25T12:00:00Z"
    with get_connection(db_file) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES ('b_fallback', 'Novel', 'd.epub', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('ch_fb', 'b_fallback', 'ch1', 0, 'OEBPS/ch1.xhtml', 'ch1.xhtml', 'Chương 1', 'application/xhtml+xml', ?, 'untranslated', ?, ?)
            """,
            (b"<html/>", now, now),
        )
        conn.execute(
            "INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, is_whitespace, parent_tag) VALUES ('s1', 'ch_fb', 'b_fallback', 0, '/p[1]', 'text', '안녕', '안녕', 0, 'p')"
        )

    # Set custom default preset
    p_def = create_prompt_preset(
        db_file,
        book_id="b_fallback",
        name="Custom Default Preset",
        system_prompt="Hệ thống dịch thuật chuyên sâu.",
        is_default=True,
    )

    mock_provider = MagicMock()
    mock_provider.all_secrets = None
    mock_provider.api_key = "dummy"
    mock_provider.is_model_supported.return_value = True
    captured_contexts = []

    async def mock_translate(batch, context=None, **kwargs):
        captured_contexts.append(context)
        return {"s1": "Xin chào."}

    mock_provider.translate = AsyncMock(side_effect=mock_translate)
    mock_provider.review = AsyncMock(return_value={"corrections": {}, "issues": []})
    mock_provider.summarize = AsyncMock(return_value="Tóm tắt.")

    worker = JobWorker(db_file, lambda: mock_provider)

    # 1. Launch job specifying preset_id
    job = create_job(
        db_file,
        book_id="b_fallback",
        chapter_ids=["ch_fb"],
        params={"preset_id": p_def["id"], "review": False},
    )

    await worker.step()

    # Verify that worker used the specified preset!
    assert len(captured_contexts) == 1
    ctx = captured_contexts[0]
    assert ctx["preset_id"] == p_def["id"]
    assert ctx["system_instruction"] == "Hệ thống dịch thuật chuyên sâu."

    # 2. Verify cache key invalidation on preset update
    key_before = memory_key("b_fallback", "ch_fb", "gemini-3.5-flash-lite", False, ctx)
    upd_preset = update_prompt_preset(db_file, p_def["id"], name="Renamed Preset")
    ctx_after = build_translation_context(
        [{"id": "s1", "source": "안녕"}],
        preset=upd_preset,
    )
    key_after = memory_key("b_fallback", "ch_fb", "gemini-3.5-flash-lite", False, ctx_after)
    assert key_before != key_after


# =============================================================================
# 5. Discovery concurrent run prevention & proposal double-review
# =============================================================================

@pytest.mark.asyncio
async def test_discovery_concurrent_run_prevention_and_double_review(tmp_path: Path):
    db_file = tmp_path / "test_disc_guard.db"
    init_db(db_file)
    now = "2026-10-25T12:00:00Z"
    with get_connection(db_file) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES ('b_disc', 'Novel', 'd.epub', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('ch_disc', 'b_disc', 'ch1', 0, 'OEBPS/ch1.xhtml', 'ch1.xhtml', 'Chương 1', 'application/xhtml+xml', ?, 'untranslated', ?, ?)
            """,
            (b"<html/>", now, now),
        )
        conn.execute(
            "INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, is_whitespace, parent_tag) VALUES ('s1', 'ch_disc', 'b_disc', 0, '/p[1]', 'text', '성진우는 헌터다.', '성진우는 헌터다.', 0, 'p')"
        )

    mock_provider = MagicMock()
    mock_provider.discover_candidates = AsyncMock(return_value=[
        {
            "canonical_source": "성진우",
            "canonical_target": "Sung Jin-woo",
            "category": "CHARACTER",
            "gender": "male",
            "source_aliases": [],
        }
    ])

    dm = DiscoveryManager(db_file, lambda: mock_provider)

    # 1. Starting discovery run
    run1 = await dm.start_discovery("b_disc")
    assert run1["status"] in ("queued", "running")

    # 2. Starting second discovery run on same book concurrently MUST raise RuntimeError
    with pytest.raises(RuntimeError, match="Đang có tiến trình gợi ý"):
        await dm.start_discovery("b_disc")

    # Cancel first run and await task clean exit
    await dm.cancel_discovery(run1["id"])

    # Now starting discovery is permitted again
    run2 = await dm.start_discovery("b_disc")
    assert run2["id"] != run1["id"]
    await dm.cancel_discovery(run2["id"])

    # 3. Double-review guard on proposals
    prop = create_discovery_proposal(
        db_file,
        book_id="b_disc",
        category="CHARACTER",
        canonical_source="차해인",
        canonical_target="Cha Hae-in",
    )
    assert prop["status"] == "pending"

    # Approve proposal
    appr = approve_discovery_proposal(db_file, prop["id"])
    assert appr["status"] == "approved"

    # Re-approving already approved proposal MUST raise ValueError
    with pytest.raises(ValueError, match="đã được xử lý"):
        approve_discovery_proposal(db_file, prop["id"])

    # Rejecting already approved proposal MUST raise ValueError
    with pytest.raises(ValueError, match="đã được xử lý"):
        reject_discovery_proposal(db_file, prop["id"])
