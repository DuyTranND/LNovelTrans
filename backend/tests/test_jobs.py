import asyncio
import pytest
from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.app.jobs import JobWorker
from backend.app.db import get_chapter, get_job, update_job_status, recover_interrupted_jobs, update_chapter_segments
from backend.app.provider import (
    ProviderAuthError,
    ProviderConnectError,
    ProviderError,
    ProviderModelError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from backend.tests.test_epub import build_test_epub_bytes


class Provider:
    def __init__(self):
        self.calls = []
        self.review_calls = []
        self.call_log = []
        self.fail_summary = False
        self.gate = None
        self.entered = asyncio.Event()
        self.bad = False
        self.bad_review = False

    async def translate(self, segments, context=None, **kwargs):
        seg_ids = [s['id'] for s in segments]
        self.calls.append(seg_ids)
        self.call_log.append(('translate', seg_ids))
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        result = {s['id']: 'Bản dịch ' + s['source'] for s in segments}
        return {} if self.bad else result

    async def review(self, segments, context=None, **kwargs):
        seg_ids = [s['id'] for s in segments]
        self.review_calls.append(seg_ids)
        self.call_log.append(('review', seg_ids))
        return {'issues': [], 'corrections': {segments[0]['id']: '   '} if self.bad_review else {}}

    async def summarize(self, segments, **kwargs):
        self.call_log.append(('summarize', len(segments)))
        if self.fail_summary:
            raise RuntimeError('summary unavailable')
        return 'Tóm tắt chương.'


@pytest.fixture
def setup(tmp_path):
    provider = Provider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)
    epub = build_test_epub_bytes([
        {'id': 'c1', 'href': 'c1.xhtml', 'content': '<html><body>' + ''.join(f'<p>문장 {i}</p>' for i in range(6)) + '</body></html>'},
        {'id': 'c2', 'href': 'c2.xhtml', 'content': '<html><body><p>다음</p></body></html>'},
    ])
    book = client.post('/api/books', files={'file': ('test.epub', epub)}).json()
    chapters = client.get(f"/api/books/{book['id']}/chapters").json()
    worker = JobWorker(app.state.db_path, lambda: provider)
    return client, app.state.db_path, provider, worker, book, chapters


@pytest.mark.asyncio
async def test_smoke_cap_is_job_wide_and_partial(setup):
    client, db, provider, worker, book, chapters = setup
    response = client.post(f"/api/books/{book['id']}/jobs", json={'max_segments': 3})
    assert response.status_code == 201
    job = await worker.step()
    assert job['status'] == 'completed'
    assert sum(map(len, provider.calls)) == 3
    assert get_chapter(db, chapters[0]['id'])['status'] == 'partial'
    assert all(s['translation'] is None for s in get_chapter(db, chapters[1]['id'])['segments'])


@pytest.mark.asyncio
async def test_summary_retry_does_not_retranslate(setup):
    client, db, provider, worker, book, chapters = setup
    job = client.post(f"/api/books/{book['id']}/jobs", json={'chapter_ids': [chapters[0]['id']]}).json()
    provider.fail_summary = True
    assert (await worker.step())['status'] == 'failed'
    calls = len(provider.calls)
    provider.fail_summary = False
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    assert (await worker.step())['status'] == 'completed'
    assert len(provider.calls) == calls


@pytest.mark.asyncio
async def test_pause_finishes_batch_then_restart_resume(setup):
    client, db, provider, worker, book, chapters = setup
    job = client.post(f"/api/books/{book['id']}/jobs").json()
    provider.gate = asyncio.Event()
    task = asyncio.create_task(worker.step())
    await asyncio.wait_for(provider.entered.wait(), 2)
    assert client.post(f"/api/jobs/{job['id']}/pause").json()['status'] == 'pausing'
    assert client.patch(f"/api/chapters/{chapters[0]['id']}", json={'segments': []}).status_code == 409
    provider.gate.set()
    assert (await task)['status'] == 'paused'
    assert all(s['translation'] for s in get_chapter(db, chapters[0]['id'])['segments'])
    recover_interrupted_jobs(db)
    assert get_job(db, job['id'])['status'] == 'paused'
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 200
    restarted_worker = JobWorker(db, lambda: provider)
    assert (await restarted_worker.step())['status'] == 'completed'
    assert sum(map(len, provider.calls)) == 7


@pytest.mark.asyncio
async def test_malformed_provider_never_saves_partial_batch(setup):
    client, db, provider, worker, book, chapters = setup
    provider.bad = True
    client.post(f"/api/books/{book['id']}/jobs")
    assert (await worker.step())['status'] == 'failed'
    assert all(s['translation'] is None for s in get_chapter(db, chapters[0]['id'])['segments'])


@pytest.mark.asyncio
async def test_saved_locked_glossary_cannot_block_translation_or_review(setup):
    """Even if SQLite has existing locked glossary rows, translation and review jobs complete successfully
    and are never blocked by locked glossary mismatch."""
    client, db, provider, worker, book, chapters = setup
    # Seed a locked glossary term in DB that is NOT in the translations
    client.post(f"/api/books/{book['id']}/glossary", json={'source': '문장', 'target': 'Thuật ngữ khóa không xuất hiện', 'locked': True})
    # Run job with review
    client.post(f"/api/books/{book['id']}/jobs", json={'review': True})
    job = await worker.step()
    # The job must complete successfully without being blocked by the locked term!
    assert job['status'] == 'completed'
    segs = get_chapter(db, chapters[0]['id'])['segments']
    assert all(s['translation'] is not None for s in segs)


