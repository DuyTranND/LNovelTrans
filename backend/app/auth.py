"""Authentication, session management, CSRF protection, and RBAC for Trạm Dịch."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from enum import Enum
from typing import Any

from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "tram_dich_session"
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days


class UserRole(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class AuthUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    username: str
    role: UserRole = UserRole.VIEWER
    password_hash: str = Field(default="", repr=False)


class LoginRequest(BaseModel):
    username: str
    password: str = Field(repr=False)


class AuthStatusResponse(BaseModel):
    authenticated: bool
    username: str | None = None
    role: str | None = None
    csrf_token: str | None = None
    auth_override: bool = False


# Ephemeral in-memory fallback secret if SESSION_SECRET is not provided in env.
_EPHEMERAL_SECRET: str = secrets.token_hex(32)


def get_session_secret() -> str:
    """Return configured SESSION_SECRET or ephemeral process secret."""
    secret = os.getenv("SESSION_SECRET", "").strip()
    if os.getenv("APP_ENV", "").strip().lower() == "production":
        if len(secret) < 32:
            raise RuntimeError(
                "SESSION_SECRET must be configured and at least 32 characters in production."
            )
        return secret
    if secret:
        return secret
    return _EPHEMERAL_SECRET


def is_local_override_enabled() -> bool:
    """Check if explicit local-only dev override is active."""
    val = os.environ.get("AUTH_OVERRIDE_LOCAL_ONLY", "").strip().lower()
    return val in ("true", "1", "yes")


DIRECT_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1"})


def is_direct_loopback_request(request: Request) -> bool:
    """Check if request directly originates from loopback (127.0.0.1 or ::1).

    Strictly inspects direct transport peer (request.client.host).
    Never trusts Forwarded, X-Forwarded-*, Host, or X-Real-IP headers.
    """
    client = getattr(request, "client", None)
    if not client:
        return False
    host = getattr(client, "host", None)
    return host in DIRECT_LOOPBACK_HOSTS


def hash_password(password: str, salt: str | None = None) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with 100,000 iterations."""
    if not salt:
        salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    )
    return f"pbkdf2_sha256$100000${salt}${derived.hex()}"


