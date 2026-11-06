"""Configuration settings for Trạm Dịch."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel

# Load project-root .env without overriding existing environment variables
_project_root = Path(__file__).resolve().parent.parent.parent
_project_env = _project_root / ".env"
load_dotenv(_project_env, override=False)


def get_gemini_api_keys() -> tuple[str, str | None, str | None]:
    """Resolve Gemini keys from one source, honoring the project's .env first.

    When a local .env configures Gemini, omitted/empty secondary or tertiary entries
    intentionally disable those fallback keys. This prevents inherited stale
    process variables from enabling an unexpected fallback credential.
    Environment-only deployments continue to work without a Gemini .env entry.
    Duplicates are deduplicated so all returned keys are unique.
    """
    local_values = dotenv_values(_project_env) if _project_env.is_file() else {}
    gemini_env_present = any(
        k in local_values
        for k in (
            "GEMINI_API_KEY",
            "GEMINI_API_KEY_SECONDARY",
            "GEMINI_API_KEY_2",
            "GEMINI_API_KEY_TERTIARY",
            "GEMINI_API_KEY_3",
        )
    )
    if gemini_env_present:
        primary = local_values.get("GEMINI_API_KEY")
        if primary is None and "GEMINI_API_KEY" not in local_values:
            primary = os.environ.get("GEMINI_API_KEY", "")
        secondary = local_values.get("GEMINI_API_KEY_SECONDARY") or local_values.get("GEMINI_API_KEY_2")
        tertiary = local_values.get("GEMINI_API_KEY_TERTIARY") or local_values.get("GEMINI_API_KEY_3")
    else:
        primary = os.environ.get("GEMINI_API_KEY", "")
        secondary = os.environ.get("GEMINI_API_KEY_SECONDARY") or os.environ.get("GEMINI_API_KEY_2", "")
        tertiary = os.environ.get("GEMINI_API_KEY_TERTIARY") or os.environ.get("GEMINI_API_KEY_3", "")

    p_clean = (primary or "").strip()
    s_clean = (secondary or "").strip() or None
    t_clean = (tertiary or "").strip() or None

    if s_clean and s_clean == p_clean:
        s_clean = None
    if t_clean and (t_clean == p_clean or t_clean == s_clean):
        t_clean = None

    return p_clean, s_clean, t_clean

DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR", str(_project_root / "data"))).resolve()
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", os.environ.get("DEFAULT_MODEL", "gemini-3.5-flash-lite"))


class AppSettings(BaseModel):
    data_dir: Path
    default_model: str
    key_configured: bool
    gemini_configured: bool = False


def get_settings(data_dir: str | Path | None = None) -> AppSettings:
    """Retrieve app settings, checking if API keys are present without exposing them."""
    resolved_dir = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    resolved_dir.mkdir(parents=True, exist_ok=True)
    gemini_configured = bool(get_gemini_api_keys()[0])
    return AppSettings(
        data_dir=resolved_dir,
        default_model=DEFAULT_MODEL,
        key_configured=gemini_configured,
        gemini_configured=gemini_configured,
    )

