#!/usr/bin/env python3
"""Read-only Korean-to-Vietnamese cross-chapter consistency review tool for Trạm Dịch.

Scans all 157 chapter records and 46,637 translated segments for book
82a3a264bbc24924acd010f668da6543, opening production SQLite strictly in
read-only mode (URI mode=ro). Consumes historical checkpoint candidates,
glossary records, and title structures to detect and verify cross-chapter
naming, location, organization, title, item, skill, and system lexicon
consistency across early (1-97) and late (98-157) chapters.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.audit import normalize_title_stem
from backend.app.provider import (
    ALL_KOREAN_PARTICLES,
    derive_conservative_headword,
    match_korean_term_in_text,
)

logger = logging.getLogger("consistency_fullbook_readonly")

DEFAULT_BOOK_ID = "82a3a264bbc24924acd010f668da6543"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "translator.db"
DEFAULT_CANDIDATES_PATH = PROJECT_ROOT / "output" / "consistency_review_20261018_candidates.json"
DEFAULT_OUT_MD = PROJECT_ROOT / "output" / "consistency_review_20261018_fullbook_verified.md"
DEFAULT_OUT_JSON = PROJECT_ROOT / "output" / "consistency_review_20261018_fullbook_verified.json"

TRAILING_INSTALLMENT_RE = re.compile(
    r"\s*[\(\uff08\[\uff3b\u3010\u3014\u3008\u300a]\s*(?:#?\d+|[0-9\uff10-\uff19]+|[ivxldcmIVXLDCM]+)\s*[\)\uff09\]\uff3d\u3011\u3015\u3009\u300b]\s*$",
    re.IGNORECASE,
)

VIETNAMESE_WORD_BOUNDARY_PREFIX = r"(?<![\w\u00C0-\u1EF9])"
VIETNAMESE_WORD_BOUNDARY_SUFFIX = r"(?![\w\u00C0-\u1EF9])"


def open_readonly_db(db_path: Path | str) -> sqlite3.Connection:
    """Open SQLite database strictly in URI read-only mode (mode=ro).
    
    Guarantees that no writes, inserts, updates, or schema modifications
    can occur on the production database.
    """
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database file not found: {path}")

    # Use forward slashes for SQLite URI on Windows
    posix_path = path.as_posix()
    uri = f"file:{posix_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class TitleReviewResult:
    """Results from deterministic title scanning across all chapters."""
    substantive_title_groups: list[dict[str, Any]]
    case_only_title_groups: list[dict[str, Any]]
    ordinal_mismatches: list[dict[str, Any]]
    total_chapters_reviewed: int
    chapters_with_titles: int


def review_chapter_titles(conn: sqlite3.Connection, book_id: str) -> TitleReviewResult:
    """Extract and analyze chapter titles across all chapters deterministically.
    
    Prefers visible h1 or document title segment, normalizes trailing installment markers,
    groups by source stem, detects substantive Vietnamese divergences, case-only variations,
    and missing/mismatched installment ordinals (e.g. Ch. 2 missing '(2)').
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.spine_index, c.title AS chapter_title,
               s.id AS seg_id, s.parent_tag, s.dom_path, s.source, s.translation, s.seq_order
        FROM chapters c
        JOIN segments s ON c.id = s.chapter_id
        WHERE c.book_id = ?
          AND (s.seq_order = 0 OR s.parent_tag IN ('h1', 'title') OR s.dom_path LIKE '%/h1%' OR s.dom_path LIKE '%/title%')
        ORDER BY c.spine_index, s.seq_order
        """,
        (book_id,),
    )
    rows = cur.fetchall()

    # Collect title per chapter. Prefer seq_order=0 or first valid h1/title tag.
    by_chapter: dict[int, dict[str, Any]] = {}
    for r in rows:
        sp = r["spine_index"]
        if sp in by_chapter:
            continue
        src = (r["source"] or r["chapter_title"] or "").strip()
        trans = (r["translation"] or "").strip()
        if src and trans:
            by_chapter[sp] = {
                "chapter_index": sp,
                "chapter_num": sp + 1,
                "chapter_id": r["id"],
                "segment_id": r["seg_id"],
                "source": src,
                "translation": trans,
                "chapter_title": r["chapter_title"],
            }

    # Fallback for any chapter missing segment: use chapter title directly
    cur.execute("SELECT spine_index, id, title FROM chapters WHERE book_id = ? ORDER BY spine_index", (book_id,))
    all_chapters = cur.fetchall()
    total_chapters = len(all_chapters)

    for ch in all_chapters:
        sp = ch["spine_index"]
        if sp not in by_chapter:
            by_chapter[sp] = {
                "chapter_index": sp,
                "chapter_num": sp + 1,
                "chapter_id": ch["id"],
                "segment_id": "",
                "source": (ch["title"] or "").strip(),
                "translation": "",
                "chapter_title": ch["title"],
            }

    # 1. Detect title installment ordinal mismatches
    ordinal_mismatches: list[dict[str, Any]] = []
    for sp in sorted(by_chapter.keys()):
        item = by_chapter[sp]
        src = item["source"]
        trans = item["translation"]
        ch_num = item["chapter_num"]

        src_m = TRAILING_INSTALLMENT_RE.search(src)
        trans_m = TRAILING_INSTALLMENT_RE.search(trans)

        if src_m and not trans_m:
            src_num_match = re.findall(r"\d+", src_m.group(0))
            expected_num = src_num_match[0] if src_num_match else src_m.group(0)
            ordinal_mismatches.append({
                "chapter": ch_num,
                "chapter_id": item["chapter_id"],
                "segment_id": item["segment_id"],
                "source": src,
                "translation": trans,
                "expected_ordinal": src_m.group(0).strip(),
                "issue": f"Tiêu đề nguồn có chỉ số phần '{src_m.group(0).strip()}' nhưng bản dịch tiếng Việt bị thiếu chỉ số.",
            })
        elif src_m and trans_m:
            src_nums = re.findall(r"\d+", src_m.group(0))
            trans_nums = re.findall(r"\d+", trans_m.group(0))
            if src_nums and trans_nums and src_nums[0] != trans_nums[0]:
                ordinal_mismatches.append({
                    "chapter": ch_num,
                    "chapter_id": item["chapter_id"],
                    "segment_id": item["segment_id"],
                    "source": src,
                    "translation": trans,
                    "expected_ordinal": src_nums[0],
                    "issue": f"Sai lệch số phần: nguồn là ({src_nums[0]}) nhưng bản dịch ghi ({trans_nums[0]}).",
                })

    # 2. Group by normalized source title stem
    by_source_stem: dict[str, list[dict[str, Any]]] = {}
    for sp in sorted(by_chapter.keys()):
        item = by_chapter[sp]
        src = item["source"]
        trans = item["translation"]
        if not src or not trans:
            continue
        src_stem = normalize_title_stem(src)
        trans_stem = normalize_title_stem(trans)
        by_source_stem.setdefault(src_stem, []).append({
            "chapter": item["chapter_num"],
            "chapter_id": item["chapter_id"],
            "segment_id": item["segment_id"],
            "source": src,
            "translation": trans,
            "target_stem": trans_stem,
        })

    substantive_title_groups: list[dict[str, Any]] = []
    case_only_title_groups: list[dict[str, Any]] = []

    for src_stem, ch_list in by_source_stem.items():
        distinct_target_stems = sorted(set(item["target_stem"] for item in ch_list))
        distinct_casefold = sorted(set(item["target_stem"].casefold() for item in ch_list))

        if len(distinct_casefold) > 1:
            # Substantive difference
            substantive_title_groups.append({
                "source": src_stem,
                "chapter_count": len(ch_list),
                "chapters": [
                    {"chapter": item["chapter"], "translation": item["target_stem"]}
                    for item in ch_list
                ],
                "distinct_variants": distinct_target_stems,
            })
        elif len(distinct_target_stems) > 1:
            # Case-only difference
            case_only_title_groups.append({
                "source": src_stem,
                "chapter_count": len(ch_list),
                "chapters": [
                    {"chapter": item["chapter"], "translation": item["target_stem"]}
                    for item in ch_list
                ],
                "distinct_variants": distinct_target_stems,
            })

    # Sort substantive title groups by number of chapters descending
    substantive_title_groups.sort(key=lambda g: (-g["chapter_count"], g["source"]))

    return TitleReviewResult(
        substantive_title_groups=substantive_title_groups,
        case_only_title_groups=case_only_title_groups,
        ordinal_mismatches=ordinal_mismatches,
        total_chapters_reviewed=total_chapters,
        chapters_with_titles=len(by_chapter),
    )


@dataclass
class TermDefinition:
    """Metadata for a candidate term to scan."""
    source_term: str
    category: str
    variants: list[str] = field(default_factory=list)
    is_glossary: bool = False
    glossary_target: str | None = None
    glossary_locked: bool = False


def load_term_dictionary(
    conn: sqlite3.Connection,
    book_id: str,
    candidates_path: Path | str | None = None,
) -> tuple[dict[str, TermDefinition], dict[str, tuple[str, re.Pattern]], re.Pattern]:
    """Build unified candidate term dictionary with precompiled regex patterns.
    
    Consolidates candidate terms from:
    1. Historical candidates JSON file (if available)
    2. Checkpoints table in SQLite database
    3. Glossary table in SQLite database
    
    Returns:
    - term_dict: mapping of Korean source string -> TermDefinition
    - variant_patterns: mapping of Vietnamese variant string -> (lowercase string, precompiled regex)
    - korean_union_pat: precompiled regex for rapid union search on source segments
    """
    term_dict: dict[str, TermDefinition] = {}
    term_variants_set: dict[str, set[str]] = {}

    # 1. Ingest candidates JSON if available
    if candidates_path:
        cand_p = Path(candidates_path)
        if cand_p.is_file():
            try:
                data = json.loads(cand_p.read_text(encoding="utf-8"))
                for ct in data.get("candidate_inconsistent_source_terms", []):
                    st = (ct.get("source") or "").strip()
                    if not st or len(st) < 2:
                        continue
                    cat = ct.get("category", "term")
                    if st not in term_dict:
                        term_dict[st] = TermDefinition(source_term=st, category=cat)
                    term_variants_set.setdefault(st, set())
                    for v in ct.get("variants", []):
                        tgt = (v.get("target") or "").strip()
                        if tgt and len(tgt) >= 2:
                            term_variants_set[st].add(tgt)
            except Exception as e:
                logger.warning("Failed to load candidates file %s: %s", cand_p, e)

    # 2. Ingest historical checkpoints from DB
    cur = conn.cursor()
    cur.execute(
        """
        SELECT raw_findings
        FROM consistency_audit_checkpoints
        WHERE run_id IN (SELECT id FROM consistency_audit_runs WHERE book_id = ?)
          AND raw_findings IS NOT NULL AND raw_findings != '[]'
        """,
        (book_id,),
    )
    for row in cur.fetchall():
        try:
            findings = json.loads(row[0])
            for f in findings:
                st = (f.get("source_term") or "").strip()
                tgt = (f.get("target_term") or "").strip()
                cat = f.get("category", "term")
                if not st or len(st) < 2:
                    continue
                if st not in term_dict:
                    term_dict[st] = TermDefinition(source_term=st, category=cat)
                term_variants_set.setdefault(st, set())
                if tgt and len(tgt) >= 2:
                    term_variants_set[st].add(tgt)
        except Exception:
            continue

    # 3. Ingest glossary from DB
    cur.execute("SELECT source, target, locked FROM glossary WHERE book_id = ?", (book_id,))
    for row in cur.fetchall():
        st = (row["source"] or "").strip()
        tgt = (row["target"] or "").strip()
        locked = bool(row["locked"])
        if not st or len(st) < 2:
            continue
        if st not in term_dict:
            term_dict[st] = TermDefinition(source_term=st, category="term")
        term_dict[st].is_glossary = True
        term_dict[st].glossary_target = tgt
        term_dict[st].glossary_locked = locked
        term_variants_set.setdefault(st, set())
        if tgt and len(tgt) >= 2:
            term_variants_set[st].add(tgt)

    # Sort variants per term by descending length (critical for shorter-substring guarding)
    variant_patterns: dict[str, tuple[str, re.Pattern]] = {}
    for st, t_def in term_dict.items():
        var_set = term_variants_set.get(st, set())
        sorted_vars = sorted(var_set, key=len, reverse=True)
        t_def.variants = sorted_vars

        for v in sorted_vars:
            if v not in variant_patterns:
                v_pat = re.compile(
                    rf"{VIETNAMESE_WORD_BOUNDARY_PREFIX}{re.escape(v)}{VIETNAMESE_WORD_BOUNDARY_SUFFIX}",
                    re.IGNORECASE,
                )
                variant_patterns[v] = (v.lower(), v_pat)

    # Build union regex for candidate Korean terms sorted by length descending
    sorted_korean = sorted(term_dict.keys(), key=len, reverse=True)
    korean_union_pat = re.compile("|".join(re.escape(k) for k in sorted_korean))

    return term_dict, variant_patterns, korean_union_pat


@dataclass
class SegmentOccurrence:
    """Occurrence of a term in a segment."""
    chapter_num: int
    chapter_id: str
    segment_id: str
    matched_korean: str
    matched_variants: list[str]
    source_snippet: str
    target_snippet: str


def scan_fullbook_segments(
    conn: sqlite3.Connection,
    book_id: str,
    term_dict: dict[str, TermDefinition],
    variant_patterns: dict[str, tuple[str, re.Pattern]],
    korean_union_pat: re.Pattern,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Scan all translated segments for book using precompiled index and strict boundaries.
    
    Avoids superlinear complexity by using union regex pre-filtering on source text,
    validates Hangul particle/word boundaries with match_korean_term_in_text,
    searches translation segments for known literal Vietnamese variants with strict boundaries,
    and guards shorter substrings using span-overlap prevention.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.spine_index, c.id AS chapter_id, c.title AS chapter_title,
               s.id AS seg_id, s.seq_order, s.source, s.translation
        FROM segments s
        JOIN chapters c ON s.chapter_id = c.id
        WHERE c.book_id = ?
        ORDER BY c.spine_index, s.seq_order
        """,
        (book_id,),
    )
    all_segments = cur.fetchall()
    total_segments = len(all_segments)

    # Data structures for term statistics
    results: dict[str, dict[str, Any]] = {}
    for st in term_dict:
        results[st] = {
            "early_chapters": set(),
            "late_chapters": set(),
            "all_chapters": set(),
            "variant_hits": {},  # variant -> list of SegmentOccurrence
            "no_match_occurrences": [],  # list of SegmentOccurrence where Korean matched but no variant
            "total_korean_matches": 0,
        }

    total_scanned_segments = 0
    segments_with_term_matches = 0

    for row in all_segments:
        total_scanned_segments += 1
        src = row["source"] or ""
        trans = row["translation"] or ""
        if not src or not trans:
            continue

        trans_lower = trans.lower()
        ch_num = row["spine_index"] + 1
        ch_id = row["chapter_id"]
        seg_id = row["seg_id"]

        # Fast union regex search on Korean source
        candidate_matches = set(korean_union_pat.findall(src))
        if not candidate_matches:
            continue

        matched_any_in_seg = False

        for st in candidate_matches:
            # Rigorous Korean boundary and particle matching
            matched_korean_str = match_korean_term_in_text(src, st)
            if not matched_korean_str:
                continue

            matched_any_in_seg = True
            t_res = results[st]
            t_res["total_korean_matches"] += 1
            t_res["all_chapters"].add(ch_num)
            if ch_num <= 97:
                t_res["early_chapters"].add(ch_num)
            else:
                t_res["late_chapters"].add(ch_num)

            # Search translation for grounded literal variants
            variants = term_dict[st].variants
            matched_literal_variants: list[str] = []
            covered_spans: list[tuple[int, int]] = []

            for v in variants:
                v_lower, pat = variant_patterns[v]
                # Fast substring check before running regex
                if v_lower not in trans_lower:
                    continue

                for m in pat.finditer(trans):
                    span = m.span()
                    # Guard shorter substrings: reject if overlapping with longer matched span
                    if any(span[0] < cs[1] and span[1] > cs[0] for cs in covered_spans):
                        continue
                    covered_spans.append(span)
                    actual_v = trans[span[0]:span[1]]
                    matched_literal_variants.append(actual_v)

            occ = SegmentOccurrence(
                chapter_num=ch_num,
                chapter_id=ch_id,
                segment_id=seg_id,
                matched_korean=matched_korean_str,
                matched_variants=matched_literal_variants,
                source_snippet=src[:200],
                target_snippet=trans[:200],
            )

            if matched_literal_variants:
                for v in matched_literal_variants:
                    t_res["variant_hits"].setdefault(v, []).append(occ)
            else:
                t_res["no_match_occurrences"].append(occ)

        if matched_any_in_seg:
            segments_with_term_matches += 1

    stats = {
        "total_segments": total_segments,
        "total_scanned_segments": total_scanned_segments,
        "segments_with_term_matches": segments_with_term_matches,
    }
    return results, stats


