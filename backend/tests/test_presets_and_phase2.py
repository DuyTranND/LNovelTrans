"""Focused tests for Phase 2: Prompt Presets, Relevance Matching, Token Batching, and Quality Gate."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.app.db import (
    create_character,
    create_glossary_item,
    create_job,
    create_prompt_preset,
    delete_prompt_preset,
    get_connection,
    get_default_prompt_preset,
    get_job,
    get_prompt_preset,
    init_db,
    list_characters,
    list_prompt_presets,
    save_parsed_book,
    set_default_prompt_preset,
    update_character,
    update_prompt_preset,
)
from backend.app.context import (
    batch_segments,
    build_translation_context,
    estimate_segment_tokens,
    match_character_term,
    resolve_relevant_characters,
)
from backend.app.quality_gate import (
    QualityFinding,
    QualityGateResult,
    evaluate_batch_quality_gate,
)
from backend.app.memory import memory_key
from backend.app.jobs import JobWorker
from backend.app.provider import ProviderValidationError
from fastapi.testclient import TestClient
from backend.app.main import create_app


def _seed_test_book(db_path: Path, book_id: str = "book_p2") -> None:
    """Helper to seed a test book with chapters and segments."""
    now = "2026-10-25T12:00:00Z"
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (book_id, "Phase 2 Novel", "dummy.epub", now, now),
        )
        ch_id = f"ch_{book_id}"
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES (?, ?, 'ch1', 0, 'OEBPS/ch1.xhtml', 'ch1.xhtml', '제1장 시작', 'application/xhtml+xml', ?, 'untranslated', ?, ?)
            """,
            (ch_id, book_id, b"<html/>", now, now),
        )
        conn.execute(
            """
            INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, is_whitespace, parent_tag)
            VALUES
            ('s1', ?, ?, 0, '/h1', 'text', '제1장 시작', '제1장 시작', 0, 'h1'),
            ('s2', ?, ?, 1, '/p[1]', 'text', '김철수는 조용히 검을 들었다.', '김철수는 조용히 검을 들었다.', 0, 'p'),
            ('s3', ?, ?, 2, '/p[2]', 'text', '그는 진우를 바라보며 미소지었다.', '그는 진우를 바라보며 미소지었다.', 0, 'p')
            """,
            (ch_id, book_id, ch_id, book_id, ch_id, book_id),
        )


# =============================================================================
# 1. Prompt Preset CRUD & Default Logic
# =============================================================================

def test_prompt_preset_crud_and_default_promotion(tmp_path: Path):
    db_file = tmp_path / "test_presets.db"
    init_db(db_file)
    _seed_test_book(db_file, "book_preset_1")

    # 1. Listing presets when none exist auto-seeds a default preset
    presets = list_prompt_presets(db_file, "book_preset_1")
    assert len(presets) == 1
    def_preset = presets[0]
    assert def_preset["is_default"] is True
    assert "Tiêu chuẩn" in def_preset["name"]
    assert "{batch_targets}" in def_preset["user_prompt_template"]

    # 2. Create second preset
    p2 = create_prompt_preset(
        db_file,
        book_id="book_preset_1",
        name="Văn phong Kiếm hiệp",
        description="Dịch giọng điệu cổ trang, hào sảng",
        system_prompt="Bạn là dịch giả tiểu thuyết kiếm hiệp.",
        few_shot_examples=[{"source": "검을 들었다", "target": "rút kiếm xuất chiêu", "notes": "kiếm hiệp"}],
        is_default=False,
    )
    assert p2["is_default"] is False
    assert len(p2["few_shot_examples"]) == 1

    # Check list has 2 presets
    all_presets = list_prompt_presets(db_file, "book_preset_1")
    assert len(all_presets) == 2

    # 3. Set second preset as default
    set_default_prompt_preset(db_file, p2["id"])
    cur_p2 = get_prompt_preset(db_file, p2["id"])
    cur_def = get_prompt_preset(db_file, def_preset["id"])
    assert cur_p2["is_default"] is True
    assert cur_def["is_default"] is False

    # 4. Update preset
    upd = update_prompt_preset(db_file, p2["id"], name="Văn phong Kiếm hiệp Cổ trang")
    assert upd["name"] == "Văn phong Kiếm hiệp Cổ trang"

    # 5. Delete default preset promotes remaining preset to default
    delete_prompt_preset(db_file, p2["id"])
    remaining = list_prompt_presets(db_file, "book_preset_1")
    assert len(remaining) == 1
    assert remaining[0]["id"] == def_preset["id"]
    assert remaining[0]["is_default"] is True

    # 6. Deleting the last preset is rejected
    with pytest.raises(ValueError, match="Không thể xóa cấu hình prompt duy nhất"):
        delete_prompt_preset(db_file, def_preset["id"])