def test_job_validation_and_nonoverlap(setup):
    client, db, provider, worker, book, chapters = setup
    url = f"/api/books/{book['id']}/jobs"
    assert client.post(url, json={'max_segments': 0}).status_code == 400
    assert client.post(url, json={'chapter_ids': ['foreign']}).status_code == 400
    assert client.post(url, json={'chapter_ids': [chapters[0]['id']] * 2}).status_code == 400
    job = client.post(url).json()
    assert client.post(url).status_code == 409
    client.post(f"/api/jobs/{job['id']}/pause")
    another = client.post(url).json()
    assert another['status'] == 'queued'
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 409


def test_memory_survives_restart_and_context_changes_invalidate(setup):
    from backend.app.memory import memory_key, load_memory, save_memory
    from backend.app.db import get_connection, init_db
    _, db, _, _, book, chapters = setup
    context = {'source': '문장', 'glossary': [], 'neighbor': 'speaker A'}
    key = memory_key(book['id'], chapters[0]['id'], 'model-a', False, context)
    with get_connection(db) as conn:
        save_memory(conn, key, {'id': 'Câu dịch'}, {})
    init_db(db)
    assert load_memory(db, key)['translations'] == {'id': 'Câu dịch'}
    assert load_memory(db, memory_key(book['id'], chapters[0]['id'], 'model-b', False, context)) is None
    assert load_memory(db, memory_key(book['id'], chapters[0]['id'], 'model-a', False, {**context, 'neighbor': 'speaker B'})) is None


def test_analysis_updates_bible_and_does_not_create_glossary(setup):
    """Story analysis extracts and updates Story Bible, without auto-creating any glossary terms."""
    client, db, provider, worker, book, chapters = setup
    seen = []
    async def analyze(segments, existing_locked_glossary=None):
        seen.extend(segments)
        return {'story_bible': 'Ngữ cảnh mới', 'glossary': []}
    provider.analyze = analyze
    client.post(f"/api/books/{book['id']}/glossary", json={'source': '문장', 'target': 'Bản dịch', 'locked': True})
    assert client.put(f"/api/books/{book['id']}/bible", json={'text': 'Ghi chú cũ'}).status_code == 200
    response = client.post(f"/api/books/{book['id']}/analyze")
    assert response.status_code == 200
    assert response.json()['text'] == 'Ghi chú cũ\n\nNgữ cảnh mới'
    assert response.json()['suggested_count'] == 0
    assert len(seen) <= 30 and sum(len(s['source']) for s in seen) <= 6000
    # No new glossary entries created in DB
    glossary = client.get(f"/api/books/{book['id']}/glossary").json()
    assert len(glossary) == 1
    assert glossary[0]['source'] == '문장'


def test_editing_source_invalidates_previous_translation(setup):
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']
    segment_id = get_chapter(db, chapter_id)['segments'][0]['id']
    url = f'/api/chapters/{chapter_id}'
    assert client.patch(url, json={'segments': [{'id': segment_id, 'translation': 'Bản dịch cũ'}]}).status_code == 200
    assert client.patch(url, json={'segments': [{'id': segment_id, 'source': '원문 수정'}]}).status_code == 200
    assert get_chapter(db, chapter_id)['segments'][0]['translation'] is None


@pytest.mark.asyncio
async def test_chapter_translation_processes_all_batches_to_completion(tmp_path):
    """Regression test: One click Dich chuong translates ALL pending text in the chapter.

    With >40 segments (max batch size = 40), the worker automatically processes multiple
    batches sequentially, marks the entire chapter as translated and job as completed,
    without requiring any max_segments cap or manual next-sentence interaction.
    """
    provider = Provider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)

    # Create EPUB with 55 paragraphs in chapter 1 (> 40 segments batch limit)
    para_count = 55
    epub_bytes = build_test_epub_bytes([
        {
            'id': 'c1',
            'href': 'c1.xhtml',
            'content': '<html><body>' + ''.join(f'<p>문장 {i}</p>' for i in range(para_count)) + '</body></html>',
        },
    ])
    book = client.post('/api/books', files={'file': ('test_large.epub', epub_bytes)}).json()
    chapters = client.get(f"/api/books/{book['id']}/chapters").json()
    chapter_id = chapters[0]['id']
    worker = JobWorker(app.state.db_path, lambda: provider)

    # Launch standard UI job request: NO max_segments specified
    response = client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    assert response.status_code == 201
    job_id = response.json()['id']

    # Process job to completion in a single worker execution step
    completed_job = await worker.step()
    assert completed_job is not None
    assert completed_job['id'] == job_id
    assert completed_job['status'] == 'completed'

    # Verify multiple batches were processed automatically by the worker
    # Batch limit is 40 segments, so 55 segments must produce 2 batches (40 and 15)
    assert len(provider.calls) == 2
    assert len(provider.calls[0]) == 40
    assert len(provider.calls[1]) == 15
    assert sum(len(call) for call in provider.calls) == 55

    # Verify that the entire chapter is completed with no pending segments remaining
    chap = get_chapter(app.state.db_path, chapter_id)
    assert chap['status'] == 'translated'
    assert len(chap['segments']) == 55
    for seg in chap['segments']:
        assert seg['translation'] is not None
        assert seg['translation'].startswith('Bản dịch 문장')

    # Verify summary was generated for the whole chapter
    from backend.app.db import get_book_summaries
    summaries = get_book_summaries(app.state.db_path, book['id'])
    assert len(summaries) == 1
    assert summaries[0] == 'Tóm tắt chương.'


