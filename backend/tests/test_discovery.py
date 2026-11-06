"""Automated tests for Pre-translation AI Discovery workflow (Mock-only Gemini)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
import pytest
from fastapi.testclient import TestClient

from backend.app.db import (
    approve_discovery_proposal,
    create_character,
    create_discovery_proposal,
    create_discovery_run,
    create_glossary_item,
    get_character,
    get_discovery_proposal,
    get_discovery_run,
    get_glossary_item,
    init_db,
    list_characters,
    list_discovery_proposals,
    list_glossary,
    recover_interrupted_discovery_runs,
    reject_discovery_proposal,
    update_discovery_run,
)
from backend.app.discovery import DiscoveryManager
from backend.app.epub import EPUB_MIMETYPE
from backend.app.main import create_app
from backend.app.provider import (
    GeminiProvider,
    ProviderValidationError,
    _validate_discovery_candidates,
)


class MockGeminiProvider:
    """Mock provider mimicking Gemini discovery output without external network calls."""

    def __init__(self, candidates_return=None):
        self.candidates_return = candidates_return or []
        self.call_count = 0

    async def discover_candidates(self, segments, *args, **kwargs):
        self.call_count += 1
        seg_id = segments[0]["id"] if segments else "seg_dummy"
        adapted = []
        for c in self.candidates_return:
            item = dict(c)
            if "canonical_source" not in item and "source_term" in item:
                item["canonical_source"] = item["source_term"]
            if "canonical_target" not in item and "target_term" in item:
                item["canonical_target"] = item["target_term"]
            item["segment_id"] = seg_id
            adapted.append(item)
        return adapted


def create_discovery_test_epub() -> bytes:
    """Generate EPUB with sample Korean text for character and term extraction."""
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

        opf_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="pub-id">test-disc-epub</dc:identifier>
        <dc:title>Solo Hunter Novel</dc:title>
        <dc:language>ko</dc:language>
        <dc:creator>Chugong</dc:creator>
    </metadata>
    <manifest>
        <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    </manifest>
    <spine>
        <itemref idref="ch1"/>
    </spine>
</package>"""
        zf.writestr("OEBPS/content.opf", opf_xml)

        ch1_xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
    <p>성진우는 D급 헌터였다. 서울의 헌터 협회 건물에서 차해인을 만났다.</p>
    <p>그림자 군주의 힘이 깨어났다.</p>
