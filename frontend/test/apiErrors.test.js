import test from 'node:test';
import assert from 'node:assert';
import { ApiError, getChapter, updateChapterSegments, login, logout } from '../src/api.js';

test('1. ApiError creates Error instance with preserved status, detail, and conflicts', () => {
  const conflictsData = [{ segment_id: 'seg-1', expected: 2, actual: 3 }];
  const err = new ApiError('Conflict: revision mismatch (409)', {
    status: 409,
    detail: 'Revision conflict detected on segment seg-1',
    conflicts: conflictsData,
    data: { status: 'error', detail: 'Revision conflict', conflicts: conflictsData },
  });

  assert.ok(err instanceof Error);
  assert.strictEqual(err.name, 'ApiError');
  assert.strictEqual(err.status, 409);
  assert.strictEqual(err.detail, 'Revision conflict detected on segment seg-1');
  assert.deepStrictEqual(err.conflicts, conflictsData);
  assert.strictEqual(err.message, 'Conflict: revision mismatch (409)');
});

test('2. ApiError preserves status 0 for network and connection failures', () => {
  const err = new ApiError('Cannot connect to server: Failed to fetch', {
    status: 0,
    detail: 'Failed to fetch',
  });

  assert.strictEqual(err.status, 0);
  assert.strictEqual(err.detail, 'Failed to fetch');
  assert.strictEqual(err.conflicts, undefined);
});

test('3. ApiError preserves structured nested conflicts payload', () => {
  const payload = {
    detail: {
      message: 'Multiple segments were modified concurrently',
      conflicts: [
        { id: 'seg-a', server_revision: 4, client_revision: 3 },
        { id: 'seg-b', server_revision: 5, client_revision: 2 },
      ],
    },
  };

  const err = new ApiError('Concurrent modification (409)', {
    status: 409,
    detail: payload.detail,
    conflicts: payload.detail.conflicts,
    data: payload,
  });

  assert.strictEqual(err.status, 409);
  assert.strictEqual(err.conflicts.length, 2);
  assert.strictEqual(err.conflicts[0].id, 'seg-a');
  assert.strictEqual(err.conflicts[1].id, 'seg-b');
});

test('4. getChapter and updateChapterSegments handle real 409 Response with nested detail and conflicts', async () => {
  const originalFetch = globalThis.fetch;
  try {
    const conflictPayload = {
      detail: {
        message: 'Conflict: Revision mismatch on segment seg-1',
        conflicts: [
          { id: 'seg-1', server_revision: 2, client_revision: 1 },
        ],
      },
    };

    globalThis.fetch = async () => {
      return new Response(JSON.stringify(conflictPayload), {
        status: 409,
        statusText: 'Conflict',
        headers: { 'Content-Type': 'application/json' },
      });
    };

    let caughtErr = null;
    try {
      await updateChapterSegments('ch-1', [
        { id: 'seg-1', expected_revision: 1, translation: 'Bản dịch mới' },
      ]);
    } catch (e) {
      caughtErr = e;
    }

    assert.ok(caughtErr, 'Expected updateChapterSegments to throw on 409');
    assert.ok(caughtErr instanceof ApiError, 'Error should be an instance of ApiError');
    assert.strictEqual(caughtErr.status, 409);
    assert.ok(Array.isArray(caughtErr.conflicts), 'conflicts should be an array');
    assert.strictEqual(caughtErr.conflicts.length, 1);
    assert.strictEqual(caughtErr.conflicts[0].id, 'seg-1');
    assert.strictEqual(caughtErr.conflicts[0].server_revision, 2);
    assert.strictEqual(caughtErr.detail.message, 'Conflict: Revision mismatch on segment seg-1');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('5. getChapter and updateChapterSegments handle network rejection with status 0 ApiError', async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => {
      throw new TypeError('Failed to fetch (network disconnected)');
    };

    let getChapterErr = null;
    try {
      await getChapter('ch-test');
    } catch (e) {
      getChapterErr = e;
    }

    assert.ok(getChapterErr instanceof ApiError);
    assert.strictEqual(getChapterErr.status, 0);
    assert.ok(getChapterErr.detail.includes('Failed to fetch'));

    let updateErr = null;
    try {
      await updateChapterSegments('ch-test', [{ id: 's1', translation: 'T' }]);
    } catch (e) {
      updateErr = e;
    }

    assert.ok(updateErr instanceof ApiError);
    assert.strictEqual(updateErr.status, 0);
    assert.ok(updateErr.detail.includes('Failed to fetch'));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('6. CSRF token header behavior: sent on PATCH when logged in, omitted on GET and after logout', async () => {
  const originalFetch = globalThis.fetch;
  const capturedRequests = [];

  try {
    globalThis.fetch = async (url, options = {}) => {
      capturedRequests.push({ url, options });
      if (url.includes('/api/auth/login')) {
        return new Response(JSON.stringify({
          id: 'u-admin',
          username: 'admin',
          role: 'admin',
          csrf_token: 'test-csrf-token-xyz',
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/auth/logout')) {
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/chapters/ch-1')) {
        if ((options.method || 'GET').toUpperCase() === 'PATCH') {
          return new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({
          id: 'ch-1',
          title: 'Chapter 1',
          segments: [],
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    // 1. Log in to establish CSRF token
    await login('admin', 'secret');

    // 2. Perform PATCH -> X-CSRF-Token MUST be present
    capturedRequests.length = 0;
    await updateChapterSegments('ch-1', [{ id: 'seg-1', translation: 'Bản dịch mới' }]);
    assert.strictEqual(capturedRequests.length, 1);
    assert.strictEqual(capturedRequests[0].options.headers['X-CSRF-Token'], 'test-csrf-token-xyz');
    assert.strictEqual(capturedRequests[0].options.headers['Content-Type'], 'application/json');

    // 3. Perform GET getChapter -> X-CSRF-Token MUST NOT be sent
    capturedRequests.length = 0;
    await getChapter('ch-1');
    assert.strictEqual(capturedRequests.length, 1);
    assert.strictEqual(capturedRequests[0].options.headers['X-CSRF-Token'], undefined);

    // 4. Log out -> CSRF token cleared
    await logout();

    // 5. Subsequent PATCH -> X-CSRF-Token MUST NOT be present
    capturedRequests.length = 0;
    await updateChapterSegments('ch-1', [{ id: 'seg-1', translation: 'Bản dịch mới' }]);
    assert.strictEqual(capturedRequests.length, 1);
    assert.strictEqual(capturedRequests[0].options.headers['X-CSRF-Token'], undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
