/**
 * Trạm Dịch - Centralized API Client
 * Accommodates FastAPI backend contract with robust schema normalization.
 */

import { translations } from './i18n/translations.js';

const API_BASE = '/api';

let csrfToken = null;

function getT(key) {
  const lang = document.documentElement.lang || 'vi';
  const dict = translations[lang] || translations['vi'];
  return dict[key] || translations['vi'][key] || key;
}

/**
 * Generic request helper with JSON error extraction and signal support.
 */
async function request(path, options = {}) {
  const { signal, headers = {}, ...rest } = options;

  const method = (rest.method || 'GET').toUpperCase();
  const reqHeaders = {
    'Accept': 'application/json',
    ...headers,
  };

  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    if (!reqHeaders['X-CSRF-Token'] && !reqHeaders['x-csrf-token']) {
      reqHeaders['X-CSRF-Token'] = csrfToken;
    }
  }

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      signal,
      credentials: 'same-origin',
      headers: reqHeaders,
      ...rest,
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      throw err; // Let caller distinguish aborts
    }
    throw new Error(`${getT('errCannotConnect')}: ${err.message}`);
  }

  if (!res.ok) {
    let baseError = `${getT('errSystem')} (${res.status})`;
    let serverError = '';
    try {
      const data = await res.json();
      if (data) {
        if (typeof data.detail === 'string') {
          serverError = data.detail;
        } else if (Array.isArray(data.detail)) {
          serverError = data.detail.map((d) => d.msg || JSON.stringify(d)).join(', ');
        } else if (data.detail && typeof data.detail === 'object') {
          serverError = data.detail.message || JSON.stringify(data.detail);
        } else if (data.message) {
          serverError = data.message;
        } else if (data.error) {
          serverError = data.error;
        }
      }
    } catch {
      // Body not JSON
    }
    const errorDetail = serverError ? `${baseError}: ${serverError}` : baseError;
    throw new Error(errorDetail);
  }

  const contentType = res.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    return res.json();
  }
  return res;
}

// -----------------------------------------------------------------------------
// Authentication & Session
// -----------------------------------------------------------------------------

export async function getAuthMe(signal) {
  const data = await request('/auth/me', { signal });
  if (data && data.csrf_token) {
    csrfToken = data.csrf_token;
  }
  return data;
}

export async function login(username, password, signal) {
  const data = await request('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
    signal,
  });
  if (data && data.csrf_token) {
    csrfToken = data.csrf_token;
  }
  return data;
}

export async function logout(signal) {
  const result = await request('/auth/logout', {method:'POST', signal});
  csrfToken = null;
  return result;
}

// -----------------------------------------------------------------------------
// Settings & Provider Models
// -----------------------------------------------------------------------------

export async function getSettings(signal) {
  const data = await request('/settings', { signal });
  return {
    model: data.model || 'gemini-3.5-flash-lite',
    key_configured: Boolean(data.key_configured),
    gemini_configured: Boolean(data.gemini_configured),
  };
}

export async function updateSettings(model, signal) {
  const data = await request('/settings', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model }),
    signal,
  });
  return {
    model: data.model,
    key_configured: Boolean(data.key_configured),
    gemini_configured: Boolean(data.gemini_configured),
  };
}

export async function getModels(signal) {
  try {
    const data = await request('/models', { signal });
    if (Array.isArray(data)) {
      return data.map((m) => (typeof m === 'string' ? { id: m, name: m } : m));
    }
    if (data && Array.isArray(data.models)) {
      return data.models.map((m) => (typeof m === 'string' ? { id: m, name: m } : m));
    }
    return [];
  } catch {
    // Return empty list on failure; never invent deprecated options
    return [];
  }
}

export async function getKeyStatus(signal) {
  return request('/provider/key-status', { signal });
}

// -----------------------------------------------------------------------------
// Books
// -----------------------------------------------------------------------------

