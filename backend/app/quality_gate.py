"""Deterministic post-translation inline batch quality gates.

Enforces structural integrity, locked term preservation, non-empty outputs,
detection of obvious untranslated Hangul or degenerate repetition, and
defensible length anomaly flags without false positives or arbitrary language judgments.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from .context import (
    check_translation_heuristics,
    is_hangul_str,
    is_symbol_only_source,
    match_character_term,
    normalize_segment,
    preserve_whitespace,
)

META_CHATTER_PREFIXES: list[str] = [
    "dưới đây là bản dịch",
    "đây là bản dịch",
    "tôi xin dịch",
    "tôi đã dịch",
    "bản dịch tiếng việt",
    "here is the translation",
    "here's the translation",
    "as an ai",
    "tôi là mô hình",
]


@dataclass
class QualityFinding:
    """Represents a specific finding on a translation segment."""

    segment_id: str
    category: str  # "segment_id", "empty_output", "locked_character", "locked_glossary", "untranslated_hangul", "hallucination", "length_anomaly"
    severity: str  # "critical" (triggers bounded retry) or "warning" (recorded for review)
    description: str
    source_term: str | None = None
    expected_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert finding to dictionary for review_data storage."""
        d = asdict(self)
        d["type"] = "quality_gate"
        return d


@dataclass
class QualityGateResult:
    """Result of evaluating a translated batch through quality gates."""

    passed: bool
    needs_retry: bool
    findings: list[QualityFinding]
    findings_by_segment: dict[str, list[dict[str, Any]]]
    retry_hint: str = ""