# =============================================================================
# 2. Token-Aware Batching & Oversized Segment Handling
# =============================================================================

def test_token_estimator_and_bounded_batching():
    # Token estimation: Hangul syllables weigh ~2.0, ASCII ~0.35 + 12 base
    korean_sample = "김철수는 조용히 검을 들었다."  # 16 chars (approx 12 Hangul chars)
    tokens = estimate_segment_tokens(korean_sample)
    assert tokens > 20
    assert tokens < 60

    empty_tokens = estimate_segment_tokens("")
    assert empty_tokens == 12

    # Create 10 segments of 30 Hangul chars each (~70 tokens each)
    segments = [
        {"id": f"seg_{i}", "source": "김철수는 조용히 검을 들고 앞으로 나아갔다. " * 2}
        for i in range(10)
    ]

    # Batch with small token limit (e.g. 150 tokens) should split into multiple batches
    batches = batch_segments(segments, max_segments=40, max_chars=6000, max_tokens=150)
    assert len(batches) > 1
    # Verify strict reading order is preserved
    unpacked_ids = [s["id"] for b in batches for s in b]
    assert unpacked_ids == [f"seg_{i}" for i in range(10)]

    # Oversized single segment exceeding max_tokens or max_chars
    oversized_seg = {"id": "seg_huge", "source": "대단히 긴 문장입니다. " * 300}  # ~6000 chars, ~6000+ tokens
    normal_seg1 = {"id": "seg_norm1", "source": "일반 문장입니다."}
    normal_seg2 = {"id": "seg_norm2", "source": "또 다른 문장입니다."}

    mixed_batches = batch_segments([normal_seg1, oversized_seg, normal_seg2], max_segments=40, max_chars=6000, max_tokens=2500)
    # The oversized segment should be isolated in its own single-item batch
    assert len(mixed_batches) == 3
    assert mixed_batches[0][0]["id"] == "seg_norm1"
    assert mixed_batches[1][0]["id"] == "seg_huge"
    assert mixed_batches[2][0]["id"] == "seg_norm2"

    # Guard against empty batch
    assert batch_segments([]) == []


# =============================================================================
# 3. Relevance-Based Character Profile Matching & Locked Priority
# =============================================================================