export async function uploadBook(file, signal) {
  const formData = new FormData();
  formData.append('file', file);

  const headers = {};
  if (csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }

  const res = await fetch(`${API_BASE}/books`, {
    method: 'POST',
    credentials: 'same-origin',
    headers,
    body: formData,
    signal,
  });

  if (!res.ok) {
    let baseError = `${getT('errUploadBook')} (${res.status})`;
    let serverError = '';
    try {
      const data = await res.json();
      serverError = data.detail || data.message || '';
    } catch {}
    const errorDetail = serverError ? `${baseError}: ${serverError}` : baseError;
    throw new Error(errorDetail);
  }

  return res.json();
}

export async function getBooks(signal) {
  const data = await request('/books', { signal });
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.books)) return data.books;
  return [];
}

export async function getBook(bookId, signal) {
  return request(`/books/${bookId}`, { signal });
}

export async function getChapters(bookId, signal) {
  const data = await request(`/books/${bookId}/chapters`, { signal });
  const rawList = Array.isArray(data) ? data : (data && Array.isArray(data.chapters)) ? data.chapters : [];
  return rawList.map((ch, idx) => ({
    ...ch,
    order: ch.order ?? ch.spine_index ?? idx,
    translated_segments: ch.translated_segments ?? ch.translated_segment_count ?? ch.completed_segments ?? 0,
    total_segments: ch.total_segments ?? ch.segment_count ?? 0,
  }));
}

// -----------------------------------------------------------------------------
// Chapter Details & Segment Editing
// -----------------------------------------------------------------------------

export async function getChapter(chapterId, signal) {
  const data = await request(`/chapters/${chapterId}`, { signal });
  const segments = (Array.isArray(data.segments) ? data.segments : []).map((seg, idx) => {
    const reviewData = seg.review_data ?? seg.review ?? null;
    let issues = [];
    if (reviewData) {
      if (Array.isArray(reviewData)) {
        issues = reviewData;
      } else if (Array.isArray(reviewData.issues)) {
        issues = reviewData.issues;
      } else if (typeof reviewData === 'string') {
        issues = [reviewData];
      } else if (typeof reviewData === 'object') {
        issues = Object.values(reviewData).flat();
      }
    }
    return {
      ...seg,
      order: seg.order ?? idx,
      review_data: reviewData,
      issues: issues.map((iss) =>
        typeof iss === 'string'
          ? iss
          : (iss.description || iss.message || iss.detail || iss.comment || 'Cần kiểm tra bản dịch')
      ),
    };
  });

  const rawReview = data.review ?? data.review_data ?? null;
  let reviewIssues = [];
  if (rawReview) {
    if (typeof rawReview === 'string') {
      reviewIssues = [rawReview];
    } else if (Array.isArray(rawReview)) {
      reviewIssues = rawReview.map((r) =>
        typeof r === 'string' ? r : (r.message || r.detail || r.comment || JSON.stringify(r))
      );
    } else if (Array.isArray(rawReview.issues)) {
      reviewIssues = rawReview.issues.map((r) =>
        typeof r === 'string' ? r : (r.message || r.detail || r.comment || JSON.stringify(r))
      );
    } else if (typeof rawReview === 'object') {
      reviewIssues = Object.entries(rawReview).map(([k, v]) =>
        `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`
      );
    }
  }

  return {
    id: data.id || chapterId,
    title: data.title || '',
    order: data.order ?? data.spine_index ?? 0,
    status: data.status || 'pending',
    review: rawReview,
    review_issues: reviewIssues,
    segments,
  };
}

export async function updateChapterSegments(chapterId, segments, signal) {
  const payload = Array.isArray(segments) ? { segments } : segments;
  return request(`/chapters/${chapterId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
}

// -----------------------------------------------------------------------------
// Glossary CRUD
// -----------------------------------------------------------------------------

export async function getGlossary(bookId, signal) {
  const data = await request(`/books/${bookId}/glossary`, { signal });
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.glossary)) return data.glossary;
  return [];
}

export async function createGlossaryTerm(bookId, term, signal) {
  return request(`/books/${bookId}/glossary`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source: term.source || '',
      target: term.target || '',
      locked: Boolean(term.locked),
      notes: term.notes || '',
    }),
    signal,
  });
}

export async function updateGlossaryTerm(termId, term, signal) {
  return request(`/glossary/${termId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source: term.source,
      target: term.target,
      locked: term.locked !== undefined ? Boolean(term.locked) : undefined,
      notes: term.notes,
    }),
    signal,
  });
}