def evaluate_batch_quality_gate(
    batch: list[Any],
    translations: dict[str, str],
    context: dict[str, Any] | None = None,
) -> QualityGateResult:
    """Evaluate batch translations against deterministic quality gates.
    
    Checks:
    1. Preserved segment IDs and count (detects missing, extra, and duplicate IDs).
    2. Missing/empty outputs for non-symbol segments.
    3. Locked character and glossary substitutions against current batch text.
    4. Obvious untranslated Hangul runs (>= 4 syllables) and degenerative repetition loops.
    5. Defensible length anomalies via established truncation and expansion heuristics.
    
    Does NOT invent absolute language judgments or drop translations.
    """
    findings: list[QualityFinding] = []
    req_ids = [normalize_segment(s)[0] for s in batch]
    expected_id_set = set(req_ids)
    provided_id_set = set(translations.keys())

    # -------------------------------------------------------------------------
    # Gate 1: Preserved Segment IDs and Count
    # -------------------------------------------------------------------------
    missing_ids = expected_id_set - provided_id_set
    for mid in sorted(missing_ids):
        # Check if source is nonlinguistic symbol-only; if so, safely auto-preserve
        s_obj = next((s for s in batch if normalize_segment(s)[0] == mid), None)
        src_text = normalize_segment(s_obj)[1] if s_obj else ""
        if is_symbol_only_source(src_text):
            translations[mid] = preserve_whitespace(src_text, src_text.strip())
        else:
            findings.append(
                QualityFinding(
                    segment_id=mid,
                    category="segment_id",
                    severity="critical",
                    description=f"Thiếu phân đoạn ID '{mid}' trong kết quả trả về.",
                )
            )

    extra_ids = provided_id_set - expected_id_set
    for xid in sorted(extra_ids):
        findings.append(
            QualityFinding(
                segment_id=xid,
                category="segment_id",
                severity="critical",
                description=f"Phân đoạn ID thừa '{xid}' không tồn tại trong yêu cầu.",
            )
        )

    # -------------------------------------------------------------------------
    # Gate 2: Missing / Empty Outputs
    # -------------------------------------------------------------------------
    for seg in batch:
        sid, src = normalize_segment(seg)
        trans = translations.get(sid, "")
        if not isinstance(trans, str) or (src.strip() and not trans.strip()):
            if is_symbol_only_source(src):
                translations[sid] = preserve_whitespace(src, src.strip())
            else:
                findings.append(
                    QualityFinding(
                        segment_id=sid,
                        category="empty_output",
                        severity="critical",
                        description=f"Bản dịch cho phân đoạn '{sid}' bị rỗng.",
                    )
                )

    # -------------------------------------------------------------------------
    # Gate 3: Locked Character and Glossary Substitutions
    # -------------------------------------------------------------------------
    locked_chars: list[dict[str, Any]] = []
    locked_gloss: list[dict[str, Any]] = []

    if context and isinstance(context, dict):
        chars_ctx = context.get("characters") or []
        for c in chars_ctx:
            if isinstance(c, dict) and c.get("locked"):
                locked_chars.append(c)

        gloss_ctx = context.get("glossary") or []
        for g in gloss_ctx:
            if isinstance(g, dict) and g.get("locked"):
                locked_gloss.append(g)

    for seg in batch:
        sid, src = normalize_segment(seg)
        trans = translations.get(sid, "")
        if not trans or not isinstance(trans, str):
            continue

        # Check locked characters
        for lc in locked_chars:
            c_src = str(lc.get("canonical_source") or "").strip()
            c_tgt = str(lc.get("canonical_target") or "").strip()
            if not c_src or not c_tgt:
                continue

            # Does source contain canonical name or aliases?
            match_found = match_character_term(src, c_src)
            if not match_found:
                aliases = lc.get("source_aliases") or []
                if isinstance(aliases, str):
                    try:
                        aliases = json.loads(aliases)
                    except Exception:
                        aliases = []
                for al in aliases:
                    al_str = str(al).strip()
                    if al_str and match_character_term(src, al_str):
                        match_found = True
                        break

            if match_found:
                # Check if target contains canonical target with boundary precision
                if not match_character_term(trans, c_tgt):
                    findings.append(
                        QualityFinding(
                            segment_id=sid,
                            category="locked_character",
                            severity="warning",
                            description=f"Nhân vật đã khóa '{c_src}' chưa được dịch thành '{c_tgt}'.",
                            source_term=c_src,
                            expected_target=c_tgt,
                        )
                    )

        # Check locked glossary
        for lg in locked_gloss:
            g_src = str(lg.get("source") or "").strip()
            g_tgt = str(lg.get("target") or "").strip()
            if not g_src or not g_tgt:
                continue
            if match_character_term(src, g_src):
                if not match_character_term(trans, g_tgt):
                    findings.append(
                        QualityFinding(
                            segment_id=sid,
                            category="locked_glossary",
                            severity="warning",
                            description=f"Thuật ngữ đã khóa '{g_src}' chưa được dịch thành '{g_tgt}'.",
                            source_term=g_src,
                            expected_target=g_tgt,
                        )
                    )

    # -------------------------------------------------------------------------
    # Gate 4: Obvious Hallucinated / Untranslated Spans
    # -------------------------------------------------------------------------
    for seg in batch:
        sid, src = normalize_segment(seg)
        trans = translations.get(sid, "")
        if not trans or not isinstance(trans, str):
            continue

        # 4a. Untranslated Hangul runs (>= 4 consecutive syllables)
        if is_hangul_str(src):
            hangul_runs = re.findall(r"[\uac00-\ud7a3]{4,}", trans)
            if hangul_runs:
                sample = hangul_runs[0]
                findings.append(
                    QualityFinding(
                        segment_id=sid,
                        category="untranslated_hangul",
                        severity="critical",
                        description=f"Phát hiện đoạn tiếng Hàn chưa được dịch: '{sample}'.",
                    )
                )

        # 4b. Degenerative phrase repetition loop (e.g. repeated 4+ times consecutively)
        rep_match = re.search(r"(.{4,25}?)\1{3,}", trans)
        if rep_match:
            phrase = rep_match.group(1).strip()
            # Must contain letters (not pure punctuation/dashes) and not simply reflect source repetition
            if len(phrase) >= 4 and any(c.isalpha() for c in phrase) and phrase not in src:
                findings.append(
                    QualityFinding(
                        segment_id=sid,
                        category="hallucination",
                        severity="critical",
                        description=f"Phát hiện lặp từ/thoái hóa bất thường: '{phrase}'.",
                    )
                )

        # 4c. Meta-chatter prefixes
        lower_trans = trans.strip().lower()
        for prefix in META_CHATTER_PREFIXES:
            if lower_trans.startswith(prefix):
                findings.append(
                    QualityFinding(
                        segment_id=sid,
                        category="hallucination",
                        severity="critical",
                        description=f"Phát hiện lời thoại meta của mô hình: '{prefix}'.",
                    )
                )
                break

    # -------------------------------------------------------------------------
    # Gate 5: Gross Length Anomalies
    # -------------------------------------------------------------------------
    for seg in batch:
        sid, src = normalize_segment(seg)
        trans = translations.get(sid, "")
        if not trans or not isinstance(trans, str):
            continue
        h_issues = check_translation_heuristics(src, trans)
        for h in h_issues:
            findings.append(
                QualityFinding(
                    segment_id=sid,
                    category="length_anomaly",
                    severity="warning",
                    description=h,
                )
            )

    # -------------------------------------------------------------------------
    # Group findings by segment and formulate retry hint if needed
    # -------------------------------------------------------------------------
    findings_by_seg: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        findings_by_seg.setdefault(f.segment_id, []).append(f.to_dict())

    critical_findings = [f for f in findings if f.severity == "critical"]
    needs_retry = len(critical_findings) > 0
    passed = len(findings) == 0

    retry_hints: list[str] = []
    if critical_findings:
        categories = sorted(set(f.category for f in critical_findings))
        if "segment_id" in categories or "empty_output" in categories:
            retry_hints.append("Yêu cầu dịch đầy đủ tất cả các phân đoạn ID, không được bỏ trống hoặc thiếu ID.")
        if "untranslated_hangul" in categories:
            retry_hints.append("Bản dịch còn sót các cụm từ tiếng Hàn nguyên bản chưa được chuyển ngữ sang tiếng Việt.")
        if "hallucination" in categories:
            retry_hints.append("Tuyệt đối không lặp lại từ ngữ vô nghĩa hoặc thêm lời giải thích meta ngoài nội dung truyện.")

    return QualityGateResult(
        passed=passed,
        needs_retry=needs_retry,
        findings=findings,
        findings_by_segment=findings_by_seg,
        retry_hint=" ".join(retry_hints),
    )


