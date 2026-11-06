"""FastAPI application for Trạm Dịch."""

from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.app.auth import (
    AuthUser,
    LoginRequest,
    UserRole,
    can_user_access_endpoint,
    clear_session_cookie,
    create_session_token,
    generate_csrf_token,
    get_configured_users,
    get_current_user_from_request,
    get_session_secret,
    is_direct_loopback_request,
    is_local_override_enabled,
    set_session_cookie,
    verify_csrf_token,
    verify_password,
)
from backend.app.config import get_gemini_api_keys, get_settings
from backend.app.context import build_translation_context
from backend.app.db import (
    RevisionConflictError,
    approve_discovery_proposal,
    claim_next_job,
    create_audit_run,
    create_character,
    create_discovery_proposal,
    create_discovery_run,
    create_glossary_item,
    create_job,
    create_prompt_preset,
    delete_character,
    delete_glossary_item,
    delete_prompt_preset,
    get_audit_findings,
    get_audit_run,
    get_book,
    get_book_chapters,
    get_chapter,
    get_character,
    get_default_prompt_preset,
    get_discovery_proposal,
    get_discovery_run,
    get_export_bundle,
    get_glossary_item,
    get_job,
    get_prompt_preset,
    get_setting,
    get_story_bible,
    has_active_audit_run,
    has_active_job,
    has_running_job,
    init_db,
    list_audit_runs,
    list_books,
    list_characters,
    list_discovery_proposals,
    list_discovery_runs,
    list_glossary,
    list_jobs,
    list_prompt_presets,
    mark_audit_runs_stale,
    recover_interrupted_audit_runs,
    recover_interrupted_discovery_runs,
    recover_interrupted_jobs,
    reject_discovery_proposal,
    save_parsed_book,
    set_default_prompt_preset,
    set_setting,
    update_audit_run,
    update_chapter_segments,
    update_character,
    update_discovery_proposal_status,
    update_discovery_run,
    update_glossary_item,
    update_job_status,
    update_prompt_preset,
    update_story_bible,
)
from backend.app.audit import AuditManager
from backend.app.discovery import DiscoveryManager
from backend.app.epub import (
    EpubError,
    parse_epub,
    rebuild_epub,
)
from backend.app.pdf_export import (
    IncompleteChapterError,
    PdfExportError,
    PdfFontError,
    check_translation_completeness,
    generate_pdf_export,
)
from backend.app.jobs import JobWorker
from backend.app.provider import (
    GeminiProvider,
    ProviderModelError,
    sanitize_secret,
    validate_model_name,
)


# Request and response models
class SegmentUpdate(BaseModel):
    id: str
    source: str | None = None
    translation: str | None = None
    expected_revision: int | None = None


class ChapterPatchRequest(BaseModel):
    segments: list[SegmentUpdate] = Field(default_factory=list)
    status: str | None = None


class GlossaryCreateRequest(BaseModel):
    source: str
    target: str
    locked: bool = False
    notes: str = ""


class GlossaryPatchRequest(BaseModel):
    source: str | None = None
    target: str | None = None
    locked: bool | None = None
    notes: str | None = None


class StoryBibleRequest(BaseModel):
    text: str = ""
    story_bible: str | None = None

    def get_text(self) -> str:
        if self.text:
            return self.text
        if self.story_bible:
            return self.story_bible
        return ""


class SettingsPatchRequest(BaseModel):
    model: str


class JobParamsRequest(BaseModel):
    review: bool = False
    max_segments: int | None = None
    model: str | None = None
    preset_id: str | None = None


class CreateJobRequest(BaseModel):
    chapter_ids: list[str] | None = None
    params: JobParamsRequest | None = None
    review: bool | None = None
    max_segments: int | None = None
    model: str | None = None
    preset_id: str | None = None


class PromptPresetCreateRequest(BaseModel):
    name: str
    description: str = ""
    system_prompt: str | None = None
    user_prompt_template: str | None = None
    review_system_prompt: str | None = None
    review_user_prompt_template: str | None = None
    few_shot_examples: list[dict[str, Any]] = Field(default_factory=list)
    is_default: bool = False


class PromptPresetPatchRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    user_prompt_template: str | None = None
    review_system_prompt: str | None = None
    review_user_prompt_template: str | None = None
    few_shot_examples: list[dict[str, Any]] | None = None
    is_default: bool | None = None


class PdfExportRequest(BaseModel):
    chapter_ids: list[str] | None = None
    content: str = "translation"


class ConsistencyAuditCreateRequest(BaseModel):
    model: str | None = None


class CharacterCreateRequest(BaseModel):
    canonical_source: str
    canonical_target: str
    source_aliases: list[str] = Field(default_factory=list)
    gender: str = "unknown"
    speech_style: str = ""
    relationships: str = ""
    first_occurrence_chapter_id: str | None = None
    first_occurrence_chapter_index: int | None = None
    locked: bool = False


class CharacterPatchRequest(BaseModel):
    canonical_source: str | None = None
    canonical_target: str | None = None
    source_aliases: list[str] | None = None
    gender: str | None = None
    speech_style: str | None = None
    relationships: str | None = None
    first_occurrence_chapter_id: str | None = None
    first_occurrence_chapter_index: int | None = None
    locked: bool | None = None


class DiscoveryStartRequest(BaseModel):
    """Payload for starting pre-translation entity and term candidate discovery.

    Attributes:
        chapter_ids: Optional list of chapter IDs to analyze. Must be non-empty if provided,
            will be deduplicated preserving order, and all IDs must belong to the target book.
            Invalid IDs will be rejected with 400 Bad Request rather than silently ignored.
        model: Model identifier for candidate extraction (defaults to gemini-3.5-flash-lite).
        max_segments: Optional positive integer capping total segments analyzed across chapters.
    """
    chapter_ids: list[str] | None = None
    model: str | None = None
    max_segments: int | None = None


class DiscoveryReviewRequest(BaseModel):
    action: str  # 'approve' or 'reject'
    override_source: str | None = None
    override_target: str | None = None
    override_aliases: list[str] | None = None
    override_gender: str | None = None
    override_speech_style: str | None = None
    override_notes: str | None = None
    override_category: str | None = None


