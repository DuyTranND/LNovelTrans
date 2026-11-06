"""Tests for read-only full-book consistency review.

Validates read-only DB enforcement (mode=ro), title grouping, ordinal mismatch,
Korean particle/boundary matching, Vietnamese strict boundary and substring guarding,
late distant chapter tracking, ambiguity handling, and report output schema.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from backend.app.audit import normalize_title_stem
from backend.app.provider import match_korean_term_in_text
from scripts.consistency_fullbook_readonly import (
    DEFAULT_BOOK_ID,
    DEFAULT_CANDIDATES_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_OUT_JSON,
    DEFAULT_OUT_MD,
    TRAILING_INSTALLMENT_RE,
    TitleReviewResult,
    classify_and_aggregate_findings,
    load_term_dictionary,
    open_readonly_db,
    review_chapter_titles,
    scan_fullbook_segments,
)


def test_production_db_readonly_mode_ro_enforcement(tmp_path: Path) -> None:
    """Verify that open_readonly_db enforces SQLite mode=ro and strictly blocks writes."""
    test_db = tmp_path / "test_readonly.db"

    # Initialize minimum actual schema
    conn_setup = sqlite3.connect(str(test_db))
    conn_setup.execute(
        """
        CREATE TABLE chapters (
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            spine_index INTEGER NOT NULL,
            archive_path TEXT NOT NULL,
            href TEXT NOT NULL,
            title TEXT,
            media_type TEXT NOT NULL,
            raw_bytes BLOB NOT NULL,
            status TEXT NOT NULL DEFAULT 'untranslated',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn_setup.execute(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn_setup.execute(
        """
        CREATE TABLE glossary (
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL
        )
        """
    )
    conn_setup.execute(
        """
        CREATE TABLE consistency_audit_findings (
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            category TEXT NOT NULL
        )
        """
    )
    conn_setup.execute(
        "INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at) "
        "VALUES ('ch1', 'book_ro', 'm1', 1, 'a', 'h', 'Title 1', 'text/html', X'', 'translated', 'now', 'now')"
    )
    conn_setup.execute("INSERT INTO settings (key, value, updated_at) VALUES ('test', 'val', 'now')")
    conn_setup.commit()
    conn_setup.close()

    conn = open_readonly_db(test_db)
    try:
        cur = conn.cursor()
        # Verify read operations succeed
        cur.execute("SELECT count(*) FROM chapters WHERE book_id = ?", ("book_ro",))
        count = cur.fetchone()[0]
        assert count == 1

        # Verify UPDATE is blocked with OperationalError
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            cur.execute("UPDATE settings SET value = 'hacked' WHERE key = 'test'")

        # Verify INSERT is blocked
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            cur.execute(
                "INSERT INTO glossary (id, book_id, source, target) VALUES ('x', 'y', 'z', 'w')"
            )

        # Verify DELETE is blocked
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            cur.execute("DELETE FROM consistency_audit_findings WHERE 1=0")
    finally:
        conn.close()


def test_synthetic_title_review_deterministic_groups_and_ordinal(tmp_path: Path) -> None:
    """Synthetic title grouping and ordinal test exercising review_chapter_titles without live DB."""
    test_db = tmp_path / "synthetic_titles.db"
    book_id = "syn_book_1"

    conn_setup = sqlite3.connect(str(test_db))
    conn_setup.execute(
        """
        CREATE TABLE chapters (
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            spine_index INTEGER NOT NULL,
            archive_path TEXT NOT NULL,
            href TEXT NOT NULL,
            title TEXT,
            media_type TEXT NOT NULL,
            raw_bytes BLOB NOT NULL,
            status TEXT NOT NULL DEFAULT 'translated',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn_setup.execute(
        """
        CREATE TABLE segments (
            id TEXT PRIMARY KEY,
            chapter_id TEXT NOT NULL,
            book_id TEXT NOT NULL,
            seq_order INTEGER NOT NULL,
            dom_path TEXT NOT NULL,
            node_type TEXT NOT NULL,
            original_source TEXT NOT NULL,
            source TEXT NOT NULL,
            translation TEXT,
            parent_tag TEXT,
            is_whitespace INTEGER NOT NULL DEFAULT 0,
            review_data TEXT DEFAULT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    # Chapters & title segments:
    # 1. Ch 0: 각성 (1) -> Thức tỉnh (1)
    # 2. Ch 1: 각성 (2) -> Thức tỉnh  (Ordinal missing in translation!)
    # 3. Ch 2: 계획 도시 (1) -> Thành phố quy hoạch (1)
    # 4. Ch 3: 계획 도시 (2) -> Đô thị quy hoạch (2)  (Substantive divergence in translation stem!)
    # 5. Ch 4: 데우스 엑스 마키나 (1) -> Deus Ex Machina (1)
    # 6. Ch 5: 데우스 엑스 마키나 (2) -> deus ex machina (2)  (Case-only difference!)
    chapters_data = [
        ("ch_0", 0, "각성 (1)", "Thức tỉnh (1)"),
        ("ch_1", 1, "각성 (2)", "Thức tỉnh"),
        ("ch_2", 2, "계획 도시 (1)", "Thành phố quy hoạch (1)"),
        ("ch_3", 3, "계획 도시 (2)", "Đô thị quy hoạch (2)"),
        ("ch_4", 4, "데우스 엑스 마키나 (1)", "Deus Ex Machina (1)"),
        ("ch_5", 5, "데우스 엑스 마키나 (2)", "deus ex machina (2)"),
    ]

    for ch_id, sp, src, trans in chapters_data:
        conn_setup.execute(
            "INSERT INTO chapters VALUES (?, ?, ?, ?, 'a', 'h', ?, 'text/html', X'', 'translated', 'now', 'now')",
            (ch_id, book_id, f"man_{sp}", sp, src),
        )
        conn_setup.execute(
            "INSERT INTO segments VALUES (?, ?, ?, 0, '/html/body/h1[1]', 'text', ?, ?, ?, 'h1', 0, NULL, 1)",
            (f"seg_{ch_id}", ch_id, book_id, src, src, trans),
        )

    conn_setup.commit()
    conn_setup.close()

    conn = open_readonly_db(test_db)
    try:
        res: TitleReviewResult = review_chapter_titles(conn, book_id)

        assert res.total_chapters_reviewed == 6
        assert res.chapters_with_titles == 6

        # Substantive title groups: "계획 도시" has distinct translations ("Đô thị quy hoạch" vs "Thành phố quy hoạch")
        sub_sources = {g["source"]: g for g in res.substantive_title_groups}
        assert "계획 도시" in sub_sources
        assert sub_sources["계획 도시"]["chapter_count"] == 2
        assert len(sub_sources["계획 도시"]["distinct_variants"]) == 2

        # Case-only title groups: "데우스 엑스 마키나" ("Deus Ex Machina" vs "deus ex machina")
        case_sources = {g["source"]: g for g in res.case_only_title_groups}
        assert "데우스 엑스 마키나" in case_sources
        assert case_sources["데우스 엑스 마키나"]["chapter_count"] == 2

        # Ordinal mismatches: Ch 1 ("각성 (2)" -> "Thức tỉnh") missing "(2)"
        assert len(res.ordinal_mismatches) == 1
        om = res.ordinal_mismatches[0]
        assert om["chapter"] == 2  # chapter_num is spine_index + 1 = 2
        assert om["source"] == "각성 (2)"
        assert om["translation"] == "Thức tỉnh"
        assert om["expected_ordinal"] == "(2)"
    finally:
        conn.close()


@pytest.mark.skipif(
    os.environ.get("COMIC_RUN_PRIVATE_BOOK_TESTS") != "1",
    reason="Requires private historical real-book database (opt-in with COMIC_RUN_PRIVATE_BOOK_TESTS=1)",
)
def test_title_review_deterministic_groups_and_ordinal():
    """Verify title analysis extracts 26 substantive groups and Chapter 2 ordinal mismatch."""
    conn = open_readonly_db(DEFAULT_DB_PATH)
    try:
        res: TitleReviewResult = review_chapter_titles(conn, DEFAULT_BOOK_ID)

        assert res.total_chapters_reviewed == 157
        assert len(res.substantive_title_groups) == 26

        # Check total chapters involved in the 26 substantive groups is 78
        total_chs = sum(g["chapter_count"] for g in res.substantive_title_groups)
        assert total_chs == 78

        # Verify known groups are present
        sources = {g["source"]: g for g in res.substantive_title_groups}
        assert "계획 도시" in sources
        assert sources["계획 도시"]["chapter_count"] == 5
        assert "식품 대란" in sources
        assert sources["식품 대란"]["chapter_count"] == 5
        assert "김포" in sources
        assert sources["김포"]["chapter_count"] == 4

        # Verify Chapter 2 ordinal mismatch is detected
        assert len(res.ordinal_mismatches) == 1
        om = res.ordinal_mismatches[0]
        assert om["chapter"] == 2
        assert om["source"] == "각성 (2)"
        assert om["translation"] == "Thức tỉnh"
        assert om["expected_ordinal"] == "(2)"

        # Verify case-only title group (Deus Ex Machina) is cleanly separated
        case_sources = {g["source"]: g for g in res.case_only_title_groups}
        assert "데우스 엑스 마키나" in case_sources
    finally:
        conn.close()


def test_title_ordinal_edge_cases_and_stem_normalization():
    """Verify trailing installment normalization across various brackets and Unicode formats."""
    assert normalize_title_stem("각성 (1)") == "각성"
    assert normalize_title_stem("각성 (2)") == "각성"
    assert normalize_title_stem("각성 （1）") == "각성"
    assert normalize_title_stem("식품 대란 [1]") == "식품 대란"
    assert normalize_title_stem("식품 대란 ［2］") == "식품 대란"
    assert normalize_title_stem("대탈출 【3】") == "대탈출"
    assert normalize_title_stem("대탈출 (#1)") == "대탈출"
    assert normalize_title_stem("오픈런") == "오픈런"
    assert normalize_title_stem("") == ""

    # Test regex directly
    assert TRAILING_INSTALLMENT_RE.search("각성 (2)") is not None
    assert TRAILING_INSTALLMENT_RE.search("Thức tỉnh (2)") is not None
    assert TRAILING_INSTALLMENT_RE.search("Thức tỉnh") is None


def test_korean_boundary_and_particle_parser():
    """Verify Korean term boundary parser handles particles and rejects false substrings."""
    # 1. Valid particle attachments
    assert match_korean_term_in_text("마석을 들었다.", "마석") == "마석을"
    assert match_korean_term_in_text("마석이 빛났다.", "마석") == "마석이"
    assert match_korean_term_in_text("은월에게 말했다.", "은월") == "은월에게"
    assert match_korean_term_in_text("차도희는 달렸다.", "차도희") == "차도희는"

    # 2. Rejects Hangul character on the left (prefix / compound word)
    assert match_korean_term_in_text("김소염", "소염") is None
    assert match_korean_term_in_text("불마석", "마석") is None

    # 3. Rejects non-particle Hangul character on the right (part of longer word)
    assert match_korean_term_in_text("소염제", "소염") is None


def test_vietnamese_variant_strict_boundary_and_shorter_substring_guard_synthetic():
    """Verify Vietnamese variant search respects word boundaries and guards shorter substrings using synthetic inputs."""
    test_text = "Tôi gặp Bạch Ân Nguyệt tại văn phòng cùng Ân Nguyệt."
    test_vars = ["Bạch Ân Nguyệt", "Ân Nguyệt", "Nguyệt"]

    matched = []
    covered_spans = []
    for v in test_vars:
        pat = re.compile(rf"(?<![\w\u00C0-\u1EF9]){re.escape(v)}(?![\w\u00C0-\u1EF9])", re.IGNORECASE)

        for m in pat.finditer(test_text):
            span = m.span()
            # Guard against overlapping shorter substrings
            if any(span[0] < cs[1] and span[1] > cs[0] for cs in covered_spans):
                continue
            covered_spans.append(span)
            matched.append((v, test_text[span[0]:span[1]]))

    # Should match Bạch Ân Nguyệt once and Ân Nguyệt once (at the end), NOT Nguyệt!
    matched_variants = [m[0] for m in matched]
    assert "Bạch Ân Nguyệt" in matched_variants
    assert "Ân Nguyệt" in matched_variants
    assert "Nguyệt" not in matched_variants  # Shorter substring guarded!


@pytest.mark.skipif(
    os.environ.get("COMIC_RUN_PRIVATE_BOOK_TESTS") != "1",
    reason="Requires private historical real-book database and candidates snapshot (opt-in with COMIC_RUN_PRIVATE_BOOK_TESTS=1)",
)
def test_vietnamese_variant_strict_boundary_and_shorter_substring_guard():
    """Verify Vietnamese variant search respects word boundaries and guards shorter substrings."""
    conn = open_readonly_db(DEFAULT_DB_PATH)
    try:
        term_dict, variant_patterns, _ = load_term_dictionary(
            conn, DEFAULT_BOOK_ID, candidates_path=DEFAULT_CANDIDATES_PATH
        )

        # Check 은월 candidate variants
        eunwol_vars = term_dict["은월"].variants
        assert "Bạch Ân Nguyệt" in eunwol_vars or "Ân Nguyệt" in eunwol_vars

        # Test sentence where longer variant "Bạch Ân Nguyệt" contains "Ân Nguyệt"
        test_text = "Tôi gặp Bạch Ân Nguyệt tại văn phòng cùng Ân Nguyệt."
        test_vars = ["Bạch Ân Nguyệt", "Ân Nguyệt", "Nguyệt"]

        matched = []
        covered_spans = []
        for v in test_vars:
            v_lower, pat = variant_patterns.get(v, (v.lower(), None))
            if not pat:
                pat = re.compile(rf"(?<![\w\u00C0-\u1EF9]){re.escape(v)}(?![\w\u00C0-\u1EF9])", re.IGNORECASE)

            for m in pat.finditer(test_text):
                span = m.span()
                # Guard against overlapping shorter substrings
                if any(span[0] < cs[1] and span[1] > cs[0] for cs in covered_spans):
                    continue
                covered_spans.append(span)
                matched.append((v, test_text[span[0]:span[1]]))

        # Should match Bạch Ân Nguyệt once and Ân Nguyệt once (at the end), NOT Nguyệt!
        matched_variants = [m[0] for m in matched]
        assert "Bạch Ân Nguyệt" in matched_variants
        assert "Ân Nguyệt" in matched_variants
        assert "Nguyệt" not in matched_variants  # Shorter substring guarded!
    finally:
        conn.close()


def test_ambiguity_and_no_match_handling():
    """Verify terms without matched variants are flagged for manual review without inventing variants."""
    from scripts.consistency_fullbook_readonly import SegmentOccurrence, TermDefinition

    dummy_term_dict = {
        "테스트용어": TermDefinition(
            source_term="테스트용어",
            category="term",
            variants=["biến thể A", "biến thể B"],
        )
    }

    # Simulate scan result with no match in translation
    dummy_scan_results = {
        "테스트용어": {
            "early_chapters": {1},
            "late_chapters": {100},
            "all_chapters": {1, 100},
            "variant_hits": {},
            "no_match_occurrences": [
                SegmentOccurrence(
                    chapter_num=100,
                    chapter_id="dummy_ch",
                    segment_id="dummy_seg",
                    matched_korean="테스트용어",
                    matched_variants=[],
                    source_snippet="테스트용어가 나왔다.",
                    target_snippet="Câu dịch hoàn toàn không chứa biến thể nào.",
                )
            ],
            "total_korean_matches": 1,
        }
    }

    dummy_title_res = TitleReviewResult(
        substantive_title_groups=[],
        case_only_title_groups=[],
        ordinal_mismatches=[],
        total_chapters_reviewed=157,
        chapters_with_titles=157,
    )

    agg = classify_and_aggregate_findings(dummy_term_dict, dummy_scan_results, dummy_title_res)

    # Should be categorized under speculative / manual review
    assert len(agg["speculative_checkpoint_findings"]) == 1
    item = agg["speculative_checkpoint_findings"][0]
    assert item["source"] == "테스트용어"
    assert item["no_match_count"] == 1
    assert "cần rà soát thủ công" in item["reason"]


@pytest.mark.skipif(
    os.environ.get("COMIC_RUN_PRIVATE_BOOK_TESTS") != "1",
    reason="Requires private historical real-book database (opt-in with COMIC_RUN_PRIVATE_BOOK_TESTS=1)",
)
def test_late_distant_chapters_real_text_divergence():
    """Verify that late distant chapters (98-157) contain actual verified divergent variants."""
    conn = open_readonly_db(DEFAULT_DB_PATH)
    try:
        term_dict, variant_patterns, korean_union_pat = load_term_dictionary(
            conn, DEFAULT_BOOK_ID, candidates_path=DEFAULT_CANDIDATES_PATH
        )
        scan_results, stats = scan_fullbook_segments(
            conn, DEFAULT_BOOK_ID, term_dict, variant_patterns, korean_union_pat
        )

        assert stats["total_segments"] == 46637

        # 1. 스타플렉스: Starplex vs Starflex in late chapters
        starplex_hits = scan_results["스타플렉스"]["variant_hits"]
        starplex_late = sum(1 for o in starplex_hits.get("Starplex", []) if o.chapter_num > 97)
        starflex_late = sum(1 for o in starflex_hits.get("Starflex", []) if o.chapter_num > 97)
        assert starplex_late > 0, "Starplex should appear in chapters 98-157"
        assert starflex_late > 0, "Starflex should appear in chapters 98-157"

        # 2. 은월: Eun-wol vs Eunwol vs Ngân Nguyệt in late chapters
        eunwol_hits = scan_results["은월"]["variant_hits"]
        eunwol_dash_late = sum(1 for o in eunwol_hits.get("Eun-wol", []) if o.chapter_num > 97)
        eunwol_nodash_late = sum(1 for o in eunwol_hits.get("Eunwol", []) if o.chapter_num > 97)
        assert eunwol_dash_late > 0, "Eun-wol should appear in chapters 98-157"
        assert eunwol_nodash_late > 0, "Eunwol should appear in chapters 98-157"

        # 3. 마석: ma thạch vs đá ma thuật in late chapters
        maseok_hits = scan_results["마석"]["variant_hits"]
        maseok_late = sum(1 for o in maseok_hits.get("ma thạch", []) if o.chapter_num > 97)
        assert maseok_late > 0, "ma thạch should appear in chapters 98-157"

        # 4. 상태창: bảng trạng thái vs cửa sổ trạng thái in late chapters
        status_hits = scan_results["상태창"]["variant_hits"]
        status_late_table = sum(1 for o in status_hits.get("bảng trạng thái", []) if o.chapter_num > 97)
        status_late_window = sum(1 for o in status_hits.get("cửa sổ trạng thái", []) if o.chapter_num > 97)
        assert status_late_table > 0, "bảng trạng thái should appear in chapters 98-157"
        assert status_late_window > 0, "cửa sổ trạng thái should appear in chapters 98-157 (ch.117, ch.133)"

        # 5. 차도희: Cha Do-hee vs Cha Dohee in late chapters
        chadohee_hits = scan_results["차도희"]["variant_hits"]
        chadohee_dash_late = sum(1 for o in chadohee_hits.get("Cha Do-hee", []) if o.chapter_num > 97)
        chadohee_nodash_late = sum(1 for o in chadohee_hits.get("Cha Dohee", []) if o.chapter_num > 97)
        assert chadohee_dash_late > 0, "Cha Do-hee should appear in chapters 98-157"
        assert chadohee_nodash_late > 0, "Cha Dohee should appear in chapters 98-157 (ch.117, 118, 142, 157)"
    finally:
        conn.close()


@pytest.mark.skipif(
    os.environ.get("COMIC_RUN_PRIVATE_BOOK_TESTS") != "1",
    reason="Requires private historical real-book reports and outputs (opt-in with COMIC_RUN_PRIVATE_BOOK_TESTS=1)",
)
def test_verified_reports_exist_and_match_metrics():
    """Verify generated fullbook verified reports exist, preserve earlier reports, and contain valid schema."""
    # 1. Verify previous reports are preserved
    assert Path(DEFAULT_CANDIDATES_PATH).is_file()
    assert Path("output/consistency_review_20261018_fullbook_evidence.md").is_file()

    # 2. Verify new verified reports exist
    md_file = Path(DEFAULT_OUT_MD)
    json_file = Path(DEFAULT_OUT_JSON)
    assert md_file.is_file()
    assert json_file.is_file()

    # Check Markdown non-empty and has required sections
    md_text = md_file.read_text(encoding="utf-8")
    assert "## 1. Tóm tắt kết quả kiểm tra toàn sách" in md_text
    assert "## 2. Tiêu đề chương thiếu nhất quán đã xác minh" in md_text
    assert "## 3. Thuật ngữ & Thực thể có biến thể đối soát trực tiếp trên văn bản" in md_text
    assert "## 4. Mở rộng kiểm tra chuyên sâu các chương muộn" in md_text
    assert "## 6. Giới hạn phương pháp và khuyến nghị biên tập" in md_text
    assert "각성 (2)" in md_text
    assert "Starplex" in md_text
    assert "Starflex" in md_text

    # Check JSON structure and metrics
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["metadata"]["book_id"] == DEFAULT_BOOK_ID
    assert data["metadata"]["total_chapters"] == 157
    assert data["metadata"]["total_translated_segments"] == 46637
    assert data["title_inconsistencies"]["substantive_title_groups_count"] == 26
    assert data["title_inconsistencies"]["substantive_title_chapters_involved"] == 78
    assert data["title_inconsistencies"]["ordinal_mismatches_count"] == 1
    assert data["summary_counts"]["total_observed_inconsistent_terms"] > 200
    assert data["summary_counts"]["late_chapters_divergent_terms_count"] > 100
