import json
import pytest
import httpx
from backend.app.context import build_translation_context
from backend.app.provider import GeminiProvider, ProviderValidationError, extract_model_json_payload
from backend.tests.test_provider import make_gemini_response


def test_token_truncated_result_rejected_even_if_json_parses():
    with pytest.raises(ProviderValidationError):
        extract_model_json_payload({'candidates':[{'finishReason':'MAX_TOKENS','content':{'parts':[{'text':'{"segments":[]}'}]}}]})


def test_thinking_only_is_not_a_translation():
    with pytest.raises(ProviderValidationError):
        extract_model_json_payload({'candidates':[{'finishReason':'STOP','content':{'parts':[{'thought':True,'text':'{"segments":[]}'}]}}]})


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, 25, {'text': 'bad'}])
async def test_translation_values_must_be_strings(invalid):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=make_gemini_response({'translations': [{'id': 's1', 'translated': invalid}]})))) as client:
        provider = GeminiProvider('test-only', client=client)
        with pytest.raises(ProviderValidationError):
            await provider.translate([{'id': 's1', 'source': '문장'}])


@pytest.mark.asyncio
@pytest.mark.parametrize('output', [
    {'issues': [], 'corrections': [{'id': 's1', 'corrected': None}]},
    {'issues': [], 'corrections': [{'id': 's1', 'corrected': 'A'}, {'id': 's1', 'corrected': 'B'}]},
    {'issues': [{'id': 'foreign', 'description': 'bad'}], 'corrections': []},
])
async def test_malformed_review_rejected(output):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=make_gemini_response(output)))) as client:
        provider = GeminiProvider('test-only', client=client)
        with pytest.raises(ProviderValidationError):
            await provider.review([{'id': 's1', 'source': '문장', 'translation': 'Câu'}])


@pytest.mark.asyncio
async def test_review_includes_prior_title_mappings_and_locked_glossary_in_prompt():
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json=make_gemini_response({"issues": [], "corrections": []}),
        )

    segments = [{"id": "s1", "source": "상점 오픈 (2)", "translation": "Mở cửa tiệm (2)"}]
    locked_glossary = [
        {"source": "상점 오픈", "target": "Mở cửa tiệm", "locked": True, "notes": "Khóa tiêu đề"}
    ]
    prior_title_mappings = {"상점 오픈": "Mở cửa tiệm"}

    ctx = build_translation_context(
        segments,
        glossary=locked_glossary,
        prior_titles=prior_title_mappings,
        is_review=True,
    )

    # Context retain checks
    assert ctx["prior_title_mappings"] == {"상점 오픈": "Mở cửa tiệm"}
    assert any(g["source"] == "상점 오픈" and g.get("locked") for g in ctx["glossary"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider("test-only", client=client)
        result = await provider.review(segments, context=ctx)

    assert result == {"issues": [], "corrections": {}}
    assert len(captured_requests) == 1

    payload = json.loads(captured_requests[0].content)

    # 1. Verify response schema is preserved
    schema = payload["generationConfig"]["responseJsonSchema"]
    assert schema["type"] == "object"
    assert "issues" in schema["properties"]
    assert "corrections" in schema["properties"]
    assert set(schema["required"]) == {"issues", "corrections"}

    # 2. Verify user prompt includes mapping and locked glossary instructions
    user_prompt = payload["contents"][0]["parts"][0]["text"]

    # Mapping
    assert "상점 오픈" in user_prompt
    assert "Mở cửa tiệm" in user_prompt
    assert "TIÊU ĐỀ ĐÃ DỊCH TỪ CÁC CHƯƠNG TRƯỚC" in user_prompt
    assert "- Gốc: '상점 오픈' => Dịch chuẩn: 'Mở cửa tiệm'" in user_prompt

    # Locked glossary and instruction
    assert "ĐÃ KHÓA / LOCKED" in user_prompt
    assert "QUY TẮC THẨM ĐỊNH THUẬT NGỮ (GLOSSARY REVIEW RULES)" in user_prompt
    assert "ƯU TIÊN CAO NHẤT CHO THUẬT NGỮ ĐÃ KHÓA (MANUALLY LOCKED GLOSSARY)" in user_prompt


@pytest.mark.asyncio
async def test_review_direct_dict_receives_title_mapping_and_locked_glossary():
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json=make_gemini_response({"issues": [], "corrections": []}),
        )

    segments = [{"id": "s1", "source": "상점 오픈 (2)", "translation": "Mở cửa tiệm (2)"}]
    raw_context = {
        "prior_title_mappings": {"상점 오픈": "Mở cửa tiệm"},
        "glossary": [{"source": "상점 오픈", "target": "Mở cửa tiệm", "locked": True}],
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider("test-only", client=client)
        result = await provider.review(segments, context=raw_context)

    assert result == {"issues": [], "corrections": {}}
    assert len(captured_requests) == 1

    payload = json.loads(captured_requests[0].content)
    schema = payload["generationConfig"]["responseJsonSchema"]
    assert set(schema["required"]) == {"issues", "corrections"}

    user_prompt = payload["contents"][0]["parts"][0]["text"]
    assert "- Gốc: '상점 오픈' => Dịch chuẩn: 'Mở cửa tiệm'" in user_prompt
    assert "- 상점 오픈 -> Mở cửa tiệm (ĐÃ KHÓA / LOCKED - BẮT BUỘC TUÂN THỦ CAO NHẤT)" in user_prompt
    assert "ƯU TIÊN CAO NHẤT CHO THUẬT NGỮ ĐÃ KHÓA (MANUALLY LOCKED GLOSSARY)" in user_prompt
