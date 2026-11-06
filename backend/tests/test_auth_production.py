"""Unit tests for production authentication configuration hardening.

Tests verify strict validation of AUTH_USERS and AUTH_ADMIN_PASSWORD_HASH
in production (APP_ENV=production) while preserving development fixture behavior.
Includes application startup tests via create_app verifying fail-fast behavior
before database creation.
All tests use isolated mock data and environment monkeypatching; no real credentials,
keys, or external services are accessed.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend.app.auth import (
    AuthUser,
    UserRole,
    get_configured_users,
    hash_password,
    validate_password_hash,
)
from backend.app.main import create_app

# Helper to construct valid PBKDF2 test hashes
VALID_SALT = "a1b2c3d4e5f60718"
VALID_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
VALID_HASH = f"pbkdf2_sha256$100000${VALID_SALT}${VALID_DIGEST}"


# ---------------------------------------------------------------------------
# 1. validate_password_hash tests
# ---------------------------------------------------------------------------

def test_validate_password_hash_valid():
    """Valid PBKDF2 hashes within iteration bounds are accepted."""
    generated = hash_password("TestSecretPass123!")
    validate_password_hash(generated)

    # Lower bound (100,000)
    validate_password_hash(f"pbkdf2_sha256$100000${VALID_SALT}${VALID_DIGEST}")

    # Mid range (500,000)
    validate_password_hash(f"pbkdf2_sha256$500000${VALID_SALT}${VALID_DIGEST}")

    # Upper bound (1,000,000)
    validate_password_hash(f"pbkdf2_sha256$1000000${VALID_SALT}${VALID_DIGEST}")


@pytest.mark.parametrize(
    "invalid_hash, match_pattern",
    [
        ("", "non-empty string"),
        ("   ", "non-empty string"),
        ("plain_password_string", "pbkdf2_sha256"),
        (f"pbkdf2_sha512$100000${VALID_SALT}${VALID_DIGEST}", "pbkdf2_sha256"),
        (f"bcrypt$100000${VALID_SALT}${VALID_DIGEST}", "pbkdf2_sha256"),
        (f"pbkdf2_sha256$100000${VALID_SALT}", "pbkdf2_sha256"),  # missing digest
        (f"pbkdf2_sha256$100000${VALID_SALT}${VALID_DIGEST}$extra", "pbkdf2_sha256"),
        (f"pbkdf2_sha256$abc${VALID_SALT}${VALID_DIGEST}", "valid integer"),
        (f"pbkdf2_sha256$99999${VALID_SALT}${VALID_DIGEST}", "bounded between"),
        (f"pbkdf2_sha256$1000${VALID_SALT}${VALID_DIGEST}", "bounded between"),
        (f"pbkdf2_sha256$1000001${VALID_SALT}${VALID_DIGEST}", "bounded between"),
        (f"pbkdf2_sha256$2000000${VALID_SALT}${VALID_DIGEST}", "bounded between"),
        (f"pbkdf2_sha256$100000$${VALID_DIGEST}", "salt"),  # empty salt
        (f"pbkdf2_sha256$100000$   ${VALID_DIGEST}", "salt"),  # whitespace salt
        (f"pbkdf2_sha256$100000$ab${VALID_DIGEST}", "salt"),  # salt < 4 chars
        (f"pbkdf2_sha256$100000${'s' * 129}${VALID_DIGEST}", "salt"),  # salt > 128 chars
        (f"pbkdf2_sha256$100000${VALID_SALT}$12345", "64-character hexadecimal"),  # digest too short
        (f"pbkdf2_sha256$100000${VALID_SALT}${VALID_DIGEST}00", "64-character hexadecimal"),  # digest too long
        (f"pbkdf2_sha256$100000${VALID_SALT}${'z' * 64}", "64-character hexadecimal"),  # non-hex chars
    ],
)
def test_validate_password_hash_rejections(invalid_hash: str, match_pattern: str):
    """Malformed or out-of-bounds password hashes are rejected with ValueError."""
    with pytest.raises(ValueError, match=match_pattern):
        validate_password_hash(invalid_hash)


# ---------------------------------------------------------------------------
# 2. Production get_configured_users rejection tests (APP_ENV=production)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    """Ensure clean auth environment variables for every test."""
    monkeypatch.delenv("AUTH_USERS", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("AUTH_OVERRIDE_LOCAL_ONLY", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "super_secret_session_key_32_characters_minimum!")


def test_prod_rejects_malformed_json_root(monkeypatch):
    """Production must reject non-JSON or malformed JSON root."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_USERS", "{not-valid-json}")

    with pytest.raises(ValueError, match="Malformed AUTH_USERS JSON"):
        get_configured_users()


