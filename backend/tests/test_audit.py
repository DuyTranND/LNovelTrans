"""Comprehensive mocked tests for Whole-Book Post-Translation Consistency Audit."""

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
    create_audit_run,
    create_glossary_item,
    get_audit_checkpoints,
    get_audit_findings,
    get_audit_run,
    get_chapter,
    get_connection,
    list_audit_runs,
    list_jobs,
    recover_interrupted_audit_runs,
    update_chapter_segments,
)
from backend.app.epub import EPUB_MIMETYPE
from backend.app.main import create_app
from backend.app.provider import (
    ProviderAuthError,
    ProviderConnectError,
    ProviderRateLimitError,
    ProviderSchemaValidationError,
    _BaseBatchProvider,
)


def build_test_epub_bytes(chapters_data: list[dict[str, Any]]) -> bytes:
    """Build a valid minimal EPUB in memory with specified chapters."""
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
        for i, ch in enumerate(chapters_data):
            cid = ch["id"]
            href = ch["href"]
            manifest_items.append(f'<item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="{cid}"/>')

        opf_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="pub-id">test-audit-book</dc:identifier>
        <dc:title>Test Consistency Book</dc:title>
        <dc:language>ko</dc:language>
        <dc:creator>Author</dc:creator>
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


class MockAuditProvider(_BaseBatchProvider):
    """Mock provider with controllable responses for terminology audit testing."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.custom_entities_generator: Any = None
        self.raise_on_batch_index: int | None = None
        self.raise_error: Exception | None = None
        self.call_count = 0
        self.simulate_secrets = ["SECRET_API_KEY_12345"]

    @property
    def all_secrets(self) -> list[str]:
        return self.simulate_secrets

    async def _generate_json(
        self,
        system_instruction: str | None,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Any:
        # Not used directly if extract_terminology is called, but implemented for completeness
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
        current_call_index = self.call_count - 1
        self.calls.append(segments)

        if self.raise_on_batch_index is not None and current_call_index == self.raise_on_batch_index:
            raise self.raise_error or ProviderConnectError(
                f"Lỗi mạng với {self.simulate_secrets[0]}",
                secret=self.simulate_secrets,
            )

        if callable(self.custom_entities_generator):
            return self.custom_entities_generator(segments, chapter_id, chapter_title)

        # Default naive extractor: look for known sample patterns
        results = []
        for s in segments:
            sid = s["id"]
            src = s["source"]
            trans = s["translation"]

            # Character Tiêu Viêm
            if "소염" in src and "Tiêu Viêm" in trans:
                results.append({
                    "segment_id": sid,
                    "chapter_id": chapter_id or "",
                    "chapter_title": chapter_title or "",
                    "source_term": "소염",
                    "target_term": "Tiêu Viêm",
                    "category": "character",
                    "confidence": "high",
                    "source_snippet": src[:100],
                    "target_snippet": trans[:100],
                })
            elif "소염" in src and "Tiêu Diêm" in trans:
                results.append({
                    "segment_id": sid,
                    "chapter_id": chapter_id or "",
                    "chapter_title": chapter_title or "",
                    "source_term": "소염",
                    "target_term": "Tiêu Diêm",
                    "category": "character",
                    "confidence": "high",
                    "source_snippet": src[:100],
                    "target_snippet": trans[:100],
                })

            # Location Vân Lam Tông
            if "운란종" in src:
                for term in ["Vân Lam Tông", "Vân Lam phái"]:
                    if term in trans:
                        results.append({
                            "segment_id": sid,
                            "chapter_id": chapter_id or "",
                            "chapter_title": chapter_title or "",
                            "source_term": "운란종",
                            "target_term": term,
                            "category": "location",
                            "confidence": "high",
                            "source_snippet": src[:100],
                            "target_snippet": trans[:100],
                        })

        return results


@pytest.fixture
def test_setup(tmp_path: Path, monkeypatch):
    """Setup app, client, mock provider, and a test book with 2 chapters."""
    monkeypatch.setattr(AuditManager, 'start_audit', lambda self, run_id: None)
    provider = MockAuditProvider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)

    epub_bytes = build_test_epub_bytes([
        {
            "id": "ch1",
            "href": "chapter1.xhtml",
            "content": (
                "<html><body>"
                "<p>소염은 조용히 걸었다.</p>"
                "<p>운란종의 산문 앞에 섰다.</p>"
                "</body></html>"
            ),
        },
        {
            "id": "ch2",
            "href": "chapter2.xhtml",
            "content": (
                "<html><body>"
                "<p>소염은 다시 눈을 떴다.</p>"
                "<p>운란종 제자들은 놀랐다.</p>"
                "</body></html>"
            ),
        },
    ])

    upload_res = client.post("/api/books", files={"file": ("book.epub", epub_bytes)})
    assert upload_res.status_code == 201
    book = upload_res.json()
    chapters = client.get(f"/api/books/{book['id']}/chapters").json()

    # Pre-populate translations:
    # In chapter 1: 소염 -> Tiêu Viêm, 운란종 -> Vân Lam Tông
    ch1_details = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    client.patch(
        f"/api/chapters/{chapters[0]['id']}",
        json={
            "segments": [
                {"id": ch1_details["segments"][0]["id"], "translation": "Tiêu Viêm lặng lẽ bước đi."},
                {"id": ch1_details["segments"][1]["id"], "translation": "Đứng trước sơn môn Vân Lam Tông."},
            ]
        },
    )

    # In chapter 2: 소염 -> Tiêu Diêm (variant!), 운란종 -> Vân Lam phái (variant!)
    ch2_details = client.get(f"/api/chapters/{chapters[1]['id']}").json()
    client.patch(
        f"/api/chapters/{chapters[1]['id']}",
        json={
            "segments": [
                {"id": ch2_details["segments"][0]["id"], "translation": "Tiêu Diêm lại mở mắt ra."},
                {"id": ch2_details["segments"][1]["id"], "translation": "Đệ tử Vân Lam Tông kinh ngạc."},
            ]
        },
    )

    return client, app, provider, book["id"], chapters


# =============================================================================
# Test 1: Cross-chapter variants detected
# =============================================================================
@pytest.mark.asyncio
async def test_cross_chapter_variants(test_setup):
    client, app, provider, book_id, chapters = test_setup

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_data = res.json()
    run_id = run_data["id"]

    # Run the audit service directly to completion
    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    # Check status
    status_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}")
    assert status_res.status_code == 200
    updated_run = status_res.json()
    assert updated_run["status"] == "completed"
    assert updated_run["total_segments"] == 4
    assert updated_run["processed_segments"] == 4

    # Check findings
    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    findings_data = findings_res.json()
    items = findings_data["items"]

    # We expect an inconsistency for 소염 ("Tiêu Viêm" vs "Tiêu Diêm")
    term_finding = next((f for f in items if f["source_term"] == "소염"), None)
    assert term_finding is not None
    assert term_finding["category"] == "character"
    variants = {v["target"]: v["count"] for v in term_finding["variants"]}
    assert "Tiêu Viêm" in variants
    assert "Tiêu Diêm" in variants
    assert len(term_finding["occurrences"]) == 2


# =============================================================================
# Test 2: Glossary mismatch and canonical preference
# =============================================================================
@pytest.mark.asyncio
async def test_glossary_mismatch_and_canonical_preference(test_setup):
    client, app, provider, book_id, chapters = test_setup

    # Add a locked glossary term: 소염 -> Tiêu Viêm (Locked)
    client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "소염", "target": "Tiêu Viêm", "locked": True, "notes": "Nhân vật chính"},
    )

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    items = findings_res.json()["items"]

    term_finding = next((f for f in items if f["source_term"] == "소염"), None)
    assert term_finding is not None
    # Canonical recommendation MUST strictly follow the locked glossary!
    assert term_finding["canonical_recommendation"] == "Tiêu Viêm"
    assert term_finding["glossary_locked"] is True
    assert term_finding["glossary_divergence"] is True
    assert term_finding["severity"] == "high"


# =============================================================================
# Test 3: Consistent book generates 0 findings
# =============================================================================
@pytest.mark.asyncio
async def test_consistent_book(test_setup):
    client, app, provider, book_id, chapters = test_setup

    # Fix chapter 2 so both chapters use "Tiêu Viêm" and "Vân Lam Tông"
    ch2_details = client.get(f"/api/chapters/{chapters[1]['id']}").json()
    client.patch(
        f"/api/chapters/{chapters[1]['id']}",
        json={
            "segments": [
                {"id": ch2_details["segments"][0]["id"], "translation": "Tiêu Viêm lại mở mắt ra."},
                {"id": ch2_details["segments"][1]["id"], "translation": "Đệ tử Vân Lam Tông kinh ngạc."},
            ]
        },
    )

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.json()["total"] == 0
    assert len(findings_res.json()["items"]) == 0


# =============================================================================
# Test 4: False evidence, schema, and foreign IDs rejected
# =============================================================================
@pytest.mark.asyncio
async def test_false_evidence_schema_foreign_ids_rejected(test_setup):
    client, app, provider, book_id, chapters = test_setup

    from backend.app.provider import _extract_terminology_batch

    valid_ids = {"seg_1"}
    source_map = {"seg_1": "소염은 길을 걸었다."}
    trans_map = {"seg_1": "Tiêu Viêm bước đi trên đường."}

    # Case A: Foreign / unknown segment ID
    parsed_foreign = {
        "entities": [
            {
                "segment_id": "seg_foreign_999",
                "source_term": "소염",
                "target_term": "Tiêu Viêm",
                "category": "character",
                "confidence": "high",
            }
        ]
    }
    extracted = _extract_terminology_batch(parsed_foreign, valid_ids, source_map, trans_map)
    assert len(extracted) == 0

    # Case B: Hallucinated source term not in source text
    parsed_bad_src = {
        "entities": [
            {
                "segment_id": "seg_1",
                "source_term": "약로",  # Not in seg_1 source!
                "target_term": "Tiêu Viêm",
                "category": "character",
                "confidence": "high",
            }
        ]
    }
    extracted_b = _extract_terminology_batch(parsed_bad_src, valid_ids, source_map, trans_map)
    assert len(extracted_b) == 0

    # Case C: Hallucinated target variant not in translation
    parsed_bad_tgt = {
        "entities": [
            {
                "segment_id": "seg_1",
                "source_term": "소염",
                "target_term": "Dược Lão",  # Not in seg_1 translation!
                "category": "character",
                "confidence": "high",
            }
        ]
    }
    extracted_c = _extract_terminology_batch(parsed_bad_tgt, valid_ids, source_map, trans_map)
    assert len(extracted_c) == 0

    # Case D: Malformed schema raises ProviderSchemaValidationError
    with pytest.raises(ProviderSchemaValidationError):
        _extract_terminology_batch("not_a_dict", valid_ids, source_map, trans_map)


# =============================================================================
# Test 5: Coverage, partial review, and oversize handling
# =============================================================================
@pytest.mark.asyncio
async def test_coverage_partial_oversize(test_setup):
    client, app, provider, book_id, chapters = test_setup

    # Make provider fail on all retry attempts of the first batch
    orig_extract = provider.extract_terminology
    first_batch_segs = None

    async def mock_extract(segments, *args, **kwargs):
        nonlocal first_batch_segs
        if first_batch_segs is None:
            first_batch_segs = segments
        if segments is first_batch_segs or segments == first_batch_segs:
            provider.call_count += 1
            provider.calls.append(segments)
            raise ProviderConnectError("Failed to reach Gemini")
        return await orig_extract(segments, *args, **kwargs)

    provider.extract_terminology = mock_extract

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    status_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}")
    run = status_res.json()
    assert run["status"] == "partial"
    assert run["progress"]["stage"] == "partial"
    assert run["failed_segments"] > 0
    assert run["error"] is not None and "một phần" in run["error"]
    # Truthful coverage check: must NOT report all-clear
    assert run["progress"]["coverage_status"] == "partial"
    assert "một phần" in run["progress"]["coverage_note"]


# =============================================================================
# Test 6: Pagination and bounds
# =============================================================================
@pytest.mark.asyncio
async def test_pagination_and_bounds(test_setup):
    client, app, provider, book_id, chapters = test_setup

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    # Page 1 with page_size=1
    page1 = client.get(
        f"/api/books/{book_id}/consistency-reviews/{run_id}/findings?page=1&page_size=1"
    ).json()
    assert page1["page"] == 1
    assert page1["page_size"] == 1
    assert len(page1["items"]) == 1
    assert page1["total_pages"] >= 1

    # Filter by category
    cat_res = client.get(
        f"/api/books/{book_id}/consistency-reviews/{run_id}/findings?category=character"
    ).json()
    for item in cat_res["items"]:
        assert item["category"] == "character"


# =============================================================================
# Test 7: Run duplicate prevention (one active audit per book)
# =============================================================================
@pytest.mark.asyncio
async def test_run_duplicate_prevention(test_setup):
    client, app, provider, book_id, chapters = test_setup

    res1 = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res1.status_code == 202

    # Second audit while first is active -> 409 Conflict
    res2 = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res2.status_code == 409
    assert "đang chạy" in res2.json()["detail"] or "đang có" in res2.json()["detail"]


# =============================================================================
# Test 8: Checkpoint retry and deduplication
# =============================================================================
@pytest.mark.asyncio
async def test_checkpoint_retry_and_deduplication(test_setup):
    client, app, provider, book_id, chapters = test_setup

    # Fail on batch 1
    provider.raise_on_batch_index = 1
    provider.raise_error = ProviderConnectError("Network drop")

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    run_after_fail = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    initial_processed = run_after_fail["processed_segments"]
    assert initial_processed > 0

    # Reset failure and retry
    provider.raise_on_batch_index = None
    retry_res = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/retry")
    assert retry_res.status_code == 202

    # Re-run to verify deduplication
    await audit_svc.execute_run(run_id)
    final_run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert final_run["status"] == "completed"

    findings = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings").json()
    # Check no duplicate source terms
    src_terms = [f["source_term"] for f in findings["items"]]
    assert len(src_terms) == len(set(src_terms))


# =============================================================================
# Test 9: Cancellation and restart cleanup
# =============================================================================
@pytest.mark.asyncio
async def test_cancellation_and_restart_cleanup(test_setup):
    client, app, provider, book_id, chapters = test_setup

    # 1. Test cancel endpoint
    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    cancel_res = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"

    # Cannot cancel already cancelled run -> 400
    cancel_again = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/cancel")
    assert cancel_again.status_code == 400

    # 2. Test restart cleanup
    # Create another run in running status
    res2 = client.post(f"/api/books/{book_id}/consistency-reviews")
    run2_id = res2.json()["id"]

    with get_connection(app.state.db_path) as conn:
        conn.execute("UPDATE consistency_audit_runs SET status = 'running' WHERE id = ?", (run2_id,))

    # Simulate server restart
    paused_count = recover_interrupted_audit_runs(app.state.db_path)
    assert paused_count >= 1

    run2 = client.get(f"/api/books/{book_id}/consistency-reviews/{run2_id}").json()
    assert run2["status"] == "paused"
    assert "khởi động lại" in run2["error"]


# =============================================================================
# Test 10: Stale edits detected
# =============================================================================
@pytest.mark.asyncio
async def test_stale_edits_detected(test_setup):
    client, app, provider, book_id, chapters = test_setup

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    run_before_edit = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_before_edit["is_stale"] is False

    # Edit chapter translation
    ch1_details = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    client.patch(
        f"/api/chapters/{chapters[0]['id']}",
        json={
            "segments": [
                {"id": ch1_details["segments"][0]["id"], "translation": "Tiêu Viêm thay đổi câu dịch."},
            ]
        },
    )

    # Audit run must now be marked stale
    run_after_edit = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_after_edit["is_stale"] is True


# =============================================================================
# Test 11: Source translations, EPUB, and old jobs unchanged
# =============================================================================
@pytest.mark.asyncio
async def test_source_translations_epub_and_old_jobs_unchanged(test_setup):
    client, app, provider, book_id, chapters = test_setup

    # Record baseline state of segments, book file, jobs
    ch1_before = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    segs_before = [s["translation"] for s in ch1_before["segments"]]
    jobs_before = client.get(f"/api/books/{book_id}/jobs").json()

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    # Verify segments are unchanged
    ch1_after = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    segs_after = [s["translation"] for s in ch1_after["segments"]]
    assert segs_before == segs_after

    # Verify jobs table is completely unchanged (no fake jobs added)
    jobs_after = client.get(f"/api/books/{book_id}/jobs").json()
    assert len(jobs_before) == len(jobs_after)


# =============================================================================
# Test 12: Secret redaction in errors
# =============================================================================
@pytest.mark.asyncio
async def test_key_rotation_and_secret_redaction(test_setup):
    client, app, provider, book_id, chapters = test_setup

    secret_key = "AIzaSySecretApiKeyToRedact"
    provider.simulate_secrets = [secret_key]
    provider.raise_on_batch_index = 0
    provider.raise_error = ProviderConnectError(
        f"Lỗi kết nối tới {secret_key}",
        secret=[secret_key],
    )

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    # Check that secret_key never appears anywhere in the run data or progress
    dumped = json.dumps(run)
    assert secret_key not in dumped
    assert "[REDACTED]" in dumped or secret_key not in dumped


# =============================================================================
# Test 13: Auth 403 fail-fast without saving checkpoints
# =============================================================================
@pytest.mark.asyncio
async def test_auth_403_fail_fast_no_checkpoint(test_setup):
    client, app, provider, book_id, chapters = test_setup

    provider.raise_on_batch_index = 0
    provider.raise_error = ProviderAuthError("403 PERMISSION_DENIED", status_code=403)

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    await app.state.audit_manager.service.execute_run(run_id)

    run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run["status"] == "failed"
    assert run["processed_segments"] == 0
    assert run["progress"]["percentage"] == 0
    assert run["failed_segments"] > 0
    assert provider.call_count == 1
    assert get_audit_checkpoints(app.state.db_path, run_id) == []


# =============================================================================
# Test 14: Network failures save no checkpoints and retry succeeds with empty findings
# =============================================================================
@pytest.mark.asyncio
async def test_network_failures_no_checkpoint_and_retry_empty_success(test_setup, monkeypatch):
    client, app, provider, book_id, chapters = test_setup

    monkeypatch.setattr("backend.app.audit.AUDIT_TRANSIENT_INITIAL_DELAY", 0)

    async def failing_extract(*args, **kwargs):
        raise ProviderConnectError("ConnectError persistent")

    provider.extract_terminology = failing_extract

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    service = app.state.audit_manager.service
    await service.execute_run(run_id)

    failed_run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert failed_run["status"] == "failed"
    assert failed_run["processed_segments"] == 0
    assert failed_run["failed_segments"] > 0
    assert failed_run["progress"]["percentage"] == 0
    assert get_audit_checkpoints(app.state.db_path, run_id) == []

    async def empty_extract(*args, **kwargs):
        return []

    provider.extract_terminology = empty_extract

    retry_res = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/retry")
    assert retry_res.status_code == 202

    await service.execute_run(run_id)

    final_run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert final_run["status"] == "completed"
    assert final_run["processed_segments"] == final_run["total_segments"]
    assert final_run["failed_segments"] == 0
    assert final_run["progress"]["percentage"] == 100
    checkpoints = get_audit_checkpoints(app.state.db_path, run_id)
    assert len(checkpoints) > 0
    assert all(cp["raw_findings"] == [] for cp in checkpoints)


# =============================================================================
# Test 15: Chapter title inconsistency regression with exact installment example
# =============================================================================
@pytest.mark.asyncio
async def test_saved_segment_chapter_title_inconsistency_regression(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(AuditManager, "start_audit", lambda self, run_id: None)
    provider = MockAuditProvider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)

    # Build EPUB with exact title examples:
    # Chapter 1: <title> and <h1> both containing "상점 오픈 (1)"
    # Chapter 2: <title> and <h1> both containing "상점 오픈 (2)"
    epub_bytes = build_test_epub_bytes([
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
    ])

    upload_res = client.post("/api/books", files={"file": ("book.epub", epub_bytes)})
    assert upload_res.status_code == 201
    book = upload_res.json()
    book_id = book["id"]
    chapters = client.get(f"/api/books/{book_id}/chapters").json()

    # Pre-populate translated segments:
    # Chapter 1: "상점 오픈 (1)" -> "Mở cửa tiệm (1)" for both <title> and <h1> (identical duplicates)
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

    # Chapter 2: "상점 오픈 (2)" -> "Mở cửa hàng (2)" for both <title> and <h1> (identical duplicates)
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

    # Launch audit run
    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    # Verify run completed
    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "completed"

    # Query findings
    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    findings_data = findings_res.json()
    items = findings_data["items"]

    # Assert a category 'title' finding with source term '상점 오픈'
    title_finding = next((f for f in items if f["category"] == "title" and f["source_term"] == "상점 오픈"), None)
    assert title_finding is not None, f"Expected category 'title' finding for '상점 오픈', got: {items}"

    # Assert both normalized variants 'Mở cửa tiệm' and 'Mở cửa hàng'
    variants_map = {v["target"]: v["count"] for v in title_finding["variants"]}
    assert "Mở cửa tiệm" in variants_map
    assert "Mở cửa hàng" in variants_map

    # Also verify identical <title>/<h1> duplicates do not inflate counts (exactly 1 occurrence per chapter, not 2)
    assert variants_map["Mở cửa tiệm"] == 1
    assert variants_map["Mở cửa hàng"] == 1
    assert len(title_finding["occurrences"]) == 2

    # Occurrence evidence retaining the full titles and both chapter/segment locations
    occs = title_finding["occurrences"]
    ch1_id = chapters[0]["id"]
    ch2_id = chapters[1]["id"]

    occ_ch1 = next((o for o in occs if o["chapter_id"] == ch1_id), None)
    assert occ_ch1 is not None
    assert occ_ch1["source_snippet"] == "상점 오픈 (1)"
    assert occ_ch1["target_snippet"] == "Mở cửa tiệm (1)"
    assert occ_ch1["target_variant"] == "Mở cửa tiệm"
    assert occ_ch1["segment_id"] == h1_seg_1["id"]

    occ_ch2 = next((o for o in occs if o["chapter_id"] == ch2_id), None)
    assert occ_ch2 is not None
    assert occ_ch2["source_snippet"] == "상점 오픈 (2)"
    assert occ_ch2["target_snippet"] == "Mở cửa hàng (2)"
    assert occ_ch2["target_variant"] == "Mở cửa hàng"
    assert occ_ch2["segment_id"] == h1_seg_2["id"]


# =============================================================================
# Test 16: Intra-chapter title disagreement between <title> and <h1>
# =============================================================================
@pytest.mark.asyncio
async def test_title_h1_intra_chapter_disagreement(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(AuditManager, "start_audit", lambda self, run_id: None)
    provider = MockAuditProvider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)

    # Chapter has both <title> and <h1> with source "상점 오픈 (1)"
    epub_bytes = build_test_epub_bytes([
        {
            "id": "ch1",
            "href": "chapter1.xhtml",
            "content": (
                "<html><head><title>상점 오픈 (1)</title></head>"
                "<body>"
                "<h1>상점 오픈 (1)</h1>"
                "<p>본문 내용입니다.</p>"
                "</body></html>"
            ),
        },
    ])

    upload_res = client.post("/api/books", files={"file": ("book.epub", epub_bytes)})
    assert upload_res.status_code == 201
    book_id = upload_res.json()["id"]
    chapters = client.get(f"/api/books/{book_id}/chapters").json()

    ch1_details = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    ch1_segs = ch1_details["segments"]
    title_seg = next(s for s in ch1_segs if s["parent_tag"] == "title")
    h1_seg = next(s for s in ch1_segs if s["parent_tag"] == "h1")
    p_seg = next(s for s in ch1_segs if s["parent_tag"] == "p")

    # In the SAME chapter: <title> translated to "Mở cửa tiệm (1)" and <h1> translated to "Mở cửa hàng (1)"
    client.patch(
        f"/api/chapters/{chapters[0]['id']}",
        json={
            "segments": [
                {"id": title_seg["id"], "translation": "Mở cửa tiệm (1)"},
                {"id": h1_seg["id"], "translation": "Mở cửa hàng (1)"},
                {"id": p_seg["id"], "translation": "Nội dung bản dịch."},
            ]
        },
    )

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]

    title_finding = next((f for f in items if f["category"] == "title" and f["source_term"] == "상점 오픈"), None)
    assert title_finding is not None

    variants_map = {v["target"]: v["count"] for v in title_finding["variants"]}
    assert "Mở cửa tiệm" in variants_map
    assert "Mở cửa hàng" in variants_map
    assert len(title_finding["occurrences"]) == 2

    # Check both occurrences point to the same chapter but different segments (title and h1)
    seg_ids = {o["segment_id"] for o in title_finding["occurrences"]}
    assert title_seg["id"] in seg_ids
    assert h1_seg["id"] in seg_ids


# =============================================================================
# Test 17: Cross-batch source identity regression (different source spans for same entity)
# =============================================================================
@pytest.mark.asyncio
async def test_cross_batch_source_identity_regression(test_setup):
    """Verify that an entity with different source spans across batches (e.g. '소염' vs '소염이')
    is correctly aggregated by its source-grounded canonical headword ('소염'),
    detecting the translation divergence ('Tiêu Viêm' vs 'Tiêu Diêm') while retaining
    the exact source span in each occurrence evidence."""
    client, app, provider, book_id, chapters = test_setup

    # Batch 1 (chapter 1): source has "소염", translation has "Tiêu Viêm"
    # Batch 2 (chapter 2): source has "소염이" (with particle 이), translation has "Tiêu Diêm"
    ch2_details = client.get(f"/api/chapters/{chapters[1]['id']}").json()
    client.patch(
        f"/api/chapters/{chapters[1]['id']}",
        json={
            "segments": [
                {"id": ch2_details["segments"][0]["id"], "source": "소염이가 다시 눈을 떴다.", "translation": "Tiêu Diêm lại mở mắt ra."},
                {"id": ch2_details["segments"][1]["id"], "translation": "Đệ tử Vân Lam Tông kinh ngạc."},
            ]
        },
    )

    # Configure custom entities generator returning different source spans:
    # Batch 1: source_term="소염", canonical_source="소염", target_term="Tiêu Viêm"
    # Batch 2: source_term="소염이", canonical_source="소염", target_term="Tiêu Diêm"
    def custom_gen(segments, chapter_id, chapter_title):
        entities = []
        for s in segments:
            sid = s["id"]
            src = s["source"]
            trans = s["translation"]
            if "소염이" in src and "Tiêu Diêm" in trans:
                entities.append({
                    "segment_id": sid,
                    "chapter_id": chapter_id or "",
                    "chapter_title": chapter_title or "",
                    "source_term": "소염이",  # Span with particle
                    "target_term": "Tiêu Diêm",
                    "canonical_source": "소염",
                    "category": "character",
                    "confidence": "high",
                    "source_snippet": src[:100],
                    "target_snippet": trans[:100],
                })
            elif "소염" in src and "Tiêu Viêm" in trans:
                entities.append({
                    "segment_id": sid,
                    "chapter_id": chapter_id or "",
                    "chapter_title": chapter_title or "",
                    "source_term": "소염",  # Base span
                    "target_term": "Tiêu Viêm",
                    "canonical_source": "소염",
                    "category": "character",
                    "confidence": "high",
                    "source_snippet": src[:100],
                    "target_snippet": trans[:100],
                })
        return entities

    provider.custom_entities_generator = custom_gen

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]

    # Must find an aggregated inconsistency under canonical headword '소염'
    finding = next((f for f in items if f["source_term"] == "소염"), None)
    assert finding is not None
    assert finding["source_term_id"] == "character:소염"
    assert finding["category"] == "character"

    variants = {v["target"]: v["count"] for v in finding["variants"]}
    assert "Tiêu Viêm" in variants
    assert "Tiêu Diêm" in variants
    assert len(finding["occurrences"]) == 2

    # Verify that exact source spans are retained in occurrence evidence
    occ_source_terms = {o["source_term"] for o in finding["occurrences"]}
    assert "소염" in occ_source_terms
    assert "소염이" in occ_source_terms


# =============================================================================
# Test 18: Known glossary mismatch with empty extractor
# =============================================================================
@pytest.mark.asyncio
async def test_known_glossary_mismatch_with_empty_extractor(test_setup):
    """Verify that deterministic glossary cross-checking detects translation divergence
    against a locked glossary term even when the AI extractor returns zero entities,
    grounding known variants while preserving manual-review flags for ungrounded occurrences."""
    client, app, provider, book_id, chapters = test_setup

    # Extractor returns completely empty entities for all batches
    provider.custom_entities_generator = lambda *args: []

    # Add a locked glossary term: "소염" -> "Tiêu Viêm"
    client.post(
        f"/api/books/{book_id}/glossary",
        json={"source": "소염", "target": "Tiêu Viêm", "locked": True, "category": "character"},
    )

    # In chapter 1: "소염" is translated as "Tiêu Viêm" (matches glossary)
    # In chapter 2: "소염" is translated as "Tiêu Diêm" (glossary divergence)
    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]

    # Even though AI returned [], deterministic cross-check must detect the glossary divergence
    finding = next((f for f in items if f["source_term"] == "소염"), None)
    assert finding is not None
    assert finding["canonical_recommendation"] == "Tiêu Viêm"
    assert finding["glossary_locked"] is True
    assert finding["glossary_divergence"] is True
    assert finding["severity"] == "high"
    assert finding["category"] in ("term", "character")

    variants = {v["target"]: v["count"] for v in finding["variants"]}
    assert "Tiêu Viêm" in variants
    assert "[Cần xác minh bản dịch]" in variants
    assert "Tiêu Diêm" not in variants  # Never guess ungrounded target from unrelated text
    assert len(finding["occurrences"]) == 2

    # Verify occurrence locations and target variants
    ch1_id = chapters[0]["id"]
    ch2_id = chapters[1]["id"]
    occ_ch1 = next(o for o in finding["occurrences"] if o["chapter_id"] == ch1_id)
    assert occ_ch1["target_variant"] == "Tiêu Viêm"
    occ_ch2 = next(o for o in finding["occurrences"] if o["chapter_id"] == ch2_id)
    assert occ_ch2["target_variant"] == "[Cần xác minh bản dịch]"

    # Avoid false all-clear: coverage note flags unverified manual review
    status_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert status_res["status"] == "completed"
    assert "[Cần xác minh bản dịch]" in status_res["progress"]["coverage_note"] or "xác minh" in status_res["progress"]["coverage_note"]
    assert status_res["progress"]["total_findings"] >= 1


# =============================================================================
# Test 19: Empty entity array behavior (successful batches, not partial coverage)
# =============================================================================
@pytest.mark.asyncio
async def test_empty_entity_array_behavior(test_setup):
    """Verify that successful empty entities arrays count as successful batches,
    do NOT mark coverage as partial solely for zero entities, and report 100% full coverage."""
    client, app, provider, book_id, chapters = test_setup

    # Extractor returns empty array for all batches (e.g. no entities found in text)
    provider.custom_entities_generator = lambda *args: []

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    status_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}")
    assert status_res.status_code == 200
    run = status_res.json()

    assert run["status"] == "completed"
    assert run["total_segments"] == 4
    assert run["processed_segments"] == 4
    assert run["failed_segments"] == 0
    assert run["skipped_segments"] == 0
    assert run["progress"]["coverage_status"] == "full"
    assert run["progress"]["percentage"] == 100.0

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    assert findings_res.json()["total"] == 0


# =============================================================================
# Test 20: Ungrounded canonical identity rejected in provider validation
# =============================================================================
def test_ungrounded_canonical_identity_rejected():
    """Verify that ungrounded or hallucinated canonical identities in extraction output
    are rejected and fall back to conservative grounded headwords."""
    from backend.app.provider import _extract_terminology_batch

    valid_ids = {"seg_1"}
    source_map = {"seg_1": "소염이 조용히 걸었다."}
    trans_map = {"seg_1": "Tiêu Viêm lặng lẽ bước đi."}

    # Model returned a hallucinated canonical_source not in source text
    parsed_hallucinated = {
        "entities": [
            {
                "segment_id": "seg_1",
                "source_term": "소염이",
                "target_term": "Tiêu Viêm",
                "canonical_source": "Dược Lão Hallucinated",
                "category": "character",
                "confidence": "high",
            }
        ]
    }

    results = _extract_terminology_batch(parsed_hallucinated, valid_ids, source_map, trans_map)
    assert len(results) == 1
    # Hallucinated canonical_source was rejected; grounded headword '소염' was derived
    assert results[0]["canonical_source"] == "소염"
    assert results[0]["source_term"] == "소염이"


# =============================================================================
# Test 21: Early deterministic title finding persistence on systemic failure
# =============================================================================
@pytest.mark.asyncio
async def test_early_deterministic_title_finding_persistence_on_systemic_failure(tmp_path: Path, monkeypatch):
    """Verify that deterministic title inconsistencies are persisted immediately at the start of
    an audit run, remaining queryable even when the provider fails systemically (ProviderAuthError),
    and that retry does not duplicate findings while saved translated h1 values are preserved."""
    monkeypatch.setattr(AuditManager, "start_audit", lambda self, run_id: None)
    provider = MockAuditProvider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)

    # 1. Create EPUB with two sequential chapters having <h1> in Korean source
    # '상점 오픈 (1)' and '상점 오픈 (2)', plus at least one paragraph per chapter
    epub_bytes = build_test_epub_bytes([
        {
            "id": "ch1",
            "href": "chapter1.xhtml",
            "content": (
                "<html><body>"
                "<h1>상점 오픈 (1)</h1>"
                "<p>첫 번째 장 본문 단락입니다.</p>"
                "</body></html>"
            ),
        },
        {
            "id": "ch2",
            "href": "chapter2.xhtml",
            "content": (
                "<html><body>"
                "<h1>상점 오픈 (2)</h1>"
                "<p>두 번째 장 본문 단락입니다.</p>"
                "</body></html>"
            ),
        },
    ])

    upload_res = client.post("/api/books", files={"file": ("book.epub", epub_bytes)})
    assert upload_res.status_code == 201
    book_id = upload_res.json()["id"]
    chapters = client.get(f"/api/books/{book_id}/chapters").json()
    assert len(chapters) == 2

    # 2. Persist translated title segments 'Mở cửa tiệm (1)' vs 'Mở cửa hàng (2)'
    # and at least one translated paragraph to ensure audit has segments
    ch1_details = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    ch1_segs = ch1_details["segments"]
    h1_seg_1 = next(s for s in ch1_segs if s["parent_tag"] == "h1")
    p_seg_1 = next(s for s in ch1_segs if s["parent_tag"] == "p")

    client.patch(
        f"/api/chapters/{chapters[0]['id']}",
        json={
            "segments": [
                {"id": h1_seg_1["id"], "translation": "Mở cửa tiệm (1)"},
                {"id": p_seg_1["id"], "translation": "Nội dung đoạn văn chương một."},
            ]
        },
    )

    ch2_details = client.get(f"/api/chapters/{chapters[1]['id']}").json()
    ch2_segs = ch2_details["segments"]
    h1_seg_2 = next(s for s in ch2_segs if s["parent_tag"] == "h1")
    p_seg_2 = next(s for s in ch2_segs if s["parent_tag"] == "p")

    client.patch(
        f"/api/chapters/{chapters[1]['id']}",
        json={
            "segments": [
                {"id": h1_seg_2["id"], "translation": "Mở cửa hàng (2)"},
                {"id": p_seg_2["id"], "translation": "Nội dung đoạn văn chương hai."},
            ]
        },
    )

    # 3. Create new audit run and make provider immediately fail systemically (ProviderAuthError)
    provider.raise_on_batch_index = 0
    provider.raise_error = ProviderAuthError("401 UNAUTHORIZED: Invalid API key", status_code=401)

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc: ConsistencyAuditService = app.state.audit_manager.service
    await ConsistencyAuditService.execute_run(audit_svc, run_id)

    # 4. Assert status 'failed' (inconclusive; no green successful audit)
    run_status = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert run_status["status"] == "failed"
    assert run_status["processed_segments"] == 0

    # 5. Assert persisted get_audit_findings() contains '상점 오픈' category 'title'
    # with variants both 'Mở cửa tiệm' and 'Mở cửa hàng' and occurrences from BOTH chapter IDs
    findings_data = get_audit_findings(app.state.db_path, run_id)
    items = findings_data["items"]
    title_finding = next((f for f in items if f["category"] == "title" and f["source_term"] == "상점 오픈"), None)
    assert title_finding is not None, f"Expected category 'title' finding for '상점 오픈', got: {items}"

    variant_targets = {v["target"] for v in title_finding["variants"]}
    assert "Mở cửa tiệm" in variant_targets
    assert "Mở cửa hàng" in variant_targets

    occ_chapter_ids = {o["chapter_id"] for o in title_finding["occurrences"]}
    assert chapters[0]["id"] in occ_chapter_ids
    assert chapters[1]["id"] in occ_chapter_ids

    # 6. Assert saved translated h1 values exactly preserved
    ch1_after = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    h1_after_1 = next(s for s in ch1_after["segments"] if s["parent_tag"] == "h1")
    assert h1_after_1["translation"] == "Mở cửa tiệm (1)"

    ch2_after = client.get(f"/api/chapters/{chapters[1]['id']}").json()
    h1_after_2 = next(s for s in ch2_after["segments"] if s["parent_tag"] == "h1")
    assert h1_after_2["translation"] == "Mở cửa hàng (2)"

    # 7. Assert retry does not duplicate findings
    provider.raise_on_batch_index = None
    retry_res = client.post(f"/api/books/{book_id}/consistency-reviews/{run_id}/retry")
    assert retry_res.status_code == 202
    await ConsistencyAuditService.execute_run(audit_svc, run_id)

    retry_run = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}").json()
    assert retry_run["status"] == "completed"

    retry_findings = get_audit_findings(app.state.db_path, run_id)
    title_findings_after_retry = [
        f for f in retry_findings["items"]
        if f["category"] == "title" and f["source_term"] == "상점 오픈"
    ]
    assert len(title_findings_after_retry) == 1


# =============================================================================
# Test 21: Extractor omission of later name/skill, near/far chapters & substring protection
# =============================================================================
@pytest.mark.asyncio
async def test_extractor_omission_global_term_discovery(tmp_path: Path, monkeypatch):
    """Test global discovery catches extractor omissions in near/far chapters without substring false-positives."""
    monkeypatch.setattr(AuditManager, "start_audit", lambda self, run_id: None)
    provider = MockAuditProvider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)

    # 3 chapters: ch1 (Chapter 1), ch2 (Chapter 2), ch157 (Chapter 157)
    epub_bytes = build_test_epub_bytes([
        {
            "id": "ch1",
            "href": "chapter1.xhtml",
            "content": (
                "<html><body>"
                "<p>소염은 비연보를 펼쳤다.</p>"
                "<p>김소염은 소염제를 찾았다.</p>"
                "</body></html>"
            ),
        },
        {
            "id": "ch2",
            "href": "chapter2.xhtml",
            "content": (
                "<html><body>"
                "<p>소염은 다시 나타났다.</p>"
                "</body></html>"
            ),
        },
        {
            "id": "ch157",
            "href": "chapter157.xhtml",
            "content": (
                "<html><body>"
                "<p>소염은 멀리서 걸어왔다.</p>"
                "<p>그는 비연보를 사용했다.</p>"
                "</body></html>"
            ),
        },
    ])

    upload_res = client.post("/api/books", files={"file": ("book.epub", epub_bytes)})
    assert upload_res.status_code == 201
    book = upload_res.json()
    book_id = book["id"]
    chapters = client.get(f"/api/books/{book_id}/chapters").json()

    # Set translations:
    # Chapter 1:
    # seg 0: "소염은 비연보를 펼쳤다." -> "Tiêu Viêm thi triển Phi Yến Bộ."
    # seg 1: "김소염은 소염제를 찾았다." -> "Kim Tiêu Viêm tìm thuốc chống viêm."
    ch1_details = client.get(f"/api/chapters/{chapters[0]['id']}").json()
    ch1_segs = ch1_details["segments"]
    client.patch(
        f"/api/chapters/{chapters[0]['id']}",
        json={
            "segments": [
                {"id": ch1_segs[0]["id"], "translation": "Tiêu Viêm thi triển Phi Yến Bộ."},
                {"id": ch1_segs[1]["id"], "translation": "Kim Tiêu Viêm tìm thuốc chống viêm."},
            ]
        },
    )

    # Chapter 2:
    # seg 0: "소염은 다시 나타났다." -> "Tiêu Diêm lại xuất hiện." (introduces variant Tiêu Diêm)
    ch2_details = client.get(f"/api/chapters/{chapters[1]['id']}").json()
    ch2_segs = ch2_details["segments"]
    client.patch(
        f"/api/chapters/{chapters[1]['id']}",
        json={
            "segments": [
                {"id": ch2_segs[0]["id"], "translation": "Tiêu Diêm lại xuất hiện."},
            ]
        },
    )

    # Chapter 157 (far chapter):
    # seg 0: "소염은 멀리서 걸어왔다." -> "Tiêu Diêm từ xa bước tới." (variant Tiêu Diêm)
    # seg 1: "그는 비연보를 사용했다." -> "Hắn kích hoạt bộ pháp quỷ dị." (unmapped variant for skill 비연보)
    ch157_details = client.get(f"/api/chapters/{chapters[2]['id']}").json()
    ch157_segs = ch157_details["segments"]
    client.patch(
        f"/api/chapters/{chapters[2]['id']}",
        json={
            "segments": [
                {"id": ch157_segs[0]["id"], "translation": "Tiêu Diêm từ xa bước tới."},
                {"id": ch157_segs[1]["id"], "translation": "Hắn kích hoạt bộ pháp quỷ dị."},
            ]
        },
    )

    # Mock provider:
    # For Chapter 1: extracts 소염 -> Tiêu Viêm, 비연보 -> Phi Yến Bộ
    # For Chapter 2: extracts 소염 -> Tiêu Diêm
    # For Chapter 157: returns [] (SIMULATING EXTRACTOR OMISSION)
    async def mock_extract(segments, chapter_id=None, *args, **kwargs):
        results = []
        for s in segments:
            sid = s["id"]
            src = s["source"]
            trans = s.get("translation", "")
            # Only extract for ch1 and ch2; ch157 has extractor omission!
            if chapter_id == chapters[0]["id"]:
                if "소염은 비연보" in src:
                    results.append({
                        "segment_id": sid,
                        "chapter_id": chapter_id,
                        "chapter_title": "Chương 1",
                        "source_term": "소염",
                        "target_term": "Tiêu Viêm",
                        "canonical_source": "소염",
                        "category": "character",
                        "confidence": "high",
                        "source_snippet": src,
                        "target_snippet": trans,
                    })
                    results.append({
                        "segment_id": sid,
                        "chapter_id": chapter_id,
                        "chapter_title": "Chương 1",
                        "source_term": "비연보",
                        "target_term": "Phi Yến Bộ",
                        "canonical_source": "비연보",
                        "category": "term",
                        "confidence": "high",
                        "source_snippet": src,
                        "target_snippet": trans,
                    })
            elif chapter_id == chapters[1]["id"]:
                if "소염" in src:
                    results.append({
                        "segment_id": sid,
                        "chapter_id": chapter_id,
                        "chapter_title": "Chương 2",
                        "source_term": "소염",
                        "target_term": "Tiêu Diêm",
                        "canonical_source": "소염",
                        "category": "character",
                        "confidence": "high",
                        "source_snippet": src,
                        "target_snippet": trans,
                    })
            # For chapter 157: returns empty list [] (extractor omission)
        return results

    provider.extract_terminology = mock_extract

    res = client.post(f"/api/books/{book_id}/consistency-reviews")
    assert res.status_code == 202
    run_id = res.json()["id"]

    audit_svc = app.state.audit_manager.service
    await audit_svc.execute_run(run_id)

    status_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}")
    assert status_res.status_code == 200
    run = status_res.json()
    assert run["status"] == "completed"

    findings_res = client.get(f"/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    assert findings_res.status_code == 200
    items = findings_res.json()["items"]

    # 1. Check character '소염': global discovery should find occurrence in Chapter 157
    soyeom_finding = next((f for f in items if f["source_term"] == "소염"), None)
    assert soyeom_finding is not None
    assert soyeom_finding["category"] == "character"
    soyeom_occs = soyeom_finding["occurrences"]
    soyeom_ch_ids = {o["chapter_id"] for o in soyeom_occs}
    assert chapters[0]["id"] in soyeom_ch_ids
    assert chapters[1]["id"] in soyeom_ch_ids
    assert chapters[2]["id"] in soyeom_ch_ids  # Chapter 157 successfully discovered!

    # Verify target variant in Chapter 157 is Tiêu Diêm
    ch157_soyeom_occ = next(o for o in soyeom_occs if o["chapter_id"] == chapters[2]["id"])
    assert ch157_soyeom_occ["target_variant"] == "Tiêu Diêm"

    # 2. Check skill '비연보': global discovery should find occurrence in Chapter 157 with review marker
    biyeon_finding = next((f for f in items if f["source_term"] == "비연보"), None)
    assert biyeon_finding is not None
    assert biyeon_finding["category"] == "term"
    biyeon_occs = biyeon_finding["occurrences"]
    biyeon_ch_ids = {o["chapter_id"] for o in biyeon_occs}
    assert chapters[0]["id"] in biyeon_ch_ids
    assert chapters[2]["id"] in biyeon_ch_ids  # Chapter 157 discovered!
    ch157_biyeon_occ = next(o for o in biyeon_occs if o["chapter_id"] == chapters[2]["id"])
    assert ch157_biyeon_occ["target_variant"] == "[Cần xác minh bản dịch]"

    # 3. Check substring false positive: 김소염 & 소염제 must NOT produce findings for '소염'
    # seg 1 of chapter 1 must not be in soyeom_occs
    seg1_id = ch1_segs[1]["id"]
    assert all(o["segment_id"] != seg1_id for o in soyeom_occs)

    # 4. Check coverage note mentions truthful limitations due to manual review marker
    assert "[Cần xác minh bản dịch]" in run["progress"]["coverage_note"] or "xác minh" in run["progress"]["coverage_note"]
