"""Context builder and batching engine for EPUB translation.

Provides deterministic prompt/context generation, batching with size and segment
count bounds, glossary resolution with strict locked priority, whitespace
preservation, translation memory lookup, and structured ID validation.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class GlossaryEntry:
    """Represents a term in the book glossary."""

    source: str
    target: str
    locked: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to dictionary."""
        return asdict(self)


def normalize_segment(seg: Any) -> tuple[str, str]:
    """Extract (id, source_text) from a dict, TextSegment, or object."""
    if isinstance(seg, dict):
        seg_id = str(seg.get("id") or seg.get("segment_id") or "")
        source = str(seg.get("source") or seg.get("text") or "")
    else:
        seg_id = str(getattr(seg, "segment_id", getattr(seg, "id", "")))
        source = str(getattr(seg, "source", getattr(seg, "text", "")))
    return seg_id, source


def preserve_whitespace(source: str, translation: str) -> str:
    """Maintain surrounding whitespace around each text/tail node after translation.

    Preserves exact leading and trailing whitespace from the source string while
    replacing the inner content with the translation.
    """
    if not source:
        return translation

    leading_len = len(source) - len(source.lstrip())
    leading_ws = source[:leading_len]
    trailing_len = len(source) - len(source.rstrip())
    trailing_ws = source[len(source) - trailing_len :] if trailing_len > 0 else ""

    stripped_trans = translation.strip()
    if not stripped_trans:
        # If translation is blank or whitespace, preserve source whitespace
        return source if source.strip() == "" else translation

    return f"{leading_ws}{stripped_trans}{trailing_ws}"


def is_symbol_only_source(source: str) -> bool:
    """Safe explicit classifier for nonlinguistic symbol-only source text.

    Returns True if stripped source is nonempty and every character is either
    whitespace or in Unicode category P (Punctuation) or S (Symbol).
    Explicitly rejects any letters (L), numbers (N), and control characters (C)
    outside standard whitespace.
    """
    if not source:
        return False
    stripped = source.strip()
    if not stripped:
        return False
    for ch in source:
        if ch in (" ", "\t", "\n", "\r") or (ch.isspace() and unicodedata.category(ch).startswith("Z")):
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            continue
        return False
    return True


is_symbol_only = is_symbol_only_source