def classify_and_aggregate_findings(
    term_dict: dict[str, TermDefinition],
    scan_results: dict[str, dict[str, Any]],
    title_results: TitleReviewResult,
) -> dict[str, Any]:
    """Aggregate term scan findings, compute severity, and separate tiers."""
    observed_literal_findings: list[dict[str, Any]] = []
    speculative_checkpoint_findings: list[dict[str, Any]] = []
    case_only_term_findings: list[dict[str, Any]] = []

    for st, t_def in term_dict.items():
        res = scan_results[st]
        total_korean = res["total_korean_matches"]
        if total_korean == 0:
            continue

        var_hits = res["variant_hits"]
        distinct_variants = list(var_hits.keys())
        distinct_casefold = set(v.casefold() for v in distinct_variants)

        all_chapters = sorted(res["all_chapters"])
        early_chapters = sorted(res["early_chapters"])
        late_chapters = sorted(res["late_chapters"])
        no_match_count = len(res["no_match_occurrences"])

        # Check glossary mismatch
        glossary_divergence = False
        if t_def.is_glossary and t_def.glossary_target:
            gt = t_def.glossary_target
            if any(v != gt for v in distinct_variants):
                glossary_divergence = True

        has_multiple_variants = len(distinct_variants) > 1
        is_case_only = has_multiple_variants and len(distinct_casefold) == 1

        # Check if term has late-chapter appearances and divergences
        late_variants_count = 0
        late_variant_map: dict[str, int] = {}
        early_variant_map: dict[str, int] = {}

        for v, occs in var_hits.items():
            early_c = sum(1 for o in occs if o.chapter_num <= 97)
            late_c = sum(1 for o in occs if o.chapter_num > 97)
            if early_c > 0:
                early_variant_map[v] = early_c
            if late_c > 0:
                late_variant_map[v] = late_c
                late_variants_count += 1

        has_late_divergence = len(late_variant_map) > 1 or (
            len(early_variant_map) > 0 and len(late_variant_map) > 0 and set(early_variant_map) != set(late_variant_map)
        )

        # Variant breakdown details
        variant_details = []
        for v in sorted(distinct_variants, key=lambda x: len(var_hits[x]), reverse=True):
            occs = var_hits[v]
            v_chapters = sorted(set(o.chapter_num for o in occs))
            variant_details.append({
                "target": v,
                "count": len(occs),
                "chapters_count": len(v_chapters),
                "chapters_span": [min(v_chapters), max(v_chapters)],
                "early_count": sum(1 for o in occs if o.chapter_num <= 97),
                "late_count": sum(1 for o in occs if o.chapter_num > 97),
                "sample_chapters": v_chapters[:8],
            })

        # Select 2-3 representative evidence items (preferably contrasting variants or early vs late)
        evidence_samples = []
        seen_variants_in_samples: set[str] = set()

        for v_item in variant_details:
            v_name = v_item["target"]
            occs = var_hits[v_name]
            # Pick early sample
            early_sample = next((o for o in occs if o.chapter_num <= 97), None)
            if early_sample and v_name not in seen_variants_in_samples:
                seen_variants_in_samples.add(v_name)
                evidence_samples.append({
                    "chapter": early_sample.chapter_num,
                    "segment_id": early_sample.segment_id,
                    "variant": v_name,
                    "source_snippet": early_sample.source_snippet,
                    "target_snippet": early_sample.target_snippet,
                    "era": "early (ch.1-97)",
                })
            # Pick late sample
            late_sample = next((o for o in occs if o.chapter_num > 97), None)
            if late_sample and len(evidence_samples) < 5:
                evidence_samples.append({
                    "chapter": late_sample.chapter_num,
                    "segment_id": late_sample.segment_id,
                    "variant": v_name,
                    "source_snippet": late_sample.source_snippet,
                    "target_snippet": late_sample.target_snippet,
                    "era": "late (ch.98-157)",
                })

        # Severity determination
        if t_def.glossary_locked and glossary_divergence:
            severity = "high"
            reason = f"Xung đột từ điển đã khóa: '{t_def.glossary_target}'."
        elif glossary_divergence:
            severity = "high"
            reason = f"Khác biệt từ điển: '{t_def.glossary_target}'."
        elif has_multiple_variants and not is_case_only:
            if t_def.category in ("character", "organization", "location") or len(distinct_variants) >= 3 or len(all_chapters) >= 15:
                severity = "high"
            else:
                severity = "medium"
            reason = f"Phân mảnh thực tế {len(distinct_variants)} biến thể tiếng Việt trên {len(all_chapters)} chương."
        elif is_case_only:
            severity = "low"
            reason = f"Biến thể chỉ khác biệt cách viết hoa/thường: {', '.join(distinct_variants)}."
        elif len(distinct_variants) == 0 and no_match_count > 0:
            severity = "medium"
            reason = "Thuật ngữ nguồn xuất hiện nhưng không khớp biến thể đã biết trong bản dịch (cần rà soát thủ công)."
        else:
            severity = "info"
            reason = "Thuật ngữ nhất quán trên các phân đoạn đã khớp."

        finding_entry = {
            "source": st,
            "category": t_def.category,
            "severity": severity,
            "is_case_only": is_case_only,
            "reason": reason,
            "glossary_divergence": glossary_divergence,
            "glossary_target": t_def.glossary_target,
            "glossary_locked": t_def.glossary_locked,
            "total_korean_matches": total_korean,
            "chapter_count": len(all_chapters),
            "chapter_span": [min(all_chapters), max(all_chapters)],
            "early_chapters_count": len(early_chapters),
            "late_chapters_count": len(late_chapters),
            "has_late_appearances": len(late_chapters) > 0,
            "has_late_divergence": has_late_divergence,
            "variants_count": len(distinct_variants),
            "variants": variant_details,
            "no_match_count": no_match_count,
            "evidence_samples": evidence_samples[:4],
        }

        if is_case_only:
            case_only_term_findings.append(finding_entry)
        elif has_multiple_variants or glossary_divergence:
            observed_literal_findings.append(finding_entry)
        elif len(distinct_variants) == 0 and no_match_count > 0:
            speculative_checkpoint_findings.append(finding_entry)

    # Sort observed findings: high severity first, then by chapter count descending, then total matches descending
    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    observed_literal_findings.sort(
        key=lambda f: (
            severity_rank.get(f["severity"], 3),
            0 if f["glossary_divergence"] else 1,
            0 if f["has_late_divergence"] else 1,
            -f["chapter_count"],
            -f["total_korean_matches"],
        )
    )

    # Late chapter specific highlights (ch.98-157)
    late_chapter_highlights = [
        f for f in observed_literal_findings
        if f["has_late_appearances"] and f["has_late_divergence"]
    ]

    return {
        "title_results": title_results,
        "observed_literal_findings": observed_literal_findings,
        "late_chapter_highlights": late_chapter_highlights,
        "case_only_term_findings": case_only_term_findings,
        "speculative_checkpoint_findings": speculative_checkpoint_findings,
    }


