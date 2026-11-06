# Implementation progress

## Local review build — 07/11/2026

- Work branch: `fix/release-readiness`; `main` and `develop` remain unchanged pending review.
- Backend independently verified in a source-only copy without `.env`, with fresh `DATA_DIR`/pytest basetemp: **373 passed, 6 skipped**. Includes production account validation, session revocation/demotion, real two-connection SQLite races, duplicate-ID atomicity, actor-attributed manual edit history and CAS restore. Background translation/review and correction apply/rollback now advance revision on changes so stale editor drafts cannot overwrite their results.
- Frontend: **43 passed**, production build succeeds. Restored glossary navigation/CRUD, role capabilities across screens, structured API errors, explicit per-segment conflict choices preserving drafts, and admin history/confirmed restore. Mocked Playwright checks pass login/logout failures, viewer restrictions, second-save revision, restore conflict/success/local override, desktop tab scrolling and mobile drawer/no overflow. Browser plugin was absent; headless Playwright used, all API routes intercepted.
- Live loopback web: `/`, `/api/health`, `/api/books` and chapter history return HTTP 200. Preserved 1 book, 157 chapters and 46,637 translated segments; read-only comparison against pre-change backup found **0 source/original_source/translation differences**. Jobs remain 2 completed and 2 failed; no new translation jobs or live Gemini translation requests were made.
- Saved-content export smoke: EPUB ZIP valid with 161 resources; first translated chapter PDF valid with 12 pages. Output is local and excluded from Git.
- Consistent SQLite backup and referenced EPUB copy stored under `output/backups/release-20261106`, verified with integrity check, row counts, reopened copy and EPUB hashes. This is a local backup, not an offsite backup or cloud cutover.
- Vercel research and setup steps are in `docs/HOSTING_PIPELINE.md`: frontend can use Vercel while the current SQLite/files/background worker require a persistent backend service. No Vercel project/domain or cloud data migration was created.
- Still external to local acceptance: Docker runtime/image verification, cloud staging, offsite backup, production cutover, GitHub branch protection and remote CI verification. Do not describe the local review build as a completed public deployment.

- [x] 1. Safe EPUB parser/rebuilder + regression tests
- [x] 2. SQLite schema, repository, upload/preview/editor API
- [x] 3. Gemini provider, context, story bible, glossary, review
- [x] 4. Durable chapter worker with pause/resume/retry/restart
- [x] 5. React workbench UI
- [ ] 6. Docker, integrated verification, real API/EPUB smoke

Each milestone is delegated separately and reviewed by GPT. No coding worker may read .env. API keys stay exclusively in the application process.

## Verification

- Backend: 92 tests pass, including real 157-chapter parsing, malformed provider output, glossary lock enforcement after review, three-segment cap, atomic batch writes, pause/resume/restart, summary retry without retranslating, bounded story analysis, source-edit invalidation, and regression verification that 1-click chapter translation automatically processes multiple batches (>40 segments) to completion. Chapter chronology updated to complete whole-chapter translation phase before running one review pass and summary. Default translation/review model set to gemini-3.5-flash-lite.
- Frontend: production build succeeds; all 6 paragraph grouping tests pass. Continuous whole-chapter novel prose reading UI implemented as primary view, with separate Korean source view, explicit "Chỉnh sửa" editor mode, safe DOM text/tail node grouping, and persistent dirty/save protection. Mobile tested with responsive typography and drawer. Jobs tab displays truthful progress for whole chapter vs capped smoke jobs and visible stages.
- Process restart retains all 157 chapters, three manually edited translations, locked glossary, Story Bible, and the failed live job.
- Downloaded EPUB roundtrip: 157 spine entries, 161 resources, Vietnamese metadata, translated matching NCX label, correct mimetype entry, no ZIP CRC errors. Only edited chapter, OPF and NCX changed; all other resources are byte-identical.
- Live Gemini model discovery succeeded. Bounded live translation smoke on 3 segments succeeded with gemini-3.5-flash-lite in 3.59s (saved to data/live-smoke.json). Supervisor verified scripts/live_smoke.py --model gemini-3.5-flash-lite --review successfully on the same 3 segments, with valid review issues and corrections. Workflow uses gemini-3.5-flash-lite for one translation pass followed by one review pass per chapter.
- Dockerfile and Compose are provided. Docker is not installed on this host, so an actual image build/container launch remains unverified.