def create_app(
    data_dir: str | Path | None = None,
    provider: Any = None,
    start_worker: bool = True,
    auth_override_local_only: bool | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    if os.getenv("APP_ENV", "").strip().lower() == "production":
        get_session_secret()
        if auth_override_local_only is True or is_local_override_enabled():
            raise RuntimeError("AUTH_OVERRIDE_LOCAL_ONLY is forbidden in production")
        if not get_configured_users():
            raise RuntimeError("At least one AUTH_USERS or admin account is required in production")
    app_settings = get_settings(data_dir)
    db_path = app_settings.data_dir / "translator.db"
    init_db(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recover_interrupted_jobs(app.state.db_path)
        recover_interrupted_audit_runs(app.state.db_path)
        recover_interrupted_discovery_runs(app.state.db_path)
        owned_provider = None
        gemini_key, sec_key, tert_key = get_gemini_api_keys()
        if app.state.provider is None:
            if gemini_key:
                owned_provider = GeminiProvider(
                    api_key=gemini_key,
                    secondary_api_key=sec_key,
                    tertiary_api_key=tert_key,
                    model=app_settings.default_model,
                )
                app.state.provider = owned_provider
        worker = None
        if getattr(app.state, "start_worker", False):
            worker = JobWorker(
                app.state.db_path,
                provider_getter=lambda: app.state.provider,
            )
            await worker.start()
            app.state.worker = worker

        audit_mgr = AuditManager(
            app.state.db_path,
            provider_getter=lambda: app.state.provider,
        )
        app.state.audit_manager = audit_mgr

        discovery_mgr = DiscoveryManager(
            app.state.db_path,
            provider_getter=lambda: app.state.provider,
        )
        app.state.discovery_manager = discovery_mgr

        try:
            yield
        finally:
            if discovery_mgr is not None:
                await discovery_mgr.shutdown()
            if audit_mgr is not None:
                await audit_mgr.shutdown()
            if worker is not None:
                await worker.stop()
            if owned_provider is not None:
                await owned_provider.close()
                app.state.provider = None

    app = FastAPI(title="Trạm Dịch API", version="0.4.0", lifespan=lifespan)

    if auth_override_local_only is None:
        auth_override_local_only = is_local_override_enabled()

    app.state.data_dir = app_settings.data_dir
    app.state.db_path = db_path
    app.state.provider = provider
    app.state.start_worker = start_worker
    app.state.auth_override_local_only = auth_override_local_only
    app.state.worker = None
    app.state.audit_manager = None
    app.state.discovery_manager = None

    @app.middleware("http")
    async def auth_and_csrf_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # Public endpoints: health, login, and static files
        if not path.startswith("/api") or path == "/api/health" or path == "/api/auth/login":
            return await call_next(request)

        override = bool(app.state.auth_override_local_only) and is_direct_loopback_request(request)

        user, session_token = get_current_user_from_request(request)
        if user and user.username == "local_admin" and session_token is None and not override:
            user = None

        # Fail-closed authentication check
        if not user:
            if override:
                user = AuthUser(username="local_admin", role=UserRole.ADMIN)
            else:
                import json as _json
                return Response(
                    content=_json.dumps({"detail": "Yêu cầu đăng nhập để truy cập tài nguyên"}),
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    media_type="application/json",
                )

        # CSRF protection on state-changing requests when authenticated via session cookie
        if method in ("POST", "PUT", "PATCH", "DELETE") and session_token:
            csrf_header = request.headers.get("x-csrf-token", "").strip()
            if not verify_csrf_token(csrf_header, session_token):
                import json as _json
                return Response(
                    content=_json.dumps({"detail": "Mã CSRF token không hợp lệ hoặc đã hết hạn"}),
                    status_code=status.HTTP_403_FORBIDDEN,
                    media_type="application/json",
                )

        # RBAC check
        role = user.role if isinstance(user.role, UserRole) else UserRole(str(user.role))
        if not can_user_access_endpoint(role, method, path):
            import json as _json
            return Response(
                content=_json.dumps({
                    "detail": f"Vai trò '{role.value}' không có quyền thực hiện thao tác {method} trên {path}"
                }),
                status_code=status.HTTP_403_FORBIDDEN,
                media_type="application/json",
            )

        request.state.user = user
        return await call_next(request)

    # Health & System
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": "Tram Dich"}

    # Authentication & Session Endpoints
    @app.post("/api/auth/login")
    async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        users = get_configured_users()
        user = users.get(payload.username.strip().lower())
        if not user or not verify_password(payload.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Tên đăng nhập hoặc mật khẩu không chính xác",
            )
        token = create_session_token(user.username, user.role.value)
        set_session_cookie(response, token, request)
        csrf = generate_csrf_token(token)
        return {
            "status": "ok",
            "username": user.username,
            "role": user.role.value,
            "csrf_token": csrf,
        }

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        clear_session_cookie(response, request)
        return {"status": "ok"}

    @app.get("/api/auth/me")
    async def get_me(request: Request) -> dict[str, Any]:
        user, token = get_current_user_from_request(request)
        override = bool(app.state.auth_override_local_only) and is_direct_loopback_request(request)

        if not user:
            if override:
                user = AuthUser(username="local_admin", role=UserRole.ADMIN)
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Chưa đăng nhập",
                )

        csrf = generate_csrf_token(token) if token else "local_csrf"
        return {
            "authenticated": True,
            "username": user.username,
            "role": user.role.value if isinstance(user.role, UserRole) else str(user.role),
            "csrf_token": csrf,
            "auth_override": bool(override),
        }

    @app.get("/api/settings")
    async def get_app_settings() -> dict[str, Any]:
        persisted_model = get_setting(app.state.db_path, "model")
        curr_settings = get_settings(app.state.data_dir)
        effective_model = persisted_model
        if effective_model:
            try:
                effective_model = validate_model_name(effective_model)
            except ProviderModelError:
                effective_model = curr_settings.default_model
        return {
            "model": effective_model or curr_settings.default_model,
            "key_configured": curr_settings.key_configured,
            "gemini_configured": curr_settings.gemini_configured,
        }

    @app.patch("/api/settings")
    async def update_app_settings(payload: SettingsPatchRequest) -> dict[str, Any]:
        try:
            valid_model = validate_model_name(payload.model)
        except ProviderModelError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        set_setting(app.state.db_path, "model", valid_model)
        curr_settings = get_settings(app.state.data_dir)
        return {
            "model": valid_model,
            "key_configured": curr_settings.key_configured,
            "gemini_configured": curr_settings.gemini_configured,
        }

    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        curr_settings = get_settings(app.state.data_dir)
        p = app.state.provider
        gemini_key, sec_key, tert_key = get_gemini_api_keys()

        if p is not None and hasattr(p, "models"):
            try:
                raw_models = await p.models()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Lỗi khi truy vấn danh sách mô hình từ provider: {e}",
                )

            models_result: list[dict[str, Any]] = []
            for m in raw_models:
                m_str = m if isinstance(m, str) else m.get("id", "")
                try:
                    valid_name = validate_model_name(m_str)
                    models_result.append({
                        "id": valid_name,
                        "name": valid_name,
                        "provider": "gemini",
                        "available": True,
                    })
                except ProviderModelError:
                    continue

            return {
                "models": models_result,
                "key_configured": curr_settings.key_configured,
                "gemini_configured": curr_settings.gemini_configured,
            }

        models_result = []

        if gemini_key:
            all_keys = [k for k in (gemini_key, sec_key, tert_key) if k]
            try:
                temp_prov = GeminiProvider(
                    api_key=gemini_key,
                    secondary_api_key=sec_key,
                    tertiary_api_key=tert_key,
                )
                try:
                    discovered = await temp_prov.models()
                    for m in discovered:
                        try:
                            valid_name = validate_model_name(m)
                            models_result.append({
                                "id": valid_name,
                                "name": valid_name,
                                "provider": "gemini",
                                "available": True,
                            })
                        except ProviderModelError:
                            continue
                finally:
                    await temp_prov.close()
            except Exception as e:
                clean_err = sanitize_secret(str(e), all_keys)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Lỗi khi truy vấn danh sách mô hình từ Gemini: {clean_err}",
                )
        else:
            models_result.append({
                "id": curr_settings.default_model,
                "name": curr_settings.default_model,
                "provider": "gemini",
                "available": False,
            })

        return {
            "models": models_result,
            "key_configured": curr_settings.key_configured,
            "gemini_configured": curr_settings.gemini_configured,
        }

    @app.get("/api/provider/key-status")
    async def get_key_status() -> dict[str, Any]:
        p = app.state.provider
        if p is not None and hasattr(p, "get_key_status") and callable(p.get_key_status):
            return p.get_key_status()

        gemini_key, sec_key, tert_key = get_gemini_api_keys()
        configured_keys = [k for k in (gemini_key, sec_key, tert_key) if k]
        count = len(configured_keys)
        slots = [
            {
                "slot": 1,
                "role": "primary",
                "label": "Khóa 1 (Chính)",
                "configured": bool(gemini_key),
                "status": "ready" if gemini_key else "not_configured",
                "inflight": 0,
                "cooldown_remaining": 0.0,
                "last_used": None,
            },
            {
                "slot": 2,
                "role": "secondary",
                "label": "Khóa 2 (Phụ)",
                "configured": bool(sec_key),
                "status": "ready" if sec_key else "not_configured",
                "inflight": 0,
                "cooldown_remaining": 0.0,
                "last_used": None,
            },
            {
                "slot": 3,
                "role": "tertiary",
                "label": "Khóa 3 (Thứ ba)",
                "configured": bool(tert_key),
                "status": "ready" if tert_key else "not_configured",
                "inflight": 0,
                "cooldown_remaining": 0.0,
                "last_used": None,
            },
        ]
        return {
            "count": count,
            "inflight": 0,
            "last_used": None,
            "slots": slots,
        }

    # Books
    @app.post("/api/books", status_code=status.HTTP_201_CREATED)
    async def upload_book(file: UploadFile = File(...)) -> dict[str, Any]:
        if not file.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tập tin tải lên không hợp lệ",
            )

        # Avoid memory bomb: max 50MB bounded read
        max_size = 50 * 1024 * 1024  # 50MB
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > max_size:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Tập tin tải lên vượt quá giới hạn cho phép (50MB)",
                )

        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tập tin tải lên rỗng",
            )

        book_id = uuid.uuid4().hex
        dest_path = app.state.data_dir / f"{book_id}.epub"

        with open(dest_path, "wb") as f:
            f.write(content)

        try:
            parsed = parse_epub(dest_path)
            book_dict = save_parsed_book(
                app.state.db_path, book_id, parsed, str(dest_path)
            )
            return book_dict
        except EpubError as e:
            if dest_path.exists():
                dest_path.unlink()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Không thể xử lý tập tin EPUB: {e}",
            )
        except Exception as e:
            if dest_path.exists():
                dest_path.unlink()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Lỗi hệ thống khi lưu sách: {e}",
            )

    @app.get("/api/books")
    async def get_books() -> list[dict[str, Any]]:
        return list_books(app.state.db_path)

    @app.get("/api/books/{book_id}")
    async def get_book_detail(book_id: str) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return book

    @app.get("/api/books/{book_id}/chapters")
    async def get_chapters_for_book(book_id: str) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return get_book_chapters(app.state.db_path, book_id)

    # Chapters
    @app.get("/api/chapters/{chapter_id}")
    async def get_chapter_detail(chapter_id: str) -> dict[str, Any]:
        chapter = get_chapter(app.state.db_path, chapter_id)
        if not chapter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy chương",
            )
        return chapter

    @app.patch("/api/chapters/{chapter_id}")
    async def patch_chapter(
        chapter_id: str, payload: ChapterPatchRequest, request: Request
    ) -> dict[str, Any]:
        chapter = get_chapter(app.state.db_path, chapter_id)
        if not chapter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy chương",
            )

        # Prohibit editing while running or pausing
        if has_running_job(app.state.db_path, chapter["book_id"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có tác vụ đang chạy hoặc tạm dừng, không thể chỉnh sửa",
            )

        override = bool(app.state.auth_override_local_only) and is_direct_loopback_request(request)

        enforce_revisions = not override

        try:
            updates = [s.model_dump(exclude_unset=True) for s in payload.segments]
            updated = update_chapter_segments(
                app.state.db_path,
                chapter_id,
                updates,
                new_status=payload.status,
                enforce_revisions=enforce_revisions,
            )
            return updated
        except RevisionConflictError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": e.message,
                    "conflicts": e.conflicts,
                },
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )

    # Glossary
    @app.get("/api/books/{book_id}/glossary")
    async def get_glossary(book_id: str) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return list_glossary(app.state.db_path, book_id)

    @app.post("/api/books/{book_id}/glossary", status_code=status.HTTP_201_CREATED)
    async def create_glossary(
        book_id: str, payload: GlossaryCreateRequest
    ) -> dict[str, Any]:
        if has_running_job(app.state.db_path, book_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có tiến trình dịch đang chạy, không thể sửa từ điển",
            )
        try:
            return create_glossary_item(
                app.state.db_path,
                book_id,
                payload.source,
                payload.target,
                locked=payload.locked,
                notes=payload.notes,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )

    @app.patch("/api/glossary/{item_id}")
    async def patch_glossary(
        item_id: str, payload: GlossaryPatchRequest
    ) -> dict[str, Any]:
        existing = get_glossary_item(app.state.db_path, item_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy mục từ điển",
            )

        if has_running_job(app.state.db_path, existing["book_id"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có tiến trình dịch đang chạy, không thể sửa từ điển",
            )

        try:
            return update_glossary_item(
                app.state.db_path,
                item_id,
                source=payload.source,
                target=payload.target,
                locked=payload.locked,
                notes=payload.notes,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy mục từ điển",
            )

    @app.delete("/api/glossary/{item_id}")
    async def delete_glossary(item_id: str) -> dict[str, Any]:
        existing = get_glossary_item(app.state.db_path, item_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy mục từ điển",
            )

        if has_running_job(app.state.db_path, existing["book_id"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có tiến trình dịch đang chạy, không thể xóa từ điển",
            )

        try:
            deleted = delete_glossary_item(app.state.db_path, item_id)
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Không tìm thấy mục từ điển",
                )
            return {"status": "deleted", "id": item_id}
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    # Character Profiles
    @app.get("/api/books/{book_id}/characters")
    async def get_book_characters_endpoint(book_id: str) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return list_characters(app.state.db_path, book_id)

    @app.post("/api/books/{book_id}/characters", status_code=status.HTTP_201_CREATED)
    async def create_book_character_endpoint(
        book_id: str, payload: CharacterCreateRequest
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        if not payload.canonical_source.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tên nguồn không được để trống",
            )
        if not payload.canonical_target.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tên dịch không được để trống",
            )
        try:
            return create_character(
                app.state.db_path,
                book_id=book_id,
                canonical_source=payload.canonical_source,
                canonical_target=payload.canonical_target,
                source_aliases=payload.source_aliases,
                gender=payload.gender,
                speech_style=payload.speech_style,
                relationships=payload.relationships,
                first_occurrence_chapter_id=payload.first_occurrence_chapter_id,
                first_occurrence_chapter_index=payload.first_occurrence_chapter_index,
                locked=payload.locked,
            )
        except ValueError as e:
            msg = str(e)
            if "đã tồn tại" in msg.lower() or "already exists" in msg.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    @app.get("/api/characters/{character_id}")
    async def get_character_endpoint(character_id: str) -> dict[str, Any]:
        char = get_character(app.state.db_path, character_id)
        if not char:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy nhân vật",
            )
        return char

    @app.patch("/api/characters/{character_id}")
    async def update_character_endpoint(
        character_id: str, payload: CharacterPatchRequest
    ) -> dict[str, Any]:
        char = get_character(app.state.db_path, character_id)
        if not char:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy nhân vật",
            )
        try:
            return update_character(
                app.state.db_path,
                character_id=character_id,
                canonical_source=payload.canonical_source,
                canonical_target=payload.canonical_target,
                source_aliases=payload.source_aliases,
                gender=payload.gender,
                speech_style=payload.speech_style,
                relationships=payload.relationships,
                first_occurrence_chapter_id=payload.first_occurrence_chapter_id,
                first_occurrence_chapter_index=payload.first_occurrence_chapter_index,
                locked=payload.locked,
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy nhân vật",
            )
        except ValueError as e:
            msg = str(e)
            if "đã tồn tại" in msg.lower() or "already exists" in msg.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)

    @app.delete("/api/characters/{character_id}")
    async def delete_character_endpoint(character_id: str) -> dict[str, Any]:
        char = get_character(app.state.db_path, character_id)
        if not char:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy nhân vật",
            )
        try:
            deleted = delete_character(app.state.db_path, character_id)
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Không tìm thấy nhân vật",
                )
            return {"status": "deleted", "id": character_id}
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    # Pre-translation AI Discovery
    @app.post("/api/books/{book_id}/discovery", status_code=status.HTTP_202_ACCEPTED)
    async def start_discovery_endpoint(
        book_id: str, payload: DiscoveryStartRequest
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        if payload.max_segments is not None and payload.max_segments <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="max_segments phải là số nguyên dương lớn hơn 0.",
            )
        dm = getattr(app.state, "discovery_manager", None)
        if dm is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Hệ thống gợi ý chưa sẵn sàng",
            )
        try:
            run = await dm.start_discovery(
                book_id=book_id,
                chapter_ids=payload.chapter_ids,
                model=payload.model,
                max_segments=payload.max_segments,
            )
            return run
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except RuntimeError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e),
            )

    @app.get("/api/books/{book_id}/discovery/runs")
    async def list_discovery_runs_endpoint(book_id: str) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return list_discovery_runs(app.state.db_path, book_id)

    @app.get("/api/books/{book_id}/discovery/runs/{run_id}")
    async def get_discovery_run_endpoint(
        book_id: str, run_id: str
    ) -> dict[str, Any]:
        run = get_discovery_run(app.state.db_path, run_id)
        if not run or run.get("book_id") != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy tiến trình gợi ý",
            )
        return run

    @app.post("/api/books/{book_id}/discovery/runs/{run_id}/cancel")
    async def cancel_discovery_run_endpoint(
        book_id: str, run_id: str
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        run = get_discovery_run(app.state.db_path, run_id)
        if not run or run.get("book_id") != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy tiến trình gợi ý",
            )
        current_status = run.get("status")
        if current_status not in ("completed", "failed", "cancelled"):
            dm = getattr(app.state, "discovery_manager", None)
            if dm:
                await dm.cancel_discovery(run_id)
            run = get_discovery_run(app.state.db_path, run_id) or run

        return {"status": run.get("status", "cancelled"), "run_id": run_id}

    @app.get("/api/books/{book_id}/discovery/proposals")
    async def list_discovery_proposals_endpoint(
        book_id: str,
        status: str | None = Query(None),
        category: str | None = Query(None),
        run_id: str | None = Query(None),
    ) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return list_discovery_proposals(
            app.state.db_path,
            book_id=book_id,
            status=status,
            category=category,
            run_id=run_id,
        )

    @app.get("/api/discovery/proposals/{proposal_id}")
    async def get_discovery_proposal_endpoint(
        proposal_id: str,
    ) -> dict[str, Any]:
        prop = get_discovery_proposal(app.state.db_path, proposal_id)
        if not prop:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đề xuất",
            )
        return prop

    @app.post("/api/discovery/proposals/{proposal_id}/review")
    @app.post("/api/books/{book_id}/discovery/proposals/{proposal_id}/review")
    async def review_discovery_proposal_endpoint(
        proposal_id: str,
        payload: DiscoveryReviewRequest,
        book_id: str | None = None,
    ) -> dict[str, Any]:
        prop = get_discovery_proposal(app.state.db_path, proposal_id)
        if not prop:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đề xuất",
            )
        if book_id is not None and prop.get("book_id") != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Đề xuất không thuộc về bộ sách này",
            )
        action = payload.action.strip().lower()
        if action == "approve":
            try:
                res = approve_discovery_proposal(
                    app.state.db_path,
                    proposal_id=proposal_id,
                    override_source=payload.override_source,
                    override_target=payload.override_target,
                    override_aliases=payload.override_aliases,
                    override_gender=payload.override_gender,
                    override_speech_style=payload.override_speech_style,
                    override_notes=payload.override_notes,
                    override_category=payload.override_category,
                )
                return res
            except ValueError as e:
                msg = str(e)
                if "đã bị khóa" in msg.lower() or "locked" in msg.lower():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=msg,
                    )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=msg,
                )
        elif action == "reject":
            return reject_discovery_proposal(app.state.db_path, proposal_id)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Hành động không hợp lệ: {payload.action}",
            )

    # -------------------------------------------------------------------------
    # Prompt Presets and Few-Shot Examples API
    # -------------------------------------------------------------------------
    @app.get("/api/books/{book_id}/presets")
    async def get_book_presets_endpoint(book_id: str) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return list_prompt_presets(app.state.db_path, book_id)

    @app.post("/api/books/{book_id}/presets", status_code=status.HTTP_201_CREATED)
    async def create_book_preset_endpoint(
        book_id: str, payload: PromptPresetCreateRequest
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        try:
            return create_prompt_preset(
                app.state.db_path,
                book_id=book_id,
                name=payload.name,
                description=payload.description,
                system_prompt=payload.system_prompt,
                user_prompt_template=payload.user_prompt_template,
                review_system_prompt=payload.review_system_prompt,
                review_user_prompt_template=payload.review_user_prompt_template,
                few_shot_examples=payload.few_shot_examples,
                is_default=payload.is_default,
            )
        except ValueError as ve:
            msg = str(ve)
            if "đã tồn tại" in msg.lower() or "already exists" in msg.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=msg,
            )

    @app.get("/api/presets/{preset_id}")
    async def get_preset_endpoint(preset_id: str) -> dict[str, Any]:
        preset = get_prompt_preset(app.state.db_path, preset_id)
        if not preset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy cấu hình prompt",
            )
        return preset

    @app.patch("/api/presets/{preset_id}")
    async def update_preset_endpoint(
        preset_id: str, payload: PromptPresetPatchRequest
    ) -> dict[str, Any]:
        preset = get_prompt_preset(app.state.db_path, preset_id)
        if not preset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy cấu hình prompt",
            )
        try:
            return update_prompt_preset(
                app.state.db_path,
                preset_id=preset_id,
                name=payload.name,
                description=payload.description,
                system_prompt=payload.system_prompt,
                user_prompt_template=payload.user_prompt_template,
                review_system_prompt=payload.review_system_prompt,
                review_user_prompt_template=payload.review_user_prompt_template,
                few_shot_examples=payload.few_shot_examples,
                is_default=payload.is_default,
            )
        except ValueError as ve:
            msg = str(ve)
            if "đã tồn tại" in msg.lower() or "already exists" in msg.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=msg,
            )

    @app.delete("/api/presets/{preset_id}")
    async def delete_preset_endpoint(preset_id: str) -> dict[str, Any]:
        preset = get_prompt_preset(app.state.db_path, preset_id)
        if not preset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy cấu hình prompt",
            )
        try:
            delete_prompt_preset(app.state.db_path, preset_id)
            return {"status": "deleted", "id": preset_id}
        except ValueError as ve:
            msg = str(ve)
            if "đã tồn tại" in msg.lower() or "already exists" in msg.lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=msg,
            )

    @app.post("/api/presets/{preset_id}/default")
    async def set_default_preset_endpoint(preset_id: str) -> dict[str, Any]:
        preset = get_prompt_preset(app.state.db_path, preset_id)
        if not preset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy cấu hình prompt",
            )
        return set_default_prompt_preset(app.state.db_path, preset_id)

    # Story Bible
    @app.get("/api/books/{book_id}/bible")
    async def get_bible(book_id: str) -> dict[str, Any]:
        bible = get_story_bible(app.state.db_path, book_id)
        if bible is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return {"book_id": book_id, "text": bible, "story_bible": bible}

    @app.put("/api/books/{book_id}/bible")
    async def put_bible(book_id: str, payload: StoryBibleRequest) -> dict[str, Any]:
        if has_running_job(app.state.db_path, book_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có tiến trình dịch đang chạy, không thể sửa story bible",
            )

        try:
            updated_text = update_story_bible(
                app.state.db_path, book_id, payload.get_text()
            )
            return {"book_id": book_id, "text": updated_text, "story_bible": updated_text}
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )

    # Analyze
    @app.post("/api/books/{book_id}/analyze")
    async def analyze_book(book_id: str) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )

        if has_running_job(app.state.db_path, book_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có tiến trình dịch đang chạy, không thể phân tích",
            )

        chapters = get_book_chapters(app.state.db_path, book_id)
        if not chapters:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Sách không có chương nào để phân tích",
            )

        first_chap = get_chapter(app.state.db_path, chapters[0]["id"])
        if not first_chap or not first_chap.get("segments"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Chương đầu tiên không có nội dung phân đoạn văn bản",
            )

        # Bounded <= 30 segments / ~6000 chars from first chapter
        selected_segments: list[dict[str, Any]] = []
        char_count = 0
        for seg in first_chap["segments"]:
            if seg.get("is_whitespace"):
                continue
            src = seg.get("source", "")
            if len(selected_segments) >= 30 or (char_count + len(src) > 6000 and selected_segments):
                break
            if len(src) > 6000 - char_count:
                if not selected_segments:
                    selected_segments.append({**seg, 'source': src[:6000]})
                break
            selected_segments.append(seg)
            char_count += len(src)

        prov = app.state.provider
        prov_owned = False
        if prov is None:
            gemini_key, sec_key, tert_key = get_gemini_api_keys()
            if not gemini_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Chưa cấu hình API key (GEMINI_API_KEY) để thực hiện phân tích",
                )
            prov = GeminiProvider(
                api_key=gemini_key,
                secondary_api_key=sec_key,
                tertiary_api_key=tert_key,
                model=app_settings.default_model,
            )
            prov_owned = True

        try:
            res = await prov.analyze(selected_segments)
        except Exception as e:
            secrets_to_sanitize = getattr(prov, "all_secrets", getattr(prov, "api_key", None))
            clean_err = sanitize_secret(str(e), secrets_to_sanitize)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Lỗi khi phân tích chương truyện: {clean_err}",
            )
        finally:
            if prov_owned:
                await prov.close()

        # Save story bible without destroying existing notes
        new_bible = res.get("story_bible", "").strip()
        if new_bible:
            curr_bible = get_story_bible(app.state.db_path, book_id) or ""
            combined_bible = f"{curr_bible}\n\n{new_bible}".strip() if curr_bible else new_bible
            update_story_bible(app.state.db_path, book_id, combined_bible)
        else:
            combined_bible = get_story_bible(app.state.db_path, book_id) or ""

        return {
            "book_id": book_id,
            "text": combined_bible,
            "story_bible": combined_bible,
            "suggested_count": 0,
        }

    # Jobs Management
    @app.post("/api/books/{book_id}/jobs", status_code=status.HTTP_201_CREATED)
    async def create_book_job(
        book_id: str, payload: CreateJobRequest | None = None
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )

        if has_active_job(app.state.db_path, book_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có tác vụ dịch đang thực hiện hoặc chờ xử lý",
            )

        book_chapters = get_book_chapters(app.state.db_path, book_id)
        if not book_chapters:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Sách không có chương nào để dịch",
            )
        valid_chap_ids = {c["id"] for c in book_chapters}

        # Resolve chapter_ids
        req_chaps = payload.chapter_ids if payload else None
        if req_chaps is not None:
            if len(req_chaps) == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Danh sách chapter_ids không được rỗng",
                )
            if len(req_chaps) != len(set(req_chaps)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Phát hiện mã chương trùng lặp trong chapter_ids",
                )
            foreign_ids = set(req_chaps) - valid_chap_ids
            if foreign_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Mã chương không thuộc quyển sách này: {sorted(foreign_ids)}",
                )
            effective_chapter_ids = req_chaps
        else:
            effective_chapter_ids = [c["id"] for c in book_chapters]

        # Resolve params and snapshot model
        persisted_model = get_setting(app.state.db_path, "model")
        req_model = None
        if payload:
            if payload.model:
                req_model = payload.model
            elif payload.params and payload.params.model:
                req_model = payload.params.model

        if req_model:
            try:
                current_model = validate_model_name(req_model)
            except ProviderModelError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        else:
            try:
                current_model = validate_model_name(persisted_model) if persisted_model else app_settings.default_model
            except ProviderModelError:
                current_model = app_settings.default_model

        prov = app.state.provider
        if prov is not None and hasattr(prov, "is_model_supported") and callable(prov.is_model_supported):
            if not prov.is_model_supported(current_model):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Mô hình '{current_model}' không được hỗ trợ bởi provider hiện tại.",
                )

        review = False
        max_segments = None
        if payload:
            if payload.params:
                review = payload.params.review
                max_segments = payload.params.max_segments
            if payload.review is not None:
                review = payload.review
            if payload.max_segments is not None:
                max_segments = payload.max_segments

        if max_segments is not None and max_segments <= 0:
            raise HTTPException(status_code=400, detail="max_segments phải lớn hơn 0")

        preset_id = None
        if payload:
            if payload.preset_id:
                preset_id = payload.preset_id
            elif payload.params and payload.params.preset_id:
                preset_id = payload.params.preset_id

        if preset_id:
            preset_row = get_prompt_preset(app.state.db_path, preset_id)
            if not preset_row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Cấu hình prompt '{preset_id}' không tồn tại",
                )
            if preset_row["book_id"] != book_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cấu hình prompt không thuộc về bộ sách này",
                )

        job_params = {
            "chapter_ids": effective_chapter_ids,
            "review": review,
            "max_segments": max_segments,
            "model": current_model,
            "preset_id": preset_id,
        }

        try:
            job = create_job(
                app.state.db_path,
                book_id=book_id,
                chapter_ids=effective_chapter_ids,
                params=job_params,
            )
            return job
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Lỗi khi tạo tác vụ dịch: {e}",
            )

    @app.get("/api/books/{book_id}/jobs")
    async def get_book_jobs(book_id: str) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return list_jobs(app.state.db_path, book_id)

    @app.get("/api/jobs/{job_id}")
    async def get_job_detail(job_id: str) -> dict[str, Any]:
        job = get_job(app.state.db_path, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy tác vụ",
            )
        return job

    @app.post("/api/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> dict[str, Any]:
        job = get_job(app.state.db_path, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy tác vụ",
            )

        cur_status = job["status"]
        if cur_status == "running":
            # Intent stored as 'pausing', in-flight batch finishes and transitions to 'paused'
            return update_job_status(app.state.db_path, job_id, "pausing")
        elif cur_status == "queued":
            return update_job_status(app.state.db_path, job_id, "paused")
        elif cur_status in ("pausing", "paused"):
            return job
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Không thể tạm dừng tác vụ có trạng thái '{cur_status}'",
            )

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> dict[str, Any]:
        job = get_job(app.state.db_path, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy tác vụ",
            )

        cur_status = job["status"]
        if cur_status == "paused":
            # Ensure no overlapping live jobs
            active = has_active_job(app.state.db_path, job["book_id"])
            if active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Đang có tác vụ khác đang chạy cho cùng quyển sách",
                )
            return update_job_status(app.state.db_path, job_id, "queued")
        elif cur_status == "pausing":
            return update_job_status(app.state.db_path, job_id, "running")
        elif cur_status in ("queued", "running"):
            return job
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Không thể tiếp tục tác vụ có trạng thái '{cur_status}'",
            )

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str) -> dict[str, Any]:
        job = get_job(app.state.db_path, job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy tác vụ",
            )

        if job["status"] != "failed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Chỉ có thể thử lại tác vụ bị thất bại",
            )

        if has_active_job(app.state.db_path, job["book_id"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Đang có tác vụ khác đang chạy hoặc chờ xử lý cho cùng quyển sách",
            )

        return update_job_status(app.state.db_path, job_id, "queued", error=None)

    # Export
    @app.head("/api/books/{book_id}/export")
    async def check_export(book_id: str) -> Response:
        book = get_book(app.state.db_path, book_id)
        if not book or not (Path(book['file_path']).is_file() or (app.state.data_dir / f'{book_id}.epub').is_file()):
            raise HTTPException(status_code=404, detail="Không tìm thấy EPUB gốc")
        return Response(status_code=200)

    @app.get("/api/books/{book_id}/export")
    async def export_book(book_id: str) -> Response:
        try:
            book, translations = get_export_bundle(app.state.db_path, book_id)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )

        file_path = Path(book["file_path"])
        if not file_path.is_file():
            # A persisted data directory may be moved or mounted inside Docker.
            file_path = app.state.data_dir / f'{book_id}.epub'
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy tập tin EPUB gốc trên máy chủ",
            )

        with open(file_path, "rb") as f:
            raw_bytes = f.read()

        try:
            rebuilt = rebuild_epub(
                raw_bytes,
                translations=translations,
                target_language="vi",
                update_opf_metadata=True,
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Lỗi khi tái tạo EPUB: {e}",
            )

        raw_title = book.get("title") or "book"
        safe_title = re.sub(r"[^\w\s-]", "", raw_title).strip()
        safe_title = safe_title.replace(" ", "_") or "book"
        ascii_fallback = "book_vi.epub"
        utf8_filename = f"{safe_title}_vi.epub"
        encoded_filename = urllib.parse.quote(utf8_filename)

        return Response(
            content=rebuilt,
            media_type="application/epub+zip",
            headers={
                "Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_filename}",
            },
        )

    @app.post("/api/books/{book_id}/export/pdf")
    async def export_book_pdf(
        book_id: str, payload: PdfExportRequest = PdfExportRequest()
    ) -> Response:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )

        if payload.content not in ("translation", "source"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="content phải là 'translation' hoặc 'source'",
            )

        all_chapters = get_book_chapters(app.state.db_path, book_id)
        if not all_chapters:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Sách không có chương nào để xuất",
            )

        all_chap_map = {c["id"]: c for c in all_chapters}

        # Resolve chapter_ids
        if payload.chapter_ids is not None:
            if len(payload.chapter_ids) == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Danh sách chapter_ids không được rỗng",
                )
            if len(payload.chapter_ids) != len(set(payload.chapter_ids)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Phát hiện mã chương trùng lặp trong chapter_ids",
                )
            foreign_ids = set(payload.chapter_ids) - set(all_chap_map.keys())
            if foreign_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Mã chương không thuộc quyển sách này: {sorted(foreign_ids)}",
                )
            selected_ids = set(payload.chapter_ids)
        else:
            selected_ids = set(all_chap_map.keys())

        # Ensure stable spine reading order regardless of input ordering
        selected_chapters = [c for c in all_chapters if c["id"] in selected_ids]

        # Fetch full chapter details including segments for each selected chapter
        chapters_with_segments: list[dict[str, Any]] = []
        for c in selected_chapters:
            chap_detail = get_chapter(app.state.db_path, c["id"])
            if chap_detail:
                chapters_with_segments.append(chap_detail)

        # In translation mode, strictly reject incomplete chapters
        if payload.content == "translation":
            incomplete = check_translation_completeness(chapters_with_segments)
            if incomplete:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Không thể xuất PDF bản dịch: Phát hiện {len(incomplete)} chương chưa hoàn thành dịch "
                        f"({'; '.join(incomplete)}). Bản dịch tiếng Việt không được phép bỏ sót câu văn hoặc "
                        "tự động thay thế bằng tiếng Hàn."
                    ),
                )

        # Offload CPU-intensive ReportLab rendering to threadpool
        try:
            pdf_bytes = await asyncio.to_thread(
                generate_pdf_export,
                book=book,
                chapters_with_segments=chapters_with_segments,
                content_mode=payload.content,
            )
        except PdfFontError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            )
        except IncompleteChapterError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except PdfExportError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Lỗi khi tạo PDF: {e}",
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Lỗi hệ thống khi tạo PDF: {e}",
            )

        raw_title = book.get("title") or "book"
        safe_title = re.sub(r"[^\w\s-]", "", raw_title).strip()
        safe_title = safe_title.replace(" ", "_") or "book"
        lang_suffix = "vi" if payload.content == "translation" else "ko"
        ascii_fallback = f"book_{lang_suffix}.pdf"
        utf8_filename = f"{safe_title}_{lang_suffix}.pdf"
        encoded_filename = urllib.parse.quote(utf8_filename)

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_filename}",
            },
        )

    # -------------------------------------------------------------------------
    # Consistency Audit Endpoints
    # -------------------------------------------------------------------------
    def _get_audit_manager() -> AuditManager:
        if getattr(app.state, "audit_manager", None) is None:
            app.state.audit_manager = AuditManager(
                app.state.db_path,
                provider_getter=lambda: app.state.provider,
            )
        return app.state.audit_manager

    @app.post(
        "/api/books/{book_id}/consistency-reviews",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_consistency_audit(
        book_id: str,
        payload: ConsistencyAuditCreateRequest | None = None,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )

        if book.get("translated_segments", 0) <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Sách chưa có phân đoạn dịch nào để rà soát tính nhất quán.",
            )

        if has_active_audit_run(app.state.db_path, book_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có một tiến trình rà soát nhất quán đang thực hiện hoặc chờ xử lý.",
            )

        persisted_model = get_setting(app.state.db_path, "model")
        req_model = payload.model if payload and payload.model else None
        candidate_model = req_model or persisted_model or get_settings(app.state.data_dir).default_model
        try:
            valid_model = validate_model_name(candidate_model)
        except ProviderModelError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

        run = create_audit_run(app.state.db_path, book_id, model=valid_model)
        mgr = _get_audit_manager()
        mgr.start_audit(run["id"])

        return run

    @app.get("/api/books/{book_id}/consistency-reviews", response_model=list[dict[str, Any]])
    async def get_book_consistency_reviews(book_id: str) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        return list_audit_runs(app.state.db_path, book_id)
    @app.get(
        "/api/books/{book_id}/consistency-reviews/snapshots",
        response_model=list[dict[str, Any]],
    )
    async def get_book_correction_snapshots_endpoint(
        book_id: str,
    ) -> list[dict[str, Any]]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy sách")
        from backend.app.db import get_correction_snapshots
        return get_correction_snapshots(app.state.db_path, book_id)

    @app.post(
        "/api/books/{book_id}/consistency-reviews/snapshots/{snapshot_id}/rollback",
        response_model=dict[str, Any],
    )
    async def rollback_correction_snapshot_endpoint(
        book_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy sách")

        from backend.app.corrections import rollback_snapshot

        try:
            return rollback_snapshot(app.state.db_path, snapshot_id)
        except KeyError as ke:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ke))
        except ValueError as ve:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))

    @app.get("/api/books/{book_id}/consistency-reviews/{run_id}")
    async def get_book_consistency_review_status(
        book_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đợt rà soát nhất quán cho cuốn sách này.",
            )
        return run

    @app.get("/api/books/{book_id}/consistency-reviews/{run_id}/findings")
    async def get_book_consistency_review_findings(
        book_id: str,
        run_id: str,
        page: int = 1,
        page_size: int = 20,
        category: str | None = None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đợt rà soát nhất quán cho cuốn sách này.",
            )
        return get_audit_findings(
            app.state.db_path,
            run_id=run_id,
            page=page,
            page_size=page_size,
            category=category,
            severity=severity,
        )

    @app.post("/api/books/{book_id}/consistency-reviews/{run_id}/cancel")
    async def cancel_book_consistency_review(
        book_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đợt rà soát nhất quán cho cuốn sách này.",
            )
        if run["status"] in ("completed", "failed", "cancelled"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Không thể hủy đợt rà soát có trạng thái '{run['status']}'.",
            )
        mgr = _get_audit_manager()
        mgr.cancel_audit(run_id)
        updated = get_audit_run(app.state.db_path, run_id)
        return updated or {"status": "cancelled", "run_id": run_id}

    @app.post(
        "/api/books/{book_id}/consistency-reviews/{run_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_book_consistency_review(
        book_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đợt rà soát nhất quán cho cuốn sách này.",
            )
        if run["status"] in ("running", "queued", "pausing"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Đợt rà soát đang hoạt động (trạng thái '{run['status']}').",
            )

        if has_active_audit_run(app.state.db_path, book_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Sách đang có một đợt rà soát khác đang chạy.",
            )

        update_audit_run(app.state.db_path, run_id, status="queued", clear_error=True)
        mgr = _get_audit_manager()
        mgr.start_audit(run_id)

        updated = get_audit_run(app.state.db_path, run_id)
        return updated or run

    @app.post(
        "/api/books/{book_id}/consistency-reviews/{run_id}/semantic-review",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_book_semantic_review(
        book_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đợt rà soát nhất quán cho cuốn sách này.",
            )

        mgr = _get_audit_manager()
        mgr.start_semantic_review(run_id)
        updated = get_audit_run(app.state.db_path, run_id)
        return updated or run

    @app.post("/api/books/{book_id}/consistency-reviews/{run_id}/semantic-cancel")
    async def cancel_book_semantic_review(
        book_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy sách",
            )
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy đợt rà soát nhất quán cho cuốn sách này.",
            )

        mgr = _get_audit_manager()
        mgr.cancel_semantic_review(run_id)
        updated = get_audit_run(app.state.db_path, run_id)
        return updated or run

    @app.post(
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/generate-preview",
        response_model=dict[str, Any],
    )
    async def generate_corrections_preview_endpoint(
        book_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Không tìm thấy sách")
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(status_code=404, detail="Không tìm thấy đợt rà soát nhất quán.")

        # Reject active, stale, or incomplete runs
        if run.get("status") in ("running", "queued", "pausing"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Không thể tạo đề xuất sửa đổi cho đợt rà soát đang chạy (trạng thái '{run['status']}').",
            )
        if run.get("is_stale"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Không thể tạo đề xuất sửa đổi cho đợt rà soát đã cũ (stale).",
            )
        progress = run.get("progress") or {}
        semantic_stage = progress.get("semantic_stage")
        if semantic_stage is not None:
            norm_semantic_stage = str(semantic_stage).strip().lower()
            semantic_cov = str(progress.get("semantic_coverage") or "").strip().lower()
            if norm_semantic_stage != "completed":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Không thể tạo đề xuất sửa đổi khi giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (trạng thái '{semantic_stage}').",
                )
            if semantic_cov != "full":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Không thể tạo đề xuất sửa đổi khi giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (độ bao phủ '{progress.get('semantic_coverage')}').",
                )
        is_full = (
            run.get("status") == "completed"
            and run.get("failed_segments", 0) == 0
            and run.get("skipped_segments", 0) == 0
            and run.get("total_segments", 0) > 0
            and run.get("processed_segments", 0) == run.get("total_segments", 0)
            and progress.get("coverage_status") == "full"
        )
        if not is_full:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Không thể tạo đề xuất sửa đổi cho đợt rà soát chưa hoàn tất độ bao phủ toàn diện (incomplete/partial).",
            )

        from backend.app.corrections import generate_correction_proposals

        try:
            res = generate_correction_proposals(
                db_path=app.state.db_path,
                run_id=run_id,
                book_id=book_id,
            )
            return res
        except ValueError as ve:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))

    @app.get(
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/preview",
        response_model=dict[str, Any],
    )
    async def get_corrections_preview_endpoint(
        book_id: str,
        run_id: str,
        classification: str | None = Query(default=None),
        category: str | None = Query(default=None),
        status_filter: str | None = Query(default=None, alias="status"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Không tìm thấy sách")
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(status_code=404, detail="Không tìm thấy đợt rà soát nhất quán.")

        from backend.app.db import get_correction_proposals
        from backend.app.corrections import generate_correction_proposals

        progress = run.get("progress") or {}
        semantic_stage = progress.get("semantic_stage")
        if semantic_stage is not None:
            norm_stage = str(semantic_stage).strip().lower()
            semantic_cov = str(progress.get("semantic_coverage") or "").strip().lower()
            is_semantic_blocked = (norm_stage != "completed" or semantic_cov != "full")
        else:
            is_semantic_blocked = False
        is_full = (
            run.get("status") == "completed"
            and not run.get("is_stale")
            and run.get("failed_segments", 0) == 0
            and run.get("skipped_segments", 0) == 0
            and run.get("total_segments", 0) > 0
            and run.get("processed_segments", 0) == run.get("total_segments", 0)
            and progress.get("coverage_status") == "full"
            and not is_semantic_blocked
        )

        res = get_correction_proposals(
            app.state.db_path,
            run_id=run_id,
            classification=classification,
            category=category,
            status=status_filter,
            page=page,
            page_size=page_size,
        )

        # Lazy auto-generation only for completed, full-coverage runs
        if is_full and res["total"] == 0 and not classification and not category and not status_filter:
            try:
                generate_correction_proposals(app.state.db_path, run_id=run_id, book_id=book_id)
                res = get_correction_proposals(
                    app.state.db_path,
                    run_id=run_id,
                    classification=classification,
                    category=category,
                    status=status_filter,
                    page=page,
                    page_size=page_size,
                )
            except Exception as e:
                logger.warning("Proposal auto-generation deferred/skipped: %s", e)

        # Manual preview metadata: clear coverage notice and explicit can_apply flag
        res["is_complete"] = is_full
        res["is_stale"] = bool(run.get("is_stale"))
        res["run_status"] = run.get("status")
        res["can_apply"] = is_full

        if not is_full:
            if is_semantic_blocked:
                if str(semantic_stage).strip().lower() != "completed":
                    res["coverage_notice"] = (
                        f"Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (trạng thái '{semantic_stage}'). "
                        "Chỉ cho phép xem trước đối sánh, KHÔNG THỂ áp dụng sửa đổi."
                    )
                else:
                    res["coverage_notice"] = (
                        f"Giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (độ bao phủ '{progress.get('semantic_coverage')}'). "
                        "Chỉ cho phép xem trước đối sánh, KHÔNG THỂ áp dụng sửa đổi."
                    )
            elif run.get("status") in ("running", "queued", "pausing"):
                res["coverage_notice"] = f"Đợt rà soát đang hoạt động (trạng thái '{run.get('status')}'). Chỉ cho phép xem trước, không thể áp dụng sửa đổi."
            elif run.get("is_stale"):
                res["coverage_notice"] = "Đợt rà soát đã cũ (stale) do bản dịch của sách đã thay đổi. Chỉ cho phép xem trước, không thể áp dụng sửa đổi."
            else:
                proc = run.get("processed_segments", 0)
                tot = run.get("total_segments", 0)
                fail = run.get("failed_segments", 0)
                skip = run.get("skipped_segments", 0)
                res["coverage_notice"] = (
                    f"Đợt rà soát chưa hoàn tất ({proc}/{tot} phân đoạn hoàn tất, {fail} phân đoạn lỗi, {skip} phân đoạn bỏ qua). "
                    "Chỉ cho phép xem trước đối sánh, KHÔNG THỂ áp dụng sửa đổi để đảm bảo an toàn toàn thư."
                )
        else:
            res["coverage_notice"] = None

        return res

    @app.put(
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/proposals/{proposal_id}",
        response_model=dict[str, Any],
    )
    async def update_proposal_selection_endpoint(
        book_id: str,
        run_id: str,
        proposal_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        is_selected = bool(payload.get("is_selected", True))
        from backend.app.db import update_correction_proposal_selection
        res = update_correction_proposal_selection(app.state.db_path, proposal_id, is_selected)
        if not res:
            raise HTTPException(status_code=404, detail="Không tìm thấy đề xuất sửa đổi.")
        return res

    @app.post(
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/select-all",
        response_model=dict[str, Any],
    )
    async def batch_select_proposals_endpoint(
        book_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        is_selected = bool(payload.get("is_selected", True))
        only_auto_safe = bool(payload.get("only_auto_safe", False))
        from backend.app.db import batch_update_correction_proposals_selection
        count = batch_update_correction_proposals_selection(
            app.state.db_path,
            run_id=run_id,
            is_selected=is_selected,
            only_auto_safe=only_auto_safe,
        )
        return {"updated_count": count}

    @app.post(
        "/api/books/{book_id}/consistency-reviews/{run_id}/corrections/apply",
        response_model=dict[str, Any],
    )
    async def apply_corrections_endpoint(
        book_id: str,
        run_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        selected_ids = payload.get("selected_ids")
        only_auto_safe = bool(payload.get("only_auto_safe", False))

        book = get_book(app.state.db_path, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Không tìm thấy sách")
        run = get_audit_run(app.state.db_path, run_id)
        if not run or run["book_id"] != book_id:
            raise HTTPException(status_code=404, detail="Không tìm thấy đợt rà soát nhất quán.")

        # Reject active, stale, or incomplete runs regardless of selected_ids or auto_safe flags
        if run.get("status") in ("running", "queued", "pausing"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Không thể áp dụng sửa đổi cho đợt rà soát đang chạy (trạng thái '{run['status']}').",
            )
        if run.get("is_stale"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Không thể áp dụng sửa đổi cho đợt rà soát đã cũ (stale) do bản dịch của sách đã thay đổi.",
            )
        progress = run.get("progress") or {}
        semantic_stage = progress.get("semantic_stage")
        if semantic_stage is not None:
            norm_semantic_stage = str(semantic_stage).strip().lower()
            semantic_cov = str(progress.get("semantic_coverage") or "").strip().lower()
            if norm_semantic_stage != "completed":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Không thể áp dụng sửa đổi khi giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (trạng thái '{semantic_stage}').",
                )
            if semantic_cov != "full":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Không thể áp dụng sửa đổi khi giai đoạn thẩm định ngữ nghĩa chưa hoàn tất (độ bao phủ '{progress.get('semantic_coverage')}').",
                )
        is_full = (
            run.get("status") == "completed"
            and run.get("failed_segments", 0) == 0
            and run.get("skipped_segments", 0) == 0
            and run.get("total_segments", 0) > 0
            and run.get("processed_segments", 0) == run.get("total_segments", 0)
            and progress.get("coverage_status") == "full"
        )
        if not is_full:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Không thể áp dụng sửa đổi cho đợt rà soát chưa hoàn tất độ bao phủ toàn diện (incomplete/partial).",
            )

        from backend.app.corrections import apply_proposals_with_snapshot

        try:
            res = apply_proposals_with_snapshot(
                db_path=app.state.db_path,
                run_id=run_id,
                book_id=book_id,
                selected_ids=selected_ids,
                only_auto_safe=only_auto_safe,
            )
            return res
        except ValueError as ve:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))

    # Mount frontend/dist at / after API routes
    frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> Response:
            # Never swallow /api 404
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(status_code=404, detail="Không tìm thấy đường dẫn API")

            target_file = (frontend_dist / full_path).resolve()
            if target_file.is_file() and target_file.is_relative_to(frontend_dist):
                return FileResponse(target_file)

            index_file = frontend_dist / "index.html"
            if index_file.is_file():
                return FileResponse(index_file)

            raise HTTPException(status_code=404, detail="Không tìm thấy tài nguyên")

    return app


app = create_app(start_worker=True)
