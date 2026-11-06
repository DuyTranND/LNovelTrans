"""Regression tests for deterministic cross-chapter title/h1 inconsistency early persistence.

Verifies that deterministic detection and evidence of cross-chapter TITLE/H1
inconsistencies is persisted BEFORE any Gemini/API calls or early failure/paused/cancelled
returns, preserving truthful run status (failed/partial, never falsely completed),
deduplicating by source_term_id, preserving findings across checkpoint resume,
and causing zero mutation to stored chapter translations.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.audit import AuditManager, ConsistencyAuditService
from backend.app.db import (
    get_audit_findings,
    get_audit_run,
    get_chapter,
    get_connection,
)
from backend.app.epub import EPUB_MIMETYPE
from backend.app.main import create_app
from backend.app.provider import (
    ProviderConnectError,
    _BaseBatchProvider,
)


def build_two_chapter_title_epub() -> bytes:
    """Build a minimal valid EPUB with two adjacent chapters having numbered Korean title stems."""
    chapters_data = [
        {
            "id": "ch1",
            "href": "chapter1.xhtml",
            "content": (
                "<html><head><title>상점 오픈 (1)</title></head>"
                "<body>"
                "<h1>상점 오픈 (1)</h1>"
                "<p>첫 번째 장 본문입니다.</p>"
                "</body></html>"
            ),
        },
        {
            "id": "ch2",
            "href": "chapter2.xhtml",
            "content": (
                "<html><head><title>상점 오픈 (2)</title></head>"
                "<body>"
                "<h1>상점 오픈 (2)</h1>"
                "<p>두 번째 장 본문입니다.</p>"
                "</body></html>"
            ),
        },
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zinfo_mime = zipfile.ZipInfo("mimetype")
        zinfo_mime.compress_type = zipfile.ZIP_STORED
        zinfo_mime.external_attr = 0o644 << 16
        zinfo_mime.extra = b""
        zf.writestr(zinfo_mime, EPUB_MIMETYPE)

        container_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>"""
        zf.writestr("META-INF/container.xml", container_xml)

        manifest_items = ['<item id="style" href="style.css" media-type="text/css"/>']
        spine_items = []
        for ch in chapters_data:
            cid = ch["id"]
            href = ch["href"]
            manifest_items.append(f'<item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="{cid}"/>')

        opf_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="pub-id">test-title-inconsistency-book</dc:identifier>
        <dc:title>Test Title Inconsistency Book</dc:title>
        <dc:language>ko</dc:language>
    </metadata>
    <manifest>
        {"".join(manifest_items)}
    </manifest>
    <spine>
        {"".join(spine_items)}
    </spine>
