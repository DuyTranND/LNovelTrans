"""Tests for Railway hosting configuration, Dockerfile invariants, and CI workflow."""

from __future__ import annotations

import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.config import get_settings


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_required_fonts_exist_for_export() -> None:
    """Verify fonts required by PDF export and Docker image build exist."""
    fonts_dir = PROJECT_ROOT / "fonts"
    assert fonts_dir.is_dir(), "fonts/ directory must exist in project root"

    regular_font = fonts_dir / "NotoSans-Regular.ttf"
    korean_font = fonts_dir / "NotoSansKR-Regular.ttf"

    assert regular_font.is_file(), "fonts/NotoSans-Regular.ttf must exist for Vietnamese/Latin export"
    assert korean_font.is_file(), "fonts/NotoSansKR-Regular.ttf must exist for Korean export"
    assert regular_font.stat().st_size > 0, "NotoSans-Regular.ttf must not be empty"
    assert korean_font.stat().st_size > 0, "NotoSansKR-Regular.ttf must not be empty"


def test_dockerfile_security_and_startup_invariants() -> None:
    """Verify Dockerfile enforces single uvicorn worker, privilege drop, and safe permissions."""
    dockerfile_path = PROJECT_ROOT / "Dockerfile"
    assert dockerfile_path.is_file(), "Dockerfile must exist"

    content = dockerfile_path.read_text(encoding="utf-8")

    # Security check: absolutely avoid chmod 777
    assert "chmod 777" not in content, "Dockerfile must not use chmod 777"
    assert "chmod -R 777" not in content, "Dockerfile must not use chmod -R 777"

    # Runtime package: gosu installed to safely drop root privileges
    assert "gosu" in content, "Dockerfile must install gosu to drop privileges to non-root UID"

    # Volume ownership fix & privilege drop to translator (UID 1000)
    assert "chown translator:translator" in content, "Dockerfile must ensure volume is owned by translator"
    assert "gosu translator" in content, "Dockerfile entrypoint must drop privileges to translator user"

    # Single uvicorn worker invariant for SQLite single-writer safety
    assert "--workers 1" in content, "Dockerfile must explicitly run ONE uvicorn worker for SQLite WAL safety"

    # Fonts copying check
    assert "COPY fonts/ ./fonts/" in content, "Dockerfile must copy fonts/ for export rendering"

    # Railway dynamic PORT binding check
    assert "${PORT:-8000}" in content or "$PORT" in content, "Dockerfile must bind to dynamic PORT"

    # No hardcoded secrets
    assert not re.search(r"AIza[0-9A-Za-z-_]{35}", content), "Dockerfile must not contain hardcoded Gemini API keys"


def test_ci_workflow_invariants() -> None:
    """Verify GitHub Actions CI workflow matches required toolchains and constraints."""
    ci_path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file(), ".github/workflows/ci.yml must exist"

    ci_content = ci_path.read_text(encoding="utf-8")

    # Python 3.12 backend testing
    assert "3.12" in ci_content, "CI must test Python 3.12"
    assert "pytest backend/tests" in ci_content, "CI must execute backend pytest suite"
    assert "--basetemp=" in ci_content, "CI must use isolated tmp directory for pytest"

    # Node 22 & pnpm 11.19 frontend testing and building
    assert "22" in ci_content, "CI must use Node.js 22"
    assert "11.19" in ci_content, "CI must use pnpm 11.19"
    assert "--frozen-lockfile" in ci_content, "CI must enforce frozen-lockfile for frontend installation"
    assert "pnpm test" in ci_content, "CI must run frontend test suite"
    assert "pnpm build" in ci_content, "CI must build frontend production bundle"

    # Docker image build
    assert "docker build" in ci_content, "CI must build Docker image"

    # Sensible job timeouts
    assert "timeout-minutes" in ci_content, "CI jobs must have timeout-minutes configured"

    # No real secrets or live keys in CI file
    assert not re.search(r"AIza[0-9A-Za-z-_]{35}", ci_content), "CI workflow must not contain live API keys"


def test_health_endpoint_contract_and_no_leak(tmp_path: Path) -> None:
    """Verify GET /api/health returns 200 without leaking credentials or environment secrets."""
    app = create_app(data_dir=tmp_path, start_worker=False)
    client = TestClient(app)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()

    assert data.get("status") == "ok"
    # Ensure no credentials or raw secrets are present in health payload
    for key in data.keys():
        assert "key" not in key.lower() or key in ("key_configured", "gemini_configured")
        assert "secret" not in key.lower()
        assert "token" not in key.lower()


def test_data_dir_isolation_and_sqlite_creation(tmp_path: Path) -> None:
    """Verify application correctly isolates data_dir to the provided directory."""
    test_dir = tmp_path / "isolated_data"
    settings = get_settings(data_dir=test_dir)
    assert settings.data_dir == test_dir.resolve()
    assert test_dir.is_dir()

    app = create_app(data_dir=test_dir, start_worker=False)
    db_file = test_dir / "translator.db"
    assert db_file.is_file(), "translator.db should be created inside isolated data_dir"