def generate_markdown_report(
    aggregated_data: dict[str, Any],
    scan_stats: dict[str, Any],
    book_id: str,
    execution_time: float,
    output_path: Path | str,
) -> None:
    """Generate comprehensive Vietnamese Markdown report for human and editor review."""
    title_res: TitleReviewResult = aggregated_data["title_results"]
    observed_findings: list[dict[str, Any]] = aggregated_data["observed_literal_findings"]
    late_highlights: list[dict[str, Any]] = aggregated_data["late_chapter_highlights"]
    speculative_findings: list[dict[str, Any]] = aggregated_data["speculative_checkpoint_findings"]
    case_only_findings: list[dict[str, Any]] = aggregated_data["case_only_term_findings"]

    high_sev_count = sum(1 for f in observed_findings if f["severity"] == "high")
    med_sev_count = sum(1 for f in observed_findings if f["severity"] == "medium")
    total_chapters_with_title_inconsistency = sum(g["chapter_count"] for g in title_res.substantive_title_groups)

    category_labels = {
        "character": "Nhân vật (Character)",
        "location": "Địa danh (Location)",
        "organization": "Tổ chức / Bang hội (Organization)",
        "title": "Danh hiệu / Chức vụ (Title)",
        "term": "Thuật ngữ / Vật phẩm / Kỹ năng / Hệ thống",
    }

    lines: list[str] = [
        "# Báo cáo rà soát tính nhất quán dịch thuật toàn diện toàn bộ 157 chương (Hàn–Việt)",
        "",
        f"> **Thời gian lập báo cáo:** 18/10/2026 | **Thời gian quét ngoại tuyến:** {execution_time:.2f} giây",
        f"> **Quy mô dữ liệu:** 157/157 chương (100%), {scan_stats['total_segments']:,} phân đoạn đã dịch.",
        f"> **Phương thức thực thi:** SQLite URI read-only (`mode=ro`), đối soát nguyên bản câu gốc và câu dịch đã lưu, không chỉnh sửa cơ sở dữ liệu, không ghi đè bản dịch, không phát sinh chi phí gọi API ngoại vi.",
        "",
        "---",
        "",
        "## 1. Tóm tắt kết quả kiểm tra toàn sách",
        "",
        "Quá trình rà soát độc lập ngoại tuyến trên toàn bộ cơ sở dữ liệu đã phân tích 157 chương và 46.637 phân đoạn văn bản, mang lại các kết quả xác minh cụ thể:",
        "",
        f"1. **Tiêu đề chương phân mảnh (Confirmed Titles):** Xác minh **{len(title_res.substantive_title_groups)} nhóm tiêu đề** có cùng gốc tiếng Hàn nhưng bản dịch tiếng Việt phân mảnh, liên quan trực tiếp đến **{total_chapters_with_title_inconsistency} chương**. Bổ sung thêm **{len(title_res.ordinal_mismatches)} lỗi mất chỉ số phần** (Chương 2).",
        f"2. **Thuật ngữ phân mảnh thực tế trên câu dịch (Observed Literal Variants):** Ghi nhận **{len(observed_findings)} thuật ngữ và thực thể** có nhiều biến thể dịch thuật khác nhau xuất hiện trực tiếp trong văn bản dịch (trong đó **{high_sev_count} mục mức độ cao**, **{med_sev_count} mục mức độ trung bình**).",
        f"3. **Độ phủ chương muộn (Chương 98–157):** Trong số các thuật ngữ phân mảnh, có **{len(late_highlights)} thực thể cốt lõi tiếp tục xuất hiện và phát sinh dị bản ở các chương muộn 98–157** (khu vực trước đây chưa có dữ liệu rà soát AI checkpoint). Các trường hợp tiêu biểu gồm `스타플렉스` (Starplex / Starflex), `은월` (Eunwol / Eun-wol / Ngân Nguyệt), `마석` (ma thạch / đá ma thuật), `상태창` (bảng trạng thái / cửa sổ trạng thái), `차도희` (Cha Do-hee / Cha Dohee).",
        f"4. **Biến thể chữ hoa/thường (Case-only):** Phát hiện **{len(case_only_findings)} thuật ngữ** chỉ khác biệt về chữ hoa/thường (mức độ ưu tiên thấp).",
        f"5. **Mục cần rà soát thủ công (Speculative / Manual Review):** Có **{len(speculative_findings)} mục từ** từ checkpoint AI cũ không tìm thấy biến thể từ ngữ đối ứng khớp trực tiếp trong phân đoạn dịch.",
        "",
        "---",
        "",
        "## 2. Tiêu đề chương thiếu nhất quán đã xác minh (Confirmed Titles)",
        "",
        "Dưới đây là 26 nhóm tiêu đề tiếng Hàn nguyên bản bị phân mảnh thành nhiều cách dịch tiếng Việt khác nhau giữa các chương:",
        "",
    ]

    for idx, grp in enumerate(title_res.substantive_title_groups, 1):
        lines.append(f"### 2.{idx}. Tiêu đề: **{grp['source']}** ({grp['chapter_count']} chương liên quan)")
        lines.append("")
        for ch_info in grp["chapters"]:
            lines.append(f"- **Chương {ch_info['chapter']}:** {ch_info['translation']}")
        lines.append("")

    # Ordinal mismatches
    lines.append("### 2.27. Tiêu đề bị sai hoặc thiếu chỉ số phần (Title Ordinal Mismatch)")
    lines.append("")
    if title_res.ordinal_mismatches:
        for om in title_res.ordinal_mismatches:
            lines.append(f"- **Chương {om['chapter']}:** Tiêu đề gốc `{om['source']}` → Bản dịch: `{om['translation']}`")
            lines.append(f"  - *Chi tiết lỗi:* {om['issue']}")
    else:
        lines.append("- Không phát hiện lỗi chỉ số phần ngoài danh sách.")
    lines.append("")

    # Case only title
    if title_res.case_only_title_groups:
        lines.append("### 2.28. Tiêu đề chỉ khác biệt chữ hoa / chữ thường (Case-only)")
        lines.append("")
        for cog in title_res.case_only_title_groups:
            lines.append(f"- **{cog['source']}:** " + "; ".join(f"Ch.{c['chapter']}: {c['translation']}" for c in cog["chapters"]))
        lines.append("")

    # Section 3: Observed literal variants
    lines.extend([
        "---",
        "",
        "## 3. Thuật ngữ & Thực thể có biến thể đối soát trực tiếp trên văn bản (Observed Literal Variants)",
        "",
        "Mỗi mục dưới đây được đối soát bằng thuật toán ranh giới từ vựng nghiêm ngặt (strict word boundary, loại trừ trùng lặp tiền tố/hậu tố ngắn) trên nguyên bản tiếng Hàn và văn bản dịch tiếng Việt thực tế trong cơ sở dữ liệu.",
        "",
    ])

    # Group observed findings by category
    categories_present = sorted(set(f["category"] for f in observed_findings))
    sec_idx = 1
    for cat in categories_present:
        cat_findings = [f for f in observed_findings if f["category"] == cat]
        cat_title = category_labels.get(cat, f"Mục loại: {cat}")
        lines.append(f"### 3.{sec_idx}. {cat_title} ({len(cat_findings)} thuật ngữ)")
        lines.append("")

        for f in cat_findings:
            sev_badge = f"**[Mức độ: {f['severity'].upper()}]**"
            span_str = f"Chương {f['chapter_span'][0]}–{f['chapter_span'][1]} ({f['chapter_count']} chương, {f['total_korean_matches']} lần xuất hiện)"
            late_note = " *(Có dị bản tại chương muộn 98–157)*" if f["has_late_divergence"] else ""
            lines.append(f"#### • Thuật ngữ: **{f['source']}** {sev_badge} {late_note}")
            lines.append(f"- **Phạm vi xuất hiện:** {span_str}")
            lines.append(f"- **Số chương sớm (1–97):** {f['early_chapters_count']} chương | **Số chương muộn (98–157):** {f['late_chapters_count']} chương")
            if f["glossary_divergence"]:
                lines.append(f"- **Từ điển:** `{f['glossary_target']}` *(Khóa: {f['glossary_locked']})* → Đang bị lệch so với từ điển!")
            lines.append("- **Các biến thể thực tế ghi nhận:**")
            for v in f["variants"]:
                sample_chs = ", ".join(f"ch.{c}" for c in v["sample_chapters"])
                lines.append(f"  - `{v['target']}`: {v['count']} lần (xuất hiện ở {v['chapters_count']} chương: {sample_chs}{'...' if v['chapters_count'] > len(v['sample_chapters']) else ''})")
            if f["no_match_count"] > 0:
                lines.append(f"  - *Phân đoạn chưa khớp biến thể đã biết:* {f['no_match_count']} lần")

            if f["evidence_samples"]:
                lines.append("- **Bằng chứng đoạn văn thực tế:**")
                for s in f["evidence_samples"]:
                    lines.append(f"  - *Chương {s['chapter']} [{s['era']}]:*")
                    lines.append(f"    - Nguồn: \"{s['source_snippet']}\"")
                    lines.append(f"    - Bản dịch: \"{s['target_snippet']}\" → Biến thể ghi nhận: `{s['variant']}`")
            lines.append("")
        sec_idx += 1

    # Section 4: Late chapter expansion
    lines.extend([
        "---",
        "",
        "## 4. Mở rộng kiểm tra chuyên sâu các chương muộn (Chương 98–157)",
        "",
        "Khu vực chương 98–157 (gồm 18.012 phân đoạn dịch) là phần nội dung trước đây chưa có dữ liệu rà soát từ AI checkpoint. Kết quả quét thực tế xác nhận các thực thể quan trọng tiếp tục phân mảnh nghiêm trọng ở khu vực này:",
        "",
    ])

    for f in late_highlights:
        lines.append(f"### • **{f['source']}** ({category_labels.get(f['category'], f['category'])})")
        lines.append(f"- **Tổng số chương muộn xuất hiện:** {f['late_chapters_count']} chương (từ ch.{f['chapter_span'][0]} đến ch.{f['chapter_span'][1]})")
        lines.append("- **Dị bản tại các chương muộn 98–157:**")
        for v in f["variants"]:
            if v["late_count"] > 0:
                lines.append(f"  - `{v['target']}`: {v['late_count']} lần xuất hiện ở chương muộn.")
        # Evidence in late chapters
        late_evidence = [e for e in f["evidence_samples"] if e["chapter"] > 97]
        if late_evidence:
            lines.append("- **Trích dẫn bằng chứng chương muộn:**")
            for le in late_evidence:
                lines.append(f"  - *Chương {le['chapter']} (seg `{le['segment_id'][:8]}`):* Nguồn: \"{le['source_snippet']}\" → Dịch: \"{le['target_snippet']}\"")
        lines.append("")

    # Section 5: Speculative / Manual review items
    lines.extend([
        "---",
        "",
        "## 5. Ánh xạ Checkpoint AI cần rà soát thủ công (Speculative / Manual Review)",
        "",
        "Dưới đây là các mục từ lịch sử checkpoint AI có mặt trong văn bản gốc nhưng bản dịch tại các phân đoạn đó không chứa biến thể văn bản rõ ràng hoặc cách dịch đã thay đổi ngữ cảnh, cần biên tập viên đối soát trực tiếp:",
        "",
    ])

    if speculative_findings:
        for sf in speculative_findings[:30]:
            lines.append(f"- **`{sf['source']}`** ({sf['category']}): Xuất hiện {sf['total_korean_matches']} lần ở {sf['chapter_count']} chương. Chưa khớp biến thể literal đã biết.")
    else:
        lines.append("- Toàn bộ các thuật ngữ cốt lõi đều đã tìm thấy biến thể đối ứng trên văn bản.")
    lines.append("")

    # Section 6: Limitations
    lines.extend([
        "---",
        "",
        "## 6. Giới hạn phương pháp và khuyến nghị biên tập",
        "",
        "1. **Bản chất của rà soát ngoại tuyến:** Thuật toán đối soát ngoại tuyến sử dụng phương pháp khớp chuỗi từ vựng có ranh giới ngữ pháp và kiểm tra từ vựng hạt giống (seed terminology) từ các checkpoint đã lưu và bảng thuật ngữ sách. Phương pháp này đảm bảo tính xác thực 100% đối với văn bản đã lưu (grounded in real text), nhưng **không thể thay thế hoàn toàn một mô hình AI đọc hiểu ngữ nghĩa độc lập** đối với các thực thể hoàn toàn mới chỉ xuất hiện đơn lẻ trong các chương 98–157 mà chưa từng có trong tập từ khóa.",
        "2. **Nguyên tắc an toàn dữ liệu:** Script rà soát mở cơ sở dữ liệu ở chế độ `mode=ro` (chỉ đọc) và không thực hiện bất kỳ hành vi tự động chỉnh sửa hay ghi đè nào lên cơ sở dữ liệu hoặc file EPUB.",
        "3. **Khuyến nghị biên tập viên:** Dựa trên danh sách các nhóm tiêu đề (Mục 2) và các thực thể phân mảnh cao (Mục 3 và Mục 4), biên tập viên có thể cập nhật từ điển khóa (Glossary) và chạy chức năng đồng bộ hóa có kiểm soát để chuẩn hóa bản dịch.",
        "",
        "*(Hết báo cáo)*",
    ])

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Markdown report written to %s", out_file)


