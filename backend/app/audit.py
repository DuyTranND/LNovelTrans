"""Post-translation whole-book consistency audit service for Trạm Dịch.

Performs cross-chapter entity and terminology consistency audits across translated
segments, maintaining a unified source-term ledger, detecting naming divergences
and glossary conflicts, with persistent checkpoints, resumability, and sanitized errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from backend.app.db import (
    append_audit_findings,
    get_adjudication_checkpoints,
    get_audit_checkpoints,
    get_audit_findings,
    get_audit_run,
    get_book,
    get_book_chapters,
    get_chapter,
    get_connection,
    get_semantic_checkpoints,
    list_glossary,
    save_adjudication_checkpoint,
    save_audit_checkpoint,
    save_audit_findings,
    save_semantic_checkpoint,
    seed_checkpoints_from_prior_runs,
    update_audit_run,
    utc_now,
)
from backend.app.jobs import is_transient_network_or_server_error
from backend.app.provider import (
    ALL_KOREAN_PARTICLES,
    ProviderAuthError,
    ProviderError,
    ProviderModelError,
    ProviderRateLimitError,
    derive_conservative_headword,
    match_korean_term_in_text,
    sanitize_secret,
)

logger = logging.getLogger(__name__)

AUDIT_MAX_BATCH_SEGMENTS = 30
AUDIT_MAX_BATCH_CHARS = 4500
AUDIT_TRANSIENT_MAX_RETRIES = 3
AUDIT_TRANSIENT_INITIAL_DELAY = 1.0
AUDIT_MAX_CONSECUTIVE_FAILURES = 3
CHECKPOINT_META_SUCCESS = "__checkpoint_success__"

SEMANTIC_429_MIN_COOLDOWN = 60.0
SEMANTIC_429_MAX_COOLDOWN = 300.0
SEMANTIC_429_MAX_RETRIES = 2
SEMANTIC_NON_429_MAX_RETRIES = 3
SEMANTIC_MAX_CONSECUTIVE_FAILURES = 3

TRAILING_INSTALLMENT_RE = re.compile(
    r"\s*[\(\uff08\[\uff3b\u3010\u3014\u3008\u300a]\s*(?:#?\d+(?:\s*[/]\s*\d+)?|[0-9\uff10-\uff19]+|[ivxldcmIVXLDCM]+)\s*[\)\uff09\]\uff3d\u3011\u3015\u3009\u300b]\s*$",
    re.IGNORECASE,
)


def normalize_title_stem(title: str | None) -> str:
    """Normalize trailing installment markers in chapter titles.
    
    Normalizes only trailing installment markers like (1), (2), （1）, [1], ［1］, 【1】,
    leaving the root source or target title stem intact.
    """
    if not title:
        return ""
    text = str(title).strip()
    stem = TRAILING_INSTALLMENT_RE.sub("", text).strip()
    return stem if stem else text


def is_h1_segment(seg: dict[str, Any]) -> bool:
    """Check if a segment corresponds to a visible <h1> heading."""
    parent_tag = str(seg.get("parent_tag") or "").strip().lower()
    dom_path = str(seg.get("dom_path") or "").lower()
    return parent_tag == "h1" or "/h1[" in dom_path or dom_path.endswith("/h1") or "/h1/" in dom_path


def is_title_segment(seg: dict[str, Any]) -> bool:
    """Check if a segment corresponds to a document <title> tag."""
    parent_tag = str(seg.get("parent_tag") or "").strip().lower()
    dom_path = str(seg.get("dom_path") or "").lower()
    return parent_tag == "title" or "/title[" in dom_path or dom_path.endswith("/title") or "/title/" in dom_path


def extract_chapter_title_entries(
    ch_meta: dict[str, Any],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract candidate chapter title entries from a chapter's translated segments.
    
    Prefers visible <h1> heading; falls back to <title> if no <h1> exists.
    Avoids double-counting identical <title>/<h1> duplicates but catches intra-chapter
    disagreements between title and heading.
    """
    ch_id = ch_meta.get("id") or ""
    ch_title = ch_meta.get("title") or f"Chương {ch_meta.get('spine_index', 0) + 1}"

    valid_segs = [
        s for s in segments
        if not s.get("is_whitespace")
        and s.get("source")
        and str(s.get("source", "")).strip()
        and s.get("translation")
        and str(s.get("translation", "")).strip()
    ]

    h1_segs = [s for s in valid_segs if is_h1_segment(s)]
    title_segs = [s for s in valid_segs if is_title_segment(s)]

    # 1. Process visible <h1> segments
    h1_entries: list[dict[str, Any]] = []
    seen_h1_signatures: set[tuple[str, str]] = set()

    for s in h1_segs:
        src = str(s.get("source", "")).strip()
        trans = str(s.get("translation", "")).strip()
        src_stem = normalize_title_stem(src)
        tgt_stem = normalize_title_stem(trans)
        if not src_stem or not tgt_stem:
            continue

        sig = (src_stem, tgt_stem)
        if sig not in seen_h1_signatures:
            seen_h1_signatures.add(sig)
            h1_entries.append({
                "chapter_id": ch_id,
                "chapter_title": ch_title,
                "segment_id": s.get("id", ""),
                "source": src,
                "translation": trans,
                "source_stem": src_stem,
                "target_stem": tgt_stem,
                "tag": "h1",
            })

    # 2. Process <title> segments
    # If no <h1> segments in this chapter: fallback to <title>
    if not h1_entries:
        fallback_entries: list[dict[str, Any]] = []
        seen_title_signatures: set[tuple[str, str]] = set()
        for s in title_segs:
            src = str(s.get("source", "")).strip()
            trans = str(s.get("translation", "")).strip()
            src_stem = normalize_title_stem(src)
            tgt_stem = normalize_title_stem(trans)
            if not src_stem or not tgt_stem:
                continue

            sig = (src_stem, tgt_stem)
            if sig not in seen_title_signatures:
                seen_title_signatures.add(sig)
                fallback_entries.append({
                    "chapter_id": ch_id,
                    "chapter_title": ch_title,
                    "segment_id": s.get("id", ""),
                    "source": src,
                    "translation": trans,
                    "source_stem": src_stem,
                    "target_stem": tgt_stem,
                    "tag": "title",
                })
        return fallback_entries

    # If <h1> segments exist:
    # Avoid double-counting identical <title>/<h1> duplicates but catch intra-chapter disagreement.
    results = list(h1_entries)
    seen_intra_disagreements: set[tuple[str, str]] = set()

    for s in title_segs:
        src = str(s.get("source", "")).strip()
        trans = str(s.get("translation", "")).strip()
        src_stem = normalize_title_stem(src)
        tgt_stem = normalize_title_stem(trans)
        if not src_stem or not tgt_stem:
            continue

        # Check if there is an h1 with the same source stem
        matching_h1 = [h for h in h1_entries if h["source_stem"] == src_stem]
        if matching_h1:
            # If any matching h1 has identical target stem -> identical duplicate, do NOT double-count!
            # If target stem differs from all matching h1 -> intra-chapter disagreement!
            if all(h["target_stem"] != tgt_stem for h in matching_h1):
                sig = (src_stem, tgt_stem)
                if sig not in seen_intra_disagreements:
                    seen_intra_disagreements.add(sig)
                    results.append({
                        "chapter_id": ch_id,
                        "chapter_title": ch_title,
                        "segment_id": s.get("id", ""),
                        "source": src,
                        "translation": trans,
                        "source_stem": src_stem,
                        "target_stem": tgt_stem,
                        "tag": "title",
                    })

    return results