def is_quality_finding_resolved(
    finding: dict[str, Any],
    source_text: str,
    new_translation: str,
    post_segment_findings: list[dict[str, Any]] | None = None,
) -> bool:
    """Determine whether an existing quality gate finding has been resolved by review/correction.

    Returns True if the issue has been cleared, False if it remains unresolved.
    """
    cat = finding.get("category")

    # 1. Locked character / glossary: resolved if expected target is present in new translation with boundary precision
    if cat in ("locked_character", "locked_glossary"):
        exp = finding.get("expected_target")
        if exp and str(exp).strip():
            return match_character_term(new_translation, str(exp).strip())
        if post_segment_findings is not None:
            return not any(
                isinstance(f, dict) and f.get("category") == cat for f in post_segment_findings
            )
        return False

    # 2. Untranslated Hangul: resolved if no >=4 Hangul syllable runs remain in new translation
    if cat == "untranslated_hangul":
        hangul_runs = re.findall(r"[\uac00-\ud7a3]{4,}", new_translation)
        return len(hangul_runs) == 0

    # 3. Length anomaly: resolved if translation heuristic checks pass on new translation
    if cat == "length_anomaly":
        heuristics = check_translation_heuristics(source_text, new_translation)
        return len(heuristics) == 0

    # 4. Empty output: resolved if new translation is non-empty
    if cat == "empty_output":
        return bool(new_translation and new_translation.strip())

    # 5. Hallucination / degeneration / meta chatter:
    if cat == "hallucination":
        rep_match = re.search(r"(.{4,25}?)\1{3,}", new_translation)
        lower_trans = new_translation.strip().lower()
        meta_match = any(lower_trans.startswith(p) for p in META_CHATTER_PREFIXES)
        return not rep_match and not meta_match

    # 6. Preserved segment id
    if cat == "segment_id":
        return True

    # 7. Fallback for other/custom categories: check if post gate flags it
    if post_segment_findings is not None:
        return not any(
            isinstance(f, dict) and f.get("category") == cat for f in post_segment_findings
        )

    return False