@pytest.mark.asyncio
async def test_whole_chapter_translate_phase_then_review_phase_once(setup):
    """Whole chapter chronology: all pending segments translated in translation phase,
    then review phase runs once over translated content, followed by summary."""
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    response = client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    assert response.status_code == 201

    completed_job = await worker.step()
    assert completed_job['status'] == 'completed'

    # Verify stage chronology in call_log
    stages = [entry[0] for entry in provider.call_log]
    assert stages == ['translate', 'review', 'summarize']

    # Chapter 0 has 6 segments: all 6 translated, then all 6 reviewed
    assert len(provider.calls[0]) == 6
    assert len(provider.review_calls[0]) == 6

    # Chapter status and review_data presence
    chap = get_chapter(db, chapter_id)
    assert chap['status'] == 'translated'
    for s in chap['segments']:
        assert s['translation'] is not None
        assert s['review_data'] is not None  # marked reviewed


@pytest.mark.asyncio
async def test_job_captures_chosen_gemini_3_5_flash_lite_model_against_later_selector_change(tmp_path):
    """Verify created job snapshots gemini-3.5-flash-lite, and fake HTTP requests
    target /models/gemini-3.5-flash-lite:generateContent regardless of later selector change."""
    import json
    import httpx
    from backend.app.provider import GeminiProvider

    http_requests = []

    def mock_gemini_handler(request: httpx.Request) -> httpx.Response:
        http_requests.append(request)
        # Verify key is in header and NOT in URL
        assert request.headers.get("x-goog-api-key") == "fake-project-key"
        assert "key" not in request.url.params
        assert "/models/gemini-3.5-flash-lite:generateContent" in request.url.path

        req_body = json.loads(request.content)
        user_text = req_body["contents"][0]["parts"][0]["text"]
        sys_inst = req_body.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")

        if "Reviewer" in sys_inst or "thẩm định" in sys_inst:
            res_payload = {
                "issues": [],
                "corrections": [],
            }
        elif "tóm tắt" in sys_inst.lower() or "summar" in sys_inst.lower():
            res_payload = {
                "summary": "Tóm tắt chương mẫu."
            }
        else:
            batch_part = user_text
            if "### CÁC PHÂN ĐOẠN CẦN DỊCH" in user_text:
                batch_part = user_text.split("### CÁC PHÂN ĐOẠN CẦN DỊCH", 1)[1]
            idx = batch_part.find("[")
            if idx != -1:
                targets, _ = json.JSONDecoder().raw_decode(batch_part[idx:])
                translations = [
                    {"id": str(item["id"]), "translated": f"Bản dịch {str(item.get('source', '')).strip() or 'nội dung'}"}
                    for item in targets
                ]
            else:
                import re
                matches = re.findall(r"\[([a-zA-Z0-9_\-]+)\]:\s*([^\n\r]*)", batch_part)
                translations = [
                    {"id": sid, "translated": f"Bản dịch {src.strip() or 'nội dung'}"}
                    for sid, src in matches
                ]
            res_payload = {
                "translations": translations
            }

        body = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": json.dumps(res_payload)}],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(mock_gemini_handler)
    async with httpx.AsyncClient(transport=transport) as client_http:
        provider = GeminiProvider(api_key="fake-project-key", client=client_http)
        app = create_app(tmp_path, provider=provider, start_worker=False)
        client = TestClient(app)

        epub = build_test_epub_bytes([
            {'id': 'c1', 'href': 'c1.xhtml', 'content': '<html><body><p>문장 0</p><p>문장 1</p></body></html>'},
        ])
        book = client.post('/api/books', files={'file': ('test.epub', epub)}).json()
        chapters = client.get(f"/api/books/{book['id']}/chapters").json()

        # Configure locked glossary and story bible to verify they reach reviewer payload
        client.post(
            f"/api/books/{book['id']}/glossary",
            json={'source': '문장', 'target': 'Bản dịch', 'locked': True},
        )
        client.put(
            f"/api/books/{book['id']}/bible",
            json={'text': 'Bối cảnh giả tưởng vũ trụ'},
        )

        # Job is created with default gemini-3.5-flash-lite
        job_resp = client.post(
            f"/api/books/{book['id']}/jobs",
            json={'chapter_ids': [chapters[0]['id']], 'review': True},
        )
        assert job_resp.status_code == 201
        job_data = job_resp.json()
        assert job_data['params']['model'] == 'gemini-3.5-flash-lite'

        # Now change settings to a completely different model afterwards
        client.patch('/api/settings', json={'model': 'gemini-custom-changed'})

        # Execute worker
        worker = JobWorker(app.state.db_path, lambda: provider)
        completed = await worker.step()
        assert completed['status'] == 'completed'

        # Verify all HTTP requests targeted gemini-3.5-flash-lite
        assert len(http_requests) == 3  # translate, review, summarize
        for req in http_requests:
            assert "/models/gemini-3.5-flash-lite:generateContent" in req.url.path

        # Verify stage ordering
        # Request 0: translate
        assert "responseJsonSchema" in json.loads(http_requests[0].content).get("generationConfig", {})

        # Request 1: review - verify review prompt contains glossary mapping and locked precedence instruction
        rev_req = json.loads(http_requests[1].content)
        rev_sys_inst = rev_req.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
        assert "Reviewer" in rev_sys_inst or "thẩm định" in rev_sys_inst
        assert "LOCKED GLOSSARY" not in rev_sys_inst
        assert "thuật ngữ đã khóa" not in rev_sys_inst.lower()
        rev_user_text = rev_req["contents"][0]["parts"][0]["text"]
        assert "Bối cảnh giả tưởng vũ trụ" in rev_user_text
        assert "문장 -> Bản dịch" in rev_user_text
        assert "TỪ ĐIỂN" in rev_user_text
        assert "GLOSSARY" in rev_user_text
        assert "ƯU TIÊN CAO NHẤT CHO THUẬT NGỮ ĐÃ KHÓA (MANUALLY LOCKED GLOSSARY)" in rev_user_text
        assert "(ĐÃ KHÓA / LOCKED - BẮT BUỘC TUÂN THỦ CAO NHẤT)" in rev_user_text
        assert "/models/gemini-3.5-flash-lite:generateContent" in http_requests[1].url.path

        # Request 2: summarize
        sum_inst = json.loads(http_requests[2].content).get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
        assert "tóm tắt" in sum_inst.lower()