</package>"""
        zf.writestr("OEBPS/content.opf", opf_xml.encode("utf-8"))
        zf.writestr("OEBPS/style.css", b"body { font-family: serif; }")

        for ch in chapters_data:
            zf.writestr(f"OEBPS/{ch['href']}", ch["content"].encode("utf-8"))

    return buf.getvalue()


class MockFailingProvider(_BaseBatchProvider):
    """Mock provider with controllable failure and extraction behavior."""

    def __init__(self, should_fail: bool = True) -> None:
        self.should_fail = should_fail
        self.call_count = 0
        self.custom_entities_generator: Any = None

    @property
    def all_secrets(self) -> list[str]:
        return ["MOCK_SECRET_KEY"]

    async def _generate_json(
        self,
        system_instruction: str | None,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Any:
        return {"entities": []}

    async def extract_terminology(
        self,
        segments: list[Any],
        chapter_id: str | None = None,
        chapter_title: str | None = None,
        glossary: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        self.call_count += 1
        if self.should_fail:
            raise ProviderConnectError("Immediate network/provider failure simulated.")
        if callable(self.custom_entities_generator):
            return self.custom_entities_generator(segments, chapter_id, chapter_title)
        return []


@pytest.fixture
def title_inconsistency_setup(tmp_path: Path, monkeypatch):
    """Create book with 2 chapters having '상점 오픈 (1)' -> 'Mở cửa tiệm (1)' and '상점 오픈 (2)' -> 'Mở cửa hàng (2)'."""
    monkeypatch.setattr(AuditManager, "start_audit", lambda self, run_id: None)
    provider = MockFailingProvider(should_fail=True)
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app, client=("127.0.0.1", 50000))

    epub_bytes = build_two_chapter_title_epub()
    upload_res = client.post("/api/books", files={"file": ("book.epub", epub_bytes)})
    assert upload_res.status_code == 201
    book = upload_res.json()
    book_id = book["id"]
    chapters = client.get(f"/api/books/{book_id}/chapters").json()

    # Chapter 1: <title> and <h1> translated to "Mở cửa tiệm (1)"
    ch1_details = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    ch1_segs = ch1_details["segments"]
    title_seg_1 = next(s for s in ch1_segs if s["parent_tag"] == "title")
    h1_seg_1 = next(s for s in ch1_segs if s["parent_tag"] == "h1")
    p_seg_1 = next(s for s in ch1_segs if s["parent_tag"] == "p")
    client.patch(
        f"/api/chapters/{chapters[0]['id']}",
        json={
            "segments": [
                {"id": title_seg_1["id"], "translation": "Mở cửa tiệm (1)"},
                {"id": h1_seg_1["id"], "translation": "Mở cửa tiệm (1)"},
                {"id": p_seg_1["id"], "translation": "Nội dung chương một."},
            ]
        },
    )

    # Chapter 2: <title> and <h1> translated to "Mở cửa hàng (2)"
    ch2_details = client.get(f"/api/chapters/{chapters[1]['id']}").json()
    ch2_segs = ch2_details["segments"]
    title_seg_2 = next(s for s in ch2_segs if s["parent_tag"] == "title")
    h1_seg_2 = next(s for s in ch2_segs if s["parent_tag"] == "h1")
    p_seg_2 = next(s for s in ch2_segs if s["parent_tag"] == "p")
    client.patch(
        f"/api/chapters/{chapters[1]['id']}",
        json={
            "segments": [
                {"id": title_seg_2["id"], "translation": "Mở cửa hàng (2)"},
                {"id": h1_seg_2["id"], "translation": "Mở cửa hàng (2)"},
                {"id": p_seg_2["id"], "translation": "Nội dung chương hai."},
            ]
        },
    )

    return client, app, provider, book_id, chapters, (h1_seg_1["id"], h1_seg_2["id"])


# =============================================================================
# Test 1: Provider fails immediately; title findings persisted, truthful failure status
# =============================================================================
@pytest.mark.asyncio
async def test_deterministic_title_inconsistency_persisted_on_immediate_provider_failure(
    title_inconsistency_setup,
):
    """Verify deterministic title inconsistencies are persisted BEFORE provider calls,
    run status is truthfully 'failed' (never falsely 'completed'), exact source stem
    and both target variants with occurrences are saved, and chapter translations are unmutated."""
    client, app, provider, book_id, chapters, (h1_id_1, h1_id_2) = title_inconsistency_setup

    # Record baseline stored chapter translations
    ch1_before = client.get(f"/api/chapters/{chapters[0]['id']}").json()["segments"]
    ch2_before = client.get(f"/api/chapters/{chapters[1]['id']}").json()["segments"]
    trans_before_ch1 = [(s["id"], s["translation"]) for s in ch1_before]
    trans_before_ch2 = [(s["id"], s["translation"]) for s in ch2_before]

    # Provider will fail immediately
    provider.should_fail = True

    # Launch audit run
    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    # 1. Assert returned persisted run is failed with truthful progress
    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "failed"
    assert run_status["processed_segments"] == 0
    assert run_status["progress"]["stage"] == "failed"
    assert run_status["progress"]["percentage"] == 0.0

    # 2. Assert fetch findings returns persisted deterministic title evidence
    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    findings_data = findings_res.json()
    assert findings_data["total"] >= 1

    title_finding = next(
        (f for f in findings_data["items"] if f["category"] == "title" and f["source_term"] == "상점 오픈"),
        None,
    )
    assert title_finding is not None, f"Expected finding for '상점 오픈', got: {findings_data['items']}"
    assert title_finding["source_term_id"] == "title:상점 오픈"

    # Both differing target variants with occurrences
    variants_map = {v["target"]: v["count"] for v in title_finding["variants"]}
    assert "Mở cửa tiệm" in variants_map
    assert "Mở cửa hàng" in variants_map
    assert variants_map["Mở cửa tiệm"] == 1
    assert variants_map["Mở cửa hàng"] == 1

    occs = title_finding["occurrences"]
    assert len(occs) == 2

    occ1 = next(o for o in occs if o["chapter_id"] == chapters[0]["id"])
    assert occ1["source_snippet"] == "상점 오픈 (1)"
    assert occ1["target_snippet"] == "Mở cửa tiệm (1)"
    assert occ1["target_variant"] == "Mở cửa tiệm"
    assert occ1["segment_id"] == h1_id_1

    occ2 = next(o for o in occs if o["chapter_id"] == chapters[1]["id"])
    assert occ2["source_snippet"] == "상점 오픈 (2)"
    assert occ2["target_snippet"] == "Mở cửa hàng (2)"
    assert occ2["target_variant"] == "Mở cửa hàng"
    assert occ2["segment_id"] == h1_id_2

    # 3. Assert zero mutation of stored chapter translations
    ch1_after = client.get(f"/api/chapters/{chapters[0]['id']}").json()["segments"]
    ch2_after = client.get(f"/api/chapters/{chapters[1]['id']}").json()["segments"]
    trans_after_ch1 = [(s["id"], s["translation"]) for s in ch1_after]
    trans_after_ch2 = [(s["id"], s["translation"]) for s in ch2_after]
    assert trans_after_ch1 == trans_before_ch1
    assert trans_after_ch2 == trans_before_ch2


# =============================================================================
# Test 2: Provider is None; deterministic title findings persisted, run failed
# =============================================================================
@pytest.mark.asyncio
async def test_deterministic_title_inconsistency_persisted_when_provider_is_none(
    title_inconsistency_setup,
):
    """Verify deterministic title inconsistencies are persisted even when AI provider is None."""
    client, app, provider, book_id, chapters, _ = title_inconsistency_setup

    # Simulate provider unavailable (None)
    app.state.provider = None

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = ConsistencyAuditService(app.state.db_path, lambda: None)
    await audit_svc.execute_run(run_id)

    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "failed"
    assert "provider unavailable" in run_status["error"].lower() or "chưa cấu hình" in run_status["error"].lower()

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]

    title_finding = next((f for f in items if f["source_term"] == "상점 오픈"), None)
    assert title_finding is not None
    variants = {v["target"]: v["count"] for v in title_finding["variants"]}
    assert "Mở cửa tiệm" in variants
    assert "Mở cửa hàng" in variants


# =============================================================================
# Test 3: Retry/successful completion merges deterministic with AI findings, no duplicates
# =============================================================================
@pytest.mark.asyncio
async def test_successful_ai_completion_merges_deterministic_and_ai_findings(
    title_inconsistency_setup,
):
    """Verify that retrying/completing successfully merges deterministic title findings
    with AI findings, deduplicating by source_term_id with deterministic priority."""
    client, app, provider, book_id, chapters, _ = title_inconsistency_setup

    # First, run with failure to persist title findings
    provider.should_fail = True
    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    failed_run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert failed_run["status"] == "failed"

    # Now recover provider: provider succeeds and extracts an entity from paragraph
    provider.should_fail = False

    def custom_gen(segments, chapter_id, chapter_title):
        # AI extracts an entity from body paragraphs
        entities = []
        for s in segments:
            if "본문" in s.get("source", ""):
                entities.append({
                    "segment_id": s["id"],
                    "chapter_id": chapter_id or "",
                    "chapter_title": chapter_title or "",
                    "source_term": "본문",
                    "target_term": "chính văn",
                    "category": "term",
                    "confidence": "high",
                    "source_snippet": s["source"],
                    "target_snippet": s["translation"],
                })
        return entities

    provider.custom_entities_generator = custom_gen

    # Retry the run
    retry_res = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/retry")
    assert retry_res.status_code == 202

    await audit_svc.execute_run(run_id)

    final_run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert final_run["status"] == "completed"
    assert final_run["progress"]["percentage"] == 100.0

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]

    # Verify deterministic title finding is still present
    title_finding = next((f for f in items if f["source_term_id"] == "title:상점 오픈"), None)
    assert title_finding is not None
    variants = {v["target"]: v["count"] for v in title_finding["variants"]}
    assert "Mở cửa tiệm" in variants
    assert "Mở cửa hàng" in variants

    # Verify no duplicate source_term_ids
    source_term_ids = [f["source_term_id"] for f in items]
    assert len(source_term_ids) == len(set(source_term_ids))


# =============================================================================
# Test 4: Pausing does not clobber deterministic evidence
# =============================================================================
@pytest.mark.asyncio
async def test_pausing_does_not_clobber_deterministic_evidence(
    title_inconsistency_setup,
):
    """Verify that a paused audit run preserves deterministic title evidence."""
    client, app, provider, book_id, chapters, _ = title_inconsistency_setup

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    # Set run status to pausing so it pauses at the loop
    with get_connection(app.state.db_path) as conn:
        conn.execute("UPDATE consistency_audit_runs SET status = 'pausing' WHERE id = ?", (run_id,))

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "paused"

    # Deterministic title finding must already be persisted in database
    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]
    assert any(f["source_term_id"] == "title:상점 오픈" for f in items)


# =============================================================================
# Test 5: Provider is None; BOTH deterministic glossary and title findings persisted
# =============================================================================
@pytest.mark.asyncio
async def test_deterministic_glossary_and_title_persisted_before_provider_none(
    title_inconsistency_setup,
):
    """Verify that both deterministic title findings and known-glossary divergences
    are persisted into database before provider None failure, with truthful failed status."""
    client, app, provider, book_id, chapters, _ = title_inconsistency_setup

    # Add a locked glossary term: "본문" -> "Chính Văn" (Locked)
    client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "본문", "target": "Chính Văn", "locked": True, "category": "term"},
    )
    # Note: Chapter 1 paragraph is translated as "Nội dung chương một." (missing "Chính Văn" -> divergence!)

    # Provider is unavailable (None)
    app.state.provider = None

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = ConsistencyAuditService(app.state.db_path, lambda: None)
    await audit_svc.execute_run(run_id)

    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "failed"
    assert "provider unavailable" in run_status["error"].lower() or "chưa cấu hình" in run_status["error"].lower()

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]

    # 1. Deterministic title finding persisted
    title_finding = next((f for f in items if f["source_term"] == "상점 오픈"), None)
    assert title_finding is not None
    assert title_finding["category"] == "title"

    # 2. Deterministic glossary divergence persisted
    glossary_finding = next((f for f in items if f["source_term"] == "본문"), None)
    assert glossary_finding is not None
    assert glossary_finding["category"] == "term"
    assert glossary_finding["canonical_recommendation"] == "Chính Văn"
    assert glossary_finding["glossary_locked"] is True
    assert glossary_finding["glossary_divergence"] is True


# =============================================================================
# Test 6: Pausing preserves BOTH deterministic title and glossary findings
# =============================================================================
@pytest.mark.asyncio
async def test_pausing_preserves_both_title_and_glossary_findings(
    title_inconsistency_setup,
):
    """Verify that a paused audit run preserves both title and glossary deterministic findings."""
    client, app, provider, book_id, chapters, _ = title_inconsistency_setup

    # Add locked glossary term with divergence in translations
    client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "본문", "target": "Chính Văn", "locked": True, "category": "term"},
    )

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    # Set run status to pausing before execution
    with get_connection(app.state.db_path) as conn:
        conn.execute("UPDATE consistency_audit_runs SET status = 'pausing' WHERE id = ?", (run_id,))

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "paused"

    # Both findings must be persisted in database without clobbering
    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]
    source_ids = {f["source_term_id"] for f in items}
    assert "title:상점 오픈" in source_ids
    assert "term:본문" in source_ids


# =============================================================================
# Test 7: Unresolved marker cannot report all-clear and produces no fabricated translation
# =============================================================================
@pytest.mark.asyncio
async def test_unresolved_marker_not_reported_all_clear_and_no_fabricated_translation(
    title_inconsistency_setup,
):
    """Verify that unresolved review markers in occurrences/variants are correctly flagged,
    cannot be reported all-clear in coverage note, and produce no fabricated Vietnamese translation."""
    client, app, provider, book_id, chapters, _ = title_inconsistency_setup

    provider.should_fail = False

    # AI returns an entity with manual review marker "[Cần xác minh bản dịch]" and no other variants
    def custom_gen(segments, chapter_id, chapter_title):
        entities = []
        for s in segments:
            if "본문" in s.get("source", ""):
                entities.append({
                    "segment_id": s["id"],
                    "chapter_id": chapter_id or "",
                    "chapter_title": chapter_title or "",
                    "source_term": "본문",
                    "target_term": "[Cần xác minh bản dịch]",
                    "canonical_source": "본문",
                    "category": "term",
                    "confidence": "low",
                    "source_snippet": s["source"],
                    "target_snippet": s.get("translation", ""),
                    "manual_review_required": True,
                })
        return entities

    provider.custom_entities_generator = custom_gen

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "completed"

    # 1. Coverage note must NOT report all-clear; must include manual review disclaimer
    coverage_note = run_status["progress"]["coverage_note"]
    assert "[Cần xác minh bản dịch]" in coverage_note or "xác minh" in coverage_note.lower()
    assert "Rà soát toàn bộ sách hoàn tất thành công qua trích xuất AI kết hợp quét đối sánh nhất quán đa chương." != coverage_note

    # 2. Finding must be flagged and not dropped
    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]
    bonmun_finding = next((f for f in items if f["source_term"] == "본문"), None)
    assert bonmun_finding is not None

    # 3. Canonical recommendation must remain the unresolved marker, NOT a fabricated Vietnamese translation
    assert bonmun_finding["canonical_recommendation"] == "[Cần xác minh bản dịch]"
    assert bonmun_finding["confidence"] == "low"
    assert "xác minh" in bonmun_finding["reason"].lower()