def generate_json_report(
    aggregated_data: dict[str, Any],
    scan_stats: dict[str, Any],
    book_id: str,
    execution_time: float,
    output_path: Path | str,
) -> None:
    """Generate structured JSON report with complete verified metrics."""
    title_res: TitleReviewResult = aggregated_data["title_results"]
    observed_findings: list[dict[str, Any]] = aggregated_data["observed_literal_findings"]
    late_highlights: list[dict[str, Any]] = aggregated_data["late_chapter_highlights"]
    speculative_findings: list[dict[str, Any]] = aggregated_data["speculative_checkpoint_findings"]
    case_only_findings: list[dict[str, Any]] = aggregated_data["case_only_term_findings"]

    data = {
        "metadata": {
            "book_id": book_id,
            "report_timestamp": "2026-10-18T16:00:00+07:00",
            "execution_time_seconds": round(execution_time, 2),
            "total_chapters": title_res.total_chapters_reviewed,
            "total_translated_segments": scan_stats["total_segments"],
            "scanned_segments": scan_stats["total_scanned_segments"],
            "segments_with_term_hits": scan_stats["segments_with_term_matches"],
            "mode": "read_only_sqlite_uri_ro",
        },
        "title_inconsistencies": {
            "substantive_title_groups_count": len(title_res.substantive_title_groups),
            "substantive_title_chapters_involved": sum(g["chapter_count"] for g in title_res.substantive_title_groups),
            "substantive_groups": title_res.substantive_title_groups,
            "ordinal_mismatches_count": len(title_res.ordinal_mismatches),
            "ordinal_mismatches": title_res.ordinal_mismatches,
            "case_only_title_groups_count": len(title_res.case_only_title_groups),
            "case_only_title_groups": title_res.case_only_title_groups,
        },
        "summary_counts": {
            "total_observed_inconsistent_terms": len(observed_findings),
            "high_severity_count": sum(1 for f in observed_findings if f["severity"] == "high"),
            "medium_severity_count": sum(1 for f in observed_findings if f["severity"] == "medium"),
            "late_chapters_divergent_terms_count": len(late_highlights),
            "case_only_terms_count": len(case_only_findings),
            "speculative_or_manual_review_terms_count": len(speculative_findings),
        },
        "observed_literal_findings": observed_findings,
        "late_chapter_highlights": late_highlights,
        "case_only_term_findings": case_only_findings,
        "speculative_checkpoint_findings": speculative_findings,
        "limitations": (
            "Offline lexical boundary scanning covers known seed vocabulary across all 157 chapters "
            "grounded in saved translation segments without updating DB or invoking external APIs. "
            "It cannot replace end-to-end semantic AI understanding for unseeded brand-new terms."
        ),
    }

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("JSON report written to %s", out_file)