export async function deleteGlossaryTerm(termId, signal) {
  return request(`/glossary/${termId}`, {
    method: 'DELETE',
    signal,
  });
}

// -----------------------------------------------------------------------------
// Story Bible
// -----------------------------------------------------------------------------

export async function getStoryBible(bookId, signal) {
  const data = await request(`/books/${bookId}/bible`, { signal });
  if (typeof data === 'string') return { text: data };
  return {
    text: data.text ?? data.story_bible ?? data.content ?? data.bible ?? '',
  };
}

export async function updateStoryBible(bookId, text, signal) {
  return request(`/books/${bookId}/bible`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
    signal,
  });
}

export async function analyzeStoryBible(bookId, signal) {
  return request(`/books/${bookId}/analyze`, {
    method: 'POST',
    signal,
  });
}

// -----------------------------------------------------------------------------
// Jobs & Queue Management
// -----------------------------------------------------------------------------

function normalizeJob(job) {
  if (!job) return job;
  let params = job.params;
  if (typeof params === 'string') {
    try { params = JSON.parse(params); } catch {}
  }
  params = params || {};

  let progress = job.progress;
  if (typeof progress === 'string') {
    try { progress = JSON.parse(progress); } catch {}
  }
  progress = progress || {};

  const chapterIds = job.chapter_ids || params.chapter_ids || (params.chapter_id ? [params.chapter_id] : (job.chapter_id ? [job.chapter_id] : []));
  const completed = job.completed_segments ?? progress.completed_segments ?? progress.completed ?? job.completed ?? 0;
  const total = job.total_segments ?? progress.total_segments ?? progress.total ?? job.total ?? 0;

  return {
    ...job,
    chapter_ids: Array.isArray(chapterIds) ? chapterIds : [],
    completed_segments: Number(completed) || 0,
    total_segments: Number(total) || 0,
    status: job.status || 'queued',
  };
}

export async function getJobs(bookId, signal) {
  const data = await request(`/books/${bookId}/jobs`, { signal });
  const list = Array.isArray(data) ? data : (data && Array.isArray(data.jobs)) ? data.jobs : [];
  return list.map(normalizeJob);
}

