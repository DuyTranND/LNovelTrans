"""Focused mocked unit tests for Gemini 3-key round-robin, failover, and key-status endpoint.

All tests utilize httpx.MockTransport with zero live network calls or credential exposure.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.config import get_gemini_api_keys
from backend.app.main import create_app
from backend.app.provider import (
    GeminiProvider,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)


def _make_gemini_ok_response(text: str = "Bản dịch") -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": json.dumps({"translations": [{"id": "s1", "translated": text}]})}],
                },
                "finishReason": "STOP",
            }
        ]
    }


# =============================================================================
# 1. Config tests: 3 keys, tertiary support, and deduplication
# =============================================================================

def test_config_get_gemini_api_keys_three_distinct() -> None:
    env_mock = {
        "GEMINI_API_KEY": "key-1",
        "GEMINI_API_KEY_SECONDARY": "key-2",
        "GEMINI_API_KEY_TERTIARY": "key-3",
    }
    with patch.dict("os.environ", env_mock, clear=True):
        with patch("backend.app.config.dotenv_values", return_value={}):
            p, s, t = get_gemini_api_keys()
            assert p == "key-1"
            assert s == "key-2"
            assert t == "key-3"


def test_config_get_gemini_api_keys_deduplication() -> None:
    # If secondary duplicates primary, secondary becomes None and tertiary remains in slot 3
    env_mock = {
        "GEMINI_API_KEY": "key-same",
        "GEMINI_API_KEY_SECONDARY": "key-same",
        "GEMINI_API_KEY_TERTIARY": "key-diff",
    }
    with patch.dict("os.environ", env_mock, clear=True):
        with patch("backend.app.config.dotenv_values", return_value={}):
            p, s, t = get_gemini_api_keys()
            assert p == "key-same"
            assert s is None
            assert t == "key-diff"


def test_config_get_gemini_api_keys_numeric_aliases() -> None:
    env_mock = {
        "GEMINI_API_KEY": "k1",
        "GEMINI_API_KEY_2": "k2",
        "GEMINI_API_KEY_3": "k3",
    }
    with patch.dict("os.environ", env_mock, clear=True):
        with patch("backend.app.config.dotenv_values", return_value={}):
            p, s, t = get_gemini_api_keys()
            assert p == "k1"
            assert s == "k2"
            assert t == "k3"


def test_config_dotenv_priority_and_disabled_secondary(tmp_path: Any) -> None:
    fake_env_file = tmp_path / ".env"
    fake_env_file.touch()

    process_env = {
        "GEMINI_API_KEY": "process-primary",
        "GEMINI_API_KEY_SECONDARY": "process-secondary",
        "GEMINI_API_KEY_TERTIARY": "process-tertiary",
    }
    # 1. Local .env defines only primary -> secondary & tertiary disabled (None)
    dotenv_only_primary = {
        "GEMINI_API_KEY": "dotenv-primary",
    }
    with patch.dict("os.environ", process_env, clear=True):
        with patch("backend.app.config._project_env", fake_env_file):
            with patch("backend.app.config.dotenv_values", return_value=dotenv_only_primary):
                p, s, t = get_gemini_api_keys()
                assert p == "dotenv-primary"
                assert s is None
                assert t is None

    # 2. Local .env defines primary and tertiary -> secondary is disabled (None), tertiary preserved
    dotenv_primary_and_tertiary = {
        "GEMINI_API_KEY": "dotenv-primary",
        "GEMINI_API_KEY_TERTIARY": "dotenv-tertiary",
    }
    with patch.dict("os.environ", process_env, clear=True):
        with patch("backend.app.config._project_env", fake_env_file):
            with patch("backend.app.config.dotenv_values", return_value=dotenv_primary_and_tertiary):
                p, s, t = get_gemini_api_keys()
                assert p == "dotenv-primary"
                assert s is None
                assert t == "dotenv-tertiary"


# =============================================================================
# 2. Provider tests: 3-key round-robin, lock, dedup, failover, cooldown
# =============================================================================

@pytest.mark.asyncio
async def test_provider_deduplicates_passed_keys() -> None:
    provider = GeminiProvider(
        api_key="key-A",
        secondary_api_key="key-A",
        tertiary_api_key="key-B",
    )
    assert provider.api_keys == ["key-A", "key-B"]
    assert provider.primary_key == "key-A"
    assert provider.secondary_key is None
    assert provider.tertiary_key == "key-B"
    assert provider.has_secondary is False
    assert provider.has_tertiary is True
    await provider.close()


@pytest.mark.asyncio
async def test_provider_blank_slot_rotation_status_and_auth_error() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        used_keys.append(key)
        if key == "k3":
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED", "message": "Bad key"}})
        return httpx.Response(200, json=_make_gemini_ok_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # Slot 2 is None/blank, slot 3 is configured
        provider = GeminiProvider(
            api_key="k1",
            secondary_api_key=None,
            tertiary_api_key="k3",
            client=client,
        )
        assert provider.primary_key == "k1"
        assert provider.secondary_key is None
        assert provider.tertiary_key == "k3"
        assert provider.has_secondary is False
        assert provider.has_tertiary is True
        assert provider.api_keys == ["k1", "k3"]

        # Check status metadata before requests
        status = provider.get_key_status()
        assert status["count"] == 2
        s1, s2, s3 = status["slots"]
        assert s1["slot"] == 1 and s1["configured"] is True and s1["status"] == "ready" and s1["role"] == "primary"
        assert s2["slot"] == 2 and s2["configured"] is False and s2["status"] == "not_configured" and s2["role"] == "secondary"
        assert s3["slot"] == 3 and s3["configured"] is True and s3["status"] == "ready" and s3["role"] == "tertiary"

        # First request uses k1 (slot 1) successfully
        res = await provider.translate([{"id": "s1", "source": "문장"}])
        assert res == {"s1": "Bản dịch"}
        assert used_keys == ["k1"]

        # Second request rotates to k3 (slot 3), receives 401
        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

        err_msg = str(exc_info.value)
        assert "thứ ba (GEMINI_API_KEY_TERTIARY)" in err_msg
        assert "k3" not in err_msg  # Secret redacted
        assert used_keys == ["k1", "k3"]


@pytest.mark.asyncio
async def test_provider_duplicated_slot_rotation_status_and_auth_error() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        used_keys.append(key)
        if key == "k3":
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED", "message": "Invalid key"}})
        return httpx.Response(200, json=_make_gemini_ok_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # Slot 2 duplicates slot 1, slot 3 is k3
        provider = GeminiProvider(
            api_key="k1",
            secondary_api_key="k1",
            tertiary_api_key="k3",
            client=client,
        )
        assert provider.primary_key == "k1"
        assert provider.secondary_key is None
        assert provider.tertiary_key == "k3"
        assert provider.has_secondary is False
        assert provider.has_tertiary is True
        assert provider.api_keys == ["k1", "k3"]

        # Check status metadata: slot 2 marked duplicate
        status = provider.get_key_status()
        assert status["count"] == 2
        s1, s2, s3 = status["slots"]
        assert s1["slot"] == 1 and s1["configured"] is True and s1["status"] == "ready"
        assert s2["slot"] == 2 and s2["configured"] is False and s2["status"] == "duplicate"
        assert s3["slot"] == 3 and s3["configured"] is True and s3["status"] == "ready"

        # Rotation uses only distinct k1 and k3
        await provider.translate([{"id": "s1", "source": "문장"}])
        assert used_keys == ["k1"]

        # 401 on k3 reports tertiary role and redacts secret
        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

        err_msg = str(exc_info.value)
        assert "thứ ba (GEMINI_API_KEY_TERTIARY)" in err_msg
        assert "k3" not in err_msg
        assert used_keys == ["k1", "k3"]


@pytest.mark.asyncio
async def test_provider_api_keys_positional_slots_and_max3() -> None:
    # 1. Overflow beyond max 3 is capped at 3
    provider_overflow = GeminiProvider(
        api_keys=["key-1", "key-2", "key-3", "key-overflow-4", "key-overflow-5"]
    )
    assert len(provider_overflow._slots) == 3
    assert provider_overflow.api_keys == ["key-1", "key-2", "key-3"]
    assert provider_overflow.primary_key == "key-1"
    assert provider_overflow.secondary_key == "key-2"
    assert provider_overflow.tertiary_key == "key-3"
    assert provider_overflow.has_secondary is True
    assert provider_overflow.has_tertiary is True
    await provider_overflow.close()

    # 2. Positional list with blank slot 2 preserves slot 3
    provider_blank_mid = GeminiProvider(
        api_keys=["pos-1", None, "pos-3"]
    )
    assert provider_blank_mid.api_keys == ["pos-1", "pos-3"]
    assert provider_blank_mid.primary_key == "pos-1"
    assert provider_blank_mid.secondary_key is None
    assert provider_blank_mid.tertiary_key == "pos-3"
    assert provider_blank_mid.has_secondary is False
    assert provider_blank_mid.has_tertiary is True

    status = provider_blank_mid.get_key_status()
    assert status["slots"][0]["configured"] is True
    assert status["slots"][1]["configured"] is False
    assert status["slots"][1]["status"] == "not_configured"
    assert status["slots"][2]["configured"] is True
    assert status["slots"][2]["status"] == "ready"
    assert provider_blank_mid._get_key_role_name("pos-3") == "thứ ba (GEMINI_API_KEY_TERTIARY)"
    await provider_blank_mid.close()

    # 3. Positional list with duplicate in slot 2
    provider_dup_mid = GeminiProvider(
        api_keys=["pos-A", "pos-A", "pos-C"]
    )
    assert provider_dup_mid.api_keys == ["pos-A", "pos-C"]
    assert provider_dup_mid.primary_key == "pos-A"
    assert provider_dup_mid.secondary_key is None
    assert provider_dup_mid.tertiary_key == "pos-C"
    dup_status = provider_dup_mid.get_key_status()
    assert dup_status["slots"][1]["status"] == "duplicate"
    await provider_dup_mid.close()


@pytest.mark.asyncio
async def test_provider_three_key_round_robin_rotation() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        used_keys.append(key)
        return httpx.Response(200, json=_make_gemini_ok_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="key-1",
            secondary_api_key="key-2",
            tertiary_api_key="key-3",
            client=client,
        )

        for _ in range(6):
            await provider.translate([{"id": "s1", "source": "문장"}])

    # Expect round-robin: key-1 -> key-2 -> key-3 -> key-1 -> key-2 -> key-3
    assert used_keys == ["key-1", "key-2", "key-3", "key-1", "key-2", "key-3"]


@pytest.mark.asyncio
async def test_provider_round_robin_concurrency_lock() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        used_keys.append(key)
        return httpx.Response(200, json=_make_gemini_ok_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="k1",
            secondary_api_key="k2",
            tertiary_api_key="k3",
            client=client,
        )

        # Launch 9 concurrent translation requests
        tasks = [
            provider.translate([{"id": "s1", "source": "문장"}])
            for _ in range(9)
        ]
        await asyncio.gather(*tasks)

    # Concurrency lock ensures each of the 3 keys is used exactly 3 times
    assert used_keys.count("k1") == 3
    assert used_keys.count("k2") == 3
    assert used_keys.count("k3") == 3


@pytest.mark.asyncio
async def test_provider_429_retry_after_cooldown_and_failover() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        used_keys.append(key)
        if key == "key-1":
            # 429 with Retry-After: 30
            return httpx.Response(
                429,
                headers={"retry-after": "30"},
                json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}},
            )
        return httpx.Response(200, json=_make_gemini_ok_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="key-1",
            secondary_api_key="key-2",
            tertiary_api_key="key-3",
            client=client,
            fallback_cooldown=5.0,
        )

        # First request starts on key-1, receives 429, puts key-1 in cooldown (30s), fails over to key-2
        res = await provider.translate([{"id": "s1", "source": "문장"}])
        assert res == {"s1": "Bản dịch"}
        assert used_keys == ["key-1", "key-2"]
        assert provider.is_key_in_cooldown("key-1")
        assert provider.get_key_cooldown_remaining("key-1") > 20

        # Next request must skip key-1 (in cooldown) and pick key-3
        res2 = await provider.translate([{"id": "s1", "source": "문장"}])
        assert res2 == {"s1": "Bản dịch"}
        assert used_keys[-1] == "key-3"


@pytest.mark.asyncio
async def test_provider_all_keys_exhausted_raises_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="k1",
            secondary_api_key="k2",
            tertiary_api_key="k3",
            client=client,
        )
        with pytest.raises(ProviderRateLimitError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

        assert "vượt hạn mức" in str(exc_info.value)
        assert provider.is_key_in_cooldown("k1")
        assert provider.is_key_in_cooldown("k2")
        assert provider.is_key_in_cooldown("k3")


@pytest.mark.asyncio
async def test_provider_nonretryable_auth_on_tertiary_reports_role() -> None:
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        used_keys.append(key)
        # Tertiary receives 401
        return httpx.Response(
            401,
            json={"error": {"status": "UNAUTHENTICATED", "message": f"Rejected {key}"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="k1",
            secondary_api_key="k2",
            tertiary_api_key="k3",
            client=client,
        )
        # Advance cursor to tertiary (idx 2)
        provider._round_robin_index = 2

        with pytest.raises(ProviderAuthError) as exc_info:
            await provider.translate([{"id": "s1", "source": "문장"}])

        err = exc_info.value
        assert used_keys == ["k3"]  # Auth does NOT rotate
        assert "GEMINI_API_KEY_TERTIARY" in str(err)
        assert "k3" not in str(err)  # Secret redacted


@pytest.mark.asyncio
async def test_provider_network_retry_stays_on_same_key() -> None:
    used_keys: list[str] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        key = request.headers.get("x-goog-api-key", "")
        used_keys.append(key)
        if calls < 3:
            raise httpx.ReadTimeout("Timeout")
        return httpx.Response(200, json=_make_gemini_ok_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="key-A",
            secondary_api_key="key-B",
            tertiary_api_key="key-C",
            client=client,
            max_retries=3,
            retry_delay=0.0,
        )
        res = await provider.translate([{"id": "s1", "source": "문장"}])

    assert res == {"s1": "Bản dịch"}
    # All 3 network attempts used key-A exclusively
    assert len(used_keys) == 3
    assert all(k == "key-A" for k in used_keys)
    assert not provider.is_key_in_cooldown("key-A")


# =============================================================================
# 3. Safe key-status endpoint tests
# =============================================================================

def test_api_provider_key_status_endpoint(tmp_path: Any) -> None:
    app = create_app(data_dir=tmp_path, start_worker=False)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.get("/api/provider/key-status")
    assert response.status_code == 200
    data = response.json()

    assert "count" in data
    assert "inflight" in data
    assert "last_used" in data
    assert "slots" in data
    assert len(data["slots"]) == 3

    slot1, slot2, slot3 = data["slots"]
    assert slot1["slot"] == 1
    assert slot1["role"] == "primary"
    assert "label" in slot1
    assert "status" in slot1
    assert "inflight" in slot1
    assert "cooldown_remaining" in slot1

    # Verify no secret leakage
    raw_json_str = response.text
    assert "AIza" not in raw_json_str


@pytest.mark.asyncio
async def test_provider_get_key_status_metadata_safety() -> None:
    provider = GeminiProvider(
        api_key="secret-key-1",
        secondary_api_key="secret-key-2",
        tertiary_api_key="secret-key-3",
    )
    # Manually set a cooldown on key 2
    provider._key_cooldowns["secret-key-2"] = time.monotonic() + 15.0

    status = provider.get_key_status()
    assert status["count"] == 3
    assert status["inflight"] == 0
    assert len(status["slots"]) == 3

    s1, s2, s3 = status["slots"]
    assert s1["configured"] is True
    assert s1["status"] == "ready"
    assert s2["configured"] is True
    assert s2["status"] == "cooldown"
    assert s2["cooldown_remaining"] > 10.0
    assert s3["configured"] is True
    assert s3["status"] == "ready"

    # Strictly verify zero secret disclosure in JSON representation
    dumped = json.dumps(status)
    for sec in ["secret-key-1", "secret-key-2", "secret-key-3"]:
        assert sec not in dumped

    await provider.close()


@pytest.mark.asyncio
async def test_provider_async_blocked_request_inflight_and_release() -> None:
    blocked_event = asyncio.Event()
    entered_event = asyncio.Event()

    async def blocking_handler(request: httpx.Request) -> httpx.Response:
        entered_event.set()
        await blocked_event.wait()
        return httpx.Response(200, json=_make_gemini_ok_response())

    transport = httpx.MockTransport(blocking_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(
            api_key="secret-block-k1",
            secondary_api_key="secret-block-k2",
            tertiary_api_key="secret-block-k3",
            client=client,
        )

        # Launch translation in background task
        task = asyncio.create_task(provider.translate([{"id": "s1", "source": "문장"}]))

        # Wait until the handler has started and is actively blocked
        await asyncio.wait_for(entered_event.wait(), timeout=5.0)

        # While blocked: inflight must be 1, status must be 'busy'
        status_busy = provider.get_key_status()
        assert status_busy["inflight"] == 1
        slot1 = status_busy["slots"][0]
        assert slot1["inflight"] == 1
        assert slot1["status"] == "busy"

        # Verify no secret leakage during busy status
        dumped_busy = json.dumps(status_busy)
        assert "secret-block" not in dumped_busy

        # Unblock the handler and wait for task completion
        blocked_event.set()
        res = await asyncio.wait_for(task, timeout=5.0)
        assert res == {"s1": "Bản dịch"}

        # After completion: inflight releases to 0, status transitions to 'ready', last_used is recorded
        status_ready = provider.get_key_status()
        assert status_ready["inflight"] == 0
        assert status_ready["last_used"] is not None
        slot1_ready = status_ready["slots"][0]
        assert slot1_ready["inflight"] == 0
        assert slot1_ready["status"] == "ready"
        assert slot1_ready["last_used"] is not None

        # Verify zero secrets leaked in status metadata
        dumped_ready = json.dumps(status_ready)
        for s in ["secret-block-k1", "secret-block-k2", "secret-block-k3"]:
            assert s not in dumped_ready


@pytest.mark.asyncio
async def test_provider_tertiary_only_request_header_and_counter() -> None:
    used_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key_header = request.headers.get("x-goog-api-key")
        used_headers.append(key_header)
        return httpx.Response(200, json=_make_gemini_ok_response("Bản dịch từ khóa thứ ba"))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # Only slot 3 (tertiary) is populated; slot 1 and slot 2 are None
        provider = GeminiProvider(
            api_key=None,
            secondary_api_key=None,
            tertiary_api_key="tertiary-only-secret",
            client=client,
        )

        assert provider.primary_key is None
        assert provider.secondary_key is None
        assert provider.tertiary_key == "tertiary-only-secret"
        assert provider.has_secondary is False
        assert provider.has_tertiary is True
        assert provider.api_keys == ["tertiary-only-secret"]

        # Send request and verify single-key execution uses api_keys[0]
        res = await provider.translate([{"id": "s1", "source": "문장"}])
        assert res == {"s1": "Bản dịch từ khóa thứ ba"}

        # Verify header is strictly the tertiary key (never None or "None")
        assert len(used_headers) == 1
        assert used_headers[0] == "tertiary-only-secret"

        # Verify inflight counter and tracking dictionaries have no None key
        assert None not in provider._inflight_counts
        assert None not in provider._last_used_timestamps
        assert provider._inflight_counts.get("tertiary-only-secret", 0) == 0

        # Verify status metadata shows slot 3 configured and active, slots 1 & 2 not configured
        status = provider.get_key_status()
        assert status["count"] == 1
        assert status["inflight"] == 0
        s1, s2, s3 = status["slots"]
        assert s1["configured"] is False and s1["status"] == "not_configured"
        assert s2["configured"] is False and s2["status"] == "not_configured"
        assert s3["configured"] is True and s3["status"] == "ready"
        assert s3["last_used"] is not None

        # Verify no secret leakage
        dumped = json.dumps(status)
        assert "tertiary-only-secret" not in dumped