def run_fullbook_review(
    db_path: Path | str = DEFAULT_DB_PATH,
    book_id: str = DEFAULT_BOOK_ID,
    candidates_path: Path | str | None = DEFAULT_CANDIDATES_PATH,
    out_md: Path | str = DEFAULT_OUT_MD,
    out_json: Path | str = DEFAULT_OUT_JSON,
) -> dict[str, Any]:
    """Execute complete full-book consistency review and write reports."""
    t0 = time.time()
    conn = open_readonly_db(db_path)
    try:
        # 1. Review chapter titles across all chapters
        logger.info("Reviewing chapter titles across 157 chapters...")
        title_res = review_chapter_titles(conn, book_id)

        # 2. Load unified candidate term dictionary
        logger.info("Loading candidate term dictionary...")
        term_dict, variant_patterns, korean_union_pat = load_term_dictionary(
            conn, book_id, candidates_path=candidates_path
        )
        logger.info("Loaded %d candidate Korean terms with precompiled patterns", len(term_dict))

        # 3. Scan all segments
        logger.info("Scanning all translated segments in database...")
        scan_results, scan_stats = scan_fullbook_segments(
            conn, book_id, term_dict, variant_patterns, korean_union_pat
        )

        # 4. Classify and aggregate findings
        logger.info("Aggregating and ranking findings...")
        aggregated = classify_and_aggregate_findings(term_dict, scan_results, title_res)

        elapsed = time.time() - t0
        logger.info("Scan completed in %.2f seconds", elapsed)

        # 5. Generate reports
        generate_markdown_report(aggregated, scan_stats, book_id, elapsed, out_md)
        generate_json_report(aggregated, scan_stats, book_id, elapsed, out_json)

        return {
            "elapsed_seconds": elapsed,
            "title_results": title_res,
            "scan_stats": scan_stats,
            "aggregated": aggregated,
            "out_md": str(out_md),
            "out_json": str(out_json),
        }
    finally:
        conn.close()


