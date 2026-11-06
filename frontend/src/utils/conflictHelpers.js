/**
 * Chapter Save 409 Conflict Resolution Helpers.
 *
 * Facilitates side-by-side comparison between server version and own draft,
 * allows explicit accept-server or rebase-draft resolution,
 * preserves unrelated drafts, and requires explicit save afterward.
 */

/**
 * Builds conflict descriptors comparing what was sent in the draft payload
 * against the latest server segments.
 *
 * Only returns conflicts when structured revision conflicts exist:
 * - patch.expected_revision !== serverSeg.revision
 * - or segment was specifically flagged in reportedConflicts
 *
 * Never flags arbitrary segments when no revision mismatch exists.
 *
 * @param {Array} changedPayload Array of { id, expected_revision, translation, source } sent in save
 * @param {Array} serverSegments Array of segment objects from latest server fetch
 * @param {Object} draftEdits Current draft edits map { [segmentId]: { translation, source } }
 * @param {Array|null} reportedConflicts Optional conflicts list from 409 API response
 * @returns {Array} List of conflict items; empty if no structured revision conflicts exist
 */
export function buildChapterSaveConflicts(changedPayload = [], serverSegments = [], draftEdits = {}, reportedConflicts = null) {
  const reportedSet = new Set();
  if (Array.isArray(reportedConflicts)) {
    for (const c of reportedConflicts) {
      if (typeof c === 'string') reportedSet.add(c);
      else if (c && c.segment_id) reportedSet.add(c.segment_id);
      else if (c && c.id) reportedSet.add(c.id);
    }
  }

  const conflicts = [];
  const serverMap = new Map((serverSegments || []).map((s) => [s.id, s]));

  for (const patch of changedPayload) {
    const serverSeg = serverMap.get(patch.id);
    if (!serverSeg) continue;

    const draft = draftEdits[patch.id] || {};
    const revMismatch =
      patch.expected_revision !== undefined &&
      serverSeg.revision !== undefined &&
      patch.expected_revision !== serverSeg.revision;

    const isReported = reportedSet.has(patch.id);

    // Only flag when actual revision mismatch or explicitly reported by server conflict payload
    if (revMismatch || isReported) {
      conflicts.push({
        segmentId: patch.id,
        order: serverSeg.order ?? 0,
        clientExpectedRevision: patch.expected_revision,
        serverRevision: serverSeg.revision,
        serverSource: serverSeg.source || '',
        serverTranslation: serverSeg.translation || '',
        draftSource: draft.source !== undefined ? draft.source : (patch.source ?? serverSeg.source ?? ''),
        draftTranslation: draft.translation !== undefined ? draft.translation : (patch.translation ?? serverSeg.translation ?? ''),
      });
    }
  }

  return conflicts;
}

/**
 * Resolves conflict by accepting server version for a segment.
 * Removes the draft edit for this segment so the UI displays the server text.
 * Preserves all other segment drafts.
 */
export function resolveConflictAcceptServer(draftEdits, segmentId) {
  const next = { ...draftEdits };
  delete next[segmentId];
  return next;
}

/**
 * Resolves conflict by rebasing the draft onto the latest revision.
 * Keeps the user's draft text in draftEdits. When chapter segments are updated
 * to the latest server revision, subsequent save will use the new revision.
 * Preserves all other segment drafts.
 */
export function resolveConflictRebaseDraft(draftEdits, segmentId, serverSegment) {
  const next = { ...draftEdits };
  if (!next[segmentId] && serverSegment) {
    next[segmentId] = {
      translation: serverSegment.translation || '',
      source: serverSegment.source || '',
    };
  }
  return next;
}

/**
 * Bulk resolve: accept server for all given conflicts.
 */
export function resolveAllConflictsAcceptServer(draftEdits, conflicts = []) {
  const next = { ...draftEdits };
  for (const c of conflicts) {
    delete next[c.segmentId];
  }
  return next;
}

/**
 * Bulk resolve: rebase all given conflicts onto latest revision.
 */
export function resolveAllConflictsRebase(draftEdits, conflicts = []) {
  const next = { ...draftEdits };
  for (const c of conflicts) {
    if (!next[c.segmentId]) {
      next[c.segmentId] = {
        translation: c.draftTranslation,
        source: c.draftSource,
      };
    }
  }
  return next;
}
