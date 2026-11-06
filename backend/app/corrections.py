"""Automatic correction pipeline for Trạm Dịch consistency review.

Generates granular preview/dry-run proposals, classifies changes into auto-safe
versus manual-review-required, preserves inflections, punctuation, and chapter
heading counters, and applies validated safe changes transactionally with
optimistic locking and recoverable backup snapshots.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from backend.app.audit import normalize_title_stem
from backend.app.db import (
    apply_correction_proposals,
    get_all_audit_findings,
    get_audit_findings,
    get_audit_run,
    get_book_chapters,
    get_chapter,
    get_connection,
    list_glossary,
    rollback_correction_snapshot,
    save_correction_proposals,
    utc_now,
)
from backend.app.provider import (
    ALL_KOREAN_PARTICLES,
    derive_conservative_headword,
    match_korean_term_in_text,
)

logger = logging.getLogger(__name__)

TRAILING_INSTALLMENT_RE = re.compile(
    r"\s*[\(\uff08\[\uff3b\u3010\u3014\u3008\u300a]\s*(?:#?\d+(?:\s*[/]\s*\d+)?|[0-9\uff10-\uff19]+|[ivxldcmIVXLDCM]+)\s*[\)\uff09\]\uff3d\u3011\u3015\u3009\u300b]\s*$",
    re.IGNORECASE,
)

EXTENDED_TRAILING_COUNTER_RE = re.compile(
    r"\s*(?:[-–—:]\s*)?(?:(?:Tập|Chương|Phần|Hồi|Episode|Ep|Ch|Vol|Part)\s*(?:#?\d+|[ivxldcmIVXLDCM]+)|[\(\uff08\[\uff3b\u3010\u3014\u3008\u300a]\s*(?:#?\d+(?:\s*[/]\s*\d+)?|[0-9\uff10-\uff19]+|[ivxldcmIVXLDCM]+)\s*[\)\uff09\]\uff3d\u3011\u3015\u3009\u300b])\s*$",
    re.IGNORECASE,
)

VIETNAMESE_WORD_BOUNDARY_PREFIX = r"(?<![\w\u00C0-\u1EF9])"
VIETNAMESE_WORD_BOUNDARY_SUFFIX = r"(?![\w\u00C0-\u1EF9])"


def extract_trailing_installment(title: str | None) -> tuple[str, str]:
    """Extract root stem and trailing bracketed chapter installment marker.
    
    Examples:
    'Phim tài liệu người thật Starplex tập 5 (3)' -> ('Phim tài liệu người thật Starplex tập 5', '(3)')
    '스타플렉스 휴먼 다큐 5화 (3)' -> ('스타플렉스 휴먼 다큐 5화', '(3)')
    'Thức tỉnh (1)' -> ('Thức tỉnh', '(1)')
    'Thức tỉnh [1/2]' -> ('Thức tỉnh', '[1/2]')
    """
    if not title:
        return ("", "")
    text = str(title).strip()
    m = TRAILING_INSTALLMENT_RE.search(text)
    if m:
        stem = text[: m.start()].strip()
        installment = text[m.start() :].strip()
        return (stem if stem else text, installment)
    return (text, "")


def extract_trailing_counter(title: str | None) -> tuple[str, str]:
    """Extract root title stem and trailing installment counter / ordinal marker.
    
    Returns (stem, counter_marker). For example:
    'Thức tỉnh (1)' -> ('Thức tỉnh', '(1)')
    'Phim tài liệu Starplex Tập 5' -> ('Phim tài liệu Starplex', 'Tập 5')
    """
    if not title:
        return ("", "")
    text = str(title).strip()
    m = EXTENDED_TRAILING_COUNTER_RE.search(text)
    if m:
        stem = text[: m.start()].strip()
        counter = text[m.start() :].strip()
        # Clean leading separator on counter
        clean_counter = re.sub(r"^[-–—:]\s*", "", counter).strip()
        return (stem if stem else text, clean_counter if clean_counter else counter)
    return (text, "")


def generate_title_correction(
    old_title: str,
    canonical_stem: str,
    source_title: str | None = None,
) -> tuple[str, str]:
    """Generate normalized new title string preserving trailing installment markers.
    
    Fixes loss of original trailing chapter installment (e.g. (1)/(2)/(3), [1/2]) while
    retaining embedded semantic episode numbering (e.g. 'Tập 5' / '5화'), original/source
    fallback when translation missed the installment, preserves fractions and brackets,
    and avoids duplicate numbers or counters.
    
    Returns (new_title, preserved_counter).
    """
    # 1. Check for trailing bracketed installment markers (e.g. (1), (2), (3), [1/2])
    old_inst_stem, old_inst = extract_trailing_installment(old_title)
    src_inst_stem, src_inst = extract_trailing_installment(source_title or "")
    inst_to_keep = old_inst or src_inst

    # Canonical stem: strip any bracketed installment so we don't carry an alien chapter's installment
    canon_clean = normalize_title_stem(canonical_stem).strip()

    if inst_to_keep:
        # Check if canon_clean already ends with this exact installment to avoid duplicates
        if canon_clean.endswith(inst_to_keep):
            new_title = canon_clean
        else:
            if inst_to_keep.startswith(("(", "[", "（", "［", "【", "〔", "〈", "《")):
                new_title = f"{canon_clean} {inst_to_keep}"
            else:
                new_title = f"{canon_clean} - {inst_to_keep}"
        return (new_title.strip(), inst_to_keep)

    # 2. If no bracketed installment exists, inspect extended trailing counters (e.g. Tập 5, Chương 2)
    old_ext_stem, old_counter = extract_trailing_counter(old_title)
    src_ext_stem, src_counter = extract_trailing_counter(source_title or "")
    counter_to_keep = old_counter or src_counter

    if counter_to_keep:
        canon_ext_stem, canon_counter = extract_trailing_counter(canon_clean)
        if canon_counter and canon_counter.lower() == counter_to_keep.lower():
            # Already includes this exact counter
            new_title = canon_clean
        elif canon_counter:
            # Canonical stem had a different counter; replace with chapter's counter
            if counter_to_keep.startswith(("(", "[", "（", "［", "【", "〔", "〈", "《")):
                new_title = f"{canon_ext_stem} {counter_to_keep}"
            else:
                new_title = f"{canon_ext_stem} - {counter_to_keep}"
        else:
            # Canonical stem has no counter; append chapter's counter
            if counter_to_keep.startswith(("(", "[", "（", "［", "【", "〔", "〈", "《")):
                new_title = f"{canon_clean} {counter_to_keep}"
            else:
                new_title = f"{canon_clean} - {counter_to_keep}"
        return (new_title.strip(), counter_to_keep)

    return (canon_clean, "")


def is_safe_word_boundary_match(text: str, target_phrase: str) -> list[tuple[int, int]]:
    """Find non-overlapping word-boundary matches of a phrase in Vietnamese text."""
    if not text or not target_phrase:
        return []
    words = target_phrase.strip().split()
    if not words:
        return []
    pattern = (
        VIETNAMESE_WORD_BOUNDARY_PREFIX
        + r"\s+".join(re.escape(w) for w in words)
        + VIETNAMESE_WORD_BOUNDARY_SUFFIX
    )
    spans: list[tuple[int, int]] = []
    try:
        for m in re.finditer(pattern, text):
            spans.append((m.start(), m.end()))
    except Exception:
        pass
    return spans


def generate_correction_proposals(
    db_path: Path | str,
    run_id: str,
    book_id: str,
    findings: list[dict[str, Any]] | None = None,
    verified_json_path: Path | str | None = None,
    check_stale: bool = False,
) -> dict[str, Any]:
    """Analyze consistency audit findings and generate dry-run correction proposals.
    
    Strictly read-only with respect to saved translations: does NOT modify segments
    or chapters. Persists proposals into consistency_correction_proposals table.
    
    Categorizes proposals into auto_safe vs manual_only using rigorous criteria:
    - Locked glossary conflicts: auto_safe with high confidence.
    - High confidence title synchronization: auto_safe with preserved ordinals.
    - Explicitly verified unambiguous forms: auto_safe.
    - Frequent lexical terms without semantic verification or locked glossary: manual_only (homograph protection).
    - Case-only, homographs, ambiguous aliases, unanchored sources: manual_only.
    """
    run = get_audit_run(db_path, run_id)
    if not run:
        raise KeyError(f"Không tìm thấy đợt rà soát '{run_id}'.")
    if check_stale and run.get("is_stale"):
        raise ValueError("Không thể tạo đề xuất sửa đổi: Đợt rà soát đã cũ (stale) do bản dịch của sách đã thay đổi.")
    if run.get("status") in ("running", "queued", "pausing"):
        raise ValueError(f"Không thể tạo đề xuất sửa đổi: Đợt rà soát đang hoạt động (trạng thái '{run.get('status')}').")

    progress = run.get("progress") or {}
    semantic_stage = progress.get("semantic_stage")
    if semantic_stage is not None:
        norm_semantic_stage = str(semantic_stage).strip().lower()
        semantic_cov = str(progress.get("semantic_coverage") or "").strip().lower()
        if norm_semantic_stage != "completed":
            raise ValueError(f"Không thể tạo đề xuất sửa đổi: Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (trạng thái '{semantic_stage}').")
        if semantic_cov != "full":
            raise ValueError(f"Không thể tạo đề xuất sửa đổi: Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (độ bao phủ '{progress.get('semantic_coverage')}').")

    is_full = (
        run.get("status") == "completed"
        and run.get("failed_segments", 0) == 0
        and run.get("skipped_segments", 0) == 0
        and run.get("total_segments", 0) > 0
        and run.get("processed_segments", 0) == run.get("total_segments", 0)
        and progress.get("coverage_status") == "full"
    )
    if not is_full:
        raise ValueError("Không thể tạo đề xuất sửa đổi: Đợt rà soát chưa hoàn tất độ bao phủ toàn diện (incomplete/partial).")

    if findings is None:
        findings = get_all_audit_findings(db_path, run_id)

    # If DB findings are empty and a verified fullbook json exists, allow importing
    if not findings and verified_json_path:
        p = Path(verified_json_path)
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    v_data = json.load(f)
                    # Convert verified fullbook format into finding objects
                    findings = _convert_verified_json_to_findings(v_data)
            except Exception as e:
                logger.warning("Could not read verified JSON %s: %s", p, e)

    book_glossary = list_glossary(db_path, book_id)
    glossary_by_src: dict[str, dict[str, Any]] = {
        g["source"].strip(): g for g in book_glossary if g.get("source")
    }
    glossary_by_src_lower: dict[str, dict[str, Any]] = {
        g["source"].strip().lower(): g for g in book_glossary if g.get("source")
    }

    # Pre-cache chapters and segments to minimize SQLite lookups
    chapters = get_book_chapters(db_path, book_id)
    chapter_index_map: dict[str, int] = {ch["id"]: ch.get("spine_index", 0) for ch in chapters}
    chapter_title_map: dict[str, str] = {
        ch["id"]: ch.get("title") or f"Chương {ch.get('spine_index', 0) + 1}"
        for ch in chapters
    }

    # Cache segments by segment_id: segment_id -> (chapter_id, source, translation)
    segment_cache: dict[str, dict[str, Any]] = {}
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.chapter_id, s.source, s.translation, s.parent_tag, s.dom_path
            FROM segments s
            JOIN chapters c ON s.chapter_id = c.id
            WHERE c.book_id = ? AND s.is_whitespace = 0 AND s.translation IS NOT NULL AND s.translation != ''
            """,
            (book_id,),
        ).fetchall()
        for r in rows:
            segment_cache[r["id"]] = dict(r)

    proposals: list[dict[str, Any]] = []
    seen_proposals_keys: set[tuple[str, str, str]] = set()

    for f in findings:
        cat = str(f.get("category") or "term").strip().lower()
        src_term = str(f.get("source_term") or "").strip()
        canonical_target = str(f.get("canonical_recommendation") or "").strip()
        confidence = str(f.get("confidence") or "medium").strip().lower()
        severity = str(f.get("severity") or "medium").strip().lower()
        is_case_only = bool(f.get("is_case_only") or f.get("case_only"))
        glossary_locked = bool(f.get("glossary_locked"))
        glossary_target = f.get("glossary_target")

        # Skip if canonical recommendation is unresolved or empty
        if not canonical_target or canonical_target.startswith("[") or "xác minh" in canonical_target.lower():
            continue

        occurrences = f.get("occurrences") or []
        variants = f.get("variants") or []

        # Calculate dominant variant ratio
        total_occs = len(occurrences)
        dominant_count = 0
        for v in variants:
            if v.get("target") == canonical_target:
                dominant_count = v.get("count", 0)
                break
        dominant_ratio = dominant_count / max(1, total_occs) if total_occs > 0 else 0.0

        for occ in occurrences:
            seg_id = str(occ.get("segment_id") or "").strip()
            ch_id = str(occ.get("chapter_id") or "").strip()
            if not seg_id:
                continue

            seg_data = segment_cache.get(seg_id)
            if not seg_data:
                continue

            current_source = str(seg_data.get("source") or "")
            current_trans = str(seg_data.get("translation") or "")
            target_variant = str(occ.get("target_variant") or "").strip()
            ch_idx = chapter_index_map.get(ch_id, 0)
            ch_title = chapter_title_map.get(ch_id, f"Chương {ch_idx + 1}")

            # 1. Title handling
            if cat == "title":
                old_text = current_trans.strip()
                new_text, counter = generate_title_correction(
                    old_text,
                    canonical_target,
                    source_title=current_source,
                )

                if new_text == old_text or not new_text:
                    continue

                prop_key = (seg_id, src_term, new_text)
                if prop_key in seen_proposals_keys:
                    continue
                seen_proposals_keys.add(prop_key)

                # Determine safety classification for title
                is_auto_safe = False
                classification_reason = ""

                if glossary_locked:
                    is_auto_safe = True
                    classification_reason = f"Tiêu đề chương đã khóa trong từ điển: '{canonical_target}'."
                elif confidence == "high" and (dominant_ratio >= 0.65 or dominant_count >= 2):
                    is_auto_safe = True
                    classification_reason = (
                        f"Đồng bộ tiêu đề chương theo chuẩn đa số: '{canonical_target}'. "
                        f"Bảo toàn chỉ số thứ tự '{counter}'."
                    )
                else:
                    is_auto_safe = False
                    classification_reason = (
                        f"Tiêu đề phân mảnh cần biên tập viên xác nhận lại cách dịch: '{canonical_target}'."
                    )

                proposals.append({
                    "id": uuid.uuid4().hex,
                    "run_id": run_id,
                    "book_id": book_id,
                    "chapter_id": ch_id,
                    "chapter_index": ch_idx,
                    "chapter_title": ch_title,
                    "segment_id": seg_id,
                    "source_term": src_term,
                    "canonical_target": canonical_target,
                    "old_text": old_text,
                    "new_text": new_text,
                    "category": "title",
                    "confidence": confidence,
                    "classification": "auto_safe" if is_auto_safe else "manual_only",
                    "reason": classification_reason,
                    "is_selected": is_auto_safe,
                    "status": "pending",
                })
                continue

            # 2. General Term / Entity handling
            # If target variant already matches canonical target, no correction needed
            if target_variant == canonical_target:
                continue

            # If target variant is unresolved marker, classify as manual_only
            is_unresolved = target_variant.startswith("[") or "xác minh" in target_variant.lower()
            if is_unresolved:
                proposals.append({
                    "id": uuid.uuid4().hex,
                    "run_id": run_id,
                    "book_id": book_id,
                    "chapter_id": ch_id,
                    "chapter_index": ch_idx,
                    "chapter_title": ch_title,
                    "segment_id": seg_id,
                    "source_term": src_term,
                    "canonical_target": canonical_target,
                    "old_text": current_trans,
                    "new_text": current_trans,
                    "category": cat,
                    "confidence": "low",
                    "classification": "manual_only",
                    "reason": "Phân đoạn chứa thực thể trong tiếng Hàn nhưng chưa có bản dịch đối ứng xác định.",
                    "is_selected": False,
                    "status": "pending",
                })
                continue

            # Anchor check: does source actually contain the Korean source term?
            matched_korean = match_korean_term_in_text(current_source, src_term)
            if not matched_korean:
                # Fallback to conservative headword check
                canon_headword = derive_conservative_headword(src_term)
                if canon_headword:
                    matched_korean = match_korean_term_in_text(current_source, canon_headword)

            if not matched_korean:
                # Source not anchored: unsafe to replace automatically
                proposals.append({
                    "id": uuid.uuid4().hex,
                    "run_id": run_id,
                    "book_id": book_id,
                    "chapter_id": ch_id,
                    "chapter_index": ch_idx,
                    "chapter_title": ch_title,
                    "segment_id": seg_id,
                    "source_term": src_term,
                    "canonical_target": canonical_target,
                    "old_text": current_trans,
                    "new_text": current_trans,
                    "category": cat,
                    "confidence": "low",
                    "classification": "manual_only",
                    "reason": "Thực thể tiếng Hàn không được neo rõ ràng trong câu nguồn của phân đoạn này.",
                    "is_selected": False,
                    "status": "pending",
                })
                continue

            # Target check: does translation contain target_variant with word boundary?
            boundary_matches = is_safe_word_boundary_match(current_trans, target_variant)

            if not boundary_matches:
                # Check case-insensitive boundary match
                words = target_variant.strip().split()
                ci_pattern = (
                    VIETNAMESE_WORD_BOUNDARY_PREFIX
                    + r"\s+".join(re.escape(w) for w in words)
                    + VIETNAMESE_WORD_BOUNDARY_SUFFIX
                )
                try:
                    ci_matches = [
                        (m.start(), m.end())
                        for m in re.finditer(ci_pattern, current_trans, re.IGNORECASE)
                    ]
                except Exception:
                    ci_matches = []

                if ci_matches:
                    boundary_matches = ci_matches
                else:
                    # Cannot safely locate the variant in translation
                    continue

            # Check ambiguity: multiple occurrences of target_variant in same segment
            if len(boundary_matches) > 1:
                # Ambiguous: multiple instances in one sentence
                proposals.append({
                    "id": uuid.uuid4().hex,
                    "run_id": run_id,
                    "book_id": book_id,
                    "chapter_id": ch_id,
                    "chapter_index": ch_idx,
                    "chapter_title": ch_title,
                    "segment_id": seg_id,
                    "source_term": src_term,
                    "canonical_target": canonical_target,
                    "old_text": current_trans,
                    "new_text": current_trans,
                    "category": cat,
                    "confidence": "medium",
                    "classification": "manual_only",
                    "reason": f"Biến thể '{target_variant}' xuất hiện nhiều lần ({len(boundary_matches)} vị trí) trong phân đoạn, cần chỉ định thủ công.",
                    "is_selected": False,
                    "status": "pending",
                })
                continue

            # Perform safe replacement on single match span
            span_start, span_end = boundary_matches[0]
            matched_sub = current_trans[span_start:span_end]

            # Construct new text
            new_trans = current_trans[:span_start] + canonical_target + current_trans[span_end:]

            if new_trans == current_trans:
                continue

            prop_key = (seg_id, src_term, new_trans)
            if prop_key in seen_proposals_keys:
                continue
            seen_proposals_keys.add(prop_key)

            # Determine safety classification
            is_auto_safe = False
            classification_reason = ""

            # Check criteria for auto_safe vs manual_only:
            # 0. Semantic nuances (chronology, relationship, context) or low/medium confidence -> MANUAL ONLY
            if cat in ("chronology", "context", "relationship") or confidence in ("medium", "low"):
                is_auto_safe = False
                classification_reason = (
                    f"Thẩm định ngữ nghĩa loại '{cat}' (độ tin cậy: {confidence}) yêu cầu biên tập viên kiểm tra thủ công."
                )

            # 1. Locked glossary match -> AUTO SAFE
            elif glossary_locked:
                is_auto_safe = True
                classification_reason = f"Thuật ngữ đã bị khóa trong từ điển sách: '{canonical_target}'."

            # 2. Well-verified unambiguous form (explicitly verified semantic consistency) -> AUTO SAFE
            elif f.get("semantic_verified") or f.get("is_unambiguous"):
                is_auto_safe = True
                classification_reason = (
                    f"Dạng chuẩn hóa đã qua xác minh ngữ nghĩa tiếng Hàn không gây đồng âm: '{canonical_target}'."
                )

            # 3. Case-only difference without locked glossary -> MANUAL ONLY
            elif is_case_only or (target_variant.casefold() == canonical_target.casefold()):
                is_auto_safe = False
                classification_reason = (
                    f"Khác biệt chỉ về chữ hoa/thường ('{matched_sub}' -> '{canonical_target}'). "
                    "Yêu cầu biên tập viên kiểm tra văn phong ngữ cảnh."
                )

            # 4. Short Korean term (len <= 2) without locked glossary -> potential homograph -> MANUAL ONLY
            elif len(src_term) <= 2 and not glossary_target:
                is_auto_safe = False
                classification_reason = (
                    f"Thuật ngữ nguồn ngắn ('{src_term}'), tiềm ẩn đồng âm hoặc trợ từ ngữ pháp. "
                    "Yêu cầu kiểm tra ngữ cảnh thủ công."
                )

            # 5. Frequent lexical terms without locked glossary or semantic verification -> MANUAL ONLY
            # (Prevents destructive homograph errors where frequent lexical terms >=70% or dominant_count>=5
            # were incorrectly assumed safe without Korean semantic verification)
            elif dominant_ratio >= 0.70 or dominant_count >= 5:
                is_auto_safe = False
                classification_reason = (
                    f"Thuật ngữ xuất hiện nhiều lần ('{matched_sub}' -> '{canonical_target}', {dominant_count}/{total_occs} lần) "
                    "nhưng chưa được khóa từ điển hoặc xác minh ngữ nghĩa tiếng Hàn, cần duyệt thủ công để tránh lỗi đồng âm."
                )

            # 6. Low/Medium confidence or split ratio -> MANUAL ONLY
            else:
                is_auto_safe = False
                classification_reason = (
                    f"Biến thể dịch thuật ('{matched_sub}' -> '{canonical_target}') "
                    "cần người dịch xác nhận ngữ cảnh."
                )

            proposals.append({
                "id": uuid.uuid4().hex,
                "run_id": run_id,
                "book_id": book_id,
                "chapter_id": ch_id,
                "chapter_index": ch_idx,
                "chapter_title": ch_title,
                "segment_id": seg_id,
                "source_term": src_term,
                "canonical_target": canonical_target,
                "old_text": current_trans,
                "new_text": new_trans,
                "category": cat,
                "confidence": confidence,
                "classification": "auto_safe" if is_auto_safe else "manual_only",
                "reason": classification_reason,
                "is_selected": is_auto_safe,
                "status": "pending",
            })

    # Save generated proposals to database
    save_correction_proposals(db_path, run_id, book_id, proposals)

    auto_safe_count = sum(1 for p in proposals if p["classification"] == "auto_safe")
    manual_only_count = sum(1 for p in proposals if p["classification"] == "manual_only")
    affected_chapters = len({p["chapter_id"] for p in proposals})

    return {
        "run_id": run_id,
        "book_id": book_id,
        "total_proposals": len(proposals),
        "auto_safe_count": auto_safe_count,
        "manual_only_count": manual_only_count,
        "affected_chapters_count": affected_chapters,
        "proposals": proposals,
    }


