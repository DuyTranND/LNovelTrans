"""Unit test suite for GeminiProvider and context module.

All tests utilize httpx.MockTransport to ensure deterministic, isolated verification
with zero live network requests or secret exposure.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.app.context import (
    GlossaryEntry,
    TranslationMemory,
    batch_segments,
    build_translation_context,
    check_translation_heuristics,
    is_symbol_only_source,
    preserve_whitespace,
    resolve_glossary,
)
from backend.app.provider import (
    GeminiProvider,
    ProviderAuthError,
    ProviderConnectError,
    ProviderEmptyContentError,
    ProviderError,
    ProviderIdValidationError,
    ProviderModelError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderResponseFormatError,
    ProviderSafetyError,
    ProviderSchemaValidationError,
    ProviderTimeoutError,
    ProviderValidationError,
    _build_id_retry_user_prompt,
    _build_recovery_user_prompt,
    _build_translation_schema,
    _extract_translations_batch,
    _format_translation_directive,
    _normalize_translation_id,
    derive_conservative_headword,
    match_korean_term_in_text,
    validate_model_name,
)


# =============================================================================
# Helper mock responses
# =============================================================================

def make_gemini_response(
    content_obj: Any,
    finish_reason: str = "STOP",
    thought_text: str | None = None,
) -> dict[str, Any]:
    """Helper to construct Google Gemini generateContent REST response structure."""
    parts: list[dict[str, Any]] = []
    if thought_text:
        parts.append({"thought": True, "text": thought_text})

    json_str = json.dumps(content_obj, ensure_ascii=False)
    parts.append({"text": json_str})

    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": parts,
                },
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
            "totalTokenCount": 150,
        },
    }


# =============================================================================
# Provider Tests
# =============================================================================

@pytest.mark.asyncio
async def test_models_discovery_filters_generation_methods() -> None:
    """Model discovery must return generation-compatible names only and strip 'models/' prefix."""
    raw_models = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                "name": "models/text-embedding-004",
                "supportedGenerationMethods": ["embedContent"],
            },
            {
                "name": "models/gemini-3.8-flash",
                "supportedGenerationMethods": ["generateContent"],
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1beta/models"
        assert request.headers.get("x-goog-api-key") == "test-key"
        # Verify API key is NOT in query params
        assert "key" not in request.url.params
        return httpx.Response(200, json=raw_models)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        available = await provider.models()

    assert available == ["gemini-2.5-flash", "gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_translate_success_with_strict_schema() -> None:
    """Translating valid segments returns dict mapping segment_id -> translated text."""
    segments = [
        {"id": "c1_s0001", "source": "안녕하세요."},
        {"id": "c1_s0002", "source": "반갑습니다."},
    ]

    expected_output = {
        "translations": [
            {"id": "c1_s0001", "translated": "Xin chào."},
            {"id": "c1_s0002", "translated": "Rất vui được gặp bạn."},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/models/gemini-3.5-flash-lite:generateContent" in request.url.path
        assert request.headers.get("x-goog-api-key") == "test-key"
        payload = json.loads(request.content)
        assert "generationConfig" in payload
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json=make_gemini_response(expected_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert result == {
        "c1_s0001": "Xin chào.",
        "c1_s0002": "Rất vui được gặp bạn.",
    }


@pytest.mark.asyncio
async def test_thinking_parts_distinguished_and_ignored() -> None:
    """Model parts with thought=True should be ignored, only the JSON part extracted."""
    segments = [{"id": "c1_s0001", "source": "마왕이 나타났다."}]
    payload = {
        "translations": [
            {"id": "c1_s0001", "translated": "Ma vương đã xuất hiện."}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=make_gemini_response(
                payload, thought_text="Thinking process: translate accurately."
            ),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert result == {"c1_s0001": "Ma vương đã xuất hiện."}


@pytest.mark.asyncio
async def test_schema_validation_malformed_json() -> None:
    """Malformed non-JSON candidate content raises ProviderValidationError."""
    segments = [{"id": "c1_s0001", "source": "테스트"}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Đây không phải là JSON hợp lệ"}],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "không phải là JSON hợp lệ" in str(exc_info.value)


@pytest.mark.asyncio
async def test_schema_validation_missing_ids() -> None:
    """Response omitting requested segment ID raises ProviderValidationError."""
    segments = [
        {"id": "c1_s0001", "source": "문장 1"},
        {"id": "c1_s0002", "source": "문장 2"},
    ]

    # Model returned only c1_s0001
    bad_output = {
        "translations": [
            {"id": "c1_s0001", "translated": "Câu 1"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "thiếu các phân đoạn ID" in str(exc_info.value)
        assert "c1_s0002" in str(exc_info.value)


@pytest.mark.asyncio
async def test_schema_validation_extra_ids() -> None:
    """Response containing unexpected extra IDs raises ProviderValidationError after bounded retry."""
    segments = [{"id": "c1_s0001", "source": "문장 1"}]

    bad_output = {
        "translations": [
            {"id": "c1_s0001", "translated": "Câu 1"},
            {"id": "c1_s9999", "translated": "Câu thừa"},
        ]
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "chứa các ID thừa" in str(exc_info.value)
        assert "c1_s9999" in str(exc_info.value)
    assert call_count == 2


@pytest.mark.asyncio
async def test_schema_validation_duplicate_ids() -> None:
    """Response containing duplicate segment IDs raises ProviderValidationError after bounded retry."""
    segments = [{"id": "c1_s0001", "source": "문장 1"}]

    bad_output = {
        "translations": [
            {"id": "c1_s0001", "translated": "Câu 1 bản A"},
            {"id": "c1_s0001", "translated": "Câu 1 bản B"},
        ]
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "trùng lặp" in str(exc_info.value)
    assert call_count == 2


@pytest.mark.asyncio
async def test_schema_validation_empty_translation() -> None:
    """Empty translation string for non-empty source raises ProviderValidationError."""
    segments = [{"id": "c1_s0001", "source": "중요한 문장"}]

    bad_output = {
        "translations": [
            {"id": "c1_s0001", "translated": "   "},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "bị rỗng" in str(exc_info.value)


@pytest.mark.asyncio
async def test_review_validation_and_corrections_subset() -> None:
    """Review returns issues and corrections; invalid or empty corrections are rejected."""
    segments = [
        {"id": "c1_s0001", "source": "마왕을 죽여라", "translation": "Giết ma vương"},
        {"id": "c1_s0002", "source": "알겠습니다", "translation": "Vâng"},
    ]

    review_data = {
        "issues": [
            {"id": "c1_s0001", "type": "glossary", "description": "Nên viết hoa Ma Vương"}
        ],
        "corrections": [
            {"id": "c1_s0001", "corrected": "Hãy tiêu diệt Ma Vương"}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(review_data))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.review(segments)

    assert result["corrections"] == {"c1_s0001": "Hãy tiêu diệt Ma Vương"}
    assert any("Ma Vương" in iss["description"] for iss in result["issues"])


@pytest.mark.asyncio
async def test_review_rejects_extra_correction_ids() -> None:
    """Review with correction ID not in requested segments raises ProviderValidationError."""
    segments = [{"id": "c1_s0001", "source": "원문", "translation": "Bản dịch"}]

    bad_review = {
        "issues": [],
        "corrections": [
            {"id": "c1_s9999", "corrected": "Sửa đoạn không tồn tại"}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_review))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.review(segments)
        assert "không thuộc các phân đoạn được yêu cầu" in str(exc_info.value)


@pytest.mark.asyncio
async def test_review_rejects_empty_correction_string() -> None:
    """Review with empty correction text raises ProviderValidationError."""
    segments = [{"id": "c1_s0001", "source": "원문", "translation": "Bản dịch"}]

    bad_review = {
        "issues": [],
        "corrections": [
            {"id": "c1_s0001", "corrected": "   "}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_review))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.review(segments)
        assert "bị rỗng" in str(exc_info.value)


@pytest.mark.asyncio
async def test_summarize_success() -> None:
    """Summarize returns a concise text string."""
    segments = [
        {"id": "c1_s0001", "source": "주인공이 각성했다."},
        {"id": "c1_s0002", "source": "던전이 열렸다."},
    ]

    summary_data = {
        "summary": "Nhân vật chính đã thức tỉnh kỹ năng mới và một hầm ngục bí ẩn xuất hiện."
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(summary_data))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        summary = await provider.summarize(segments)

    assert "thức tỉnh" in summary


@pytest.mark.asyncio
async def test_analyze_extracts_story_bible_only() -> None:
    """Analyze returns story bible only, without generating or suggesting glossary items."""
    segments = [{"id": "c1_s0001", "source": "용사가 성검을 들었다."}]

    model_analysis = {
        "story_bible": "Thế giới hậu tận thế giả tưởng.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        schema = payload["generationConfig"]["responseJsonSchema"]
        assert "story_bible" in schema["properties"]
        assert "glossary" not in schema["properties"]
        return httpx.Response(200, json=make_gemini_response(model_analysis))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.analyze(segments)

    assert result["story_bible"] == "Thế giới hậu tận thế giả tưởng."
    assert result["glossary"] == []


# =============================================================================
# Retries vs Non-retryable Failures & Key Redaction
# =============================================================================

@pytest.mark.asyncio
async def test_retries_on_503_and_recovers() -> None:
    """Transient 503 error is retried and succeeds on subsequent try."""
    attempts = 0
    segments = [{"id": "c1_s0001", "source": "테스트"}]
    ok_payload = {"translations": [{"id": "c1_s0001", "translated": "Thử nghiệm"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json=make_gemini_response(ok_payload))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="test-key",
            client=client,
            max_retries=2,
            retry_delay=0.0,
        )
        result = await provider.translate(segments)

    assert attempts == 2
    assert result == {"c1_s0001": "Thử nghiệm"}


@pytest.mark.asyncio
async def test_retries_exhausted_on_429_raises_rate_limit_error() -> None:
    """Repeated 429 rate limit errors exhaust retries and raise ProviderRateLimitError."""
    attempts = 0
    segments = [{"id": "c1_s0001", "source": "테스트"}]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, text="Resource Exhausted")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="test-key",
            client=client,
            max_retries=2,
            retry_delay=0.0,
        )
        with pytest.raises(ProviderRateLimitError) as exc_info:
            await provider.translate(segments)
        assert exc_info.value.status_code == 429
        assert "Rate Limit" in str(exc_info.value)

    assert attempts == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_auth_failure_401_no_retry() -> None:
    """HTTP 401 raises ProviderAuthError immediately without retrying."""
    attempts = 0
    segments = [{"id": "c1_s0001", "source": "테스트"}]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="test-key",
            client=client,
            max_retries=3,
            retry_delay=0.0,
        )
        with pytest.raises(ProviderAuthError):
            await provider.translate(segments)

    assert attempts == 1  # No retry for 401


@pytest.mark.asyncio
async def test_auth_401_reports_primary_and_sanitizes_upstream_details() -> None:
    primary = "dummy-primary-secret"
    secondary = "dummy-secondary-secret"
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        used_keys.append(request.headers["x-goog-api-key"])
        return httpx.Response(401, json={"error": {
            "status": "UNAUTHENTICATED",
            "message": f"Credential rejected: {primary} and {secondary}",
            "details": [{"reason": "API_KEY_INVALID"}],
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider(
            api_key=primary, secondary_api_key=secondary, client=client, retry_delay=0
        )
        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

    error = exc_info.value
    assert used_keys == [primary]  # Authentication errors do not rotate keys.
    assert error.status_code == 401
    assert "HTTP 401" in str(error)
    assert "GEMINI_API_KEY)" in str(error)
    assert "UNAUTHENTICATED" in str(error)
    assert "API_KEY_INVALID" in str(error)
    assert "[REDACTED]" in str(error)
    assert primary not in str(error) and secondary not in str(error)


@pytest.mark.asyncio
async def test_permission_403_reports_primary_without_switching() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        used_keys.append(request.headers["x-goog-api-key"])
        return httpx.Response(403, json={"error": {
            "status": "PERMISSION_DENIED",
            "message": "API key is restricted to another API or project.",
            "details": [{"reason": "API_KEY_SERVICE_BLOCKED"}],
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider(
            api_key="primary", secondary_api_key="secondary", client=client, retry_delay=0
        )
        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

    assert used_keys == ["primary"]
    assert exc_info.value.status_code == 403
    for value in ("HTTP 403", "GEMINI_API_KEY)", "PERMISSION_DENIED", "API_KEY_SERVICE_BLOCKED"):
        assert value in str(exc_info.value)


@pytest.mark.asyncio
async def test_permission_403_on_secondary_reports_correct_key_role() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        used_keys.append(key)
        if key == "primary":
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(403, json={"error": {
            "status": "PERMISSION_DENIED", "message": "Project not permitted"
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider(
            api_key="primary", secondary_api_key="secondary", client=client, retry_delay=0
        )
        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

    assert used_keys == ["primary", "secondary"]
    assert exc_info.value.status_code == 403
    assert "GEMINI_API_KEY_SECONDARY" in str(exc_info.value)
    assert "PERMISSION_DENIED" in str(exc_info.value)


@pytest.mark.asyncio
async def test_structured_quota_403_rotates_to_secondary() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        used_keys.append(key)
        if key == "primary":
            return httpx.Response(403, json={"error": {
                "status": "RESOURCE_EXHAUSTED", "message": "Quota exhausted"
            }})
        return httpx.Response(200, json=make_gemini_response({
            "translations": [{"id": "s1", "translated": "Bản dịch"}]
        }))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider(
            api_key="primary", secondary_api_key="secondary", client=client, retry_delay=0
        )
        result = await provider.translate([{"id": "s1", "source": "문장"}])

    assert used_keys == ["primary", "secondary"]
    assert result == {"s1": "Bản dịch"}


@pytest.mark.asyncio
async def test_quota_403_without_secondary_preserves_original_status() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(403, json={"error": {
            "status": "RESOURCE_EXHAUSTED", "message": "Quota exhausted"
        }})
    )) as client:
        provider = GeminiProvider(api_key="primary", client=client, max_retries=0)
        with pytest.raises(ProviderRateLimitError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

    assert exc_info.value.status_code == 403
    assert "RESOURCE_EXHAUSTED" in str(exc_info.value)


@pytest.mark.asyncio
async def test_permission_403_with_quota_words_does_not_rotate() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        used_keys.append(request.headers["x-goog-api-key"])
        return httpx.Response(403, json={"error": {
            "status": "PERMISSION_DENIED", "message": "Some text mentions RESOURCE_EXHAUSTED"
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider(
            api_key="primary", secondary_api_key="secondary", client=client, retry_delay=0
        )
        with pytest.raises(ProviderAuthError):
            await provider.translate([{"id": "s1", "source": "문장"}])

    assert used_keys == ["primary"]


@pytest.mark.asyncio
async def test_model_not_found_404_no_retry() -> None:
    """HTTP 404 raises ProviderModelError immediately without retrying."""
    attempts = 0
    segments = [{"id": "c1_s0001", "source": "테스트"}]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"error": {"message": "Model not found"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="test-key",
            model="gemini-nonexistent",
            client=client,
            max_retries=3,
            retry_delay=0.0,
        )
        with pytest.raises(ProviderModelError):
            await provider.translate(segments)

    assert attempts == 1  # No retry for 404


@pytest.mark.asyncio
async def test_secret_not_present_in_provider_error() -> None:
    """API key secret must never appear in ProviderError exception messages or details."""
    secret = "AIzaSyD_SECRET_KEY_123456789"
    segments = [{"id": "c1_s0001", "source": "테스트"}]

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate upstream server leaking the key in error body
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Bad request for key {secret} on resource",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key=secret,
            client=client,
            max_retries=0,
        )
        with pytest.raises(ProviderError) as exc_info:
            await provider.translate(segments)

        err_str = str(exc_info.value)
        err_details = str(exc_info.value.details)
        assert secret not in err_str
        assert secret not in err_details
        assert "[REDACTED]" in err_str or "[REDACTED]" in err_details


@pytest.mark.asyncio
async def test_safety_finish_reason_raises_safety_error() -> None:
    """Candidates blocked with SAFETY finishReason raise ProviderSafetyError."""
    segments = [{"id": "c1_s0001", "source": "위험한 내용"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response({}, finish_reason="SAFETY"))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderSafetyError):
            await provider.translate(segments)


# =============================================================================
# Context, Batching, and Whitespace Tests
# =============================================================================

def test_preserve_whitespace() -> None:
    """Whitespace preservation accurately preserves leading and trailing whitespace."""
    # Leading and trailing spaces
    assert preserve_whitespace("   안녕하세요   ", "Xin chào") == "   Xin chào   "

    # Newlines and tabs
    assert preserve_whitespace("\n\t단어\n", "Từ ngữ") == "\n\tTừ ngữ\n"

    # No whitespace
    assert preserve_whitespace("단어", "Từ ngữ") == "Từ ngữ"

    # Only leading
    assert preserve_whitespace("  단어", "Từ") == "  Từ"

    # Only trailing
    assert preserve_whitespace("단어  ", "Từ") == "Từ  "

    # All whitespace source
    assert preserve_whitespace("   ", "") == "   "


def test_batch_segments_max_count_and_char_limits() -> None:
    """batch_segments respects max 40 segments and ~6000 character limits."""
    # 50 segments of 50 chars each -> batch 1 has 40, batch 2 has 10
    segments = [{"id": f"s_{i}", "source": f"Text {i}"} for i in range(50)]
    batches = batch_segments(segments, max_segments=40, max_chars=6000)
    assert len(batches) == 2
    assert len(batches[0]) == 40
    assert len(batches[1]) == 10


def test_batch_segments_oversized_single_segment() -> None:
    """Single segment exceeding max_chars forms an isolated batch without text splitting."""
    seg1 = {"id": "s_1", "source": "Ngắn"}
    oversized = {"id": "s_2", "source": "A" * 7000}  # > 6000 chars
    seg3 = {"id": "s_3", "source": "Ngắn tiếp"}

    batches = batch_segments([seg1, oversized, seg3], max_segments=40, max_chars=6000)
    assert len(batches) == 3
    assert batches[0] == [seg1]
    assert batches[1] == [oversized]
    assert batches[2] == [seg3]


def test_resolve_glossary_locked_priority() -> None:
    """Locked glossary entries take absolute priority over unlocked entries."""
    glossary = [
        {"source": "마왕", "target": "Ma vương thông thường", "locked": False},
        {"source": "마왕", "target": "Ma Vương Tối Thượng", "locked": True},
        {"source": "용사", "target": "Dũng Giả", "locked": False},
    ]

    locked, unlocked = resolve_glossary(glossary, batch_text="마왕이 나타났다.")
    assert len(locked) == 1
    assert locked[0].source == "마왕"
    assert locked[0].target == "Ma Vương Tối Thượng"
    assert locked[0].locked is True

    # '용사' was filtered out because it is not in batch_text
    assert len(unlocked) == 0


def test_translation_memory_glossary_version_compatibility() -> None:
    """Translation memory reuses exact source only when glossary version matches."""
    mem = TranslationMemory(book_id="book_1", glossary_version="v1")
    mem.set("테스트", "Thử nghiệm", glossary_version="v1")

    # Match version
    assert mem.get("테스트", glossary_version="v1") == "Thử nghiệm"

    # Incompatible version -> returns None
    assert mem.get("테스트", glossary_version="v2") is None


def test_check_translation_heuristics() -> None:
    """Heuristic reports flags for severe truncation and excessive expansion."""
    # Truncation: 50 Korean chars -> 2 Vietnamese chars
    issues = check_translation_heuristics("이것은 매우 긴 한국어 문장으로 주인공이 모험을 떠나는 내용입니다.", "Đi")
    assert any("truncation" in iss for iss in issues)

    # Expansion: 5 Korean chars -> 50 Vietnamese chars
    issues = check_translation_heuristics("안녕하세요", "Xin chào tất cả mọi người trên thế giới này, hôm nay tôi rất vui được gặp các bạn ở đây.")
    assert any("expansion" in iss for iss in issues)


@pytest.mark.asyncio
async def test_client_close_ownership() -> None:
    """Provider only closes client if client was owned, not when injected."""
    mock_client = httpx.AsyncClient()
    provider_injected = GeminiProvider(api_key="test-key", client=mock_client)
    assert provider_injected._client_owned is False
    await provider_injected.close()
    assert not mock_client.is_closed
    await mock_client.aclose()

    provider_owned = GeminiProvider(api_key="test-key", client=None)
    assert provider_owned._client_owned is True
    await provider_owned.close()
    assert provider_owned._client.is_closed


@pytest.mark.asyncio
async def test_gemini_provider_review_context_payload_assertion() -> None:
    """Verify that build_translation_context (locked glossary, story bible, summaries, neighbors)
    is properly incorporated into the reviewer HTTP payload with gemini-3.5-flash-lite model,
    making exactly ONE call without injecting translate directives."""
    recorded_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        res_payload = {
            "issues": [
                {"id": "seg_1", "type": "style", "description": "Lưu ý hành văn"}
            ],
            "corrections": [
                {"id": "seg_1", "corrected": "Bản dịch đã hiệu đính"}
            ],
        }
        return httpx.Response(200, json=make_gemini_response(res_payload))

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client_http:
        provider = GeminiProvider(api_key="test-api-key", client=client_http, model="gemini-3.5-flash-lite")

        # Prepare context using build_translation_context
        segments = [
            {"id": "seg_1", "source": "문장 1", "translation": "Bản dịch 1"},
        ]
        ctx = build_translation_context(
            segments,
            story_bible="Bối cảnh đại chiến thiên hà",
            recent_summaries=["Tóm tắt tập 1", "Tóm tắt tập 2"],
            neighboring_before=[{"id": "pre_1", "source": "Ngữ cảnh trước"}],
            neighboring_after=[{"id": "post_1", "source": "Ngữ cảnh sau"}],
        )

        review_input = [
            {"id": "seg_1", "source": "문장 1", "translation": "Bản dịch 1"}
        ]
        res = await provider.review(review_input, context=ctx, model="gemini-3.5-flash-lite")
        assert res["corrections"]["seg_1"] == "Bản dịch đã hiệu đính"
        assert len(res["issues"]) == 1

        # Assertions on the fake HTTP request:
        # 1. Exactly ONE call
        assert len(recorded_requests) == 1
        req = recorded_requests[0]

        # 2. Explicit gemini-3.5-flash-lite URL
        assert "/models/gemini-3.5-flash-lite:generateContent" in req.url.path

        # 3. Payload inspection: system instruction emphasizes natural consistency without locked glossary
        req_json = json.loads(req.content)
        sys_inst = req_json.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
        assert "Reviewer" in sys_inst or "thẩm định" in sys_inst
        assert "LOCKED GLOSSARY" not in sys_inst
        assert "thuật ngữ đã khóa" not in sys_inst.lower()

        # Prompt content includes context: story bible, recent summaries, neighbors (no glossary)
        user_prompt = req_json["contents"][0]["parts"][0]["text"]
        assert "Bối cảnh đại chiến thiên hà" in user_prompt
        assert "Tóm tắt tập 1" in user_prompt
        assert "Tóm tắt tập 2" in user_prompt
        assert "문장 -> Thuật ngữ khóa" not in user_prompt
        assert "TỪ ĐIỂN" not in user_prompt
        assert "GLOSSARY" not in user_prompt
        assert "Ngữ cảnh trước" in user_prompt
        assert "Ngữ cảnh sau" in user_prompt
        assert "[seg_1]" in user_prompt

        # 4. No translate directive injected in prompt or schema
        assert "Hãy dịch toàn bộ" not in user_prompt
        assert "TRANSLATE BATCH" not in user_prompt
        resp_schema = req_json.get("generationConfig", {}).get("responseJsonSchema", {})
        assert "issues" in resp_schema.get("properties", {})
        assert "corrections" in resp_schema.get("properties", {})
        assert "translations" not in resp_schema.get("properties", {})


@pytest.mark.asyncio
async def test_translate_schema_compatible_baseline_and_directive_prompt() -> None:
    """Verify translate request uses compatible baseline schema while enforcing exact IDs and count in prompt."""
    segments = [
        {"id": "seg_001", "source": "첫 번째 문장."},
        {"id": "seg_002", "source": "두 번째 문장."},
        {"id": "seg_003", "source": "세 번째 문장."},
    ]

    expected_output = {
        "translations": [
            {"id": "seg_001", "translated": "Câu thứ nhất."},
            {"id": "seg_002", "translated": "Câu thứ hai."},
            {"id": "seg_003", "translated": "Câu thứ ba."},
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        payload = json.loads(request.content)
        schema = payload["generationConfig"]["responseJsonSchema"]
        trans_prop = schema["properties"]["translations"]

        # Compatible baseline schema (no unportable minItems/maxItems or enum)
        assert schema["type"] == "object"
        assert trans_prop["type"] == "array"
        assert trans_prop["items"]["type"] == "object"
        assert trans_prop["items"]["properties"]["id"]["type"] == "string"
        assert trans_prop["items"]["properties"]["translated"]["type"] == "string"
        assert "minItems" not in trans_prop
        assert "maxItems" not in trans_prop
        assert "enum" not in trans_prop["items"]["properties"]["id"]

        # User prompt contains required IDs, count, and strict directives
        user_prompt = payload["contents"][0]["parts"][0]["text"]
        assert "3 ID" in user_prompt
        assert "'seg_001', 'seg_002', 'seg_003'" in user_prompt
        assert "BẢO TOÀN CHÍNH XÁC ID" in user_prompt
        assert "KHÔNG GỘP PHÂN ĐOẠN NGUỒN" in user_prompt
        assert "BỎ QUA ID NGỮ CẢNH" in user_prompt

        return httpx.Response(200, json=make_gemini_response(expected_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert len(recorded_requests) == 1
    assert result == {
        "seg_001": "Câu thứ nhất.",
        "seg_002": "Câu thứ hai.",
        "seg_003": "Câu thứ ba.",
    }


@pytest.mark.asyncio
async def test_translate_bounded_targeted_recovery_recovers_missing_id() -> None:
    """When initial response misses an ID, targeted recovery requests only missing ID and merges output."""
    segments = [
        {"id": "seg_101", "source": "문장 하나"},
        {"id": "seg_102", "source": "문장 둘"},
        {"id": "seg_103", "source": "문장 셋"},
    ]

    # Initial call returns only seg_101 and seg_102 (missing seg_103)
    initial_output = {
        "translations": [
            {"id": "seg_101", "translated": "Câu một"},
            {"id": "seg_102", "translated": "Câu hai"},
        ]
    }

    # Targeted recovery call returns seg_103
    recovery_output = {
        "translations": [
            {"id": "seg_103", "translated": "Câu ba"},
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        payload = json.loads(request.content)
        schema = payload["generationConfig"]["responseJsonSchema"]
        trans_prop = schema["properties"]["translations"]

        # Compatible baseline schema for both initial and recovery
        assert "minItems" not in trans_prop
        assert "maxItems" not in trans_prop
        assert "enum" not in trans_prop["items"]["properties"]["id"]

        user_prompt = payload["contents"][0]["parts"][0]["text"]
        if len(recorded_requests) == 1:
            # First request: all 3 IDs in prompt
            assert "3 ID" in user_prompt
            assert "'seg_101', 'seg_102', 'seg_103'" in user_prompt
            return httpx.Response(200, json=make_gemini_response(initial_output))
        elif len(recorded_requests) == 2:
            # Second request: ONLY missing seg_103 narrowed in prompt
            assert "seg_103" in user_prompt
            assert "TARGETED MISSING SEGMENTS" in user_prompt
            assert "1 ID" in user_prompt
            return httpx.Response(200, json=make_gemini_response(recovery_output))
        return httpx.Response(500, text="Unexpected extra request")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert len(recorded_requests) == 2
    assert result == {
        "seg_101": "Câu một",
        "seg_102": "Câu hai",
        "seg_103": "Câu ba",
    }


@pytest.mark.asyncio
async def test_translate_bounded_targeted_recovery_exhaustion_raises() -> None:
    """When recovery repeatedly fails to return missing ID, ProviderValidationError is raised after bounded retries."""
    segments = [
        {"id": "seg_201", "source": "문장 1"},
        {"id": "seg_202", "source": "문장 2"},
    ]

    # Handler always omits seg_202
    incomplete_output = {
        "translations": [
            {"id": "seg_201", "translated": "Câu 1"},
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json=make_gemini_response(incomplete_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)

        assert "thiếu các phân đoạn ID" in str(exc_info.value)
        assert "seg_202" in str(exc_info.value)

    # 1 initial request + max 2 recovery attempts = 3 requests total (bounded, no endless loop)
    assert len(recorded_requests) == 3


@pytest.mark.asyncio
async def test_translate_recovery_rejects_unknown_and_malformed_output() -> None:
    """Recovery rejects unknown IDs or empty translations and preserves already validated entries."""
    segments = [
        {"id": "seg_301", "source": "문장 1"},
        {"id": "seg_302", "source": "문장 2"},
    ]

    initial_output = {
        "translations": [
            {"id": "seg_301", "translated": "Câu 1"},
        ]
    }

    # Recovery attempt 1 returns unknown extra ID
    unknown_id_output = {
        "translations": [
            {"id": "seg_unknown", "translated": "Câu lạ"},
        ]
    }

    # Recovery attempt 2 returns empty translation
    empty_output = {
        "translations": [
            {"id": "seg_302", "translated": "   "},
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if len(recorded_requests) == 1:
            return httpx.Response(200, json=make_gemini_response(initial_output))
        elif len(recorded_requests) == 2:
            return httpx.Response(200, json=make_gemini_response(unknown_id_output))
        elif len(recorded_requests) == 3:
            return httpx.Response(200, json=make_gemini_response(empty_output))
        return httpx.Response(500, text="Unexpected")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)

        assert isinstance(exc_info.value, ProviderEmptyContentError)
        assert "bị rỗng sau các lần thử lại" in str(exc_info.value)
        assert "seg_302" in str(exc_info.value)

    assert len(recorded_requests) == 3


def test_schema_and_directive_helpers_unit() -> None:
    """Direct unit tests for schema generation and prompt directive helpers."""
    schema = _build_translation_schema(["id_a", "id_b", "id_a"])
    assert schema["type"] == "object"
    trans_p = schema["properties"]["translations"]
    assert trans_p["type"] == "array"
    assert trans_p["items"]["properties"]["id"]["type"] == "string"
    assert trans_p["items"]["properties"]["translated"]["type"] == "string"
    assert "minItems" not in trans_p
    assert "maxItems" not in trans_p
    assert "enum" not in trans_p["items"]["properties"]["id"]

    directive = _format_translation_directive(["id_1", "id_2"])
    assert "2 ID" in directive
    assert "'id_1', 'id_2'" in directive
    assert "KHÔNG GỘP PHÂN ĐOẠN NGUỒN" in directive
    assert "BỎ QUA ID NGỮ CẢNH" in directive
    assert "copy IDs exactly without punctuation" in directive


def test_extract_translations_batch_normal_exact_ids() -> None:
    """Normal exact IDs are extracted without modification and with correct mapping."""
    expected_ids = ["seg_1", "seg_2"]
    source_map = {"seg_1": "원문 1", "seg_2": "원문 2"}
    payload = {
        "translations": [
            {"id": "seg_1", "translated": "Dịch 1"},
            {"id": "seg_2", "translated": "Dịch 2"},
        ]
    }
    result, missing = _extract_translations_batch(payload, expected_ids, source_map)
    assert result == {"seg_1": "Dịch 1", "seg_2": "Dịch 2"}
    assert missing == []


def test_extract_translations_batch_trailing_colon_corrected_without_changed_text() -> None:
    """Single trailing ASCII colon on segment ID is normalized to expected ID without altering translation text."""
    expected_ids = ["84b5c5f0623d45019d0221f89f1fdab6"]
    source_map = {"84b5c5f0623d45019d0221f89f1fdab6": "원문 내용"}
    payload = {
        "translations": [
            {"id": "84b5c5f0623d45019d0221f89f1fdab6:", "translated": "Bản dịch giữ nguyên không đổi."},
        ]
    }
    result, missing = _extract_translations_batch(payload, expected_ids, source_map)
    assert result == {"84b5c5f0623d45019d0221f89f1fdab6": "Bản dịch giữ nguyên không đổi."}
    assert missing == []


def test_extract_translations_batch_surrounding_whitespace() -> None:
    """Surrounding whitespace around exact IDs and colon-decorated IDs is trimmed properly."""
    expected_ids = ["seg_ws_1", "seg_ws_2"]
    source_map = {"seg_ws_1": "원문 1", "seg_ws_2": "원문 2"}
    payload = {
        "translations": [
            {"id": "  seg_ws_1  ", "translated": "Dịch 1"},
            {"id": " \t seg_ws_2:  \n", "translated": "Dịch 2"},
        ]
    }
    result, missing = _extract_translations_batch(payload, expected_ids, source_map)
    assert result == {"seg_ws_1": "Dịch 1", "seg_ws_2": "Dịch 2"}
    assert missing == []


def test_extract_translations_batch_truly_unknown_id_still_rejected() -> None:
    """Truly unknown IDs, with or without trailing colon, are rejected as extra IDs."""
    expected_ids = ["seg_known"]
    source_map = {"seg_known": "원문"}
    payload = {
        "translations": [
            {"id": "seg_known", "translated": "Dịch chuẩn"},
            {"id": "unknown_random_id:", "translated": "Dịch lạ"},
        ]
    }
    with pytest.raises(ProviderValidationError) as exc_info:
        _extract_translations_batch(payload, expected_ids, source_map)
    assert "chứa các ID thừa không có trong yêu cầu" in str(exc_info.value)
    assert "unknown_random_id:" in str(exc_info.value)


def test_extract_translations_batch_two_colons_rejected() -> None:
    """ID with two colons is not stripped to expected ID and is rejected as extra ID."""
    expected_ids = ["seg_1"]
    source_map = {"seg_1": "원문"}
    payload = {
        "translations": [
            {"id": "seg_1::", "translated": "Dịch"},
        ]
    }
    with pytest.raises(ProviderValidationError) as exc_info:
        _extract_translations_batch(payload, expected_ids, source_map)
    assert "chứa các ID thừa không có trong yêu cầu" in str(exc_info.value)
    assert "seg_1::" in str(exc_info.value)


def test_extract_translations_batch_canonical_plus_decorated_duplicate_rejected() -> None:
    """Duplicate containing both canonical ID and colon-decorated ID is rejected even if translation identical."""
    expected_ids = ["seg_1"]
    source_map = {"seg_1": "원문"}
    payload = {
        "translations": [
            {"id": "seg_1", "translated": "Bản dịch giống nhau"},
            {"id": "seg_1:", "translated": "Bản dịch giống nhau"},
        ]
    }
    with pytest.raises(ProviderValidationError) as exc_info:
        _extract_translations_batch(payload, expected_ids, source_map)
    assert "trùng lặp" in str(exc_info.value)
    assert "seg_1" in str(exc_info.value)


def test_extract_translations_batch_expected_id_ending_colon_precedence() -> None:
    """Expected ID itself ending with a colon takes exact precedence over colon stripping."""
    expected_ids = ["custom_id:", "custom_id"]
    source_map = {"custom_id:": "원문 콜론", "custom_id": "원문 기본"}
    payload = {
        "translations": [
            {"id": "custom_id:", "translated": "Dịch cho ID có sẵn dấu hai chấm"},
            {"id": "custom_id", "translated": "Dịch cho ID cơ bản"},
        ]
    }
    result, missing = _extract_translations_batch(payload, expected_ids, source_map)
    assert result == {
        "custom_id:": "Dịch cho ID có sẵn dấu hai chấm",
        "custom_id": "Dịch cho ID cơ bản",
    }
    assert missing == []


def test_normalize_translation_id_unit_precedence() -> None:
    """Direct test of _normalize_translation_id confirming exact precedence and single colon rule."""
    expected_set = {"colon_id:", "base_id"}
    # Exact match for ID ending in colon
    assert _normalize_translation_id("colon_id:", expected_set) == "colon_id:"
    # Stripping single colon yields base_id
    assert _normalize_translation_id("base_id:", expected_set) == "base_id"
    # Stripping surrounding whitespace
    assert _normalize_translation_id("  base_id:  ", expected_set) == "base_id"
    # Two colons not normalized
    assert _normalize_translation_id("base_id::", expected_set) == "base_id::"
    # Unknown ID with colon not in expected set
    assert _normalize_translation_id("unknown:", expected_set) == "unknown:"


def test_extract_translations_batch_empty_translation_rejected_after_canonicalization() -> None:
    """Empty translation string for non-empty source is still rejected after colon canonicalization."""
    expected_ids = ["84b5c5f0623d45019d0221f89f1fdab6"]
    source_map = {"84b5c5f0623d45019d0221f89f1fdab6": "원문 소스"}
    payload = {
        "translations": [
            {"id": "84b5c5f0623d45019d0221f89f1fdab6:", "translated": "   "},
        ]
    }
    with pytest.raises(ProviderValidationError) as exc_info:
        _extract_translations_batch(payload, expected_ids, source_map)
    assert "có bản dịch bị rỗng" in str(exc_info.value)
    assert "84b5c5f0623d45019d0221f89f1fdab6" in str(exc_info.value)


@pytest.mark.asyncio
async def test_translate_colon_id_normalized_end_to_end() -> None:
    """End-to-end translate succeeds when model returns ID with trailing colon."""
    segments = [{"id": "84b5c5f0623d45019d0221f89f1fdab6", "source": "한국어 텍스트"}]
    mock_payload = {
        "translations": [
            {"id": "84b5c5f0623d45019d0221f89f1fdab6:", "translated": "Văn bản tiếng Việt hoàn chỉnh"}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(mock_payload))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert result == {"84b5c5f0623d45019d0221f89f1fdab6": "Văn bản tiếng Việt hoàn chỉnh"}


@pytest.mark.asyncio
async def test_translate_recovery_normalizes_colon_id() -> None:
    """Targeted recovery accepting colon-appended ID successfully merges result into final output."""
    segments = [
        {"id": "seg_rec_1", "source": "문장 1"},
        {"id": "seg_rec_2", "source": "문장 2"},
    ]
    initial_output = {
        "translations": [
            {"id": "seg_rec_1", "translated": "Câu 1"},
        ]
    }
    recovery_output = {
        "translations": [
            {"id": "seg_rec_2:", "translated": "Câu 2"},
        ]
    }
    req_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal req_count
        req_count += 1
        if req_count == 1:
            return httpx.Response(200, json=make_gemini_response(initial_output))
        elif req_count == 2:
            req_json = json.loads(request.content)
            user_prompt = req_json["contents"][0]["parts"][0]["text"]
            assert "copy IDs exactly without punctuation" in user_prompt
            return httpx.Response(200, json=make_gemini_response(recovery_output))
        return httpx.Response(500, text="Unexpected")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert result == {
        "seg_rec_1": "Câu 1",
        "seg_rec_2": "Câu 2",
    }
    assert req_count == 2


# =============================================================================
# Malformed ID and Bounded Authorized Re-request Tests
# =============================================================================

@pytest.mark.asyncio
async def test_translate_literal_user_malformed_id_retries_and_succeeds() -> None:
    """Literal user malformed ID (delimiter+source copied into id field) triggers bounded retry,
    succeeding on second try without wrong mapping."""
    segments = [
        {
            "id": "84b5c5f0623d45019d0221f89f1fdab6",
            "source": "[실시간으로 수명이 늘어나는 빅재미까지 선물하는 도파민 능력이야.]",
        }
    ]

    malformed_id_output = {
        "translations": [
            {
                "id": "84b5c5f0623d45019d0221f89f1fdab6]: [실시간으로 수명이 늘어나는 빅재미까지 선물하는 도파민 능력이야.",
                "translated": "Khả năng dopamine mang lại niềm vui lớn.",
            }
        ]
    }

    correct_output = {
        "translations": [
            {
                "id": "84b5c5f0623d45019d0221f89f1fdab6",
                "translated": "Đó là năng lực dopamine mang lại niềm vui lớn kéo dài tuổi thọ theo thời gian thực.",
            }
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if len(recorded_requests) == 1:
            return httpx.Response(200, json=make_gemini_response(malformed_id_output))
        elif len(recorded_requests) == 2:
            req_json = json.loads(request.content)
            user_prompt = req_json["contents"][0]["parts"][0]["text"]
            assert "COPY ONLY ID PROPERTY" in user_prompt
            assert '"id": "84b5c5f0623d45019d0221f89f1fdab6"' in user_prompt
            return httpx.Response(200, json=make_gemini_response(correct_output))
        return httpx.Response(500, text="Unexpected extra request")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert len(recorded_requests) == 2
    assert result == {
        "84b5c5f0623d45019d0221f89f1fdab6": "Đó là năng lực dopamine mang lại niềm vui lớn kéo dài tuổi thọ theo thời gian thực."
    }


@pytest.mark.asyncio
async def test_translate_repeated_bad_result_fails_fixed_total_two_requests() -> None:
    """Repeated malformed ID on both initial request and retry fails with fixed total 2 requests."""
    segments = [{"id": "84b5c5f0623d45019d0221f89f1fdab6", "source": "문장"}]

    bad_output = {
        "translations": [
            {
                "id": "84b5c5f0623d45019d0221f89f1fdab6]: [문장",
                "translated": "Câu văn",
            }
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert isinstance(exc_info.value, ProviderIdValidationError)
        assert "chứa các ID thừa" in str(exc_info.value)

    assert len(recorded_requests) == 2


@pytest.mark.asyncio
async def test_translate_unknown_real_id_remains_invalid() -> None:
    """Model returning completely unrequested ID remains invalid after authorized retry (total 2 requests)."""
    segments = [{"id": "seg_valid_01", "source": "원본 문장"}]

    bad_output = {
        "translations": [
            {"id": "seg_unexpected_unknown", "translated": "Dịch không yêu cầu"}
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "chứa các ID thừa không có trong yêu cầu" in str(exc_info.value)
        assert "seg_unexpected_unknown" in str(exc_info.value)

    assert len(recorded_requests) == 2


@pytest.mark.asyncio
async def test_translate_duplicate_after_colon_normalization_rejects_with_retry() -> None:
    """Duplicate resulting from colon normalization (e.g. seg_1 and seg_1:) is rejected with retry (total 2 requests)."""
    segments = [{"id": "seg_1", "source": "문장 1"}]

    bad_output = {
        "translations": [
            {"id": "seg_1", "translated": "Bản dịch 1"},
            {"id": "seg_1:", "translated": "Bản dịch 2"},
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "trùng lặp" in str(exc_info.value)
        assert "seg_1" in str(exc_info.value)

    assert len(recorded_requests) == 2


@pytest.mark.asyncio
async def test_translate_json_escaping_quote_bracket_newline_preserves_separation() -> None:
    """JSON escaping of quotes, brackets, and newlines in source text preserves id/source separation."""
    segments = [
        {
            "id": "seg_special_chars",
            "source": '["시스템 메시지"]: "경고!"\n[상태창] <HP: 100/100>',
        }
    ]

    expected_output = {
        "translations": [
            {
                "id": "seg_special_chars",
                "translated": '["Thông báo hệ thống"]: "Cảnh báo!"\n[Bảng trạng thái] <HP: 100/100>',
            }
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        req_json = json.loads(request.content)
        user_prompt = req_json["contents"][0]["parts"][0]["text"]
        # Explicit JSON targets block
        assert '"id": "seg_special_chars"' in user_prompt
        assert '"source": "[\\"시스템 메시지\\"]: \\"경고!\\"\\n[상태창] <HP: 100/100>"' in user_prompt
        return httpx.Response(200, json=make_gemini_response(expected_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert len(recorded_requests) == 1
    assert result == {
        "seg_special_chars": '["Thông báo hệ thống"]: "Cảnh báo!"\n[Bảng trạng thái] <HP: 100/100>'
    }


@pytest.mark.asyncio
async def test_translate_retry_prompt_model_preserved() -> None:
    """Retry prompt preserves the explicitly selected model parameter on both calls."""
    segments = [{"id": "seg_m_1", "source": "문장"}]

    bad_output = {
        "translations": [
            {"id": "seg_m_1]: [문장", "translated": "Dịch sai id"}
        ]
    }
    good_output = {
        "translations": [
            {"id": "seg_m_1", "translated": "Dịch chuẩn"}
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if len(recorded_requests) == 1:
            return httpx.Response(200, json=make_gemini_response(bad_output))
        return httpx.Response(200, json=make_gemini_response(good_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments, model="gemini-3.8-flash")

    assert len(recorded_requests) == 2
    assert "/models/gemini-3.8-flash:generateContent" in recorded_requests[0].url.path
    assert "/models/gemini-3.8-flash:generateContent" in recorded_requests[1].url.path
    assert result == {"seg_m_1": "Dịch chuẩn"}


@pytest.mark.asyncio
async def test_translate_malformed_payload_and_empty_string_bounded_retry_and_recovery() -> None:
    """Non-JSON payload triggers bounded retry (calls == 2); empty linguistic translation enters targeted recovery (calls == 3)."""
    segments = [{"id": "seg_e_1", "source": "문장"}]

    # Case 1: Malformed non-JSON triggers bounded retry (calls == 2) then fails with ProviderValidationError
    calls_case1 = 0

    def handler_case1(request: httpx.Request) -> httpx.Response:
        nonlocal calls_case1
        calls_case1 += 1
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "Not a JSON"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    transport1 = httpx.MockTransport(handler_case1)
    async with httpx.AsyncClient(transport=transport1) as client1:
        provider1 = GeminiProvider(api_key="test-key", client=client1)
        with pytest.raises(ProviderValidationError):
            await provider1.translate(segments)
    assert calls_case1 == 2

    # Case 2: Empty translation for non-empty source enters targeted recovery (initial + 2 recovery = 3 calls)
    calls_case2 = 0
    empty_trans_output = {"translations": [{"id": "seg_e_1", "translated": "   "}]}

    def handler_case2(request: httpx.Request) -> httpx.Response:
        nonlocal calls_case2
        calls_case2 += 1
        return httpx.Response(200, json=make_gemini_response(empty_trans_output))

    transport2 = httpx.MockTransport(handler_case2)
    async with httpx.AsyncClient(transport=transport2) as client2:
        provider2 = GeminiProvider(api_key="test-key", client=client2)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider2.translate(segments)
        assert "bị rỗng" in str(exc_info.value)
    assert calls_case2 == 3


def test_build_translation_context_json_targets_and_readonly_neighbors() -> None:
    """build_translation_context formats targets as explicit JSON and neighbors as read-only bullets."""
    segments = [
        {"id": "t_01", "source": "첫 번째 문장"},
        {"id": "t_02", "source": "두 번째 [대괄호] 문장"},
    ]
    nb_before = [{"id": "pre_01", "source": "이전 문장"}]
    nb_after = [{"id": "post_01", "source": "이후 문장"}]

    ctx = build_translation_context(
        segments=segments,
        story_bible="Thế giới giả tưởng",
        recent_summaries=["Tóm tắt gần nhất"],
        neighboring_before=nb_before,
        neighboring_after=nb_after,
    )

    user_prompt = ctx["user_prompt"]
    # 1. Targets formatted as JSON array
    assert '"id": "t_01"' in user_prompt
    assert '"source": "첫 번째 문장"' in user_prompt
    assert '"id": "t_02"' in user_prompt
    assert '"source": "두 번째 [대괄호] 문장"' in user_prompt

    # 2. Neighbors have NO [id]: format
    assert "[pre_01]:" not in user_prompt
    assert "[post_01]:" not in user_prompt
    assert "- 이전 문장" in user_prompt
    assert "- 이후 문장" in user_prompt

    # 3. Prompt version included
    assert ctx.get("prompt_version") == "v2_json_targets"


# =============================================================================
# Nonlinguistic Symbol-Only Source Preservation Tests
# =============================================================================

def test_is_symbol_only_classifier() -> None:
    """Safe explicit classifier identifies symbol-only sources and strictly rejects letters, numbers, control chars."""
    # Symbols and punctuation (must be True)
    assert is_symbol_only_source("*") is True
    assert is_symbol_only_source("****") is True
    assert is_symbol_only_source("  *  \n") is True
    assert is_symbol_only_source("* * *") is True
    assert is_symbol_only_source("―") is True
    assert is_symbol_only_source("…") is True
    assert is_symbol_only_source("※") is True
    assert is_symbol_only_source("◆") is True
    assert is_symbol_only_source("◇") is True
    assert is_symbol_only_source("★") is True
    assert is_symbol_only_source("☆") is True
    assert is_symbol_only_source("•") is True
    assert is_symbol_only_source("·") is True
    assert is_symbol_only_source("~") is True
    assert is_symbol_only_source("---") is True
    assert is_symbol_only_source("===") is True
    assert is_symbol_only_source("!?") is True

    # Empty or whitespace only (must be False)
    assert is_symbol_only_source("") is False
    assert is_symbol_only_source("   ") is False
    assert is_symbol_only_source("\n\t") is False

    # Mixed symbol + Korean (must be False)
    assert is_symbol_only_source("* 안녕하세요 *") is False
    assert is_symbol_only_source("*가*") is False
    assert is_symbol_only_source("마왕*") is False

    # Numbers and digits (must be False)
    assert is_symbol_only_source("123") is False
    assert is_symbol_only_source("1.") is False
    assert is_symbol_only_source("* 1 *") is False
    assert is_symbol_only_source("①") is False

    # Latin / Vietnamese language (must be False)
    assert is_symbol_only_source("Hello") is False
    assert is_symbol_only_source("Xin chào") is False
    assert is_symbol_only_source("* a *") is False

    # Control characters (must be False)
    assert is_symbol_only_source("\x00") is False
    assert is_symbol_only_source("*\x00*") is False
    assert is_symbol_only_source("*\x1b*") is False
    assert is_symbol_only_source("*\x07*") is False


@pytest.mark.asyncio
async def test_translate_actual_star_empty_returns_star() -> None:
    """Actual segment with source '*' (scene break) returns '*' when Gemini returns empty translation."""
    segments = [{"id": "b0c10a60e0134a69932ba198d15a6235", "source": "*"}]
    empty_output = {
        "translations": [
            {"id": "b0c10a60e0134a69932ba198d15a6235", "translated": ""}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(empty_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert result == {"b0c10a60e0134a69932ba198d15a6235": "*"}


@pytest.mark.asyncio
async def test_translate_omitted_star_combined_with_valid_language_no_recovery() -> None:
    """Omitted '*' combined with valid language completes in single call without unnecessary recovery calls."""
    segments = [
        {"id": "b0c10a60e0134a69932ba198d15a6235", "source": "*"},
        {"id": "c1_s0002", "source": "다음 날 아침이 밝았다."},
    ]

    # Model omits b0c10a60e0134a69932ba198d15a6235 completely
    model_output = {
        "translations": [
            {"id": "c1_s0002", "translated": "Sáng hôm sau trời đã sáng."},
        ]
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=make_gemini_response(model_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert call_count == 1
    assert result == {
        "b0c10a60e0134a69932ba198d15a6235": "*",
        "c1_s0002": "Sáng hôm sau trời đã sáng.",
    }


@pytest.mark.asyncio
async def test_translate_mixed_symbol_and_korean_empty_rejects() -> None:
    """Mixed symbol + Korean source with empty translation must be rejected."""
    segments = [{"id": "seg_mixed", "source": "* 안녕하세요 *"}]
    bad_output = {
        "translations": [
            {"id": "seg_mixed", "translated": "   "}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "bị rỗng" in str(exc_info.value)


@pytest.mark.asyncio
async def test_translate_digits_empty_rejects() -> None:
    """Numeric digit source with empty translation must be rejected."""
    segments = [{"id": "seg_digits", "source": "123"}]
    bad_output = {
        "translations": [
            {"id": "seg_digits", "translated": ""}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments)
        assert "bị rỗng" in str(exc_info.value)


@pytest.mark.asyncio
async def test_translate_unicode_punctuation_preserves() -> None:
    """Unicode punctuation separators and scene breaks are preserved when model returns empty or omits."""
    segments = [
        {"id": "s_dash", "source": "―"},
        {"id": "s_ellipsis", "source": "…"},
        {"id": "s_stars", "source": "****"},
        {"id": "s_ref", "source": "※"},
        {"id": "s_dia", "source": "◆"},
    ]
    # Model returns empty for some, omits others
    model_output = {
        "translations": [
            {"id": "s_dash", "translated": ""},
            {"id": "s_ellipsis", "translated": "   "},
            {"id": "s_stars", "translated": ""},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(model_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert result == {
        "s_dash": "―",
        "s_ellipsis": "…",
        "s_stars": "****",
        "s_ref": "※",
        "s_dia": "◆",
    }


@pytest.mark.asyncio
async def test_translate_symbol_duplicate_and_unknown_remain_invalid() -> None:
    """Duplicate symbol IDs and unknown IDs remain strictly invalid after bounded retry."""
    # Duplicate symbol IDs
    segments_dup = [{"id": "seg_star", "source": "*"}]
    bad_dup_output = {
        "translations": [
            {"id": "seg_star", "translated": "*"},
            {"id": "seg_star", "translated": "*"},
        ]
    }

    def handler_dup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_dup_output))

    transport_dup = httpx.MockTransport(handler_dup)
    async with httpx.AsyncClient(transport=transport_dup) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments_dup)
        assert isinstance(exc_info.value, ProviderIdValidationError)
        assert "trùng lặp" in str(exc_info.value)

    # Unknown ID
    segments_unk = [{"id": "seg_star", "source": "*"}]
    bad_unk_output = {
        "translations": [
            {"id": "seg_star", "translated": "*"},
            {"id": "seg_unknown", "translated": "*"},
        ]
    }

    def handler_unk(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_unk_output))

    transport_unk = httpx.MockTransport(handler_unk)
    async with httpx.AsyncClient(transport=transport_unk) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.translate(segments_unk)
        assert isinstance(exc_info.value, ProviderIdValidationError)
        assert "chứa các ID thừa" in str(exc_info.value)


@pytest.mark.asyncio
async def test_reviewer_empty_correction_on_star_cannot_erase_it() -> None:
    """Reviewer empty correction on symbol-only source '*' restores source and does not erase it."""
    segments = [{"id": "seg_star", "source": "*", "translation": "*"}]
    review_output = {
        "issues": [],
        "corrections": [
            {"id": "seg_star", "corrected": "   "}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(review_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.review(segments)

    assert result["corrections"] == {"seg_star": "*"}


@pytest.mark.asyncio
async def test_reviewer_empty_correction_on_normal_text_still_rejects() -> None:
    """Reviewer empty correction on ordinary language text is strictly rejected."""
    segments = [{"id": "seg_normal", "source": "안녕하세요", "translation": "Xin chào"}]
    bad_review_output = {
        "issues": [],
        "corrections": [
            {"id": "seg_normal", "corrected": "   "}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_gemini_response(bad_review_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderValidationError) as exc_info:
            await provider.review(segments)
        assert "bị rỗng" in str(exc_info.value)


# =============================================================================
# Comprehensive Translation Robustness Audit Tests
# =============================================================================

@pytest.mark.asyncio
async def test_translate_mixed_valid_and_empty_targeted_only_recovery() -> None:
    """Mixed valid, empty, and omitted translations recover ONLY unresolved IDs; valid are kept."""
    segments = [
        {"id": "seg_valid", "source": "첫 번째 문장"},
        {"id": "seg_empty", "source": "두 번째 문장"},
        {"id": "seg_omitted", "source": "세 번째 문장"},
    ]

    initial_output = {
        "translations": [
            {"id": "seg_valid", "translated": "Câu thứ nhất"},
            {"id": "seg_empty", "translated": ""},
        ]
    }

    recovery_output = {
        "translations": [
            {"id": "seg_empty", "translated": "Câu thứ hai"},
            {"id": "seg_omitted", "translated": "Câu thứ ba"},
        ]
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if len(recorded_requests) == 1:
            return httpx.Response(200, json=make_gemini_response(initial_output))
        elif len(recorded_requests) == 2:
            req_body = json.loads(request.content)
            user_prompt = req_body["contents"][0]["parts"][0]["text"]
            # Assert targeted recovery prompt contains ONLY unresolved IDs and NOT seg_valid
            assert '"id": "seg_empty"' in user_prompt
            assert '"id": "seg_omitted"' in user_prompt
            assert '"id": "seg_valid"' not in user_prompt
            return httpx.Response(200, json=make_gemini_response(recovery_output))
        return httpx.Response(500, text="Unexpected extra call")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert len(recorded_requests) == 2
    assert result == {
        "seg_valid": "Câu thứ nhất",
        "seg_empty": "Câu thứ hai",
        "seg_omitted": "Câu thứ ba",
    }


@pytest.mark.asyncio
async def test_translate_malformed_response_bounded_retry_succeeds() -> None:
    """Malformed non-JSON response on initial call succeeds via bounded re-request (total 2 calls)."""
    segments = [{"id": "seg_m_1", "source": "문장 1"}]
    good_output = {
        "translations": [{"id": "seg_m_1", "translated": "Bản dịch câu 1"}]
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Model returns non-JSON raw text
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "Xin lỗi, tôi không thể trả về JSON."}]},
                            "finishReason": "STOP",
                        }
                    ]
                },
            )
        elif call_count == 2:
            req_body = json.loads(request.content)
            user_prompt = req_body["contents"][0]["parts"][0]["text"]
            assert "RE-REQUEST: STRICT JSON SCHEMA" in user_prompt
            return httpx.Response(200, json=make_gemini_response(good_output))
        return httpx.Response(500, text="Unexpected extra call")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert call_count == 2
    assert result == {"seg_m_1": "Bản dịch câu 1"}


@pytest.mark.asyncio
async def test_translate_recovery_repeated_empty_exhaustion_actionable_error() -> None:
    """Repeatedly empty linguistic translation enters recovery and exhausts with actionable error without source fallback."""
    segments = [{"id": "seg_empty_lang", "source": "원문 문장"}]
    empty_output = {
        "translations": [{"id": "seg_empty_lang", "translated": "   "}]
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=make_gemini_response(empty_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderEmptyContentError) as exc_info:
            await provider.translate(segments)
        assert "có bản dịch bị rỗng sau các lần thử lại" in str(exc_info.value)
        assert "seg_empty_lang" in str(exc_info.value)
        assert exc_info.value.empty_ids == {"seg_empty_lang"}

    # 1 initial call + 2 targeted recovery attempts = 3 total calls
    assert call_count == 3


@pytest.mark.asyncio
async def test_translate_unknown_and_duplicate_never_accepted_after_retry() -> None:
    """Initial duplicate ID and retry unknown ID are strictly rejected after bounded retry (total 2 calls)."""
    segments = [{"id": "seg_clean", "source": "문장"}]

    dup_output = {
        "translations": [
            {"id": "seg_clean", "translated": "Dịch 1"},
            {"id": "seg_clean", "translated": "Dịch 2"},
        ]
    }
    unknown_output = {
        "translations": [
            {"id": "seg_alien_id", "translated": "Dịch không rõ"},
        ]
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=make_gemini_response(dup_output))
        return httpx.Response(200, json=make_gemini_response(unknown_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderIdValidationError) as exc_info:
            await provider.translate(segments)
        assert "chứa các ID thừa không có trong yêu cầu" in str(exc_info.value)

    assert call_count == 2


@pytest.mark.asyncio
async def test_reviewer_empty_language_repair_preserving_model_and_atomicity() -> None:
    """Reviewer empty correction on ordinary text recovers via bounded re-request preserving model."""
    segments = [{"id": "seg_rev_1", "source": "안녕하세요", "translation": "Xin chào"}]

    empty_corr_output = {
        "issues": [],
        "corrections": [
            {"id": "seg_rev_1", "corrected": "   "}
        ],
    }
    valid_corr_output = {
        "issues": [{"id": "seg_rev_1", "type": "politeness", "description": "Tăng tính trang trọng"}],
        "corrections": [
            {"id": "seg_rev_1", "corrected": "Kính chào quý khách"}
        ],
    }

    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if len(recorded_requests) == 1:
            return httpx.Response(200, json=make_gemini_response(empty_corr_output))
        elif len(recorded_requests) == 2:
            req_body = json.loads(request.content)
            user_prompt = req_body["contents"][0]["parts"][0]["text"]
            assert "RE-REQUEST REVIEW" in user_prompt
            assert "/models/gemini-3.5-flash-lite:generateContent" in request.url.path
            return httpx.Response(200, json=make_gemini_response(valid_corr_output))
        return httpx.Response(500, text="Unexpected extra call")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.review(segments, model="gemini-3.5-flash-lite")

    assert len(recorded_requests) == 2
    assert result["corrections"] == {"seg_rev_1": "Kính chào quý khách"}
    assert len(result["issues"]) == 1


@pytest.mark.asyncio
async def test_reviewer_repeated_empty_language_correction_fails_strictly() -> None:
    """Repeated empty correction on ordinary language text strictly fails after bounded retry (total 2 calls)."""
    segments = [{"id": "seg_rev_fail", "source": "문장", "translation": "Câu"}]
    empty_corr_output = {
        "issues": [],
        "corrections": [{"id": "seg_rev_fail", "corrected": "   "}],
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=make_gemini_response(empty_corr_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderEmptyContentError) as exc_info:
            await provider.review(segments)
        assert "bị rỗng" in str(exc_info.value)

    assert call_count == 2


@pytest.mark.asyncio
async def test_reviewer_symbols_remain_verbatim_on_empty_correction() -> None:
    """Symbol-only source '*' with empty reviewer correction restores source immediately with 1 call."""
    segments = [{"id": "seg_star_rev", "source": "*", "translation": "*"}]
    empty_corr_output = {
        "issues": [],
        "corrections": [{"id": "seg_star_rev", "corrected": "   "}],
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=make_gemini_response(empty_corr_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.review(segments)

    assert call_count == 1
    assert result["corrections"] == {"seg_star_rev": "*"}


@pytest.mark.asyncio
async def test_translate_id_punctuation_bracket_error_re_request_succeeds() -> None:
    """When model decorates ID with bracket and source text, re-request succeeds cleanly with total 2 calls."""
    segments = [{"id": "s_bracket_id", "source": "테스트 원문"}]

    bad_id_output = {
        "translations": [{"id": "s_bracket_id]: [테스트 원문", "translated": "Bản dịch thô"}]
    }
    good_id_output = {
        "translations": [{"id": "s_bracket_id", "translated": "Bản dịch chuẩn"}]
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=make_gemini_response(bad_id_output))
        return httpx.Response(200, json=make_gemini_response(good_id_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        result = await provider.translate(segments)

    assert call_count == 2
    assert result == {"s_bracket_id": "Bản dịch chuẩn"}


@pytest.mark.asyncio
async def test_response_finish_safety_and_network_failures_obey_appropriate_retry() -> None:
    """Safety blocks fail immediately (1 call); transient HTTP 500 and network errors retry and succeed."""
    segments = [{"id": "seg_1", "source": "문장"}]
    valid_output = {"translations": [{"id": "seg_1", "translated": "Dịch"}]}

    # 1. SAFETY finishReason fails immediately without retry (call_count == 1)
    call_safety = 0

    def handler_safety(request: httpx.Request) -> httpx.Response:
        nonlocal call_safety
        call_safety += 1
        return httpx.Response(200, json=make_gemini_response(valid_output, finish_reason="SAFETY"))

    transport_s = httpx.MockTransport(handler_safety)
    async with httpx.AsyncClient(transport=transport_s) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderSafetyError):
            await provider.translate(segments)
    assert call_safety == 1

    # 2. 401 Auth error fails immediately without retry (call_count == 1)
    call_auth = 0

    def handler_auth(request: httpx.Request) -> httpx.Response:
        nonlocal call_auth
        call_auth += 1
        return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

    transport_a = httpx.MockTransport(handler_auth)
    async with httpx.AsyncClient(transport=transport_a) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderAuthError):
            await provider.translate(segments)
    assert call_auth == 1

    # 3. Transient 500 server error retries with backoff and succeeds on attempt 2
    call_500 = 0

    def handler_500(request: httpx.Request) -> httpx.Response:
        nonlocal call_500
        call_500 += 1
        if call_500 == 1:
            return httpx.Response(500, json={"error": {"message": "Internal error"}})
        return httpx.Response(200, json=make_gemini_response(valid_output))

    transport_500 = httpx.MockTransport(handler_500)
    async with httpx.AsyncClient(transport=transport_500) as client:
        provider = GeminiProvider(api_key="test-key", client=client, retry_delay=0.01)
        res = await provider.translate(segments)
    assert call_500 == 2
    assert res == {"seg_1": "Dịch"}


@pytest.mark.asyncio
async def test_summarize_bounded_retry_succeeds() -> None:
    """Summarize with initial malformed output succeeds via bounded re-request (total 2 calls)."""
    segments = [{"id": "s1", "source": "문장 1"}]

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Empty summary string
            return httpx.Response(200, json=make_gemini_response({"summary": "   "}))
        elif call_count == 2:
            req_body = json.loads(request.content)
            user_prompt = req_body["contents"][0]["parts"][0]["text"]
            assert "chuỗi tóm tắt văn học" in user_prompt
            return httpx.Response(200, json=make_gemini_response({"summary": "Tóm tắt chương mạch lạc."}))
        return httpx.Response(500, text="Unexpected extra call")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        summary = await provider.summarize(segments)

    assert call_count == 2
    assert summary == "Tóm tắt chương mạch lạc."


def test_gemini_provider_interface_methods_callable() -> None:
    """GeminiProvider interface regression: translate, review, summarize, analyze must be callable methods."""
    provider = GeminiProvider(api_key="test-key")
    assert callable(getattr(provider, "translate", None))
    assert callable(getattr(provider, "review", None))
    assert callable(getattr(provider, "summarize", None))
    assert callable(getattr(provider, "analyze", None))


@pytest.mark.asyncio
async def test_review_bad_issue_first_good_second_bounded_retry() -> None:
    """Review with invalid issue description on first call succeeds via bounded re-request (total 2 calls)."""
    segments = [{"id": "seg_iss_1", "source": "문장 1", "translation": "Câu 1"}]

    bad_issue_output = {
        "issues": [{"id": "seg_iss_1", "type": "style", "description": "   "}],
        "corrections": [],
    }
    good_issue_output = {
        "issues": [{"id": "seg_iss_1", "type": "style", "description": "Câu văn chưa tự nhiên"}],
        "corrections": [{"id": "seg_iss_1", "corrected": "Câu 1 mượt mà"}],
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=make_gemini_response(bad_issue_output))
        elif call_count == 2:
            req_body = json.loads(request.content)
            user_prompt = req_body["contents"][0]["parts"][0]["text"]
            assert "RE-REQUEST REVIEW" in user_prompt
            return httpx.Response(200, json=make_gemini_response(good_issue_output))
        return httpx.Response(500, text="Unexpected extra call")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        res = await provider.review(segments)

    assert call_count == 2
    assert res["issues"][0]["description"] == "Câu văn chưa tự nhiên"
    assert res["corrections"] == {"seg_iss_1": "Câu 1 mượt mà"}


@pytest.mark.asyncio
async def test_review_persistent_invalid_issue_rejected() -> None:
    """Review with persistent invalid issue description raises ProviderSchemaValidationError after 2 calls."""
    segments = [{"id": "seg_iss_1", "source": "문장 1", "translation": "Câu 1"}]

    bad_issue_output = {
        "issues": [{"id": "seg_iss_1", "type": "style", "description": "   "}],
        "corrections": [],
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=make_gemini_response(bad_issue_output))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client)
        with pytest.raises(ProviderSchemaValidationError) as exc_info:
            await provider.review(segments)
        assert "description" in str(exc_info.value)

    assert call_count == 2


# =============================================================================
# Timeout, ConnectError vs ReadTimeout, and Key Rotation Exclusivity Tests
# =============================================================================

def test_gemini_provider_default_timeout_and_custom_timeout() -> None:
    """GeminiProvider defaults to read timeout ~120 and connect ~10; respects custom timeout."""
    # Default initialization
    provider_default = GeminiProvider(api_key="test-key")
    assert isinstance(provider_default.timeout, httpx.Timeout)
    assert provider_default.timeout.read == 120.0
    assert provider_default.timeout.connect == 10.0

    # Custom float timeout
    provider_custom = GeminiProvider(api_key="test-key", timeout=60.0)
    assert isinstance(provider_custom.timeout, httpx.Timeout)
    assert provider_custom.timeout.read == 60.0
    assert provider_custom.timeout.connect == 10.0

    # Custom httpx.Timeout object
    custom_to = httpx.Timeout(90.0, connect=15.0)
    provider_obj = GeminiProvider(api_key="test-key", timeout=custom_to)
    assert provider_obj.timeout == custom_to


@pytest.mark.asyncio
async def test_gemini_provider_connect_error_sanitized_and_typed() -> None:
    """ConnectError raises ProviderConnectError with sanitized secret and specific message."""
    secret_key = "AIzaSy_SUPER_SECRET_KEY_999"
    segments = [{"id": "seg_c1", "source": "문장"}]

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Failed to connect to host with key {secret_key}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key=secret_key, client=client, max_retries=1, retry_delay=0.0)
        with pytest.raises(ProviderConnectError) as exc_info:
            await provider.translate(segments)

        err_msg = str(exc_info.value)
        assert secret_key not in err_msg
        assert "[REDACTED]" in err_msg or "[REDACTED]" in str(exc_info.value.details)
        assert "ConnectError" in err_msg


@pytest.mark.asyncio
async def test_gemini_provider_read_timeout_sanitized_and_typed() -> None:
    """ReadTimeout raises ProviderTimeoutError with sanitized secret and specific message."""
    secret_key = "AIzaSy_ANOTHER_SECRET_777"
    segments = [{"id": "seg_r1", "source": "문장"}]

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"Timed out reading response with key {secret_key}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key=secret_key, client=client, max_retries=1, retry_delay=0.0)
        with pytest.raises(ProviderTimeoutError) as exc_info:
            await provider.translate(segments)

        err_msg = str(exc_info.value)
        assert secret_key not in err_msg
        assert "[REDACTED]" in err_msg or "[REDACTED]" in str(exc_info.value.details)
        assert "ReadTimeout" in err_msg


@pytest.mark.asyncio
async def test_gemini_provider_transient_read_timeout_retries_and_succeeds() -> None:
    """Transient ReadTimeout retries internally and succeeds on subsequent try."""
    calls = 0
    segments = [{"id": "seg_1", "source": "문장"}]
    ok_payload = {"translations": [{"id": "seg_1", "translated": "Bản dịch"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("Server took too long")
        return httpx.Response(200, json=make_gemini_response(ok_payload))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(api_key="test-key", client=client, max_retries=2, retry_delay=0.0)
        res = await provider.translate(segments)

    assert calls == 2
    assert res == {"seg_1": "Bản dịch"}


@pytest.mark.asyncio
async def test_gemini_provider_network_and_timeout_errors_never_rotate_api_key() -> None:
    """ReadTimeout and ConnectError never trigger key rotation/failover, preserving rotation exclusively for 429."""
    primary_key = "primary-key-123"
    secondary_key = "secondary-key-456"
    segments = [{"id": "seg_1", "source": "문장"}]

    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        used_keys.append(request.headers.get("x-goog-api-key", ""))
        raise httpx.ReadTimeout("Connection read timeout")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key=primary_key,
            secondary_api_key=secondary_key,
            client=client,
            max_retries=2,
            retry_delay=0.0,
        )
        with pytest.raises(ProviderTimeoutError):
            await provider.translate(segments)

    # All attempts (1 initial + 2 retries = 3) used the primary key exclusively
    assert len(used_keys) == 3
    assert all(k == primary_key for k in used_keys)
    assert secondary_key not in used_keys
    # Primary key must NOT be placed into cooldown
    assert not provider.is_key_in_cooldown(primary_key)


# =============================================================================
# Korean Term Matching and Boundary/Suffix Regression Tests
# =============================================================================

def test_korean_term_matching_boundaries_and_copula_endings() -> None:
    """Verify boundary checks and grammatical copula/particle recognition in Korean text."""
    # 1. Exact match and non-Hangul boundaries
    assert match_korean_term_in_text("본문", "본문") == "본문"
    assert match_korean_term_in_text("첫 번째 장 본문입니다.", "본문") == "본문입니다"
    assert match_korean_term_in_text("이것이 본문입니까?", "본문") == "본문입니까"
    assert match_korean_term_in_text("이전 본문이었습니다.", "본문") == "본문이었습니다"
    assert match_korean_term_in_text("해당 본문이었다.", "본문") == "본문이었다"
    assert match_korean_term_in_text("이곳이 본문이에요.", "본문") == "본문이에요"
    assert match_korean_term_in_text("본문이다.", "본문") == "본문이다"
    assert match_korean_term_in_text("본문이고 다른 것은...", "본문") == "본문이고"
    assert match_korean_term_in_text("본문이며 다음 장은...", "본문") == "본문이며"
    assert match_korean_term_in_text("본문이면 좋겠다.", "본문") == "본문이면"
    assert match_korean_term_in_text("본문입니다만 짧습니다.", "본문") == "본문입니다만"

    # 2. Vowel stem endings
    assert match_korean_term_in_text("그는 의사였습니다.", "의사") == "의사였습니다"
    assert match_korean_term_in_text("그는 의사예요.", "의사") == "의사예요"
    assert match_korean_term_in_text("그는 의사라면...", "의사") == "의사라면"
    assert match_korean_term_in_text("그는 의사라서...", "의사") == "의사라서"

    # 3. Particle endings
    assert match_korean_term_in_text("본문에서는 다루지 않는다.", "본문") == "본문에서는"
    assert match_korean_term_in_text("본문으로서는 적절하다.", "본문") == "본문으로서는"
    assert match_korean_term_in_text("소염을 투여했다.", "소염") == "소염을"
    assert match_korean_term_in_text("소염은 필요하다.", "소염") == "소염은"
    assert match_korean_term_in_text("소염만 처방했다.", "소염") == "소염만"

    # 4. Negative boundary cases: compounds and prefixes must NOT match (false-positive prevention)
    assert match_korean_term_in_text("소염제", "소염") is None
    assert match_korean_term_in_text("소염제의 효능", "소염") is None
    assert match_korean_term_in_text("소염환을 복용했다.", "소염") is None
    assert match_korean_term_in_text("김소염", "소염") is None
    assert match_korean_term_in_text("김소염입니다.", "소염") is None
    assert match_korean_term_in_text("대소염", "소염") is None

    # 5. Multiple occurrences in one string: skip false positive compound/prefix, find genuine occurrence
    assert match_korean_term_in_text("김소염은 소염제와 소염을 복용했다.", "소염") == "소염을"
    assert match_korean_term_in_text("소염제 대신 소염을 처방했다.", "소염") == "소염을"

    # 6. Title stem matching with parentheses / symbols
    assert match_korean_term_in_text("상점 오픈 (1)", "상점 오픈") == "상점 오픈"
    assert match_korean_term_in_text("상점 오픈입니다.", "상점 오픈") == "상점 오픈입니다"


def test_korean_headword_derivation_with_copula() -> None:
    """Verify conservative headword derivation strips legitimate copula/particle suffixes."""
    assert derive_conservative_headword("본문입니다") == "본문"
    assert derive_conservative_headword("본문입니까?") == "본문"
    assert derive_conservative_headword("본문이었습니다") == "본문"
    assert derive_conservative_headword("본문이었다") == "본문"
    assert derive_conservative_headword("본문이에요") == "본문"
    assert derive_conservative_headword("본문에서는") == "본문"
    assert derive_conservative_headword("본문으로서는") == "본문"
    assert derive_conservative_headword("의사였습니다") == "의사"
    assert derive_conservative_headword("의사예요") == "의사"

    # Negative cases: compounds and names are NOT stripped
    assert derive_conservative_headword("소염제") == "소염제"
    assert derive_conservative_headword("김소염") == "김소염"
    assert derive_conservative_headword("소염환") == "소염환"