def validate_password_hash(hash_str: str) -> None:
    """Validate password hash format, algorithm, iterations, salt, and digest.

    Requirements:
    - Algorithm: pbkdf2_sha256
    - Iterations: integer bounded between 100,000 and 1,000,000
    - Salt: non-empty reasonable salt (4..128 printable chars)
    - Digest: exactly 64 hexadecimal characters
    """
    if not isinstance(hash_str, str) or not hash_str.strip():
        raise ValueError("Password hash must be a non-empty string")
    parts = hash_str.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        raise ValueError("Password hash must use pbkdf2_sha256 algorithm format: pbkdf2_sha256$<iter>$<salt>$<digest>")
    try:
        iterations = int(parts[1])
    except ValueError:
        raise ValueError("Password hash iterations must be a valid integer")
    if not (100_000 <= iterations <= 1_000_000):
        raise ValueError(f"Password hash iterations ({iterations}) must be bounded between 100,000 and 1,000,000")
    salt = parts[2]
    if not salt or len(salt) < 4 or len(salt) > 128 or not salt.isprintable() or salt.isspace():
        raise ValueError("Password hash salt must be non-empty and between 4 and 128 characters")
    digest = parts[3]
    if len(digest) != 64 or not all(c in "0123456789abcdefABCDEF" for c in digest):
        raise ValueError("Password hash digest must be a 64-character hexadecimal string")


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hashed string using constant-time comparison."""
    if not hashed:
        return False
    parts = hashed.split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            iterations = int(parts[1])
            salt = parts[2]
            expected_hex = parts[3]
            derived = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            )
            return hmac.compare_digest(derived.hex(), expected_hex)
        except Exception:
            return False
    # Fallback constant-time check if hashed is plain string (e.g. dev/test fixtures)
    if os.getenv("APP_ENV", "").strip().lower() == "production":
        return False
    return hmac.compare_digest(password, hashed)


def get_configured_users() -> dict[str, AuthUser]:
    """Load invite-only users configured via safe environment variables."""
    users: dict[str, AuthUser] = {}
    is_prod = os.getenv("APP_ENV", "").strip().lower() == "production"

    raw_json = os.environ.get("AUTH_USERS", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except Exception as e:
            if is_prod:
                raise ValueError(f"Malformed AUTH_USERS JSON: {e}") from e
            logger.warning("Failed to parse AUTH_USERS JSON: %s", type(e).__name__)
            parsed = None

        if parsed is None:
            if is_prod:
                raise ValueError("AUTH_USERS root cannot be null; must be a non-empty JSON list")
        elif not isinstance(parsed, list):
            if is_prod:
                raise ValueError("AUTH_USERS root must be a JSON list")
            logger.warning("AUTH_USERS root must be a JSON list")
        elif is_prod and len(parsed) == 0:
            raise ValueError("AUTH_USERS list cannot be empty in production")
        else:
            for idx, item in enumerate(parsed):
                if not isinstance(item, dict):
                    if is_prod:
                        raise ValueError(f"AUTH_USERS entry at index {idx} must be a dictionary")
                    continue

                raw_u = item.get("username")
                if is_prod:
                    if raw_u is None or not isinstance(raw_u, str):
                        raise ValueError(f"AUTH_USERS entry at index {idx} username must be a non-empty string")
                    u = raw_u.strip()
                    if not u:
                        raise ValueError(f"AUTH_USERS entry at index {idx} has empty username")
                else:
                    if raw_u is None:
                        continue
                    u = str(raw_u).strip()
                    if not u:
                        continue

                u_lower = u.lower()
                if u_lower in users:
                    if is_prod:
                        raise ValueError(f"Duplicate username '{u}' configured in AUTH_USERS")
                    continue

                if "role" in item:
                    raw_role = item["role"]
                    if is_prod and (raw_role is None or not isinstance(raw_role, str)):
                        raise ValueError(f"Invalid role for user '{u}': role must be a non-empty string")
                    if raw_role is not None:
                        r_str = str(raw_role).strip().lower()
                        try:
                            role = UserRole(r_str)
                        except ValueError:
                            if is_prod:
                                raise ValueError(f"Invalid role '{raw_role}' for user '{u}'")
                            role = UserRole.VIEWER
                    else:
                        role = UserRole.VIEWER
                else:
                    role = UserRole.VIEWER

                if is_prod and "password" in item:
                    raise ValueError("Plaintext AUTH_USERS passwords forbidden in production")

                pw_hash = str(item.get("password_hash", ""))
                if not pw_hash and "password" in item:
                    # Convert plain password in env directly to hash in memory
                    pw_hash = hash_password(str(item["password"]))

                if is_prod:
                    if not pw_hash:
                        raise ValueError(f"Missing password_hash for user '{u}'")
                    validate_password_hash(pw_hash)

                users[u_lower] = AuthUser(
                    username=u,
                    role=role,
                    password_hash=pw_hash,
                )

    # Single-admin shorthand configuration
    admin_pw = os.environ.get("AUTH_ADMIN_PASSWORD", "").strip()
    if is_prod and admin_pw:
        raise ValueError("Plaintext admin password forbidden in production")
    admin_hash = os.environ.get("AUTH_ADMIN_PASSWORD_HASH", "").strip()

    if admin_pw or admin_hash:
        if is_prod:
            if "admin" in users:
                raise ValueError("Duplicate configured admin: username 'admin' already configured in AUTH_USERS")
            if not admin_hash:
                raise ValueError("AUTH_ADMIN_PASSWORD_HASH must not be empty in production")
            validate_password_hash(admin_hash)
            effective_hash = admin_hash
        else:
            effective_hash = admin_hash or hash_password(admin_pw)

        users["admin"] = AuthUser(
            username="admin",
            role=UserRole.ADMIN,
            password_hash=effective_hash,
        )

    return users


def create_session_token(
    username: str,
    role: str,
    secret: str | None = None,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> str:
    """Create a signed, tamper-evident session token."""
    effective_secret = secret or get_session_secret()
    payload = {
        "sub": username,
        "role": role,
        "exp": int(time.time()) + ttl_seconds,
        "jti": secrets.token_hex(8),
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8").rstrip("=")
    signature = hmac.new(
        effective_secret.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_session_token(token: str, secret: str | None = None) -> dict[str, Any] | None:
    """Verify session token signature and expiration."""
    if not token or "." not in token:
        return None
    effective_secret = secret or get_session_secret()
    parts = token.split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, signature = parts

    expected_sig = hmac.new(
        effective_secret.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        return None

    try:
        # Restore padding for base64 decode
        rem = len(payload_b64) % 4
        padded = payload_b64 + ("=" * (4 - rem) if rem else "")
        payload_bytes = base64.urlsafe_b64decode(padded)
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            return None

        # Check expiration
        exp = payload.get("exp")
        if exp is None or time.time() > float(exp):
            return None

        return payload
    except Exception:
        return None


def generate_csrf_token(session_token: str, secret: str | None = None) -> str:
    """Derive deterministic CSRF token from session token via HMAC."""
    effective_secret = secret or get_session_secret()
    data = f"{session_token}:csrf".encode("utf-8")
    return hmac.new(effective_secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def verify_csrf_token(csrf_token: str, session_token: str, secret: str | None = None) -> bool:
    """Verify CSRF token matches session."""
    if not csrf_token or not session_token:
        return False
    expected = generate_csrf_token(session_token, secret)
    return hmac.compare_digest(csrf_token, expected)


def should_cookie_be_secure(request: Request) -> bool:
    """Determine whether session cookie must set the Secure flag."""
    if os.getenv("APP_ENV", "").strip().lower() == "production":
        return True

    explicit = os.getenv("COOKIE_SECURE", "").strip().lower()
    if explicit in ("true", "1", "yes"):
        return True
    if explicit in ("false", "0", "no"):
        return False

    return request.url.scheme == "https"


def set_session_cookie(
    response: Response,
    session_token: str,
    request: Request,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> None:
    """Attach HttpOnly, SameSite session cookie with appropriate security flags."""
    secure_flag = should_cookie_be_secure(request)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=secure_flag,
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    """Clear session cookie on logout."""
    secure_flag = should_cookie_be_secure(request)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=secure_flag,
        path="/",
    )


def get_current_user_from_request(
    request: Request,
    secret: str | None = None,
) -> tuple[AuthUser | None, str | None]:
    """Extract authenticated user and raw session token from request.

    Returns (user, session_token) or (None, None).
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        # Check Authorization header: Bearer <token>
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if token:
        payload = verify_session_token(token, secret)
        if payload:
            username = str(payload.get("sub", ""))
            users = get_configured_users()
            user = users.get(username.strip().lower())
            if not user:
                return None, None
            return user, token

    # Check explicit local-only override (strictly restricted to direct loopback)
    if is_local_override_enabled() and is_direct_loopback_request(request):
        return AuthUser(username="local_admin", role=UserRole.ADMIN), None

    return None, None


