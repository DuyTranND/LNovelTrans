"""SQLite persistence layer for Trạm Dịch."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from backend.app.epub import ParsedEpub


class RevisionConflictError(Exception):
    """Raised when one or more segments fail optimistic concurrency check."""

    def __init__(self, conflicts: list[dict[str, Any]], message: str = "Revision conflict detected"):
        super().__init__(message)
        self.conflicts = conflicts
        self.message = message


def utc_now() -> str:
    """Current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_connection(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Provide a per-operation SQLite connection with foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str) -> None:
    """Initialize database schema with WAL mode and required tables."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY,
                title TEXT,
                author TEXT,
                language TEXT,
                identifier TEXT,
                file_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                story_bible TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS chapters (
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
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS segments (
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
                revision INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS glossary (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                locked INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                chapter_id TEXT,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                chapter_id TEXT,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                params TEXT DEFAULT '{}',
                progress TEXT DEFAULT '{}',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS translation_memory (
                context_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS model_comparisons (
                id TEXT PRIMARY KEY,
                book_id TEXT,
                chapter_id TEXT NOT NULL,
                segment_ids TEXT NOT NULL,
                source_segments TEXT NOT NULL,
                shared_prompt TEXT NOT NULL,
                model_a TEXT NOT NULL,
                model_b TEXT NOT NULL,
                result_a TEXT NOT NULL,
                result_b TEXT NOT NULL,
                preferred_model TEXT DEFAULT NULL,
                rating INTEGER DEFAULT NULL,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_chapters_book_spine ON chapters(book_id, spine_index);
            CREATE INDEX IF NOT EXISTS idx_segments_chapter_seq ON segments(chapter_id, seq_order);
            CREATE INDEX IF NOT EXISTS idx_glossary_book ON glossary(book_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_chapter_status ON jobs(chapter_id, status);
            CREATE INDEX IF NOT EXISTS idx_jobs_book_status ON jobs(book_id, status);
            CREATE INDEX IF NOT EXISTS idx_model_comparisons_chapter ON model_comparisons(chapter_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_model_comparisons_created_at ON model_comparisons(created_at DESC);

            CREATE TABLE IF NOT EXISTS consistency_audit_runs (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                model TEXT NOT NULL,
                total_chapters INTEGER NOT NULL DEFAULT 0,
                total_segments INTEGER NOT NULL DEFAULT 0,
                processed_segments INTEGER NOT NULL DEFAULT 0,
                failed_segments INTEGER NOT NULL DEFAULT 0,
                skipped_segments INTEGER NOT NULL DEFAULT 0,
                progress TEXT DEFAULT '{}',
                error TEXT DEFAULT NULL,
                is_stale INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS consistency_audit_checkpoints (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                chapter_id TEXT NOT NULL,
                segment_ids TEXT NOT NULL,
                raw_findings TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES consistency_audit_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS consistency_audit_findings (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                book_id TEXT NOT NULL,
                category TEXT NOT NULL,
                source_term TEXT NOT NULL,
                source_term_id TEXT NOT NULL,
                canonical_recommendation TEXT NOT NULL,
                reason TEXT NOT NULL,
                confidence TEXT NOT NULL,
                severity TEXT NOT NULL,
                glossary_divergence INTEGER NOT NULL DEFAULT 0,
                glossary_target TEXT DEFAULT NULL,
                glossary_locked INTEGER NOT NULL DEFAULT 0,
                variants TEXT NOT NULL,
                occurrences TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES consistency_audit_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_audit_runs_book_status ON consistency_audit_runs(book_id, status);
            CREATE INDEX IF NOT EXISTS idx_audit_runs_book_created ON consistency_audit_runs(book_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_checkpoints_run_batch ON consistency_audit_checkpoints(run_id, batch_index);
            CREATE INDEX IF NOT EXISTS idx_audit_findings_run_cat ON consistency_audit_findings(run_id, category);
            CREATE INDEX IF NOT EXISTS idx_audit_findings_run_sev ON consistency_audit_findings(run_id, severity);

            CREATE TABLE IF NOT EXISTS consistency_adjudication_checkpoints (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                candidate_terms TEXT NOT NULL,
                adjudications TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES consistency_audit_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_adjudication_checkpoints_run_batch ON consistency_adjudication_checkpoints(run_id, batch_index);

            CREATE TABLE IF NOT EXISTS consistency_semantic_checkpoints (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                segment_ids TEXT NOT NULL,
                raw_findings TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES consistency_audit_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_semantic_checkpoints_run_batch ON consistency_semantic_checkpoints(run_id, batch_index);

            CREATE TABLE IF NOT EXISTS consistency_correction_proposals (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                book_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL DEFAULT 0,
                chapter_title TEXT NOT NULL DEFAULT '',
                segment_id TEXT NOT NULL,
                source_term TEXT NOT NULL,
                canonical_target TEXT NOT NULL,
                old_text TEXT NOT NULL,
                new_text TEXT NOT NULL,
                category TEXT NOT NULL,
                confidence TEXT NOT NULL,
                classification TEXT NOT NULL,
                reason TEXT NOT NULL,
                is_selected INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                applied_at TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES consistency_audit_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE,
                FOREIGN KEY (segment_id) REFERENCES segments(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_corr_proposals_run_class ON consistency_correction_proposals(run_id, classification, status);
            CREATE INDEX IF NOT EXISTS idx_corr_proposals_book_seg ON consistency_correction_proposals(book_id, segment_id);

            CREATE TABLE IF NOT EXISTS consistency_correction_snapshots (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                total_segments_changed INTEGER NOT NULL DEFAULT 0,
                is_rolled_back INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                rolled_back_at TEXT DEFAULT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES consistency_audit_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_corr_snapshots_book ON consistency_correction_snapshots(book_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS consistency_correction_backup_items (
                id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                pre_text TEXT NOT NULL,
                post_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (snapshot_id) REFERENCES consistency_correction_snapshots(id) ON DELETE CASCADE,
                FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE,
                FOREIGN KEY (segment_id) REFERENCES segments(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_corr_backup_snapshot ON consistency_correction_backup_items(snapshot_id);

            CREATE TABLE IF NOT EXISTS characters (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                canonical_source TEXT NOT NULL,
                canonical_target TEXT NOT NULL,
                source_aliases TEXT NOT NULL DEFAULT '[]',
                gender TEXT NOT NULL DEFAULT 'unknown',
                speech_style TEXT NOT NULL DEFAULT '',
                relationships TEXT NOT NULL DEFAULT '',
                first_occurrence_chapter_id TEXT DEFAULT NULL,
                first_occurrence_chapter_index INTEGER DEFAULT NULL,
                locked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                FOREIGN KEY (first_occurrence_chapter_id) REFERENCES chapters(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_characters_book ON characters(book_id);
            CREATE INDEX IF NOT EXISTS idx_characters_book_source ON characters(book_id, canonical_source);

            CREATE TABLE IF NOT EXISTS discovery_runs (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                model TEXT NOT NULL,
                chapter_ids TEXT NOT NULL DEFAULT '[]',
                total_chapters INTEGER NOT NULL DEFAULT 0,
                processed_chapters INTEGER NOT NULL DEFAULT 0,
                total_segments INTEGER NOT NULL DEFAULT 0,
                processed_segments INTEGER NOT NULL DEFAULT 0,
                proposals_count INTEGER NOT NULL DEFAULT 0,
                error TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_discovery_runs_book ON discovery_runs(book_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS discovery_proposals (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                run_id TEXT,
                category TEXT NOT NULL,
                canonical_source TEXT NOT NULL,
                canonical_target TEXT NOT NULL,
                source_aliases TEXT NOT NULL DEFAULT '[]',
                gender TEXT NOT NULL DEFAULT 'unknown',
                speech_style TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                first_occurrence_chapter_id TEXT DEFAULT NULL,
                first_occurrence_chapter_index INTEGER DEFAULT NULL,
                segment_id TEXT DEFAULT NULL,
                source_span TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES discovery_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (first_occurrence_chapter_id) REFERENCES chapters(id) ON DELETE SET NULL,
                FOREIGN KEY (segment_id) REFERENCES segments(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_disc_proposals_book_status ON discovery_proposals(book_id, status);
            CREATE INDEX IF NOT EXISTS idx_disc_proposals_run ON discovery_proposals(run_id);
            CREATE INDEX IF NOT EXISTS idx_disc_proposals_book_cat ON discovery_proposals(book_id, category);

            CREATE TABLE IF NOT EXISTS prompt_presets (
                id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                system_prompt TEXT NOT NULL,
                user_prompt_template TEXT NOT NULL,
                review_system_prompt TEXT DEFAULT '',
                review_user_prompt_template TEXT DEFAULT '',
                few_shot_examples TEXT NOT NULL DEFAULT '[]',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_prompt_presets_book ON prompt_presets(book_id);
            """
        )

        # Additive migration for segments.revision
        seg_cols = [col["name"] for col in conn.execute("PRAGMA table_info(segments)").fetchall()]
        if seg_cols and "revision" not in seg_cols:
            conn.execute("ALTER TABLE segments ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;")


def save_parsed_book(
    db_path: Path | str,
    book_id: str,
    parsed: ParsedEpub,
    file_path: str,
) -> dict[str, Any]:
    """Persist a parsed EPUB into database."""
    now = utc_now()
    title = parsed.metadata.title or "Untitled"
    author = parsed.metadata.creator or ""
    language = parsed.metadata.language or ""
    identifier = parsed.metadata.identifier or ""

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO books (id, title, author, language, identifier, file_path, created_at, updated_at, story_bible)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (book_id, title, author, language, identifier, file_path, now, now),
        )

        for spine_idx, chapter in enumerate(parsed.spine_chapters):
            chapter_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO chapters (id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, raw_bytes, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'untranslated', ?, ?)
                """,
                (
                    chapter_id,
                    book_id,
                    chapter.chapter_id,
                    spine_idx,
                    chapter.archive_path,
                    chapter.href,
                    chapter.title,
                    chapter.media_type,
                    chapter.raw_bytes,
                    now,
                    now,
                ),
            )

            for seq_idx, seg in enumerate(chapter.segments):
                seg_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO segments (id, chapter_id, book_id, seq_order, dom_path, node_type, original_source, source, translation, parent_tag, is_whitespace, review_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
                    """,
                    (
                        seg_id,
                        chapter_id,
                        book_id,
                        seq_idx,
                        seg.dom_path,
                        seg.node_type,
                        seg.text,
                        seg.text,
                        seg.parent_tag,
                        1 if seg.is_whitespace else 0,
                    ),
                )

    return get_book(db_path, book_id) or {}


def list_books(db_path: Path | str) -> list[dict[str, Any]]:
    """List all books with chapter and translation counts."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                b.id, b.title, b.author, b.language, b.identifier, b.created_at, b.updated_at,
                COUNT(DISTINCT c.id) as chapter_count,
                COUNT(DISTINCT CASE WHEN c.status = 'translated' THEN c.id END) as translated_count,
                COUNT(DISTINCT s.id) as total_segments,
                COUNT(DISTINCT CASE WHEN s.translation IS NOT NULL AND s.translation != '' THEN s.id END) as translated_segments
            FROM books b
            LEFT JOIN chapters c ON b.id = c.book_id
            LEFT JOIN segments s ON c.id = s.chapter_id
            GROUP BY b.id
            ORDER BY b.created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def get_book(db_path: Path | str, book_id: str) -> dict[str, Any] | None:
    """Retrieve book metadata and progress."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                b.id, b.title, b.author, b.language, b.identifier, b.file_path, b.created_at, b.updated_at, b.story_bible,
                COUNT(DISTINCT c.id) as chapter_count,
                COUNT(DISTINCT CASE WHEN c.status = 'translated' THEN c.id END) as translated_count,
                COUNT(DISTINCT s.id) as total_segments,
                COUNT(DISTINCT CASE WHEN s.translation IS NOT NULL AND s.translation != '' THEN s.id END) as translated_segments
            FROM books b
            LEFT JOIN chapters c ON b.id = c.book_id
            LEFT JOIN segments s ON c.id = s.chapter_id
            WHERE b.id = ?
            GROUP BY b.id
            """,
            (book_id,),
        ).fetchone()
        return dict(row) if row else None


def get_book_chapters(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """Retrieve all chapters for a book in exact spine reading order."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                c.id, c.book_id, c.manifest_id, c.spine_index, c.archive_path, c.href, c.title, c.media_type, c.status,
                COUNT(s.id) as segment_count,
                COUNT(CASE WHEN s.translation IS NOT NULL AND s.translation != '' THEN 1 END) as translated_segment_count
            FROM chapters c
            LEFT JOIN segments s ON c.id = s.chapter_id
            WHERE c.book_id = ?
            GROUP BY c.id
            ORDER BY c.spine_index ASC
            """,
            (book_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_chapter(db_path: Path | str, chapter_id: str) -> dict[str, Any] | None:
    """Retrieve chapter details with ordered text segments (safe preview)."""
    with get_connection(db_path) as conn:
        c_row = conn.execute(
            """
            SELECT id, book_id, manifest_id, spine_index, archive_path, href, title, media_type, status, created_at, updated_at
            FROM chapters
            WHERE id = ?
            """,
            (chapter_id,),
        ).fetchone()
        if not c_row:
            return None

        seg_rows = conn.execute(
            """
            SELECT id, chapter_id, book_id, seq_order, dom_path, node_type, source, translation, parent_tag, is_whitespace, review_data, revision
            FROM segments
            WHERE chapter_id = ?
            ORDER BY seq_order ASC
            """,
            (chapter_id,),
        ).fetchall()

        chapter_dict = dict(c_row)
        segments: list[dict[str, Any]] = []
        for s in seg_rows:
            seg = dict(s)
            seg["is_whitespace"] = bool(seg["is_whitespace"])
            seg["revision"] = int(s["revision"]) if "revision" in s.keys() and s["revision"] is not None else 1
            if seg["review_data"]:
                try:
                    seg["review_data"] = json.loads(seg["review_data"])
                except Exception:
                    pass
            segments.append(seg)
        chapter_dict["segments"] = segments
        return chapter_dict


def has_running_job(
    db_path: Path | str, book_id: str, chapter_id: str | None = None
) -> bool:
    """Check if there is an active job running or pausing for the given book or chapter."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM jobs
            WHERE book_id = ? AND status IN ('running', 'pausing')
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()
        return bool(row)


