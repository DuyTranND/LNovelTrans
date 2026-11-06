import test from 'node:test';
import assert from 'node:assert/strict';
import { translations } from '../src/i18n/translations.js';
import { isAuditAllClear, getAuditEmptyState } from '../src/utils/consistencyAuditHelpers.js';

test('1. i18n dictionary completeness for consistency review keys', () => {
  const consistencyKeys = [
    'tabConsistency',
    'consistencyTitle',
    'consistencySubtitle',
    'consistencyDisclaimer',
    'startConsistencyReview',
    'startingConsistencyReview',
    'consistencyModelNotice',
    'consistencyNoKeyNotice',
    'consistencyNoTranslatedSegments',
    'consistencyActiveRunNotice',
    'cancelReview',
    'cancellingReview',
    'retryReview',
    'retryingReview',
    'refreshReview',
    'currentRun',
    'reviewHistory',
    'noReviewsYet',
    'noReviewsDesc',
    'progressSegments',
    'progressChapters',
    'findingsCount',
    'failedSegmentsCount',
    'skippedSegmentsCount',
    'staleWarning',
    'partialCoverageWarning',
    'findingsTitle',
    'findingsDesc',
    'sourceTerm',
    'canonicalRecommendation',
    'recommendationReason',
    'confidence',
    'confidenceHigh',
    'confidenceMedium',
    'confidenceLow',
    'variantsAndUsage',
    'sampleLocations',
    'occurrencesEvidence',
    'showOccurrences',
    'hideOccurrences',
    'glossaryConflict',
    'glossaryLocked',
    'glossaryTarget',
    'allCategories',
    'allSeverities',
    'categoryCharacter',
    'categoryLocation',
    'categoryTitle',
    'categorySkill',
    'categoryItem',
    'categoryOrganization',
    'categoryConcept',
    'categoryTerm',
    'severityHigh',
    'severityMedium',
    'severityLow',
    'filterByCategory',
    'filterBySeverity',
    'noFindings',
    'noFindingsFiltered',
    'auditIncompleteInconclusive',
    'auditIncompleteDesc',
    'pageInfo',
    'prevPage',
    'nextPage',
    'pageSize',
    'readOnlyNotice',
    'runStartedAt',
    'runCompletedAt',
    'selectReview',
    'selectRunPrompt',
    'statusCancelled',
    'errCannotConnectReview',
    'errCreateReview',
    'errCancelReview',
    'errRetryReview',
  ];

  for (const key of consistencyKeys) {
    assert.ok(translations.vi[key], `Missing vi translation for key: ${key}`);
    assert.ok(translations.en[key], `Missing en translation for key: ${key}`);
  }
});

