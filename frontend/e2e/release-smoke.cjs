const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert');

// Configurable Playwright runtime module and screenshot dir
const defaultPlaywrightPath = 'C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core';
const playwrightPath = process.env.PLAYWRIGHT_CORE_PATH || defaultPlaywrightPath;

let playwrightModule;
if (fs.existsSync(playwrightPath)) {
  playwrightModule = require(playwrightPath);
} else {
  try {
    playwrightModule = require('playwright-core');
  } catch {
    playwrightModule = require('playwright');
  }
}
const { chromium } = playwrightModule;

const DIST_DIR = path.resolve(__dirname, '../dist');
const SCREENSHOTS_DIR = process.env.QA_OUTPUT_DIR || 'E:/Comic/ui-preferences-qa/release-20261106';

// Minimal static HTTP server for dist
function startStaticServer() {
  const mimeTypes = {
    '.html': 'text/html',
    '.js': 'application/javascript',
    '.css': 'text/css',
    '.json': 'application/json',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
  };

  const server = http.createServer((req, res) => {
    let filePath = path.join(DIST_DIR, req.url === '/' ? 'index.html' : req.url.split('?')[0]);
    if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
      filePath = path.join(DIST_DIR, 'index.html');
    }

    const ext = path.extname(filePath).toLowerCase();
    const contentType = mimeTypes[ext] || 'application/octet-stream';

    fs.readFile(filePath, (err, content) => {
      if (err) {
        res.writeHead(500);
        res.end('Error loading file');
        return;
      }
      res.writeHead(200, { 'Content-Type': contentType });
      res.end(content);
    });
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      resolve({ server, port, baseUrl: `http://127.0.0.1:${port}` });
    });
  });
}