def test_character_relevance_matching_and_prompt_budget():
    chars = [
        {
            "id": "char_1",
            "canonical_source": "김철수",
            "canonical_target": "Kim Chul Soo",
            "source_aliases": ["철수", "김 사원"],
            "gender": "male",
            "speech_style": "lạnh lùng, quả quyết",
            "relationships": "Bạn của Jin Woo",
            "locked": True,
        },
        {
            "id": "char_2",
            "canonical_source": "강진우",
            "canonical_target": "Kang Jin Woo",
            "source_aliases": ["진우"],
            "gender": "male",
            "speech_style": "thân thiện, hoạt bát",
            "relationships": "Đồng đội của Chul Soo",
            "locked": False,
        },
        {
            "id": "char_3",
            "canonical_source": "이영희",
            "canonical_target": "Lee Young Hee",
            "source_aliases": ["영희"],
            "gender": "female",
            "speech_style": "dịu dàng",
            "relationships": "Không quen biết",
            "locked": False,
        },
    ]

    # Batch 1 contains "김철수" and "진우" but NOT "이영희"
    batch_text = "김철수는 칼을 뽑았다. 진우야, 조심해!"
    matched = resolve_relevant_characters(chars, batch_text, is_review=False)
    matched_names = [m["canonical_source"] for m in matched]

    assert "김철수" in matched_names
    assert "강진우" in matched_names
    assert "이영희" not in matched_names  # Excluded to avoid irrelevant context dumps!

    # Locked character must be sorted first
    assert matched[0]["canonical_source"] == "김철수"
    assert matched[0]["locked"] is True
    assert "(ĐÃ KHÓA / LOCKED)" in matched[0]["formatted_line"]
    assert "Giới tính: male" in matched[0]["formatted_line"]
    assert "Xưng hô/Giọng điệu: lạnh lùng, quả quyết" in matched[0]["formatted_line"]

    # Boundary check: "소염" should not falsely match "김소염" or compound words
    assert match_character_term("김소염이 웃었다", "소염") is False
    assert match_character_term("소염은 웃었다", "소염") is True
    assert match_character_term("소염제가 필요하다", "소염") is False


# =============================================================================
# 4. Context Builder with Presets & Few-Shot Rendering
# =============================================================================

def test_build_context_with_preset_and_placeholders():
    segments = [
        {"id": "seg_1", "source": "김철수는 검을 쥐었다."},
        {"id": "seg_2", "source": "진우가 물었다."},
    ]
    chars = [
        {
            "id": "c1",
            "canonical_source": "김철수",
            "canonical_target": "Kim Chul Soo",
            "source_aliases": [],
            "gender": "male",
            "speech_style": "nghiêm nghị",
            "relationships": "",
            "locked": True,
        }
    ]
    preset = {
        "id": "preset_wuxia",
        "name": "Kiếm hiệp",
        "system_prompt": "Quy tắc dịch kiếm hiệp: Giữ nguyên chiêu thức võ công.",
        "user_prompt_template": "### BỐI CẢNH:\n{characters}\n\n### DANH SÁCH:\n{batch_targets}\n\n{few_shots}",
        "few_shot_examples": [
            {"source": "검을 쥐었다", "target": "nắm chặt chuôi kiếm", "notes": "dứt khoát"}
        ],
    }

    ctx = build_translation_context(
        segments,
        characters=chars,
        preset=preset,
    )

    # 1. System instruction matches preset system prompt and contains NO source text
    assert ctx["system_instruction"] == "Quy tắc dịch kiếm hiệp: Giữ nguyên chiêu thức võ công."
    assert "김철수는" not in ctx["system_instruction"]

    # 2. User prompt contains substituted placeholders
    assert "Kim Chul Soo" in ctx["user_prompt"]
    assert "nắm chặt chuôi kiếm" in ctx["user_prompt"]
    assert "seg_1" in ctx["user_prompt"]
    assert "seg_2" in ctx["user_prompt"]

    # 3. Context hash sensitivity: different preset changes memory_key
    key1 = memory_key("b1", "ch1", "model1", False, ctx)
    ctx2 = dict(ctx)
    ctx2["few_shots"] = []
    key2 = memory_key("b1", "ch1", "model1", False, ctx2)
    assert key1 != key2  # Cache key sensitivity ensures stale memories are not reused!


# =============================================================================
# 5. Deterministic Quality Gate Evaluation
# =============================================================================