@pytest.mark.asyncio
async def test_pause_and_retry_does_not_repeat_completed_review(setup):
    """Resume and retry must not repeat already reviewed content."""
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    # Trigger failure at summary stage so translation + review complete first
    provider.fail_summary = True
    job = client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    ).json()

    failed_step = await worker.step()
    assert failed_step['status'] == 'failed'
    assert len(provider.calls) == 1
    assert len(provider.review_calls) == 1

    # Retry job with working summary
    provider.fail_summary = False
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200

    completed_step = await worker.step()
    assert completed_step['status'] == 'completed'

    # Translation and review were NOT repeated on retry!
    assert len(provider.calls) == 1
    assert len(provider.review_calls) == 1


def test_source_and_manual_changes_invalidate_review_state(setup):
    """Manual changes to translation or source invalidate relevant review state."""
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']
    seg_id = get_chapter(db, chapter_id)['segments'][0]['id']
    url = f'/api/chapters/{chapter_id}'

    # Seed reviewed fixture through legitimate DB helper
    update_chapter_segments(
        db,
        chapter_id,
        [{
            'id': seg_id,
            'translation': 'Bản dịch cũ',
            'review_data': [{'id': seg_id, 'type': 'style', 'description': 'lưu ý'}],
        }],
    )
    seg = next(s for s in get_chapter(db, chapter_id)['segments'] if s['id'] == seg_id)
    assert seg['review_data'] is not None
    assert seg['translation'] == 'Bản dịch cũ'

    # Editing translation manually clears review_data
    patch_resp = client.patch(url, json={'segments': [{'id': seg_id, 'translation': 'Bản dịch mới biên tập'}]})
    assert patch_resp.status_code == 200
    seg = next(s for s in get_chapter(db, chapter_id)['segments'] if s['id'] == seg_id)
    assert seg['translation'] == 'Bản dịch mới biên tập'
    assert seg['review_data'] is None

    # Re-seed reviewed fixture to test source change invalidation
    update_chapter_segments(
        db,
        chapter_id,
        [{
            'id': seg_id,
            'translation': 'Bản dịch mới biên tập',
            'review_data': [{'id': seg_id, 'type': 'style', 'description': 'lưu ý'}],
        }],
    )
    seg = next(s for s in get_chapter(db, chapter_id)['segments'] if s['id'] == seg_id)
    assert seg['review_data'] is not None

    # Editing source manually clears translation and review_data
    patch_src = client.patch(url, json={'segments': [{'id': seg_id, 'source': '원문 변경'}]})
    assert patch_src.status_code == 200
    seg = next(s for s in get_chapter(db, chapter_id)['segments'] if s['id'] == seg_id)
    assert seg['source'] == '원문 변경'
    assert seg['translation'] is None
    assert seg['review_data'] is None


@pytest.mark.asyncio
async def test_malformed_reviewer_correction_safety(setup):
    """Malformed reviewer correction (e.g. whitespace-only or empty) fails the job and rejects invalid corrections."""
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    # Provider review returns an invalid empty string correction
    provider.bad_review = True
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )

    result = await worker.step()
    assert result['status'] == 'failed'
    assert 'bị rỗng hoặc không hợp lệ' in result['error']

    # The invalid correction must never be applied
    chap = get_chapter(db, chapter_id)
    for seg in chap['segments']:
        assert seg['translation'] is not None and seg['translation'].strip() != ''
        assert 'Bản dịch' in seg['translation']


@pytest.mark.asyncio
async def test_review_cache_manual_translation_edit_and_cache_reuse(setup):
    """Manual translation edit causes fresh review; matching exact input reuses valid cache."""
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    # Step 1: Initial translation + review run
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    res = await worker.step()
    assert res['status'] == 'completed'
    assert len(provider.review_calls) == 1

    # Step 2: Clear review_data on segments to simulate need to review,
    # but KEEP identical translations and sources
    from backend.app.db import get_connection
    with get_connection(db) as conn:
        conn.execute("UPDATE segments SET review_data = NULL WHERE chapter_id = ?", (chapter_id,))

    # Step 3: Run review job again with exact same inputs -> cache hit, provider.review NOT called
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    res2 = await worker.step()
    assert res2['status'] == 'completed'
    assert len(provider.review_calls) == 1  # Still 1, reused cache!

    # Step 4: Now edit translation manually via client PATCH
    seg_id = get_chapter(db, chapter_id)['segments'][0]['id']
    client.patch(f"/api/chapters/{chapter_id}", json={
        'segments': [{'id': seg_id, 'translation': 'Bản dịch mới hoàn toàn'}]
    })

    # Step 5: Run review job again -> input has changed, cache miss, fresh review called!
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    res3 = await worker.step()
    assert res3['status'] == 'completed'
    assert len(provider.review_calls) == 2  # Incremented to 2!


