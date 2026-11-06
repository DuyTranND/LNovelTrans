"""Pre-translation AI discovery service for Trạm Dịch.

Performs bounded, deterministic entity and terminology candidate discovery
from original source chapter/segment texts (characters, terms, places, organizations),
validating Korean boundaries, enforcing zero hallucinations, and persisting suggestions
in pending proposal records without overwriting locked records.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from backend.app.db import (
    create_discovery_proposal,
    create_discovery_run,
    get_book,
    get_book_chapters,
    get_chapter,
    get_connection,
    get_discovery_run,
    list_characters,
    list_glossary,
    update_discovery_run,
    utc_now,
)
from backend.app.jobs import is_transient_network_or_server_error
from backend.app.provider import (
    ProviderAuthError,
    ProviderError,
    ProviderModelError,
    ProviderValidationError,
    sanitize_secret,
)

logger = logging.getLogger(__name__)

DISCOVERY_MAX_BATCH_SEGMENTS = 25
DISCOVERY_MAX_BATCH_CHARS = 4000
DISCOVERY_TRANSIENT_MAX_RETRIES = 3
DISCOVERY_TRANSIENT_INITIAL_DELAY = 1.0


def _chunk_text_safely(text: str, max_chars: int = DISCOVERY_MAX_BATCH_CHARS) -> list[str]:
    """Split long source text into safely bounded chunks <= max_chars without losing content."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        if total - start <= max_chars:
            chunks.append(text[start:])
            break

        end = start + max_chars
        break_pos = -1
        # Try paragraph break first
        p_break = text.rfind("\n\n", start + (max_chars // 2), end)
        if p_break != -1:
            break_pos = p_break + 2
        else:
            # Try newline break
            nl_break = text.rfind("\n", start + (max_chars // 2), end)
            if nl_break != -1:
                break_pos = nl_break + 1
            else:
                # Try sentence boundaries
                for sep in (". ", "? ", "! ", ".\n", "?\n", "!\n"):
                    s_break = text.rfind(sep, start + (max_chars // 2), end)
                    if s_break != -1:
                        break_pos = s_break + len(sep)
                        break
                if break_pos == -1:
                    # Try whitespace
                    sp_break = text.rfind(" ", start + (max_chars // 2), end)
                    if sp_break != -1:
                        break_pos = sp_break + 1

        if break_pos <= start:
            break_pos = end

        chunk = text[start:break_pos]
        if chunk.strip():
            chunks.append(chunk)
        start = break_pos

    return [c for c in chunks if c.strip()]


class DiscoveryManager:
    """Manages pre-translation entity and terminology candidate discovery runs."""

    def __init__(
        self,
        db_path: Path | str,
        provider_getter: Callable[[], Any],
    ):
        self.db_path = Path(db_path)
        self.provider_getter = provider_getter
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}

    async def shutdown(self) -> None:
        """Cancel and clean up all in-flight discovery tasks."""
        for run_id, event in self._stop_events.items():
            event.set()
        for run_id, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._active_tasks.clear()
        self._stop_events.clear()

    async def start_discovery(
        self,
        book_id: str,
        chapter_ids: list[str] | None = None,
        model: str | None = None,
        max_segments: int | None = None,
    ) -> dict[str, Any]:
        """Initiate an asynchronous discovery run for the specified book and chapters.

        Validates:
        - book_id exists
        - no active discovery run for this book
        - max_segments, if provided: must be a positive integer (> 0)
        - chapter_ids, if provided: must be non-empty, deduplicated, and all IDs must exist in the book
          (invalid mixed IDs raise ValueError so they are not silently lost)
        """
        if max_segments is not None:
            if isinstance(max_segments, bool) or not isinstance(max_segments, int) or max_segments <= 0:
                raise ValueError("max_segments phải là số nguyên dương lớn hơn 0.")

        book = get_book(self.db_path, book_id)
        if not book:
            raise KeyError(f"Book '{book_id}' not found")

        # Ensure no concurrent discovery runs are active for this book
        for rid, t in list(self._active_tasks.items()):
            if not t.done():
                run_obj = get_discovery_run(self.db_path, rid)
                if run_obj and run_obj.get("book_id") == book_id:
                    raise RuntimeError("Đang có tiến trình gợi ý thực thể đang chạy cho bộ sách này.")

        all_chapters = get_book_chapters(self.db_path, book_id)
        if not all_chapters:
            raise ValueError("Sách không có chương nào để phân tích.")

        all_ch_map = {c["id"]: c for c in all_chapters}

        if chapter_ids is not None:
            if not chapter_ids:
                raise ValueError("Danh sách chapter_ids không được rỗng khi được cung cấp.")
            seen_cids: set[str] = set()
            dedup_chapter_ids: list[str] = []
            for cid in chapter_ids:
                if cid not in seen_cids:
                    seen_cids.add(cid)
                    dedup_chapter_ids.append(cid)

            invalid_cids = [cid for cid in dedup_chapter_ids if cid not in all_ch_map]
            if invalid_cids:
                raise ValueError(
                    f"Các mã chương không hợp lệ hoặc không thuộc về sách này: {', '.join(invalid_cids)}"
                )
            target_chapter_ids = dedup_chapter_ids
        else:
            target_chapter_ids = [c["id"] for c in all_chapters]

        total_segments = 0
        for cid in target_chapter_ids:
            ch_data = get_chapter(self.db_path, cid)
            if ch_data:
                segs = ch_data.get("segments") or []
                total_segments += sum(1 for s in segs if not s.get("is_whitespace") and (s.get("source") or "").strip())

        if max_segments is not None:
            total_segments = min(total_segments, max_segments)

        effective_model = model or "gemini-3.5-flash-lite"
        run = create_discovery_run(
            self.db_path,
            book_id=book_id,
            model=effective_model,
            chapter_ids=target_chapter_ids,
            total_chapters=len(target_chapter_ids),
            total_segments=total_segments,
        )

        run_id = run["id"]
        stop_event = asyncio.Event()
        self._stop_events[run_id] = stop_event

        task = asyncio.create_task(
            self._execute_run(
                run_id=run_id,
                book_id=book_id,
                chapter_ids=target_chapter_ids,
                model=effective_model,
                stop_event=stop_event,
                max_segments=max_segments,
            )
        )
        self._active_tasks[run_id] = task

        def _cleanup(t: asyncio.Task[None]) -> None:
            self._active_tasks.pop(run_id, None)
            self._stop_events.pop(run_id, None)

        task.add_done_callback(_cleanup)
        return run

    async def cancel_discovery(self, run_id: str) -> bool:
        """Cancel an in-flight discovery run.

        Preserves terminal states ('completed', 'failed', 'cancelled').
        Returns True if a running/queued run was cancelled, or if already cancelled (idempotent).
        Returns False if run is in another terminal state ('completed', 'failed') or not found.
        """
        run = get_discovery_run(self.db_path, run_id)
        if not run:
            return False

        current_status = run.get("status")
        if current_status in ("completed", "failed"):
            return False
        if current_status == "cancelled":
            return True

        event = self._stop_events.get(run_id)
        if event:
            event.set()
        task = self._active_tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        update_discovery_run(self.db_path, run_id, status="cancelled")
        return True

    async def _execute_run(
        self,
        run_id: str,
        book_id: str,
        chapter_ids: list[str],
        model: str,
        stop_event: asyncio.Event,
        max_segments: int | None = None,
    ) -> None:
        """Core execution loop for pre-translation entity and term discovery."""
        provider = self.provider_getter()
        if not provider:
            update_discovery_run(
                self.db_path,
                run_id,
                status="failed",
                error="Chưa cấu hình dịch vụ AI (Provider) cho hệ thống.",
            )
            return

        update_discovery_run(self.db_path, run_id, status="running")

        existing_characters = list_characters(self.db_path, book_id)
        existing_glossary = list_glossary(self.db_path, book_id)

        processed_chapters = 0
        processed_segments = 0
        proposals_count = 0

        try:
            for ch_id in chapter_ids:
                if stop_event.is_set():
                    update_discovery_run(self.db_path, run_id, status="cancelled")
                    return

                ch_data = get_chapter(self.db_path, ch_id)
                if not ch_data:
                    processed_chapters += 1
                    continue

                ch_title = ch_data.get("title") or ""
                spine_idx = ch_data.get("spine_index")
                segments = ch_data.get("segments") or []
                non_ws = [
                    s for s in segments
                    if not s.get("is_whitespace") and (s.get("source") or "").strip()
                ]

                # Bounded batching and chunking:
                # - Process no more than max_segments cap (including partial final batch)
                # - Safely bound ALL actual provider payloads to <= DISCOVERY_MAX_BATCH_CHARS (4000)
                #   and <= DISCOVERY_MAX_BATCH_SEGMENTS (25), chunking oversized single sources safely
                #   while preserving segment IDs and exact source-candidate associations.
                if max_segments is not None:
                    remaining_cap = max_segments - processed_segments
                    if remaining_cap <= 0:
                        break
                    non_ws = non_ws[:remaining_cap]

                cur_batch: list[dict[str, Any]] = []
                cur_chars = 0

                async def _dispatch_batch(batch_items: list[dict[str, Any]]) -> None:
                    nonlocal proposals_count
                    if not batch_items:
                        return
                    if stop_event.is_set():
                        update_discovery_run(self.db_path, run_id, status="cancelled")
                        return

                    candidates = await self._call_provider_with_retry(
                        provider=provider,
                        method_name="discover_candidates",
                        segments=batch_items,
                        chapter_id=ch_id,
                        chapter_title=ch_title,
                        existing_characters=existing_characters,
                        existing_glossary=existing_glossary,
                        model=model,
                    )

                    for cand in candidates:
                        try:
                            create_discovery_proposal(
                                self.db_path,
                                book_id=book_id,
                                run_id=run_id,
                                category=cand.get("category", "GLOSSARY"),
                                canonical_source=cand.get("canonical_source") or cand.get("source_term", ""),
                                canonical_target=cand.get("canonical_target") or cand.get("target_term", ""),
                                source_aliases=cand.get("source_aliases", []),
                                gender=cand.get("gender", "unknown"),
                                speech_style=cand.get("speech_style", ""),
                                notes=cand.get("notes", ""),
                                first_occurrence_chapter_id=ch_id,
                                first_occurrence_chapter_index=spine_idx,
                                segment_id=cand.get("segment_id"),
                                source_span=cand.get("source_span", ""),
                            )
                            proposals_count += 1
                        except Exception as e:
                            logger.warning(f"Error persisting candidate proposal: {e}")

                for s in non_ws:
                    if stop_event.is_set():
                        update_discovery_run(self.db_path, run_id, status="cancelled")
                        return

                    src_text = s.get("source") or ""
                    src_len = len(src_text)

                    if src_len > DISCOVERY_MAX_BATCH_CHARS:
                        # Flush any pending standard batch first
                        if cur_batch:
                            await _dispatch_batch(cur_batch)
                            if stop_event.is_set():
                                return
                            processed_segments += len(cur_batch)
                            cur_batch = []
                            cur_chars = 0
                            update_discovery_run(
                                self.db_path,
                                run_id,
                                processed_segments=processed_segments,
                                proposals_count=proposals_count,
                            )

                        # Chunk oversized single segment text to safely bound provider payload
                        chunks = _chunk_text_safely(src_text, DISCOVERY_MAX_BATCH_CHARS)
                        for chunk in chunks:
                            if stop_event.is_set():
                                update_discovery_run(self.db_path, run_id, status="cancelled")
                                return
                            chunk_item = dict(s)
                            chunk_item["source"] = chunk
                            await _dispatch_batch([chunk_item])

                        processed_segments += 1
                        update_discovery_run(
                            self.db_path,
                            run_id,
                            processed_segments=processed_segments,
                            proposals_count=proposals_count,
                        )
                    else:
                        if cur_batch and (
                            len(cur_batch) >= DISCOVERY_MAX_BATCH_SEGMENTS
                            or cur_chars + src_len > DISCOVERY_MAX_BATCH_CHARS
                        ):
                            await _dispatch_batch(cur_batch)
                            if stop_event.is_set():
                                return
                            processed_segments += len(cur_batch)
                            cur_batch = []
                            cur_chars = 0
                            update_discovery_run(
                                self.db_path,
                                run_id,
                                processed_segments=processed_segments,
                                proposals_count=proposals_count,
                            )

                        cur_batch.append(s)
                        cur_chars += src_len

                if cur_batch:
                    if stop_event.is_set():
                        update_discovery_run(self.db_path, run_id, status="cancelled")
                        return
                    await _dispatch_batch(cur_batch)
                    if stop_event.is_set():
                        return
                    processed_segments += len(cur_batch)
                    cur_batch = []
                    cur_chars = 0
                    update_discovery_run(
                        self.db_path,
                        run_id,
                        processed_segments=processed_segments,
                        proposals_count=proposals_count,
                    )

                processed_chapters += 1
                update_discovery_run(
                    self.db_path,
                    run_id,
                    processed_chapters=processed_chapters,
                    processed_segments=processed_segments,
                    proposals_count=proposals_count,
                )

                if max_segments is not None and processed_segments >= max_segments:
                    break

            update_discovery_run(
                self.db_path,
                run_id,
                status="completed",
                processed_chapters=processed_chapters,
                processed_segments=processed_segments,
                proposals_count=proposals_count,
            )
        except asyncio.CancelledError:
            update_discovery_run(self.db_path, run_id, status="cancelled")
            raise
        except Exception as exc:
            secret = getattr(provider, "all_secrets", getattr(provider, "api_key", None))
            safe_err = sanitize_secret(str(exc), secret)
            logger.error(f"Discovery run {run_id} failed: {safe_err}", exc_info=True)
            update_discovery_run(
                self.db_path,
                run_id,
                status="failed",
                error=safe_err,
                processed_chapters=processed_chapters,
                processed_segments=processed_segments,
                proposals_count=proposals_count,
            )

    async def _call_provider_with_retry(
        self,
        provider: Any,
        method_name: str,
        *args: Any,
        max_retries: int = DISCOVERY_TRANSIENT_MAX_RETRIES,
        initial_delay: float = DISCOVERY_TRANSIENT_INITIAL_DELAY,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Execute provider discovery call with bounded retry for transient network/server issues."""
        method = getattr(provider, method_name, None)
        if not method or not callable(method):
            raise ProviderError(f"Provider does not implement '{method_name}'")

        attempt = 0
        while True:
            try:
                res = await method(*args, **kwargs)
                if not isinstance(res, list):
                    raise ProviderValidationError(
                        f"Phản hồi AI không hợp lệ: kết quả phải là danh sách, nhận được {type(res).__name__}."
                    )
                for item in res:
                    if not isinstance(item, dict):
                        raise ProviderValidationError(
                            f"Phần tử trong danh sách gợi ý phải là dictionary, nhận được {type(item).__name__}."
                        )
                    src_term = item.get("canonical_source") or item.get("source_term")
                    tgt_term = item.get("canonical_target") or item.get("target_term")
                    if not src_term or not str(src_term).strip():
                        raise ProviderValidationError(
                            "Ứng viên gợi ý thiếu thông tin thuật ngữ nguồn hợp lệ."
                        )
                    if not tgt_term or not str(tgt_term).strip():
                        raise ProviderValidationError(
                            "Ứng viên gợi ý thiếu thông tin thuật ngữ đích hợp lệ."
                        )
                return res
            except Exception as exc:
                if not is_transient_network_or_server_error(exc):
                    raise
                if attempt >= max_retries:
                    raise
                attempt += 1
                delay = initial_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