def test_prod_rejects_explicit_json_null(monkeypatch):
    """Production must reject explicitly supplied JSON null as AUTH_USERS."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_USERS", "null")

    with pytest.raises(ValueError, match="root cannot be null"):
        get_configured_users()


def test_prod_rejects_empty_json_list(monkeypatch):
    """Production must reject explicitly supplied empty JSON list [] as AUTH_USERS."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_USERS", "[]")

    with pytest.raises(ValueError, match="list cannot be empty"):
        get_configured_users()


def test_prod_rejects_non_list_root(monkeypatch):
    """Production must reject JSON root if it is a dictionary, string, or number."""
    monkeypatch.setenv("APP_ENV", "production")

    monkeypatch.setenv("AUTH_USERS", json.dumps({"username": "alice"}))
    with pytest.raises(ValueError, match="must be a JSON list"):
        get_configured_users()

    monkeypatch.setenv("AUTH_USERS", json.dumps("string_root"))
    with pytest.raises(ValueError, match="must be a JSON list"):
        get_configured_users()

    monkeypatch.setenv("AUTH_USERS", json.dumps(12345))
    with pytest.raises(ValueError, match="must be a JSON list"):
        get_configured_users()


def test_prod_rejects_malformed_entry(monkeypatch):
    """Production must reject list entries that are not dictionaries."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_USERS", json.dumps(["not_a_dict"]))

    with pytest.raises(ValueError, match="must be a dictionary"):
        get_configured_users()


def test_prod_rejects_empty_or_non_string_username(monkeypatch):
    """Production must reject entries with non-string, missing, empty, or whitespace username."""
    monkeypatch.setenv("APP_ENV", "production")

    # Missing username key
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"role": "viewer", "password_hash": VALID_HASH}]))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        get_configured_users()

    # Non-string username (integer)
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": 12345, "password_hash": VALID_HASH}]))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        get_configured_users()

    # Non-string username (null)
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": None, "password_hash": VALID_HASH}]))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        get_configured_users()

    # Non-string username (list)
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": ["user"], "password_hash": VALID_HASH}]))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        get_configured_users()

    # Empty string username
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": "", "password_hash": VALID_HASH}]))
    with pytest.raises(ValueError, match="empty username"):
        get_configured_users()

    # Whitespace username
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": "   ", "password_hash": VALID_HASH}]))
    with pytest.raises(ValueError, match="empty username"):
        get_configured_users()


def test_prod_rejects_duplicate_username_case_insensitive(monkeypatch):
    """Production must reject duplicate usernames even if casing differs."""
    monkeypatch.setenv("APP_ENV", "production")
    users = [
        {"username": "Alice", "role": "viewer", "password_hash": VALID_HASH},
        {"username": "alice", "role": "editor", "password_hash": VALID_HASH},
    ]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users))

    with pytest.raises(ValueError, match="Duplicate username 'alice'"):
        get_configured_users()


def test_prod_rejects_invalid_and_explicit_null_role(monkeypatch):
    """Production must reject invalid and explicit null roles instead of falling back to viewer."""
    monkeypatch.setenv("APP_ENV", "production")

    # Explicit null role
    users_null = [{"username": "alice", "role": None, "password_hash": VALID_HASH}]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_null))
    with pytest.raises(ValueError, match="role must be a non-empty string"):
        get_configured_users()

    # Non-string role (integer)
    users_int = [{"username": "alice", "role": 99, "password_hash": VALID_HASH}]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_int))
    with pytest.raises(ValueError, match="role must be a non-empty string"):
        get_configured_users()

    # Invalid string role
    users_invalid = [{"username": "alice", "role": "superadmin", "password_hash": VALID_HASH}]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_invalid))
    with pytest.raises(ValueError, match="Invalid role 'superadmin'"):
        get_configured_users()


def test_prod_missing_role_defaults_to_viewer(monkeypatch):
    """Omitted role attribute safely defaults to viewer in production."""
    monkeypatch.setenv("APP_ENV", "production")
    users_data = [{"username": "alice", "password_hash": VALID_HASH}]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_data))

    users = get_configured_users()
    assert "alice" in users
    assert users["alice"].role == UserRole.VIEWER


def test_prod_rejects_empty_or_malformed_password_hash(monkeypatch):
    """Production must reject empty, missing, or malformed password_hash in AUTH_USERS."""
    monkeypatch.setenv("APP_ENV", "production")

    # Missing password_hash
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": "alice", "role": "viewer"}]))
    with pytest.raises(ValueError, match="Missing password_hash"):
        get_configured_users()

    # Empty password_hash
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": "alice", "role": "viewer", "password_hash": ""}]))
    with pytest.raises(ValueError, match="Missing password_hash"):
        get_configured_users()

    # Malformed password_hash
    monkeypatch.setenv(
        "AUTH_USERS",
        json.dumps([{"username": "alice", "role": "viewer", "password_hash": "plain_password"}]),
    )
    with pytest.raises(ValueError, match="pbkdf2_sha256"):
        get_configured_users()


def test_prod_rejects_plaintext_password(monkeypatch):
    """Production must reject 'password' attribute in AUTH_USERS and AUTH_ADMIN_PASSWORD."""
    monkeypatch.setenv("APP_ENV", "production")

    # In AUTH_USERS
    users = [{"username": "alice", "role": "viewer", "password": "PlaintextPassword123"}]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users))
    with pytest.raises(ValueError, match="Plaintext AUTH_USERS passwords forbidden"):
        get_configured_users()

    # In AUTH_ADMIN_PASSWORD
    monkeypatch.delenv("AUTH_USERS", raising=False)
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "AdminPlainPassword123")
    with pytest.raises(ValueError, match="Plaintext admin password forbidden"):
        get_configured_users()


def test_prod_validates_admin_hash(monkeypatch):
    """Production must validate AUTH_ADMIN_PASSWORD_HASH format and bounds."""
    monkeypatch.setenv("APP_ENV", "production")

    # Malformed admin hash
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD_HASH", "invalid_hash_string")
    with pytest.raises(ValueError, match="pbkdf2_sha256"):
        get_configured_users()

    # Out of bounds iterations
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD_HASH", f"pbkdf2_sha256$50000${VALID_SALT}${VALID_DIGEST}")
    with pytest.raises(ValueError, match="bounded between"):
        get_configured_users()


def test_prod_rejects_duplicate_admin_username_collision(monkeypatch):
    """Production rejects collision between AUTH_USERS username 'admin' and AUTH_ADMIN_PASSWORD_HASH."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD_HASH", VALID_HASH)

    # User named 'admin' (even with viewer role) collides with shorthand admin
    monkeypatch.setenv(
        "AUTH_USERS",
        json.dumps([{"username": "admin", "role": "viewer", "password_hash": VALID_HASH}]),
    )
    with pytest.raises(ValueError, match="Duplicate configured admin: username 'admin' already configured"):
        get_configured_users()

    # Case-insensitive collision: 'Admin'
    monkeypatch.setenv(
        "AUTH_USERS",
        json.dumps([{"username": "Admin", "role": "editor", "password_hash": VALID_HASH}]),
    )
    with pytest.raises(ValueError, match="Duplicate configured admin: username 'admin' already configured"):
        get_configured_users()