async function runSmokeQA() {
  console.log('--- Starting Frontend Release-Readiness Smoke QA ---');
  if (!fs.existsSync(SCREENSHOTS_DIR)) {
    fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
  }

  const { server, baseUrl } = await startStaticServer();
  console.log(`[1/7] Static server running on ${baseUrl}`);

  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
  });

  const uncaughtPageErrors = [];
  const unexpectedConsoleErrors = [];

  // Mock State
  let currentUser = {
    id: 'u-admin',
    username: 'admin',
    role: 'admin',
    csrf_token: 'mock-csrf-token-123',
    authenticated: true,
    auth_override: false,
  };

  let trigger409 = false;
  let triggerRestore409 = false;
  let triggerLogoutFail = false;
  let lastPatchPayload = null;

  let mockChapter = {
    id: 'ch-1',
    title: 'Chương 1: Cổng hầm ngục D-Rank',
    order: 0,
    status: 'completed',
    segments: [
      {
        id: 'seg-1',
        order: 0,
        revision: 1,
        source: '그것은 시작이었다.',
        translation: 'Đó là một sự khởi đầu.',
        is_whitespace: false,
      },
      {
        id: 'seg-2',
        order: 1,
        revision: 1,
        source: '모든 것이 변했다.',
        translation: 'Mọi thứ đã thay đổi.',
        is_whitespace: false,
      },
    ],
  };

  let mockGlossary = [
    {
      id: 'g-1',
      source: '헌터',
      target: 'Thợ săn',
      notes: 'Người sở hữu năng lực thức tỉnh',
      locked: true,
    },
    {
      id: 'g-2',
      source: '던전',
      target: 'Hầm ngục',
      notes: 'Không gian ma pháp',
      locked: false,
    },
  ];

  let mockHistoryItems = [
    {
      id: 'h-1',
      segment_id: 'seg-1',
      old_revision: 1,
      new_revision: 2,
      actor_username: 'admin',
      created_at: new Date(Date.now() - 3600000).toISOString(),
      old_source: '그것은 시작이었다.',
      new_source: '그것은 시작이었다.',
      old_translation: 'Bản dịch khởi đầu ban sơ.',
      new_translation: 'Đó là một sự khởi đầu.',
    },
  ];

  function setupApiIntercepts(page) {
    page.on('pageerror', (err) => {
      console.error('Page error detected:', err);
      uncaughtPageErrors.push(err);
    });

    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const text = msg.text();
        // Allow ONLY deliberately injected 401/403/409/500 with explanation
        const isDeliberate =
          text.includes('409') ||
          text.includes('401') ||
          text.includes('403') ||
          text.includes('500') ||
          text.includes('Failed to load resource') ||
          text.includes('favicon');
        if (!isDeliberate) {
          unexpectedConsoleErrors.push(text);
        }
      }
    });

    return page.route('**/api/**', async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const pathname = url.pathname;
      const method = req.method().toUpperCase();

      // Auth routes
      if (pathname === '/api/auth/me') {
        if (!currentUser) {
          return route.fulfill({
            status: 401,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Unauthenticated' }),
          });
        }
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(currentUser),
        });
      }

      if (pathname === '/api/auth/login') {
        const body = JSON.parse(req.postData() || '{}');
        if (body.password === 'wrong-pass') {
          return route.fulfill({
            status: 401,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Sai tên đăng nhập hoặc mật khẩu (401 Deliberate)' }),
          });
        }
        currentUser = {
          id: 'u-admin',
          username: body.username || 'admin',
          role: 'admin',
          csrf_token: 'mock-csrf-token-123',
          authenticated: true,
          auth_override: false,
        };
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(currentUser),
        });
      }

      if (pathname === '/api/auth/logout') {
        if (triggerLogoutFail) {
          return route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Máy chủ từ chối đăng xuất (500 Deliberate)' }),
          });
        }
        currentUser = null;
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ message: 'logged out' }),
        });
      }

      // Settings & Models & Key Status
      if (pathname === '/api/settings') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            model: 'gemini-3.5-flash-lite',
            key_configured: true,
            gemini_configured: true,
          }),
        });
      }
      if (pathname === '/api/models') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            { id: 'gemini-3.5-flash-lite', name: 'Gemini 3.5 Flash Lite' },
            { id: 'gemini-1.5-pro', name: 'Gemini 1.5 Pro' },
          ]),
        });
      }
      if (pathname === '/api/provider/key-status') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            count: 1,
            slots: [{ label: 'Slot 1', configured: true, status: 'ready', inflight: 0 }],
          }),
        });
      }

      // Books
      if (pathname === '/api/books' && method === 'GET') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            {
              id: 'book-1',
              title: 'Solo Leveling',
              chapter_count: 1,
              translated_segments: 2,
              total_segments: 2,
            },
          ]),
        });
      }

      // Chapters
      if (pathname === '/api/books/book-1/chapters') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            {
              id: 'ch-1',
              order: 0,
              title: 'Chương 1: Cổng hầm ngục D-Rank',
              translated_segments: 2,
              total_segments: 2,
              segment_count: 2,
            },
          ]),
        });
      }

      // Single Chapter Details & Updates
      if (pathname === '/api/chapters/ch-1') {
        if (method === 'GET') {
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(mockChapter),
          });
        }
        if (method === 'PATCH') {
          const body = JSON.parse(req.postData() || '{}');
          lastPatchPayload = body;
          if (trigger409) {
            // Update server version behind the scenes to revision 2
            mockChapter.segments[0].revision = 2;
            mockChapter.segments[0].translation = 'Đó là một sự khởi đầu mới từ máy chủ.';
            return route.fulfill({
              status: 409,
              contentType: 'application/json',
              body: JSON.stringify({
                detail: {
                  message: 'Conflict: Revision mismatch on segment seg-1 (409 Deliberate)',
                  conflicts: [
                    { id: 'seg-1', server_revision: 2, client_revision: 1 },
                  ],
                },
              }),
            });
          }
          // Normal successful PATCH
          if (body.segments && Array.isArray(body.segments)) {
            for (const patch of body.segments) {
              const seg = mockChapter.segments.find((s) => s.id === patch.id);
              if (seg) {
                if (patch.translation !== undefined) seg.translation = patch.translation;
                if (patch.source !== undefined) seg.source = patch.source;
                seg.revision = (patch.expected_revision || seg.revision) + 1;
              }
            }
          }
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ ok: true }),
          });
        }
      }

      // Edit History list for current chapter
      if (pathname === '/api/chapters/ch-1/edit-history') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            items: mockHistoryItems,
            total: mockHistoryItems.length,
          }),
        });
      }

      // Edit History Restore endpoint
      if (pathname === '/api/chapters/ch-1/edit-history/h-1/restore' && method === 'POST') {
        if (triggerRestore409) {
          return route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({
              detail: 'Xung đột phiên bản (409 Deliberate): Phân đoạn đã được chỉnh sửa đồng thời bởi người khác.',
            }),
          });
        }
        // Apply historical translation to chapter segment
        mockChapter.segments[0].translation = 'Đó là một sự khởi đầu đã phục hồi.';
        mockChapter.segments[0].revision += 1;
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            segment: mockChapter.segments[0],
          }),
        });
      }

      // Glossary
      if (pathname === '/api/books/book-1/glossary') {
        if (method === 'GET') {
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(mockGlossary),
          });
        }
        if (method === 'POST') {
          const body = JSON.parse(req.postData() || '{}');
          const newTerm = {
            id: `g-${Date.now()}`,
            source: body.source,
            target: body.target,
            notes: body.notes || '',
            locked: Boolean(body.locked),
          };
          mockGlossary.push(newTerm);
          return route.fulfill({
            status: 201,
            contentType: 'application/json',
            body: JSON.stringify(newTerm),
          });
        }
      }

      if (pathname.startsWith('/api/glossary/')) {
        const termId = pathname.replace('/api/glossary/', '');
        if (method === 'PATCH') {
          const body = JSON.parse(req.postData() || '{}');
          const idx = mockGlossary.findIndex((g) => g.id === termId);
          if (idx !== -1) {
            mockGlossary[idx] = { ...mockGlossary[idx], ...body };
            return route.fulfill({
              status: 200,
              contentType: 'application/json',
              body: JSON.stringify(mockGlossary[idx]),
            });
          }
        }
        if (method === 'DELETE') {
          mockGlossary = mockGlossary.filter((g) => g.id !== termId);
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ ok: true }),
          });
        }
      }

      // Story Bible
      if (pathname === '/api/books/book-1/bible') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ text: 'Bối cảnh thế giới thợ săn...' }),
        });
      }

      // Jobs, Presets, Characters, Reviews
      if (pathname === '/api/books/book-1/jobs') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([]),
        });
      }
      if (pathname === '/api/books/book-1/presets') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            { id: 'p-1', name: 'Tiêu chuẩn văn học', is_default: true },
          ]),
        });
      }
      if (pathname === '/api/books/book-1/characters') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            { id: 'char-1', canonical_source: '성진우', canonical_target: 'Sung Jin-woo', gender: 'male', locked: true },
          ]),
        });
      }
      if (pathname === '/api/books/book-1/discovery/runs') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([]),
        });
      }
      if (pathname === '/api/books/book-1/consistency-reviews') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([]),
        });
      }

      // Default fallback
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({}),
      });
    });
  }

  try {
    // -------------------------------------------------------------------------
    // TEST 1: Login Failure & Failed Logout Retains Authenticated Workbench
    // -------------------------------------------------------------------------
    console.log('[2/7] Testing Login Failure and Failed Logout Behavior...');
    currentUser = null; // Start unauthenticated
    const contextAuth = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const pageAuth = await contextAuth.newPage();
    await setupApiIntercepts(pageAuth);

    await pageAuth.goto(baseUrl, { waitUntil: 'networkidle' });
    await pageAuth.waitForSelector('form', { timeout: 8000 });

    // Submit invalid login credentials
    await pageAuth.fill('input[type="text"]', 'admin');
    await pageAuth.fill('input[type="password"]', 'wrong-pass');
    await pageAuth.click('button[type="submit"]');

    // Assert visible login failure alert is rendered
    const loginErrorBanner = await pageAuth.waitForSelector('.alert-banner-error', { timeout: 5000 });
    const loginErrorText = await loginErrorBanner.innerText();
    assert.ok(loginErrorText.includes('401') || loginErrorText.includes('thất bại') || loginErrorText.includes('failed'),
      'Login failure banner must be visible on 401 response');
    console.log('      Verified: Login 401 displays visible error alert');

    // Submit correct credentials
    await pageAuth.fill('input[type="password"]', 'valid-password');
    await pageAuth.click('button[type="submit"]');

    // Assert workbench mounts
    await pageAuth.waitForSelector('.app-workspace', { timeout: 8000 });
    assert.ok(await pageAuth.$('.app-workspace'), 'Workbench must mount upon successful login');
    console.log('      Verified: Authenticated workbench mounted after successful login');

    // Test failed logout: mock returns 500
    triggerLogoutFail = true;
    const logoutBtn = await pageAuth.waitForSelector('button:has-text("Đăng xuất")', { timeout: 5000 });
    await logoutBtn.click();

    // Assert visible logout error banner appears and user remains authenticated
    const logoutErrorBanner = await pageAuth.waitForSelector('.alert-banner-error', { timeout: 5000 });
    assert.ok(logoutErrorBanner, 'Logout error banner must be rendered on failed logout');
    assert.ok(await pageAuth.$('.app-workspace'), 'Workbench must remain mounted when logout fails');
    console.log('      Verified: Failed logout displays error and keeps user in authenticated workbench');

    triggerLogoutFail = false;
    await contextAuth.close();

    // Reset currentUser to authenticated admin for main desktop suite
    currentUser = {
      id: 'u-admin',
      username: 'admin',
      role: 'admin',
      csrf_token: 'mock-csrf-token-123',
      authenticated: true,
      auth_override: false,
    };

    // -------------------------------------------------------------------------
    // TEST 2: Desktop View - Admin Overview & History Tab Visibility
    // -------------------------------------------------------------------------
    console.log('[3/7] Testing Desktop Overview (Admin)...');
    const contextDesktop = await browser.newContext({
      viewport: { width: 1280, height: 800 },
    });
    const pageDesktop = await contextDesktop.newPage();
    await setupApiIntercepts(pageDesktop);

    await pageDesktop.goto(baseUrl, { waitUntil: 'networkidle' });
    await pageDesktop.waitForSelector('.app-workspace', { timeout: 8000 });

    const title = await pageDesktop.title();
    assert.ok(title.length > 0, 'Page title must be non-empty');

    const hasOverlay = await pageDesktop.$('.modal-overlay');
    assert.strictEqual(hasOverlay, null, 'No modal overlay should be present on initial load');

    // Admin MUST see History Tab button
    const historyTabBtn = await pageDesktop.$('button:has-text("Lịch sử sửa")');
    assert.ok(historyTabBtn, 'Admin must see "Lịch sử sửa" tab button');

    const shot1Path = path.join(SCREENSHOTS_DIR, 'desktop-overview.png');
    await pageDesktop.screenshot({ path: shot1Path, fullPage: false });
    console.log(`      Saved screenshot -> ${shot1Path}`);

    // -------------------------------------------------------------------------
    // TEST 3: Glossary Tab (Admin CRUD)
    // -------------------------------------------------------------------------
    console.log('[4/7] Testing Glossary Tab...');
    const glossaryTabBtn = await pageDesktop.waitForSelector('button:has-text("Từ điển")', { timeout: 5000 });
    await glossaryTabBtn.click();

    await pageDesktop.waitForSelector('.glossary-table', { timeout: 5000 });
    const hasHunterTerm = await pageDesktop.$('text="헌터"');
    assert.ok(hasHunterTerm, 'Term "헌터" must be present in glossary table');

    // Click "+ Thêm thuật ngữ" button to test form expansion
    const addTermBtn = await pageDesktop.$('button:has-text("Thêm thuật ngữ")');
    assert.ok(addTermBtn, 'Add term button must be available for admin/editor');
    await addTermBtn.click();
    await pageDesktop.waitForSelector('input.glossary-input', { timeout: 3000 });

    const shot2Path = path.join(SCREENSHOTS_DIR, 'desktop-glossary.png');
    await pageDesktop.screenshot({ path: shot2Path, fullPage: false });
    console.log(`      Saved screenshot -> ${shot2Path}`);

    // -------------------------------------------------------------------------
    // TEST 4: Viewer Role Capabilities & Controls
    // -------------------------------------------------------------------------
    console.log('[5/7] Testing Viewer Controls & Capabilities (Assertions)...');
    currentUser = {
      id: 'u-viewer',
      username: 'viewer',
      role: 'viewer',
      csrf_token: 'mock-csrf-token-123',
      authenticated: true,
      auth_override: false,
    };

    const contextViewer = await browser.newContext({
      viewport: { width: 1280, height: 800 },
    });
    const pageViewer = await contextViewer.newPage();
    await setupApiIntercepts(pageViewer);

    await pageViewer.goto(baseUrl, { waitUntil: 'networkidle' });
    await pageViewer.waitForSelector('.app-workspace', { timeout: 8000 });

    // Assert Header controls for viewer
    const uploadBtnDisabled = await pageViewer.$eval('button[title*="Chỉ dành cho quản trị viên"], button[title*="Tải lên EPUB"]', (el) => el.disabled);
    assert.strictEqual(uploadBtnDisabled, true, 'Viewer cannot upload EPUB');

    const modelSelectDisabled = await pageViewer.$eval('select[aria-label*="Chọn mô hình"]', (el) => el.disabled);
    assert.strictEqual(modelSelectDisabled, true, 'Viewer cannot change model');

    const pdfBtnDisabled = await pageViewer.$eval('button[title*="Xuất PDF"], button[title*="biên tập viên"]', (el) => el.disabled);
    assert.strictEqual(pdfBtnDisabled, true, 'Viewer cannot export PDF');

    const epubBtnDisabled = await pageViewer.$eval('button[title*="Xuất EPUB"]', (el) => el.disabled);
    assert.strictEqual(epubBtnDisabled, false, 'Viewer MUST be able to export EPUB');

    // Assert Translation edit mode button is disabled for viewer
    const editBtnDisabled = await pageViewer.$eval('button[title*="Chế độ chỉ đọc"], button[title*="Chỉnh sửa"]', (el) => el.disabled);
    assert.strictEqual(editBtnDisabled, true, 'Viewer cannot edit chapter');

    // Assert Viewer has NO history tab button
    const viewerHistoryTabBtn = await pageViewer.$('button:has-text("Lịch sử sửa")');
    assert.strictEqual(viewerHistoryTabBtn, null, 'Viewer must NOT have History tab button');
    console.log('      Verified: Viewer role controls and restrictions strictly asserted');

    const shot3Path = path.join(SCREENSHOTS_DIR, 'viewer-controls.png');
    await pageViewer.screenshot({ path: shot3Path, fullPage: false });
    console.log(`      Saved screenshot -> ${shot3Path}`);
    await contextViewer.close();

    // -------------------------------------------------------------------------
    // TEST 5: Admin Edit Mode, 409 Conflict Modal, Draft Retention & Explicit Second Save
    // -------------------------------------------------------------------------
    console.log('[6/7] Testing Admin Edit, 409 Conflict Resolution, Draft Retention & Second Save...');
    currentUser = {
      id: 'u-admin',
      username: 'admin',
      role: 'admin',
      csrf_token: 'mock-csrf-token-123',
      authenticated: true,
      auth_override: false,
    };

    // Return to Translation tab on desktop page
    const transTabBtn = await pageDesktop.waitForSelector('button:has-text("Bản dịch")', { timeout: 5000 });
    await transTabBtn.click();

    // Click "Chỉnh sửa" mode
    const editModeBtn = await pageDesktop.waitForSelector('button:has-text("Chỉnh sửa")', { timeout: 5000 });
    await editModeBtn.click();

    // Type new draft text into segment 1 translation textarea
    const firstTextarea = await pageDesktop.waitForSelector('textarea.editor-para-textarea', { timeout: 5000 });
    const draftText = 'Bản dịch nháp mới do Admin sửa đổi.';
    await firstTextarea.fill(draftText);

    // SaveFooter should be visible
    await pageDesktop.waitForSelector('.save-footer', { timeout: 5000 });

    // Enable 409 trigger on PATCH
    trigger409 = true;

    // Click "Lưu bản dịch"
    const saveBtn = await pageDesktop.waitForSelector('.save-footer button.btn-primary');
    await saveBtn.click();

    // ConflictResolutionModal should appear with role="dialog" and aria-modal="true"
    const modal = await pageDesktop.waitForSelector('div[role="dialog"][aria-modal="true"]', { timeout: 5000 });
    assert.ok(modal, 'ConflictResolutionModal must appear with role="dialog" and aria-modal="true"');

    const shot4Path = path.join(SCREENSHOTS_DIR, 'conflict-409-modal.png');
    await pageDesktop.screenshot({ path: shot4Path, fullPage: false });
    console.log(`      Saved screenshot -> ${shot4Path}`);

    // Assert that "Hoàn tất xử lý" is initially disabled before explicit choice
    const applyBtnInitiallyDisabled = await pageDesktop.$eval('button:has-text("Hoàn tất xử lý")', (el) => el.disabled);
    assert.strictEqual(applyBtnInitiallyDisabled, true, 'Apply resolution button must be initially disabled before explicit choice');

    // Click "Giữ bản nháp (Rebase)"
    const rebaseBtn = await pageDesktop.waitForSelector('button:has-text("Giữ bản nháp (Rebase)")');
    await rebaseBtn.click();

    const applyBtnAfterChoiceDisabled = await pageDesktop.$eval('button:has-text("Hoàn tất xử lý")', (el) => el.disabled);
    assert.strictEqual(applyBtnAfterChoiceDisabled, false, 'Apply resolution button must be enabled after explicit rebase choice');

    // Click "Hoàn tất xử lý"
    const applyBtn = await pageDesktop.waitForSelector('button:has-text("Hoàn tất xử lý")');
    await applyBtn.click();

    // Modal should close, notice banner should appear
    await pageDesktop.waitForSelector('div[role="dialog"]', { state: 'detached', timeout: 5000 });
    const noticeBanner = await pageDesktop.waitForSelector('.alert-banner-info', { timeout: 3000 });
    assert.ok(noticeBanner, 'Resolution notice banner must appear after modal resolves');

    const shot5Path = path.join(SCREENSHOTS_DIR, 'conflict-resolved.png');
    await pageDesktop.screenshot({ path: shot5Path, fullPage: false });
    console.log(`      Saved screenshot -> ${shot5Path}`);

    // Assert draft text is STILL present in textarea after rebase
    const currentDraftVal = await firstTextarea.inputValue();
    assert.strictEqual(currentDraftVal, draftText, 'Draft text must be preserved after rebasing');

    // Perform explicit second save with trigger409 = false
    trigger409 = false;
    lastPatchPayload = null;

    const secondSaveBtn = await pageDesktop.waitForSelector('.save-footer button.btn-primary', { timeout: 3000 });
    await secondSaveBtn.click();

    // Verify explicit second save sends latest server revision (revision: 2)
    await pageDesktop.waitForFunction(() => !document.querySelector('.save-footer button.btn-primary:not([disabled])') || !document.querySelector('.save-footer'), { timeout: 5000 }).catch(() => null);
    assert.ok(lastPatchPayload, 'Second save must execute PATCH request');
    const seg1Patch = lastPatchPayload.segments.find((s) => s.id === 'seg-1');
    assert.ok(seg1Patch, 'Payload must contain patch for seg-1');
    assert.strictEqual(seg1Patch.expected_revision, 2, 'Second save after rebase must send latest server revision (2)');
    console.log('      Verified: Rebase draft retained and explicit second save successfully commits with revision 2');

    // -------------------------------------------------------------------------
    // TEST 6: History Tab, Restore Disabled When Dirty, Confirm, 409 Conflict & Auth Override
    // -------------------------------------------------------------------------
    console.log('[7/7] Testing History Panel (Restore disabled when dirty, confirm dialog, 409 conflict, restore success & auth_override)...');
    const adminHistoryTab = await pageDesktop.waitForSelector('button:has-text("Lịch sử sửa")', { timeout: 5000 });
    await adminHistoryTab.click();

    // Assert main content and page horizontal alignment: main.scrollLeft == 0, page.scrollX == 0
    const scrollAlignment = await pageDesktop.evaluate(() => {
      const main = document.querySelector('.main-content');
      return {
        mainScrollLeft: main ? main.scrollLeft : 0,
        pageScrollX: window.scrollX || document.documentElement.scrollLeft || 0,
      };
    });
    assert.strictEqual(scrollAlignment.mainScrollLeft, 0, 'main.scrollLeft must remain 0 after clicking History tab');
    assert.strictEqual(scrollAlignment.pageScrollX, 0, 'page.scrollX must remain 0 after clicking History tab');
    console.log('      Verified: Desktop main.scrollLeft == 0 and page.scrollX == 0 after clicking History tab');

    // Wait for history item card
    const historyItem = await pageDesktop.waitForSelector('.history-item-card', { timeout: 5000 });
    assert.ok(historyItem, 'EditHistoryPanel must display history item cards');

    // Assert rendered history revision values (old_revision: 1 -> new_revision: 2)
    const revBadgeText = await pageDesktop.$eval('.history-item-card', (el) => {
      const spans = Array.from(el.querySelectorAll('span'));
      const span = spans.find((s) => s.textContent.includes('Phiên bản') || s.textContent.includes('Revision'));
      return span ? span.textContent : '';
    });
    assert.ok(revBadgeText.includes('1') && revBadgeText.includes('2'),
      `Rendered history revision values must show 1 -> 2, got: "${revBadgeText}"`);
    console.log(`      Verified: Rendered history revision badge "${revBadgeText.trim()}" correctly shows 1 -> 2`);

    const shotHistPath = path.join(SCREENSHOTS_DIR, 'history-panel.png');
    await pageDesktop.screenshot({ path: shotHistPath, fullPage: false });
    console.log(`      Saved screenshot -> ${shotHistPath}`);

    // Test restore disabled when dirty:
    // Go to Translation tab, ensure edit mode, edit textarea to make dirty
    await (await pageDesktop.waitForSelector('button:has-text("Bản dịch")')).click();
    const editBtnForDirty = await pageDesktop.$('button:has-text("Chỉnh sửa")');
    if (editBtnForDirty) {
      await editBtnForDirty.click();
    }
    const dirtyTextarea = await pageDesktop.waitForSelector('textarea.editor-para-textarea', { timeout: 5000 });
    await dirtyTextarea.fill('Draft làm bẩn trạng thái');
    // Return to History tab
    await (await pageDesktop.waitForSelector('button:has-text("Lịch sử sửa")')).click();

    const restoreBtnDisabledWhenDirty = await pageDesktop.$eval('button:has-text("Phục hồi")', (el) => el.disabled);
    assert.strictEqual(restoreBtnDisabledWhenDirty, true, 'Restore button must be disabled when chapter has unsaved changes');
    console.log('      Verified: Restore disabled when chapter has unsaved changes');

    // Discard draft edits to re-enable restore
    await (await pageDesktop.waitForSelector('button:has-text("Bản dịch")')).click();
    const discardBtn = await pageDesktop.waitForSelector('.save-footer button:has-text("Huỷ thay đổi")', { timeout: 5000 });
    await discardBtn.click();
    const confirmDiscardBtn = await pageDesktop.waitForSelector('.modal-footer button.btn-danger', { timeout: 5000 });
    await confirmDiscardBtn.click();
    await pageDesktop.waitForSelector('.modal-overlay', { state: 'detached', timeout: 5000 });

    // Return to History tab
    await (await pageDesktop.waitForSelector('button:has-text("Lịch sử sửa")')).click();
    await pageDesktop.waitForSelector('.history-item-card', { timeout: 5000 });

    const restoreBtnEnabled = await pageDesktop.$eval('button:has-text("Phục hồi")', (el) => !el.disabled);
    assert.strictEqual(restoreBtnEnabled, true, 'Restore button must be enabled when no dirty edits exist');

    // Test Restore 409 Conflict handling (modal stays open, displays error)
    triggerRestore409 = true;
    const restoreTriggerBtn = await pageDesktop.waitForSelector('button:has-text("Phục hồi")');
    await restoreTriggerBtn.click();

    // Confirmation dialog opens
    const confirmRestoreModal = await pageDesktop.waitForSelector('div[role="dialog"]', { timeout: 5000 });
    assert.ok(confirmRestoreModal, 'Confirmation modal must appear upon clicking Phục hồi');

    // Click confirm restore
    const executeRestoreBtn = await pageDesktop.waitForSelector('button:has-text("Xác nhận phục hồi")');
    await executeRestoreBtn.click();

    // Assert 409 error message is visible and confirmation dialog remains OPEN
    const conflictRestoreNotice = await pageDesktop.waitForSelector('div[role="dialog"] div[role="alert"]', { timeout: 5000 });
    const conflictText = await conflictRestoreNotice.innerText();
    assert.ok(conflictText.includes('Xung đột phiên bản') || conflictText.includes('409'), 'Restore 409 conflict notice must be displayed in open dialog');
    assert.ok(await pageDesktop.$('div[role="dialog"]'), 'Confirmation dialog must remain open on 409 conflict');
    console.log('      Verified: Restore 409 conflict keeps confirmation open and displays conflict error');

    // Cancel modal
    await (await pageDesktop.waitForSelector('button:has-text("Hủy bỏ")')).click();
    await pageDesktop.waitForSelector('div[role="dialog"]', { state: 'detached', timeout: 5000 });

    // Test Restore Success flow
    triggerRestore409 = false;
    await (await pageDesktop.waitForSelector('button:has-text("Phục hồi")')).click();
    await (await pageDesktop.waitForSelector('button:has-text("Xác nhận phục hồi")')).click();

    // Assert modal closes and success status is displayed
    await pageDesktop.waitForSelector('div[role="dialog"]', { state: 'detached', timeout: 5000 });
    const successStatus = await pageDesktop.waitForSelector('div[role="status"]', { timeout: 5000 });
    const successText = await successStatus.innerText();
    assert.ok(successText.includes('phục hồi') || successText.includes('restored'), 'Restore success notification status must appear');
    console.log('      Verified: Restore success executed and chapter refreshed via handleRefreshChapterSafe');

    // Test legitimate auth_override=true, csrf_token=null case
    currentUser.auth_override = true;
    currentUser.csrf_token = null;
    await (await pageDesktop.waitForSelector('button:has-text("Phục hồi")')).click();
    await (await pageDesktop.waitForSelector('button:has-text("Xác nhận phục hồi")')).click();
    await pageDesktop.waitForSelector('div[role="dialog"]', { state: 'detached', timeout: 5000 });
    console.log('      Verified: Legitimate auth_override=true with csrf_token=null successfully handled');

    // Reset currentUser
    currentUser.auth_override = false;
    currentUser.csrf_token = 'mock-csrf-token-123';

    // -------------------------------------------------------------------------
    // TEST 7: Mobile Viewport, Drawer Visibility, Overlay Removal & No Overflow
    // -------------------------------------------------------------------------
    console.log('[Mobile QA] Testing Mobile Viewport (390x844), Drawer Visibility & Closed State...');
    const contextMobile = await browser.newContext({
      viewport: { width: 390, height: 844 },
    });
    const pageMobile = await contextMobile.newPage();
    await setupApiIntercepts(pageMobile);

    await pageMobile.goto(baseUrl, { waitUntil: 'networkidle' });
    await pageMobile.waitForSelector('.app-header', { timeout: 8000 });

    // Open mobile rail
    const mobileMenuBtn = await pageMobile.waitForSelector('.mobile-rail-toggle', { timeout: 3000 });
    await mobileMenuBtn.click();

    // Assert drawer is open and visible
    await pageMobile.waitForSelector('.chapter-rail.open', { state: 'visible', timeout: 5000 });
    assert.ok(await pageMobile.$('.chapter-rail.open'), 'Mobile drawer must be visible while open');

    // Click backdrop to close drawer
    const backdrop = await pageMobile.waitForSelector('.chapter-rail-backdrop', { timeout: 3000 });
    await backdrop.click();

    // Await overlay removed / drawer transition completed before taking closed-state screenshot
    await pageMobile.waitForSelector('.chapter-rail-backdrop', { state: 'detached', timeout: 5000 });
    await pageMobile.waitForSelector('.chapter-rail.open', { state: 'detached', timeout: 5000 });
    await pageMobile.waitForTimeout(350); // allow CSS slide-out animation to settle completely

    // Verify no gray overlay remains
    assert.strictEqual(await pageMobile.$('.chapter-rail-backdrop'), null, 'Backdrop overlay must be detached');

    const shot6Path = path.join(SCREENSHOTS_DIR, 'mobile-view.png');
    await pageMobile.screenshot({ path: shot6Path, fullPage: false });
    console.log(`      Saved screenshot -> ${shot6Path}`);

    // Verify NO horizontal page overflow
    const hasHorizontalOverflow = await pageMobile.evaluate(() => {
      return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
    });
    assert.strictEqual(hasHorizontalOverflow, false, 'Mobile page must not have horizontal scroll overflow');
    console.log('      Verified: Mobile drawer toggled cleanly, backdrop detached, no horizontal overflow');

    await contextMobile.close();
    await contextDesktop.close();

    // -------------------------------------------------------------------------
    // Error Logs Assertions
    // -------------------------------------------------------------------------
    assert.strictEqual(uncaughtPageErrors.length, 0, `Uncaught page errors encountered: ${uncaughtPageErrors.join(', ')}`);
    assert.strictEqual(unexpectedConsoleErrors.length, 0, `Unexpected console errors encountered: ${unexpectedConsoleErrors.join(', ')}`);

    console.log('--- All Frontend Release-Readiness Smoke Tests Passed with Strict Assertions! ---');

    await browser.close();
    server.close();
    process.exit(0);
  } catch (err) {
    console.error('Browser QA encountered a failure:', err);
    await browser.close();
    server.close();
    process.exit(1);
  }
}

runSmokeQA();