@pytest.mark.asyncio
async def test_legacy_combined_cache_never_interpreted_as_new_review(setup):
    """Legacy combined translation+review cache entry must never be treated as valid review cache."""
    from backend.app.db import get_connection
    from backend.app.memory import load_review_memory, save_memory
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    # First translate without review
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': False},
    )
    await worker.step()
    assert len(provider.review_calls) == 0

    # Inject legacy unversioned entry into translation_memory table
    with get_connection(db) as conn:
        save_memory(conn, "legacy_combined_key", {"seg_1": "Bản dịch cũ"}, [{"id": "seg_1"}])

    # load_review_memory rejects legacy format
    assert load_review_memory(db, "legacy_combined_key") is None

    # Run review job -> provider.review MUST be called
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    res = await worker.step()
    assert res['status'] == 'completed'
    assert len(provider.review_calls) == 1


@pytest.mark.asyncio
async def test_cached_review_malformed_quarantined_fresh_recovery(setup, monkeypatch):
    """Cached review with malformed structure is quarantined and requested fresh rather than poisoning retry."""
    import backend.app.jobs
    from backend.app.db import get_connection
    from backend.app.memory import load_review_memory, save_review_memory
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    # First translate without review
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': False},
    )
    await worker.step()

    # Get segment details
    chap = get_chapter(db, chapter_id)
    segs = chap['segments']
    seg0_id = segs[0]['id']

    # Safely wrap review_memory_key to inject corrupted cache under the EXACT runtime key used by worker
    captured_keys: list[str] = []
    orig_review_key = backend.app.jobs.review_memory_key

    def wrapper(b_id, c_id, model, inputs, context):
        k = orig_review_key(b_id, c_id, model, inputs, context)
        captured_keys.append(k)
        if len(captured_keys) == 1:
            # Poison cache with an invalid empty string correction under exact key
            with get_connection(db) as conn:
                save_review_memory(conn, k, {seg0_id: '   '}, {})
        return k

    monkeypatch.setattr("backend.app.jobs.review_memory_key", wrapper)

    # Start review job - it encounters poisoned cache, quarantines it, calls provider fresh, and completes!
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    job = await worker.step()
    assert job['status'] == 'completed'
    assert len(captured_keys) == 1
    exact_key = captured_keys[0]

    # Verify provider was called fresh (corrupted cache did not bypass review)
    assert len(provider.review_calls) == 1

    # Ensure poisoned correction was NEVER applied to database segments
    chap_after = get_chapter(db, chapter_id)
    assert chap_after['segments'][0]['translation'] != '   '
    assert 'Bản dịch' in chap_after['segments'][0]['translation']

    # Verify invalid cache was quarantined and fresh valid review is stored in DB under exact key
    fresh_cache = load_review_memory(db, exact_key)
    assert fresh_cache is not None
    assert fresh_cache['corrections'] != {seg0_id: '   '}
    assert isinstance(fresh_cache['corrections'], dict)
    assert isinstance(fresh_cache['issues'], dict)


@pytest.mark.asyncio
async def test_cached_translation_malformed_quarantined_fresh_recovery(setup):
    """Cached translation with malformed payload is quarantined and requested fresh rather than failing forever."""
    from backend.app.db import get_connection
    from backend.app.context import build_translation_context
    from backend.app.memory import memory_key, save_memory, load_memory
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    chap = get_chapter(db, chapter_id)
    segs = chap['segments']
    ctx = build_translation_context(segs)
    t_key = memory_key(book['id'], chapter_id, 'gemini-3.5-flash-lite', False, ctx)

    # Poison translation cache with an empty translation string
    with get_connection(db) as conn:
        save_memory(conn, t_key, {segs[0]['id']: '   '}, {})

    # Start job - worker detects invalid cache, quarantines it, calls provider fresh
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': False},
    )
    job = await worker.step()
    assert job['status'] == 'completed'
    assert len(provider.calls) == 1

    # Ensure translation is valid and not empty
    chap_after = get_chapter(db, chapter_id)
    assert chap_after['segments'][0]['translation'].startswith('Bản dịch')