def _convert_verified_json_to_findings(v_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert findings from consistency_review_20261018_fullbook_verified.json into audit findings."""
    findings: list[dict[str, Any]] = []

    # Title inconsistencies
    for tg in v_data.get("title_inconsistencies", []):
        src_stem = tg.get("source_stem") or ""
        canon = tg.get("canonical_recommendation") or ""
        occs = []
        for ch in tg.get("chapters", []):
            occs.append({
                "chapter_id": ch.get("chapter_id", ""),
                "chapter_title": ch.get("chapter_title", ""),
                "segment_id": ch.get("segment_id", ""),
                "source_term": src_stem,
                "target_variant": ch.get("translation", ""),
                "source_snippet": ch.get("source", ""),
                "target_snippet": ch.get("translation", ""),
            })
        findings.append({
            "category": "title",
            "source_term": src_stem,
            "source_term_id": f"title:{src_stem}",
            "canonical_recommendation": canon,
            "reason": tg.get("reason", "Tiêu đề phân mảnh"),
            "confidence": "high",
            "severity": "high",
            "glossary_divergence": False,
            "glossary_locked": False,
            "occurrences": occs,
            "variants": tg.get("variants", []),
        })

    # Observed literal findings
    for item in v_data.get("observed_literal_findings", []):
        src = item.get("source") or ""
        cat = item.get("category") or "term"
        sev = item.get("severity") or "medium"
        is_case = bool(item.get("is_case_only"))
        variants = item.get("variants") or []
        sorted_v = sorted(variants, key=lambda x: x.get("count", 0), reverse=True)
        canon = sorted_v[0]["target"] if sorted_v else ""

        occs = []
        for es in item.get("evidence_samples", []):
            occs.append({
                "chapter_id": "",
                "chapter_title": f"Chương {es.get('chapter', 1)}",
                "segment_id": es.get("segment_id", ""),
                "source_term": src,
                "target_variant": es.get("variant", ""),
                "source_snippet": es.get("source_snippet", ""),
                "target_snippet": es.get("target_snippet", ""),
            })

        findings.append({
            "category": cat,
            "source_term": src,
            "source_term_id": f"{cat}:{src}",
            "canonical_recommendation": canon,
            "reason": item.get("reason", "Phân mảnh biến thể dịch thuật"),
            "confidence": "high" if len(sorted_v) > 0 and sorted_v[0].get("count", 0) >= 5 else "medium",
            "severity": sev,
            "is_case_only": is_case,
            "case_only": is_case,
            "glossary_divergence": bool(item.get("glossary_divergence")),
            "glossary_locked": bool(item.get("glossary_locked")),
            "glossary_target": item.get("glossary_target"),
            "occurrences": occs,
            "variants": variants,
        })

    return findings


def apply_proposals_with_snapshot(
    db_path: Path | str,
    run_id: str,
    book_id: str,
    selected_ids: list[str] | None = None,
    only_auto_safe: bool = False,
    check_stale: bool = False,
) -> dict[str, Any]:
    """Execute transactional apply of proposals with optimistic locking and automatic snapshot."""
    return apply_correction_proposals(
        db_path=db_path,
        run_id=run_id,
        book_id=book_id,
        selected_ids=selected_ids,
        only_auto_safe=only_auto_safe,
        check_stale=check_stale,
    )


def rollback_snapshot(db_path: Path | str, snapshot_id: str) -> dict[str, Any]:
    """Execute transactional rollback of a snapshot."""
    return rollback_correction_snapshot(db_path=db_path, snapshot_id=snapshot_id)
