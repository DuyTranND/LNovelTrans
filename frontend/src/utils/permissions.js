/**
 * Role capabilities and permission helpers.
 *
 * Roles matching backend:
 * - admin: full permissions
 * - editor: chapter/glossary/character/story-bible edits, discovery approvals, PDF export
 * - viewer: read-only + EPUB export only
 *
 * Admin-only actions:
 * - Upload book
 * - Model settings change / writes
 * - Preset writes (create, update, delete, set default)
 * - Jobs management (launch, pause, resume, retry)
 * - Discovery run start / cancel
 * - Consistency audit start / cancel / retry / semantic review
 * - Correction actions (generate preview, toggle proposal, apply, rollback)
 * - Story bible AI analysis
 */

export function getRoleCapabilities(role = 'viewer') {
  const normRole = (typeof role === 'string' ? role.toLowerCase() : 'viewer');
  const isAdmin = normRole === 'admin';
  const isEditor = normRole === 'editor';
  const isViewer = !isAdmin && !isEditor;

  return {
    role: isAdmin ? 'admin' : (isEditor ? 'editor' : 'viewer'),
    isAdmin,
    isEditor,
    isViewer,

    // Admin-only capabilities
    canUpload: isAdmin,
    canChangeModel: isAdmin,
    canEditPresets: isAdmin,
    canManageJobs: isAdmin,
    canStartDiscovery: isAdmin,
    canRunAudit: isAdmin,
    canApplyCorrections: isAdmin,
    canAnalyzeStoryBible: isAdmin,

    // Admin + Editor capabilities
    canEditChapter: isAdmin || isEditor,
    canEditGlossary: isAdmin || isEditor,
    canEditCharacters: isAdmin || isEditor,
    canEditStoryBible: isAdmin || isEditor,
    canReviewDiscovery: isAdmin || isEditor,
    canExportPdf: isAdmin || isEditor,

    // Universal capabilities
    canExportEpub: true,
  };
}

export function assertCapability(capabilities, capabilityName, errorMessage) {
  if (!capabilities || !capabilities[capabilityName]) {
    const err = new Error(errorMessage || `Unauthorized: Missing ${capabilityName} capability`);
    err.status = 403;
    throw err;
  }
}