@pytest.mark.asyncio
async def test_cached_review_malformed_provider_bad_fails_safely(setup, monkeypatch):
    """When cache is poisoned AND provider also fails, job fails truthfully with sanitized error."""
    import backend.app.jobs
    from backend.app.db import get_connection
    from backend.app.memory import save_review_memory
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    # Translate first
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': False},
    )
    await worker.step()

    segs = get_chapter(db, chapter_id)['segments']
    seg0_id = segs[0]['id']

    # Safely wrap review_memory_key to inject corrupted cache under the EXACT runtime key used by worker
    captured_keys: list[str] = []
    orig_review_key = backend.app.jobs.review_memory_key

    def wrapper(b_id, c_id, model, inputs, context):
        k = orig_review_key(b_id, c_id, model, inputs, context)
        captured_keys.append(k)
        if len(captured_keys) == 1:
            with get_connection(db) as conn:
                save_review_memory(conn, k, {seg0_id: '   '}, {})
        return k

    monkeypatch.setattr("backend.app.jobs.review_memory_key", wrapper)

    provider.bad_review = True
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    failed_job = await worker.step()
    assert failed_job['status'] == 'failed'
    assert 'bị rỗng hoặc không hợp lệ' in failed_job['error']
    assert len(captured_keys) == 1
    exact_key = captured_keys[0]

    # Verify provider was indeed called fresh (corrupted cache was consumed and quarantined, not bypassed)
    assert len(provider.review_calls) == 1

    # Verify poisoned cache row was quarantined (deleted from DB, not left behind)
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT 1 FROM translation_memory WHERE context_key = ?",
            (exact_key,),
        ).fetchone()
    assert row is None

    # Verify book translations in DB were preserved safely and not overwritten
    chap_after = get_chapter(db, chapter_id)
    assert chap_after['segments'][0]['translation'] != '   '
    assert 'Bản dịch' in chap_after['segments'][0]['translation']


@pytest.mark.asyncio
async def test_cached_review_missing_fields_cannot_bypass_review(setup, monkeypatch):
    """Cached review missing corrections or issues cannot bypass review, is quarantined and invokes fresh review."""
    import json
    import backend.app.jobs
    from backend.app.db import get_connection
    from backend.app.memory import load_review_memory
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]['id']

    # Translate first
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': False},
    )
    await worker.step()

    # Safely wrap review_memory_key to inject malformed cache missing corrections/issues under EXACT runtime key
    captured_keys: list[str] = []
    orig_review_key = backend.app.jobs.review_memory_key

    def wrapper(b_id, c_id, model, inputs, context):
        k = orig_review_key(b_id, c_id, model, inputs, context)
        captured_keys.append(k)
        if len(captured_keys) == 1:
            # Inject malformed review cache missing corrections and issues entirely
            with get_connection(db) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO translation_memory(context_key, payload) VALUES (?, ?)",
                    (k, json.dumps({"kind": "review", "version": 2})),
                )
        return k

    monkeypatch.setattr("backend.app.jobs.review_memory_key", wrapper)

    # Start review job - worker detects missing cache fields, quarantines row, calls provider.review fresh, and persists valid review
    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapter_id], 'review': True},
    )
    job = await worker.step()
    assert job['status'] == 'completed'
    assert len(captured_keys) == 1
    exact_key = captured_keys[0]

    # Crucial: fresh review invoked, did NOT bypass!
    assert len(provider.review_calls) == 1

    # Verify updated cache in DB has valid corrections and issues dicts
    fresh_cache = load_review_memory(db, exact_key)
    assert fresh_cache is not None
    assert isinstance(fresh_cache.get("corrections"), dict)
    assert isinstance(fresh_cache.get("issues"), dict)
    assert fresh_cache.get("kind") == "review"
    assert fresh_cache.get("version") == 2


@pytest.mark.asyncio
async def test_call_provider_typeerror_valueerror_called_exactly_once_and_sanitized(tmp_path):
    """Provider raising TypeError or ValueError is called exactly once (no retry/re-invocation)
    and fails the job truthfully with sanitized error message."""
    class FailingProvider:
        def __init__(self):
            self.calls = 0
            self.api_key = "secret_key_12345"

        async def translate(self, segments, context=None, model=None):
            self.calls += 1
            # Raise TypeError or ValueError with sensitive data
            raise TypeError("Lỗi kiểu dữ liệu với secret_key_12345")

    provider = FailingProvider()
    app = create_app(tmp_path, provider=provider, start_worker=False)
    client = TestClient(app)

    epub = build_test_epub_bytes([
        {'id': 'c1', 'href': 'c1.xhtml', 'content': '<html><body><p>문장 0</p></body></html>'},
    ])
    book = client.post('/api/books', files={'file': ('test.epub', epub)}).json()
    chapters = client.get(f"/api/books/{book['id']}/chapters").json()

    client.post(
        f"/api/books/{book['id']}/jobs",
        json={'chapter_ids': [chapters[0]['id']]},
    )

    worker = JobWorker(app.state.db_path, lambda: provider)
    job = await worker.step()

    # Crucial assertion: provider was called EXACTLY ONCE!
    assert provider.calls == 1
    assert job['status'] == 'failed'
    # Secret must be sanitized
    assert "secret_key_12345" not in job['error']
    assert "[REDACTED]" in job['error'] or "Lỗi" in job['error']


def test_validate_review_output_rejects_mismatched_and_malformed_cached_issues():
    """validate_review_output strictly enforces required id/type/description strings
    and rejects cached per-segment entries where issue id does not match the map key."""
    from backend.app.jobs import validate_review_output
    from backend.app.provider import ProviderValidationError

    rev_batch = [
        {"id": "seg_1", "source": "원문 1", "translation": "Bản dịch 1"},
        {"id": "seg_2", "source": "원문 2", "translation": "Bản dịch 2"},
    ]

    # Case 1: Cached issue ID belongs to batch (seg_2) but does not match the map key (seg_1)
    mismatched_issues = {
        "seg_1": [{"id": "seg_2", "type": "warning", "description": "Lỗi ngữ pháp"}]
    }
    with pytest.raises(ProviderValidationError) as exc1:
        validate_review_output(rev_batch, {}, mismatched_issues, [])
    assert "không khớp với khóa phân đoạn" in str(exc1.value)

    # Case 2: Missing required string fields (type or description)
    missing_fields_issues = [
        {"id": "seg_1", "type": "warning"}  # missing description
    ]
    with pytest.raises(ProviderValidationError) as exc2:
        validate_review_output(rev_batch, {}, missing_fields_issues, [])
    assert "description" in str(exc2.value)

    # Case 3: Empty string type
    empty_type_issues = [
        {"id": "seg_1", "type": "  ", "description": "Lỗi ngữ pháp"}
    ]
    with pytest.raises(ProviderValidationError) as exc3:
        validate_review_output(rev_batch, {}, empty_type_issues, [])
    assert "type" in str(exc3.value)

    # Case 4: Valid issues pass cleanly
    valid_issues = {
        "seg_1": [{"id": "seg_1", "type": "style", "description": "Lưu ý câu từ"}]
    }
    corr, res_issues = validate_review_output(rev_batch, {}, valid_issues, [])
    assert res_issues["seg_1"][0]["id"] == "seg_1"