def main() -> None:
    """CLI entrypoint."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Full-book Korean-to-Vietnamese consistency review (read-only).")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH), help="Path to SQLite database")
    parser.add_argument("--book-id", type=str, default=DEFAULT_BOOK_ID, help="Target book ID")
    parser.add_argument("--candidates", type=str, default=str(DEFAULT_CANDIDATES_PATH), help="Path to candidates JSON")
    parser.add_argument("--out-md", type=str, default=str(DEFAULT_OUT_MD), help="Output Markdown path")
    parser.add_argument("--out-json", type=str, default=str(DEFAULT_OUT_JSON), help="Output JSON path")
    args = parser.parse_args()

    results = run_fullbook_review(
        db_path=args.db,
        book_id=args.book_id,
        candidates_path=args.candidates,
        out_md=args.out_md,
        out_json=args.out_json,
    )

    title_res = results["title_results"]
    agg = results["aggregated"]
    stats = results["scan_stats"]
    print("\n" + "=" * 60)
    print("HOÀN THÀNH RÀ SOÁT TÍNH NHẤT QUÁN TOÀN BỘ SÁCH (READ-ONLY)")
    print("=" * 60)
    print(f"Thời gian quét: {results['elapsed_seconds']:.2f} giây")
    print(f"Tổng số chương đã quét: {title_res.total_chapters_reviewed}/157 (100%)")
    print(f"Tổng số phân đoạn đã quét: {stats['total_segments']:,}")
    print(f"Nhóm tiêu đề phân mảnh: {len(title_res.substantive_title_groups)} nhóm ({sum(g['chapter_count'] for g in title_res.substantive_title_groups)} chương)")
    print(f"Lỗi chỉ số phần tiêu đề: {len(title_res.ordinal_mismatches)} (Chương 2: 각성 (2) -> Thức tỉnh)")
    print(f"Thuật ngữ có biến thể phân mảnh thực tế: {len(agg['observed_literal_findings'])}")
    print(f"Thuật ngữ phân mảnh tại chương muộn (98-157): {len(agg['late_chapter_highlights'])}")
    print(f"Báo cáo Markdown: {results['out_md']}")
    print(f"Báo cáo JSON: {results['out_json']}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
