from __future__ import annotations

import hashlib
import json
from typing import Any
from backend.app.db import get_connection


def memory_key(book_id: str, chapter_id: str, model: str, review: bool, context: Any) -> str:
    payload = [book_id, chapter_id, model, review, context]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_memory(db_path: Any, key: str) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        row = conn.execute('SELECT payload FROM translation_memory WHERE context_key = ?', (key,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row['payload'])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_memory(conn: Any, key: str, translations: Any, issues: Any) -> None:
    conn.execute('INSERT OR REPLACE INTO translation_memory(context_key, payload) VALUES (?, ?)',
                 (key, json.dumps({'translations': translations, 'issues': issues}, ensure_ascii=False)))


def review_memory_key(
    book_id: str,
    chapter_id: str,
    model: str,
    review_inputs: list[dict[str, Any]],
    context: Any = None,
) -> str:
    """Distinct versioned review-only cache key.

    Includes version namespace, book/chapter/model, exact ordered review inputs
    {id, source, translation}, and context.
    """
    ordered_inputs = [
        {
            "id": str(item.get("id", "")),
            "source": str(item.get("source", "")),
            "translation": str(item.get("translation", "")),
        }
        for item in review_inputs
    ]
    payload = [
        "review_memory_v2",
        book_id,
        chapter_id,
        model,
        ordered_inputs,
        context,
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def save_review_memory(
    conn: Any,
    key: str,
    corrections: dict[str, str],
    issues: dict[str, list[dict[str, Any]]],
) -> None:
    """Persist validated review output under distinct review-only versioned payload."""
    payload = {
        "kind": "review",
        "version": 2,
        "corrections": corrections,
        "issues": issues,
    }
    conn.execute(
        "INSERT OR REPLACE INTO translation_memory(context_key, payload) VALUES (?, ?)",
        (key, json.dumps(payload, ensure_ascii=False)),
    )


def load_review_memory(db_path: Any, key: str) -> dict[str, Any] | None:
    """Load cached review output, strictly rejecting legacy combined or unversioned entries."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT payload FROM translation_memory WHERE context_key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # Reject legacy combined cache without versioned review namespace
    if data.get("kind") != "review" or data.get("version") != 2:
        return None
    # Require both corrections and issues to be present and be dictionaries
    if not isinstance(data.get("corrections"), dict) or not isinstance(data.get("issues"), dict):
        return None
    return data