</body>
</html>""".encode("utf-8")
        zf.writestr("OEBPS/chapter1.xhtml", ch1_xml)

    return buf.getvalue()


@pytest.fixture
def mock_app_client(tmp_path):
    mock_prov = MockGeminiProvider(
        candidates_return=[
            {
                "segment_id": "seg_1",
                "source_term": "성진우",
                "source_span": "성진우는 D급 헌터였다.",
                "target_term": "Sung Jin-woo",
                "category": "CHARACTER",
                "gender": "male",
                "source_aliases": ["진우"],
                "speech_style": "Điềm tĩnh",
                "notes": "Nhân vật chính",
            },
            {
                "segment_id": "seg_1",
                "source_term": "차해인",
                "source_span": "차해인을 만났다.",
                "target_term": "Cha Hae-in",
                "category": "CHARACTER",
                "gender": "female",
                "source_aliases": [],
                "speech_style": "Nghiêm túc",
                "notes": "Phó hội trưởng",
            },
            {
                "segment_id": "seg_1",
                "source_term": "헌터 협회",
                "source_span": "서울의 헌터 협회 건물에서",
                "target_term": "Hiệp hội Thợ săn",
                "category": "ORGANIZATION",
                "gender": "unknown",
                "source_aliases": [],
                "speech_style": "",
                "notes": "Tổ chức quản lý thợ săn",
            },
            {
                "segment_id": "seg_2",
                "source_term": "그림자 군주",
                "source_span": "그림자 군주의 힘이 깨어났다.",
                "target_term": "Chúa Tể Bóng Tối",
                "category": "GLOSSARY",
                "gender": "unknown",
                "source_aliases": [],
                "speech_style": "",
                "notes": "Danh hiệu sức mạnh",
            },
        ]
    )
    app = create_app(data_dir=tmp_path, provider=mock_prov, start_worker=False)
    # inject discovery manager with mock provider
    app.state.discovery_manager = DiscoveryManager(
        app.state.db_path,
        provider_getter=lambda: mock_prov,
    )
    return TestClient(app, client=("127.0.0.1", 50000)), mock_prov, app.state.db_path


def test_provider_anti_hallucination_validation():
    """Test validation of discovery candidates against source text."""
    valid_segment_map = {
        "seg_100": "성진우는 서울에 도착했다.",
        "seg_101": "마수들이 던전에서 쏟아져 나왔다.",
    }

    raw_candidates = [
        # Valid candidate: occurs in seg_100
        {
            "segment_id": "seg_100",
            "source_term": "성진우",
            "source_span": "성진우는 서울에 도착했다.",
            "target_term": "Sung Jin-woo",
            "category": "CHARACTER",
            "gender": "male",
            "source_aliases": ["진우"],
        },
        # Hallucinated term: "이민호" does NOT occur in any segment!
        {
            "segment_id": "seg_100",
            "source_term": "이민호",
            "source_span": "이민호는 서울에 도착했다.",
            "target_term": "Lee Min-ho",
            "category": "CHARACTER",
        },
        # Candidate referencing non-existent segment_id
        {
            "segment_id": "seg_999",
            "source_term": "마수",
            "source_span": "마수들이",
            "target_term": "Ma thú",
            "category": "GLOSSARY",
        },
    ]

    validated = _validate_discovery_candidates(
        {"candidates": raw_candidates},
        valid_segment_map,
        chapter_id="chap_1",
        chapter_title="Chapter 1",
    )
    # Only the first candidate should pass validation
    assert len(validated) == 1
    assert validated[0]["canonical_source"] == "성진우"
    assert validated[0]["canonical_target"] == "Sung Jin-woo"


def test_proposal_approval_and_rejection_logic(tmp_path):
    """Test proposal status transitions, entity creation, and locked record collision prevention."""
    db_path = tmp_path / "test_disc_review.db"
    init_db(db_path)

    # Setup dummy book and chapter
    book_id = "book_rv"
    with open(tmp_path / "dummy.epub", "w") as f:
        f.write("dummy")

    from backend.app.db import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, ?, 'dummy.epub', 'now', 'now')",
            (book_id, "Review Test Book"),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('chap_rv1', ?, 'ch1', 0, 'ch1.xhtml', 'ch1.xhtml', 'Ch1', 'application/xhtml+xml', ?, 'untranslated', 'now', 'now')
            """,
            (book_id, b''),
        )
        conn.execute(
            """
            INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace)
            VALUES ('seg_rv1', 'chap_rv1', ?, 0, '/p[1]', 'text', '차해인 등장', '차해인 등장', NULL, 'p', 0)
            """,
            (book_id,),
        )

    # 1. Create a CHARACTER proposal
    prop_char = create_discovery_proposal(
        db_path,
        book_id=book_id,
        category="CHARACTER",
        canonical_source="차해인",
        canonical_target="Cha Hae-in",
        source_aliases=["해인"],
        gender="female",
        speech_style="Lịch sự",
        notes="Phó hội trưởng",
        segment_id="seg_rv1",
        source_span="차해인 등장",
    )
    assert prop_char["status"] == "pending"

    # 2. Approve CHARACTER proposal -> Creates Character profile
    app_res = approve_discovery_proposal(db_path, prop_char["id"])
    assert app_res["status"] == "approved"
    assert app_res["entity"]["canonical_source"] == "차해인"
    assert app_res["entity"]["canonical_target"] == "Cha Hae-in"

    # Verify character was created in DB
    chars = list_characters(db_path, book_id)
    assert len(chars) == 1
    created_char = chars[0]
    assert created_char["canonical_source"] == "차해인"

    # 3. Collision Protection: Lock character and try approving duplicate proposal
    from backend.app.db import update_character
    update_character(db_path, created_char["id"], locked=True)

    prop_char2 = create_discovery_proposal(
        db_path,
        book_id=book_id,
        category="CHARACTER",
        canonical_source="차해인",
        canonical_target="Cha Hae In Different",
    )
    with pytest.raises(ValueError, match="Không thể ghi đè nhân vật '차해인' vì đã bị khóa"):
        approve_discovery_proposal(db_path, prop_char2["id"])

    # 4. GLOSSARY proposal approval
    prop_gloss = create_discovery_proposal(
        db_path,
        book_id=book_id,
        category="GLOSSARY",
        canonical_source="던전 브레이크",
        canonical_target="Dungeon Break",
        notes="Sự cố cổng",
    )
    gloss_res = approve_discovery_proposal(db_path, prop_gloss["id"])
    assert gloss_res["status"] == "approved"
    gloss_list = list_glossary(db_path, book_id)
    assert len(gloss_list) == 1
    assert gloss_list[0]["source"] == "던전 브레이크"

    # 5. Reject proposal
    prop_reject = create_discovery_proposal(
        db_path,
        book_id=book_id,
        category="PLACE",
        canonical_source="제주도",
        canonical_target="Đảo Jeju",
    )
    rej_res = reject_discovery_proposal(db_path, prop_reject["id"])
    assert rej_res["status"] == "rejected"
    assert rej_res["proposal"]["status"] == "rejected"

    # Verify rejected item did not create character or glossary entry
    assert len(list_characters(db_path, book_id)) == 1
    assert len(list_glossary(db_path, book_id)) == 1

    # 6. Verify segments were NOT modified by any discovery action
    with get_connection(db_path) as conn:
        seg = conn.execute("SELECT translation FROM segments WHERE id = 'seg_rv1'").fetchone()
        assert seg["translation"] is None


