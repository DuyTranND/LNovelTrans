/**
 * Helper utilities for Consistency Review audit status and empty-state evaluations.
 */

/**
 * Checks whether an audit run qualifies for an all-clear result.
 *
 * All-clear is strictly allowed only when:
 * 1. run is non-null and status === 'completed'
 * 2. explicit run.findings_count === 0
 * 3. processed_segments === total_segments and total_segments > 0
 * 4. explicit run.failed_segments === 0
 * 5. explicit run.skipped_segments === 0
 * 6. coverage_status must equal 'full' if present (including an empty string)
 * 7. is_stale is false / no stale warning invalidates the result
 *
 * @param {object|null|undefined} run
 * @returns {boolean}
 */
export function isAuditAllClear(run) {
  if (!run || typeof run !== 'object') {
    return false;
  }

  if (run.status !== 'completed') {
    return false;
  }

  if (run.findings_count !== 0) {
    return false;
  }

  const totalSegments = run.total_segments ?? 0;
  const processedSegments = run.processed_segments ?? 0;
  if (totalSegments <= 0 || processedSegments !== totalSegments) {
    return false;
  }

  if (run.failed_segments !== 0) {
    return false;
  }

  if (run.skipped_segments !== 0) {
    return false;
  }

  if (Boolean(run.is_stale)) {
    return false;
  }

  // Coverage status check: if present (including an empty string), must equal 'full'
  const progressCoverage = run.progress && typeof run.progress === 'object' ? run.progress.coverage_status : undefined;
  if (progressCoverage !== undefined && progressCoverage !== 'full') {
    return false;
  }
  if (run.coverage_status !== undefined && run.coverage_status !== 'full') {
    return false;
  }

  return true;
}

/**
 * Determines the empty state presentation for ConsistencyReviewTab.
 *
 * @param {object|null|undefined} run - selectedRun object
 * @param {object} [options]
 * @param {number} [options.findingsCount] - total findings in run (from run.findings_count or pagination.total)
 * @param {number} [options.displayedCount] - count of findings currently returned by filter
 * @returns {{ type: 'all_clear' | 'filtered_empty' | 'inconclusive', titleKey: string, descKey: string | null, status: 'success' | 'neutral' | 'warning' }}
 */
export function getAuditEmptyState(run, { findingsCount, displayedCount } = {}) {
  const totalFindings = typeof findingsCount === 'number'
    ? findingsCount
    : (run?.findings_count ?? 0);

  const displayed = typeof displayedCount === 'number' ? displayedCount : 0;

  // 1. Findings exist in this run, but none match the current filter
  if (totalFindings > 0 && displayed === 0) {
    return {
      type: 'filtered_empty',
      titleKey: 'noFindingsFiltered',
      descKey: null,
      status: 'neutral',
    };
  }

  // 2. All-clear check
  if (isAuditAllClear(run)) {
    return {
      type: 'all_clear',
      titleKey: 'noFindings',
      descKey: null,
      status: 'success',
    };
  }

  // 3. Inconclusive: run is incomplete, failed, cancelled, running, partial coverage, stale, etc.
  return {
    type: 'inconclusive',
    titleKey: 'auditIncompleteInconclusive',
    descKey: 'auditIncompleteDesc',
    status: 'warning',
  };
}

/**
 * Checks whether the offline/deterministic audit stage of a run is currently active.
 *
 * @param {object|null|undefined} run
 * @returns {boolean}
 */
export function isOfflineAuditActive(run) {
  if (!run || typeof run !== 'object') {
    return false;
  }
  return ['running', 'queued', 'pausing'].includes(run.status);
}

/**
 * Checks whether the semantic review stage of a run is currently active.
 *
 * @param {object|null|undefined} run
 * @returns {boolean}
 */
export function isSemanticAuditActive(run) {
  if (!run || typeof run !== 'object') {
    return false;
  }
  const stage = run.progress?.semantic_stage ?? run.semantic_stage;
  if (typeof stage !== 'string') {
    return false;
  }
  return ['running', 'queued', 'pausing'].includes(stage.trim().toLowerCase());
}

/**
 * Checks whether any stage of the audit run (offline or semantic) is actively executing.
 * Auto-refresh polling must remain active as long as this returns true.
 *
 * @param {object|null|undefined} run
 * @returns {boolean}
 */
export function isAuditRunActive(run) {
  return isOfflineAuditActive(run) || isSemanticAuditActive(run);
}

/**
 * Resolves the appropriate error or pause notification state for an audit run.
 *
 * Requirements:
 * 1. Surfacing genuine semantic failures (semantic_stage === 'failed' or semantic_error) accurately.
 * 2. Surfacing genuine semantic pause notices (semantic_stage === 'paused' && semantic_pause_reason).
 * 3. Suppressing stale historical run.error when offline audit is completed and semantic audit
 *    is running or completed healthy (semantic_error is null).
 * 4. Preserving real offline run.error when offline failed or incomplete.
 *
 * @param {object|null|undefined} run
 * @returns {{ type: 'error' | 'warning', labelKey: string, message: string } | null}
 */
export function getAuditRunErrorNotice(run) {
  if (!run || typeof run !== 'object') {
    return null;
  }

  const rawSemanticStage = run.progress?.semantic_stage ?? run.semantic_stage;
  const semanticStage = typeof rawSemanticStage === 'string' ? rawSemanticStage.trim().toLowerCase() : '';
  const semanticError = run.progress?.semantic_error ?? run.semantic_error ?? null;
  const semanticPauseReason = run.progress?.semantic_pause_reason ?? run.semantic_pause_reason ?? null;

  // 1. Semantic stage genuine failure takes precedence
  if (semanticStage === 'failed' || (semanticError && semanticStage !== 'running' && semanticStage !== 'completed')) {
    return {
      type: 'error',
      labelKey: 'semanticErrorLabel',
      message: semanticError || run.error || '',
    };
  }

  // 2. Semantic stage pause reason
  if (semanticStage === 'paused' && semanticPauseReason) {
    return {
      type: 'warning',
      labelKey: 'semanticPauseLabel',
      message: semanticPauseReason,
    };
  }

  // 3. Stale error suppression:
  // If offline stage completed and semantic stage is running or completed (with no semantic error),
  // suppress any stale historical selectedRun.error (e.g. past network or socket timeouts).
  const isOfflineCompleted = run.status === 'completed';
  const isSemanticHealthy = semanticStage === 'running' || semanticStage === 'completed';

  if (isOfflineCompleted && isSemanticHealthy) {
    return null;
  }

  // If offline completed and semantic stage never started or is clean with 0 failed segments,
  // historical error strings should not falsely mark the completed run as failed.
  if (isOfflineCompleted && !run.failed_segments && !semanticError) {
    return null;
  }

  // 4. Real offline error
  if (run.error) {
    return {
      type: 'error',
      labelKey: 'reviewErrorLabel',
      message: run.error,
    };
  }

  return null;
}