# ---------------------------------------------------------------------------
# 3. Positive production configuration tests
# ---------------------------------------------------------------------------

def test_prod_allows_multiple_admin_role_users(monkeypatch):
    """Multiple DIFFERENT usernames with role 'admin' are legitimate and permitted."""
    monkeypatch.setenv("APP_ENV", "production")
    users_data = [
        {"username": "lead_admin", "role": "admin", "password_hash": VALID_HASH},
        {"username": "backup_admin", "role": "admin", "password_hash": VALID_HASH},
        {"username": "editor_user", "role": "editor", "password_hash": VALID_HASH},
    ]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_data))

    users = get_configured_users()
    assert len(users) == 3
    assert "lead_admin" in users
    assert users["lead_admin"].role == UserRole.ADMIN
    assert "backup_admin" in users
    assert users["backup_admin"].role == UserRole.ADMIN
    assert "editor_user" in users
    assert users["editor_user"].role == UserRole.EDITOR


def test_prod_allows_shorthand_admin_with_differently_named_admin_user(monkeypatch):
    """Shorthand admin (username='admin') can combine with differently-named admin in AUTH_USERS."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD_HASH", VALID_HASH)
    monkeypatch.setenv(
        "AUTH_USERS",
        json.dumps([
            {"username": "superadmin", "role": "admin", "password_hash": VALID_HASH},
            {"username": "editor_user", "role": "editor", "password_hash": VALID_HASH},
        ]),
    )

    users = get_configured_users()
    assert len(users) == 3
    assert "admin" in users
    assert users["admin"].role == UserRole.ADMIN
    assert "superadmin" in users
    assert users["superadmin"].role == UserRole.ADMIN
    assert "editor_user" in users
    assert users["editor_user"].role == UserRole.EDITOR


def test_prod_valid_admin_shorthand_alone(monkeypatch):
    """Valid AUTH_ADMIN_PASSWORD_HASH alone configures the single admin."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD_HASH", VALID_HASH)

    users = get_configured_users()
    assert len(users) == 1
    assert "admin" in users
    assert users["admin"].role == UserRole.ADMIN
    assert users["admin"].password_hash == VALID_HASH


