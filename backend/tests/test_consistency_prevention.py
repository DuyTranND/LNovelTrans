"""Regression and integration tests for chapter title consistency prevention.

Tests get_prior_chapter_title_mappings against isolated temporary SQLite databases:
1. Normalizes Korean numbered (1) installment titles to root mappings.
2. Excludes current and future chapters (spine_index >= before_spine_index).
3. Omits conflicting historical variants unless locked glossary overrides.
4. Preserves all saved user data without modifications (strictly read-only).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from backend.app.context import get_prior_chapter_title_mappings
from backend.app.db import create_glossary_item, get_connection, init_db, utc_now


def _insert_book(conn: sqlite3.Connection, book_id: str = "book_1") -> str:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO books (id, title, author, language, identifier, file_path, created_at, updated_at, story_bible)
        VALUES (?, 'Test Novel', 'Author', 'ko', 'ident-1', 'test.epub', ?, ?, '')
        """,
        (book_id, now, now),
    )
    return book_id


def _insert_chapter(
    conn: sqlite3.Connection,
    book_id: str,
    chapter_id: str,
    spine_index: int,
    title: str = "Chapter",
) -> str:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO chapters (
            id, book_id, manifest_id, spine_index, archive_path, href, title,
            media_type, raw_bytes, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'application/xhtml+xml', X'00', 'translated', ?, ?)
        """,
        (chapter_id, book_id, f"man_{chapter_id}", spine_index, f"OEBPS/{chapter_id}.xhtml", f"{chapter_id}.xhtml", title, now, now),
    )
    return chapter_id


def _insert_segment(
    conn: sqlite3.Connection,
    segment_id: str,
    chapter_id: str,
    book_id: str,
    seq_order: int,
    source: str,
    translation: str,
    parent_tag: str = "h1",
    dom_path: str = "/html/body/h1",
) -> str:
    conn.execute(
        """
        INSERT INTO segments (
            id, chapter_id, book_id, seq_order, dom_path, node_type,
            original_source, source, translation, parent_tag, is_whitespace
        ) VALUES (?, ?, ?, ?, ?, 'text', ?, ?, ?, ?, 0)
        """,
        (segment_id, chapter_id, book_id, seq_order, dom_path, source, source, translation, parent_tag),
    )
    return segment_id


@pytest.fixture
def temp_db(tmp_path: Path) -> tuple[Path, str]:
    db_path = tmp_path / "test_consistency.db"
    init_db(db_path)
    book_id = "test_book_consistency"
    with get_connection(db_path) as conn:
        _insert_book(conn, book_id=book_id)
    return db_path, book_id


def test_numbered_installment_normalized_to_root_mapping(temp_db: tuple[Path, str]) -> None:
    """Korean numbered installment title like (1) is stripped to root mapping."""
    db_path, book_id = temp_db

    with get_connection(db_path) as conn:
        _insert_chapter(conn, book_id=book_id, chapter_id="ch_0", spine_index=0, title="상점 오픈 (1)")
        _insert_segment(
            conn,
            segment_id="seg_0_1",
            chapter_id="ch_0",
            book_id=book_id,
            seq_order=0,
            source="상점 오픈 (1)",
            translation="Mở cửa tiệm (1)",
            parent_tag="h1",
        )

    mappings = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=1)
    assert mappings == {"상점 오픈": "Mở cửa tiệm"}


def test_future_and_current_chapters_excluded(temp_db: tuple[Path, str]) -> None:
    """Chapters with spine_index >= before_spine_index are strictly excluded."""
    db_path, book_id = temp_db

    with get_connection(db_path) as conn:
        # Chapter 0 (spine 0)
        _insert_chapter(conn, book_id=book_id, chapter_id="ch_0", spine_index=0)
        _insert_segment(
            conn, "s0", "ch_0", book_id, 0,
            source="상점 오픈 (1)", translation="Mở cửa tiệm (1)", parent_tag="h1"
        )
        # Chapter 1 (spine 1)
        _insert_chapter(conn, book_id=book_id, chapter_id="ch_1", spine_index=1)
        _insert_segment(
            conn, "s1", "ch_1", book_id, 0,
            source="새로운 여정 (1)", translation="Hành trình mới (1)", parent_tag="h1"
        )
        # Chapter 2 (spine 2)
        _insert_chapter(conn, book_id=book_id, chapter_id="ch_2", spine_index=2)
        _insert_segment(
            conn, "s2", "ch_2", book_id, 0,
            source="미래의 전투 (1)", translation="Trận chiến tương lai (1)", parent_tag="h1"
        )

    # When querying for chapter 1 (before_spine_index=1), only chapter 0 is included
    mappings_for_ch1 = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=1)
    assert mappings_for_ch1 == {"상점 오픈": "Mở cửa tiệm"}
    assert "새로운 여정" not in mappings_for_ch1
    assert "미래의 전투" not in mappings_for_ch1

    # When querying for chapter 2 (before_spine_index=2), chapters 0 and 1 are included, chapter 2 excluded
    mappings_for_ch2 = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=2)
    assert mappings_for_ch2 == {
        "새로운 여정": "Hành trình mới",
        "상점 오픈": "Mở cửa tiệm",
    }
    assert "미래의 전투" not in mappings_for_ch2


def test_conflicting_historical_variants_omitted_unless_locked_glossary_overrides(
    temp_db: tuple[Path, str],
) -> None:
    """Historical conflicts across prior chapters are omitted, but locked glossary overrides strictly."""
    db_path, book_id = temp_db

    with get_connection(db_path) as conn:
        # Chapter 0 (spine 0): translated as 'Chiến đấu'
        _insert_chapter(conn, book_id=book_id, chapter_id="ch_0", spine_index=0)
        _insert_segment(
            conn, "s0", "ch_0", book_id, 0,
            source="전투 (1)", translation="Chiến đấu (1)", parent_tag="h1"
        )
        # Chapter 1 (spine 1): conflicting translation 'Trận đánh'
        _insert_chapter(conn, book_id=book_id, chapter_id="ch_1", spine_index=1)
        _insert_segment(
            conn, "s1", "ch_1", book_id, 0,
            source="전투 (2)", translation="Trận đánh (2)", parent_tag="h1"
        )

    # 1. Without locked glossary: conflict detected, variant is omitted
    mappings_conflict = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=2)
    assert "전투" not in mappings_conflict

    # 2. With locked glossary: locked entry takes strict priority and resolves the mapping
    create_glossary_item(
        db_path,
        book_id=book_id,
        source="전투",
        target="Chiến trận",
        locked=True,
        notes="Ưu tiên tuyệt đối",
    )

    mappings_locked = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=2)
    assert mappings_locked.get("전투") == "Chiến trận"


def test_no_saved_user_data_edits_strictly_read_only(temp_db: tuple[Path, str]) -> None:
    """Calling get_prior_chapter_title_mappings is strictly read-only and never modifies saved user data."""
    db_path, book_id = temp_db

    with get_connection(db_path) as conn:
        _insert_chapter(conn, book_id=book_id, chapter_id="ch_0", spine_index=0)
        _insert_segment(
            conn, "s0", "ch_0", book_id, 0,
            source="상점 오픈 (1)", translation="Mở cửa tiệm (1)", parent_tag="h1"
        )

    create_glossary_item(
        db_path,
        book_id=book_id,
        source="상점 오픈",
        target="Mở cửa tiệm",
        locked=True,
    )

    def _snapshot_tables() -> dict[str, list[dict[str, Any]]]:
        snap = {}
        with get_connection(db_path) as conn:
            for table in ("books", "chapters", "segments", "glossary"):
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                snap[table] = [dict(r) for r in rows]
        return snap

    before_snapshot = _snapshot_tables()

    # Call get_prior_chapter_title_mappings multiple times
    res1 = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=1)
    res2 = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=2)
    res3 = get_prior_chapter_title_mappings(db_path, book_id=book_id, before_spine_index=0)
    assert res1 == {"상점 오픈": "Mở cửa tiệm"}
    assert res2 == {"상점 오픈": "Mở cửa tiệm"}
    assert res3 == {}

    after_snapshot = _snapshot_tables()

    # Verify zero mutations
    assert before_snapshot == after_snapshot
