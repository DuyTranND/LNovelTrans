import test from 'node:test';
import assert from 'node:assert/strict';
import {
  getChapterCompletionStatus,
  resolveExportChapterIds,
  filterChaptersBySearch,
  getIncompleteChaptersForTranslation,
  validatePdfExportRequest,
} from '../src/utils/exportHelpers.js';

test('1. getChapterCompletionStatus: accurately categorizes translated, partial, and untranslated', () => {
  const completeChap = { id: 'c1', total_segments: 10, translated_segments: 10 };
  assert.equal(getChapterCompletionStatus(completeChap), 'translated');

  const partialChap = { id: 'c2', total_segments: 10, translated_segments: 4 };
  assert.equal(getChapterCompletionStatus(partialChap), 'partial');

  const untranslatedChap = { id: 'c3', total_segments: 10, translated_segments: 0 };
  assert.equal(getChapterCompletionStatus(untranslatedChap), 'untranslated');

  const emptyChap = { id: 'c4', total_segments: 0, translated_segments: 0 };
  assert.equal(getChapterCompletionStatus(emptyChap), 'untranslated');

  assert.equal(getChapterCompletionStatus(null), 'untranslated');
});

test('2. resolveExportChapterIds: respects scope and preserves stable spine reading order', () => {
  const allChapters = [
    { id: 'ch-1', spine_index: 0 },
    { id: 'ch-2', spine_index: 1 },
    { id: 'ch-3', spine_index: 2 },
  ];

  // Scope 'all'
  assert.deepEqual(
    resolveExportChapterIds('all', allChapters, 'ch-2'),
    ['ch-1', 'ch-2', 'ch-3']
  );

  // Scope 'current'
  assert.deepEqual(
    resolveExportChapterIds('current', allChapters, 'ch-2'),
    ['ch-2']
  );
  assert.deepEqual(
    resolveExportChapterIds('current', allChapters, null),
    ['ch-1'],
    'Defaults to first chapter if active chapter is null'
  );

  // Scope 'custom' - selection added out of order (ch-3, ch-1)
  const customSet = new Set(['ch-3', 'ch-1']);
  const resolvedCustom = resolveExportChapterIds('custom', allChapters, null, customSet);
  assert.deepEqual(
    resolvedCustom,
    ['ch-1', 'ch-3'],
    'Must strictly preserve original spine reading order'
  );
});

test('3. filterChaptersBySearch: filters by order, title, and localized title strings', () => {
  const chapters = [
    { id: 'c1', title: '각성 (1)', spine_index: 0, order: 1 },
    { id: 'c2', title: '비밀의 방', spine_index: 1, order: 2 },
    { id: 'c3', title: '에필로그', spine_index: 2, order: 3 },
  ];

  // Empty query returns all
  assert.equal(filterChaptersBySearch(chapters, '').length, 3);
  assert.equal(filterChaptersBySearch(chapters, '   ').length, 3);

  // Search by order
  assert.equal(filterChaptersBySearch(chapters, '1').length, 1);
  assert.equal(filterChaptersBySearch(chapters, 'chương 2').length, 1);

  // Search by Korean title
  const resultKo = filterChaptersBySearch(chapters, '각성');
  assert.equal(resultKo.length, 1);
  assert.equal(resultKo[0].id, 'c1');

  // Search by localized string via lookup helper
  const mockGetSearchStrings = (title) => {
    if (title === '각성 (1)') return ['각성 (1)', 'Thức tỉnh (1)', 'Awakening (1)'];
    if (title === '비밀의 방') return ['비밀의 방', 'Căn phòng bí mật', 'Chamber of Secrets'];
    return [title];
  };

  const resultVi = filterChaptersBySearch(chapters, 'Thức tỉnh', mockGetSearchStrings);
  assert.equal(resultVi.length, 1);
  assert.equal(resultVi[0].id, 'c1');

  const resultEn = filterChaptersBySearch(chapters, 'Chamber', mockGetSearchStrings);
  assert.equal(resultEn.length, 1);
  assert.equal(resultEn[0].id, 'c2');
});

test('4. getIncompleteChaptersForTranslation and validatePdfExportRequest: rejects incomplete translations', () => {
  const chapters = [
    { id: 'c1', title: 'Chương 1', total_segments: 10, translated_segments: 10 },
    { id: 'c2', title: 'Chương 2', total_segments: 15, translated_segments: 5 },
    { id: 'c3', title: 'Chương 3', total_segments: 20, translated_segments: 0 },
  ];

  // Selected c1 and c2
  const incomplete = getIncompleteChaptersForTranslation(chapters, ['c1', 'c2']);
  assert.equal(incomplete.length, 1);
  assert.equal(incomplete[0].id, 'c2');

  // Translation mode validation: invalid because c2 is incomplete
  const transValidation = validatePdfExportRequest({
    bookId: 'book-123',
    selectedChapterIds: ['c1', 'c2'],
    contentMode: 'translation',
    chapters,
  });
  assert.equal(transValidation.valid, false);
  assert.equal(transValidation.error, 'incompleteChapters');
  assert.equal(transValidation.incompleteCount, 1);

  // Original source mode validation: VALID even though c2 is incomplete!
  const sourceValidation = validatePdfExportRequest({
    bookId: 'book-123',
    selectedChapterIds: ['c1', 'c2'],
    contentMode: 'source',
    chapters,
  });
  assert.equal(sourceValidation.valid, true);

  // Translation mode validation with only c1: VALID
  const c1Validation = validatePdfExportRequest({
    bookId: 'book-123',
    selectedChapterIds: ['c1'],
    contentMode: 'translation',
    chapters,
  });
  assert.equal(c1Validation.valid, true);

  // Empty selection validation: INVALID
  const emptyValidation = validatePdfExportRequest({
    bookId: 'book-123',
    selectedChapterIds: [],
    contentMode: 'translation',
    chapters,
  });
  assert.equal(emptyValidation.valid, false);
  assert.equal(emptyValidation.error, 'noChaptersSelected');
});
