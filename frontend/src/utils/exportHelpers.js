/**
 * Helper utilities for PDF export chapter selection, status evaluation, and filtering.
 */

/**
 * Determine translation completion status for a chapter.
 * @param {Object} chapter - Chapter object with segment counts.
 * @returns {'translated' | 'partial' | 'untranslated'}
 */
export function getChapterCompletionStatus(chapter) {
  if (!chapter) return 'untranslated';
  const total = Number(chapter.total_segments ?? chapter.segment_count ?? 0);
  const completed = Number(chapter.translated_segments ?? chapter.translated_segment_count ?? 0);

  if (total > 0 && completed >= total) {
    return 'translated';
  }
  if (completed > 0 && completed < total) {
    return 'partial';
  }
  return 'untranslated';
}

/**
 * Resolve effective chapter IDs based on export scope.
 * Preserves the original spine reading order of allChapters.
 *
 * @param {'all' | 'current' | 'custom'} scope
 * @param {Array<Object>} allChapters - Ordered list of chapters in book spine order.
 * @param {string | null} activeChapterId
 * @param {Set<string> | Array<string>} customSelectedIds
 * @returns {Array<string>}
 */
export function resolveExportChapterIds(scope, allChapters = [], activeChapterId = null, customSelectedIds = new Set()) {
  if (!Array.isArray(allChapters) || allChapters.length === 0) {
    return [];
  }

  if (scope === 'all') {
    return allChapters.map((c) => c.id);
  }

  if (scope === 'current') {
    if (activeChapterId && allChapters.some((c) => c.id === activeChapterId)) {
      return [activeChapterId];
    }
    return [allChapters[0].id];
  }

  // Custom selection: maintain exact spine reading order
  const customSet = customSelectedIds instanceof Set ? customSelectedIds : new Set(customSelectedIds);
  return allChapters.filter((c) => customSet.has(c.id)).map((c) => c.id);
}

/**
 * Filter chapters by search query matching chapter title, translation, order, or index.
 *
 * @param {Array<Object>} chapters
 * @param {string} query
 * @param {Function} [getSearchStrings] - Optional helper for localized search strings.
 * @returns {Array<Object>}
 */
export function filterChaptersBySearch(chapters = [], query = '', getSearchStrings = null) {
  if (!query || !query.trim()) {
    return chapters;
  }
  const q = query.trim().toLowerCase();

  return chapters.filter((c, idx) => {
    // 1. Direct index or order match (e.g. "chương 1", "1", "10")
    const orderStr = String(c.order ?? c.spine_index ?? idx + 1);
    if (orderStr === q || `chương ${orderStr}`.toLowerCase().includes(q) || `chapter ${orderStr}`.toLowerCase().includes(q)) {
      return true;
    }

    // 2. Direct title match
    if (c.title && c.title.toLowerCase().includes(q)) {
      return true;
    }

    // 3. Localized translation strings
    if (typeof getSearchStrings === 'function' && c.title) {
      const strings = getSearchStrings(c.title);
      if (Array.isArray(strings) && strings.some((s) => s.toLowerCase().includes(q))) {
        return true;
      }
    }

    return false;
  });
}

/**
 * Identify any selected chapters that are not fully translated.
 *
 * @param {Array<Object>} chapters
 * @param {Array<string>} selectedIds
 * @returns {Array<Object>}
 */
export function getIncompleteChaptersForTranslation(chapters = [], selectedIds = []) {
  const selectedSet = new Set(selectedIds);
  return chapters.filter((c) => selectedSet.has(c.id) && getChapterCompletionStatus(c) !== 'translated');
}

/**
 * Validate PDF export request before sending.
 *
 * @param {Object} params
 * @param {string} params.bookId
 * @param {Array<string>} params.selectedChapterIds
 * @param {'translation' | 'source'} params.contentMode
 * @param {Array<Object>} params.chapters
 * @returns {{ valid: boolean, error?: string, incompleteCount?: number }}
 */
export function validatePdfExportRequest({ bookId, selectedChapterIds, contentMode, chapters = [] }) {
  if (!bookId) {
    return { valid: false, error: 'noBookSelected' };
  }

  if (!Array.isArray(selectedChapterIds) || selectedChapterIds.length === 0) {
    return { valid: false, error: 'noChaptersSelected' };
  }

  if (contentMode === 'translation') {
    const incomplete = getIncompleteChaptersForTranslation(chapters, selectedChapterIds);
    if (incomplete.length > 0) {
      return {
        valid: false,
        error: 'incompleteChapters',
        incompleteCount: incomplete.length,
        incompleteChapters: incomplete,
      };
    }
  }

  return { valid: true };
}