def test_discovery_interruption_recovery(tmp_path):
    """Test that interrupted discovery runs are safely marked failed upon app startup."""
    db_path = tmp_path / "test_recovery.db"
    init_db(db_path)

    # Insert dummy book
    from backend.app.db import get_connection
    with get_connection(db_path) as conn:
        conn.execute("INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES ('b1', 'B1', 'f1.epub', 'now', 'now')")

    run = create_discovery_run(db_path, book_id="b1", model="gemini-3.5-flash-lite", chapter_ids=["ch1"])
    update_discovery_run(db_path, run["id"], status="processing")

    # Recovery function
    recovered = recover_interrupted_discovery_runs(db_path)
    assert recovered == 1

    updated_run = get_discovery_run(db_path, run["id"])
    assert updated_run["status"] == "interrupted"
    assert "gián đoạn" in updated_run["error"]


@pytest.mark.anyio
async def test_discovery_full_api_workflow(mock_app_client):
    """Test full HTTP API lifecycle: start discovery -> list proposals -> review proposal."""
    client, mock_prov, db_path = mock_app_client

    # 1. Upload EPUB
    epub_bytes = create_discovery_test_epub()
    upload_res = client.post(
        "/api/books",
        files={"file": ("hunter.epub", epub_bytes, "application/epub+zip")},
    )
    assert upload_res.status_code == 201
    book_id = upload_res.json()["id"]

    # 2. Trigger Pre-translation Discovery
    start_res = client.post(
        f"/api/books/{book_id}/discovery",
        json={"model": "gemini-3.5-flash-lite"},
    )
    assert start_res.status_code == 202
    run_data = start_res.json()
    run_id = run_data["id"]
    assert run_data["status"] in ("queued", "processing", "completed")

    # Wait for mock execution to complete (mock provider is synchronous inside discovery task)
    import asyncio
    for _ in range(20):
        run_status = client.get(f"/api/books/{book_id}/discovery/runs/{run_id}").json()
        if run_status["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.1)

    assert run_status["status"] == "completed"
    assert run_status["proposals_count"] >= 4

    # 3. List proposals
    props_res = client.get(f"/api/books/{book_id}/discovery/proposals?status=pending")
    assert props_res.status_code == 200
    proposals = props_res.json()
    assert len(proposals) >= 4

    # Find character proposal for '성진우'
    jinwoo_prop = next((p for p in proposals if p["canonical_source"] == "성진우"), None)
    assert jinwoo_prop is not None
    assert jinwoo_prop["status"] == "pending"
    assert jinwoo_prop["source_span"] != ""

    # 4. Approve '성진우' proposal with override
    review_res = client.post(
        f"/api/discovery/proposals/{jinwoo_prop['id']}/review",
        json={
            "action": "approve",
            "override_target": "Sung Jin-woo (Shadow Monarch)",
        },
    )
    assert review_res.status_code == 200
    res_data = review_res.json()
    assert res_data["status"] == "approved"
    assert res_data["entity"]["canonical_target"] == "Sung Jin-woo (Shadow Monarch)"

    # Verify character was created in book character list
    chars_res = client.get(f"/api/books/{book_id}/characters")
    assert chars_res.status_code == 200
    chars = chars_res.json()
    assert any(c["canonical_source"] == "성진우" for c in chars)

    # 5. Reject '헌터 협회' proposal
    assoc_prop = next((p for p in proposals if p["canonical_source"] == "헌터 협회"), None)
    assert assoc_prop is not None
    rej_res = client.post(
        f"/api/discovery/proposals/{assoc_prop['id']}/review",
        json={"action": "reject"},
    )
    assert rej_res.status_code == 200
    assert rej_res.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_discovery_max_segments_cap_exact_and_counts(tmp_path):
    """Test that max_segments processes exactly no more than cap and reports accurate counts."""
    db_path = tmp_path / "test_max_seg.db"
    init_db(db_path)
    book_id = "book_max_seg"

    from backend.app.db import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, 'Max Seg Book', 'b.epub', 'now', 'now')",
            (book_id,),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('chap_1', ?, 'c1', 0, 'c1.xhtml', 'c1.xhtml', 'Ch1', 'application/xhtml+xml', ?, 'untranslated', 'now', 'now')
            """,
            (book_id, b''),
        )
        for i in range(10):
            conn.execute(
                """
                INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace)
                VALUES (?, 'chap_1', ?, ?, '/p', 'text', ?, ?, NULL, 'p', 0)
                """,
                (f"seg_{i}", book_id, i, f"한국어 문장 {i}입니다.", f"한국어 문장 {i}입니다."),
            )

    received_batches = []

    class CapMockProvider:
        async def discover_candidates(self, segments, *args, **kwargs):
            received_batches.append(segments)
            return []

    mock_prov = CapMockProvider()
    dm = DiscoveryManager(db_path, lambda: mock_prov)

    # Request cap of 3 segments out of 10
    run = await dm.start_discovery(book_id, max_segments=3)
    assert run["total_segments"] == 3

    # Wait for completion
    import asyncio
    for _ in range(50):
        r = get_discovery_run(db_path, run["id"])
        if r and r["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert r["status"] == "completed"
    assert r["processed_segments"] == 3
    assert r["total_segments"] == 3

    # Total segments passed to provider across all batches must be exactly 3
    total_received = sum(len(b) for b in received_batches)
    assert total_received == 3


@pytest.mark.asyncio
async def test_discovery_oversized_single_source_chunking(tmp_path):
    """Test single >4000 char source is safely chunked under DISCOVERY_MAX_BATCH_CHARS while preserving segment ID."""
    db_path = tmp_path / "test_oversized.db"
    init_db(db_path)
    book_id = "book_oversized"

    # 5200 characters string
    long_korean_text = ("성진우는 던전에서 마수를 사냥하며 성장했다. " * 200) + "차해인이 나타났다."
    assert len(long_korean_text) > 4000

    from backend.app.db import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, 'Oversized Book', 'b.epub', 'now', 'now')",
            (book_id,),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('chap_over', ?, 'c1', 0, 'c1.xhtml', 'c1.xhtml', 'Ch1', 'application/xhtml+xml', ?, 'untranslated', 'now', 'now')
            """,
            (book_id, b''),
        )
        conn.execute(
            """
            INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace)
            VALUES ('seg_huge_1', 'chap_over', ?, 0, '/p', 'text', ?, ?, NULL, 'p', 0)
            """,
            (book_id, long_korean_text, long_korean_text),
        )

    dispatched_payload_chars = []

    class ChunkVerifierProvider:
        async def discover_candidates(self, segments, *args, **kwargs):
            total_chars = sum(len(s.get("source") or "") for s in segments)
            dispatched_payload_chars.append(total_chars)
            # return proposal with original segment id
            return [
                {
                    "canonical_source": "성진우",
                    "canonical_target": "Sung Jin-woo",
                    "category": "CHARACTER",
                    "segment_id": segments[0]["id"],
                }
            ]

    mock_prov = ChunkVerifierProvider()
    dm = DiscoveryManager(db_path, lambda: mock_prov)

    run = await dm.start_discovery(book_id)
    import asyncio
    for _ in range(50):
        r = get_discovery_run(db_path, run["id"])
        if r and r["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert r["status"] == "completed"
    assert r["processed_segments"] == 1

    # Every dispatched payload must strictly respect DISCOVERY_MAX_BATCH_CHARS (4000)
    assert len(dispatched_payload_chars) >= 2
    for payload_size in dispatched_payload_chars:
        assert payload_size <= 4000

    # Proposal persisted with the original segment_id
    props = list_discovery_proposals(db_path, book_id)
    assert len(props) >= 1
    assert props[0]["segment_id"] == "seg_huge_1"
    assert props[0]["canonical_source"] == "성진우"


@pytest.mark.asyncio
async def test_discovery_validation_max_segments_and_chapter_ids(tmp_path):
    """Test validation of positive max_segments and invalid/mixed/duplicate chapter_ids."""
    db_path = tmp_path / "test_val.db"
    init_db(db_path)
    book_id = "book_val"

    from backend.app.db import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, 'Val Book', 'b.epub', 'now', 'now')",
            (book_id,),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('c_valid1', ?, 'c1', 0, 'c1.xhtml', 'c1.xhtml', 'Ch1', 'application/xhtml+xml', ?, 'untranslated', 'now', 'now'),
                   ('c_valid2', ?, 'c2', 1, 'c2.xhtml', 'c2.xhtml', 'Ch2', 'application/xhtml+xml', ?, 'untranslated', 'now', 'now')
            """,
            (book_id, b'', book_id, b''),
        )

    dm = DiscoveryManager(db_path, lambda: None)

    # 1. max_segments <= 0 rejected
    with pytest.raises(ValueError, match="max_segments phải là số nguyên dương"):
        await dm.start_discovery(book_id, max_segments=0)
    with pytest.raises(ValueError, match="max_segments phải là số nguyên dương"):
        await dm.start_discovery(book_id, max_segments=-3)

    # 2. Empty chapter_ids rejected
    with pytest.raises(ValueError, match="chapter_ids không được rỗng"):
        await dm.start_discovery(book_id, chapter_ids=[])

    # 3. Invalid mixed chapter IDs rejected rather than silently lost
    with pytest.raises(ValueError, match="c_bogus"):
        await dm.start_discovery(book_id, chapter_ids=["c_valid1", "c_bogus"])

    # 4. Duplicate chapter IDs deduplicated without error
    # Provide provider to allow run creation
    class EmptyProvider:
        async def discover_candidates(self, segments, *args, **kwargs):
            return []

    dm_work = DiscoveryManager(db_path, lambda: EmptyProvider())
    run = await dm_work.start_discovery(book_id, chapter_ids=["c_valid1", "c_valid1", "c_valid2"])
    assert run["chapter_ids"] == ["c_valid1", "c_valid2"]
    assert run["total_chapters"] == 2


@pytest.mark.asyncio
async def test_discovery_cancel_preserves_terminal_states_and_accurate_responses(tmp_path):
    """Test active cancellation, idempotence, and preservation of completed/failed terminal states."""
    db_path = tmp_path / "translator.db"
    init_db(db_path)
    book_id = "book_cancel"

    from backend.app.db import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, 'Cancel Book', 'b.epub', 'now', 'now')",
            (book_id,),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('chap_c', ?, 'c1', 0, 'c1.xhtml', 'c1.xhtml', 'Ch1', 'application/xhtml+xml', ?, 'untranslated', 'now', 'now')
            """,
            (book_id, b''),
        )
        conn.execute(
            """
            INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace)
            VALUES ('s_c1', 'chap_c', ?, 0, '/p', 'text', '문장', '문장', NULL, 'p', 0)
            """,
            (book_id,),
        )

    # 1. Active cancellation on in-flight run
    import asyncio
    unblock_event = asyncio.Event()

    class HangingProvider:
        async def discover_candidates(self, segments, *args, **kwargs):
            await unblock_event.wait()
            return []

    hanging_prov = HangingProvider()
    dm = DiscoveryManager(db_path, lambda: hanging_prov)

    run = await dm.start_discovery(book_id)
    run_id = run["id"]

    # Active cancel
    cancelled = await dm.cancel_discovery(run_id)
    assert cancelled is True
    unblock_event.set()

    r = get_discovery_run(db_path, run_id)
    assert r["status"] == "cancelled"

    # Idempotent cancel on already cancelled run
    cancelled_again = await dm.cancel_discovery(run_id)
    assert cancelled_again is True
    assert get_discovery_run(db_path, run_id)["status"] == "cancelled"

    # 2. Terminal state preservation: completed run must NOT change to cancelled
    comp_run = create_discovery_run(db_path, book_id=book_id, model="gemini-3.5-flash-lite", chapter_ids=["chap_c"])
    update_discovery_run(db_path, comp_run["id"], status="completed")
    res_comp = await dm.cancel_discovery(comp_run["id"])
    assert res_comp is False
    assert get_discovery_run(db_path, comp_run["id"])["status"] == "completed"

    # 3. Terminal state preservation: failed run must NOT change to cancelled
    fail_run = create_discovery_run(db_path, book_id=book_id, model="gemini-3.5-flash-lite", chapter_ids=["chap_c"])
    update_discovery_run(db_path, fail_run["id"], status="failed", error="Fatal error")
    res_fail = await dm.cancel_discovery(fail_run["id"])
    assert res_fail is False
    assert get_discovery_run(db_path, fail_run["id"])["status"] == "failed"

    # 4. HTTP API Endpoint testing: terminal preservation, accurate response, and wrong-book 404
    app = create_app(data_dir=tmp_path, provider=hanging_prov, start_worker=False)
    app.state.discovery_manager = dm
    client = TestClient(app, client=("127.0.0.1", 50000))

    # Cancel on completed run returns completed, preserves DB
    http_comp = client.post(f"/api/books/{book_id}/discovery/runs/{comp_run['id']}/cancel")
    assert http_comp.status_code == 200
    assert http_comp.json() == {"status": "completed", "run_id": comp_run["id"]}
    assert get_discovery_run(db_path, comp_run["id"])["status"] == "completed"

    # Cancel on failed run returns failed, preserves DB
    http_fail = client.post(f"/api/books/{book_id}/discovery/runs/{fail_run['id']}/cancel")
    assert http_fail.status_code == 200
    assert http_fail.json() == {"status": "failed", "run_id": fail_run["id"]}
    assert get_discovery_run(db_path, fail_run["id"])["status"] == "failed"

    # Wrong book ID returns 404
    http_wb = client.post(f"/api/books/other_book/discovery/runs/{comp_run['id']}/cancel")
    assert http_wb.status_code == 404


