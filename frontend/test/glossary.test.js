import test from 'node:test';
import assert from 'node:assert';
import {
  getGlossary,
  createGlossaryTerm,
  updateGlossaryTerm,
  deleteGlossaryTerm,
  login,
  logout,
  ApiError,
} from '../src/api.js';

test('1. getGlossary fetches and normalizes glossary entries across array and object formats', async () => {
  const originalFetch = globalThis.fetch;
  try {
    // Array response
    globalThis.fetch = async (url) => {
      assert.ok(url.includes('/api/books/b-1/glossary'));
      return new Response(JSON.stringify([
        { id: 'g-1', source: '마법사', target: 'Pháp sư', locked: true, notes: 'Ma pháp sư' },
      ]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const list1 = await getGlossary('b-1');
    assert.strictEqual(Array.isArray(list1), true);
    assert.strictEqual(list1.length, 1);
    assert.strictEqual(list1[0].source, '마법사');

    // Wrapped { glossary: [...] } response
    globalThis.fetch = async () => {
      return new Response(JSON.stringify({
        glossary: [
          { id: 'g-2', source: '기사', target: 'Kỵ sĩ', locked: false },
        ],
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const list2 = await getGlossary('b-1');
    assert.strictEqual(Array.isArray(list2), true);
    assert.strictEqual(list2.length, 1);
    assert.strictEqual(list2[0].source, '기사');

    // Server error propagates as ApiError
    globalThis.fetch = async () => {
      return new Response(JSON.stringify({ detail: 'Database connection failed' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    let fetchErr = null;
    try {
      await getGlossary('b-1');
    } catch (e) {
      fetchErr = e;
    }
    assert.ok(fetchErr instanceof ApiError);
    assert.strictEqual(fetchErr.status, 500);
    assert.strictEqual(fetchErr.detail, 'Database connection failed');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('2. createGlossaryTerm formats payload and passes CSRF token on POST', async () => {
  const originalFetch = globalThis.fetch;
  const capturedCalls = [];

  try {
    globalThis.fetch = async (url, options = {}) => {
      capturedCalls.push({ url, options });
      if (url.includes('/api/auth/login')) {
        return new Response(JSON.stringify({ csrf_token: 'csrf-glossary-test' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({
        id: 'g-new',
        source: '던전',
        target: 'Hầm ngục',
        locked: true,
        notes: 'Ngục tối',
      }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    await login('admin', 'pass');

    capturedCalls.length = 0;
    const termData = {
      source: '던전',
      target: 'Hầm ngục',
      locked: true,
      notes: 'Ngục tối',
    };
    const created = await createGlossaryTerm('book-99', termData);

    assert.strictEqual(capturedCalls.length, 1);
    const call = capturedCalls[0];
    assert.ok(call.url.includes('/api/books/book-99/glossary'));
    assert.strictEqual(call.options.method, 'POST');
    assert.strictEqual(call.options.headers['X-CSRF-Token'], 'csrf-glossary-test');
    assert.strictEqual(call.options.headers['Content-Type'], 'application/json');

    const sentBody = JSON.parse(call.options.body);
    assert.strictEqual(sentBody.source, '던전');
    assert.strictEqual(sentBody.target, 'Hầm ngục');
    assert.strictEqual(sentBody.locked, true);
    assert.strictEqual(sentBody.notes, 'Ngục tối');

    assert.strictEqual(created.id, 'g-new');

    await logout();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('3. updateGlossaryTerm and deleteGlossaryTerm route correctly and handle 404/400 errors', async () => {
  const originalFetch = globalThis.fetch;
  const capturedCalls = [];

  try {
    globalThis.fetch = async (url, options = {}) => {
      capturedCalls.push({ url, options });
      if (url.includes('/api/glossary/g-missing')) {
        return new Response(JSON.stringify({ detail: 'Term not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    // Update existing term
    capturedCalls.length = 0;
    await updateGlossaryTerm('g-1', { target: 'Pháp sư tối cao', locked: true });
    assert.strictEqual(capturedCalls.length, 1);
    assert.ok(capturedCalls[0].url.includes('/api/glossary/g-1'));
    assert.strictEqual(capturedCalls[0].options.method, 'PATCH');
    const updateBody = JSON.parse(capturedCalls[0].options.body);
    assert.strictEqual(updateBody.target, 'Pháp sư tối cao');
    assert.strictEqual(updateBody.locked, true);

    // Delete existing term
    capturedCalls.length = 0;
    await deleteGlossaryTerm('g-1');
    assert.strictEqual(capturedCalls.length, 1);
    assert.ok(capturedCalls[0].url.includes('/api/glossary/g-1'));
    assert.strictEqual(capturedCalls[0].options.method, 'DELETE');

    // 404 on missing term throws ApiError with status 404
    let notFoundErr = null;
    try {
      await updateGlossaryTerm('g-missing', { target: 'None' });
    } catch (e) {
      notFoundErr = e;
    }
    assert.ok(notFoundErr instanceof ApiError);
    assert.strictEqual(notFoundErr.status, 404);
    assert.strictEqual(notFoundErr.detail, 'Term not found');
  } finally {
    globalThis.fetch = originalFetch;
  }
});