# High-risk / Gemini / Admin-only actions and paths
ADMIN_ONLY_PREFIXES = (
    "/api/books/{book_id}/jobs",
    "/api/jobs",
    "/api/books/{book_id}/analyze",
    "/api/books/{book_id}/discovery",
    "/api/books/{book_id}/consistency-reviews",
    "/api/presets",
)

# Editor allowed write routes: strictly mapped by HTTP method and anchored regex patterns.
# Any write route not matching this allowlist is denied by default for Editor.
EDITOR_ALLOWED_MUTATIONS: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = (
    # PATCH /api/chapters/{chapter_id}
    (frozenset({"PATCH"}), re.compile(r"^/api/chapters/[^/]+$")),
    # POST /api/books/{book_id}/glossary
    (frozenset({"POST"}), re.compile(r"^/api/books/[^/]+/glossary$")),
    # PATCH,DELETE /api/glossary/{item_id}
    (frozenset({"PATCH", "DELETE"}), re.compile(r"^/api/glossary/[^/]+$")),
    # POST /api/books/{book_id}/characters
    (frozenset({"POST"}), re.compile(r"^/api/books/[^/]+/characters$")),
    # PATCH,DELETE /api/characters/{character_id}
    (frozenset({"PATCH", "DELETE"}), re.compile(r"^/api/characters/[^/]+$")),
    # PUT /api/books/{book_id}/bible
    (frozenset({"PUT"}), re.compile(r"^/api/books/[^/]+/bible$")),
    # POST /api/discovery/proposals/{proposal_id}/review
    (frozenset({"POST"}), re.compile(r"^/api/discovery/proposals/[^/]+/review$")),
    # POST /api/books/{book_id}/discovery/proposals/{proposal_id}/review
    (frozenset({"POST"}), re.compile(r"^/api/books/[^/]+/discovery/proposals/[^/]+/review$")),
    # POST /api/books/{book_id}/export/pdf
    (frozenset({"POST"}), re.compile(r"^/api/books/[^/]+/export/pdf$")),
)


def can_user_access_endpoint(
    user_role: UserRole,
    method: str,
    path: str,
) -> bool:
    """RBAC validation:

    - Viewer: Read-only (GET, HEAD, OPTIONS) on all readable endpoints, plus POST /api/auth/logout.
    - Editor: Read-only access plus explicitly allowlisted mutation routes (chapter edits,
              glossary, story bible, characters, discovery proposal reviews, PDF export).
              Default DENY for any unlisted write route (uploads, jobs, settings, presets, audit, etc.).
    - Admin: Full access.
    """
    method = method.strip().upper()

    # Safe read methods are allowed for viewer, editor, admin
    if method in ("GET", "HEAD", "OPTIONS"):
        return True

    # Logout is permitted for all authenticated roles (including Viewer)
    if method == "POST" and path == "/api/auth/logout":
        return True

    # Admin has unrestricted access to all endpoints
    if user_role in (UserRole.ADMIN, UserRole.ADMIN.value):
        return True

    # Viewer cannot perform any other state-changing requests
    if user_role in (UserRole.VIEWER, UserRole.VIEWER.value):
        return False

    # Editor write operations: strictly allowlisted by exact method and anchored path patterns
    if user_role in (UserRole.EDITOR, UserRole.EDITOR.value):
        for allowed_methods, pattern in EDITOR_ALLOWED_MUTATIONS:
            if method in allowed_methods and pattern.fullmatch(path):
                return True
        return False

    # Fail closed for any unknown or unhandled role
    return False