def has_active_job(db_path: Path | str, book_id: str) -> bool:
    """Check if there is any live job queued, running, or pausing for the given book."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM jobs
            WHERE book_id = ? AND status IN ('queued', 'running', 'pausing', 'pending')
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()
        return bool(row)


def update_chapter_segments(
    db_path: Path | str,
    chapter_id: str,
    segment_updates: list[dict[str, Any]],
    new_status: str | None = None,
    enforce_revisions: bool = False,
) -> dict[str, Any]:
    """Apply source and translation edits to segments in a chapter with validation and CAS concurrency.

    Source change invalidates old translation/review unless new translation explicitly provided.
    Does not accept arbitrary client chapter status; recalculates accurately.
    Atomic CAS check guarantees no partial updates if any revision conflict is encountered.
    """
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        # Check chapter existence
        c_row = conn.execute(
            "SELECT id, book_id, status FROM chapters WHERE id = ?", (chapter_id,)
        ).fetchone()
        if not c_row:
            raise KeyError(f"Chapter '{chapter_id}' not found")

        book_id = c_row["book_id"]

        # Validate segment IDs and fetch current state including revision
        seg_ids = [u["id"] for u in segment_updates if "id" in u]
        if len(seg_ids) != len(set(seg_ids)): raise ValueError("Duplicate segment ID in one update batch")
        found_map: dict[str, sqlite3.Row] = {}
        if seg_ids:
            placeholders = ",".join("?" for _ in seg_ids)
            rows = conn.execute(
                f"SELECT id, source, translation, review_data, revision FROM segments WHERE chapter_id = ? AND id IN ({placeholders})",
                [chapter_id, *seg_ids],
            ).fetchall()
            found_map = {r["id"]: r for r in rows}
            missing = set(seg_ids) - set(found_map.keys())
            if missing:
                raise KeyError(f"Invalid segment IDs for chapter: {missing}")

            # Optimistic concurrency check (Atomic CAS)
            conflicts: list[dict[str, Any]] = []
            for update in segment_updates:
                seg_id = update["id"]
                curr_row = found_map[seg_id]
                expected_rev = update.get("expected_revision")

                if enforce_revisions and expected_rev is None:
                    raise ValueError(f"Thiếu 'expected_revision' cho đoạn '{seg_id}' trong chế độ cộng tác")

                if expected_rev is not None:
                    curr_rev = (
                        int(curr_row["revision"])
                        if "revision" in curr_row.keys() and curr_row["revision"] is not None
                        else 1
                    )
                    if int(expected_rev) != curr_rev:
                        conflicts.append({
                            "segment_id": seg_id,
                            "expected_revision": int(expected_rev),
                            "current_revision": curr_rev,
                            "current_source": curr_row["source"],
                            "current_translation": curr_row["translation"],
                        })

            if conflicts:
                raise RevisionConflictError(
                    conflicts=conflicts,
                    message="Xung đột phiên bản: Một hoặc nhiều đoạn văn đã được sửa đổi bởi biên tập viên khác.",
                )

        for update in segment_updates:
            seg_id = update["id"]
            curr_row = found_map.get(seg_id)
            if not curr_row:
                curr_row = conn.execute(
                    "SELECT source, translation, review_data, revision FROM segments WHERE id = ?",
                    (seg_id,),
                ).fetchone()

            fields: list[str] = []
            values: list[Any] = []

            source_changed = False
            if "source" in update and update["source"] is not None:
                new_src = update["source"]
                if curr_row and new_src != curr_row["source"]:
                    source_changed = True
                fields.append("source = ?")
                values.append(new_src)

            translation_changed = False
            if "translation" in update:
                new_trans = update["translation"]
                if curr_row and new_trans != curr_row["translation"]:
                    translation_changed = True
                fields.append("translation = ?")
                values.append(new_trans)
            elif source_changed:
                # Source change invalidates old translation unless explicitly provided
                fields.append("translation = ?")
                values.append(None)

            if "review_data" in update:
                fields.append("review_data = ?")
                rev = update["review_data"]
                values.append(json.dumps(rev) if isinstance(rev, (dict, list)) else rev)
            elif source_changed or translation_changed:
                # Source or translation change invalidates old review unless explicitly provided
                fields.append("review_data = ?")
                values.append(None)

            if fields:
                fields.append("revision = revision + 1")
                expected_rev = update.get("expected_revision")
                if expected_rev is not None:
                    cursor = conn.execute(
                        f"UPDATE segments SET {', '.join(fields)} WHERE id = ? AND chapter_id = ? AND revision = ?",
                        [*values, seg_id, chapter_id, int(expected_rev)],
                    )
                    if cursor.rowcount == 0:
                        cur_row = conn.execute(
                            "SELECT source, translation, revision FROM segments WHERE id = ?",
                            (seg_id,),
                        ).fetchone()
                        cur_rev = (
                            int(cur_row["revision"])
                            if cur_row and cur_row["revision"] is not None
                            else 1
                        )
                        cur_source = cur_row["source"] if cur_row else ""
                        cur_trans = cur_row["translation"] if cur_row else None
                        raise RevisionConflictError(
                            conflicts=[{
                                "segment_id": seg_id,
                                "expected_revision": int(expected_rev),
                                "current_revision": cur_rev,
                                "current_source": cur_source,
                                "current_translation": cur_trans,
                            }],
                            message="Xung đột phiên bản: Một hoặc nhiều đoạn văn đã được sửa đổi bởi biên tập viên khác.",
                        )
                else:
                    values.append(seg_id)
                    conn.execute(
                        f"UPDATE segments SET {', '.join(fields)} WHERE id = ?",
                        values,
                    )

        # Recompute chapter status based on actual segment completion
        total_non_ws = conn.execute(
            "SELECT COUNT(*) as cnt FROM segments WHERE chapter_id = ? AND is_whitespace = 0",
            (chapter_id,),
        ).fetchone()["cnt"]

        translated_count = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM segments
            WHERE chapter_id = ? AND is_whitespace = 0 AND translation IS NOT NULL AND translation != ''
            """,
            (chapter_id,),
        ).fetchone()["cnt"]

        if translated_count == 0:
            computed_status = "untranslated"
        elif translated_count == total_non_ws:
            computed_status = "translated"
        else:
            computed_status = "partial"

        # Client cannot force an arbitrary or false chapter status
        status_to_set = computed_status

        conn.execute(
            "UPDATE chapters SET status = ?, updated_at = ? WHERE id = ?",
            (status_to_set, now, chapter_id),
        )
        conn.execute(
            "UPDATE books SET updated_at = ? WHERE id = ?",
            (now, book_id),
        )
        conn.execute(
            "UPDATE consistency_audit_runs SET is_stale = 1, updated_at = ? WHERE book_id = ?",
            (now, book_id),
        )

    updated = get_chapter(db_path, chapter_id)
    if not updated:
        raise KeyError(f"Failed to retrieve updated chapter {chapter_id}")
    return updated


def list_glossary(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """List glossary terms for a book."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, book_id, source, target, locked, notes, created_at, updated_at
            FROM glossary
            WHERE book_id = ?
            ORDER BY source ASC
            """,
            (book_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["locked"] = bool(d["locked"])
            result.append(d)
        return result


def create_glossary_item(
    db_path: Path | str,
    book_id: str,
    source: str,
    target: str,
    locked: bool = False,
    notes: str = "",
) -> dict[str, Any]:
    """Create a new glossary item, rejecting conflicting duplicate source terms."""
    now = utc_now()
    item_id = uuid.uuid4().hex
    norm_source = source.strip()
    norm_target = target.strip()
    if not norm_source:
        raise ValueError("Thuật ngữ nguồn không được để trống")
    if not norm_target:
        raise ValueError("Thuật ngữ dịch không được để trống")

    with get_connection(db_path) as conn:
        # Verify book exists
        if not conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone():
            raise KeyError(f"Book '{book_id}' not found")

        # Conflicting duplicate source term reject
        dup = conn.execute(
            "SELECT id FROM glossary WHERE book_id = ? AND LOWER(TRIM(source)) = LOWER(?)",
            (book_id, norm_source),
        ).fetchone()
        if dup:
            raise ValueError(f"Thuật ngữ nguồn '{norm_source}' đã tồn tại trong từ điển.")

        conn.execute(
            """
            INSERT INTO glossary (id, book_id, source, target, locked, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, book_id, norm_source, norm_target, 1 if locked else 0, notes, now, now),
        )
    return {
        "id": item_id,
        "book_id": book_id,
        "source": norm_source,
        "target": norm_target,
        "locked": locked,
        "notes": notes,
        "created_at": now,
        "updated_at": now,
    }


def get_glossary_item(db_path: Path | str, item_id: str) -> dict[str, Any] | None:
    """Retrieve single glossary item."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, book_id, source, target, locked, notes, created_at, updated_at FROM glossary WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["locked"] = bool(d["locked"])
        return d


def update_glossary_item(
    db_path: Path | str,
    item_id: str,
    source: str | None = None,
    target: str | None = None,
    locked: bool | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Update an existing glossary item, rejecting edits to locked terms unless unlocked first."""
    now = utc_now()
    with get_connection(db_path) as conn:
        current = conn.execute(
            "SELECT id, book_id, source, target, locked, notes FROM glossary WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not current:
            raise KeyError(f"Glossary item '{item_id}' not found")

        book_id = current["book_id"]
        is_locked = bool(current["locked"])

        # Reject modifications to locked glossary unless explicitly unlocked first
        if is_locked and (locked is None or locked is True):
            if source is not None or target is not None or notes is not None:
                raise ValueError("Không thể chỉnh sửa thuật ngữ đã khóa. Vui lòng mở khóa (locked=False) trước.")

        # Check duplicate source if source is updated
        if source is not None:
            norm_source = source.strip()
            if not norm_source:
                raise ValueError("Thuật ngữ nguồn không được để trống")
            dup = conn.execute(
                "SELECT id FROM glossary WHERE book_id = ? AND id != ? AND LOWER(TRIM(source)) = LOWER(?)",
                (book_id, item_id, norm_source),
            ).fetchone()
            if dup:
                raise ValueError(f"Thuật ngữ nguồn '{norm_source}' đã tồn tại trong từ điển.")

        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]
        if source is not None:
            fields.append("source = ?")
            values.append(source.strip())
        if target is not None:
            fields.append("target = ?")
            values.append(target.strip())
        if locked is not None:
            fields.append("locked = ?")
            values.append(1 if locked else 0)
        if notes is not None:
            fields.append("notes = ?")
            values.append(notes)

        values.append(item_id)
        conn.execute(f"UPDATE glossary SET {', '.join(fields)} WHERE id = ?", values)

    item = get_glossary_item(db_path, item_id)
    if not item:
        raise KeyError(f"Glossary item '{item_id}' not found after update")
    return item


def delete_glossary_item(db_path: Path | str, item_id: str) -> bool:
    """Delete a glossary item, rejecting deletion if locked."""
    with get_connection(db_path) as conn:
        current = conn.execute("SELECT locked FROM glossary WHERE id = ?", (item_id,)).fetchone()
        if not current:
            return False
        if bool(current["locked"]):
            raise ValueError("Không thể xóa thuật ngữ đã khóa. Vui lòng mở khóa trước khi xóa.")

        cursor = conn.execute("DELETE FROM glossary WHERE id = ?", (item_id,))
        return cursor.rowcount > 0


def create_job(
    db_path: Path | str,
    book_id: str,
    chapter_ids: list[str] | None = None,
    params: dict[str, Any] | None = None,
    job_type: str = "translation",
) -> dict[str, Any]:
    """Create a new job in queued status."""
    now = utc_now()
    job_id = uuid.uuid4().hex
    job_params = params or {}

    with get_connection(db_path) as conn:
        if not conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone():
            raise KeyError(f"Book '{book_id}' not found")

        if not chapter_ids:
            rows = conn.execute(
                "SELECT id FROM chapters WHERE book_id = ? ORDER BY spine_index ASC",
                (book_id,),
            ).fetchall()
            effective_chapter_ids = [r["id"] for r in rows]
        else:
            effective_chapter_ids = chapter_ids

        job_params["chapter_ids"] = effective_chapter_ids

        total_segments = 0
        completed_segments = 0
        if effective_chapter_ids:
            placeholders = ",".join("?" for _ in effective_chapter_ids)
            row_total = conn.execute(
                f"SELECT COUNT(*) as cnt FROM segments WHERE chapter_id IN ({placeholders}) AND is_whitespace = 0",
                effective_chapter_ids,
            ).fetchone()
            total_segments = row_total["cnt"] if row_total else 0

            row_comp = conn.execute(
                f"""
                SELECT COUNT(*) as cnt FROM segments
                WHERE chapter_id IN ({placeholders}) AND is_whitespace = 0 AND translation IS NOT NULL AND translation != ''
                """,
                effective_chapter_ids,
            ).fetchone()
            completed_segments = row_comp["cnt"] if row_comp else 0

        initial_progress = {
            "completed_segments": completed_segments,
            "total_segments": total_segments,
            "chapters": {cid: {"completed_segments": 0, "status": "queued"} for cid in effective_chapter_ids},
            "stage": "queued",
            "summaries": {},
            "review": {"issues": [], "corrections": {}},
        }
        if job_params.get('max_segments') is not None:
            initial_progress['total_segments'] = min(total_segments, completed_segments + job_params['max_segments'])

        conn.execute(
            """
            INSERT INTO jobs (id, book_id, chapter_id, job_type, status, params, progress, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, NULL, ?, ?)
            """,
            (
                job_id,
                book_id,
                effective_chapter_ids[0] if effective_chapter_ids else None,
                job_type,
                json.dumps(job_params),
                json.dumps(initial_progress),
                now,
                now,
            ),
        )

    job = get_job(db_path, job_id)
    if not job:
        raise KeyError("Failed to retrieve created job")
    return job


def get_job(db_path: Path | str, job_id: str) -> dict[str, Any] | None:
    """Retrieve job details with deserialized params and progress."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, book_id, chapter_id, job_type, status, params, progress, error, created_at, updated_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["params"] = json.loads(d["params"]) if d["params"] else {}
        except Exception:
            d["params"] = {}
        try:
            d["progress"] = json.loads(d["progress"]) if d["progress"] else {}
        except Exception:
            d["progress"] = {}

        d["chapter_ids"] = d["params"].get("chapter_ids", [])
        d["completed_segments"] = d["progress"].get("completed_segments", 0)
        d["total_segments"] = d["progress"].get("total_segments", 0)
        return d


def list_jobs(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """List all jobs for a book ordered by created_at DESC."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, book_id, chapter_id, job_type, status, params, progress, error, created_at, updated_at FROM jobs WHERE book_id = ? ORDER BY created_at DESC",
            (book_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["params"] = json.loads(d["params"]) if d["params"] else {}
            except Exception:
                d["params"] = {}
            try:
                d["progress"] = json.loads(d["progress"]) if d["progress"] else {}
            except Exception:
                d["progress"] = {}
            d["chapter_ids"] = d["params"].get("chapter_ids", [])
            d["completed_segments"] = d["progress"].get("completed_segments", 0)
            d["total_segments"] = d["progress"].get("total_segments", 0)
            result.append(d)
        return result


def update_job_status(
    db_path: Path | str,
    job_id: str,
    status: str,
    error: str | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically update status, error, and progress of a job."""
    now = utc_now()
    with get_connection(db_path) as conn:
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, now]
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        elif status in ("queued", "running"):
            fields.append("error = NULL")

        if progress is not None:
            fields.append("progress = ?")
            values.append(json.dumps(progress))

        values.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)

    job = get_job(db_path, job_id)
    if not job:
        raise KeyError(f"Job '{job_id}' not found")
    return job


def update_job_progress(
    db_path: Path | str,
    job_id: str,
    progress: dict[str, Any],
) -> None:
    """Update job progress payload."""
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?",
            (json.dumps(progress), now, job_id),
        )


def recover_interrupted_jobs(db_path: Path | str) -> None:
    """Restart recovery: convert interrupted 'running' jobs to 'queued' and 'pausing' to 'paused'."""
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
            (now,),
        )
        conn.execute(
            "UPDATE jobs SET status = 'paused', updated_at = ? WHERE status = 'pausing'",
            (now,),
        )


def claim_next_job(db_path: Path | str) -> dict[str, Any] | None:
    """Claim next queued job atomically ensuring no other live job runs for same book."""
    now = utc_now()
    with get_connection(db_path) as conn:
        candidates = conn.execute(
            "SELECT id, book_id FROM jobs WHERE status = 'queued' ORDER BY created_at ASC",
        ).fetchall()

        for cand in candidates:
            cand_id = cand["id"]
            b_id = cand["book_id"]
            active = conn.execute(
                "SELECT 1 FROM jobs WHERE book_id = ? AND id != ? AND status IN ('running', 'pausing') LIMIT 1",
                (b_id, cand_id),
            ).fetchone()
            if active:
                continue

            cursor = conn.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
                (now, cand_id),
            )
            if cursor.rowcount > 0:
                return get_job(db_path, cand_id)

    return None