@pytest.mark.asyncio
async def test_discovery_malformed_provider_response_fails_run_and_preserves_empty_success(tmp_path):
    """Test non-list or malformed provider response raises validation error and fails run, while [] succeeds."""
    db_path = tmp_path / "test_malformed.db"
    init_db(db_path)
    book_id = "book_malformed"

    from backend.app.db import get_connection
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO books (id, title, file_path, created_at, updated_at) VALUES (?, 'Mal Book', 'b.epub', 'now', 'now')",
            (book_id,),
        )
        conn.execute(
            """
            INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
            VALUES ('chap_m', ?, 'c1', 0, 'c1.xhtml', 'c1.xhtml', 'Ch1', 'application/xhtml+xml', ?, 'untranslated', 'now', 'now')
            """,
            (book_id, b''),
        )
        conn.execute(
            """
            INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace)
            VALUES ('s_m1', 'chap_m', ?, 0, '/p', 'text', '문장', '문장', NULL, 'p', 0)
            """,
            (book_id,),
        )

    import asyncio

    # 1. Non-list response (e.g. dict) -> must fail run with validation error
    class DictProvider:
        async def discover_candidates(self, segments, *args, **kwargs):
            return {"candidates": []}

    dm1 = DiscoveryManager(db_path, lambda: DictProvider())
    run1 = await dm1.start_discovery(book_id)
    for _ in range(50):
        r1 = get_discovery_run(db_path, run1["id"])
        if r1 and r1["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert r1["status"] == "failed"
    assert "danh sách" in r1["error"].lower() or "list" in r1["error"].lower()

    # 2. Malformed item in list (missing source/target) -> must fail run
    class MalformedItemProvider:
        async def discover_candidates(self, segments, *args, **kwargs):
            return [{"invalid_key": "some_value"}]

    dm2 = DiscoveryManager(db_path, lambda: MalformedItemProvider())
    run2 = await dm2.start_discovery(book_id)
    for _ in range(50):
        r2 = get_discovery_run(db_path, run2["id"])
        if r2 and r2["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert r2["status"] == "failed"
    assert "thuật ngữ" in r2["error"].lower()

    # 3. Legitimate empty list [] -> succeeds with completed status and 0 proposals
    class LegitimateEmptyProvider:
        async def discover_candidates(self, segments, *args, **kwargs):
            return []

    dm3 = DiscoveryManager(db_path, lambda: LegitimateEmptyProvider())
    run3 = await dm3.start_discovery(book_id)
    for _ in range(50):
        r3 = get_discovery_run(db_path, run3["id"])
        if r3 and r3["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert r3["status"] == "completed"
    assert r3["proposals_count"] == 0
