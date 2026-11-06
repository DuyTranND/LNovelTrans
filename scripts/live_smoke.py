"""Explicit, small Gemini translation smoke test. Never translates a full book."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import get_settings
from backend.app.epub import parse_epub
from backend.app.provider import GeminiProvider, sanitize_secret


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", nargs="?", type=Path)
    parser.add_argument("--model", default=get_settings().default_model)
    parser.add_argument(
        "--review",
        action="store_true",
        help="Run one translation and then one review on the same segments.",
    )
    args = parser.parse_args()

    sample = args.epub or next((ROOT.parent / "Comic").glob("*.epub"), None)
    if not sample:
        raise SystemExit("Provide a sample EPUB path.")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise SystemExit("GEMINI_API_KEY is not configured.")

    chapter = parse_epub(sample).spine_chapters[0]
    selected = [
        s for s in chapter.segments if s.text.strip() and s.parent_tag != "title"
    ][:3]
    segments = [{"id": s.segment_id, "source": s.text} for s in selected]
    req_ids = {s["id"] for s in segments}

    provider = GeminiProvider(key, model=args.model, max_retries=0, timeout=45)
    try:
        # Pass 1: Translation pass
        trans_result = await provider.translate(segments, model=args.model)
        if not isinstance(trans_result, dict):
            raise ValueError("Bản dịch trả về không phải là dictionary.")
        if set(trans_result.keys()) != req_ids:
            missing = req_ids - set(trans_result.keys())
            extra = set(trans_result.keys()) - req_ids
            raise ValueError(
                f"Translation ID mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
            )

        # Pass 2: Optional review pass on the SAME segments
        rev_result: dict[str, Any] | None = None
        if args.review:
            review_input = [
                {
                    "id": s["id"],
                    "source": s["source"],
                    "translation": trans_result.get(s["id"], ""),
                }
                for s in segments
            ]
            rev_result = await provider.review(review_input, model=args.model)
            if not isinstance(rev_result, dict):
                raise ValueError("Kết quả thẩm định không hợp lệ.")
            corrections = rev_result.get("corrections", {})
            if not isinstance(corrections, dict):
                raise ValueError("Review corrections phải là dictionary.")
            for cid in corrections.keys():
                if cid not in req_ids:
                    raise ValueError(f"Review correction ID '{cid}' không thuộc danh sách yêu cầu.")
            for iss in rev_result.get("issues", []):
                if isinstance(iss, dict) and iss.get("id"):
                    if str(iss["id"]) not in req_ids:
                        raise ValueError(f"Review issue ID '{iss['id']}' không thuộc danh sách yêu cầu.")

        destination = get_settings().data_dir / "live-smoke.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        artifact_payload: dict[str, Any] = {
            "model": args.model,
            "source": segments,
            "translations": trans_result,
        }
        if args.review and rev_result is not None:
            artifact_payload["review"] = rev_result

        destination.write_text(
            json.dumps(artifact_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        summary: dict[str, Any] = {
            "status": "success",
            "model": args.model,
            "segments": len(trans_result),
            "output": str(destination),
        }
        if args.review and rev_result is not None:
            summary["review_issues"] = len(rev_result.get("issues", []))
            summary["review_corrections"] = len(rev_result.get("corrections", {}))

        print(json.dumps(summary, ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": sanitize_secret(str(exc), key)},
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        await provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
