import test from 'node:test';
import assert from 'node:assert';
import {
  buildChapterSaveConflicts,
  resolveConflictAcceptServer,
  resolveConflictRebaseDraft,
  resolveAllConflictsAcceptServer,
  resolveAllConflictsRebase,
} from '../src/utils/conflictHelpers.js';

test('1. buildChapterSaveConflicts: identifies revision mismatch and flags conflicting segments', () => {
  const changedPayload = [
    { id: 'seg-1', expected_revision: 2, translation: 'Bản dịch mới của tôi' },
    { id: 'seg-2', expected_revision: 5, translation: 'Đoạn không đổi trên server' },
  ];

  const serverSegments = [
    { id: 'seg-1', revision: 3, order: 0, source: '원문 1', translation: 'Bản dịch máy chủ vừa cập nhật' },
    { id: 'seg-2', revision: 5, order: 1, source: '원문 2', translation: 'Bản dịch cũ' },
  ];

  const draftEdits = {
    'seg-1': { translation: 'Bản dịch mới của tôi' },
    'seg-2': { translation: 'Đoạn không đổi trên server' },
    'seg-3': { translation: 'Bản nháp ở đoạn khác không gửi lưu lần này' },
  };

  const conflicts = buildChapterSaveConflicts(changedPayload, serverSegments, draftEdits);

  assert.strictEqual(conflicts.length, 1);
  assert.strictEqual(conflicts[0].segmentId, 'seg-1');
  assert.strictEqual(conflicts[0].clientExpectedRevision, 2);
  assert.strictEqual(conflicts[0].serverRevision, 3);
  assert.strictEqual(conflicts[0].serverTranslation, 'Bản dịch máy chủ vừa cập nhật');
  assert.strictEqual(conflicts[0].draftTranslation, 'Bản dịch mới của tôi');
});

test('2. buildChapterSaveConflicts: returns empty array on generic 409 without revision mismatch', () => {
  const changedPayload = [
    { id: 'seg-1', expected_revision: 5, translation: 'Bản dịch thử' },
  ];

  const serverSegments = [
    { id: 'seg-1', revision: 5, order: 0, translation: 'Bản dịch thử' },
  ];

  const conflicts = buildChapterSaveConflicts(changedPayload, serverSegments, {});
  // Generic 409 (e.g., job active or rate limit) should NOT open revision conflict modal
  assert.strictEqual(conflicts.length, 0);
});

test('3. resolveConflictAcceptServer: discards draft edit for segment and preserves other drafts', () => {
  const draftEdits = {
    'seg-1': { translation: 'Bản nháp bị xung đột' },
    'seg-2': { translation: 'Bản nháp hợp lệ đoạn 2' },
    'seg-3': { translation: 'Bản nháp hợp lệ đoạn 3' },
  };

  const resolved = resolveConflictAcceptServer(draftEdits, 'seg-1');

  // seg-1 is removed so server text displays
  assert.strictEqual(resolved['seg-1'], undefined);
  // Other drafts are strictly preserved
  assert.deepStrictEqual(resolved['seg-2'], { translation: 'Bản nháp hợp lệ đoạn 2' });
  assert.deepStrictEqual(resolved['seg-3'], { translation: 'Bản nháp hợp lệ đoạn 3' });
});

test('4. resolveConflictRebaseDraft: keeps draft text for rebasing onto latest revision and preserves other drafts', () => {
  const draftEdits = {
    'seg-1': { translation: 'Bản nháp muốn giữ lại' },
    'seg-2': { translation: 'Bản nháp đoạn 2' },
  };

  const serverSeg = { id: 'seg-1', revision: 4, translation: 'Bản dịch máy chủ' };
  const resolved = resolveConflictRebaseDraft(draftEdits, 'seg-1', serverSeg);

  // seg-1 draft text remains intact
  assert.deepStrictEqual(resolved['seg-1'], { translation: 'Bản nháp muốn giữ lại' });
  // seg-2 draft text remains intact
  assert.deepStrictEqual(resolved['seg-2'], { translation: 'Bản nháp đoạn 2' });
});

test('5. Bulk conflict resolvers: accept all and rebase all preserve unaffected drafts', () => {
  const draftEdits = {
    'seg-1': { translation: 'Draft 1' },
    'seg-2': { translation: 'Draft 2' },
    'seg-unrelated': { translation: 'Draft 99' },
  };

  const conflicts = [
    { segmentId: 'seg-1', draftTranslation: 'Draft 1' },
    { segmentId: 'seg-2', draftTranslation: 'Draft 2' },
  ];

  const allAccepted = resolveAllConflictsAcceptServer(draftEdits, conflicts);
  assert.strictEqual(allAccepted['seg-1'], undefined);
  assert.strictEqual(allAccepted['seg-2'], undefined);
  assert.deepStrictEqual(allAccepted['seg-unrelated'], { translation: 'Draft 99' });

  const allRebased = resolveAllConflictsRebase(draftEdits, conflicts);
  assert.deepStrictEqual(allRebased['seg-1'], { translation: 'Draft 1' });
  assert.deepStrictEqual(allRebased['seg-2'], { translation: 'Draft 2' });
  assert.deepStrictEqual(allRebased['seg-unrelated'], { translation: 'Draft 99' });
});