def test_quality_gate_evaluations():
    batch = [
        {"id": "s1", "source": "김철수는 말했다."},
        {"id": "s2", "source": "진우가 달렸다."},
        {"id": "s3", "source": "---"},  # Symbol-only segment
    ]

    # Perfect translations
    translations_good = {
        "s1": "Kim Chul Soo đã nói.",
        "s2": "Jin Woo đã chạy.",
        "s3": "---",
    }
    context = {
        "characters": [
            {"canonical_source": "김철수", "canonical_target": "Kim Chul Soo", "locked": True}
        ]
    }
    res_good = evaluate_batch_quality_gate(batch, translations_good, context=context)
    assert res_good.passed is True
    assert res_good.needs_retry is False
    assert len(res_good.findings) == 0

    # Case A: Missing segment ID and empty translation
    translations_bad_id = {
        "s2": "",  # Empty
        # s1 missing
    }
    res_bad_id = evaluate_batch_quality_gate(batch, translations_bad_id, context=context)
    assert res_bad_id.passed is False
    assert res_bad_id.needs_retry is True
    categories = [f.category for f in res_bad_id.findings]
    assert "segment_id" in categories
    assert "empty_output" in categories

    # Case B: Locked character substitution violation
    translations_bad_char = {
        "s1": "Một người đàn ông đã nói.",  # Missed Kim Chul Soo
        "s2": "Jin Woo đã chạy.",
        "s3": "---",
    }
    res_bad_char = evaluate_batch_quality_gate(batch, translations_bad_char, context=context)
    assert res_bad_char.passed is False
    char_findings = [f for f in res_bad_char.findings if f.category == "locked_character"]
    assert len(char_findings) == 1
    assert char_findings[0].source_term == "김철수"
    assert char_findings[0].expected_target == "Kim Chul Soo"

    # Case C: Untranslated Korean in output
    translations_untranslated = {
        "s1": "Kim Chul Soo 말했습니다.",  # Untranslated Hangul
        "s2": "Jin Woo đã chạy.",
        "s3": "---",
    }
    res_untrans = evaluate_batch_quality_gate(batch, translations_untranslated, context=context)
    assert res_untrans.needs_retry is True
    assert any(f.category == "untranslated_hangul" for f in res_untrans.findings)

    # Case D: Repetition / degenerate loop
    translations_loop = {
        "s1": "đã chạy đi đã chạy đi đã chạy đi đã chạy đi đã chạy đi rất nhanh.",
        "s2": "Jin Woo đã chạy.",
        "s3": "---",
    }
    res_loop = evaluate_batch_quality_gate(batch, translations_loop, context=context)
    assert any(f.category == "hallucination" for f in res_loop.findings)


# =============================================================================
# 6. End-to-End Job Worker Integration with Quality Gate & Presets
# =============================================================================