test('2. Consistency review API client functions', async () => {
  // Mock global fetch to verify requests
  const calls = [];
  const originalFetch = globalThis.fetch;

  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return {
      ok: true,
      headers: {
        get: (h) => (h.toLowerCase() === 'content-type' ? 'application/json' : null),
      },
      json: async () => ({ success: true, url, method: options.method || 'GET' }),
    };
  };

  try {
    const api = await import('../src/api.js');

    // Test createConsistencyReview
    await api.createConsistencyReview('book-1', { model: 'gemini-3.5-flash-lite' });
    const createCall = calls[calls.length - 1];
    assert.equal(createCall.url, '/api/books/book-1/consistency-reviews');
    assert.equal(createCall.options.method, 'POST');
    assert.deepEqual(JSON.parse(createCall.options.body), { model: 'gemini-3.5-flash-lite' });

    // Test getBookConsistencyReviews
    await api.getBookConsistencyReviews('book-1');
    const listCall = calls[calls.length - 1];
    assert.equal(listCall.url, '/api/books/book-1/consistency-reviews');

    // Test getConsistencyReviewStatus
    await api.getConsistencyReviewStatus('book-1', 'run-123');
    const statusCall = calls[calls.length - 1];
    assert.equal(statusCall.url, '/api/books/book-1/consistency-reviews/run-123');

    // Test getConsistencyReviewFindings with filters and pagination
    await api.getConsistencyReviewFindings('book-1', 'run-123', {
      page: 2,
      page_size: 50,
      category: 'character',
      severity: 'high',
    });
    const findingsCall = calls[calls.length - 1];
    assert.equal(
      findingsCall.url,
      '/api/books/book-1/consistency-reviews/run-123/findings?page=2&page_size=50&category=character&severity=high'
    );

    // Test getConsistencyReviewFindings with 'all' filters omitted
    await api.getConsistencyReviewFindings('book-1', 'run-123', {
      page: 1,
      page_size: 20,
      category: 'all',
      severity: 'all',
    });
    const findingsCallAll = calls[calls.length - 1];
    assert.equal(
      findingsCallAll.url,
      '/api/books/book-1/consistency-reviews/run-123/findings?page=1&page_size=20'
    );

    // Test cancelConsistencyReview
    await api.cancelConsistencyReview('book-1', 'run-123');
    const cancelCall = calls[calls.length - 1];
    assert.equal(cancelCall.url, '/api/books/book-1/consistency-reviews/run-123/cancel');
    assert.equal(cancelCall.options.method, 'POST');

    // Test retryConsistencyReview
    await api.retryConsistencyReview('book-1', 'run-123');
    const retryCall = calls[calls.length - 1];
    assert.equal(retryCall.url, '/api/books/book-1/consistency-reviews/run-123/retry');
    assert.equal(retryCall.options.method, 'POST');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('3. All-clear eligibility logic (isAuditAllClear)', () => {
  const baseValidRun = {
    id: 'run-valid',
    status: 'completed',
    findings_count: 0,
    total_segments: 100,
    processed_segments: 100,
    failed_segments: 0,
    skipped_segments: 0,
    is_stale: false,
    progress: { coverage_status: 'full', percentage: 100 },
  };

  // 1. Fully completed full-coverage zero-finding run succeeds
  assert.equal(isAuditAllClear(baseValidRun), true);

  // Legacy run without explicit coverage_status in progress also succeeds if 100% processed without failed/skipped
  const legacyValidRun = {
    id: 'run-legacy',
    status: 'completed',
    findings_count: 0,
    total_segments: 50,
    processed_segments: 50,
    failed_segments: 0,
    skipped_segments: 0,
    is_stale: false,
  };
  assert.equal(isAuditAllClear(legacyValidRun), true);

  // Legacy run is valid only when all numeric count fields are explicitly supplied
  const legacyWithoutFindings = { ...legacyValidRun };
  delete legacyWithoutFindings.findings_count;
  assert.equal(isAuditAllClear(legacyWithoutFindings), false);

  const legacyWithoutFailed = { ...legacyValidRun };
  delete legacyWithoutFailed.failed_segments;
  assert.equal(isAuditAllClear(legacyWithoutFailed), false);

  const legacyWithoutSkipped = { ...legacyValidRun };
  delete legacyWithoutSkipped.skipped_segments;
  assert.equal(isAuditAllClear(legacyWithoutSkipped), false);

  // 2. Failed run cannot succeed
  assert.equal(isAuditAllClear({ ...baseValidRun, status: 'failed' }), false);

  // 3. Cancelled run cannot succeed
  assert.equal(isAuditAllClear({ ...baseValidRun, status: 'cancelled' }), false);

  // 4. Running / queued / paused run cannot succeed
  assert.equal(isAuditAllClear({ ...baseValidRun, status: 'running' }), false);
  assert.equal(isAuditAllClear({ ...baseValidRun, status: 'queued' }), false);
  assert.equal(isAuditAllClear({ ...baseValidRun, status: 'pausing' }), false);
  assert.equal(isAuditAllClear({ ...baseValidRun, status: 'paused' }), false);

  // 5. Coverage partial cannot succeed
  assert.equal(
    isAuditAllClear({
      ...baseValidRun,
      progress: { coverage_status: 'partial' },
    }),
    false
  );
  assert.equal(
    isAuditAllClear({
      ...baseValidRun,
      coverage_status: 'partial',
    }),
    false
  );

  // 6. Present-but-empty coverage_status cannot pass (top-level and nested in progress)
  assert.equal(
    isAuditAllClear({
      ...baseValidRun,
      coverage_status: '',
    }),
    false
  );
  assert.equal(
    isAuditAllClear({
      ...baseValidRun,
      progress: { coverage_status: '' },
    }),
    false
  );

  // 7. Missing findings_count cannot pass (omitted or undefined)
  const runWithoutFindings = { ...baseValidRun };
  delete runWithoutFindings.findings_count;
  assert.equal(isAuditAllClear(runWithoutFindings), false);
  assert.equal(isAuditAllClear({ ...baseValidRun, findings_count: undefined }), false);

  // 8. Missing failed_segments cannot pass (omitted or undefined)
  const runWithoutFailed = { ...baseValidRun };
  delete runWithoutFailed.failed_segments;
  assert.equal(isAuditAllClear(runWithoutFailed), false);
  assert.equal(isAuditAllClear({ ...baseValidRun, failed_segments: undefined }), false);

  // 9. Missing skipped_segments cannot pass (omitted or undefined)
  const runWithoutSkipped = { ...baseValidRun };
  delete runWithoutSkipped.skipped_segments;
  assert.equal(isAuditAllClear(runWithoutSkipped), false);
  assert.equal(isAuditAllClear({ ...baseValidRun, skipped_segments: undefined }), false);

  // 10. Failed segments > 0 cannot succeed
  assert.equal(isAuditAllClear({ ...baseValidRun, failed_segments: 2 }), false);

  // 11. Skipped segments > 0 cannot succeed
  assert.equal(isAuditAllClear({ ...baseValidRun, skipped_segments: 5 }), false);

  // 12. Processed < total segments cannot succeed
  assert.equal(
    isAuditAllClear({ ...baseValidRun, processed_segments: 95, total_segments: 100 }),
    false
  );

  // 13. Total segments <= 0 cannot succeed
  assert.equal(
    isAuditAllClear({ ...baseValidRun, processed_segments: 0, total_segments: 0 }),
    false
  );

  // 14. Stale warning run cannot succeed
  assert.equal(isAuditAllClear({ ...baseValidRun, is_stale: true }), false);

  // 15. Run with findings cannot succeed
  assert.equal(isAuditAllClear({ ...baseValidRun, findings_count: 1 }), false);

  // 16. Invalid / null / undefined run cannot succeed
  assert.equal(isAuditAllClear(null), false);
  assert.equal(isAuditAllClear(undefined), false);
  assert.equal(isAuditAllClear({}), false);
});

test('4. Empty state determination logic (getAuditEmptyState)', () => {
  const completedCleanRun = {
    id: 'run-clean',
    status: 'completed',
    findings_count: 0,
    total_segments: 80,
    processed_segments: 80,
    failed_segments: 0,
    skipped_segments: 0,
    is_stale: false,
    progress: { coverage_status: 'full' },
  };

  // Case 1: Fully completed, full coverage, zero findings -> all_clear (success)
  const cleanState = getAuditEmptyState(completedCleanRun, {
    findingsCount: 0,
    displayedCount: 0,
  });
  assert.deepEqual(cleanState, {
    type: 'all_clear',
    titleKey: 'noFindings',
    descKey: null,
    status: 'success',
  });

  // Case 2: Existing findings in run, but current filter matches zero items -> filtered_empty (neutral)
  const filteredRun = {
    ...completedCleanRun,
    findings_count: 5,
  };
  const filteredState = getAuditEmptyState(filteredRun, {
    findingsCount: 5,
    displayedCount: 0,
  });
  assert.deepEqual(filteredState, {
    type: 'filtered_empty',
    titleKey: 'noFindingsFiltered',
    descKey: null,
    status: 'neutral',
  });

  // Case 3: Incomplete run (running) with zero findings -> inconclusive (warning)
  const runningState = getAuditEmptyState(
    { ...completedCleanRun, status: 'running', processed_segments: 40 },
    { findingsCount: 0, displayedCount: 0 }
  );
  assert.deepEqual(runningState, {
    type: 'inconclusive',
    titleKey: 'auditIncompleteInconclusive',
    descKey: 'auditIncompleteDesc',
    status: 'warning',
  });

  // Case 4: Failed run with zero findings -> inconclusive (warning)
  const failedState = getAuditEmptyState(
    { ...completedCleanRun, status: 'failed' },
    { findingsCount: 0, displayedCount: 0 }
  );
  assert.deepEqual(failedState, {
    type: 'inconclusive',
    titleKey: 'auditIncompleteInconclusive',
    descKey: 'auditIncompleteDesc',
    status: 'warning',
  });

  // Case 5: Cancelled run with zero findings -> inconclusive (warning)
  const cancelledState = getAuditEmptyState(
    { ...completedCleanRun, status: 'cancelled' },
    { findingsCount: 0, displayedCount: 0 }
  );
  assert.deepEqual(cancelledState, {
    type: 'inconclusive',
    titleKey: 'auditIncompleteInconclusive',
    descKey: 'auditIncompleteDesc',
    status: 'warning',
  });

  // Case 6: Completed but partial coverage run -> inconclusive (warning)
  const partialCoverageState = getAuditEmptyState(
    {
      ...completedCleanRun,
      failed_segments: 2,
      processed_segments: 78,
      progress: { coverage_status: 'partial' },
    },
    { findingsCount: 0, displayedCount: 0 }
  );
  assert.deepEqual(partialCoverageState, {
    type: 'inconclusive',
    titleKey: 'auditIncompleteInconclusive',
    descKey: 'auditIncompleteDesc',
    status: 'warning',
  });

  // Case 7: Stale completed run -> inconclusive (warning)
  const staleState = getAuditEmptyState(
    { ...completedCleanRun, is_stale: true },
    { findingsCount: 0, displayedCount: 0 }
  );
  assert.deepEqual(staleState, {
    type: 'inconclusive',
    titleKey: 'auditIncompleteInconclusive',
    descKey: 'auditIncompleteDesc',
    status: 'warning',
  });
});

test('5. Confirm title category and incomplete-run message translations', () => {
  // Category Title
  assert.equal(translations.vi.categoryTitle, 'Tiêu đề chương');
  assert.equal(translations.en.categoryTitle, 'Chapter Title');

  // Audit Incomplete Inconclusive Title
  assert.equal(translations.vi.auditIncompleteInconclusive, 'Chưa có kết luận về tính nhất quán');
  assert.equal(translations.en.auditIncompleteInconclusive, 'Inconclusive Consistency Status');

  // Audit Incomplete Description
  assert.equal(
    translations.vi.auditIncompleteDesc,
    'Đợt rà soát chưa hoàn tất toàn bộ sách hoặc gặp sự cố. Không thể xác nhận bản dịch là hoàn toàn nhất quán.'
  );
  assert.equal(
    translations.en.auditIncompleteDesc,
    'The audit is incomplete or encountered errors. Full consistency cannot be confirmed.'
  );
});

test('6. Terminal and incomplete audit states never claim all-clear even with persisted findings or zero findings', () => {
  const terminalNonCompletedStatuses = ['failed', 'cancelled', 'paused'];
  for (const status of terminalNonCompletedStatuses) {
    // Zero findings on terminal incomplete run must never be all-clear
    assert.equal(
      isAuditAllClear({
        id: `run-${status}`,
        status,
        findings_count: 0,
        total_segments: 100,
        processed_segments: 50,
        failed_segments: 1,
        skipped_segments: 0,
      }),
      false,
      `Run with status ${status} must not be all-clear`
    );

    // Zero findings empty state must be inconclusive with warning status
    const emptyState = getAuditEmptyState(
      { id: `run-${status}`, status, findings_count: 0 },
      { findingsCount: 0, displayedCount: 0 }
    );
    assert.equal(emptyState.type, 'inconclusive');
    assert.equal(emptyState.status, 'warning');
    assert.equal(emptyState.titleKey, 'auditIncompleteInconclusive');

    // When deterministic findings exist (e.g. title inconsistencies) on a terminal run,
    // all-clear remains strictly false (no-green-success)
    assert.equal(
      isAuditAllClear({
        id: `run-${status}-with-findings`,
        status,
        findings_count: 2,
        total_segments: 100,
        processed_segments: 50,
      }),
      false
    );

    // When findings exist on a terminal run and filter matches 0 items, reports filtered_empty
    const filteredState = getAuditEmptyState(
      { id: `run-${status}-with-findings`, status, findings_count: 2 },
      { findingsCount: 2, displayedCount: 0 }
    );
    assert.equal(filteredState.type, 'filtered_empty');
    assert.equal(filteredState.status, 'neutral');
  }
});