@pytest.mark.asyncio
async def test_validate_review_output_empty_correction_on_star_restored_while_normal_text_rejects():
    """validate_review_output restores symbol-only source when reviewer returns empty correction,
    while normal language empty correction strictly raises ProviderValidationError."""
    from backend.app.jobs import validate_review_output
    from backend.app.provider import ProviderValidationError

    rev_batch = [
        {"id": "seg_star", "source": "*", "translation": "*"},
        {"id": "seg_lang", "source": "문장 1", "translation": "Câu 1"},
    ]

    # 1. Empty correction on '*' is safely restored to '*'
    corrs_star = {"seg_star": "   "}
    corr_res, issues_res = validate_review_output(rev_batch, corrs_star, [])
    assert corr_res["seg_star"] == "*"

    # 2. Empty correction on normal language segment is strictly rejected
    corrs_lang = {"seg_lang": "   "}
    with pytest.raises(ProviderValidationError) as exc:
        validate_review_output(rev_batch, corrs_lang, [])
    assert "bị rỗng hoặc không hợp lệ" in str(exc.value)


@pytest.mark.asyncio
async def test_job_worker_reviewer_empty_correction_on_star_durable_status(setup):
    """JobWorker preserves '*' when reviewer returns empty correction on scene break,
    durably marking it reviewed without failing the job."""
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]["id"]

    # Patch segments in chapter to have a scene break '*' and a normal sentence
    c_segs = get_chapter(db, chapter_id)["segments"]
    star_seg_id = c_segs[0]["id"]
    lang_seg_id = c_segs[1]["id"]
    update_chapter_segments(
        db,
        chapter_id,
        [
            {"id": star_seg_id, "source": "*", "translation": "*"},
            {"id": lang_seg_id, "source": "문장 1", "translation": "Câu 1"},
        ],
    )

    # Provider review returns empty correction for star segment
    async def mock_review(segments, context=None, **kwargs):
        return {
            "issues": [],
            "corrections": {star_seg_id: "   "},
        }

    provider.review = mock_review

    # Run review job
    resp = client.post(
        f"/api/books/{book['id']}/jobs",
        json={"chapter_ids": [chapter_id], "review": True},
    )
    assert resp.status_code == 201

    job = await worker.step()
    assert job["status"] == "completed"

    refreshed_segs = get_chapter(db, chapter_id)["segments"]
    star_seg = next(s for s in refreshed_segs if s["id"] == star_seg_id)
    assert star_seg["translation"] == "*"
    assert star_seg["review_data"] is not None  # durably reviewed


@pytest.mark.asyncio
async def test_job_worker_review_transient_read_timeout_retries_at_in_flight_batch_and_completes(
    setup, monkeypatch
):
    """Mirroring the 157-chapter review failure: a transient ReadTimeout in REVIEWING phase
    retries at the exact in-flight batch, completes without duplicating work, and marks job completed."""
    monkeypatch.setattr("backend.app.jobs.JOB_TRANSIENT_INITIAL_DELAY", 0.001)
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]["id"]

    review_attempts = 0

    async def flaky_review(segments, context=None, **kwargs):
        nonlocal review_attempts
        review_attempts += 1
        if review_attempts == 1:
            raise ProviderTimeoutError(
                "Quá thời gian phản hồi (ReadTimeout) từ máy chủ Gemini sau 3 lần thử lại."
            )
        # Succeeded on retry
        return {
            "issues": [],
            "corrections": {},
        }

    provider.review = flaky_review

    resp = client.post(
        f"/api/books/{book['id']}/jobs",
        json={"chapter_ids": [chapter_id], "review": True},
    )
    assert resp.status_code == 201

    job = await worker.step()
    assert job["status"] == "completed"
    # Review failed once on ReadTimeout, then succeeded on in-flight retry -> 2 calls
    assert review_attempts == 2

    # All segments durably reviewed
    refreshed_segs = get_chapter(db, chapter_id)["segments"]
    assert all(s["review_data"] is not None for s in refreshed_segs)