TRAILING_INSTALLMENT_RE = re.compile(
    r"\s*[\(\uff08\[\uff3b\u3010\u3014\u3008\u300a]\s*(?:#?\d+|[0-9\uff10-\uff19]+|[ivxldcmIVXLDCM]+)\s*[\)\uff09\]\uff3d\u3011\u3015\u3009\u300b]\s*$",
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


def is_h1_segment(seg: Any) -> bool:
    """Check if a segment corresponds to a visible <h1> heading."""
    if isinstance(seg, dict):
        parent_tag = str(seg.get("parent_tag") or "").strip().lower()
        dom_path = str(seg.get("dom_path") or "").lower()
    elif isinstance(seg, sqlite3.Row):
        parent_tag = str(seg["parent_tag"] if "parent_tag" in seg.keys() else "").strip().lower()
        dom_path = str(seg["dom_path"] if "dom_path" in seg.keys() else "").lower()
    else:
        parent_tag = str(getattr(seg, "parent_tag", "") or "").strip().lower()
        dom_path = str(getattr(seg, "dom_path", "") or "").lower()
    return parent_tag == "h1" or "/h1[" in dom_path or dom_path.endswith("/h1") or "/h1/" in dom_path


def is_title_segment(seg: Any) -> bool:
    """Check if a segment corresponds to a document <title> tag."""
    if isinstance(seg, dict):
        parent_tag = str(seg.get("parent_tag") or "").strip().lower()
        dom_path = str(seg.get("dom_path") or "").lower()
    elif isinstance(seg, sqlite3.Row):
        parent_tag = str(seg["parent_tag"] if "parent_tag" in seg.keys() else "").strip().lower()
        dom_path = str(seg["dom_path"] if "dom_path" in seg.keys() else "").lower()
    else:
        parent_tag = str(getattr(seg, "parent_tag", "") or "").strip().lower()
        dom_path = str(getattr(seg, "dom_path", "") or "").lower()
    return parent_tag == "title" or "/title[" in dom_path or dom_path.endswith("/title") or "/title/" in dom_path


def get_prior_chapter_title_mappings(
    db_path: Path | str,
    book_id: str,
    before_spine_index: int | None,
    glossary: list[dict[str, Any]] | None = None,
    max_entries: int = 50,
    max_chars: int = 1500,
) -> dict[str, str]:
    """Extract persistent, bounded, deterministic chapter title root mappings from chronological prior chapters.
    
    Reads already-saved translated segments from prior chapters (spine_index < before_spine_index).
    Extracts <h1> (preferred) or <title> segments, normalizes trailing installment markers like (1)/(2),
    gives strict priority to manually locked glossary entries, and detects conflicting historical variants
    deterministically without silently deciding incorrectly.
    """
    if before_spine_index is None or before_spine_index <= 0:
        return {}

    from backend.app.db import get_connection, list_glossary

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.chapter_id, s.source, s.translation, s.parent_tag, s.dom_path, c.spine_index
            FROM segments s
            JOIN chapters c ON s.chapter_id = c.id
            WHERE c.book_id = ?
              AND c.spine_index < ?
              AND s.is_whitespace = 0
              AND s.translation IS NOT NULL
              AND TRIM(s.translation) != ''
              AND s.source IS NOT NULL
              AND TRIM(s.source) != ''
              AND (
                  LOWER(s.parent_tag) IN ('h1', 'title')
                  OR LOWER(s.dom_path) LIKE '%/h1%'
                  OR LOWER(s.dom_path) LIKE '%/title%'
              )
            ORDER BY c.spine_index ASC, s.seq_order ASC
            """,
            (book_id, before_spine_index),
        ).fetchall()

    if not rows:
        return {}

    # Group rows by chapter_id in chronological spine order
    by_chapter: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_chapter.setdefault(r["chapter_id"], []).append(dict(r))

    by_source_stem: dict[str, list[str]] = {}

    for ch_id, ch_segs in by_chapter.items():
        # Prefer visible h1 segments, fallback to title
        h1_segs = [s for s in ch_segs if is_h1_segment(s)]
        title_segs = [s for s in ch_segs if is_title_segment(s)]
        candidate_segs = h1_segs if h1_segs else title_segs

        seen_in_chapter: set[tuple[str, str]] = set()
        for s in candidate_segs:
            raw_src = str(s.get("source") or "").strip()
            raw_tgt = str(s.get("translation") or "").strip()
            src_stem = normalize_title_stem(raw_src)
            tgt_stem = normalize_title_stem(raw_tgt)
            if not src_stem or not tgt_stem:
                continue
            pair = (src_stem, tgt_stem)
            if pair not in seen_in_chapter:
                seen_in_chapter.add(pair)
                by_source_stem.setdefault(src_stem, []).append(tgt_stem)

    if not by_source_stem:
        return {}

    # Load glossary if not passed
    if glossary is None:
        glossary = list_glossary(db_path, book_id)

    locked_glossary: dict[str, str] = {}
    unlocked_glossary: dict[str, str] = {}
    for g in glossary:
        if isinstance(g, dict):
            raw_g_src = str(g.get("source") or "").strip()
            raw_g_tgt = str(g.get("target") or "").strip()
            g_src = normalize_title_stem(raw_g_src)
            g_tgt = normalize_title_stem(raw_g_tgt)
            if not g_src or not g_tgt:
                continue
            if g.get("locked"):
                locked_glossary[g_src] = g_tgt
            else:
                unlocked_glossary[g_src] = g_tgt

    resolved_mappings: dict[str, str] = {}

    for src_stem, tgt_stems in by_source_stem.items():
        # 1. Manually locked glossary has STRICT PRIORITY
        if src_stem in locked_glossary:
            resolved_mappings[src_stem] = locked_glossary[src_stem]
            continue

        # 2. Check historical variants across prior chapters
        unique_targets = list(dict.fromkeys(tgt_stems))
        if len(unique_targets) == 1:
            # Unambiguous: all prior chapters agree
            resolved_mappings[src_stem] = unique_targets[0]
        else:
            # Multiple conflicting historical variants across prior chapters!
            # If unlocked glossary matches one of the variants, use it deterministically
            if src_stem in unlocked_glossary and unlocked_glossary[src_stem] in unique_targets:
                resolved_mappings[src_stem] = unlocked_glossary[src_stem]
            else:
                # CONFLICT: Do NOT silently pick an arbitrary variant.
                # Omit to avoid leaking conflicting historical titles and avoid incorrect enforcement.
                pass

    # Bounded prompt: sort alphabetically by source stem
    sorted_items = sorted(resolved_mappings.items(), key=lambda x: x[0])
    bounded: dict[str, str] = {}
    total_chars = 0

    for src, tgt in sorted_items:
        if len(bounded) >= max_entries:
            break
        entry_len = len(src) + len(tgt) + 8
        if total_chars + entry_len > max_chars:
            break
        bounded[src] = tgt
        total_chars += entry_len

    return bounded


def estimate_segment_tokens(text: str) -> int:
    """Conservative token estimator for LLM context budgeting.
    
    Overestimates slightly to ensure safety against provider context and rate limits.
    Hangul syllables: ~2.0 tokens each.
    CJK ideographs: ~2.0 tokens each.
    ASCII / Latin characters: ~0.35 tokens each (~1 token per 3 chars).
    Other unicode characters / diacritics: ~1.0 token each.
    Base per-segment JSON structure framing overhead: 12 tokens.
    """
    if not text:
        return 12
    tokens = 12.0
    for ch in text:
        code = ord(ch)
        if (0xAC00 <= code <= 0xD7A3) or (0x1100 <= code <= 0x11FF) or (0x3130 <= code <= 0x318F):
            tokens += 2.0
        elif (0x4E00 <= code <= 0x9FFF) or (0x3400 <= code <= 0x4DBF):
            tokens += 2.0
        elif code < 128:
            tokens += 0.35
        else:
            tokens += 1.0
    return int(tokens) + 1


def batch_segments(
    segments: list[Any],
    max_segments: int = 40,
    max_chars: int = 6000,
    max_tokens: int = 2500,
) -> list[list[Any]]:
    """Batch segments obeying max segment count (default 40), character limits (~6000), and conservative token limit (~2500).

    Oversized single segments exceeding max_chars or max_tokens are handled as single-item batches
    without splitting mid-text.
    Maintains strict chapter and segment sequence intact.
    Guards against empty batch and infinite loops.
    """
    if not segments:
        return []

    batches: list[list[Any]] = []
    current_batch: list[Any] = []
    current_chars = 0
    current_tokens = 0

    for seg in segments:
        _, text = normalize_segment(seg)
        seg_len = len(text)
        seg_tokens = estimate_segment_tokens(text)

        # Explicit oversized single handling:
        # If this single segment alone exceeds max_chars or max_tokens, emit it alone
        if seg_len > max_chars or seg_tokens > max_tokens:
            if current_batch:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
                current_tokens = 0
            batches.append([seg])
            continue

        # If adding this segment exceeds max_segments, max_chars, or max_tokens, flush batch
        if current_batch and (
            len(current_batch) >= max_segments
            or current_chars + seg_len > max_chars
            or current_tokens + seg_tokens > max_tokens
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
            current_tokens = 0

        current_batch.append(seg)
        current_chars += seg_len
        current_tokens += seg_tokens

    if current_batch:
        batches.append(current_batch)

    return batches


def resolve_glossary(
    glossary: list[GlossaryEntry | dict[str, Any]],
    batch_text: str | None = None,
) -> tuple[list[GlossaryEntry], list[GlossaryEntry]]:
    """Resolve glossary entries ensuring locked items have strict priority.

    Returns (locked_entries, unlocked_entries).
    If batch_text is supplied, only entries whose source appears in batch_text are returned.
    """
    entries: list[GlossaryEntry] = []
    for item in glossary:
        if isinstance(item, GlossaryEntry):
            entries.append(item)
        elif isinstance(item, dict):
            entries.append(
                GlossaryEntry(
                    source=str(item.get("source", "")),
                    target=str(item.get("target", "")),
                    locked=bool(item.get("locked", False)),
                    notes=str(item.get("notes", "")),
                )
            )

    # De-duplicate: locked items MUST have priority over unlocked items
    by_source: dict[str, GlossaryEntry] = {}
    for entry in entries:
        src = entry.source.strip()
        if not src:
            continue
        if src in by_source:
            existing = by_source[src]
            if not existing.locked and entry.locked:
                by_source[src] = entry
            elif existing.locked and not entry.locked:
                continue
            else:
                # Same lock status, replace with latest
                by_source[src] = entry
        else:
            by_source[src] = entry

    # Filter by batch_text if provided
    selected: list[GlossaryEntry] = []
    for entry in by_source.values():
        if batch_text is None or entry.source in batch_text:
            selected.append(entry)

    locked = [e for e in selected if e.locked]
    unlocked = [e for e in selected if not e.locked]

    # Deterministic sort: longer source first, then alphabetical
    locked.sort(key=lambda x: (-len(x.source), x.source))
    unlocked.sort(key=lambda x: (-len(x.source), x.source))

    return locked, unlocked


# -----------------------------------------------------------------------------
# Relevance-Based Character Profile Matching
# -----------------------------------------------------------------------------

def is_hangul_str(text: str) -> bool:
    """Check if string contains any Hangul syllables or Jamo."""
    return any(
        (0xAC00 <= ord(c) <= 0xD7A3) or (0x1100 <= ord(c) <= 0x11FF) or (0x3130 <= ord(c) <= 0x318F)
        for c in text
    )


def match_character_term(text: str, term: str) -> bool:
    """Conservatively check if character canonical name or alias appears in text with boundary awareness."""
    if not text or not term:
        return False
    term_clean = term.strip()
    if not term_clean:
        return False

    if is_hangul_str(term_clean):
        pos = 0
        korean_particles = {
            "은", "는", "이", "가", "을", "를", "의", "에", "에게", "한테", "께", "도",
            "와", "과", "랑", "이랑", "로", "으로", "에서", "부터", "까지", "님", "씨",
            "군", "양", "선배", "후배", "형", "오빠", "누나", "언니", "동생",
            "아", "야", "여", "이여",
        }
        while True:
            idx = text.find(term_clean, pos)
            if idx == -1:
                return False
            pos = idx + 1
            # Left boundary check: must not be preceded by Hangul
            if idx > 0:
                prev_ch = text[idx - 1]
                if 0xAC00 <= ord(prev_ch) <= 0xD7A3 or 0x1100 <= ord(prev_ch) <= 0x11FF or 0x3130 <= ord(prev_ch) <= 0x318F:
                    continue
            end_idx = idx + len(term_clean)
            if end_idx == len(text):
                return True
            next_ch = text[end_idx]
            if not (0xAC00 <= ord(next_ch) <= 0xD7A3 or 0x1100 <= ord(next_ch) <= 0x11FF or 0x3130 <= ord(next_ch) <= 0x318F):
                return True
            # Check trailing particle suffix
            hend = end_idx
            while hend < len(text) and (0xAC00 <= ord(text[hend]) <= 0xD7A3 or 0x1100 <= ord(text[hend]) <= 0x11FF):
                hend += 1
            suffix = text[end_idx:hend]
            if suffix in korean_particles:
                return True
            continue
    else:
        # Non-Hangul word boundary check with punctuation/bracket tolerance
        prefix = r"(?<!\w)" if re.match(r"^\w", term_clean) else r""
        suffix = r"(?!\w)" if re.search(r"\w$", term_clean) else r""
        pattern = prefix + re.escape(term_clean) + suffix
        return bool(re.search(pattern, text, re.IGNORECASE))


def resolve_relevant_characters(
    characters: list[dict[str, Any]],
    batch_text: str,
    is_review: bool = False,
    max_entries: int = 20,
    max_chars: int = 2000,
) -> list[dict[str, Any]]:
    """Conservatively match character profiles against the CURRENT batch source and translation.
    
    Locked characters have strictly higher priority.
    Includes gender, speech_style, relationships, and aliases only when relevant.
    Characters not appearing in current batch are excluded to prevent context dumps.
    """
    if not characters or not batch_text:
        return []

    matched: list[tuple[int, int, dict[str, Any]]] = []

    for ch in characters:
        src = str(ch.get("canonical_source") or "").strip()
        tgt = str(ch.get("canonical_target") or "").strip()
        if not src or not tgt:
            continue

        raw_aliases = ch.get("source_aliases") or []
        aliases = raw_aliases if isinstance(raw_aliases, list) else []
        if isinstance(raw_aliases, str):
            try:
                aliases = json.loads(raw_aliases)
            except Exception:
                aliases = []

        is_match = False
        if match_character_term(batch_text, src):
            is_match = True
        if not is_match:
            for al in aliases:
                al_str = str(al).strip()
                if al_str and match_character_term(batch_text, al_str):
                    is_match = True
                    break
        if not is_match and is_review:
            if match_character_term(batch_text, tgt):
                is_match = True

        if is_match:
            locked_val = 1 if ch.get("locked") else 0
            matched.append((-locked_val, -len(src), ch))

    matched.sort(key=lambda x: (x[0], x[1]))

    bounded: list[dict[str, Any]] = []
    total_len = 0

    for _, _, ch in matched:
        if len(bounded) >= max_entries:
            break

        src = str(ch.get("canonical_source") or "").strip()
        tgt = str(ch.get("canonical_target") or "").strip()
        locked = bool(ch.get("locked"))
        gender = str(ch.get("gender") or "").strip()
        speech = str(ch.get("speech_style") or "").strip()
        rel = str(ch.get("relationships") or "").strip()
        raw_aliases = ch.get("source_aliases") or []
        aliases = raw_aliases if isinstance(raw_aliases, list) else []
        if isinstance(raw_aliases, str):
            try:
                aliases = json.loads(raw_aliases)
            except Exception:
                aliases = []

        desc_parts = []
        if gender and gender not in ("unknown", ""):
            desc_parts.append(f"Giới tính: {gender}")
        if speech:
            desc_parts.append(f"Xưng hô/Giọng điệu: {speech}")
        if rel:
            desc_parts.append(f"Quan hệ: {rel}")
        if aliases:
            desc_parts.append(f"Tên khác: {', '.join(aliases)}")

        details_str = f" [{'; '.join(desc_parts)}]" if desc_parts else ""
        entry_repr = f"- {src} -> {tgt}" + (" (ĐÃ KHÓA / LOCKED)" if locked else "") + details_str
        if total_len + len(entry_repr) > max_chars:
            break

        bounded.append({
            "id": ch.get("id"),
            "canonical_source": src,
            "canonical_target": tgt,
            "locked": locked,
            "gender": gender,
            "speech_style": speech,
            "relationships": rel,
            "source_aliases": aliases,
            "formatted_line": entry_repr,
        })
        total_len += len(entry_repr)

    return bounded


TRANSLATION_SYSTEM_RULES = """Bạn là chuyên gia dịch thuật văn học chuyên nghiệp từ tiếng Hàn sang tiếng Việt (sản phẩm Trạm Dịch).
Nhiệm vụ: Dịch chính xác từng phân đoạn văn bản được cung cấp theo đúng các nguyên tắc sau:
1. ĐẦY ĐỦ VÀ TRUNG THỰC: Dịch trọn vẹn 100% nội dung. Tuyệt đối KHÔNG tự ý thêm thắt tình tiết, KHÔNG lược bỏ bất kỳ câu chữ nào.
2. VĂN PHONG TIẾNG VIỆT TỰ NHIÊN: Lời văn mượt mà, đúng chuẩn ngữ pháp và văn phong tiểu thuyết tiếng Việt hiện đại.
3. NHẤT QUÁN TÊN RIÊNG VÀ THUẬT NGỮ: Giữ tính nhất quán tự nhiên của tên nhân vật, địa danh, chiêu thức, tổ chức dựa theo diễn biến của chương truyện và bối cảnh (Story Bible).
4. XƯNG HÔ VÀ GIỌNG ĐIỆU: Giữ đúng ngôi kể, giọng điệu nhân vật (lạnh lùng, kính cẩn, thân mật, hài hước...), quan hệ xã hội theo ngữ cảnh câu chuyện.
5. THUẬT NGỮ HỆ THỐNG / KÝ HIỆU: Giữ nguyên các ký hiệu ngoặc vuông [...], ngoặc nhọn <...>, thông số hệ thống, thanh trạng thái hoặc các nhãn game/apocalypse.
6. CẤU TRÚC PHÂN ĐOẠN: Mỗi phân đoạn có một ID duy nhất. Chỉ sao chép chính xác giá trị thuộc tính 'id' từ danh sách mục tiêu JSON (copy only id property value), kết quả trả về phải chứa đầy đủ và chính xác các ID này, không được thiếu, thừa hay trùng lặp."""

REVIEW_SYSTEM_RULES = (
    "Bạn là biên tập viên văn học tiếng Việt cao cấp (Reviewer Trạm Dịch).\n"
    "Nhiệm vụ: Thẩm định bản dịch tiếng Hàn sang tiếng Việt, phát hiện câu văn chưa tự nhiên, "
    "dịch thiếu, dịch thừa, sai đại từ nhân xưng hoặc không nhất quán về xưng hô, tên nhân vật. "
    "Đảm bảo tên riêng và thuật ngữ nhất quán tự nhiên theo ngữ cảnh chương truyện, Story Bible, "
    "bảng ánh xạ tiêu đề chuẩn và từ điển thuật ngữ. "
    "Đưa ra danh sách issues và corrections chuẩn xác."
)


def build_translation_context(
    segments: list[Any],
    glossary: list[GlossaryEntry | dict[str, Any]] | None = None,
    story_bible: str | None = None,
    recent_summaries: list[str] | None = None,
    neighboring_before: list[Any] | None = None,
    neighboring_after: list[Any] | None = None,
    prior_titles: dict[str, str] | None = None,
    is_review: bool = False,
    characters: list[dict[str, Any]] | None = None,
    preset: dict[str, Any] | None = None,
    few_shot_examples: list[dict[str, Any]] | None = None,
    max_bible_chars: int = 2500,
    max_summaries_chars: int = 2000,
    max_neighbors_count: int = 3,
    max_prior_titles_chars: int = 1500,
    max_characters_chars: int = 2000,
    max_few_shot_chars: int = 2000,
) -> dict[str, Any]:
    """Build deterministic translation context with bounded story bible, summaries, prior titles, glossary, characters, few-shots, and neighboring segments."""
    # Bounded story bible
    bounded_bible = ""
    if story_bible:
        bounded_bible = story_bible[:max_bible_chars].strip()

    # Bounded summaries
    bounded_summaries: list[str] = []
    if recent_summaries:
        total_len = 0
        for s in reversed(recent_summaries):
            if total_len + len(s) > max_summaries_chars:
                break
            bounded_summaries.insert(0, s.strip())
            total_len += len(s)

    # Bounded prior chapter title root mappings
    bounded_prior_titles: dict[str, str] = {}
    if prior_titles:
        total_t_len = 0
        for src, tgt in sorted(prior_titles.items()):
            src_str = str(src).strip()
            tgt_str = str(tgt).strip()
            if not src_str or not tgt_str:
                continue
            entry_len = len(src_str) + len(tgt_str) + 12
            if total_t_len + entry_len > max_prior_titles_chars:
                break
            bounded_prior_titles[src_str] = tgt_str
            total_t_len += entry_len

    # Assemble batch text for relevance matching
    batch_text = "\n".join(normalize_segment(s)[1] for s in segments)
    if is_review:
        trans_parts = []
        for s in segments:
            if isinstance(s, dict):
                t = str(s.get("translation") or s.get("translated") or "")
            else:
                t = str(getattr(s, "translation", getattr(s, "translated", "")))
            if t:
                trans_parts.append(t)
        if trans_parts:
            batch_text = f"{batch_text}\n" + "\n".join(trans_parts)

    # Bounded glossary (scoped by batch text)
    bounded_glossary: list[dict[str, Any]] = []
    if glossary:
        locked_entries, unlocked_entries = resolve_glossary(glossary, batch_text)
        all_resolved = locked_entries + unlocked_entries
        total_g_len = 0
        for g in all_resolved:
            d = g.to_dict()
            g_line = f"- {d['source']} -> {d['target']}" + (" (ĐÃ KHÓA / LOCKED)" if d.get("locked") else "")
            if total_g_len + len(g_line) > 2500:
                break
            bounded_glossary.append(d)
            total_g_len += len(g_line)

    # Bounded relevance-matched characters
    bounded_characters: list[dict[str, Any]] = []
    if characters:
        bounded_characters = resolve_relevant_characters(
            characters,
            batch_text=batch_text,
            is_review=is_review,
            max_chars=max_characters_chars,
        )

    # Bounded few-shot examples
    effective_few_shots = few_shot_examples
    if effective_few_shots is None and preset:
        effective_few_shots = preset.get("few_shot_examples")

    bounded_few_shots: list[dict[str, Any]] = []
    if effective_few_shots:
        total_fs = 0
        for ex in effective_few_shots[:5]:
            src = str(ex.get("source") or "").strip()
            tgt = str(ex.get("target") or "").strip()
            if not src or not tgt:
                continue
            notes = str(ex.get("notes") or "").strip()
            line = f"- Gốc: \"{src}\" => Dịch: \"{tgt}\"" + (f" (Ghi chú: {notes})" if notes else "")
            if total_fs + len(line) > max_few_shot_chars:
                break
            bounded_few_shots.append({
                "source": src,
                "target": tgt,
                "notes": notes,
                "formatted_line": line,
            })
            total_fs += len(line)

    # Bounded neighboring segments (source only for context)
    nb_before: list[dict[str, str]] = []
    if neighboring_before:
        for s in neighboring_before[-max_neighbors_count:]:
            sid, src = normalize_segment(s)
            nb_before.append({"id": sid, "source": src[:500]})

    nb_after: list[dict[str, str]] = []
    if neighboring_after:
        for s in neighboring_after[:max_neighbors_count]:
            sid, src = normalize_segment(s)
            nb_after.append({"id": sid, "source": src[:500]})

    # Section strings for placeholders
    sec_bible = f"### STORY BIBLE (Bối cảnh truyện):\n{bounded_bible}\n" if bounded_bible else ""
    sec_summaries = ("### TÓM TẮT CÁC CHƯƠNG GẦN NHẤT:\n" + "\n".join(f"- {s}" for s in bounded_summaries) + "\n") if bounded_summaries else ""

    if bounded_prior_titles:
        if is_review:
            title_lines = [f"- Gốc: '{src}' => Dịch chuẩn: '{tgt}'" for src, tgt in sorted(bounded_prior_titles.items())]
            sec_titles = (
                "### TIÊU ĐỀ ĐÃ DỊCH TỪ CÁC CHƯƠNG TRƯỚC (AUTHORITATIVE PRIOR TITLE MAPPINGS):\n"
                "Bảng ánh xạ chuẩn xác và có thẩm quyền bắt buộc (authoritative) giữa phần gốc tiêu đề tiếng Hàn (Korean title root) -> phần gốc tiêu đề tiếng Việt (Vietnamese title root):\n"
                + "\n".join(title_lines)
                + "\n\nQUY TẮC THẨM ĐỊNH TIÊU ĐỀ (CHAPTER TITLE REVIEW RULES):\n"
                "- TÍNH THẨM QUYỀN BẮT BUỘC: Bảng ánh xạ gốc tiêu đề ở trên là chuẩn hóa bắt buộc (explicitly authoritative). "
                "Thẩm định viên PHẢI đối chiếu và phát hiện các trường hợp không nhất quán (inconsistencies) của phần gốc tiêu đề chương so với các chương trước.\n"
                "- TIÊU ĐỀ NHIỀU PHẦN CÓ ĐÁNH SỐ THỨ TỰ (NUMBERED MULTIPART TITLES): Đối với các tiêu đề chương dạng nhiều phần có đánh số thứ tự như (1), (2), [1], [2], Hồi 1, Phần 1...: "
                "BẮT BUỘC phát hiện và sửa các điểm không nhất quán của phần gốc tiêu đề (root title stem) để khớp đúng với tiêu đề chuẩn đã dịch ở trên, "
                "đồng thời PHẢI BẢO TOÀN NGUYÊN VẸN phần hậu tố đánh số thứ tự (installment suffix) phía sau, tuyệt đối không được làm mất, thay đổi hoặc dịch sai phần đánh số này.\n"
            )
        else:
            sec_titles = (
                "### TIÊU ĐỀ ĐÃ DỊCH TỪ CÁC CHƯƠNG TRƯỚC (PRIOR CHAPTER TITLES):\n"
                + "\n".join(f"- '{src}' => '{tgt}'" for src, tgt in sorted(bounded_prior_titles.items()))
                + "\n(LƯU Ý: Với các tiêu đề chương có đánh số thứ tự như (1), (2), [1]...: BẮT BUỘC giữ đúng từ dịch gốc của tiêu đề chương như đã dịch ở trên, đồng thời bảo toàn nguyên vẹn phần đánh số thứ tự phía sau không thay đổi.)\n"
            )
    else:
        sec_titles = ""

    sec_chars = ("### HỒ SƠ NHÂN VẬT LIÊN QUAN (RELEVANT CHARACTER PROFILES):\n" + "\n".join(c["formatted_line"] for c in bounded_characters) + "\n(LƯU Ý: BẮT BUỘC tuân thủ đúng tên và xưng hô của các nhân vật ĐÃ KHÓA / LOCKED.)\n") if bounded_characters else ""

    if bounded_glossary:
        if is_review:
            glossary_lines = []
            for d in bounded_glossary:
                status = " (ĐÃ KHÓA / LOCKED - BẮT BUỘC TUÂN THỦ CAO NHẤT)" if d.get("locked") else ""
                glossary_lines.append(f"- {d['source']} -> {d['target']}{status}")
            sec_glossary = (
                "### TỪ ĐIỂN THUẬT NGỮ ÁP DỤNG (APPLICABLE GLOSSARY):\n"
                + "\n".join(glossary_lines)
                + "\n\nQUY TẮC THẨM ĐỊNH THUẬT NGỮ (GLOSSARY REVIEW RULES):\n"
                "- ƯU TIÊN CAO NHẤT CHO THUẬT NGỮ ĐÃ KHÓA (MANUALLY LOCKED GLOSSARY): Các thuật ngữ được đánh dấu '(ĐÃ KHÓA / LOCKED - BẮT BUỘC TUÂN THỦ CAO NHẤT)' "
                "là do người dùng thiết lập thủ công, có độ ưu tiên cao nhất tuyệt đối (strictly higher priority) so với thuật ngữ chưa khóa hoặc ngữ cảnh tự do. "
                "Thẩm định viên BẮT BUỘC phải đối chiếu và yêu cầu bản dịch tuân thủ đúng 100% thuật ngữ đã khóa. Nếu phát hiện phân đoạn dịch dùng từ khác hoặc sai lệch so với thuật ngữ đã khóa, "
                "BẮT BUỘC phải báo lỗi trong 'issues' và sửa lại trong 'corrections' về đúng thuật ngữ đã khóa.\n"
                "- Thuật ngữ chưa khóa: Đảm bảo sử dụng nhất quán và chính xác theo ngữ cảnh câu chuyện.\n"
            )
        else:
            sec_glossary = ("### TỪ ĐIỂN THUẬT NGỮ (GLOSSARY):\n" + "\n".join(f"- {d['source']} -> {d['target']}" + (" (ĐÃ KHÓA / LOCKED)" if d.get("locked") else "") for d in bounded_glossary) + "\n")
    else:
        sec_glossary = ""

    sec_few_shots = ("### VÍ DỤ MẪU (FEW-SHOT EXAMPLES):\n" + "\n".join(f["formatted_line"] for f in bounded_few_shots) + "\n") if bounded_few_shots else ""

    nb_label = "(CHỈ ĐỂ THAM KHẢO NGỮ CẢNH, KHÔNG SỬA - READ-ONLY CONTEXT)" if is_review else "(CHỈ ĐỂ THAM KHẢO NGỮ CẢNH, KHÔNG DỊCH - READ-ONLY CONTEXT)"
    sec_nb_before = (f"### ĐOẠN VĂN BẢN LIỀN TRƯỚC {nb_label}:\n" + "\n".join(f"- {item['source']}" for item in nb_before) + "\n") if nb_before else ""
    sec_nb_after = (f"### ĐOẠN VĂN BẢN LIỀN SAU {nb_label}:\n" + "\n".join(f"- {item['source']}" for item in nb_after) + "\n") if nb_after else ""

    batch_targets = [
        {"id": normalize_segment(s)[0], "source": normalize_segment(s)[1]}
        for s in segments
    ]
    batch_targets_str = "### CÁC PHÂN ĐOẠN CẦN DỊCH (TRANSLATE BATCH):\n" + json.dumps(batch_targets, ensure_ascii=False, indent=2) + "\n"

    review_lines = [
        "### CÁC PHÂN ĐOẠN CẦN THẨM ĐỊNH (REVIEW BATCH):",
        "Hãy thẩm định các phân đoạn dịch sau:",
    ]
    for s in segments:
        sid, src = normalize_segment(s)
        trans = s.get("translation", "") if isinstance(s, dict) else getattr(s, "translation", "")
        review_lines.append(f"[{sid}]\n- Gốc: {src}\n- Dịch: {trans}")
    batch_review_targets_str = "\n".join(review_lines) + "\n"

    # Select system prompt and user template
    sys_instruction = ""
    template = ""
    if preset:
        if is_review:
            sys_instruction = (preset.get("review_system_prompt") or "").strip()
            template = (preset.get("review_user_prompt_template") or "").strip()
        else:
            sys_instruction = (preset.get("system_prompt") or "").strip()
            template = (preset.get("user_prompt_template") or "").strip()

    if not sys_instruction:
        sys_instruction = REVIEW_SYSTEM_RULES if is_review else TRANSLATION_SYSTEM_RULES

    # Build user prompt
    if template:
        placeholders = {
            "{story_bible}": sec_bible,
            "{recent_summaries}": sec_summaries,
            "{prior_titles}": sec_titles,
            "{characters}": sec_chars,
            "{glossary}": sec_glossary,
            "{few_shots}": sec_few_shots,
            "{neighboring_before}": sec_nb_before,
            "{batch_targets}": batch_review_targets_str if is_review else batch_targets_str,
            "{batch_review_targets}": batch_review_targets_str,
            "{neighboring_after}": sec_nb_after,
            "{custom_instructions}": "",
        }
        rendered = template
        for ph, val in placeholders.items():
            rendered = rendered.replace(ph, val)
        target_token = "{batch_review_targets}" if is_review else "{batch_targets}"
        if target_token not in template and batch_targets_str not in rendered and batch_review_targets_str not in rendered:
            rendered += "\n\n" + (batch_review_targets_str if is_review else batch_targets_str)
        user_prompt = rendered.strip()
    else:
        prompt_lines: list[str] = []
        if sec_bible: prompt_lines.append(sec_bible.strip())
        if sec_summaries: prompt_lines.append(sec_summaries.strip())
        if sec_titles: prompt_lines.append(sec_titles.strip())
        if sec_chars: prompt_lines.append(sec_chars.strip())
        if sec_glossary: prompt_lines.append(sec_glossary.strip())
        if sec_few_shots: prompt_lines.append(sec_few_shots.strip())
        if sec_nb_before: prompt_lines.append(sec_nb_before.strip())
        if is_review:
            prompt_lines.append(batch_review_targets_str.strip())
            if sec_nb_after: prompt_lines.append(sec_nb_after.strip())
            prompt_lines.append(
                "Hãy thẩm định toàn bộ danh sách 'CÁC PHÂN ĐOẠN CẦN THẨM ĐỊNH' ở trên, "
                "phát hiện các lỗi về tính nhất quán, thuật ngữ đã khóa, tiêu đề, xưng hô, ngữ nghĩa, "
                "và trả về định dạng JSON theo schema gồm 'issues' và 'corrections'."
            )
        else:
            prompt_lines.append(batch_targets_str.strip())
            if sec_nb_after: prompt_lines.append(sec_nb_after.strip())
            prompt_lines.append("Hãy dịch toàn bộ danh sách 'CÁC PHÂN ĐOẠN CẦN DỊCH' ở trên sang tiếng Việt và trả về định dạng JSON theo schema đã quy định.")
        user_prompt = "\n\n".join(prompt_lines).strip()

    res = {
        "prompt_version": "v2_json_targets",
        "is_review": is_review,
        "system_instruction": sys_instruction,
        "user_prompt": user_prompt,
        "story_bible": bounded_bible,
        "recent_summaries": bounded_summaries,
        "prior_title_mappings": bounded_prior_titles,
        "glossary": bounded_glossary,
        "neighboring_before": nb_before,
        "neighboring_after": nb_after,
        "batch_segments": [
            {
                "id": normalize_segment(s)[0],
                "source": normalize_segment(s)[1],
                **({"translation": s.get("translation", "") if isinstance(s, dict) else getattr(s, "translation", "")} if is_review else {})
            }
            for s in segments
        ],
    }
    if preset:
        res["preset_id"] = preset.get("id")
        if preset.get("updated_at"):
            res["preset_updated_at"] = preset.get("updated_at")
    if bounded_characters:
        res["characters"] = bounded_characters
    if bounded_few_shots:
        res["few_shots"] = bounded_few_shots
    return res


def check_translation_heuristics(source: str, translation: str) -> list[str]:
    """Check for excessive expansion or severe truncation heuristics."""
    issues: list[str] = []
    src = source.strip()
    trans = translation.strip()

    if not trans and src:
        issues.append("Bản dịch trống mặc dù đoạn gốc có nội dung.")
        return issues

    if not src or is_symbol_only_source(src):
        return issues

    src_len = len(src)
    trans_len = len(trans)

    # Heuristic for truncation: if source has >= 15 characters but translation is < 15% length
    if src_len >= 15 and trans_len < max(3, int(src_len * 0.15)):
        issues.append(
            f"Cảnh báo cắt xén nội dung (truncation): Gốc {src_len} ký tự, Dịch {trans_len} ký tự."
        )

    # Heuristic for expansion: if translation is more than 5x longer than source (and > 40 chars)
    if src_len >= 5 and trans_len > 40 and trans_len > src_len * 5:
        issues.append(
            f"Cảnh báo phóng đại/thêm bớt nội dung (excessive expansion): Gốc {src_len} ký tự, Dịch {trans_len} ký tự."
        )

    return issues


class TranslationMemory:
    """Deterministic translation memory caching exact source segments within the same book context."""

    def __init__(self, book_id: str, glossary_version: str = "v1", preset_version: str = "v1"):
        self.book_id = book_id
        self.glossary_version = glossary_version
        self.preset_version = preset_version
        self._cache: dict[str, str] = {}

    def get(
        self,
        source: str,
        glossary_version: str | None = None,
        preset_version: str | None = None,
    ) -> str | None:
        """Lookup translation for exact source text if glossary and preset versions match."""
        target_version = glossary_version or self.glossary_version
        if target_version != self.glossary_version:
            return None
        target_p_version = preset_version or self.preset_version
        if target_p_version != self.preset_version:
            return None
        return self._cache.get(source)

    def set(
        self,
        source: str,
        translation: str,
        glossary_version: str | None = None,
        preset_version: str | None = None,
    ) -> None:
        """Cache translation for exact source text if glossary and preset versions match."""
        target_version = glossary_version or self.glossary_version
        target_p_version = preset_version or self.preset_version
        if (
            target_version == self.glossary_version
            and target_p_version == self.preset_version
            and source.strip()
        ):
            self._cache[source] = translation

    def clear(self) -> None:
        """Clear memory cache."""
        self._cache.clear()
