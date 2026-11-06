"""Durable asynchronous job worker for Trạm Dịch.

Handles sequential chapter translation jobs with SQLite persistence,
batching bounds, strict locked glossary validation, optional reviewer,
chapter narrative summaries, pause/resume/retry, restart recovery,
and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from backend.app.context import (
    batch_segments,
    build_translation_context,
    get_prior_chapter_title_mappings,
    is_symbol_only_source,
    normalize_segment,
    preserve_whitespace,
)
from backend.app.db import (
    claim_next_job,
    get_book_chapters,
    get_book_summaries,
    get_chapter,
    get_connection,
    get_default_prompt_preset,
    get_job,
    get_prompt_preset,
    get_story_bible,
    get_untranslated_segments_count,
    list_characters,
    list_glossary,
    recover_interrupted_jobs,
    save_summary,
    update_chapter_status,
    update_job_progress,
    update_job_status,
    utc_now,
)
from backend.app.quality_gate import evaluate_batch_quality_gate, is_quality_finding_resolved
from backend.app.provider import (
    ProviderAuthError,
    ProviderConnectError,
    ProviderError,
    ProviderModelError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderSafetyError,
    ProviderTimeoutError,
    ProviderValidationError,
    sanitize_secret,
)

logger = logging.getLogger(__name__)
from backend.app.memory import (
    load_memory,
    load_review_memory,
    memory_key,
    review_memory_key,
    save_memory,
    save_review_memory,
)

JOB_TRANSIENT_MAX_RETRIES = 3
JOB_TRANSIENT_INITIAL_DELAY = 1.0


def is_transient_network_or_server_error(exc: Exception) -> bool:
    """Determine whether an error is a transient network or server error eligible for bounded retry."""
    # Strict exclusion: Never retry auth, model, schema, validation, safety, or programming errors
    if isinstance(
        exc,
        (
            ProviderAuthError,
            ProviderModelError,
            ProviderValidationError,
            ProviderSafetyError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
            RuntimeError,
            AssertionError,
        ),
    ):
        return False

    # Explicit provider network/timeout/rate-limit types
    if isinstance(
        exc,
        (
            ProviderConnectError,
            ProviderTimeoutError,
            ProviderNetworkError,
            ProviderRateLimitError,
        ),
    ):
        return True

    # httpx network and timeout errors
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True

    # ProviderError status checks (server 5xx or rate limit 429)
    if isinstance(exc, ProviderError):
        if exc.status_code is not None:
            if exc.status_code in {429, 500, 502, 503, 504}:
                return True
            if 400 <= exc.status_code < 500:
                return False
        # Fallback text check on provider error message
        msg = (str(exc) + " " + str(getattr(exc, "details", ""))).lower()
        if any(
            kw in msg
            for kw in [
                "timeout",
                "connect",
                "kết nối",
                "quá thời gian",
                "mạng",
                "network",
                "500",
                "502",
                "503",
                "504",
                "rate limit",
            ]
        ):
            return True

    # Standard python socket/connection/timeout errors
    if isinstance(
        exc,
        (
            ConnectionResetError,
            ConnectionRefusedError,
            ConnectionAbortedError,
            BrokenPipeError,
            asyncio.TimeoutError,
        ),
    ):
        return True

    return False


async def _call_provider(
    method: Callable[..., Any],
    *args: Any,
    model: str | None = None,
    **kwargs: Any,
) -> Any:
    """Invoke provider method passing model keyword argument if supported."""
    supports_model = False
    try:
        sig = inspect.signature(method)
        params = sig.parameters
        supports_model = "model" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (ValueError, TypeError):
        supports_model = False

    if supports_model:
        return await method(*args, model=model, **kwargs)
    return await method(*args, **kwargs)


def _validate_single_issue(
    iss: Any,
    expected_id: str | None = None,
    batch_ids: set[str] | None = None,
) -> dict[str, str]:
    if not isinstance(iss, dict):
        raise ProviderValidationError("Review issue phải là dictionary.")

    iid = iss.get("id")
    itype = iss.get("type")
    idesc = iss.get("description")

    if not isinstance(iid, str) or not iid.strip():
        raise ProviderValidationError("Review issue thiếu trường 'id' hợp lệ dạng chuỗi.")
    if not isinstance(itype, str) or not itype.strip():
        raise ProviderValidationError("Review issue thiếu trường 'type' hợp lệ dạng chuỗi.")
    if not isinstance(idesc, str) or not idesc.strip():
        raise ProviderValidationError("Review issue thiếu trường 'description' hợp lệ dạng chuỗi.")

    clean_id = iid.strip()
    if batch_ids is not None and clean_id not in batch_ids:
        raise ProviderValidationError(f"Review chứa issue ID không có trong đợt: {clean_id}")
    if expected_id is not None and clean_id != expected_id:
        raise ProviderValidationError(
            f"Review issue ID '{clean_id}' không khớp với khóa phân đoạn '{expected_id}'."
        )

    return {
        "id": clean_id,
        "type": itype.strip(),
        "description": idesc.strip(),
    }


def validate_review_output(
    rev_batch: list[Any],
    corrections: Any,
    raw_issues: Any,
    locked_glossary: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
    """Strictly validate review output (both live and cached) identically before applying to DB.

    Validates:
    - Corrections must be a dict
    - All correction IDs must be a subset of requested batch IDs
    - All correction values must be non-empty strings
    - Resulting translations must not be empty for non-empty sources
    - Issues must be a valid list or per-segment dict with valid batch IDs
    - All issue entries must contain non-empty string id, type, description
    - Cached per-segment issue entry id must match its map key
    """
    if not isinstance(corrections, dict):
        raise ProviderValidationError("Review corrections phải là dictionary.")

    batch_ids = {normalize_segment(s)[0] for s in rev_batch}

    for cid in corrections.keys():
        if cid not in batch_ids:
            raise ProviderValidationError(f"Reviewer trả về ID không có trong đợt: {cid}")

    # Validate reviewer corrections before any write, including strings
    for s in rev_batch:
        sid, src = normalize_segment(s)
        if sid in corrections:
            corr_val = corrections[sid]
            if not isinstance(corr_val, str) or not corr_val.strip():
                if isinstance(corr_val, str) and not corr_val.strip() and is_symbol_only_source(src):
                    corrections[sid] = preserve_whitespace(src, src.strip())
                else:
                    raise ProviderValidationError(
                        f"Bản sửa đổi cho phân đoạn '{sid}' bị rỗng hoặc không hợp lệ."
                    )

        trans = corrections.get(sid, s.get("translation", ""))
        if not isinstance(trans, str) or (src.strip() and not trans.strip()):
            if isinstance(trans, str) and not trans.strip() and is_symbol_only_source(src):
                trans = preserve_whitespace(src, src.strip())
                corrections[sid] = trans
            else:
                raise ProviderValidationError("Reviewer trả về bản dịch không hợp lệ.")

    # Validate and normalize issues
    review_data_per_seg: dict[str, list[dict[str, Any]]] = {}
    if isinstance(raw_issues, list):
        for iss in raw_issues:
            valid_iss = _validate_single_issue(iss, batch_ids=batch_ids)
            review_data_per_seg.setdefault(valid_iss["id"], []).append(valid_iss)
    elif isinstance(raw_issues, dict):
        for isid, iss_list in raw_issues.items():
            if not isinstance(isid, str) or isid not in batch_ids:
                raise ProviderValidationError(f"Review chứa issue ID không có trong đợt: {isid}")
            if not isinstance(iss_list, list):
                raise ProviderValidationError("Review issues per segment phải là danh sách.")
            norm_list: list[dict[str, Any]] = []
            for iss in iss_list:
                valid_iss = _validate_single_issue(iss, expected_id=isid, batch_ids=batch_ids)
                norm_list.append(valid_iss)
            review_data_per_seg[isid] = norm_list
    else:
        raise ProviderValidationError("Review issues phải là danh sách hoặc dictionary.")

    return corrections, review_data_per_seg


def needs_llm_review(s_item: dict[str, Any]) -> bool:
    """Determine whether a segment requires LLM review.

    Rules:
    - Segments without non-empty translation do not undergo review.
    - review_data is None: Not yet reviewed -> True.
    - review_data is []: Completed review with no issues -> False (never reprocessed).
    - Normal existing human/LLM issue lists: Completed review -> False.
    - Quality-gate-only findings array:
        - Fresh/pending findings (no 'reviewed' flag): Need first review -> True.
        - Findings marked reviewed (e.g. 'reviewed': True or status == 'reviewed'): Completed review -> False.
    """
    if not s_item.get("translation") or not s_item.get("translation", "").strip():
        return False
    r = s_item.get("review_data")
    if r is None:
        return True
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            return True
    if r is None:
        return True
    if isinstance(r, dict):
        r = [r]
    if isinstance(r, list):
        if not r:
            return False
        # If any item is not a quality_gate finding (e.g. human or LLM review issue),
        # this row has already completed review -> do not re-review.
        if any(not (isinstance(x, dict) and x.get("type") == "quality_gate") for x in r):
            return False
        # For quality_gate-only finding arrays, check if any finding is still pending first review.
        return any(
            not (bool(x.get("reviewed")) or x.get("status") == "reviewed")
            for x in r
            if isinstance(x, dict)
        )
    return False


class JobWorker:
    """Single durable worker for sequential translation and processing jobs."""

    def __init__(
        self,
        db_path: Path | str,
        provider_getter: Callable[[], Any],
        poll_interval: float = 0.5,
    ):
        self.db_path = Path(db_path)
        self.provider_getter = provider_getter
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the background worker task."""
        self._stop_event.clear()
        recover_interrupted_jobs(self.db_path)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Gracefully stop the background worker task."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def step(self) -> dict[str, Any] | None:
        """Process one pending job if available, useful for tests and single-stepping."""
        job = claim_next_job(self.db_path)
        if not job:
            return None
        await self._run_job(job)
        return get_job(self.db_path, job["id"])

    async def _worker_loop(self) -> None:
        """Continuous polling loop."""
        while not self._stop_event.is_set():
            try:
                job = claim_next_job(self.db_path)
                if job:
                    await self._run_job(job)
                else:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in worker loop: {e}", exc_info=True)
                await asyncio.sleep(self.poll_interval)

    async def _wait_retry_or_interrupt(
        self, delay: float, job_id: str, progress: dict[str, Any]
    ) -> bool:
        """Wait for delay seconds while remaining interruptible by pause or stop.

        Returns True if interrupted (worker stop requested or job was paused), False otherwise.
        """
        end_time = time.monotonic() + delay
        while time.monotonic() < end_time:
            if self._stop_event.is_set():
                return True
            cur_job = get_job(self.db_path, job_id)
            if cur_job and cur_job["status"] == "pausing":
                update_job_status(self.db_path, job_id, "paused", progress=progress)
                return True
            sleep_chunk = min(0.5, max(0.0, end_time - time.monotonic()))
            if sleep_chunk > 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_chunk)
                    return True
                except asyncio.TimeoutError:
                    pass
        if self._stop_event.is_set():
            return True
        cur_job = get_job(self.db_path, job_id)
        if cur_job and cur_job["status"] == "pausing":
            update_job_status(self.db_path, job_id, "paused", progress=progress)
            return True
        return False

    async def _call_provider_with_retry(
        self,
        job_id: str,
        progress: dict[str, Any],
        method: Callable[..., Any],
        *args: Any,
        model: str | None = None,
        max_retries: int = JOB_TRANSIENT_MAX_RETRIES,
        initial_delay: float = JOB_TRANSIENT_INITIAL_DELAY,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        """Call provider method with bounded retries for transient network/server errors.

        Returns (result, interrupted):
        - If interrupted by pause or stop, returns (None, True).
        - If successful, returns (result, False).
        - If retry budget exhausted or error is non-retryable, raises the exception.
        """
        attempt = 0
        while True:
            try:
                res = await _call_provider(method, *args, model=model, **kwargs)
                return res, False
            except Exception as exc:
                if not is_transient_network_or_server_error(exc):
                    raise
                if attempt >= max_retries:
                    logger.warning(
                        "Retry budget (%d) exhausted for transient error on job %s: %s",
                        max_retries,
                        job_id,
                        exc,
                    )
                    raise
                attempt += 1
                delay = initial_delay * (2 ** (attempt - 1))
                logger.info(
                    "Transient error on job %s (%s). Retrying %d/%d in %.1fs...",
                    job_id,
                    exc,
                    attempt,
                    max_retries,
                    delay,
                )
                interrupted = await self._wait_retry_or_interrupt(delay, job_id, progress)
                if interrupted:
                    return None, True

    async def _run_job(self, job: dict[str, Any]) -> None:
        """Execute a translation job step-by-step with persistence and boundaries."""
        job_id = job["id"]
        book_id = job["book_id"]
        params = job.get("params") or {}
        progress = job.get("progress") or {}

        provider = self.provider_getter()
        if not provider:
            update_job_status(
                self.db_path,
                job_id,
                "failed",
                error="Chưa cấu hình dịch vụ AI (Provider) cho hệ thống.",
                progress=progress,
            )
            return

        secrets_to_sanitize = getattr(provider, "all_secrets", getattr(provider, "api_key", None))
        api_key = secrets_to_sanitize

        # Snapshot model configuration per-job
        job_model = params.get("model") or "gemini-3.5-flash-lite"
        preset_id = params.get("preset_id")
        preset = get_prompt_preset(self.db_path, preset_id) if preset_id else None
        if not preset:
            preset = get_default_prompt_preset(self.db_path, book_id)

        chapter_ids: list[str] = params.get("chapter_ids") or []
        review: bool = bool(params.get("review", False))
        max_segments: int | None = params.get("max_segments")

        # Load book context and character profiles
        story_bible = get_story_bible(self.db_path, book_id) or ""
        recent_summaries = get_book_summaries(self.db_path, book_id)
        characters = list_characters(self.db_path, book_id)

        completed_segments_count = progress.get("completed_segments", 0)
        processed_count = progress.get("processed_segments", 0)

        try:
            if hasattr(provider, "is_model_supported") and callable(provider.is_model_supported):
                if not provider.is_model_supported(job_model):
                    update_job_status(
                        self.db_path,
                        job_id,
                        "failed",
                        error=f"Mô hình '{job_model}' không còn được hỗ trợ hoặc chưa cấu hình.",
                        progress=progress,
                    )
                    return
            if hasattr(provider, "set_model") and callable(provider.set_model):
                try:
                    provider.set_model(job_model)
                except ProviderModelError as pme:
                    update_job_status(
                        self.db_path,
                        job_id,
                        "failed",
                        error=str(pme),
                        progress=progress,
                    )
                    return

            for ch_id in chapter_ids:
                if self._stop_event.is_set():
                    return

                # Check if pause was requested before starting chapter
                cur_job = get_job(self.db_path, job_id)
                if cur_job and cur_job["status"] == "pausing":
                    update_job_status(self.db_path, job_id, "paused", progress=progress)
                    return

                chap_data = get_chapter(self.db_path, ch_id)
                if not chap_data:
                    continue
                spine_index = chap_data.get("spine_index")
                recent_summaries = get_book_summaries(self.db_path, book_id, spine_index)
                glossary = list_glossary(self.db_path, book_id)
                prior_titles = get_prior_chapter_title_mappings(
                    self.db_path, book_id, before_spine_index=spine_index, glossary=glossary
                )

                all_segments = chap_data.get("segments") or []
                non_ws_segments = [s for s in all_segments if not s.get("is_whitespace")]

                # ---------------------------------------------------------------------
                # Phase 1: TRANSLATION PHASE for the chapter
                # ---------------------------------------------------------------------
                pending_segments = [
                    s for s in non_ws_segments
                    if not s.get("translation") or s.get("translation", "").strip() == ""
                ]

                if pending_segments:
                    # Batch pending segments (max 40 segments, max ~6000 chars)
                    if max_segments is not None:
                        remaining = max_segments - processed_count
                        if remaining <= 0:
                            rem = get_untranslated_segments_count(self.db_path, ch_id)
                            chap_status = "partial" if rem > 0 else "translated"
                            update_chapter_status(self.db_path, ch_id, chap_status)
                            progress.setdefault("chapters", {}).setdefault(ch_id, {})["status"] = chap_status
                            progress["stage"] = "completed"
                            update_job_status(self.db_path, job_id, "completed", progress=progress)
                            return
                        pending_segments = pending_segments[:remaining]
                    batches = batch_segments(pending_segments, max_segments=40, max_chars=6000, max_tokens=2500)

                    for batch in batches:
                        if self._stop_event.is_set():
                            return

                        # 1. Enforce single segment length limit
                        for seg in batch:
                            _, src_text = normalize_segment(seg)
                            if len(src_text) > 15000:
                                raise ProviderValidationError(
                                    f"Phân đoạn '{seg.get('id')}' vượt quá giới hạn độ dài cho phép của provider (15000 ký tự)."
                                )

                        # 2. Check pause intent before dispatching batch
                        cur_job = get_job(self.db_path, job_id)
                        if cur_job and cur_job["status"] == "pausing":
                            update_job_status(self.db_path, job_id, "paused", progress=progress)
                            return

                        # 3. Build bounded translation context
                        first_seg_seq = batch[0].get("seq_order", 0)
                        last_seg_seq = batch[-1].get("seq_order", 0)
                        nb_before = [s for s in all_segments if s.get("seq_order", 0) < first_seg_seq][-3:]
                        nb_after = [s for s in all_segments if s.get("seq_order", 0) > last_seg_seq][:3]

                        ctx = build_translation_context(
                            batch,
                            glossary=glossary,
                            story_bible=story_bible,
                            recent_summaries=recent_summaries,
                            neighboring_before=nb_before,
                            neighboring_after=nb_after,
                            prior_titles=prior_titles,
                            is_review=False,
                            characters=characters,
                            preset=preset,
                        )

                        # 4. Call provider translation
                        progress["stage"] = "translating"
                        progress["current_chapter_id"] = ch_id
                        update_job_progress(self.db_path, job_id, progress)
                        cache_key = memory_key(book_id, ch_id, job_model, False, ctx)
                        cached = load_memory(self.db_path, cache_key)
                        cached_translations = None
                        if cached is not None and isinstance(cached, dict):
                            cand = cached.get("translations")
                            if isinstance(cand, dict):
                                req_ids = [normalize_segment(s)[0] for s in batch]
                                valid_cache = True
                                for s in batch:
                                    sid, stext = normalize_segment(s)
                                    if sid not in cand:
                                        if is_symbol_only_source(stext):
                                            cand[sid] = preserve_whitespace(stext, stext.strip())
                                        else:
                                            valid_cache = False
                                            break
                                    elif not isinstance(cand[sid], str) or (stext.strip() and not cand[sid].strip()):
                                        if is_symbol_only_source(stext):
                                            cand[sid] = preserve_whitespace(stext, stext.strip())
                                        else:
                                            valid_cache = False
                                            break
                                if valid_cache and not (set(cand.keys()) - set(req_ids)):
                                    cached_translations = cand
                                else:
                                    logger.warning("Quarantining invalid cached translation for key %s", cache_key)
                                    with get_connection(self.db_path) as conn:
                                        conn.execute("DELETE FROM translation_memory WHERE context_key = ?", (cache_key,))
                            else:
                                logger.warning("Quarantining invalid cached translation payload for key %s", cache_key)
                                with get_connection(self.db_path) as conn:
                                    conn.execute("DELETE FROM translation_memory WHERE context_key = ?", (cache_key,))

                        if cached_translations is not None:
                            translations = cached_translations
                        else:
                            trans_res, interrupted = await self._call_provider_with_retry(
                                job_id,
                                progress,
                                provider.translate,
                                batch,
                                context=ctx,
                                model=job_model,
                            )
                            if interrupted:
                                return
                            translations = trans_res

                        if not isinstance(translations, dict):
                            raise ProviderValidationError("Bản dịch trả về không phải là dictionary.")

                        # 5. Deterministic quality gate and bounded retry
                        gate_result = evaluate_batch_quality_gate(batch, translations, context=ctx)
                        if not gate_result.passed and gate_result.needs_retry and cached_translations is None:
                            logger.info(
                                "Quality gate flagged critical issues for batch, initiating bounded retry: %s",
                                gate_result.retry_hint,
                            )
                            retry_ctx = dict(ctx)
                            if gate_result.retry_hint:
                                retry_ctx["user_prompt"] = ctx["user_prompt"] + f"\n\n[LƯU Ý CHẤT LƯỢNG BẮT BUỘC: {gate_result.retry_hint}]"
                            retry_res, interrupted = await self._call_provider_with_retry(
                                job_id,
                                progress,
                                provider.translate,
                                batch,
                                context=retry_ctx,
                                model=job_model,
                            )
                            if interrupted:
                                return
                            if isinstance(retry_res, dict):
                                translations = retry_res
                                gate_result = evaluate_batch_quality_gate(batch, translations, context=ctx)

                        # Independent validation of IDs and content
                        req_ids = [normalize_segment(s)[0] for s in batch]
                        missing_ids = set(req_ids) - set(translations.keys())
                        if missing_ids:
                            truly_missing = []
                            for mid in missing_ids:
                                s_mid = next((s for s in batch if normalize_segment(s)[0] == mid), None)
                                src_mid = normalize_segment(s_mid)[1] if s_mid else ""
                                if is_symbol_only_source(src_mid):
                                    translations[mid] = preserve_whitespace(src_mid, src_mid.strip())
                                else:
                                    truly_missing.append(mid)
                            if truly_missing:
                                raise ProviderValidationError(
                                    f"Bản dịch thiếu các phân đoạn ID: {sorted(truly_missing)}"
                                )
                        extra_ids = set(translations.keys()) - set(req_ids)
                        if extra_ids:
                            raise ProviderValidationError(
                                f"Bản dịch chứa các ID thừa: {sorted(extra_ids)}"
                            )

                        for s in batch:
                            sid, stext = normalize_segment(s)
                            trans = translations.get(sid, "")
                            if not isinstance(trans, str):
                                raise ProviderValidationError("Bản dịch phải là chuỗi văn bản.")
                            if stext.strip() and not trans.strip():
                                if is_symbol_only_source(stext):
                                    translations[sid] = preserve_whitespace(stext, stext.strip())
                                else:
                                    raise ProviderValidationError(
                                        f"Bản dịch cho phân đoạn '{sid}' bị rỗng."
                                    )

                        # 6. Persist batch translations atomically with actionable findings
                        now_str = utc_now()
                        with get_connection(self.db_path) as conn:
                            save_memory(conn, cache_key, translations, {})
                            for s in batch:
                                sid, src = normalize_segment(s)
                                final_trans = preserve_whitespace(src, translations[sid])
                                seg_findings = gate_result.findings_by_segment.get(sid)
                                if seg_findings:
                                    rdata_str = json.dumps(seg_findings, ensure_ascii=False)
                                    conn.execute(
                                        """
                                        UPDATE segments
                                        SET translation = ?,
                                            review_data = ?,
                                            revision = CASE
                                                WHEN (translation IS NOT ? OR review_data IS NOT ?) THEN revision + 1
                                                ELSE revision
                                            END
                                        WHERE id = ?
                                        """,
                                        (final_trans, rdata_str, final_trans, rdata_str, sid),
                                    )
                                else:
                                    conn.execute(
                                        """
                                        UPDATE segments
                                        SET translation = ?,
                                            review_data = NULL,
                                            revision = CASE
                                                WHEN (translation IS NOT ? OR review_data IS NOT NULL) THEN revision + 1
                                                ELSE revision
                                            END
                                        WHERE id = ?
                                        """,
                                        (final_trans, final_trans, sid),
                                    )

                            completed_segments_count += len(batch)
                            processed_count += len(batch)
                            progress["processed_segments"] = processed_count
                            progress["completed_segments"] = completed_segments_count
                            progress.setdefault("chapters", {}).setdefault(ch_id, {})["completed_segments"] = (
                                progress["chapters"][ch_id].get("completed_segments", 0) + len(batch)
                            )
                            conn.execute(
                                "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?",
                                (json.dumps(progress), now_str, job_id),
                            )

                        # 7. Check max_segments smoke cap
                        if max_segments is not None and processed_count >= max_segments:
                            rem = get_untranslated_segments_count(self.db_path, ch_id)
                            chap_status = "partial" if rem > 0 else "translated"
                            update_chapter_status(self.db_path, ch_id, chap_status)
                            progress.setdefault("chapters", {}).setdefault(ch_id, {})["status"] = chap_status
                            progress["stage"] = "completed"
                            update_job_status(self.db_path, job_id, "completed", progress=progress)
                            return

                        # 8. Check pause intent after in-flight batch finishes and persists
                        cur_job = get_job(self.db_path, job_id)
                        if cur_job and cur_job["status"] == "pausing":
                            update_job_status(self.db_path, job_id, "paused", progress=progress)
                            return

                # ---------------------------------------------------------------------
                # Phase 2: REVIEW PHASE for the chapter (if requested)
                # ---------------------------------------------------------------------
                if review:
                    cur_chap = get_chapter(self.db_path, ch_id)
                    cur_segs = cur_chap.get("segments") or []
                    cur_non_ws = [s for s in cur_segs if not s.get("is_whitespace")]
                    # Resume must not repeat already reviewed content:
                    # Segments with review_data is None (or containing only pending quality gate findings) have not completed LLM review.
                    def _needs_llm_review(s_item: dict[str, Any]) -> bool:
                        return needs_llm_review(s_item)

                    unreviewed_segments = [s for s in cur_non_ws if _needs_llm_review(s)]

                    if unreviewed_segments:
                        progress["stage"] = "reviewing"
                        progress["current_chapter_id"] = ch_id
                        update_job_progress(self.db_path, job_id, progress)

                        rev_batches = batch_segments(unreviewed_segments, max_segments=40, max_chars=6000, max_tokens=2500)
                        for rev_batch in rev_batches:
                            if self._stop_event.is_set():
                                return

                            cur_job = get_job(self.db_path, job_id)
                            if cur_job and cur_job["status"] == "pausing":
                                update_job_status(self.db_path, job_id, "paused", progress=progress)
                                return

                            first_seq = rev_batch[0].get("seq_order", 0)
                            last_seq = rev_batch[-1].get("seq_order", 0)
                            nb_before = [s for s in cur_segs if s.get("seq_order", 0) < first_seq][-3:]
                            nb_after = [s for s in cur_segs if s.get("seq_order", 0) > last_seq][:3]

                            rev_ctx = build_translation_context(
                                rev_batch,
                                glossary=glossary,
                                story_bible=story_bible,
                                recent_summaries=recent_summaries,
                                neighboring_before=nb_before,
                                neighboring_after=nb_after,
                                prior_titles=prior_titles,
                                is_review=True,
                                characters=characters,
                                preset=preset,
                            )

                            batch_review_input = [
                                {
                                    "id": normalize_segment(s)[0],
                                    "source": normalize_segment(s)[1],
                                    "translation": s.get("translation", ""),
                                }
                                for s in rev_batch
                            ]

                            rev_cache_key = review_memory_key(
                                book_id, ch_id, job_model, batch_review_input, rev_ctx
                            )
                            cached_rev = load_review_memory(self.db_path, rev_cache_key)
                            corrections = None
                            review_data_per_seg = None

                            if cached_rev is not None:
                                raw_corrections = cached_rev.get("corrections")
                                raw_issues = cached_rev.get("issues")
                                try:
                                    if not isinstance(raw_corrections, dict) or not isinstance(raw_issues, dict):
                                        raise ProviderValidationError(
                                            "Bộ nhớ đệm thẩm định thiếu trường 'corrections' hoặc 'issues' dạng dict."
                                        )
                                    corrections, review_data_per_seg = validate_review_output(
                                        rev_batch, raw_corrections, raw_issues
                                    )
                                except ProviderValidationError as cache_err:
                                    logger.warning(
                                        "Quarantining invalid cached review for key %s: %s",
                                        rev_cache_key,
                                        cache_err,
                                    )
                                    with get_connection(self.db_path) as conn:
                                        conn.execute(
                                            "DELETE FROM translation_memory WHERE context_key = ?",
                                            (rev_cache_key,),
                                        )
                                    cached_rev = None
                            else:
                                with get_connection(self.db_path) as conn:
                                    bad_row = conn.execute(
                                        "SELECT 1 FROM translation_memory WHERE context_key = ?",
                                        (rev_cache_key,),
                                    ).fetchone()
                                    if bad_row:
                                        logger.warning(
                                            "Quarantining malformed cached review row for key %s",
                                            rev_cache_key,
                                        )
                                        conn.execute(
                                            "DELETE FROM translation_memory WHERE context_key = ?",
                                            (rev_cache_key,),
                                        )

                            if cached_rev is None:
                                rev_res, interrupted = await self._call_provider_with_retry(
                                    job_id,
                                    progress,
                                    provider.review,
                                    batch_review_input,
                                    context=rev_ctx,
                                    model=job_model,
                                )
                                if interrupted:
                                    return
                                review_result = rev_res
                                if not isinstance(review_result, dict):
                                    raise ProviderValidationError("Kết quả thẩm định không hợp lệ.")

                                raw_corrections = review_result.get("corrections", {})
                                raw_issues = review_result.get("issues", [])
                                corrections, review_data_per_seg = validate_review_output(
                                    rev_batch, raw_corrections, raw_issues
                                )

                            # Re-evaluate quality gate deterministically on updated translations
                            final_translations: dict[str, str] = {}
                            for s in rev_batch:
                                sid, src = normalize_segment(s)
                                if sid in corrections and corrections[sid].strip():
                                    final_translations[sid] = preserve_whitespace(src, corrections[sid])
                                elif is_symbol_only_source(src):
                                    final_translations[sid] = s.get("translation") or preserve_whitespace(src, src.strip())
                                else:
                                    final_translations[sid] = s.get("translation", "")

                            post_gate_result = evaluate_batch_quality_gate(
                                rev_batch, final_translations, context=rev_ctx
                            )

                            # Safety protection: If reviewer correction introduced critical defects
                            # (e.g. untranslated Hangul runs >= 4 or degenerative repetition loops or meta-chatter),
                            # revert that segment's translation to prior translation rather than corrupting the book.
                            rolled_back_sids: set[str] = set()
                            for sid, pfindings in post_gate_result.findings_by_segment.items():
                                if sid in corrections:
                                    crit_findings = [f for f in pfindings if f.get("severity") == "critical"]
                                    if crit_findings:
                                        s_orig = next((s for s in rev_batch if normalize_segment(s)[0] == sid), None)
                                        if s_orig and s_orig.get("translation"):
                                            final_translations[sid] = s_orig["translation"]
                                            rolled_back_sids.add(sid)
                                            corrections.pop(sid, None)
                                            logger.warning(
                                                "Discarding reviewer correction for segment %s due to critical quality gate failure: %s",
                                                sid,
                                                [f.get("description") for f in crit_findings],
                                            )

                            # Re-evaluate quality gate deterministically on actual final saved text after rollback
                            # so that stale critical findings from discarded corrections are not stored.
                            if rolled_back_sids:
                                post_gate_result = evaluate_batch_quality_gate(
                                    rev_batch, final_translations, context=rev_ctx
                                )

                            now_str = utc_now()
                            with get_connection(self.db_path) as conn:
                                if cached_rev is None or rolled_back_sids:
                                    save_review_memory(
                                        conn, rev_cache_key, corrections, review_data_per_seg
                                    )
                                for s in rev_batch:
                                    sid, src = normalize_segment(s)
                                    new_trans = final_translations[sid]
                                    rdata = review_data_per_seg.get(sid, [])
                                    orig_r = s.get("review_data")
                                    if isinstance(orig_r, str):
                                        try:
                                            orig_r = json.loads(orig_r)
                                        except Exception:
                                            orig_r = []
                                    if not isinstance(orig_r, list):
                                        orig_r = []

                                    qg_items = [
                                        x for x in orig_r
                                        if isinstance(x, dict) and x.get("type") == "quality_gate"
                                    ]
                                    unresolved_qg: list[dict[str, Any]] = []
                                    for qg in qg_items:
                                        if not is_quality_finding_resolved(
                                            qg,
                                            src,
                                            new_trans,
                                            post_gate_result.findings_by_segment.get(sid, []),
                                        ):
                                            item_copy = dict(qg)
                                            item_copy["reviewed"] = True
                                            unresolved_qg.append(item_copy)

                                    # Also capture any newly surfaced quality gate findings on corrected translation
                                    post_findings = post_gate_result.findings_by_segment.get(sid, [])
                                    seen_sigs = {
                                        (it.get("category"), it.get("expected_target"), it.get("source_term"))
                                        for it in unresolved_qg
                                    }
                                    for pf in post_findings:
                                        sig = (pf.get("category"), pf.get("expected_target"), pf.get("source_term"))
                                        if sig not in seen_sigs:
                                            pf_copy = dict(pf)
                                            pf_copy["reviewed"] = True
                                            unresolved_qg.append(pf_copy)
                                            seen_sigs.add(sig)

                                    if unresolved_qg:
                                        rdata = unresolved_qg + [x for x in rdata if x not in unresolved_qg]

                                    rdata_str = json.dumps(rdata)
                                    conn.execute(
                                        """
                                        UPDATE segments
                                        SET translation = ?,
                                            review_data = ?,
                                            revision = CASE
                                                WHEN (translation IS NOT ? OR review_data IS NOT ?) THEN revision + 1
                                                ELSE revision
                                            END
                                        WHERE id = ?
                                        """,
                                        (new_trans, rdata_str, new_trans, rdata_str, sid),
                                    )
                                conn.execute(
                                    "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?",
                                    (json.dumps(progress), now_str, job_id),
                                )

                            cur_job = get_job(self.db_path, job_id)
                            if cur_job and cur_job["status"] == "pausing":
                                update_job_status(self.db_path, job_id, "paused", progress=progress)
                                return

                # ---------------------------------------------------------------------
                # Phase 3: SUMMARY PHASE for the chapter (after review)
                # ---------------------------------------------------------------------
                chap_summary_existing = progress.get("summaries", {}).get(ch_id)
                if not chap_summary_existing:
                    progress["stage"] = "summarizing"
                    progress["current_chapter_id"] = ch_id
                    update_job_progress(self.db_path, job_id, progress)
                    chap_segs_after = get_chapter(self.db_path, ch_id).get("segments") or []
                    try:
                        sum_res, interrupted = await self._call_provider_with_retry(
                            job_id,
                            progress,
                            provider.summarize,
                            chap_segs_after,
                            model=job_model,
                        )
                        if interrupted:
                            return
                        summary_text = sum_res
                        save_summary(self.db_path, book_id, ch_id, summary_text)
                        recent_summaries.append(summary_text)
                        progress.setdefault("summaries", {})[ch_id] = summary_text
                        update_job_progress(self.db_path, job_id, progress)
                    except Exception as sum_err:
                        err_msg = f"Lỗi tạo tóm tắt chương {chap_data.get('title') or ch_id}: {sanitize_secret(str(sum_err), api_key)}"
                        update_job_status(self.db_path, job_id, "failed", error=err_msg, progress=progress)
                        return

                update_chapter_status(self.db_path, ch_id, "translated")
                progress.setdefault("chapters", {}).setdefault(ch_id, {})["status"] = "translated"
                update_job_progress(self.db_path, job_id, progress)

            # All chapters completed
            progress["stage"] = "completed"
            update_job_status(self.db_path, job_id, "completed", progress=progress)

        except Exception as e:
            err_msg = sanitize_secret(str(e), api_key)
            logger.error(f"Job {job_id} failed: {err_msg}")
            update_job_status(self.db_path, job_id, "failed", error=err_msg, progress=progress)