@pytest.mark.asyncio
async def test_job_worker_with_presets_and_quality_gate(tmp_path: Path):
    db_file = tmp_path / "test_jobs_phase2.db"
    init_db(db_file)
    _seed_test_book(db_file, "book_worker_p2")

    # Seed locked character
    create_character(
        db_file,
        book_id="book_worker_p2",
        canonical_source="김철수",
        canonical_target="Kim Chul Soo",
        locked=True,
    )

    # Create custom preset
    preset = create_prompt_preset(
        db_file,
        book_id="book_worker_p2",
        name="Preset Test",
        is_default=True,
    )

    # Mock provider returning translations
    mock_provider = MagicMock()
    mock_provider.all_secrets = None
    mock_provider.api_key = "dummy_key"
    mock_provider.is_model_supported.return_value = True

    # Translation returned on segment 1 has a warning: misses locked name
    with get_connection(db_file) as conn:
        chap_row = conn.execute("SELECT id FROM chapters WHERE book_id = 'book_worker_p2'").fetchone()
        ch_id = chap_row["id"]
        seg_rows = conn.execute("SELECT id FROM segments WHERE chapter_id = ?", (ch_id,)).fetchall()
        s_ids = [r["id"] for r in seg_rows]

    mock_provider.translate = AsyncMock(return_value={
        s_ids[0]: "Chương 1 Bắt đầu",
        s_ids[1]: "Anh ta giơ thanh kiếm lên một cách lặng lẽ.",  # Missing Kim Chul Soo
        s_ids[2]: "Anh ấy mỉm cười nhìn Jin Woo.",
    })
    mock_provider.review = AsyncMock(return_value={"corrections": {}, "issues": []})
    mock_provider.summarize = AsyncMock(return_value="Tóm tắt chương 1.")

    worker = JobWorker(db_file, lambda: mock_provider)

    # Create job specifying preset_id
    job = create_job(
        db_file,
        book_id="book_worker_p2",
        chapter_ids=[ch_id],
        params={"preset_id": preset["id"], "review": False},
    )

    # Run one step
    done_job = await worker.step()
    assert done_job is not None
    assert done_job["status"] == "completed"

    # Verify translations saved to DB without data loss
    with get_connection(db_file) as conn:
        saved_segs = conn.execute("SELECT id, translation, review_data FROM segments WHERE chapter_id = ?", (ch_id,)).fetchall()
        for s in saved_segs:
            assert s["translation"] is not None
            assert len(s["translation"].strip()) > 0
        # Segment 1 should have recorded the locked character warning in review_data
        seg_p = saved_segs[1]
        assert seg_p["review_data"] is not None
        r_data = json.loads(seg_p["review_data"])
        assert any(item.get("category") == "locked_character" for item in r_data)


# =============================================================================
# 7. FastAPI API Endpoints for Presets & Backwards Compatibility
# =============================================================================

def test_preset_api_endpoints_and_job_launch(tmp_path: Path):
    test_app = create_app(data_dir=tmp_path)
    db_file = test_app.state.db_path
    _seed_test_book(db_file, "book_api_p2")
    client = TestClient(test_app)

    # 1. GET presets for book
    res = client.get("/api/books/book_api_p2/presets")
    assert res.status_code == 200
    presets = res.json()
    assert len(presets) >= 1
    preset_id = presets[0]["id"]

    # 2. POST create preset
    res_create = client.post(
        "/api/books/book_api_p2/presets",
        json={
            "name": "API Custom Preset",
            "description": "Test via API",
            "few_shot_examples": [{"source": "검", "target": "kiếm", "notes": ""}],
        },
    )
    assert res_create.status_code == 201
    created = res_create.json()
    assert created["name"] == "API Custom Preset"
    new_preset_id = created["id"]

    # 3. PATCH update preset
    res_patch = client.patch(
        f"/api/presets/{new_preset_id}",
        json={"name": "API Custom Preset Updated"},
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["name"] == "API Custom Preset Updated"

    # 4. POST set default
    res_def = client.post(f"/api/presets/{new_preset_id}/default")
    assert res_def.status_code == 200
    assert res_def.json()["is_default"] is True

    # 5. POST create job with preset_id (explicit per-job selection)
    res_job = client.post(
        "/api/books/book_api_p2/jobs",
        json={"preset_id": new_preset_id, "review": False},
    )
    assert res_job.status_code == 201
    job_data = res_job.json()
    job_params = json.loads(job_data["params"]) if isinstance(job_data["params"], str) else job_data["params"]
    assert job_params.get("preset_id") == new_preset_id

    # 6. POST create job WITHOUT preset_id (backwards compatibility test)
    # Cancel previous job so new one can be created
    with get_connection(db_file) as conn:
        conn.execute("UPDATE jobs SET status = 'completed' WHERE id = ?", (job_data["id"],))

    res_legacy_job = client.post(
        "/api/books/book_api_p2/jobs",
        json={"review": False},
    )
    assert res_legacy_job.status_code == 201
    legacy_data = res_legacy_job.json()
    legacy_params = json.loads(legacy_data["params"]) if isinstance(legacy_data["params"], str) else legacy_data["params"]
    assert legacy_params.get("preset_id") is None  # Defaults smoothly to book default preset
