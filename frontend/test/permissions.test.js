import test from 'node:test';
import assert from 'node:assert';
import { getRoleCapabilities, assertCapability } from '../src/utils/permissions.js';

test('1. Role capabilities: Admin has full access to all controls', () => {
  const caps = getRoleCapabilities('admin');
  assert.strictEqual(caps.role, 'admin');
  assert.strictEqual(caps.isAdmin, true);
  assert.strictEqual(caps.isEditor, false);
  assert.strictEqual(caps.isViewer, false);

  // Admin-only capabilities
  assert.strictEqual(caps.canUpload, true);
  assert.strictEqual(caps.canChangeModel, true);
  assert.strictEqual(caps.canEditPresets, true);
  assert.strictEqual(caps.canManageJobs, true);
  assert.strictEqual(caps.canStartDiscovery, true);
  assert.strictEqual(caps.canRunAudit, true);
  assert.strictEqual(caps.canApplyCorrections, true);
  assert.strictEqual(caps.canAnalyzeStoryBible, true);

  // Content edits & approvals
  assert.strictEqual(caps.canEditChapter, true);
  assert.strictEqual(caps.canEditGlossary, true);
  assert.strictEqual(caps.canEditCharacters, true);
  assert.strictEqual(caps.canEditStoryBible, true);
  assert.strictEqual(caps.canReviewDiscovery, true);
  assert.strictEqual(caps.canExportPdf, true);
  assert.strictEqual(caps.canExportEpub, true);
});

test('2. Role capabilities: Editor has chapter/glossary/character/story-bible edits, discovery approvals, PDF; no admin actions', () => {
  const caps = getRoleCapabilities('editor');
  assert.strictEqual(caps.role, 'editor');
  assert.strictEqual(caps.isAdmin, false);
  assert.strictEqual(caps.isEditor, true);
  assert.strictEqual(caps.isViewer, false);

  // Allowed for editor
  assert.strictEqual(caps.canEditChapter, true);
  assert.strictEqual(caps.canEditGlossary, true);
  assert.strictEqual(caps.canEditCharacters, true);
  assert.strictEqual(caps.canEditStoryBible, true);
  assert.strictEqual(caps.canReviewDiscovery, true);
  assert.strictEqual(caps.canExportPdf, true);
  assert.strictEqual(caps.canExportEpub, true);

  // Forbidden for editor (Admin-only actions)
  assert.strictEqual(caps.canUpload, false);
  assert.strictEqual(caps.canChangeModel, false);
  assert.strictEqual(caps.canEditPresets, false);
  assert.strictEqual(caps.canManageJobs, false);
  assert.strictEqual(caps.canStartDiscovery, false);
  assert.strictEqual(caps.canRunAudit, false);
  assert.strictEqual(caps.canApplyCorrections, false);
  assert.strictEqual(caps.canAnalyzeStoryBible, false);
});

test('3. Role capabilities: Viewer has read-only + EPUB export only', () => {
  const caps = getRoleCapabilities('viewer');
  assert.strictEqual(caps.role, 'viewer');
  assert.strictEqual(caps.isAdmin, false);
  assert.strictEqual(caps.isEditor, false);
  assert.strictEqual(caps.isViewer, true);

  // Allowed for viewer
  assert.strictEqual(caps.canExportEpub, true);

  // All edits and admin actions forbidden
  assert.strictEqual(caps.canUpload, false);
  assert.strictEqual(caps.canChangeModel, false);
  assert.strictEqual(caps.canEditPresets, false);
  assert.strictEqual(caps.canManageJobs, false);
  assert.strictEqual(caps.canStartDiscovery, false);
  assert.strictEqual(caps.canRunAudit, false);
  assert.strictEqual(caps.canApplyCorrections, false);
  assert.strictEqual(caps.canAnalyzeStoryBible, false);
  assert.strictEqual(caps.canEditChapter, false);
  assert.strictEqual(caps.canEditGlossary, false);
  assert.strictEqual(caps.canEditCharacters, false);
  assert.strictEqual(caps.canEditStoryBible, false);
  assert.strictEqual(caps.canReviewDiscovery, false);
  assert.strictEqual(caps.canExportPdf, false);
});

test('4. Role capabilities: Unknown or empty role safely defaults to viewer', () => {
  const capsNull = getRoleCapabilities(null);
  assert.strictEqual(capsNull.role, 'viewer');
  assert.strictEqual(capsNull.isViewer, true);
  assert.strictEqual(capsNull.canEditChapter, false);

  const capsUndefined = getRoleCapabilities(undefined);
  assert.strictEqual(capsUndefined.role, 'viewer');
  assert.strictEqual(capsUndefined.isViewer, true);
});

test('5. assertCapability helper enforces security guards', () => {
  const editorCaps = getRoleCapabilities('editor');
  assert.doesNotThrow(() => assertCapability(editorCaps, 'canEditChapter'));
  assert.throws(() => assertCapability(editorCaps, 'canUpload'), /Missing canUpload/);
});