export async function createJob(bookId, options = {}, signal) {
  // options: { chapter_ids: string[], review: boolean, max_segments?: number }
  return request(`/books/${bookId}/jobs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options),
    signal,
  });
}

export async function pauseJob(jobId, signal) {
  return request(`/jobs/${jobId}/pause`, {
    method: 'POST',
    signal,
  });
}

export async function resumeJob(jobId, signal) {
  return request(`/jobs/${jobId}/resume`, {
    method: 'POST',
    signal,
  });
}

export async function retryJob(jobId, signal) {
  return request(`/jobs/${jobId}/retry`, {
    method: 'POST',
    signal,
  });
}

// -----------------------------------------------------------------------------
// EPUB Export
// -----------------------------------------------------------------------------

export async function exportBook(bookId) {
  const url = `${API_BASE}/books/${bookId}/export`;
  const res = await fetch(url, { method: 'HEAD' });
  if (!res.ok) {
    let baseError = `${getT('errExport')} (${res.status})`;
    let serverError = '';
    try {
      const data = await res.json();
      serverError = data.detail || data.message || '';
    } catch {}
    const errorDetail = serverError ? `${baseError}: ${serverError}` : baseError;
    throw new Error(errorDetail);
  }

  // Use a native HTTP download; the server supplies the UTF-8 filename.
  const a = document.createElement('a');
  a.style.display = 'none';
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// -----------------------------------------------------------------------------
// PDF Export
// -----------------------------------------------------------------------------

export async function exportBookPdf(bookId, { chapter_ids, content = 'translation' } = {}) {
  const payload = {};
  if (chapter_ids !== undefined) {
    payload.chapter_ids = chapter_ids;
  }
  if (content !== undefined) {
    payload.content = content;
  }

  const headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/pdf, application/json',
  };
  if (csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }

  const res = await fetch(`${API_BASE}/books/${bookId}/export/pdf`, {
    method: 'POST',
    credentials: 'same-origin',
    headers,
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    let baseError = `${getT('errExportPdf')} (${res.status})`;
    let serverError = '';
    try {
      const data = await res.json();
      serverError = data.detail || data.message || '';
    } catch {}
    const errorDetail = serverError ? `${baseError}: ${serverError}` : baseError;
    throw new Error(errorDetail);
  }

  const blob = await res.blob();

  // Try to parse filename from Content-Disposition header
  let filename = `book_${content === 'translation' ? 'vi' : 'ko'}.pdf`;
  const disposition = res.headers.get('content-disposition') || '';
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match && utf8Match[1]) {
    try {
      filename = decodeURIComponent(utf8Match[1]);
    } catch {}
  } else {
    const asciiMatch = disposition.match(/filename="?([^";]+)"?/i);
    if (asciiMatch && asciiMatch[1]) {
      filename = asciiMatch[1];
    }
  }

  // Safe download via blob URL and revocation
  const blobUrl = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.style.display = 'none';
  a.href = blobUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(blobUrl);
}

// -----------------------------------------------------------------------------
// Consistency Audit / Reviews
// -----------------------------------------------------------------------------

export async function createConsistencyReview(bookId, { model } = {}, signal) {
  const payload = model ? { model } : {};
  return request(`/books/${bookId}/consistency-reviews`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
}

export async function getBookConsistencyReviews(bookId, signal) {
  return request(`/books/${bookId}/consistency-reviews`, { signal });
}

export async function getConsistencyReviewStatus(bookId, runId, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}`, { signal });
}

export async function getConsistencyReviewFindings(bookId, runId, { page = 1, page_size = 20, category, severity } = {}, signal) {
  const params = new URLSearchParams();
  if (page) params.set('page', String(page));
  if (page_size) params.set('page_size', String(page_size));
  if (category && category !== 'all') params.set('category', category);
  if (severity && severity !== 'all') params.set('severity', severity);
  const qs = params.toString();
  return request(`/books/${bookId}/consistency-reviews/${runId}/findings${qs ? `?${qs}` : ''}`, { signal });
}

export async function cancelConsistencyReview(bookId, runId, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/cancel`, {
    method: 'POST',
    signal,
  });
}

export async function retryConsistencyReview(bookId, runId, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/retry`, {
    method: 'POST',
    signal,
  });
}

export async function startSemanticReview(bookId, runId, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/semantic-review`, {
    method: 'POST',
    signal,
  });
}

export async function cancelSemanticReview(bookId, runId, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/semantic-cancel`, {
    method: 'POST',
    signal,
  });
}

export async function getCorrectionPreview(bookId, runId, params = {}, signal) {
  const query = new URLSearchParams();
  if (params.page) query.set('page', params.page);
  if (params.pageSize) query.set('page_size', params.pageSize);
  if (params.classification && params.classification !== 'all') query.set('classification', params.classification);
  if (params.category && params.category !== 'all') query.set('category', params.category);
  if (params.status && params.status !== 'all') query.set('status', params.status);
  const qs = query.toString();
  return request(`/books/${bookId}/consistency-reviews/${runId}/corrections/preview${qs ? `?${qs}` : ''}`, { signal });
}

