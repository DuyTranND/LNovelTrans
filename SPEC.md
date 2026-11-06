# EPUB Translator

Build a complete self-hosted Korean to Vietnamese EPUB translation workbench.
GPT is architect/reviewer; Antigravity CLI gemini-3.8-flash-high implements scoped milestones.
The application uses GEMINI_API_KEY from .env only for book translation and related story analysis/review. Never read, display, log or commit credentials in coding tasks.

## Architecture
Python 3.11+ FastAPI backend, SQLite persistence, lxml for safe XML/EPUB DOM parsing, httpx Gemini REST provider, React + Vite frontend. FastAPI serves compiled frontend at localhost:8000. Docker Compose with persistent /data volume. One durable worker processes chapter jobs sequentially, restart recovers unfinished work. Backend is authoritative; frontend polls progress.

## Features
Upload EPUB, use container.xml -> OPF manifest -> spine order, preview source chapters, story bible, contextual translation, reviewer and corrections, chapter summaries and translation memory, persistent progress, pause/resume/retry, rebuild EPUB preserving original CSS/resources/TOC/images/inline markup.
User requirement: "1 lần dịch là dịch cả 1 chương... không đưa 1 câu gốc rồi 1 câu dịch". One-click "Dịch chương" translates ALL pending text in the selected chapter; provider batching is invisible and automatic. The chapter-scoped chronology completes the chapter translation phase, then executes a single review pass over the translated content, followed by chapter narrative summary.
Gemini receives identified text segments and bounded context, never raw XHTML. Return validated structured JSON preserving segment IDs. Context contains rules, relevant story bible, recent summaries, current batch and surrounding text. Translation and review must preserve meaning, completeness, names, terminology, voice, pronouns; no additions/omissions. Translation batching must obey provider size limits. Failed API requests never mark chapter done.
Allow model configuration in UI/backend with default model gemini-3.5-flash-lite for REST translation and review, discover provider models, keep API key server-side. Mock provider for deterministic tests, live test only a few short segments. Never translate all 157 sample chapters during development. Live Gemini generation tests previously encountered 503/timeout and remain unverified.

## EPUB safeguards
No filename sorting. Validate ZIP names and limits, no Zip Slip or unsafe XML entity resolution. Preserve inline DOM through indexed text/tail nodes; never destroy tags. Preview must be safe against uploaded active HTML. Rebuild starts from original ZIP, writes mimetype first uncompressed, translates only selected nodes and preserves untouched resources. Keep nav and NCX valid, translate titles when matched; metadata language vi for translated output.

## Persistence and concurrency
Store books, chapter source documents, segments, story bible, summaries, jobs, review state, errors. SQLite transactions and one worker. Pause stops at safe batch boundary and persists; resume reuses completed segments. Restart recovers active jobs without duplicating translated work. Editor writes must not race running translation: reject edits to active chapter or require paused state. Retry targeted failed chapter. Rebuild only includes saved translations and reports remaining untranslated chapters.

## UI & Reading Experience
Vietnamese interface, product name Trạm Dịch. White background, dark navy text, cobalt action buttons, slate borders, subtle blue-gray sidebar.
The primary reading UI is a continuous WHOLE CHAPTER Vietnamese document formatted as novel prose with 18px / 1.8 typography, paragraph spacing, heading treatment, and preserved text order — not paired numbered source/translation cards per sentence/segment.
No "Đoạn 1 / Bản dịch 1" labels or empty textareas/placeholders for missing segments. Missing translation displays a single chapter-level notice. Partial chapters are clearly labeled and show only saved translations in original order, with no invented translations or implicit Korean fallback in Vietnamese prose.
Source view is a separate full-chapter document selectable via clear Vietnamese controls ("Bản dịch (Tiếng Việt)" vs "Bản gốc (Tiếng Hàn)"), using a full document column rather than side-by-side split rows.
An explicit "Chỉnh sửa" mode enables paragraph/segment editing without dominating reading mode. Segment grouping joins adjacent DOM text/tail nodes belonging to the same paragraph to prevent inline formatting tags from breaking text across lines. Draft edits are immediately reflected in reading mode when toggled. Unsaved changes are protected by dirty state tracking and navigation guards.
Toolbar: Dịch chương / Tạm dừng / Tiếp tục / Xuất EPUB; Tabs: Bản dịch / Story Bible / Công việc. Explicit loading/error/empty states, responsive stacking on mobile.

## Acceptance
Real sample at ../Comic/아포칼립스 시스템으로 착각당했다.epub, 157 spine entries. Test upload, order, preview, provider validation, review, editor, persistence, worker pause/resume/restart/retry, rebuilt EPUB roundtrip with all resources. Docker compose up -d --build serves localhost:8000. Test real API narrowly and report if credentials/provider prevent it. Do not claim validation that was not executed.