def detect_title_inconsistencies(
    chapters: list[dict[str, Any]],
    db_path: Path | str,
    glossary: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Detect cross-chapter title inconsistencies deterministically.
    
    Prefers visible <h1> titles, falling back to <title> if no <h1> exists.
    Normalizes trailing installment markers such as (1), (2), （1）, [1].
    Groups by normalized source title stem and flags divergent target stems or glossary conflicts.
    Avoids double-counting identical <title>/<h1> duplicates but catches intra-chapter disagreements.
    """
    glossary = glossary or []
    glossary_by_src_exact: dict[str, dict[str, Any]] = {
        g["source"].strip(): g for g in glossary if g.get("source")
    }
    glossary_by_src_lower: dict[str, dict[str, Any]] = {
        g["source"].strip().lower(): g for g in glossary if g.get("source")
    }

    # 1. Collect all candidate title entries across all chapters
    all_entries: list[dict[str, Any]] = []
    for ch in chapters:
        ch_data = get_chapter(db_path, ch["id"])
        if not ch_data:
            continue
        segs = ch_data.get("segments") or []
        ch_entries = extract_chapter_title_entries(ch, segs)
        all_entries.extend(ch_entries)

    # 2. Group by normalized source title stem
    by_source_stem: dict[str, list[dict[str, Any]]] = {}
    for entry in all_entries:
        by_source_stem.setdefault(entry["source_stem"], []).append(entry)

    findings: list[dict[str, Any]] = []

    for source_stem, occurrences in by_source_stem.items():
        # Group occurrences by normalized target stem
        variants_map: dict[str, list[dict[str, Any]]] = {}
        for occ in occurrences:
            tgt = occ["target_stem"]
            variants_map.setdefault(tgt, []).append(occ)

        # Check glossary match
        glossary_match = glossary_by_src_exact.get(source_stem) or glossary_by_src_lower.get(source_stem.lower())
        glossary_target = glossary_match["target"].strip() if glossary_match else None
        glossary_locked = bool(glossary_match["locked"]) if glossary_match else False

        has_multiple_variants = len(variants_map) > 1
        has_glossary_divergence = False
        if glossary_target:
            normalized_glossary_target = normalize_title_stem(glossary_target)
            if any(normalize_title_stem(tgt) != normalized_glossary_target for tgt in variants_map):
                has_glossary_divergence = True

        # If consistent with single variant and no glossary conflict, no finding
        if not has_multiple_variants and not has_glossary_divergence:
            continue

        variant_counts = {tgt: len(occs) for tgt, occs in variants_map.items()}
        sorted_variants = sorted(variant_counts.items(), key=lambda x: x[1], reverse=True)
        total_occurrences_count = sum(variant_counts.values())

        if glossary_locked and glossary_target:
            canonical_rec = glossary_target
            reason = f"Tiêu đề chương đã bị khóa trong từ điển: '{glossary_target}'."
            confidence = "high"
            severity = "high"
        elif glossary_target:
            canonical_rec = glossary_target
            reason = f"Tiêu đề chương có trong từ điển sách: '{glossary_target}'."
            confidence = "high"
            severity = "medium"
        else:
            best_tgt, best_count = sorted_variants[0]
            ratio = best_count / max(1, total_occurrences_count)
            canonical_rec = best_tgt
            reason = f"Biến thể tiêu đề phổ biến nhất trong sách ({best_count}/{total_occurrences_count} lần xuất hiện)."
            if ratio >= 0.7:
                confidence = "high"
            elif ratio >= 0.5:
                confidence = "medium"
            else:
                confidence = "low"

            if len(variants_map) >= 3 or ratio < 0.5:
                severity = "high"
            else:
                severity = "medium"

        variants_list = [
            {
                "target": tgt,
                "count": count,
                "sample_locations": [
                    {
                        "chapter_id": occ["chapter_id"],
                        "chapter_title": occ["chapter_title"],
                        "segment_id": occ["segment_id"],
                    }
                    for occ in variants_map[tgt][:5]
                ],
            }
            for tgt, count in sorted_variants
        ]

        occurrences_list = [
            {
                "chapter_id": occ["chapter_id"],
                "chapter_title": occ["chapter_title"],
                "segment_id": occ["segment_id"],
                "target_variant": occ["target_stem"],
                "source_snippet": occ["source"],
                "target_snippet": occ["translation"],
            }
            for occ in occurrences
        ]

        findings.append({
            "category": "title",
            "source_term": source_stem,
            "source_term_id": f"title:{source_stem}",
            "canonical_recommendation": canonical_rec,
            "reason": reason,
            "confidence": confidence,
            "severity": severity,
            "glossary_divergence": has_glossary_divergence,
            "glossary_target": glossary_target,
            "glossary_locked": glossary_locked,
            "variants": variants_list,
            "occurrences": occurrences_list,
        })

    return findings


def load_audit_findings_for_run(db_path: Path | str, run_id: str) -> list[dict[str, Any]]:
    """Load existing persisted findings for an audit run."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                category, source_term, source_term_id,
                canonical_recommendation, reason, confidence, severity,
                glossary_divergence, glossary_target, glossary_locked,
                variants, occurrences
            FROM consistency_audit_findings
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
        findings: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["glossary_divergence"] = bool(d["glossary_divergence"])
            d["glossary_locked"] = bool(d["glossary_locked"])
            if isinstance(d.get("variants"), str):
                try:
                    d["variants"] = json.loads(d["variants"])
                except Exception:
                    d["variants"] = []
            elif not isinstance(d.get("variants"), list):
                d["variants"] = []

            if isinstance(d.get("occurrences"), str):
                try:
                    d["occurrences"] = json.loads(d["occurrences"])
                except Exception:
                    d["occurrences"] = []
            elif not isinstance(d.get("occurrences"), list):
                d["occurrences"] = []

            findings.append(d)
        return findings


def merge_audit_findings(
    base_findings: list[dict[str, Any]],
    deterministic_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge deterministic findings with base findings, deduping by source_term_id.
    
    Deterministic findings take precedence over base findings with the same source_term_id.
    """
    merged = list(base_findings)
    existing_source_term_ids = {f["source_term_id"] for f in merged}
    for df in deterministic_findings:
        if df["source_term_id"] in existing_source_term_ids:
            for idx, af in enumerate(merged):
                if af["source_term_id"] == df["source_term_id"]:
                    merged[idx] = df
                    break
        else:
            merged.append(df)
            existing_source_term_ids.add(df["source_term_id"])
    return merged


def is_systemic_audit_error(exc: Exception | None) -> bool:
    """Determine whether an error is systemic (auth, permission, 401, 403, quota, model denial).
    
    Systemic errors should terminate the audit run immediately rather than
    burning further requests across thousands of segments.
    """
    if exc is None:
        return False
    if isinstance(exc, (ProviderAuthError, ProviderModelError)):
        return True
    if isinstance(exc, ProviderError) and getattr(exc, "status_code", None) in (401, 403, 404):
        return True
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    err_str = (
        str(exc) + " " + str(getattr(exc, "details", "")) + " " + str(getattr(exc, "message", ""))
    ).lower()
    systemic_keywords = (
        "401",
        "403",
        "unauthorized",
        "unauthenticated",
        "permission",
        "permission_denied",
        "forbidden",
        "api key",
        "api_key",
        "invalid api key",
        "model not found",
        "model denied",
        "not supported for this model",
        "project denied access",
        "resource has been exhausted",
        "quota exceeded",
        "quota",
        "account suspended",
        "billing",
    )
    return any(kw in err_str for kw in systemic_keywords)


def is_rate_limit_or_quota_error(exc: Exception | None) -> tuple[bool, float | None, bool]:
    """Detect whether an error is a rate limit or quota exhaustion error.
    
    Returns:
        tuple[is_rate_limit, retry_after_seconds, is_daily_quota]
    """
    if exc is None:
        return False, None, False

    is_rl = False
    retry_after: float | None = None
    is_daily = False

    # 1. Direct type match
    if isinstance(exc, ProviderRateLimitError):
        is_rl = True
        retry_after = getattr(exc, "retry_after", None)

    # 2. HTTP status code check
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        is_rl = True
    elif status_code == 403:
        details = getattr(exc, "details", None)
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and item.get("reason") in {
                    "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED"
                }:
                    is_rl = True
                    break

    # 3. String content checks
    err_str = (
        str(exc) + " " + str(getattr(exc, "details", "")) + " " + str(getattr(exc, "message", ""))
    ).lower()

    rl_keywords = (
        "429",
        "rate limit",
        "rate_limit",
        "resource_exhausted",
        "resource has been exhausted",
        "quota exceeded",
        "quota_exceeded",
        "too many requests",
        "exceeded your current quota",
    )
    if any(kw in err_str for kw in rl_keywords):
        is_rl = True

    # 4. Check for daily quota exhaustion
    daily_keywords = (
        "generations per day",
        "daily quota",
        "per day",
        "per_day",
        "free_tier_requests_per_day",
        "daily limit",
        "day limit",
    )
    if any(kw in err_str for kw in daily_keywords):
        is_daily = True
        is_rl = True

    # 5. Extract retry-after if not yet found
    if retry_after is None:
        attr_ra = getattr(exc, "retry_after", None)
        if isinstance(attr_ra, (int, float)) and attr_ra > 0:
            retry_after = float(attr_ra)
        else:
            match = re.search(r"(?:retry[-_]?after|retrydelay)[:=\s]+(\d+(?:\.\d+)?)\s*s?", err_str)
            if match:
                try:
                    val = float(match.group(1))
                    if val > 0:
                        retry_after = val
                except ValueError:
                    pass

    return is_rl, retry_after, is_daily


def batch_audit_segments(
    segments: list[dict[str, Any]],
    max_segments: int = AUDIT_MAX_BATCH_SEGMENTS,
    max_chars: int = AUDIT_MAX_BATCH_CHARS,
) -> list[list[dict[str, Any]]]:
    """Partition translated segments into bounded batches respecting segment count and character limits.
    
    Oversized segments are partitioned into single-item batches to guarantee coverage.
    """
    batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_chars = 0

    for seg in segments:
        src = seg.get("source") or ""
        trans = seg.get("translation") or ""
        seg_chars = len(src) + len(trans)

        # If adding this segment exceeds count or char limit (and current batch is non-empty)
        if current_batch and (
            len(current_batch) >= max_segments or (current_chars + seg_chars > max_chars)
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0

        current_batch.append(seg)
        current_chars += seg_chars

        # If a single segment is oversized, flush it immediately as its own batch
        if seg_chars >= max_chars and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_chars = 0

    if current_batch:
        batches.append(current_batch)

    return batches


def cross_check_glossary_occurrences(
    planned_batches: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    existing_raw_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministically cross-check exact occurrences of known glossary source terms against translations.
    
    Operates even if AI extraction returned no entities for those segments.
    Preserves locked glossary priority, merges duplicate evidence safely by verifying
    against existing segment evidence keys, and uses conservative exact substring matching.
    """
    if not glossary or not planned_batches:
        return []

    # Map existing evidence to prevent duplicate occurrences
    existing_evidence: set[tuple[str, str]] = set()
    for occ in existing_raw_findings:
        sid = str(occ.get("segment_id") or "").strip()
        sterm = str(occ.get("source_term") or "").strip()
        csrc = str(occ.get("canonical_source") or "").strip() or derive_conservative_headword(sterm)
        if sid and sterm:
            existing_evidence.add((sid, sterm))
        if sid and csrc:
            existing_evidence.add((sid, csrc))

    deterministic_findings: list[dict[str, Any]] = []

    for g in glossary:
        g_src = str(g.get("source") or "").strip()
        g_tgt = str(g.get("target") or "").strip()
        if not g_src or not g_tgt:
            continue
        g_cat = str(g.get("category") or "term").strip().lower()
        if g_cat not in ("character", "location", "title", "term", "organization"):
            g_cat = "term"
        canon_g_src = derive_conservative_headword(g_src)

        for batch in planned_batches:
            ch_id = batch.get("chapter_id") or ""
            ch_title = batch.get("chapter_title") or ""
            for seg in batch.get("segments", []):
                sid = str(seg.get("id") or "").strip()
                src = str(seg.get("source") or "")
                trans = str(seg.get("translation") or "")
                if not sid or not src or not trans:
                    continue

                # Boundary-aware Korean term matching to prevent substring false-positives
                matched_korean_str = match_korean_term_in_text(src, g_src)
                if not matched_korean_str:
                    continue

                # Merge duplicate evidence safely: skip if already extracted for this segment & term
                if (
                    (sid, g_src) in existing_evidence
                    or (sid, canon_g_src) in existing_evidence
                    or (sid, matched_korean_str) in existing_evidence
                ):
                    continue

                existing_evidence.add((sid, g_src))
                existing_evidence.add((sid, canon_g_src))
                existing_evidence.add((sid, matched_korean_str))

                # Check if target is present in translation
                if g_tgt in trans:
                    target_variant = g_tgt
                else:
                    # Target is absent: glossary divergence! Do not guess from unrelated phrases
                    target_variant = "[Cần xác minh bản dịch]"

                deterministic_findings.append({
                    "segment_id": sid,
                    "chapter_id": ch_id,
                    "chapter_title": ch_title,
                    "source_term": matched_korean_str,
                    "target_term": target_variant,
                    "canonical_source": canon_g_src,
                    "category": g_cat,
                    "confidence": "high" if target_variant == g_tgt else "low",
                    "source_snippet": src[:150],
                    "target_snippet": trans[:150],
                    "from_glossary_cross_check": True,
                    "manual_review_required": target_variant != g_tgt,
                })

    return deterministic_findings


def _persist_early_failure_findings(
    db_path: Path | str,
    run_id: str,
    book_id: str,
    planned_batches: list[dict[str, Any]],
    book_glossary: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
) -> None:
    """Persist deterministic known-glossary mismatches and title findings early before aborting."""
    try:
        title_findings = detect_title_inconsistencies(chapters, db_path, book_glossary)
        existing = load_audit_findings_for_run(db_path, run_id)
        non_title = [
            f for f in existing
            if not f.get("source_term_id", "").startswith("title:") and f.get("category") != "title"
        ]
        merged = merge_audit_findings(non_title, title_findings)

        if book_glossary and planned_batches:
            raw_glossary_findings = cross_check_glossary_occurrences(planned_batches, book_glossary, [])
            if raw_glossary_findings:
                by_canon: dict[str, list[dict[str, Any]]] = {}
                for occ in raw_glossary_findings:
                    c = occ.get("canonical_source") or occ.get("source_term") or ""
                    by_canon.setdefault(c, []).append(occ)

                glossary_findings: list[dict[str, Any]] = []
                for csrc, occs in by_canon.items():
                    cat = occs[0].get("category") or "term"
                    v_map: dict[str, list[dict[str, Any]]] = {}
                    for o in occs:
                        tgt = str(o.get("target_term") or "").strip()
                        if tgt:
                            v_map.setdefault(tgt, []).append(o)

                    g_match = next(
                        (g for g in book_glossary
                         if str(g.get("source", "")).strip() == csrc
                         or derive_conservative_headword(str(g.get("source", "")).strip()) == csrc
                         or str(g.get("source", "")).strip().lower() == csrc.lower()),
                        None,
                    )
                    g_target = g_match.get("target").strip() if g_match and g_match.get("target") else None
                    g_locked = bool(g_match.get("locked")) if g_match else False

                    has_multiple_variants = len(v_map) > 1
                    has_glossary_divergence = False
                    if g_target:
                        if any(tgt != g_target for tgt in v_map):
                            has_glossary_divergence = True

                    has_unresolved = any(
                        tgt.startswith("[") or "xác minh" in tgt.lower()
                        for tgt in v_map
                    ) or any(
                        str(o.get("target_term", "")).startswith("[")
                        or "xác minh" in str(o.get("target_term", "")).lower()
                        or bool(o.get("manual_review_required"))
                        for o in occs
                    )

                    if not has_multiple_variants and not has_glossary_divergence and not has_unresolved:
                        continue

                    variant_counts = {tgt: len(items) for tgt, items in v_map.items()}
                    sorted_variants = sorted(variant_counts.items(), key=lambda x: x[1], reverse=True)
                    real_variants = [v for v in sorted_variants if not v[0].startswith("[") and "xác minh" not in v[0].lower()]

                    is_case_only = False
                    distinct_lower = {v.casefold() for v in v_map}
                    if len(v_map) > 1 and len(distinct_lower) == 1:
                        is_case_only = True
                    elif len(v_map) == 1 and g_target:
                        only_tgt = list(v_map.keys())[0]
                        if only_tgt != g_target and only_tgt.casefold() == g_target.casefold():
                            is_case_only = True

                    if g_locked and g_target:
                        canonical_rec = g_target
                        reason = f"Thuật ngữ đã bị khóa trong từ điển: '{g_target}'."
                        confidence = "high"
                        severity = "high"
                    elif g_target:
                        canonical_rec = g_target
                        reason = f"Thuật ngữ có trong từ điển sách: '{g_target}'."
                        confidence = "high"
                        severity = "medium"
                    elif real_variants:
                        best_tgt, best_count = real_variants[0]
                        canonical_rec = best_tgt
                        reason = f"Biến thể phổ biến nhất trong sách ({best_count}/{len(occs)} lần xuất hiện)."
                        confidence = "medium"
                        severity = "low" if is_case_only else "medium"
                    else:
                        best_tgt, best_count = sorted_variants[0] if sorted_variants else ("[Cần xác minh bản dịch]", 0)
                        canonical_rec = best_tgt
                        reason = "Cần biên tập viên xác minh bản dịch cho thuật ngữ này."
                        confidence = "low"
                        severity = "medium"

                    variants_list = [
                        {
                            "target": tgt,
                            "count": count,
                            "sample_locations": [
                                {
                                    "chapter_id": i["chapter_id"],
                                    "chapter_title": i["chapter_title"],
                                    "segment_id": i["segment_id"],
                                }
                                for i in v_map[tgt][:5]
                            ],
                        }
                        for tgt, count in sorted_variants
                    ]
                    occs_list = [
                        {
                            "chapter_id": o["chapter_id"],
                            "chapter_title": o["chapter_title"],
                            "segment_id": o["segment_id"],
                            "source_term": o.get("source_term", csrc),
                            "target_variant": o.get("target_term", ""),
                            "source_snippet": o.get("source_snippet", ""),
                            "target_snippet": o.get("target_snippet", ""),
                        }
                        for o in occs
                    ]
                    glossary_findings.append({
                        "category": cat,
                        "source_term": csrc,
                        "source_term_id": f"{cat}:{csrc}",
                        "canonical_recommendation": canonical_rec,
                        "reason": reason,
                        "confidence": confidence,
                        "severity": severity,
                        "glossary_divergence": has_glossary_divergence,
                        "glossary_target": g_target,
                        "glossary_locked": g_locked,
                        "is_case_only": is_case_only,
                        "case_only": is_case_only,
                        "variants": variants_list,
                        "occurrences": occs_list,
                    })
                merged = merge_audit_findings(merged, glossary_findings)

        save_audit_findings(db_path, run_id, book_id, merged)
    except Exception as exc:
        logger.warning("Failed to persist early failure deterministic findings: %s", exc)


def discover_missed_global_term_occurrences(
    planned_batches: list[dict[str, Any]],
    all_raw_findings: list[dict[str, Any]],
    glossary: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Discover missed global term occurrences across all translated segments in the book.

    Builds a registry of canonical terms from source-grounded extracted findings
    and known glossary items, with category and all observed target phrase variants.
    Uses match_korean_term_in_text(text, term) to avoid substring false matches.
    For each segment source containing a registered term not already extracted for that
    segment and canonical term:
    - Infers target ONLY if exactly one distinct known target variant actually occurs
      in translation (casefold and whitespace normalized; longest phrase on overlaps).
    - If no known variant or ambiguous variants occur, records '[Cần xác minh bản dịch]'
      with confidence 'low' and origin 'manual_review_required' True.
    - Preserves actual substring in translated text for matched targets with confidence 'medium'.
    - Returns raw occurrence dicts without modifying saved book translations.
    """
    glossary = glossary or []
    if not planned_batches:
        return []

    # 1. Build registry from source-grounded findings and glossary
    CATEGORY_PRIORITY = {"character": 5, "location": 4, "organization": 3, "title": 2, "term": 1}

    # Registry keyed by canonical source term
    registry: dict[str, dict[str, Any]] = {}

    # Incorporate glossary terms
    for g in glossary:
        g_src = str(g.get("source") or "").strip()
        g_tgt = str(g.get("target") or "").strip()
        if not g_src or not g_tgt:
            continue
        g_cat = str(g.get("category") or "term").strip().lower()
        if g_cat not in CATEGORY_PRIORITY:
            g_cat = "term"
        canon = derive_conservative_headword(g_src) or g_src
        if not canon:
            continue

        if canon not in registry:
            registry[canon] = {
                "canonical_source": canon,
                "category": g_cat,
                "category_counts": {},
                "target_variants": set(),
                "search_terms": set(),
                "is_glossary": True,
            }
        entry = registry[canon]
        entry["target_variants"].add(g_tgt)
        entry["search_terms"].add(canon)
        entry["search_terms"].add(g_src)
        entry["category_counts"][g_cat] = entry["category_counts"].get(g_cat, 0) + 10

    # Incorporate AI extracted findings
    for occ in all_raw_findings:
        src = str(occ.get("source_term") or "").strip()
        tgt = str(occ.get("target_term") or "").strip()
        if not src:
            continue
        canon = str(occ.get("canonical_source") or "").strip() or derive_conservative_headword(src) or src
        if not canon:
            continue
        cat = str(occ.get("category") or "term").strip().lower()
        if cat not in CATEGORY_PRIORITY:
            cat = "term"

        if canon not in registry:
            registry[canon] = {
                "canonical_source": canon,
                "category": cat,
                "category_counts": {},
                "target_variants": set(),
                "search_terms": set(),
                "is_glossary": False,
            }
        entry = registry[canon]
        entry["search_terms"].add(canon)
        entry["search_terms"].add(src)
        entry["category_counts"][cat] = entry["category_counts"].get(cat, 0) + 1
        # Add real target variants (ignore review / divergence markers)
        if tgt and not tgt.startswith("["):
            entry["target_variants"].add(tgt)

    if not registry:
        return []

    # Finalize dominant category for each registered canonical term
    for canon, entry in registry.items():
        sorted_cats = sorted(
            entry["category_counts"].items(),
            key=lambda x: (x[1], CATEGORY_PRIORITY.get(x[0], 0)),
            reverse=True,
        )
        if sorted_cats:
            entry["category"] = sorted_cats[0][0]

    # Map existing evidence: (segment_id, canonical_source) and (segment_id, source_term)
    existing_evidence: set[tuple[str, str]] = set()
    for occ in all_raw_findings:
        sid = str(occ.get("segment_id") or "").strip()
        sterm = str(occ.get("source_term") or "").strip()
        csrc = str(occ.get("canonical_source") or "").strip() or derive_conservative_headword(sterm)
        if sid and sterm:
            existing_evidence.add((sid, sterm))
        if sid and csrc:
            existing_evidence.add((sid, csrc))

    # Sort registered canonical items by descending length of search terms
    # to prioritize longer phrases and prevent false substring matches in compound words
    sorted_registry_entries = sorted(
        registry.values(),
        key=lambda e: max((len(t) for t in e["search_terms"]), default=len(e["canonical_source"])),
        reverse=True,
    )

    discovered_findings: list[dict[str, Any]] = []

    # 2. Scan all planned batches and segments
    for batch in planned_batches:
        ch_id = str(batch.get("chapter_id") or "")
        ch_title = str(batch.get("chapter_title") or "")
        for seg in batch.get("segments", []):
            sid = str(seg.get("id") or "").strip()
            source = str(seg.get("source") or "")
            trans = str(seg.get("translation") or "")
            if not sid or not source or not trans:
                continue

            # Track matched spans in source text to prevent shorter sub-terms matching inside longer compounds
            matched_source_spans: list[tuple[int, int]] = []

            for entry in sorted_registry_entries:
                canon = entry["canonical_source"]

                # Skip if already extracted/grounded in this segment for this canonical term
                if (sid, canon) in existing_evidence:
                    continue

                # Quick check: does any search term appear in source text at all?
                candidate_terms = sorted(entry["search_terms"], key=len, reverse=True)
                if not any(t in source for t in candidate_terms):
                    continue

                # Detailed boundary-aware Korean term matching
                matched_korean_str: str | None = None
                matched_span: tuple[int, int] | None = None

                for term in candidate_terms:
                    m_str = match_korean_term_in_text(source, term)
                    if m_str:
                        # Find span in source
                        idx = source.find(m_str)
                        if idx != -1:
                            span = (idx, idx + len(m_str))
                            # Check overlap with already matched longer source spans
                            if any(not (span[1] <= o[0] or span[0] >= o[1]) for o in matched_source_spans):
                                continue
                            matched_korean_str = m_str
                            matched_span = span
                            break

                if not matched_korean_str or not matched_span:
                    continue

                # Term verified in segment source without false substring match
                matched_source_spans.append(matched_span)
                existing_evidence.add((sid, canon))
                existing_evidence.add((sid, matched_korean_str))

                # 3. Target variant resolution in translation text
                known_variants = entry["target_variants"]
                matched_target_term: str | None = None
                confidence = "medium"
                manual_review_required = False

                if known_variants:
                    # Find all occurrences of known variants in translated text
                    raw_target_matches: list[dict[str, Any]] = []
                    for v in sorted(known_variants, key=len, reverse=True):
                        v_clean = v.strip()
                        if not v_clean:
                            continue
                        words = v_clean.split()
                        if not words:
                            continue
                        pattern = r"\s+".join(re.escape(w) for w in words)
                        try:
                            for m in re.finditer(pattern, trans, re.IGNORECASE):
                                raw_target_matches.append({
                                    "start": m.start(),
                                    "end": m.end(),
                                    "actual_substring": trans[m.start():m.end()],
                                    "normalized_variant": " ".join(words).casefold(),
                                })
                        except Exception:
                            continue

                    # Choose longest phrase when overlaps
                    raw_target_matches.sort(key=lambda m: (m["end"] - m["start"]), reverse=True)
                    non_overlapping: list[dict[str, Any]] = []
                    occupied_trans_spans: list[tuple[int, int]] = []

                    for m in raw_target_matches:
                        s, e = m["start"], m["end"]
                        if any(not (e <= os or s >= oe) for os, oe in occupied_trans_spans):
                            continue
                        occupied_trans_spans.append((s, e))
                        non_overlapping.append(m)

                    # Distinct known target variants that actually occur
                    distinct_variants: dict[str, dict[str, Any]] = {}
                    for m in non_overlapping:
                        norm_v = m["normalized_variant"]
                        if norm_v not in distinct_variants:
                            distinct_variants[norm_v] = m

                    if len(distinct_variants) == 1:
                        # Exactly one distinct known target variant actually occurs
                        single_match = list(distinct_variants.values())[0]
                        matched_target_term = single_match["actual_substring"]
                        confidence = "medium"
                        manual_review_required = False
                    else:
                        # 0 or >1 ambiguous variants
                        matched_target_term = "[Cần xác minh bản dịch]"
                        confidence = "low"
                        manual_review_required = True
                else:
                    matched_target_term = "[Cần xác minh bản dịch]"
                    confidence = "low"
                    manual_review_required = True

                occurrence_record: dict[str, Any] = {
                    "segment_id": sid,
                    "chapter_id": ch_id,
                    "chapter_title": ch_title,
                    "source_term": matched_korean_str,
                    "target_term": matched_target_term,
                    "canonical_source": canon,
                    "category": entry["category"],
                    "confidence": confidence,
                    "source_snippet": source,
                    "target_snippet": trans,
                    "origin": "manual_review_required" if manual_review_required else "global_term_discovery",
                    "manual_review_required": manual_review_required,
                    "from_global_discovery": True,
                }
                discovered_findings.append(occurrence_record)

    return discovered_findings


class ConsistencyAuditService:
    """Service that executes whole-book consistency audit runs."""

    def __init__(
        self,
        db_path: Path | str,
        provider_getter: Callable[[], Any],
    ) -> None:
        self.db_path = db_path
        self.provider_getter = provider_getter

    async def execute_run(
        self,
        run_id: str,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Execute or resume a consistency audit run until completion or cancellation."""
        run = get_audit_run(self.db_path, run_id)
        if not run:
            logger.error("Audit run %s not found", run_id)
            return

        if run["status"] in ("completed", "cancelled"):
            return

        book_id = run["book_id"]
        book = get_book(self.db_path, book_id)
        if not book:
            update_audit_run(
                self.db_path,
                run_id,
                status="failed",
                error=f"Không tìm thấy sách '{book_id}'.",
            )
            return

        # 1. Collect all chapters and translated non-whitespace segments in deterministic spine order
        chapters = get_book_chapters(self.db_path, book_id)
        planned_batches: list[dict[str, Any]] = []
        total_segments_count = 0
        total_chapters_with_trans = 0

        for ch in chapters:
            ch_data = get_chapter(self.db_path, ch["id"])
            if not ch_data:
                continue
            segs = ch_data.get("segments") or []
            # Only non-whitespace translated segments
            translated_segs = [
                s for s in segs
                if not s.get("is_whitespace")
                and s.get("translation")
                and str(s.get("translation", "")).strip()
            ]
            if not translated_segs:
                continue

            total_chapters_with_trans += 1
            total_segments_count += len(translated_segs)

            ch_batches = batch_audit_segments(translated_segs)
            for b in ch_batches:
                planned_batches.append({
                    "chapter_id": ch["id"],
                    "chapter_title": ch.get("title") or f"Chương {ch.get('spine_index', 0) + 1}",
                    "segments": b,
                })

        if total_segments_count == 0:
            update_audit_run(
                self.db_path,
                run_id,
                status="failed",
                error="Sách chưa có phân đoạn dịch nào để rà soát tính nhất quán.",
            )
            return

        # 2. Check existing checkpoints for resumability
        # Note on historical empty checkpoints: earlier versions could persist empty checkpoints []
        # for failed batches. In this version, only successful batches are checkpointed.
        # Checkpoints with raw_findings represent confirmed processed batches.
        existing_cps = get_audit_checkpoints(self.db_path, run_id)
        if not existing_cps:
            # Safely reuse validated checkpoints from any prior run of the same book
            seeded = seed_checkpoints_from_prior_runs(self.db_path, run_id, book_id, planned_batches)
            if seeded > 0:
                logger.info("Audit run %s seeded %d validated checkpoints from prior runs", run_id, seeded)
                existing_cps = get_audit_checkpoints(self.db_path, run_id)

        if existing_cps and run.get("processed_segments", 0) == 0 and run.get("failed_segments", 0) > 0:
            has_valid_findings = any(bool(cp.get("raw_findings")) for cp in existing_cps)
            if not has_valid_findings:
                update_audit_run(
                    self.db_path,
                    run_id,
                    status="failed",
                    error="Checkpoint cũ không đáng tin cậy do lỗi review trước đó. Hãy tạo lượt rà soát mới; không tiếp tục lượt cũ.",
                )
                return

        completed_indices: set[int] = {cp["batch_index"] for cp in existing_cps}
        all_raw_findings: list[dict[str, Any]] = []
        for cp in existing_cps:
            all_raw_findings.extend(cp.get("raw_findings") or [])

        # Calculate initial processed segments count from completed checkpoints
        processed_count = 0
        for idx in completed_indices:
            if idx < len(planned_batches):
                processed_count += len(planned_batches[idx]["segments"])

        # On resume/retry, reset failed_count for pending batches to avoid double counting.
        # Pending batches will be re-attempted and will accurately count failures if they fail again.
        failed_count = 0
        skipped_count = run.get("skipped_segments", 0)

        initial_pct = round(100.0 * processed_count / max(1, total_segments_count), 1)

        # Check current run status from DB before updating
        latest_run = get_audit_run(self.db_path, run_id)
        if latest_run and latest_run["status"] in ("cancelled", "paused"):
            return
        initial_status = latest_run["status"] if latest_run and latest_run["status"] == "pausing" else "running"

        # Update initial run stats
        update_audit_run(
            self.db_path,
            run_id,
            status=initial_status,
            total_chapters=total_chapters_with_trans,
            total_segments=total_segments_count,
            processed_segments=processed_count,
            failed_segments=failed_count,
            skipped_segments=skipped_count,
            clear_error=True,
            progress={
                "stage": "extracting",
                "total_batches": len(planned_batches),
                "completed_batches": len(completed_indices),
                "percentage": initial_pct,
                "attempted_percentage": initial_pct,
            },
        )

        book_glossary = list_glossary(self.db_path, book_id)

        # Deterministic cross-chapter title consistency and known glossary divergence detection and earliest persistence
        _persist_early_failure_findings(self.db_path, run_id, book_id, planned_batches, book_glossary, chapters)

        # Check if run was requested to pause or cancel before invoking AI provider
        if stop_event and stop_event.is_set():
            update_audit_run(self.db_path, run_id, status="cancelled")
            return

        cur_run = get_audit_run(self.db_path, run_id)
        if cur_run and cur_run["status"] == "cancelled":
            return
        if cur_run and cur_run["status"] == "pausing":
            update_audit_run(self.db_path, run_id, status="paused")
            return

        run_model = run.get("model") or "gemini-3.5-flash-lite"
        provider = self.provider_getter()
        if provider is None:
            err_msg = "Chưa cấu hình dịch vụ AI (provider unavailable)."
            _persist_early_failure_findings(self.db_path, run_id, book_id, planned_batches, book_glossary, chapters)
            success_pct = round(100.0 * processed_count / max(1, total_segments_count), 1)
            attempted_pct = round(100.0 * (processed_count + failed_count) / max(1, total_segments_count), 1)
            update_audit_run(
                self.db_path,
                run_id,
                status="failed",
                error=err_msg,
                processed_segments=processed_count,
                failed_segments=failed_count,
                skipped_segments=skipped_count,
                progress={
                    "stage": "failed",
                    "total_batches": len(planned_batches),
                    "completed_batches": len(completed_indices),
                    "percentage": success_pct,
                    "attempted_percentage": attempted_pct,
                    "error": err_msg,
                },
            )
            return

        secrets = provider.all_secrets if provider and hasattr(provider, "all_secrets") else []

        consecutive_failures = 0
        last_batch_error = None

        # 3. Process batches sequentially
        for batch_idx, batch_info in enumerate(planned_batches):
            # Check cooperative stop
            if stop_event and stop_event.is_set():
                update_audit_run(self.db_path, run_id, status="cancelled")
                return

            cur_run = get_audit_run(self.db_path, run_id)
            if cur_run and cur_run["status"] == "cancelled":
                return
            if cur_run and cur_run["status"] == "pausing":
                update_audit_run(self.db_path, run_id, status="paused")
                return

            # Already checkpointed? Skip calling provider
            if batch_idx in completed_indices:
                continue

            batch_segs = batch_info["segments"]
            ch_id = batch_info["chapter_id"]
            ch_title = batch_info["chapter_title"]
            seg_ids = [s["id"] for s in batch_segs]

            provider = self.provider_getter()
            if provider is None:
                err_msg = "Chưa cấu hình dịch vụ AI (provider unavailable)."
                _persist_early_failure_findings(self.db_path, run_id, book_id, planned_batches, book_glossary, chapters)
                success_pct = round(100.0 * processed_count / max(1, total_segments_count), 1)
                attempted_pct = round(100.0 * (processed_count + failed_count) / max(1, total_segments_count), 1)
                update_audit_run(
                    self.db_path,
                    run_id,
                    status="failed",
                    error=err_msg,
                    processed_segments=processed_count,
                    failed_segments=failed_count,
                    skipped_segments=skipped_count,
                    progress={
                        "stage": "failed",
                        "total_batches": len(planned_batches),
                        "completed_batches": len(completed_indices),
                        "percentage": success_pct,
                        "attempted_percentage": attempted_pct,
                        "error": err_msg,
                    },
                )
                return

            # Call provider with bounded retry for transient errors
            batch_findings: list[dict[str, Any]] = []
            call_success = False
            last_error: Exception | None = None

            for attempt in range(AUDIT_TRANSIENT_MAX_RETRIES):
                if stop_event and stop_event.is_set():
                    update_audit_run(self.db_path, run_id, status="cancelled")
                    return

                try:
                    batch_findings = await provider.extract_terminology(
                        batch_segs,
                        chapter_id=ch_id,
                        chapter_title=ch_title,
                        glossary=book_glossary,
                        model=run_model,
                    )
                    call_success = True
                    break
                except Exception as exc:
                    last_error = exc
                    if not is_transient_network_or_server_error(exc) or attempt == AUDIT_TRANSIENT_MAX_RETRIES - 1:
                        break
                    delay = AUDIT_TRANSIENT_INITIAL_DELAY * (2 ** attempt)
                    await asyncio.sleep(delay)

            if call_success:
                consecutive_failures = 0
                processed_count += len(batch_segs)
                all_raw_findings.extend(batch_findings)
                save_audit_checkpoint(
                    self.db_path,
                    run_id,
                    batch_idx,
                    ch_id,
                    seg_ids,
                    batch_findings,
                )
                completed_indices.add(batch_idx)
            else:
                consecutive_failures += 1
                failed_count += len(batch_segs)
                last_batch_error = last_error
                clean_err = sanitize_secret(str(last_error or "Unknown error"), secrets)
                logger.warning(
                    "Audit run %s batch %d failed: %s (consecutive failures: %d)",
                    run_id,
                    batch_idx,
                    clean_err,
                    consecutive_failures,
                )

                # 1. Immediate termination on systemic error (401, 403, permission, quota, invalid model)
                if is_systemic_audit_error(last_error):
                    diagnostic_err = f"Lỗi hệ thống từ AI provider: {clean_err}"
                    logger.error("Systemic audit error for run %s: %s", run_id, diagnostic_err)
                    _persist_early_failure_findings(self.db_path, run_id, book_id, planned_batches, book_glossary, chapters)
                    pct = round(100.0 * processed_count / max(1, total_segments_count), 1)
                    attempted_pct = round(100.0 * (processed_count + failed_count) / max(1, total_segments_count), 1)
                    update_audit_run(
                        self.db_path,
                        run_id,
                        status="failed",
                        error=clean_err,
                        processed_segments=processed_count,
                        failed_segments=failed_count,
                        skipped_segments=skipped_count,
                        progress={
                            "stage": "failed",
                            "current_chapter_id": ch_id,
                            "current_chapter_title": ch_title,
                            "total_batches": len(planned_batches),
                            "completed_batches": len(completed_indices),
                            "percentage": pct,
                            "attempted_percentage": attempted_pct,
                            "error": clean_err,
                        },
                    )
                    return

                # 2. Enforce AUDIT_MAX_CONSECUTIVE_FAILURES for repeated network/timeout failures
                if consecutive_failures >= AUDIT_MAX_CONSECUTIVE_FAILURES:
                    streak_err = (
                        f"Đã đạt giới hạn {AUDIT_MAX_CONSECUTIVE_FAILURES} lần thất bại liên tiếp "
                        f"(lỗi gần nhất: {clean_err}). Dừng tiến trình rà soát để bảo vệ hạn ngạch."
                    )
                    logger.error("Run %s exceeded consecutive failure threshold: %s", run_id, streak_err)
                    _persist_early_failure_findings(self.db_path, run_id, book_id, planned_batches, book_glossary, chapters)
                    pct = round(100.0 * processed_count / max(1, total_segments_count), 1)
                    attempted_pct = round(100.0 * (processed_count + failed_count) / max(1, total_segments_count), 1)
                    update_audit_run(
                        self.db_path,
                        run_id,
                        status="failed",
                        error=clean_err,
                        processed_segments=processed_count,
                        failed_segments=failed_count,
                        skipped_segments=skipped_count,
                        progress={
                            "stage": "failed",
                            "current_chapter_id": ch_id,
                            "current_chapter_title": ch_title,
                            "total_batches": len(planned_batches),
                            "completed_batches": len(completed_indices),
                            "percentage": pct,
                            "attempted_percentage": attempted_pct,
                            "error": clean_err,
                        },
                    )
                    return

            # Update progress: percentage reflects success only, never includes failures as success
            success_pct = round(100.0 * processed_count / max(1, total_segments_count), 1)
            attempted_pct = round(100.0 * (processed_count + failed_count) / max(1, total_segments_count), 1)
            update_audit_run(
                self.db_path,
                run_id,
                processed_segments=processed_count,
                failed_segments=failed_count,
                skipped_segments=skipped_count,
                progress={
                    "stage": "extracting",
                    "current_chapter_id": ch_id,
                    "current_chapter_title": ch_title,
                    "total_batches": len(planned_batches),
                    "completed_batches": len(completed_indices),
                    "percentage": min(100.0, success_pct),
                    "attempted_percentage": min(100.0, attempted_pct),
                },
            )

        # Fail run if 0 segments succeeded
        if processed_count == 0 and total_segments_count > 0:
            _persist_early_failure_findings(self.db_path, run_id, book_id, planned_batches, book_glossary, chapters)
            err_msg = f"Rà soát không thành công: không có phân đoạn nào được xử lý ({failed_count}/{total_segments_count} phân đoạn lỗi)."
            update_audit_run(
                self.db_path,
                run_id,
                status="failed",
                error=err_msg,
                processed_segments=0,
                failed_segments=failed_count,
                skipped_segments=skipped_count,
                progress={
                    "stage": "failed",
                    "total_batches": len(planned_batches),
                    "completed_batches": 0,
                    "percentage": 0.0,
                    "attempted_percentage": round(100.0 * failed_count / max(1, total_segments_count), 1),
                    "error": err_msg,
                },
            )
            return

        # 4. Global Term Discovery across all planned batches
        missed_global_findings = discover_missed_global_term_occurrences(
            planned_batches,
            all_raw_findings,
            book_glossary,
        )
        if missed_global_findings:
            all_raw_findings.extend(missed_global_findings)

        # 5. Deterministic Known-Glossary Cross-Checking
        deterministic_glossary_findings = cross_check_glossary_occurrences(
            planned_batches,
            book_glossary,
            all_raw_findings,
        )
        if deterministic_glossary_findings:
            all_raw_findings.extend(deterministic_glossary_findings)

        # 5. Cross-Chapter Source-Term Ledger Aggregation
        agg_pct = round(100.0 * processed_count / max(1, total_segments_count), 1)
        update_audit_run(
            self.db_path,
            run_id,
            progress={
                "stage": "aggregating",
                "total_batches": len(planned_batches),
                "completed_batches": len(completed_indices),
                "percentage": min(100.0, agg_pct),
                "attempted_percentage": min(100.0, round(100.0 * (processed_count + failed_count) / max(1, total_segments_count), 1)),
            },
        )

        glossary_by_src_exact: dict[str, dict[str, Any]] = {
            g["source"].strip(): g for g in book_glossary if g.get("source")
        }
        glossary_by_src_lower: dict[str, dict[str, Any]] = {
            g["source"].strip().lower(): g for g in book_glossary if g.get("source")
        }
        known_glossary_sources = list(glossary_by_src_exact.keys())

        # Group raw occurrences by grounded canonical source identity
        by_canonical: dict[str, list[dict[str, Any]]] = {}
        for occ in all_raw_findings:
            src = str(occ.get("source_term") or "").strip()
            if not src:
                continue
            canon = str(occ.get("canonical_source") or "").strip() or derive_conservative_headword(src, known_glossary_sources)
            if not canon:
                canon = src
            by_canonical.setdefault(canon, []).append(occ)

        # Build ledger using dominant category for each canonical source identity
        ledger: dict[str, list[dict[str, Any]]] = {}
        for canon, occurrences in by_canonical.items():
            g_match = glossary_by_src_exact.get(canon) or glossary_by_src_lower.get(canon.lower())
            if g_match and g_match.get("category"):
                dominant_cat = str(g_match["category"]).strip().lower()
            else:
                cat_counts: dict[str, int] = {}
                for occ in occurrences:
                    c = str(occ.get("category") or "term").strip().lower()
                    cat_counts[c] = cat_counts.get(c, 0) + 1
                priority = {"character": 5, "location": 4, "organization": 3, "title": 2, "term": 1}
                sorted_cats = sorted(
                    cat_counts.items(),
                    key=lambda x: (x[1], priority.get(x[0], 0)),
                    reverse=True,
                )
                dominant_cat = sorted_cats[0][0] if sorted_cats else "term"

            key = f"{dominant_cat}:{canon}"
            ledger[key] = occurrences

        aggregated_findings: list[dict[str, Any]] = []

        for key, occurrences in ledger.items():
            cat = key.split(":", 1)[0]
            canon_src = key.split(":", 1)[1]

            # Ensure no double counting based on chapter/segment/source
            seen_evidence_keys: set[tuple[str, str, str]] = set()
            deduped_occurrences: list[dict[str, Any]] = []
            for occ in occurrences:
                evidence_key = (
                    str(occ.get("chapter_id") or "").strip(),
                    str(occ.get("segment_id") or "").strip(),
                    str(occ.get("source_term") or "").strip(),
                )
                if evidence_key in seen_evidence_keys:
                    continue
                seen_evidence_keys.add(evidence_key)
                deduped_occurrences.append(occ)

            occurrences = deduped_occurrences

            # Group occurrences by target variant
            variants_map: dict[str, list[dict[str, Any]]] = {}
            for occ in occurrences:
                tgt = str(occ.get("target_term") or "").strip()
                if tgt:
                    variants_map.setdefault(tgt, []).append(occ)

            if not variants_map:
                continue

            # Check glossary match: by canonical headword or by any occurrence's source term
            glossary_match = glossary_by_src_exact.get(canon_src) or glossary_by_src_lower.get(canon_src.lower())
            if not glossary_match:
                for occ in occurrences:
                    st = str(occ.get("source_term") or "").strip()
                    g_m = glossary_by_src_exact.get(st) or glossary_by_src_lower.get(st.lower())
                    if g_m:
                        glossary_match = g_m
                        break

            glossary_target = glossary_match["target"].strip() if glossary_match else None
            glossary_locked = bool(glossary_match["locked"]) if glossary_match else False

            # Distinguish case-only variants
            distinct_lower_variants = {v.casefold() for v in variants_map}
            is_case_only = False
            if len(variants_map) > 1 and len(distinct_lower_variants) == 1:
                is_case_only = True
            elif len(variants_map) == 1 and glossary_target:
                only_tgt = list(variants_map.keys())[0]
                if only_tgt != glossary_target and only_tgt.casefold() == glossary_target.casefold():
                    is_case_only = True

            # Check for inconsistency
            has_multiple_variants = len(variants_map) > 1
            has_glossary_divergence = False
            if glossary_target:
                # Any target variant differs from glossary target
                if any(tgt != glossary_target for tgt in variants_map):
                    has_glossary_divergence = True

            has_unresolved_marker = any(
                tgt.startswith("[") or "xác minh" in tgt.lower()
                for tgt in variants_map
            ) or any(
                str(occ.get("target_term", "")).startswith("[")
                or "xác minh" in str(occ.get("target_term", "")).lower()
                or bool(occ.get("manual_review_required"))
                for occ in occurrences
            )

            # If perfectly consistent with single variant, no unresolved markers, and matches glossary (or no glossary entry) -> no finding
            if not has_multiple_variants and not has_glossary_divergence and not has_unresolved_marker:
                continue

            # Inconsistency detected: compute canonical recommendation and severity
            variant_counts = {tgt: len(occs) for tgt, occs in variants_map.items()}
            sorted_variants = sorted(variant_counts.items(), key=lambda x: x[1], reverse=True)
            total_occurrences_count = sum(variant_counts.values())

            # Filter out review flags like [Cần xác minh bản dịch] when picking best canonical recommendation
            real_variants = [v for v in sorted_variants if not v[0].startswith("[") and "xác minh" not in v[0].lower()]

            if glossary_locked and glossary_target:
                canonical_rec = glossary_target
                reason = f"Thuật ngữ đã bị khóa trong từ điển: '{glossary_target}'."
                confidence = "high"
                severity = "high"
            elif glossary_target:
                canonical_rec = glossary_target
                reason = f"Thuật ngữ có trong từ điển sách: '{glossary_target}'."
                confidence = "high"
                severity = "medium"
            elif real_variants:
                best_tgt, best_count = real_variants[0]
                ratio = best_count / max(1, total_occurrences_count)
                canonical_rec = best_tgt
                if is_case_only:
                    reason = f"Biến thể chỉ khác biệt về chữ hoa/chữ thường ({best_count}/{total_occurrences_count} lần xuất hiện)."
                else:
                    reason = f"Biến thể phổ biến nhất trong sách ({best_count}/{total_occurrences_count} lần xuất hiện)."
                if ratio >= 0.7:
                    confidence = "high"
                elif ratio >= 0.5:
                    confidence = "medium"
                else:
                    confidence = "low"

                if is_case_only:
                    severity = "low"
                elif len(variants_map) >= 3 or ratio < 0.5:
                    severity = "high"
                else:
                    severity = "medium"
            else:
                best_tgt, best_count = sorted_variants[0]
                canonical_rec = best_tgt
                reason = "Cần biên tập viên xác minh bản dịch cho thuật ngữ này."
                confidence = "low"
                severity = "medium"

            # Construct structured variants JSON
            variants_list = [
                {
                    "target": tgt,
                    "count": count,
                    "sample_locations": [
                        {
                            "chapter_id": occ.get("chapter_id", ""),
                            "chapter_title": occ.get("chapter_title", ""),
                            "segment_id": occ.get("segment_id", ""),
                        }
                        for occ in variants_map[tgt][:5]
                    ],
                }
                for tgt, count in sorted_variants
            ]

            # Construct structured occurrences evidence list (retaining exact source spans)
            occurrences_list = [
                {
                    "chapter_id": occ.get("chapter_id", ""),
                    "chapter_title": occ.get("chapter_title", ""),
                    "segment_id": occ.get("segment_id", ""),
                    "source_term": occ.get("source_term", canon_src),
                    "target_variant": occ.get("target_term", ""),
                    "source_snippet": occ.get("source_snippet", ""),
                    "target_snippet": occ.get("target_snippet", ""),
                }
                for occ in occurrences
            ]

            aggregated_findings.append({
                "category": cat,
                "source_term": canon_src,
                "source_term_id": key,
                "canonical_recommendation": canonical_rec,
                "reason": reason,
                "confidence": confidence,
                "severity": severity,
                "glossary_divergence": has_glossary_divergence,
                "glossary_target": glossary_target,
                "glossary_locked": glossary_locked,
                "is_case_only": is_case_only,
                "case_only": is_case_only,
                "variants": variants_list,
                "occurrences": occurrences_list,
            })


        # Deterministic cross-chapter title consistency detection and merging
        title_findings = detect_title_inconsistencies(chapters, self.db_path, book_glossary)
        aggregated_findings = merge_audit_findings(aggregated_findings, title_findings)

        # Contextual AI Adjudication for ambiguous candidates (Task B)
        had_adj_failure = False
        total_adj_candidates = 0
        completed_adj_batches_count = 0
        total_adj_batches = 0

        if provider and hasattr(provider, "adjudicate_inconsistencies"):
            ambiguous_candidates: list[dict[str, Any]] = []
            for f in aggregated_findings:
                if not f.get("glossary_locked") and len(f.get("variants", [])) > 1:
                    top_cnt = f["variants"][0]["count"] if f["variants"] else 0
                    total_cnt = sum(v["count"] for v in f["variants"])
                    ratio = top_cnt / max(1, total_cnt)
                    if ratio < 0.65 or f.get("confidence") in ("medium", "low"):
                        samples = []
                        for occ in f.get("occurrences", [])[:3]:
                            samples.append({
                                "chapter_title": occ.get("chapter_title", ""),
                                "source_snippet": occ.get("source_snippet", "") or occ.get("source_term", ""),
                                "target_snippet": occ.get("target_snippet", "") or occ.get("target_variant", ""),
                            })
                        ambiguous_candidates.append({
                            "source_term": f["source_term"],
                            "category": f["category"],
                            "variants": f["variants"],
                            "context_samples": samples,
                        })

            total_adj_candidates = len(ambiguous_candidates)
            if ambiguous_candidates:
                # Load existing adjudication checkpoints for resilient resumability without redoing completed calls
                existing_adj_cps = get_adjudication_checkpoints(self.db_path, run_id)
                completed_adj_batches = {cp["batch_index"]: cp["adjudications"] for cp in existing_adj_cps}

                adj_map: dict[str, dict[str, Any]] = {}
                for batch_adjs in completed_adj_batches.values():
                    for a in batch_adjs:
                        if isinstance(a, dict) and a.get("source_term"):
                            adj_map[a["source_term"]] = a

                batch_size = 10
                total_adj_batches = (len(ambiguous_candidates) + batch_size - 1) // batch_size
                completed_adj_batches_count = len(completed_adj_batches)

                for batch_idx in range(total_adj_batches):
                    if stop_event and stop_event.is_set():
                        logger.info("Adjudication pass stopped by request for run %s", run_id)
                        break

                    if batch_idx in completed_adj_batches:
                        logger.debug("Reusing checkpointed adjudication batch %d for run %s", batch_idx, run_id)
                        continue

                    batch = ambiguous_candidates[batch_idx * batch_size : (batch_idx + 1) * batch_size]
                    candidate_terms = [c["source_term"] for c in batch]

                    adjudications = None
                    for attempt in range(3):
                        try:
                            adjudications = await provider.adjudicate_inconsistencies(
                                batch,
                                glossary=book_glossary,
                                model=run_model,
                            )
                            break
                        except Exception as batch_err:
                            logger.warning(
                                "Adjudication batch %d attempt %d failed for run %s: %s",
                                batch_idx,
                                attempt + 1,
                                run_id,
                                batch_err,
                            )
                            if attempt < 2:
                                await asyncio.sleep(1.0 * (2 ** attempt))

                    if adjudications is not None:
                        save_adjudication_checkpoint(
                            self.db_path,
                            run_id=run_id,
                            batch_index=batch_idx,
                            candidate_terms=candidate_terms,
                            adjudications=adjudications,
                        )
                        completed_adj_batches[batch_idx] = adjudications
                        completed_adj_batches_count += 1
                        for a in adjudications:
                            if isinstance(a, dict) and a.get("source_term"):
                                adj_map[a["source_term"]] = a
                    else:
                        had_adj_failure = True
                        logger.warning(
                            "Adjudication batch %d failed after retries; continuing with partial coverage for run %s",
                            batch_idx,
                            run_id,
                        )

                # Safely update findings from adjudications: no false-positives, no blanket auto-safe
                for f in aggregated_findings:
                    st = f["source_term"]
                    if st in adj_map:
                        a = adj_map[st]
                        rec = str(a.get("canonical_recommendation") or "").strip()
                        known_target_variants = {
                            v["target"].strip().lower()
                            for v in f.get("variants", [])
                            if v.get("target")
                        }
                        if rec and (rec.lower() in known_target_variants or f.get("glossary_target")):
                            f["canonical_recommendation"] = rec
                            if a.get("reason"):
                                f["reason"] = f"Thẩm định AI: {a['reason'].strip()}"
                            if a.get("confidence") in ("high", "medium", "low"):
                                f["confidence"] = a["confidence"]

        # Save aggregated findings into DB (clearing any old findings for run)
        save_audit_findings(self.db_path, run_id, book_id, aggregated_findings)

        # 6. Finalize run status and truthful coverage reporting
        coverage_status = "full"
        final_pct = 100.0

        def _is_unresolved(val: Any) -> bool:
            if not val:
                return False
            s = str(val).strip()
            return s.startswith("[") or "xác minh" in s.lower()

        has_manual_review = any(
            any(_is_unresolved(occ.get("target_variant")) or bool(occ.get("manual_review_required"))
                for occ in f.get("occurrences", []))
            or any(_is_unresolved(v.get("target"))
                   for v in f.get("variants", []))
            or _is_unresolved(f.get("canonical_recommendation"))
            for f in aggregated_findings
        )

        is_full_coverage = (
            failed_count == 0
            and skipped_count == 0
            and processed_count == total_segments_count
            and total_segments_count > 0
        )

        if not is_full_coverage:
            coverage_status = "partial"
            final_status = "partial"
            stage = "partial"
            coverage_note = (
                f"Rà soát một phần ({processed_count}/{total_segments_count} phân đoạn hoàn tất, "
                f"{failed_count} phân đoạn lỗi hoặc bỏ qua). "
                "Không thể kết luận an toàn tuyệt đối cho các đoạn bị lỗi hoặc bỏ qua. "
                "Đối chiếu thực thể AI kết hợp quét đối sánh toàn thư; các vị trí chưa rõ bản dịch cần biên tập viên xác minh thủ công."
            )
            # Never report 100% when there are unresolved failures or skipped segments
            final_pct = round(100.0 * processed_count / max(1, total_segments_count), 1)
            if last_batch_error:
                clean_last_err = sanitize_secret(str(last_batch_error), secrets)
                run_error = (
                    f"Rà soát một phần: {processed_count}/{total_segments_count} phân đoạn hoàn tất "
                    f"({failed_count} phân đoạn lỗi, {skipped_count} phân đoạn bỏ qua). Lỗi gần nhất: {clean_last_err}"
                )
            else:
                run_error = (
                    f"Rà soát một phần: chỉ hoàn thành {processed_count}/{total_segments_count} phân đoạn "
                    f"({failed_count} phân đoạn lỗi, {skipped_count} phân đoạn bỏ qua)."
                )
        elif has_manual_review:
            coverage_status = "full"
            final_status = "completed"
            stage = "completed"
            run_error = None
            coverage_note = (
                "Rà soát toàn bộ sách hoàn tất thành công. "
                "Lưu ý giới hạn ngữ nghĩa: Các thực thể xuất hiện trong nguồn nhưng chưa rõ biến thể tương ứng đã được gắn cờ [Cần xác minh bản dịch] để biên tập viên kiểm tra."
            )
        else:
            coverage_status = "full"
            final_status = "completed"
            stage = "completed"
            run_error = None
            coverage_note = (
                "Rà soát toàn bộ sách hoàn tất thành công qua trích xuất AI kết hợp quét đối sánh nhất quán đa chương."
            )

        categories_summary: dict[str, int] = {}
        severities_summary: dict[str, int] = {}
        for f in aggregated_findings:
            c = f["category"]
            s = f["severity"]
            categories_summary[c] = categories_summary.get(c, 0) + 1
            severities_summary[s] = severities_summary.get(s, 0) + 1

        update_audit_run(
            self.db_path,
            run_id,
            status=final_status,
            processed_segments=processed_count,
            failed_segments=failed_count,
            skipped_segments=skipped_count,
            error=run_error,
            clear_error=(run_error is None),
            progress={
                "stage": stage,
                "coverage_status": coverage_status,
                "coverage_note": coverage_note,
                "total_batches": len(planned_batches),
                "completed_batches": len(completed_indices),
                "percentage": min(100.0, final_pct),
                "attempted_percentage": min(100.0, round(100.0 * (processed_count + failed_count) / max(1, total_segments_count), 1)),
                "total_findings": len(aggregated_findings),
                "categories": categories_summary,
                "severities": severities_summary,
                "adjudication_total_candidates": total_adj_candidates,
                "adjudication_total_batches": total_adj_batches,
                "adjudication_completed_batches": completed_adj_batches_count,
                "adjudication_status": "partial" if had_adj_failure else ("completed" if total_adj_batches > 0 else "none"),
                **({"error": run_error} if run_error else {}),
            },
        )

        # Automatically generate dry-run correction proposals ONLY for completed full-coverage audit runs
        if is_full_coverage:
            try:
                from backend.app.corrections import generate_correction_proposals
                generate_correction_proposals(self.db_path, run_id, book_id, aggregated_findings)
            except Exception as prop_err:
                logger.warning("Automatic correction proposals generation deferred: %s", prop_err)

    def _integrate_semantic_findings(
        self,
        run_id: str,
        book_id: str,
        semantic_findings: list[dict[str, Any]],
        book_glossary: list[dict[str, Any]],
    ) -> None:
        """Integrate semantic review findings into consistency_audit_findings table."""
        if not semantic_findings:
            return

        glossary_by_src = {g["source"].strip().lower(): g for g in book_glossary if g.get("source")}
        structured_findings: list[dict[str, Any]] = []

        for item in semantic_findings:
            if not isinstance(item, dict):
                continue
            src_term = str(item.get("source_term") or "").strip()
            prob_trans = str(item.get("problematic_translation") or "").strip()
            rec_trans = str(item.get("recommended_translation") or "").strip()
            seg_id = str(item.get("segment_id") or "").strip()
            cat = str(item.get("category") or "context").strip()
            confidence = str(item.get("confidence") or "medium").strip()
            reason = str(item.get("reason") or "Phát hiện sai lệch ngữ nghĩa trong bản dịch").strip()

            if not src_term and not prob_trans:
                continue

            # Determine glossary divergence
            has_glossary_divergence = False
            glossary_target = None
            glossary_locked = False
            g_item = glossary_by_src.get(src_term.lower())
            if g_item:
                glossary_target = g_item.get("target")
                glossary_locked = bool(g_item.get("locked"))
                if rec_trans and rec_trans.lower() != str(glossary_target).lower():
                    has_glossary_divergence = True

            ch_id = item.get("chapter_id", "")
            ch_title = item.get("chapter_title", "")

            occurrences = [
                {
                    "chapter_id": ch_id,
                    "chapter_title": ch_title,
                    "segment_id": seg_id,
                    "source_term": src_term,
                    "target_variant": prob_trans,
                    "source_snippet": src_term,
                    "target_snippet": prob_trans,
                }
            ]
            variants = [
                {"target": prob_trans, "count": 1},
                {"target": rec_trans, "count": 1},
            ]

            severity = "high" if cat in ("chronology", "title") or glossary_locked else "medium"

            structured_findings.append({
                "category": cat,
                "source_term": src_term if src_term else prob_trans,
                "source_term_id": src_term.lower() if src_term else prob_trans.lower(),
                "canonical_recommendation": rec_trans,
                "reason": f"Thẩm định ngữ nghĩa: {reason}",
                "confidence": confidence,
                "severity": severity,
                "glossary_divergence": has_glossary_divergence,
                "glossary_target": glossary_target,
                "glossary_locked": glossary_locked,
                "variants": variants,
                "occurrences": occurrences,
            })

        if structured_findings:
            seen_finding_keys: set[tuple[str, str, str, str]] = set()
            deduped_findings: list[dict[str, Any]] = []
            for sf in structured_findings:
                occs = sf.get("occurrences", [])
                seg_id = occs[0].get("segment_id", "") if occs else ""
                key = (
                    str(sf.get("source_term") or ""),
                    str(sf.get("canonical_recommendation") or ""),
                    str(sf.get("category") or ""),
                    str(seg_id),
                )
                if key not in seen_finding_keys:
                    seen_finding_keys.add(key)
                    deduped_findings.append(sf)

            with get_connection(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM consistency_audit_findings WHERE run_id = ? AND reason LIKE 'Thẩm định ngữ nghĩa:%'",
                    (run_id,),
                )
            append_audit_findings(self.db_path, run_id, book_id, deduped_findings)

    async def execute_semantic_review(
        self,
        run_id: str,
        model: str | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Resumable, separately invocable Gemini 3.5 flash lite semantic consistency stage on an existing audit run.
        
        Reviews substantive story content across all chapters and segments:
        - Paired Korean source + Vietnamese translation for 100% story segments
        - Sliding adjacent context (preceding and subsequent segments)
        - Book-global glossary and entity knowledge
        - Checkpointed per batch in consistency_semantic_checkpoints for instant resumability
        - Progress tracking with honest complete vs partial coverage reporting
        - High-confidence findings populated into audit findings
        """
        run = get_audit_run(self.db_path, run_id)
        if not run:
            raise KeyError(f"Không tìm thấy đợt rà soát '{run_id}'.")

        book_id = run["book_id"]
        book = get_book(self.db_path, book_id)
        if not book:
            raise KeyError(f"Không tìm thấy sách '{book_id}'.")

        provider = self.provider_getter()
        if not provider or not hasattr(provider, "audit_semantic_consistency"):
            err_msg = "Nhà cung cấp AI không hỗ trợ tính năng thẩm định ngữ nghĩa."
            logger.error(err_msg)
            existing_prog = run.get("progress") or {}
            update_audit_run(
                self.db_path,
                run_id,
                progress={
                    **existing_prog,
                    "semantic_stage": "failed",
                    "semantic_error": err_msg,
                },
            )
            return

        run_model = model or "gemini-3.5-flash-lite"
        chapters = get_book_chapters(self.db_path, book_id)
        book_glossary = list_glossary(self.db_path, book_id)

        # Retrieve prior findings to extract established global entity/character knowledge
        existing_findings_res = get_audit_findings(self.db_path, run_id, page=1, page_size=1000)
        existing_findings = existing_findings_res.get("items", [])
        known_entities = [
            {
                "source_term": f["source_term"],
                "canonical_recommendation": f["canonical_recommendation"],
                "category": f.get("category", "term"),
            }
            for f in existing_findings
            if f.get("source_term") and f.get("canonical_recommendation")
        ]

        # Gather all segments and plan semantic batches with sliding window context
        planned_semantic_batches: list[dict[str, Any]] = []
        total_segments_count = 0
        batch_segment_size = 25

        for ch in chapters:
            ch_id = ch["id"]
            ch_data = get_chapter(self.db_path, ch_id)
            if not ch_data:
                continue
            segments = ch_data.get("segments", [])
            total_segments_count += len(segments)
            ch_title = ch.get("title") or f"Chương {ch.get('spine_index', 0) + 1}"

            for start_idx in range(0, len(segments), batch_segment_size):
                batch_segs = segments[start_idx : start_idx + batch_segment_size]
                # Sliding context: 2 segments before, 2 segments after
                before_segs = segments[max(0, start_idx - 2) : start_idx]
                after_segs = segments[start_idx + batch_segment_size : start_idx + batch_segment_size + 2]

                ctx_before = "\n".join(
                    f"[{s.get('source', '').strip()}] -> [{s.get('translation', '').strip()}]"
                    for s in before_segs
                    if s.get("translation")
                )
                ctx_after = "\n".join(
                    f"[{s.get('source', '').strip()}] -> [{s.get('translation', '').strip()}]"
                    for s in after_segs
                    if s.get("translation")
                )

                planned_semantic_batches.append({
                    "batch_index": len(planned_semantic_batches),
                    "chapter_id": ch_id,
                    "chapter_title": ch_title,
                    "segments": batch_segs,
                    "segment_ids": [s["id"] for s in batch_segs if s.get("id")],
                    "context_before": ctx_before,
                    "context_after": ctx_after,
                })

        total_batches = len(planned_semantic_batches)
        if total_batches == 0:
            logger.info("No segments found for semantic review on run %s", run_id)
            return

        # Load existing semantic checkpoints for resumability
        existing_checkpoints = get_semantic_checkpoints(self.db_path, run_id)
        completed_checkpoints = {cp["batch_index"]: cp for cp in existing_checkpoints}

        existing_prog = run.get("progress") or {}
        completed_count = len(completed_checkpoints)
        processed_segments = sum(len(cp.get("segment_ids", [])) for cp in existing_checkpoints)
        failed_batches = 0

        # Reset failed_batches, semantic_pause_reason, and semantic_error on resumed pass
        update_audit_run(
            self.db_path,
            run_id,
            progress={
                **existing_prog,
                "semantic_stage": "running",
                "semantic_total_batches": total_batches,
                "semantic_completed_batches": completed_count,
                "semantic_total_segments": total_segments_count,
                "semantic_processed_segments": processed_segments,
                "semantic_failed_batches": 0,
                "semantic_pause_reason": None,
                "semantic_error": None,
                "semantic_percentage": round(100.0 * completed_count / max(1, total_batches), 1),
            },
        )

        all_semantic_findings: list[dict[str, Any]] = []
        for cp in existing_checkpoints:
            all_semantic_findings.extend(cp.get("raw_findings", []))

        secrets = provider.all_secrets if provider and hasattr(provider, "all_secrets") else []
        current_429_cooldown = SEMANTIC_429_MIN_COOLDOWN
        consecutive_non_429_failures = 0

        for batch_info in planned_semantic_batches:
            if stop_event and stop_event.is_set():
                logger.info("Semantic review stopped by user for run %s", run_id)
                existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                update_audit_run(
                    self.db_path,
                    run_id,
                    progress={
                        **existing_prog,
                        "semantic_stage": "paused",
                        "semantic_pause_reason": "Người dùng đã tạm dừng tiến trình.",
                    },
                )
                return

            b_idx = batch_info["batch_index"]
            if b_idx in completed_checkpoints:
                continue

            batch_segs = batch_info["segments"]
            seg_payload = [
                {
                    "id": s.get("id", ""),
                    "source": s.get("source", ""),
                    "translation": s.get("translation", ""),
                }
                for s in batch_segs
            ]

            batch_findings = None
            rate_limit_attempts = 0
            non_rate_limit_attempts = 0

            while True:
                if stop_event and stop_event.is_set():
                    logger.info("Semantic review stopped by user for run %s", run_id)
                    existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                    update_audit_run(
                        self.db_path,
                        run_id,
                        progress={
                            **existing_prog,
                            "semantic_stage": "paused",
                            "semantic_pause_reason": "Người dùng đã tạm dừng tiến trình.",
                        },
                    )
                    return

                try:
                    batch_findings = await provider.audit_semantic_consistency(
                        segments_payload=seg_payload,
                        context_before=batch_info["context_before"],
                        context_after=batch_info["context_after"],
                        chapter_title=batch_info["chapter_title"],
                        glossary=book_glossary,
                        known_entities=known_entities,
                        model=run_model,
                    )
                    # Succeeded: reset cooldown and non-429 consecutive failures
                    current_429_cooldown = SEMANTIC_429_MIN_COOLDOWN
                    consecutive_non_429_failures = 0
                    break
                except Exception as b_err:
                    is_rl, retry_after, is_daily = is_rate_limit_or_quota_error(b_err)

                    if is_rl:
                        clean_err = sanitize_secret(str(b_err), secrets)
                        if is_daily:
                            pause_reason = (
                                f"Hạn mức API hàng ngày (Daily Quota) đã hết tại lô {b_idx + 1}/{total_batches}. "
                                "Tiến trình tạm dừng an toàn; tất cả checkpoint đã được bảo toàn để tiếp tục khi hạn mức được khôi phục."
                            )
                            logger.warning("Semantic review daily quota reached for run %s: %s", run_id, clean_err)
                            existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                            update_audit_run(
                                self.db_path,
                                run_id,
                                progress={
                                    **existing_prog,
                                    "semantic_stage": "paused",
                                    "semantic_pause_reason": pause_reason,
                                    "semantic_completed_batches": completed_count,
                                    "semantic_failed_batches": failed_batches,
                                    "semantic_total_batches": total_batches,
                                    "semantic_percentage": round(100.0 * completed_count / max(1, total_batches), 1),
                                },
                            )
                            return

                        rate_limit_attempts += 1
                        if rate_limit_attempts > SEMANTIC_429_MAX_RETRIES:
                            pause_reason = (
                                f"Gặp lỗi giới hạn tần suất API (429/RESOURCE_EXHAUSTED) kéo dài tại lô {b_idx + 1}/{total_batches}. "
                                f"Đã thử lại {rate_limit_attempts - 1} lần sau thời gian chờ nhưng API vẫn bị giới hạn ({clean_err}). "
                                "Tiến trình tạm dừng an toàn; tất cả checkpoint đã được bảo toàn để tiếp tục khi hạn mức được khôi phục."
                            )
                            logger.warning("Semantic review run %s paused due to persistent 429: %s", run_id, clean_err)
                            existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                            update_audit_run(
                                self.db_path,
                                run_id,
                                progress={
                                    **existing_prog,
                                    "semantic_stage": "paused",
                                    "semantic_pause_reason": pause_reason,
                                    "semantic_completed_batches": completed_count,
                                    "semantic_failed_batches": failed_batches,
                                    "semantic_total_batches": total_batches,
                                    "semantic_percentage": round(100.0 * completed_count / max(1, total_batches), 1),
                                },
                            )
                            return

                        cooldown_wait = max(current_429_cooldown, retry_after or 0.0)
                        cooldown_wait = min(cooldown_wait, SEMANTIC_429_MAX_COOLDOWN)
                        logger.warning(
                            "Semantic review batch %d/%d received 429 rate limit (attempt %d/%d). "
                            "Cooling down for %.1fs (retry_after=%s): %s",
                            b_idx + 1,
                            total_batches,
                            rate_limit_attempts,
                            SEMANTIC_429_MAX_RETRIES,
                            cooldown_wait,
                            retry_after,
                            clean_err,
                        )
                        # Exponential backoff for subsequent rate-limit cooldown
                        current_429_cooldown = min(SEMANTIC_429_MAX_COOLDOWN, cooldown_wait * 2.0)

                        # Wait for cooldown or stop_event
                        waited = 0.0
                        while waited < cooldown_wait:
                            if stop_event and stop_event.is_set():
                                existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                                update_audit_run(
                                    self.db_path,
                                    run_id,
                                    progress={
                                        **existing_prog,
                                        "semantic_stage": "paused",
                                        "semantic_pause_reason": "Người dùng đã tạm dừng tiến trình.",
                                    },
                                )
                                return
                            step = min(0.5, cooldown_wait - waited)
                            await asyncio.sleep(step)
                            waited += step

                        continue

                    # Non-rate-limit error (e.g. malformed format, schema, network timeout, 5xx)
                    clean_err = sanitize_secret(str(b_err), secrets)
                    if is_systemic_audit_error(b_err):
                        logger.error("Systemic error during semantic review run %s: %s", run_id, clean_err)
                        existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                        update_audit_run(
                            self.db_path,
                            run_id,
                            progress={
                                **existing_prog,
                                "semantic_stage": "failed",
                                "semantic_error": clean_err,
                            },
                        )
                        return

                    non_rate_limit_attempts += 1
                    logger.warning(
                        "Semantic review batch %d/%d attempt %d failed: %s",
                        b_idx + 1,
                        total_batches,
                        non_rate_limit_attempts,
                        clean_err,
                    )
                    if non_rate_limit_attempts >= SEMANTIC_NON_429_MAX_RETRIES:
                        pause_msg = (
                            f"Gặp lỗi khi xử lý lô {b_idx + 1}/{total_batches} ({clean_err}). "
                            f"Đã thử lại {non_rate_limit_attempts} lần nhưng không thành công. "
                            "Tiến trình tạm dừng an toàn; tất cả checkpoint đã được bảo toàn để tiếp tục."
                        )
                        logger.warning(
                            "Semantic review run %s paused at batch %d/%d after %d failed attempts: %s",
                            run_id,
                            b_idx + 1,
                            total_batches,
                            non_rate_limit_attempts,
                            clean_err,
                        )
                        existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                        update_audit_run(
                            self.db_path,
                            run_id,
                            progress={
                                **existing_prog,
                                "semantic_stage": "paused",
                                "semantic_pause_reason": pause_msg,
                                "semantic_completed_batches": completed_count,
                                "semantic_failed_batches": 0,
                                "semantic_total_batches": total_batches,
                                "semantic_percentage": round(100.0 * completed_count / max(1, total_batches), 1),
                            },
                        )
                        return

                    short_delay = 1.0 * (2 ** (non_rate_limit_attempts - 1))
                    await asyncio.sleep(short_delay)

            if batch_findings is not None:
                enriched_findings = []
                for f in batch_findings:
                    if isinstance(f, dict) and f.get("segment_id"):
                        f["chapter_id"] = batch_info["chapter_id"]
                        f["chapter_title"] = batch_info["chapter_title"]
                        enriched_findings.append(f)

                save_semantic_checkpoint(
                    self.db_path,
                    run_id=run_id,
                    chapter_id=batch_info["chapter_id"],
                    batch_index=b_idx,
                    segment_ids=batch_info["segment_ids"],
                    raw_findings=enriched_findings,
                )
                completed_checkpoints[b_idx] = enriched_findings
                completed_count += 1
                processed_segments += len(batch_info["segment_ids"])
                all_semantic_findings.extend(enriched_findings)

            # Update progress
            pct = round(100.0 * completed_count / max(1, total_batches), 1)
            existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
            update_audit_run(
                self.db_path,
                run_id,
                progress={
                    **existing_prog,
                    "semantic_stage": "running",
                    "semantic_total_batches": total_batches,
                    "semantic_completed_batches": completed_count,
                    "semantic_total_segments": total_segments_count,
                    "semantic_processed_segments": processed_segments,
                    "semantic_failed_batches": failed_batches,
                    "semantic_percentage": pct,
                },
            )

        # Merge semantic findings into consistency_audit_findings
        self._integrate_semantic_findings(run_id, book_id, all_semantic_findings, book_glossary)

        # Finalize semantic review status
        is_semantic_full = (failed_batches == 0 and completed_count == total_batches)
        final_semantic_status = "completed" if is_semantic_full else "partial"
        final_pct = 100.0 if is_semantic_full else round(100.0 * completed_count / max(1, total_batches), 1)

        existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
        update_audit_run(
            self.db_path,
            run_id,
            progress={
                **existing_prog,
                "semantic_stage": final_semantic_status,
                "semantic_coverage": "full" if is_semantic_full else "partial",
                "semantic_total_batches": total_batches,
                "semantic_completed_batches": completed_count,
                "semantic_total_segments": total_segments_count,
                "semantic_processed_segments": processed_segments,
                "semantic_failed_batches": failed_batches,
                "semantic_percentage": final_pct,
                "semantic_findings_count": len(all_semantic_findings),
            },
        )
        logger.info(
            "Semantic review finished for run %s: status=%s, batches=%d/%d, findings=%d",
            run_id,
            final_semantic_status,
            completed_count,
            total_batches,
            len(all_semantic_findings),
        )


class AuditManager:
    """Manages asynchronous consistency audit tasks and system lifecycle."""

    def __init__(
        self,
        db_path: Path | str,
        provider_getter: Callable[[], Any],
    ) -> None:
        self.db_path = db_path
        self.provider_getter = provider_getter
        self.service = ConsistencyAuditService(db_path, provider_getter)
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._semantic_tasks: dict[str, asyncio.Task] = {}
        self._semantic_stop_events: dict[str, asyncio.Event] = {}

    def start_audit(self, run_id: str) -> None:
        """Start or resume background audit task."""
        if run_id in self._tasks and not self._tasks[run_id].done():
            return

        stop_ev = asyncio.Event()
        self._stop_events[run_id] = stop_ev

        async def _wrapper():
            try:
                await self.service.execute_run(run_id, stop_event=stop_ev)
            except asyncio.CancelledError:
                update_audit_run(self.db_path, run_id, status="cancelled")
                raise
            except Exception as e:
                provider = self.provider_getter()
                secrets = provider.all_secrets if provider and hasattr(provider, "all_secrets") else []
                err_msg = sanitize_secret(str(e), secrets)
                logger.error("Audit run %s crashed: %s", run_id, err_msg)
                update_audit_run(self.db_path, run_id, status="failed", error=err_msg)
            finally:
                self._tasks.pop(run_id, None)
                self._stop_events.pop(run_id, None)

        task = asyncio.create_task(_wrapper())
        self._tasks[run_id] = task

    def cancel_audit(self, run_id: str) -> None:
        """Cancel a running or queued audit."""
        stop_ev = self._stop_events.get(run_id)
        if stop_ev:
            stop_ev.set()
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        update_audit_run(self.db_path, run_id, status="cancelled")

    def start_semantic_review(self, run_id: str, model: str | None = None) -> None:
        """Start or resume background semantic review task on an existing audit run."""
        if run_id in self._semantic_tasks and not self._semantic_tasks[run_id].done():
            return

        stop_ev = asyncio.Event()
        self._semantic_stop_events[run_id] = stop_ev

        async def _wrapper():
            try:
                await self.service.execute_semantic_review(run_id, model=model, stop_event=stop_ev)
            except asyncio.CancelledError:
                existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                update_audit_run(self.db_path, run_id, progress={**existing_prog, "semantic_stage": "cancelled"})
                raise
            except Exception as e:
                provider = self.provider_getter()
                secrets = provider.all_secrets if provider and hasattr(provider, "all_secrets") else []
                err_msg = sanitize_secret(str(e), secrets)
                logger.error("Semantic review run %s crashed: %s", run_id, err_msg)
                existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
                update_audit_run(
                    self.db_path,
                    run_id,
                    progress={**existing_prog, "semantic_stage": "failed", "semantic_error": err_msg},
                )
            finally:
                self._semantic_tasks.pop(run_id, None)
                self._semantic_stop_events.pop(run_id, None)

        task = asyncio.create_task(_wrapper())
        self._semantic_tasks[run_id] = task

    def cancel_semantic_review(self, run_id: str) -> None:
        """Cancel a running or queued semantic review."""
        stop_ev = self._semantic_stop_events.get(run_id)
        if stop_ev:
            stop_ev.set()
        task = self._semantic_tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        existing_prog = (get_audit_run(self.db_path, run_id) or {}).get("progress") or {}
        update_audit_run(self.db_path, run_id, progress={**existing_prog, "semantic_stage": "cancelled"})

    async def shutdown(self) -> None:
        """Gracefully cancel and await all running audit and semantic tasks at server shutdown."""
        for run_id, stop_ev in self._stop_events.items():
            stop_ev.set()
        for run_id, stop_ev in self._semantic_stop_events.items():
            stop_ev.set()

        running_tasks = [t for t in list(self._tasks.values()) + list(self._semantic_tasks.values()) if not t.done()]
        for t in running_tasks:
            t.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        self._tasks.clear()
        self._stop_events.clear()
        self._semantic_tasks.clear()
        self._semantic_stop_events.clear()