export async function generateCorrectionPreview(bookId, runId, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/corrections/generate-preview`, {
    method: 'POST',
    signal,
  });
}

export async function updateCorrectionProposalSelection(bookId, runId, proposalId, isSelected, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/corrections/proposals/${proposalId}`, {
    method: 'PUT',
    body: JSON.stringify({ is_selected: isSelected }),
    signal,
  });
}

export async function batchUpdateCorrectionSelection(bookId, runId, isSelected, onlyAutoSafe = false, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/corrections/select-all`, {
    method: 'POST',
    body: JSON.stringify({ is_selected: isSelected, only_auto_safe: onlyAutoSafe }),
    signal,
  });
}

export async function applyCorrectionProposals(bookId, runId, options = {}, signal) {
  return request(`/books/${bookId}/consistency-reviews/${runId}/corrections/apply`, {
    method: 'POST',
    body: JSON.stringify(options),
    signal,
  });
}

export async function getCorrectionSnapshots(bookId, signal) {
  return request(`/books/${bookId}/consistency-reviews/snapshots`, { signal });
}

export async function rollbackCorrectionSnapshot(bookId, snapshotId, signal) {
  return request(`/books/${bookId}/consistency-reviews/snapshots/${snapshotId}/rollback`, {
    method: 'POST',
    signal,
  });
}

// -----------------------------------------------------------------------------
// Character Profiles
// -----------------------------------------------------------------------------

export async function getCharacters(bookId, signal) {
  return request(`/books/${bookId}/characters`, { signal });
}

export async function createCharacter(bookId, payload, signal) {
  return request(`/books/${bookId}/characters`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
}

export async function getCharacter(characterId, signal) {
  return request(`/characters/${characterId}`, { signal });
}

export async function updateCharacter(characterId, payload, signal) {
  return request(`/characters/${characterId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
}

export async function deleteCharacter(characterId, signal) {
  return request(`/characters/${characterId}`, {
    method: 'DELETE',
    signal,
  });
}

// -----------------------------------------------------------------------------
// Pre-translation AI Discovery
// -----------------------------------------------------------------------------

export async function startDiscovery(bookId, options = {}, signal) {
  return request(`/books/${bookId}/discovery`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options),
    signal,
  });
}

export async function getDiscoveryRuns(bookId, signal) {
  return request(`/books/${bookId}/discovery/runs`, { signal });
}

export async function getDiscoveryRun(bookId, runId, signal) {
  return request(`/books/${bookId}/discovery/runs/${runId}`, { signal });
}

export async function cancelDiscoveryRun(bookId, runId, signal) {
  return request(`/books/${bookId}/discovery/runs/${runId}/cancel`, {
    method: 'POST',
    signal,
  });
}

export async function getDiscoveryProposals(bookId, params = {}, signal) {
  const query = new URLSearchParams();
  if (params.status && params.status !== 'all') query.set('status', params.status);
  if (params.category && params.category !== 'all') query.set('category', params.category);
  if (params.runId) query.set('run_id', params.runId);
  const qs = query.toString();
  return request(`/books/${bookId}/discovery/proposals${qs ? `?${qs}` : ''}`, { signal });
}

export async function reviewDiscoveryProposal(proposalId, payload, signal) {
  return request(`/discovery/proposals/${proposalId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
}

// -----------------------------------------------------------------------------
// Prompt Presets and Few-Shot Examples
// -----------------------------------------------------------------------------

export async function getPresets(bookId, signal) {
  return request(`/books/${bookId}/presets`, { signal });
}

export async function createPreset(bookId, payload, signal) {
  return request(`/books/${bookId}/presets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
}

export async function getPreset(presetId, signal) {
  return request(`/presets/${presetId}`, { signal });
}

export async function updatePreset(presetId, payload, signal) {
  return request(`/presets/${presetId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
}

export async function deletePreset(presetId, signal) {
  return request(`/presets/${presetId}`, {
    method: 'DELETE',
    signal,
  });
}

export async function setDefaultPreset(presetId, signal) {
  return request(`/presets/${presetId}/default`, {
    method: 'POST',
    signal,
  });
}