# ---------------------------------------------------------------------------
# 4. Application startup tests via create_app (isolated with tmp_path)
# ---------------------------------------------------------------------------

def test_startup_fails_fast_on_invalid_config_before_db_creation(tmp_path: Path, monkeypatch):
    """create_app in production fails fast on invalid auth config before creating any database."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Invalid config: empty username
    monkeypatch.setenv("AUTH_USERS", json.dumps([{"username": "", "password_hash": VALID_HASH}]))

    expected_db = tmp_path / "translator.db"
    assert not expected_db.exists()

    with pytest.raises(ValueError, match="empty username"):
        create_app(data_dir=tmp_path, start_worker=False, auth_override_local_only=False)

    # Database file must not have been created
    assert not expected_db.exists()


def test_startup_succeeds_on_valid_production_config(tmp_path: Path, monkeypatch):
    """create_app in production successfully initializes database with valid configuration."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    users_data = [
        {"username": "admin_user", "role": "admin", "password_hash": VALID_HASH},
        {"username": "editor_user", "role": "editor", "password_hash": VALID_HASH},
    ]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_data))

    app = create_app(data_dir=tmp_path, start_worker=False, auth_override_local_only=False)
    client = TestClient(app)

    # Health check is accessible
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # Database was safely created in tmp_path
    expected_db = tmp_path / "translator.db"
    assert expected_db.exists()


# ---------------------------------------------------------------------------
# 5. Development mode preservation tests (APP_ENV != production)
# ---------------------------------------------------------------------------

def test_dev_supports_plaintext_passwords(monkeypatch):
    """Development mode supports plaintext passwords and automatically hashes them."""
    monkeypatch.setenv("APP_ENV", "development")
    users_data = [
        {"username": "dev_user", "role": "editor", "password": "devpassword123"},
    ]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_data))

    users = get_configured_users()
    assert "dev_user" in users
    assert users["dev_user"].role == UserRole.EDITOR
    assert users["dev_user"].password_hash.startswith("pbkdf2_sha256$")


def test_dev_supports_admin_plaintext_password(monkeypatch):
    """Development mode supports AUTH_ADMIN_PASSWORD."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD", "AdminDev123")

    users = get_configured_users()
    assert "admin" in users
    assert users["admin"].role == UserRole.ADMIN
    assert users["admin"].password_hash.startswith("pbkdf2_sha256$")


def test_dev_falls_back_to_viewer_on_invalid_or_null_role(monkeypatch):
    """Development mode preserves viewer fallback for unknown roles."""
    monkeypatch.setenv("APP_ENV", "development")
    users_data = [
        {"username": "user_one", "role": "unknown_role", "password": "pass"},
        {"username": "user_two", "role": None, "password": "pass"},
    ]
    monkeypatch.setenv("AUTH_USERS", json.dumps(users_data))

    users = get_configured_users()
    assert "user_one" in users
    assert users["user_one"].role == UserRole.VIEWER
    assert "user_two" in users
    assert users["user_two"].role == UserRole.VIEWER


def test_dev_skips_malformed_json_gracefully(monkeypatch):
    """Development mode logs warning and does not crash on malformed JSON or empty list."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_USERS", "{not-valid-json}")

    users = get_configured_users()
    assert users == {}

    monkeypatch.setenv("AUTH_USERS", "[]")
    users_empty = get_configured_users()
    assert users_empty == {}