@pytest.mark.asyncio
async def test_job_worker_translate_transient_connect_error_retries_and_completes(
    setup, monkeypatch
):
    """A transient ConnectError during translation retries at the in-flight batch and completes."""
    monkeypatch.setattr("backend.app.jobs.JOB_TRANSIENT_INITIAL_DELAY", 0.001)
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]["id"]

    translate_attempts = 0

    async def flaky_translate(segments, context=None, **kwargs):
        nonlocal translate_attempts
        translate_attempts += 1
        if translate_attempts == 1:
            raise ProviderConnectError(
                "Lỗi kết nối mạng (ConnectError) tới máy chủ Gemini sau 3 lần thử lại."
            )
        return {s["id"]: "Bản dịch " + s["source"] for s in segments}

    provider.translate = flaky_translate

    client.post(
        f"/api/books/{book['id']}/jobs",
        json={"chapter_ids": [chapter_id]},
    )

    job = await worker.step()
    assert job["status"] == "completed"
    assert translate_attempts == 2
    segs = get_chapter(db, chapter_id)["segments"]
    assert all(s["translation"] is not None for s in segs)


@pytest.mark.asyncio
async def test_job_worker_summarize_transient_timeout_retries_and_completes(
    setup, monkeypatch
):
    """A transient timeout during chapter summarization retries and saves summary successfully."""
    monkeypatch.setattr("backend.app.jobs.JOB_TRANSIENT_INITIAL_DELAY", 0.001)
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]["id"]

    summarize_attempts = 0

    async def flaky_summarize(segments, **kwargs):
        nonlocal summarize_attempts
        summarize_attempts += 1
        if summarize_attempts == 1:
            raise ProviderTimeoutError(
                "Quá thời gian chờ (Timeout) tới máy chủ Gemini sau 3 lần thử lại."
            )
        return "Tóm tắt chương đã hồi phục thành công."

    provider.summarize = flaky_summarize

    client.post(
        f"/api/books/{book['id']}/jobs",
        json={"chapter_ids": [chapter_id]},
    )

    job = await worker.step()
    assert job["status"] == "completed"
    assert summarize_attempts == 2

    from backend.app.db import get_book_summaries
    summaries = get_book_summaries(db, book["id"])
    assert len(summaries) == 1
    assert "hồi phục thành công" in summaries[0]


@pytest.mark.asyncio
async def test_job_worker_transient_retry_budget_exhausted_fails_safely(
    setup, monkeypatch
):
    """When transient network error persists beyond retry budget (3 retries = 4 total calls),
    the worker exhausts its budget and truthfully fails the job."""
    monkeypatch.setattr("backend.app.jobs.JOB_TRANSIENT_INITIAL_DELAY", 0.001)
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]["id"]

    call_count = 0

    async def persistent_failure(segments, context=None, **kwargs):
        nonlocal call_count
        call_count += 1
        raise ProviderTimeoutError(
            "Quá thời gian phản hồi (ReadTimeout) từ máy chủ Gemini."
        )

    provider.review = persistent_failure

    client.post(
        f"/api/books/{book['id']}/jobs",
        json={"chapter_ids": [chapter_id], "review": True},
    )

    job = await worker.step()
    assert job["status"] == "failed"
    # Initial call + 3 retries = 4 total calls
    assert call_count == 4
    assert "ReadTimeout" in job["error"]


@pytest.mark.asyncio
async def test_job_worker_non_retryable_auth_and_schema_errors_fail_immediately(setup):
    """AuthError and ValidationError are strictly non-retryable and fail with exactly 1 call."""
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]["id"]

    call_count = 0

    async def auth_error(segments, context=None, **kwargs):
        nonlocal call_count
        call_count += 1
        raise ProviderAuthError("Xác thực API Gemini thất bại (401/403).")

    provider.translate = auth_error

    client.post(
        f"/api/books/{book['id']}/jobs",
        json={"chapter_ids": [chapter_id]},
    )

    job = await worker.step()
    assert job["status"] == "failed"
    assert call_count == 1  # Exactly 1 call, zero retry!
    assert "Xác thực API Gemini thất bại" in job["error"]


@pytest.mark.asyncio
async def test_job_worker_pause_interrupts_transient_retry_delay(setup, monkeypatch):
    """When a transient error triggers a retry delay, requesting pause interrupts the delay
    and sets status to paused without failing the job, resuming cleanly afterwards."""
    # Use 1 second delay so we have time to trigger pause
    monkeypatch.setattr("backend.app.jobs.JOB_TRANSIENT_INITIAL_DELAY", 1.0)
    client, db, provider, worker, book, chapters = setup
    chapter_id = chapters[0]["id"]

    first_attempt_event = asyncio.Event()
    attempts = 0

    async def failing_once_review(segments, context=None, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_attempt_event.set()
            raise ProviderTimeoutError("Transient timeout")
        return {"issues": [], "corrections": {}}

    provider.review = failing_once_review

    job = client.post(
        f"/api/books/{book['id']}/jobs",
        json={"chapter_ids": [chapter_id], "review": True},
    ).json()

    # Run step as background task
    step_task = asyncio.create_task(worker.step())

    # Wait until first attempt fails and enters retry delay
    await asyncio.wait_for(first_attempt_event.wait(), 2.0)
    # Pause requested during retry delay
    pause_resp = client.post(f"/api/jobs/{job['id']}/pause")
    assert pause_resp.json()["status"] == "pausing"

    # Step task should cleanly return with paused status
    result_job = await asyncio.wait_for(step_task, 3.0)
    assert result_job["status"] == "paused"

    # Resume job and verify it completes without duplicate work
    monkeypatch.setattr("backend.app.jobs.JOB_TRANSIENT_INITIAL_DELAY", 0.001)
    resume_resp = client.post(f"/api/jobs/{job['id']}/resume")
    assert resume_resp.status_code == 200

    completed_job = await worker.step()
    assert completed_job["status"] == "completed"
    assert attempts == 2