- Final local runtime: HTTP 200, configured gemini-3.5-flash-lite, API key configured without disclosure, existing 157-chapter book and 3 saved translations retained. Browser automation could not be repeated this session because its sandbox helper failed to initialize; previous desktop/mobile reading QA remains the last visual check.

- Full live chapter 1 verified: job f44db86b228e468592db11d72b6c69d0 completed with all 328 non-whitespace segments translated and reviewed using gemini-3.5-flash-lite; source unchanged. One scene-break **** was preserved via chapter content API after Gemini returned empty. Compatible simple JSON schema retained; bounded missing-ID-only recovery added by Antigravity. Full text and EPUB exported to data/.

## PDF export verification (2026-10-08)

- Implemented by Antigravity CLI and reviewed by Codex: select book, current chapter, custom chapter selection, or all chapters; export saved Vietnamese translation or Korean source. Incomplete translations reject export with a readable error. Export does not call Gemini.
- Bundled licensed Noto fonts cover Vietnamese, Korean, Thai and combining characters. A4 PDF includes chapter headings and page numbers; chapters follow EPUB spine order.
- Backend: 107 tests pass. Frontend: 10 tests pass; production build succeeds.
- Isolated Playwright Chrome QA passes desktop/mobile layout, content option bounds, book-change selection reset, focus trap/Escape, real Vietnamese chapter download, incomplete whole-book rejection, and multi-chapter source download. No unexpected console or page errors. Browser plugin/CUA initialization was unavailable; isolated Playwright fallback used.
- Real original-book PDF includes all 157 chapter headings in spine order (1513 pages). Final chapter 1 Vietnamese PDF has 12 pages; rendered first and last pages checked for legibility and correct Vietnamese accents. Example: output/pdf/chuong-1-thuc-tinh.pdf.
- 2026-10-08: Antigravity implemented strict safe trailing-colon translation ID normalization. Exact expected IDs take precedence; only one appended ASCII colon can be corrected to an exact requested ID. Canonical collisions, unknown IDs and empty output still reject. Supervisor verified 118 backend tests pass, including initial and targeted-recovery paths; idle local backend restarted and HTTP 200 confirmed. Saved progress retained; no live translation job retried automatically.
- 2026-10-08: Antigravity changed translation and missing-ID recovery target prompts to explicit JSON id/source objects, made neighbor context read-only without target IDs, versioned prompt cache context, and added one bounded original-batch re-request for initial ID validation errors. Strict unknown/duplicate/empty validation retained; no fuzzy matching or source-derived ID truncation. Supervisor verified 126 backend tests pass (including literal malformed-ID report and fixed retry bounds), restarted idle local backend, HTTP 200, retained 930 saved progress. No real Gemini job run during verification.
- 2026-10-08: Antigravity added safe symbol-only source classification (Unicode P/S and whitespace, no letters/numbers/control content), verbatim fallback for empty or omitted symbolic translations, and empty symbolic review correction preservation. Unknown/duplicate ID validation remains strict. Supervisor verified 137 backend tests including end-to-end durable review pass, restarted idle backend, HTTP 200 confirmed. Saved2701 progress retained; no real Gemini job initiated during verification.

## Translation robustness audit (2026-10-08)

- User requested one proactive audit rather than repeated single-error fixes. Antigravity implemented; Codex reviewed and required repairs before deployment.
- Empty linguistic translations now recover only unresolved IDs while preserving valid response entries. Symbol-only fallback remains limited to punctuation/symbols and whitespace; ordinary language/numbers never fall back to source.
- Structured response-format, schema, ID and empty-content errors distinguish recoverable model output from auth/network/safety failures. Initial unsafe/malformed translation response allows one whole-batch re-request; unresolved IDs allow at most two targeted attempts. Review and summary malformed/empty output each allow one re-request. Existing transport retry policy is separate and bounded.
- Invalid translation/review caches are rejected and refreshed. Missing review cache fields cannot bypass review. Corrected review output still validates IDs, nonempty strings and issue fields before atomic writes.
- Regression checks cover malformed/duplicate/extra IDs, repeated empty output exhaustion, review repair preserving selected model, cache quarantine/fresh recovery, scene breaks, safety/finish/network behavior, summary retry without retranslating, pause/resume and reviewed-status durability.
- Supervisor validation: 153 backend tests and 10 frontend tests pass; production build succeeds. No real Gemini translation requests issued during verification. Idle backend restarted; HTTP 200 confirmed. All 157 chapters and 2701 saved translations retained. Failed job remains available for user Retry.