def save_summary(db_path: Path | str, book_id: str, chapter_id: str, summary: str) -> None:
    """Save or update chapter narrative summary."""
    now = utc_now()
    summary_id = uuid.uuid4().hex
    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM summaries WHERE book_id = ? AND chapter_id = ?",
            (book_id, chapter_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE summaries SET summary = ?, created_at = ? WHERE id = ?",
                (summary, now, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO summaries (id, book_id, chapter_id, summary, created_at) VALUES (?, ?, ?, ?, ?)",
                (summary_id, book_id, chapter_id, summary, now),
            )


def get_book_summaries(db_path: Path | str, book_id: str, before_spine_index: int | None = None) -> list[str]:
    """Retrieve list of summaries for a book in creation order."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT s.summary FROM summaries s JOIN chapters c ON c.id = s.chapter_id "
            "WHERE s.book_id = ? AND (? IS NULL OR c.spine_index < ?) ORDER BY c.spine_index ASC",
            (book_id, before_spine_index, before_spine_index),
        ).fetchall()
        return [r["summary"] for r in rows if r["summary"]]


def get_untranslated_segments_count(db_path: Path | str, chapter_id: str) -> int:
    """Count non-whitespace untranslated segments for a chapter."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM segments
            WHERE chapter_id = ? AND is_whitespace = 0 AND (translation IS NULL OR translation = '')
            """,
            (chapter_id,),
        ).fetchone()
        return row["cnt"] if row else 0


def update_chapter_status(db_path: Path | str, chapter_id: str, status: str) -> None:
    """Update status of a chapter."""
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE chapters SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, chapter_id),
        )


def get_story_bible(db_path: Path | str, book_id: str) -> str | None:
    """Retrieve story bible content for a book."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT story_bible FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            return None
        return row["story_bible"] or ""


def update_story_bible(db_path: Path | str, book_id: str, text: str) -> str:
    """Update story bible content for a book."""
    now = utc_now()
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE books SET story_bible = ?, updated_at = ? WHERE id = ?",
            (text, now, book_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"Book '{book_id}' not found")
    return text


def get_export_bundle(
    db_path: Path | str, book_id: str
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Retrieve book metadata and translations map for export."""
    book = get_book(db_path, book_id)
    if not book:
        raise KeyError(f"Book '{book_id}' not found")

    with get_connection(db_path) as conn:
        chapters = conn.execute(
            "SELECT id, archive_path FROM chapters WHERE book_id = ? ORDER BY spine_index ASC",
            (book_id,),
        ).fetchall()

        translations: dict[str, dict[str, str]] = {}
        for c in chapters:
            c_id = c["id"]
            arc_path = c["archive_path"]

            segments = conn.execute(
                """
                SELECT dom_path, original_source, source, translation
                FROM segments
                WHERE chapter_id = ?
                ORDER BY seq_order ASC
                """,
                (c_id,),
            ).fetchall()

            chapter_mods: dict[str, str] = {}
            for s in segments:
                # Apply translation if present; else apply edited source if changed from original
                if s["translation"] is not None and s["translation"] != "":
                    chapter_mods[s["dom_path"]] = s["translation"]
                elif s["source"] != s["original_source"]:
                    chapter_mods[s["dom_path"]] = s["source"]

            if chapter_mods:
                translations[arc_path] = chapter_mods

    return book, translations


def get_setting(db_path: Path | str, key: str, default: str | None = None) -> str | None:
    """Retrieve setting value."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(db_path: Path | str, key: str, value: str) -> None:
    """Set setting value."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _deserialize_comparison(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Deserialize JSON text fields of a model_comparison row."""
    data = dict(row)
    for field in ("segment_ids", "source_segments", "result_a", "result_b"):
        val = data.get(field)
        if isinstance(val, str):
            try:
                data[field] = json.loads(val)
            except Exception:
                pass
    return data


def create_comparison(
    db_path: Path | str,
    comparison_data: dict[str, Any],
) -> dict[str, Any]:
    """Persist a new model comparison record in isolated model_comparisons table."""
    now = utc_now()
    cid = comparison_data.get("id") or str(uuid.uuid4())
    seg_ids_json = (
        json.dumps(comparison_data["segment_ids"], ensure_ascii=False)
        if not isinstance(comparison_data.get("segment_ids"), str)
        else comparison_data["segment_ids"]
    )
    src_segs_json = (
        json.dumps(comparison_data["source_segments"], ensure_ascii=False)
        if not isinstance(comparison_data.get("source_segments"), str)
        else comparison_data["source_segments"]
    )
    res_a_json = (
        json.dumps(comparison_data["result_a"], ensure_ascii=False)
        if not isinstance(comparison_data.get("result_a"), str)
        else comparison_data["result_a"]
    )
    res_b_json = (
        json.dumps(comparison_data["result_b"], ensure_ascii=False)
        if not isinstance(comparison_data.get("result_b"), str)
        else comparison_data["result_b"]
    )
    shared_prompt = comparison_data.get("shared_prompt", "")

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO model_comparisons (
                id, book_id, chapter_id, segment_ids, source_segments,
                shared_prompt, model_a, model_b, result_a, result_b,
                preferred_model, rating, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                comparison_data.get("book_id"),
                comparison_data["chapter_id"],
                seg_ids_json,
                src_segs_json,
                shared_prompt,
                comparison_data["model_a"],
                comparison_data["model_b"],
                res_a_json,
                res_b_json,
                comparison_data.get("preferred_model"),
                comparison_data.get("rating"),
                comparison_data.get("notes", ""),
                now,
                now,
            ),
        )
    result = get_comparison(db_path, cid)
    if result is None:
        raise RuntimeError(f"Không thể truy vấn bản ghi so sánh vừa tạo: {cid}")
    return result


def list_comparisons(
    db_path: Path | str,
    chapter_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List comparison history ordered by creation date descending."""
    with get_connection(db_path) as conn:
        if chapter_id:
            rows = conn.execute(
                """
                SELECT * FROM model_comparisons
                WHERE chapter_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (chapter_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM model_comparisons
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_deserialize_comparison(row) for row in rows]


def get_comparison(db_path: Path | str, comparison_id: str) -> dict[str, Any] | None:
    """Retrieve single comparison record by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM model_comparisons WHERE id = ?", (comparison_id,)
        ).fetchone()
        return _deserialize_comparison(row) if row else None


def update_comparison(
    db_path: Path | str,
    comparison_id: str,
    preferred_model: str | None = None,
    rating: int | None = None,
    notes: str | None = None,
) -> dict[str, Any] | None:
    """Update human rating, preferred model, and notes for an existing comparison."""
    now = utc_now()
    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM model_comparisons WHERE id = ?", (comparison_id,)
        ).fetchone()
        if not existing:
            return None

        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if preferred_model is not None:
            updates.append("preferred_model = ?")
            params.append(preferred_model)
        if rating is not None:
            updates.append("rating = ?")
            params.append(rating)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)

        params.append(comparison_id)
        conn.execute(
            f"UPDATE model_comparisons SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    return get_comparison(db_path, comparison_id)


def get_chapter_segments_by_ids(
    db_path: Path | str,
    chapter_id: str,
    segment_ids: list[str],
) -> list[dict[str, Any]]:
    """Retrieve specific segments for a chapter, strictly ordered by seq_order."""
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, chapter_id, book_id, seq_order, dom_path, node_type, source, translation, parent_tag, is_whitespace
            FROM segments
            WHERE chapter_id = ? AND id IN ({placeholders})
            ORDER BY seq_order ASC
            """,
            (chapter_id, *segment_ids),
        ).fetchall()
        return [dict(r) for r in rows]


def create_audit_run(
    db_path: Path | str,
    book_id: str,
    model: str = "gemini-3.5-flash-lite",
) -> dict[str, Any]:
    """Create a new consistency audit run for a book in queued status."""
    now = utc_now()
    run_id = uuid.uuid4().hex
    with get_connection(db_path) as conn:
        # Verify book exists
        b_row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
        if not b_row:
            raise KeyError(f"Book '{book_id}' not found")

        conn.execute(
            """
            INSERT INTO consistency_audit_runs (
                id, book_id, status, model, total_chapters, total_segments,
                processed_segments, failed_segments, skipped_segments,
                progress, error, is_stale, created_at, updated_at
            )
            VALUES (?, ?, 'queued', ?, 0, 0, 0, 0, 0, '{}', NULL, 0, ?, ?)
            """,
            (run_id, book_id, model, now, now),
        )
    return get_audit_run(db_path, run_id) or {}


def get_audit_run(db_path: Path | str, run_id: str) -> dict[str, Any] | None:
    """Retrieve details and progress of a consistency audit run."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                r.id, r.book_id, r.status, r.model, r.total_chapters, r.total_segments,
                r.processed_segments, r.failed_segments, r.skipped_segments,
                r.progress, r.error, r.is_stale, r.created_at, r.updated_at,
                COUNT(f.id) as findings_count
            FROM consistency_audit_runs r
            LEFT JOIN consistency_audit_findings f ON r.id = f.run_id
            WHERE r.id = ?
            GROUP BY r.id
            """,
            (run_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["is_stale"] = bool(d["is_stale"])
        if isinstance(d["progress"], str):
            try:
                d["progress"] = json.loads(d["progress"])
            except Exception:
                d["progress"] = {}
        return d


def list_audit_runs(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """List all consistency audit runs for a book, ordered by creation time descending."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                r.id, r.book_id, r.status, r.model, r.total_chapters, r.total_segments,
                r.processed_segments, r.failed_segments, r.skipped_segments,
                r.progress, r.error, r.is_stale, r.created_at, r.updated_at,
                COUNT(f.id) as findings_count
            FROM consistency_audit_runs r
            LEFT JOIN consistency_audit_findings f ON r.id = f.run_id
            WHERE r.book_id = ?
            GROUP BY r.id
            ORDER BY r.created_at DESC
            """,
            (book_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["is_stale"] = bool(d["is_stale"])
            if isinstance(d["progress"], str):
                try:
                    d["progress"] = json.loads(d["progress"])
                except Exception:
                    d["progress"] = {}
            result.append(d)
        return result


def update_audit_run(
    db_path: Path | str,
    run_id: str,
    status: str | None = None,
    processed_segments: int | None = None,
    failed_segments: int | None = None,
    skipped_segments: int | None = None,
    total_chapters: int | None = None,
    total_segments: int | None = None,
    progress: dict[str, Any] | str | None = None,
    error: str | None = None,
    is_stale: bool | None = None,
    clear_error: bool = False,
) -> dict[str, Any] | None:
    """Update fields on a consistency audit run."""
    now = utc_now()
    updates: list[str] = ["updated_at = ?"]
    params: list[Any] = [now]

    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if processed_segments is not None:
        updates.append("processed_segments = ?")
        params.append(processed_segments)
    if failed_segments is not None:
        updates.append("failed_segments = ?")
        params.append(failed_segments)
    if skipped_segments is not None:
        updates.append("skipped_segments = ?")
        params.append(skipped_segments)
    if total_chapters is not None:
        updates.append("total_chapters = ?")
        params.append(total_chapters)
    if total_segments is not None:
        updates.append("total_segments = ?")
        params.append(total_segments)
    if progress is not None:
        updates.append("progress = ?")
        params.append(json.dumps(progress) if isinstance(progress, dict) else progress)
    if clear_error or (error is not None and error == ""):
        updates.append("error = NULL")
    elif error is not None:
        updates.append("error = ?")
        params.append(error)
    if is_stale is not None:
        updates.append("is_stale = ?")
        params.append(1 if is_stale else 0)

    params.append(run_id)

    with get_connection(db_path) as conn:
        conn.execute(
            f"UPDATE consistency_audit_runs SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    return get_audit_run(db_path, run_id)


def has_active_audit_run(db_path: Path | str, book_id: str) -> bool:
    """Check if there is an active (queued, running, pausing) audit run for the book."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM consistency_audit_runs
            WHERE book_id = ? AND status IN ('queued', 'running', 'pausing')
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()
        return row is not None


def save_audit_checkpoint(
    db_path: Path | str,
    run_id: str,
    batch_index: int,
    chapter_id: str,
    segment_ids: list[str],
    raw_findings: list[dict[str, Any]],
) -> str:
    """Persist a completed batch checkpoint with extracted occurrences for resumability."""
    cp_id = uuid.uuid4().hex
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_audit_checkpoints (
                id, run_id, batch_index, chapter_id, segment_ids, raw_findings, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cp_id,
                run_id,
                batch_index,
                chapter_id,
                json.dumps(segment_ids),
                json.dumps(raw_findings),
                now,
            ),
        )
    return cp_id


def get_audit_checkpoints(db_path: Path | str, run_id: str) -> list[dict[str, Any]]:
    """Retrieve all saved batch checkpoints for an audit run."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, run_id, batch_index, chapter_id, segment_ids, raw_findings, created_at
            FROM consistency_audit_checkpoints
            WHERE run_id = ?
            ORDER BY batch_index ASC
            """,
            (run_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["segment_ids"] = json.loads(d["segment_ids"])
            except Exception:
                d["segment_ids"] = []
            try:
                d["raw_findings"] = json.loads(d["raw_findings"])
            except Exception:
                d["raw_findings"] = []
            result.append(d)
        return result


def save_adjudication_checkpoint(
    db_path: Path | str,
    run_id: str,
    batch_index: int,
    candidate_terms: list[str],
    adjudications: list[dict[str, Any]],
) -> str:
    """Persist an adjudication batch checkpoint for resilient restartability."""
    now = utc_now()
    cp_id = str(uuid.uuid4()).replace("-", "")
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_adjudication_checkpoints (
                id, run_id, batch_index, candidate_terms, adjudications, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cp_id,
                run_id,
                batch_index,
                json.dumps(candidate_terms, ensure_ascii=False),
                json.dumps(adjudications, ensure_ascii=False),
                now,
            ),
        )
    return cp_id


def get_adjudication_checkpoints(db_path: Path | str, run_id: str) -> list[dict[str, Any]]:
    """Retrieve all saved adjudication checkpoints for an audit run."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, run_id, batch_index, candidate_terms, adjudications, created_at
            FROM consistency_adjudication_checkpoints
            WHERE run_id = ?
            ORDER BY batch_index ASC
            """,
            (run_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["candidate_terms"] = json.loads(d["candidate_terms"])
            except Exception:
                d["candidate_terms"] = []
            try:
                d["adjudications"] = json.loads(d["adjudications"])
            except Exception:
                d["adjudications"] = []
            result.append(d)
        return result


def save_semantic_checkpoint(
    db_path: Path | str,
    run_id: str,
    chapter_id: str,
    batch_index: int,
    segment_ids: list[str],
    raw_findings: list[dict[str, Any]],
) -> str:
    """Persist a semantic review batch checkpoint for resumability."""
    now = utc_now()
    cp_id = str(uuid.uuid4()).replace("-", "")
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_semantic_checkpoints (
                id, run_id, chapter_id, batch_index, segment_ids, raw_findings, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cp_id,
                run_id,
                chapter_id,
                batch_index,
                json.dumps(segment_ids, ensure_ascii=False),
                json.dumps(raw_findings, ensure_ascii=False),
                now,
            ),
        )
    return cp_id


def get_semantic_checkpoints(db_path: Path | str, run_id: str) -> list[dict[str, Any]]:
    """Retrieve all saved semantic review checkpoints for an audit run."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, run_id, chapter_id, batch_index, segment_ids, raw_findings, created_at
            FROM consistency_semantic_checkpoints
            WHERE run_id = ?
            ORDER BY batch_index ASC
            """,
            (run_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["segment_ids"] = json.loads(d["segment_ids"])
            except Exception:
                d["segment_ids"] = []
            try:
                d["raw_findings"] = json.loads(d["raw_findings"])
            except Exception:
                d["raw_findings"] = []
            result.append(d)
        return result


def save_audit_findings(
    db_path: Path | str,
    run_id: str,
    book_id: str,
    findings: list[dict[str, Any]],
) -> None:
    """Save aggregated audit findings, replacing any prior findings for the run to prevent duplicates."""
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM consistency_audit_findings WHERE run_id = ?", (run_id,))
        for f in findings:
            find_id = uuid.uuid4().hex
            occs = f.get("occurrences", [])
            variants = f.get("variants", [])
            conn.execute(
                """
                INSERT INTO consistency_audit_findings (
                    id, run_id, book_id, category, source_term, source_term_id,
                    canonical_recommendation, reason, confidence, severity,
                    glossary_divergence, glossary_target, glossary_locked,
                    variants, occurrences, occurrence_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    find_id,
                    run_id,
                    book_id,
                    f.get("category", "term"),
                    f.get("source_term", ""),
                    f.get("source_term_id", ""),
                    f.get("canonical_recommendation", ""),
                    f.get("reason", ""),
                    f.get("confidence", "medium"),
                    f.get("severity", "medium"),
                    1 if f.get("glossary_divergence") else 0,
                    f.get("glossary_target"),
                    1 if f.get("glossary_locked") else 0,
                    json.dumps(variants, ensure_ascii=False),
                    json.dumps(occs, ensure_ascii=False),
                    len(occs),
                    now,
                ),
            )


def append_audit_findings(
    db_path: Path | str,
    run_id: str,
    book_id: str,
    findings: list[dict[str, Any]],
) -> None:
    """Append new audit findings without deleting existing findings."""
    now = utc_now()
    with get_connection(db_path) as conn:
        for f in findings:
            find_id = uuid.uuid4().hex
            occs = f.get("occurrences", [])
            variants = f.get("variants", [])
            conn.execute(
                """
                INSERT INTO consistency_audit_findings (
                    id, run_id, book_id, category, source_term, source_term_id,
                    canonical_recommendation, reason, confidence, severity,
                    glossary_divergence, glossary_target, glossary_locked,
                    variants, occurrences, occurrence_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    find_id,
                    run_id,
                    book_id,
                    f.get("category", "term"),
                    f.get("source_term", ""),
                    f.get("source_term_id", "") or f.get("source_term", ""),
                    f.get("canonical_recommendation", ""),
                    f.get("reason", ""),
                    f.get("confidence", "medium"),
                    f.get("severity", "medium"),
                    1 if f.get("glossary_divergence") else 0,
                    f.get("glossary_target"),
                    1 if f.get("glossary_locked") else 0,
                    json.dumps(variants, ensure_ascii=False) if isinstance(variants, list) else str(variants),
                    json.dumps(occs, ensure_ascii=False) if isinstance(occs, list) else str(occs),
                    len(occs) if isinstance(occs, list) else 0,
                    now,
                ),
            )


def get_audit_findings(
    db_path: Path | str,
    run_id: str,
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """Retrieve paginated and filtered findings for an audit run."""
    import math

    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size

    where_clauses = ["run_id = ?"]
    params: list[Any] = [run_id]

    if category and category.lower() != "all":
        where_clauses.append("category = ?")
        params.append(category.lower())

    if severity and severity.lower() != "all":
        where_clauses.append("severity = ?")
        params.append(severity.lower())

    where_sql = " AND ".join(where_clauses)

    with get_connection(db_path) as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM consistency_audit_findings WHERE {where_sql}",
            tuple(params),
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        # Order by high severity first, then occurrence count, then creation
        order_sql = """
            ORDER BY
                CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END ASC,
                occurrence_count DESC,
                source_term ASC,
                id ASC
        """
        rows = conn.execute(
            f"""
            SELECT
                id, run_id, book_id, category, source_term, source_term_id,
                canonical_recommendation, reason, confidence, severity,
                glossary_divergence, glossary_target, glossary_locked,
                variants, occurrences, occurrence_count, created_at
            FROM consistency_audit_findings
            WHERE {where_sql}
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, offset),
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d["glossary_divergence"] = bool(d["glossary_divergence"])
            d["glossary_locked"] = bool(d["glossary_locked"])
            try:
                d["variants"] = json.loads(d["variants"])
            except Exception:
                d["variants"] = []
            try:
                d["occurrences"] = json.loads(d["occurrences"])
            except Exception:
                d["occurrences"] = []
            items.append(d)

        total_pages = math.ceil(total / page_size) if total > 0 else 1
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }


def get_all_audit_findings(
    db_path: Path | str,
    run_id: str,
    category: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve all findings for an audit run via complete deterministic pagination through get_audit_findings."""
    findings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page = 1
    page_size = 100
    while True:
        res = get_audit_findings(
            db_path,
            run_id,
            page=page,
            page_size=page_size,
            category=category,
            severity=severity,
        )
        items = res.get("items", [])
        if not items:
            break
        for item in items:
            item_id = item.get("id")
            if item_id:
                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    findings.append(item)
            else:
                findings.append(item)
        if page >= res.get("total_pages", 1) or len(items) < page_size:
            break
        page += 1
    return findings


def mark_audit_runs_stale(db_path: Path | str, book_id: str) -> None:
    """Mark all consistency audit runs for a book as stale when book translations are edited."""
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE consistency_audit_runs SET is_stale = 1, updated_at = ? WHERE book_id = ?",
            (now, book_id),
        )


def recover_interrupted_audit_runs(db_path: Path | str) -> int:
    """Mark any running or queued audit runs as paused on system restart without launching paid audits."""
    now = utc_now()
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE consistency_audit_runs
            SET status = 'paused',
                error = 'Bị ngắt do khởi động lại hệ thống. Sẵn sàng tiếp tục.',
                updated_at = ?
            WHERE status IN ('queued', 'running', 'pausing')
            """,
            (now,),
        )
        return cursor.rowcount


def seed_checkpoints_from_prior_runs(
    db_path: Path | str,
    target_run_id: str,
    book_id: str,
    planned_batches: list[dict[str, Any]],
) -> int:
    """Safely seed validated checkpoints from prior runs of the same book into target run.
    
    Checks that batch_index, chapter_id, and segment_ids match the planned batches
    and that the source checkpoint contains valid extracted raw_findings.
    Does not overwrite existing checkpoints in target_run_id.
    """
    if not planned_batches:
        return 0

    now = utc_now()
    seeded_count = 0

    with get_connection(db_path) as conn:
        existing_target_batches = {
            r["batch_index"]
            for r in conn.execute(
                "SELECT batch_index FROM consistency_audit_checkpoints WHERE run_id = ?",
                (target_run_id,),
            ).fetchall()
        }

        prior_runs = conn.execute(
            """
            SELECT r.id, count(c.id) as cp_count
            FROM consistency_audit_runs r
            JOIN consistency_audit_checkpoints c ON r.id = c.run_id
            WHERE r.book_id = ? AND r.id != ?
            GROUP BY r.id
            ORDER BY cp_count DESC
            """,
            (book_id, target_run_id),
        ).fetchall()

        if not prior_runs:
            return 0

        planned_map: dict[int, tuple[str, list[str]]] = {}
        for b_idx, b_info in enumerate(planned_batches):
            ch_id = str(b_info.get("chapter_id") or "")
            s_ids = [str(s.get("id") or "") for s in b_info.get("segments", [])]
            planned_map[b_idx] = (ch_id, s_ids)

        for p_run in prior_runs:
            p_run_id = p_run["id"]
            cps = conn.execute(
                """
                SELECT batch_index, chapter_id, segment_ids, raw_findings
                FROM consistency_audit_checkpoints
                WHERE run_id = ?
                ORDER BY batch_index
                """,
                (p_run_id,),
            ).fetchall()

            for cp in cps:
                b_idx = cp["batch_index"]
                if b_idx in existing_target_batches:
                    continue
                if b_idx not in planned_map:
                    continue

                planned_ch_id, planned_s_ids = planned_map[b_idx]
                if cp["chapter_id"] != planned_ch_id:
                    continue

                try:
                    cp_seg_ids = json.loads(cp["segment_ids"])
                except Exception:
                    continue

                if cp_seg_ids != planned_s_ids:
                    continue

                try:
                    raw_f = json.loads(cp["raw_findings"])
                    if not isinstance(raw_f, list):
                        continue
                except Exception:
                    continue

                cp_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO consistency_audit_checkpoints (
                        id, run_id, batch_index, chapter_id, segment_ids, raw_findings, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cp_id,
                        target_run_id,
                        b_idx,
                        cp["chapter_id"],
                        cp["segment_ids"],
                        cp["raw_findings"],
                        now,
                    ),
                )
                existing_target_batches.add(b_idx)
                seeded_count += 1

    return seeded_count


def save_correction_proposals(
    db_path: Path | str,
    run_id: str,
    book_id: str,
    proposals: list[dict[str, Any]],
) -> None:
    """Save correction proposals for an audit run, replacing any prior pending proposals."""
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM consistency_correction_proposals WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
        for p in proposals:
            p_id = p.get("id") or uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO consistency_correction_proposals (
                    id, run_id, book_id, chapter_id, chapter_index, chapter_title,
                    segment_id, source_term, canonical_target, old_text, new_text,
                    category, confidence, classification, reason, is_selected,
                    status, applied_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p_id,
                    run_id,
                    book_id,
                    p.get("chapter_id", ""),
                    int(p.get("chapter_index", 0)),
                    p.get("chapter_title", ""),
                    p.get("segment_id", ""),
                    p.get("source_term", ""),
                    p.get("canonical_target", ""),
                    p.get("old_text", ""),
                    p.get("new_text", ""),
                    p.get("category", "term"),
                    p.get("confidence", "medium"),
                    p.get("classification", "manual_only"),
                    p.get("reason", ""),
                    1 if p.get("is_selected", True) else 0,
                    p.get("status", "pending"),
                    p.get("applied_at"),
                    p.get("created_at", now),
                    now,
                ),
            )


def get_correction_proposals(
    db_path: Path | str,
    run_id: str,
    classification: str | None = None,
    category: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Retrieve filtered, paginated correction proposals with summary metrics."""
    where_clauses = ["run_id = ?"]
    params: list[Any] = [run_id]

    if classification and classification != "all":
        where_clauses.append("classification = ?")
        params.append(classification)

    if category and category != "all":
        where_clauses.append("category = ?")
        params.append(category)

    if status and status != "all":
        where_clauses.append("status = ?")
        params.append(status)

    where_sql = " AND ".join(where_clauses)

    with get_connection(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) as cnt FROM consistency_correction_proposals WHERE {where_sql}",
            params,
        ).fetchone()["cnt"]

        run_stats = conn.execute(
            """
            SELECT
                COUNT(*) as total_proposals,
                SUM(CASE WHEN classification = 'auto_safe' THEN 1 ELSE 0 END) as auto_safe_count,
                SUM(CASE WHEN classification = 'manual_only' THEN 1 ELSE 0 END) as manual_only_count,
                SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END) as applied_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_count,
                SUM(CASE WHEN status = 'conflict' THEN 1 ELSE 0 END) as conflict_count,
                COUNT(DISTINCT chapter_id) as affected_chapters_count
            FROM consistency_correction_proposals
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size
        total_pages = max(1, (total + page_size - 1) // page_size)

        rows = conn.execute(
            f"""
            SELECT * FROM consistency_correction_proposals
            WHERE {where_sql}
            ORDER BY chapter_index ASC, created_at ASC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d["is_selected"] = bool(d["is_selected"])
            items.append(d)

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "summary": {
                "total_proposals": run_stats["total_proposals"] or 0,
                "auto_safe_count": run_stats["auto_safe_count"] or 0,
                "manual_only_count": run_stats["manual_only_count"] or 0,
                "applied_count": run_stats["applied_count"] or 0,
                "pending_count": run_stats["pending_count"] or 0,
                "conflict_count": run_stats["conflict_count"] or 0,
                "affected_chapters_count": run_stats["affected_chapters_count"] or 0,
            },
        }


def get_correction_proposal(db_path: Path | str, proposal_id: str) -> dict[str, Any] | None:
    """Retrieve single correction proposal by ID."""
    with get_connection(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM consistency_correction_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d["is_selected"] = bool(d["is_selected"])
        return d


def update_correction_proposal_selection(
    db_path: Path | str, proposal_id: str, is_selected: bool
) -> dict[str, Any] | None:
    """Update selection flag on a single correction proposal."""
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE consistency_correction_proposals SET is_selected = ?, updated_at = ? WHERE id = ?",
            (1 if is_selected else 0, now, proposal_id),
        )
    return get_correction_proposal(db_path, proposal_id)


def batch_update_correction_proposals_selection(
    db_path: Path | str,
    run_id: str,
    is_selected: bool,
    only_auto_safe: bool = False,
) -> int:
    """Batch update selection flag across proposals for an audit run."""
    now = utc_now()
    sql = "UPDATE consistency_correction_proposals SET is_selected = ?, updated_at = ? WHERE run_id = ?"
    params: list[Any] = [1 if is_selected else 0, now, run_id]
    if only_auto_safe:
        sql += " AND classification = 'auto_safe'"
    with get_connection(db_path) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


def create_correction_snapshot(
    db_path: Path | str,
    book_id: str,
    run_id: str,
    description: str,
    items: list[dict[str, Any]],
) -> str:
    """Create a recoverable backup snapshot before applying corrections."""
    now = utc_now()
    snapshot_id = uuid.uuid4().hex
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consistency_correction_snapshots (
                id, book_id, run_id, description, total_segments_changed, is_rolled_back, created_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (snapshot_id, book_id, run_id, description, len(items), now),
        )
        for it in items:
            conn.execute(
                """
                INSERT INTO consistency_correction_backup_items (
                    id, snapshot_id, chapter_id, segment_id, pre_text, post_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    snapshot_id,
                    it["chapter_id"],
                    it["segment_id"],
                    it["pre_text"],
                    it["post_text"],
                    now,
                ),
            )
    return snapshot_id


def get_correction_snapshots(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """List historical snapshots for a book in reverse chronological order."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM consistency_correction_snapshots
            WHERE book_id = ?
            ORDER BY created_at DESC
            """,
            (book_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_correction_snapshot(db_path: Path | str, snapshot_id: str) -> dict[str, Any] | None:
    """Retrieve single correction snapshot by ID."""
    with get_connection(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM consistency_correction_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        return dict(r) if r else None


def get_correction_backup_items(db_path: Path | str, snapshot_id: str) -> list[dict[str, Any]]:
    """Retrieve all backup items for a snapshot."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM consistency_correction_backup_items WHERE snapshot_id = ? ORDER BY created_at ASC",
            (snapshot_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def apply_correction_proposals(
    db_path: Path | str,
    run_id: str,
    book_id: str,
    selected_ids: list[str] | None = None,
    only_auto_safe: bool = False,
    check_stale: bool = False,
) -> dict[str, Any]:
    """Atomically and idempotently apply selected correction proposals with optimistic locking and automatic snapshot.
    
    Protects against race conditions: if a segment has been changed since the preview
    was generated (current translation != old_text), it marks the proposal as 'conflict'
    and avoids clobbering the segment.
    Creates an automatic recoverable snapshot in consistency_correction_snapshots.
    """
    now = utc_now()

    with get_connection(db_path) as conn:
        run_row = conn.execute(
            "SELECT id, book_id, status, is_stale, total_segments, processed_segments, failed_segments, skipped_segments, progress FROM consistency_audit_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if not run_row:
            raise KeyError(f"Không tìm thấy đợt rà soát '{run_id}'.")
        if check_stale and run_row["is_stale"]:
            raise ValueError("Không thể áp dụng sửa đổi: Đợt rà soát đã cũ (stale) do bản dịch của sách đã thay đổi.")
        if run_row["status"] in ("running", "queued", "pausing"):
            raise ValueError(f"Không thể áp dụng sửa đổi: Đợt rà soát đang hoạt động (trạng thái '{run_row['status']}').")

        progress = {}
        if run_row["progress"]:
            try:
                progress = json.loads(run_row["progress"]) if isinstance(run_row["progress"], str) else run_row["progress"]
            except Exception:
                progress = {}

        semantic_stage = progress.get("semantic_stage")
        if semantic_stage is not None:
            norm_semantic_stage = str(semantic_stage).strip().lower()
            semantic_cov = str(progress.get("semantic_coverage") or "").strip().lower()
            if norm_semantic_stage != "completed":
                raise ValueError(f"Không thể áp dụng sửa đổi: Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (trạng thái '{semantic_stage}').")
            if semantic_cov != "full":
                raise ValueError(f"Không thể áp dụng sửa đổi: Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (độ bao phủ '{progress.get('semantic_coverage')}').")

        is_full = (
            run_row["status"] == "completed"
            and run_row["failed_segments"] == 0
            and run_row["skipped_segments"] == 0
            and run_row["total_segments"] > 0
            and run_row["processed_segments"] == run_row["total_segments"]
            and progress.get("coverage_status") == "full"
        )
        if not is_full:
            raise ValueError("Không thể áp dụng sửa đổi: Đợt rà soát chưa hoàn tất độ bao phủ toàn diện (incomplete/partial).")

        sql = "SELECT * FROM consistency_correction_proposals WHERE run_id = ? AND is_selected = 1"
        params: list[Any] = [run_id]

        if only_auto_safe:
            sql += " AND classification = 'auto_safe'"

        if selected_ids is not None:
            if not selected_ids:
                return {
                    "snapshot_id": None,
                    "applied_count": 0,
                    "conflict_count": 0,
                    "already_applied_count": 0,
                    "total_selected": 0,
                    "message": "Không có đề xuất nào được chọn để áp dụng.",
                }
            placeholders = ",".join("?" for _ in selected_ids)
            sql += f" AND id IN ({placeholders})"
            params.extend(selected_ids)

        proposals = [dict(r) for r in conn.execute(sql, params).fetchall()]

        if not proposals:
            return {
                "snapshot_id": None,
                "applied_count": 0,
                "conflict_count": 0,
                "already_applied_count": 0,
                "total_selected": 0,
                "message": "Không tìm thấy đề xuất nào phù hợp điều kiện áp dụng.",
            }

        to_apply: list[dict[str, Any]] = []
        conflicts_count = 0
        already_applied_count = 0

        for p in proposals:
            seg_row = conn.execute(
                "SELECT id, chapter_id, translation, source FROM segments WHERE id = ?",
                (p["segment_id"],),
            ).fetchone()

            if not seg_row:
                conn.execute(
                    "UPDATE consistency_correction_proposals SET status = 'failed', reason = 'Phân đoạn không còn tồn tại trong CSDL', updated_at = ? WHERE id = ?",
                    (now, p["id"]),
                )
                conflicts_count += 1
                continue

            current_trans = seg_row["translation"] or ""
            current_src = seg_row["source"] or ""

            if current_trans == p["new_text"]:
                conn.execute(
                    "UPDATE consistency_correction_proposals SET status = 'applied', applied_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, p["id"]),
                )
                already_applied_count += 1
                continue

            if current_trans != p["old_text"]:
                conn.execute(
                    "UPDATE consistency_correction_proposals SET status = 'conflict', reason = 'Văn bản phân đoạn đã bị thay đổi (xung đột dữ liệu)', updated_at = ? WHERE id = ?",
                    (now, p["id"]),
                )
                conflicts_count += 1
                continue

            # Stale source protection: ensure source still anchors the expected source term
            src_term = (p.get("source_term") or "").strip()
            cat = (p.get("category") or "").strip().lower()
            if src_term:
                if cat == "title":
                    from backend.app.audit import normalize_title_stem
                    stem_src = normalize_title_stem(current_src)
                    stem_term = normalize_title_stem(src_term)
                    if stem_src != stem_term and src_term not in current_src:
                        conn.execute(
                            "UPDATE consistency_correction_proposals SET status = 'conflict', reason = 'Văn bản nguồn đã bị thay đổi (tiêu đề nguồn không còn khớp)', updated_at = ? WHERE id = ?",
                            (now, p["id"]),
                        )
                        conflicts_count += 1
                        continue
                else:
                    from backend.app.provider import derive_conservative_headword, match_korean_term_in_text
                    matched_korean = (src_term in current_src) or match_korean_term_in_text(current_src, src_term)
                    if not matched_korean:
                        headword = derive_conservative_headword(src_term)
                        if headword:
                            matched_korean = (headword in current_src) or match_korean_term_in_text(current_src, headword)
                    if not matched_korean:
                        conn.execute(
                            "UPDATE consistency_correction_proposals SET status = 'conflict', reason = 'Văn bản nguồn đã bị thay đổi (thuật ngữ nguồn không còn khớp)', updated_at = ? WHERE id = ?",
                            (now, p["id"]),
                        )
                        conflicts_count += 1
                        continue

            to_apply.append({
                "proposal_id": p["id"],
                "chapter_id": p["chapter_id"],
                "segment_id": p["segment_id"],
                "pre_text": current_trans,
                "post_text": p["new_text"],
            })

        if not to_apply:
            return {
                "snapshot_id": None,
                "applied_count": 0,
                "conflict_count": conflicts_count,
                "already_applied_count": already_applied_count,
                "total_selected": len(proposals),
                "message": f"Không có phân đoạn nào cần cập nhật ({already_applied_count} đoạn đã trùng khớp, {conflicts_count} đoạn xung đột).",
            }

        snapshot_id = uuid.uuid4().hex
        snapshot_desc = f"Tự động sao lưu trước khi áp dụng {len(to_apply)} sửa đổi tính nhất quán cho đợt {run_id[:8]}"
        conn.execute(
            """
            INSERT INTO consistency_correction_snapshots (
                id, book_id, run_id, description, total_segments_changed, is_rolled_back, created_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (snapshot_id, book_id, run_id, snapshot_desc, len(to_apply), now),
        )

        impacted_chapters: set[str] = set()

        for item in to_apply:
            conn.execute(
                """
                INSERT INTO consistency_correction_backup_items (
                    id, snapshot_id, chapter_id, segment_id, pre_text, post_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    snapshot_id,
                    item["chapter_id"],
                    item["segment_id"],
                    item["pre_text"],
                    item["post_text"],
                    now,
                ),
            )

            conn.execute(
                "UPDATE segments SET translation = ? WHERE id = ? AND translation = ?",
                (item["post_text"], item["segment_id"], item["pre_text"]),
            )

            conn.execute(
                "UPDATE consistency_correction_proposals SET status = 'applied', applied_at = ?, updated_at = ? WHERE id = ?",
                (now, now, item["proposal_id"]),
            )

            impacted_chapters.add(item["chapter_id"])

        for ch_id in impacted_chapters:
            conn.execute("UPDATE chapters SET updated_at = ? WHERE id = ?", (now, ch_id))
        conn.execute("UPDATE books SET updated_at = ? WHERE id = ?", (now, book_id))

        conn.execute(
            "UPDATE consistency_audit_runs SET is_stale = 1, updated_at = ? WHERE book_id = ?",
            (now, book_id),
        )

        return {
            "snapshot_id": snapshot_id,
            "applied_count": len(to_apply),
            "conflict_count": conflicts_count,
            "already_applied_count": already_applied_count,
            "total_selected": len(proposals),
            "affected_chapters_count": len(impacted_chapters),
            "message": f"Đã áp dụng thành công {len(to_apply)} sửa đổi. Mã bản sao lưu an toàn: {snapshot_id}.",
        }


def rollback_correction_snapshot(db_path: Path | str, snapshot_id: str) -> dict[str, Any]:
    """Safely and transactionally rollback all changes from a correction snapshot back to pre_text."""
    now = utc_now()
    with get_connection(db_path) as conn:
        snap = conn.execute(
            "SELECT * FROM consistency_correction_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()

        if not snap:
            raise KeyError(f"Không tìm thấy bản sao lưu '{snapshot_id}'.")

        if snap["is_rolled_back"]:
            raise ValueError(f"Bản sao lưu '{snapshot_id}' đã được hoàn tác trước đó (vào lúc {snap['rolled_back_at']}).")

        items = conn.execute(
            "SELECT * FROM consistency_correction_backup_items WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchall()

        impacted_chapters: set[str] = set()

        for it in items:
            conn.execute(
                "UPDATE segments SET translation = ? WHERE id = ?",
                (it["pre_text"], it["segment_id"]),
            )
            conn.execute(
                """
                UPDATE consistency_correction_proposals
                SET status = 'pending', applied_at = NULL, updated_at = ?
                WHERE run_id = ? AND segment_id = ? AND status = 'applied'
                """,
                (now, snap["run_id"], it["segment_id"]),
            )
            impacted_chapters.add(it["chapter_id"])

        conn.execute(
            "UPDATE consistency_correction_snapshots SET is_rolled_back = 1, rolled_back_at = ? WHERE id = ?",
            (now, snapshot_id),
        )

        for ch_id in impacted_chapters:
            conn.execute("UPDATE chapters SET updated_at = ? WHERE id = ?", (now, ch_id))
        conn.execute("UPDATE books SET updated_at = ? WHERE id = ?", (now, snap["book_id"]))
        conn.execute(
            "UPDATE consistency_audit_runs SET is_stale = 1, updated_at = ? WHERE book_id = ?",
            (now, snap["book_id"]),
        )

        return {
            "snapshot_id": snapshot_id,
            "rolled_back_segments": len(items),
            "affected_chapters_count": len(impacted_chapters),
            "message": f"Đã hoàn tác thành công {len(items)} phân đoạn về nguyên trạng trước khi sửa.",
        }




# -----------------------------------------------------------------------------
# Structured Character Profiles
# -----------------------------------------------------------------------------

def _normalize_character_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Convert DB row to typed character profile dictionary."""
    d = dict(row)
    d["locked"] = bool(d.get("locked", False))
    raw_aliases = d.get("source_aliases", "[]")
    if isinstance(raw_aliases, str):
        try:
            parsed = json.loads(raw_aliases)
            d["source_aliases"] = parsed if isinstance(parsed, list) else []
        except Exception:
            d["source_aliases"] = []
    elif isinstance(raw_aliases, list):
        d["source_aliases"] = raw_aliases
    else:
        d["source_aliases"] = []
    return d


def list_characters(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """List all character profiles for a book, ordered by first appearance then name."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, book_id, canonical_source, canonical_target, source_aliases,
                   gender, speech_style, relationships, first_occurrence_chapter_id,
                   first_occurrence_chapter_index, locked, created_at, updated_at
            FROM characters
            WHERE book_id = ?
            ORDER BY
                CASE WHEN first_occurrence_chapter_index IS NULL THEN 1 ELSE 0 END,
                first_occurrence_chapter_index ASC,
                canonical_source ASC,
                created_at ASC
            """,
            (book_id,),
        ).fetchall()
        return [_normalize_character_row(r) for r in rows]


def get_character(db_path: Path | str, character_id: str) -> dict[str, Any] | None:
    """Retrieve single character profile by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, book_id, canonical_source, canonical_target, source_aliases,
                   gender, speech_style, relationships, first_occurrence_chapter_id,
                   first_occurrence_chapter_index, locked, created_at, updated_at
            FROM characters
            WHERE id = ?
            """,
            (character_id,),
        ).fetchone()
        return _normalize_character_row(row) if row else None


def _clean_aliases(aliases: Any, canonical_source: str) -> list[str]:
    """Clean, deduplicate, and exclude canonical source from aliases."""
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except Exception:
            aliases = [aliases]
    if not isinstance(aliases, list):
        return []
    cleaned: list[str] = []
    canon_norm = canonical_source.strip().lower()
    for a in aliases:
        if not isinstance(a, str):
            continue
        c = a.strip()
        if c and c.lower() != canon_norm and c not in cleaned:
            cleaned.append(c)
    return cleaned


def create_character(
    db_path: Path | str,
    book_id: str,
    canonical_source: str,
    canonical_target: str,
    source_aliases: list[str] | str | None = None,
    gender: str = "unknown",
    speech_style: str = "",
    relationships: str = "",
    first_occurrence_chapter_id: str | None = None,
    first_occurrence_chapter_index: int | None = None,
    locked: bool = False,
) -> dict[str, Any]:
    """Create a new book-scoped character profile with validation and conflict checks."""
    now = utc_now()
    char_id = uuid.uuid4().hex
    norm_source = (canonical_source or "").strip()
    norm_target = (canonical_target or "").strip()
    norm_gender = (gender or "unknown").strip().lower()
    if norm_gender not in ("male", "female", "other", "unknown"):
        norm_gender = "unknown"

    if not norm_source:
        raise ValueError("Tên nguồn của nhân vật không được để trống")
    if not norm_target:
        raise ValueError("Tên dịch của nhân vật không được để trống")

    cleaned_aliases = _clean_aliases(source_aliases, norm_source)
    aliases_json = json.dumps(cleaned_aliases, ensure_ascii=False)

    with get_connection(db_path) as conn:
        if not conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone():
            raise KeyError(f"Book '{book_id}' not found")

        dup = conn.execute(
            "SELECT id FROM characters WHERE book_id = ? AND LOWER(TRIM(canonical_source)) = LOWER(?)",
            (book_id, norm_source),
        ).fetchone()
        if dup:
            raise ValueError(f"Nhân vật '{norm_source}' đã tồn tại trong hồ sơ nhân vật.")

        conn.execute(
            """
            INSERT INTO characters (
                id, book_id, canonical_source, canonical_target, source_aliases,
                gender, speech_style, relationships, first_occurrence_chapter_id,
                first_occurrence_chapter_index, locked, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                char_id,
                book_id,
                norm_source,
                norm_target,
                aliases_json,
                norm_gender,
                (speech_style or "").strip(),
                (relationships or "").strip(),
                first_occurrence_chapter_id,
                first_occurrence_chapter_index,
                1 if locked else 0,
                now,
                now,
            ),
        )

    res = get_character(db_path, char_id)
    if not res:
        raise RuntimeError("Failed to retrieve created character")
    return res


def update_character(
    db_path: Path | str,
    character_id: str,
    canonical_source: str | None = None,
    canonical_target: str | None = None,
    source_aliases: list[str] | str | None = None,
    gender: str | None = None,
    speech_style: str | None = None,
    relationships: str | None = None,
    first_occurrence_chapter_id: str | None = None,
    first_occurrence_chapter_index: int | None = None,
    locked: bool | None = None,
) -> dict[str, Any]:
    """Update an existing character profile, rejecting edits to locked profiles unless unlocked first."""
    now = utc_now()
    with get_connection(db_path) as conn:
        current = conn.execute(
            "SELECT id, book_id, canonical_source, canonical_target, source_aliases, gender, locked FROM characters WHERE id = ?",
            (character_id,),
        ).fetchone()
        if not current:
            raise KeyError(f"Character '{character_id}' not found")

        book_id = current["book_id"]
        is_locked = bool(current["locked"])

        # Reject modifications to locked characters unless explicitly unlocking (locked=False)
        has_substantive_edits = any(
            x is not None for x in (
                canonical_source, canonical_target, source_aliases, gender,
                speech_style, relationships, first_occurrence_chapter_id,
                first_occurrence_chapter_index
            )
        )
        if is_locked and (locked is None or locked is True):
            if has_substantive_edits:
                raise ValueError("Không thể chỉnh sửa nhân vật đã khóa. Vui lòng mở khóa (locked=False) trước.")

        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]

        effective_source = current["canonical_source"]
        if canonical_source is not None:
            norm_source = canonical_source.strip()
            if not norm_source:
                raise ValueError("Tên nguồn của nhân vật không được để trống")
            dup = conn.execute(
                "SELECT id FROM characters WHERE book_id = ? AND id != ? AND LOWER(TRIM(canonical_source)) = LOWER(?)",
                (book_id, character_id, norm_source),
            ).fetchone()
            if dup:
                raise ValueError(f"Nhân vật '{norm_source}' đã tồn tại trong hồ sơ nhân vật.")
            fields.append("canonical_source = ?")
            values.append(norm_source)
            effective_source = norm_source

        if canonical_target is not None:
            norm_target = canonical_target.strip()
            if not norm_target:
                raise ValueError("Tên dịch của nhân vật không được để trống")
            fields.append("canonical_target = ?")
            values.append(norm_target)

        if source_aliases is not None:
            cleaned_aliases = _clean_aliases(source_aliases, effective_source)
            fields.append("source_aliases = ?")
            values.append(json.dumps(cleaned_aliases, ensure_ascii=False))

        if gender is not None:
            norm_gender = gender.strip().lower()
            if norm_gender not in ("male", "female", "other", "unknown"):
                norm_gender = "unknown"
            fields.append("gender = ?")
            values.append(norm_gender)

        if speech_style is not None:
            fields.append("speech_style = ?")
            values.append(speech_style.strip())

        if relationships is not None:
            fields.append("relationships = ?")
            values.append(relationships.strip())

        if first_occurrence_chapter_id is not None:
            fields.append("first_occurrence_chapter_id = ?")
            values.append(first_occurrence_chapter_id)

        if first_occurrence_chapter_index is not None:
            fields.append("first_occurrence_chapter_index = ?")
            values.append(first_occurrence_chapter_index)

        if locked is not None:
            fields.append("locked = ?")
            values.append(1 if locked else 0)

        values.append(character_id)
        conn.execute(f"UPDATE characters SET {', '.join(fields)} WHERE id = ?", values)

    res = get_character(db_path, character_id)
    if not res:
        raise KeyError(f"Character '{character_id}' not found after update")
    return res


def delete_character(db_path: Path | str, character_id: str) -> bool:
    """Delete a character profile, rejecting deletion if locked."""
    with get_connection(db_path) as conn:
        current = conn.execute("SELECT locked FROM characters WHERE id = ?", (character_id,)).fetchone()
        if not current:
            return False
        if bool(current["locked"]):
            raise ValueError("Không thể xóa nhân vật đã khóa. Vui lòng mở khóa trước khi xóa.")

        cursor = conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))
        return cursor.rowcount > 0


def find_character_by_source_term(
    db_path: Path | str,
    book_id: str,
    term: str,
) -> dict[str, Any] | None:
    """Lookup character by exact canonical source, alias, or particle-stripped stem."""
    clean_term = (term or "").strip()
    if not clean_term:
        return None
    chars = list_characters(db_path, book_id)
    # 1. Exact match canonical source or alias
    for c in chars:
        if c["canonical_source"] == clean_term:
            return c
        if clean_term in c["source_aliases"]:
            return c

    # 2. Try stripping common Korean particles if remaining stem is >= 2 chars
    korean_particles = (
        "에게서", "한테서", "에서는", "으로는", "에게", "한테", "에서",
        "으로", "로는", "와는", "과는", "는", "은", "이", "가", "를",
        "을", "의", "에", "와", "과", "로"
    )
    for p in korean_particles:
        if clean_term.endswith(p):
            stem = clean_term[:-len(p)].strip()
            if len(stem) >= 2:
                for c in chars:
                    if c["canonical_source"] == stem or stem in c["source_aliases"]:
                        return c
    return None


# -----------------------------------------------------------------------------
# Discovery Runs & Proposals
# -----------------------------------------------------------------------------

def create_discovery_run(
    db_path: Path | str,
    book_id: str,
    model: str,
    chapter_ids: list[str],
    total_chapters: int = 0,
    total_segments: int = 0,
) -> dict[str, Any]:
    """Create a new discovery run record."""
    now = utc_now()
    run_id = uuid.uuid4().hex
    ch_json = json.dumps(chapter_ids)
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO discovery_runs (
                id, book_id, status, model, chapter_ids, total_chapters,
                processed_chapters, total_segments, processed_segments,
                proposals_count, error, created_at, updated_at
            )
            VALUES (?, ?, 'queued', ?, ?, ?, 0, ?, 0, 0, NULL, ?, ?)
            """,
            (run_id, book_id, model, ch_json, total_chapters, total_segments, now, now),
        )
    return get_discovery_run(db_path, run_id)  # type: ignore


def get_discovery_run(db_path: Path | str, run_id: str) -> dict[str, Any] | None:
    """Retrieve discovery run by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM discovery_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["chapter_ids"] = json.loads(d.get("chapter_ids", "[]"))
        except Exception:
            d["chapter_ids"] = []
        return d


def list_discovery_runs(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """List all discovery runs for a book."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM discovery_runs WHERE book_id = ? ORDER BY created_at DESC",
            (book_id,),
        ).fetchall()
        runs: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["chapter_ids"] = json.loads(d.get("chapter_ids", "[]"))
            except Exception:
                d["chapter_ids"] = []
            runs.append(d)
        return runs


def update_discovery_run(
    db_path: Path | str,
    run_id: str,
    status: str | None = None,
    processed_chapters: int | None = None,
    processed_segments: int | None = None,
    proposals_count: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Update progress or status of a discovery run."""
    now = utc_now()
    with get_connection(db_path) as conn:
        fields = ["updated_at = ?"]
        values: list[Any] = [now]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if processed_chapters is not None:
            fields.append("processed_chapters = ?")
            values.append(processed_chapters)
        if processed_segments is not None:
            fields.append("processed_segments = ?")
            values.append(processed_segments)
        if proposals_count is not None:
            fields.append("proposals_count = ?")
            values.append(proposals_count)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        values.append(run_id)
        conn.execute(f"UPDATE discovery_runs SET {', '.join(fields)} WHERE id = ?", values)
    res = get_discovery_run(db_path, run_id)
    if not res:
        raise KeyError(f"Discovery run '{run_id}' not found after update")
    return res


def recover_interrupted_discovery_runs(db_path: Path | str) -> int:
    """Mark interrupted discovery runs as interrupted on application restart."""
    now = utc_now()
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE discovery_runs
            SET status = 'interrupted',
                error = 'Tiến trình bị gián đoạn do khởi động lại ứng dụng',
                updated_at = ?
            WHERE status IN ('queued', 'running', 'processing')
            """,
            (now,),
        )
        return cursor.rowcount


def _normalize_proposal_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    raw_aliases = d.get("source_aliases", "[]")
    if isinstance(raw_aliases, str):
        try:
            parsed = json.loads(raw_aliases)
            d["source_aliases"] = parsed if isinstance(parsed, list) else []
        except Exception:
            d["source_aliases"] = []
    elif isinstance(raw_aliases, list):
        d["source_aliases"] = raw_aliases
    else:
        d["source_aliases"] = []
    return d


def get_discovery_proposal(db_path: Path | str, proposal_id: str) -> dict[str, Any] | None:
    """Retrieve single discovery proposal by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM discovery_proposals WHERE id = ?", (proposal_id,)).fetchone()
        return _normalize_proposal_row(row) if row else None


def create_discovery_proposal(
    db_path: Path | str,
    book_id: str,
    run_id: str | None = None,
    category: str = "GLOSSARY",
    canonical_source: str = "",
    canonical_target: str = "",
    source_aliases: list[str] | str | None = None,
    gender: str = "unknown",
    speech_style: str = "",
    notes: str = "",
    first_occurrence_chapter_id: str | None = None,
    first_occurrence_chapter_index: int | None = None,
    segment_id: str | None = None,
    source_span: str = "",
) -> dict[str, Any]:
    """Persist an AI discovery candidate proposal."""
    now = utc_now()
    prop_id = uuid.uuid4().hex
    norm_cat = (category or "GLOSSARY").strip().upper()
    if norm_cat not in ("CHARACTER", "GLOSSARY", "PLACE", "ORGANIZATION"):
        norm_cat = "GLOSSARY"

    norm_src = (canonical_source or "").strip()
    norm_tgt = (canonical_target or "").strip()
    norm_gender = (gender or "unknown").strip().lower()
    if norm_gender not in ("male", "female", "other", "unknown"):
        norm_gender = "unknown"

    if not norm_src or not norm_tgt:
        raise ValueError("Source term and target term must not be empty.")

    cleaned_aliases = _clean_aliases(source_aliases, norm_src)
    aliases_json = json.dumps(cleaned_aliases, ensure_ascii=False)

    with get_connection(db_path) as conn:
        existing = conn.execute(
            """
            SELECT id FROM discovery_proposals
            WHERE book_id = ? AND category = ? AND LOWER(TRIM(canonical_source)) = LOWER(?) AND status = 'pending'
            """,
            (book_id, norm_cat, norm_src),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE discovery_proposals
                SET canonical_target = ?, source_aliases = ?, gender = ?,
                    speech_style = CASE WHEN speech_style = '' THEN ? ELSE speech_style END,
                    notes = CASE WHEN notes = '' THEN ? ELSE notes END,
                    source_span = CASE WHEN source_span = '' THEN ? ELSE source_span END,
                    updated_at = ?
                WHERE id = ?
                """,
                (norm_tgt, aliases_json, norm_gender, speech_style.strip(), notes.strip(), source_span.strip(), now, existing["id"]),
            )
            prop_id = existing["id"]
        else:
            conn.execute(
                """
                INSERT INTO discovery_proposals (
                    id, book_id, run_id, category, canonical_source, canonical_target,
                    source_aliases, gender, speech_style, notes, first_occurrence_chapter_id,
                    first_occurrence_chapter_index, segment_id, source_span, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    prop_id,
                    book_id,
                    run_id,
                    norm_cat,
                    norm_src,
                    norm_tgt,
                    aliases_json,
                    norm_gender,
                    speech_style.strip(),
                    notes.strip(),
                    first_occurrence_chapter_id,
                    first_occurrence_chapter_index,
                    segment_id,
                    source_span.strip(),
                    now,
                    now,
                ),
            )

    res = get_discovery_proposal(db_path, prop_id)
    if not res:
        raise RuntimeError("Failed to retrieve created proposal")
    return res


def list_discovery_proposals(
    db_path: Path | str,
    book_id: str,
    status: str | None = None,
    category: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """List discovery proposals for a book with optional status/category/run filters."""
    with get_connection(db_path) as conn:
        query = ["SELECT * FROM discovery_proposals WHERE book_id = ?"]
        params: list[Any] = [book_id]
        if status:
            query.append("AND status = ?")
            params.append(status.strip().lower())
        if category:
            query.append("AND category = ?")
            params.append(category.strip().upper())
        if run_id:
            query.append("AND run_id = ?")
            params.append(run_id)

        query.append("ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, created_at DESC")
        rows = conn.execute(" ".join(query), params).fetchall()
        return [_normalize_proposal_row(r) for r in rows]


def update_discovery_proposal_status(
    db_path: Path | str,
    proposal_id: str,
    status: str,
) -> dict[str, Any]:
    """Update status of a discovery proposal (pending/approved/rejected)."""
    norm_status = status.strip().lower()
    if norm_status not in ("pending", "approved", "rejected"):
        raise ValueError(f"Invalid proposal status '{status}'. Must be pending, approved, or rejected.")
    now = utc_now()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE discovery_proposals SET status = ?, updated_at = ? WHERE id = ?",
            (norm_status, now, proposal_id),
        )
    res = get_discovery_proposal(db_path, proposal_id)
    if not res:
        raise KeyError(f"Proposal '{proposal_id}' not found")
    return res


def approve_discovery_proposal(
    db_path: Path | str,
    proposal_id: str,
    override_source: str | None = None,
    override_target: str | None = None,
    override_aliases: list[str] | str | None = None,
    override_gender: str | None = None,
    override_speech_style: str | None = None,
    override_notes: str | None = None,
    override_category: str | None = None,
) -> dict[str, Any]:
    """Approve a discovery proposal, creating/updating character or glossary without overwriting locked records."""
    prop = get_discovery_proposal(db_path, proposal_id)
    if not prop:
        raise KeyError(f"Proposal '{proposal_id}' not found")
    if prop.get("status") != "pending":
        raise ValueError(f"Đề xuất '{proposal_id}' đã được xử lý (trạng thái: {prop.get('status')}).")

    book_id = prop["book_id"]
    category = (override_category or prop["category"]).strip().upper()
    canonical_source = (override_source or prop["canonical_source"]).strip()
    canonical_target = (override_target or prop["canonical_target"]).strip()
    aliases = override_aliases if override_aliases is not None else prop.get("source_aliases", [])
    gender = (override_gender or prop.get("gender") or "unknown").strip().lower()
    speech_style = (override_speech_style or prop.get("speech_style") or "").strip()
    notes = (override_notes or prop.get("notes") or "").strip()

    if not canonical_source:
        raise ValueError("Tên nguồn không được để trống")
    if not canonical_target:
        raise ValueError("Tên dịch không được để trống")

    created_or_updated_entity: dict[str, Any]

    if category == "CHARACTER":
        with get_connection(db_path) as conn:
            existing_char = conn.execute(
                "SELECT id, locked, source_aliases FROM characters WHERE book_id = ? AND LOWER(TRIM(canonical_source)) = LOWER(?)",
                (book_id, canonical_source),
            ).fetchone()

        if existing_char:
            if bool(existing_char["locked"]):
                raise ValueError(f"Không thể ghi đè nhân vật '{canonical_source}' vì đã bị khóa.")
            existing_aliases = []
            try:
                existing_aliases = json.loads(existing_char["source_aliases"])
            except Exception:
                pass
            merged_aliases = list(existing_aliases)
            clean_new_aliases = _clean_aliases(aliases, canonical_source)
            for a in clean_new_aliases:
                if a not in merged_aliases:
                    merged_aliases.append(a)

            created_or_updated_entity = update_character(
                db_path,
                existing_char["id"],
                canonical_target=canonical_target,
                source_aliases=merged_aliases,
                gender=gender if gender != "unknown" else None,
                speech_style=speech_style or None,
                relationships=notes or None,
            )
        else:
            created_or_updated_entity = create_character(
                db_path,
                book_id=book_id,
                canonical_source=canonical_source,
                canonical_target=canonical_target,
                source_aliases=aliases,
                gender=gender,
                speech_style=speech_style,
                relationships=notes,
                first_occurrence_chapter_id=prop.get("first_occurrence_chapter_id"),
                first_occurrence_chapter_index=prop.get("first_occurrence_chapter_index"),
                locked=False,
            )
    else:
        with get_connection(db_path) as conn:
            existing_gloss = conn.execute(
                "SELECT id, locked FROM glossary WHERE book_id = ? AND LOWER(TRIM(source)) = LOWER(?)",
                (book_id, canonical_source),
            ).fetchone()

        notes_combined = notes
        if category in ("PLACE", "ORGANIZATION"):
            prefix = f"[{category}] "
            if not notes_combined.startswith(prefix):
                notes_combined = f"{prefix}{notes_combined}".strip()

        if existing_gloss:
            if bool(existing_gloss["locked"]):
                raise ValueError(f"Không thể ghi đè thuật ngữ '{canonical_source}' vì đã bị khóa.")
            created_or_updated_entity = update_glossary_item(
                db_path,
                existing_gloss["id"],
                target=canonical_target,
                notes=notes_combined or None,
            )
        else:
            created_or_updated_entity = create_glossary_item(
                db_path,
                book_id=book_id,
                source=canonical_source,
                target=canonical_target,
                locked=False,
                notes=notes_combined,
            )

    update_discovery_proposal_status(db_path, proposal_id, "approved")
    updated_prop = get_discovery_proposal(db_path, proposal_id)
    return {
        "status": "approved",
        "proposal": updated_prop,
        "entity": created_or_updated_entity,
        "category": category,
    }


def reject_discovery_proposal(db_path: Path | str, proposal_id: str) -> dict[str, Any]:
    """Reject a discovery proposal without modifying characters, glossary, or translations."""
    prop = get_discovery_proposal(db_path, proposal_id)
    if not prop:
        raise KeyError(f"Proposal '{proposal_id}' not found")
    if prop.get("status") != "pending":
        raise ValueError(f"Đề xuất '{proposal_id}' đã được xử lý (trạng thái: {prop.get('status')}).")
    update_discovery_proposal_status(db_path, proposal_id, "rejected")
    updated_prop = get_discovery_proposal(db_path, proposal_id)
    return {
        "status": "rejected",
        "proposal": updated_prop,
    }


# -----------------------------------------------------------------------------
# Book-Scoped Prompt Presets and Few-Shot Examples
# -----------------------------------------------------------------------------

DEFAULT_TRANSLATION_SYSTEM_PROMPT = (
    "Bạn là chuyên gia dịch thuật văn học chuyên nghiệp từ tiếng Hàn sang tiếng Việt (sản phẩm Trạm Dịch).\n"
    "Nhiệm vụ: Dịch chính xác từng phân đoạn văn bản được cung cấp theo đúng các nguyên tắc sau:\n"
    "1. ĐẦY ĐỦ VÀ TRUNG THỰC: Dịch trọn vẹn 100% nội dung. Tuyệt đối KHÔNG tự ý thêm thắt tình tiết, KHÔNG lược bỏ bất kỳ câu chữ nào.\n"
    "2. VĂN PHONG TIẾNG VIỆT TỰ NHIÊN: Lời văn mượt mà, đúng chuẩn ngữ pháp và văn phong tiểu thuyết tiếng Việt hiện đại.\n"
    "3. NHẤT QUÁN TÊN RIÊNG VÀ THUẬT NGỮ: Giữ tính nhất quán tự nhiên của tên nhân vật, địa danh, chiêu thức, tổ chức dựa theo diễn biến của chương truyện và bối cảnh (Story Bible).\n"
    "4. XƯNG HÔ VÀ GIỌNG ĐIỆU: Giữ đúng ngôi kể, giọng điệu nhân vật (lạnh lùng, kính cẩn, thân mật, hài hước...), quan hệ xã hội theo ngữ cảnh câu chuyện.\n"
    "5. THUẬT NGỮ HỆ THỐNG / KÝ HIỆU: Giữ nguyên các ký hiệu ngoặc vuông [...], ngoặc nhọn <...>, thông số hệ thống, thanh trạng thái hoặc các nhãn game/apocalypse.\n"
    "6. CẤU TRÚC PHÂN ĐOẠN: Mỗi phân đoạn có một ID duy nhất. Chỉ sao chép chính xác giá trị thuộc tính 'id' từ danh sách mục tiêu JSON (copy only id property value), kết quả trả về phải chứa đầy đủ và chính xác các ID này, không được thiếu, thừa hay trùng lặp."
)

DEFAULT_TRANSLATION_USER_TEMPLATE = (
    "{story_bible}\n"
    "{recent_summaries}\n"
    "{prior_titles}\n"
    "{characters}\n"
    "{glossary}\n"
    "{few_shots}\n"
    "{neighboring_before}\n"
    "{batch_targets}\n"
    "{neighboring_after}\n"
    "Hãy dịch toàn bộ danh sách 'CÁC PHÂN ĐOẠN CẦN DỊCH' ở trên sang tiếng Việt và trả về định dạng JSON theo schema đã quy định."
)

DEFAULT_REVIEW_SYSTEM_PROMPT = (
    "Bạn là biên tập viên văn học tiếng Việt cao cấp (Reviewer Trạm Dịch).\n"
    "Nhiệm vụ: Thẩm định bản dịch tiếng Hàn sang tiếng Việt, phát hiện câu văn chưa tự nhiên, "
    "dịch thiếu, dịch thừa, sai đại từ nhân xưng hoặc không nhất quán về xưng hô, tên nhân vật. "
    "Đảm bảo tên riêng và thuật ngữ nhất quán tự nhiên theo ngữ cảnh chương truyện, Story Bible, "
    "bảng ánh xạ tiêu đề chuẩn và từ điển thuật ngữ. "
    "Đưa ra danh sách issues và corrections chuẩn xác."
)

DEFAULT_REVIEW_USER_TEMPLATE = (
    "{story_bible}\n"
    "{recent_summaries}\n"
    "{prior_titles}\n"
    "{characters}\n"
    "{glossary}\n"
    "{neighboring_before}\n"
    "{batch_review_targets}\n"
    "{neighboring_after}"
)


def _normalize_preset_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Convert DB row to typed preset dictionary."""
    d = dict(row)
    d["is_default"] = bool(d.get("is_default", 0))
    raw_few = d.get("few_shot_examples")
    if isinstance(raw_few, str):
        try:
            d["few_shot_examples"] = json.loads(raw_few)
        except Exception:
            d["few_shot_examples"] = []
    elif not isinstance(raw_few, list):
        d["few_shot_examples"] = []
    return d


def list_prompt_presets(db_path: Path | str, book_id: str) -> list[dict[str, Any]]:
    """List all prompt presets for a book, seeding default if none exist."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, book_id, name, description, system_prompt, user_prompt_template,
                   review_system_prompt, review_user_prompt_template, few_shot_examples,
                   is_default, created_at, updated_at
            FROM prompt_presets
            WHERE book_id = ?
            ORDER BY is_default DESC, created_at ASC
            """,
            (book_id,),
        ).fetchall()
        if rows:
            return [_normalize_preset_row(r) for r in rows]

    # Auto-seed standard default preset for this book if none exist
    default_preset = create_prompt_preset(
        db_path,
        book_id=book_id,
        name="Tiêu chuẩn Tiếng Việt (Mặc định)",
        description="Dịch văn học trung thực, tự nhiên, bảo toàn tên riêng, xưng hô và cấu trúc phân đoạn.",
        system_prompt=DEFAULT_TRANSLATION_SYSTEM_PROMPT,
        user_prompt_template=DEFAULT_TRANSLATION_USER_TEMPLATE,
        review_system_prompt=DEFAULT_REVIEW_SYSTEM_PROMPT,
        review_user_prompt_template=DEFAULT_REVIEW_USER_TEMPLATE,
        few_shot_examples=[],
        is_default=True,
    )
    return [default_preset]


def get_prompt_preset(db_path: Path | str, preset_id: str) -> dict[str, Any] | None:
    """Retrieve single prompt preset by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, book_id, name, description, system_prompt, user_prompt_template,
                   review_system_prompt, review_user_prompt_template, few_shot_examples,
                   is_default, created_at, updated_at
            FROM prompt_presets
            WHERE id = ?
            """,
            (preset_id,),
        ).fetchone()
        return _normalize_preset_row(row) if row else None


def get_default_prompt_preset(db_path: Path | str, book_id: str) -> dict[str, Any]:
    """Retrieve the active default prompt preset for a book, seeding if needed."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, book_id, name, description, system_prompt, user_prompt_template,
                   review_system_prompt, review_user_prompt_template, few_shot_examples,
                   is_default, created_at, updated_at
            FROM prompt_presets
            WHERE book_id = ? AND is_default = 1
            LIMIT 1
            """,
            (book_id,),
        ).fetchone()
        if row:
            return _normalize_preset_row(row)

    presets = list_prompt_presets(db_path, book_id)
    return presets[0]


def create_prompt_preset(
    db_path: Path | str,
    book_id: str,
    name: str,
    description: str = "",
    system_prompt: str | None = None,
    user_prompt_template: str | None = None,
    review_system_prompt: str | None = None,
    review_user_prompt_template: str | None = None,
    few_shot_examples: list[dict[str, Any]] | None = None,
    is_default: bool = False,
) -> dict[str, Any]:
    """Create a new book-scoped prompt preset."""
    name_clean = (name or "").strip()
    if not name_clean:
        raise ValueError("Tên cấu hình prompt không được để trống")

    now = utc_now()
    preset_id = uuid.uuid4().hex
    eff_sys = (system_prompt or "").strip() or DEFAULT_TRANSLATION_SYSTEM_PROMPT
    eff_user = (user_prompt_template or "").strip() or DEFAULT_TRANSLATION_USER_TEMPLATE
    eff_rev_sys = (review_system_prompt or "").strip() or DEFAULT_REVIEW_SYSTEM_PROMPT
    eff_rev_user = (review_user_prompt_template or "").strip() or DEFAULT_REVIEW_USER_TEMPLATE
    few_json = json.dumps(few_shot_examples or [], ensure_ascii=False)

    with get_connection(db_path) as conn:
        dup = conn.execute(
            "SELECT id FROM prompt_presets WHERE book_id = ? AND LOWER(TRIM(name)) = LOWER(?)",
            (book_id, name_clean),
        ).fetchone()
        if dup:
            raise ValueError(f"Cấu hình prompt '{name_clean}' đã tồn tại trong danh sách.")

        # Check if first preset for book
        existing_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM prompt_presets WHERE book_id = ?",
            (book_id,),
        ).fetchone()["cnt"]

        make_default = is_default or (existing_count == 0)
        if make_default:
            conn.execute(
                "UPDATE prompt_presets SET is_default = 0 WHERE book_id = ?",
                (book_id,),
            )

        conn.execute(
            """
            INSERT INTO prompt_presets (
                id, book_id, name, description, system_prompt, user_prompt_template,
                review_system_prompt, review_user_prompt_template, few_shot_examples,
                is_default, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                preset_id,
                book_id,
                name_clean,
                description.strip(),
                eff_sys,
                eff_user,
                eff_rev_sys,
                eff_rev_user,
                few_json,
                1 if make_default else 0,
                now,
                now,
            ),
        )

    res = get_prompt_preset(db_path, preset_id)
    if not res:
        raise RuntimeError("Failed to retrieve created prompt preset")
    return res


def update_prompt_preset(
    db_path: Path | str,
    preset_id: str,
    name: str | None = None,
    description: str | None = None,
    system_prompt: str | None = None,
    user_prompt_template: str | None = None,
    review_system_prompt: str | None = None,
    review_user_prompt_template: str | None = None,
    few_shot_examples: list[dict[str, Any]] | None = None,
    is_default: bool | None = None,
) -> dict[str, Any]:
    """Update an existing prompt preset."""
    current = get_prompt_preset(db_path, preset_id)
    if not current:
        raise KeyError(f"Prompt preset '{preset_id}' not found")

    book_id = current["book_id"]
    now = utc_now()
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [now]

    if name is not None:
        name_clean = name.strip()
        if not name_clean:
            raise ValueError("Tên cấu hình prompt không được để trống")
        with get_connection(db_path) as conn:
            dup = conn.execute(
                "SELECT id FROM prompt_presets WHERE book_id = ? AND id != ? AND LOWER(TRIM(name)) = LOWER(?)",
                (book_id, preset_id, name_clean),
            ).fetchone()
            if dup:
                raise ValueError(f"Cấu hình prompt '{name_clean}' đã tồn tại trong danh sách.")
        fields.append("name = ?")
        values.append(name_clean)

    if description is not None:
        fields.append("description = ?")
        values.append(description.strip())

    if system_prompt is not None:
        fields.append("system_prompt = ?")
        values.append(system_prompt.strip())

    if user_prompt_template is not None:
        fields.append("user_prompt_template = ?")
        values.append(user_prompt_template.strip())

    if review_system_prompt is not None:
        fields.append("review_system_prompt = ?")
        values.append(review_system_prompt.strip())

    if review_user_prompt_template is not None:
        fields.append("review_user_prompt_template = ?")
        values.append(review_user_prompt_template.strip())

    if few_shot_examples is not None:
        fields.append("few_shot_examples = ?")
        values.append(json.dumps(few_shot_examples, ensure_ascii=False))

    with get_connection(db_path) as conn:
        if is_default is not None:
            if is_default:
                conn.execute(
                    "UPDATE prompt_presets SET is_default = 0 WHERE book_id = ?",
                    (book_id,),
                )
                fields.append("is_default = 1")
            else:
                fields.append("is_default = 0")
                if current["is_default"]:
                    # Promote another preset to default so book always has one default
                    next_preset = conn.execute(
                        "SELECT id FROM prompt_presets WHERE book_id = ? AND id != ? ORDER BY created_at ASC LIMIT 1",
                        (book_id, preset_id),
                    ).fetchone()
                    if next_preset:
                        conn.execute(
                            "UPDATE prompt_presets SET is_default = 1 WHERE id = ?",
                            (next_preset["id"],),
                        )
                    else:
                        # Sole preset cannot be un-defaulted
                        fields.pop()
                        fields.append("is_default = 1")

        values.append(preset_id)
        conn.execute(f"UPDATE prompt_presets SET {', '.join(fields)} WHERE id = ?", values)

    res = get_prompt_preset(db_path, preset_id)
    if not res:
        raise KeyError(f"Prompt preset '{preset_id}' not found after update")
    return res


def delete_prompt_preset(db_path: Path | str, preset_id: str) -> bool:
    """Delete a prompt preset, promoting another to default if deleting the default."""
    current = get_prompt_preset(db_path, preset_id)
    if not current:
        raise KeyError(f"Prompt preset '{preset_id}' not found")

    book_id = current["book_id"]
    was_default = current["is_default"]

    with get_connection(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM prompt_presets WHERE book_id = ?",
            (book_id,),
        ).fetchone()["cnt"]

        if count <= 1:
            raise ValueError("Không thể xóa cấu hình prompt duy nhất của sách.")

        conn.execute("DELETE FROM prompt_presets WHERE id = ?", (preset_id,))

        if was_default:
            # Promote another preset to default
            next_preset = conn.execute(
                "SELECT id FROM prompt_presets WHERE book_id = ? ORDER BY created_at ASC LIMIT 1",
                (book_id,),
            ).fetchone()
            if next_preset:
                conn.execute(
                    "UPDATE prompt_presets SET is_default = 1 WHERE id = ?",
                    (next_preset["id"],),
                )
    return True


def set_default_prompt_preset(db_path: Path | str, preset_id: str) -> dict[str, Any]:
    """Set the specified prompt preset as the default for its book."""
    current = get_prompt_preset(db_path, preset_id)
    if not current:
        raise KeyError(f"Prompt preset '{preset_id}' not found")
    book_id = current["book_id"]

    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE prompt_presets SET is_default = 0 WHERE book_id = ?",
            (book_id,),
        )
        conn.execute(
            "UPDATE prompt_presets SET is_default = 1, updated_at = ? WHERE id = ?",
            (utc_now(), preset_id),
        )

    return get_prompt_preset(db_path, preset_id) or current

