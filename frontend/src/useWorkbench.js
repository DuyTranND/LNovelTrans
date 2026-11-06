import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import * as api from './api';
import { usePreferences } from './contexts/PreferencesContext';
import { getRoleCapabilities } from './utils/permissions';
import { buildChapterSaveConflicts } from './utils/conflictHelpers';

export function useWorkbench(user) {
  const { t, lang } = usePreferences();
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);
  const stableT = useCallback((key, params) => tRef.current(key, params), []);

  const capabilities = useMemo(() => getRoleCapabilities(user?.role), [user?.role]);

  // Global & Settings State
  const [settings, setSettings] = useState({
    model: 'gemini-3.5-flash-lite',
    key_configured: false,
    gemini_configured: false,
  });
  const [keyStatus, setKeyStatus] = useState(null);
  const [models, setModels] = useState([]);
  const [isUpdatingModel, setIsUpdatingModel] = useState(false);
  const [globalError, setGlobalError] = useState(null);
  const [conflictNotice, setConflictNotice] = useState(null);

  // Books State
  const [books, setBooks] = useState([]);
  const [activeBookId, setActiveBookId] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const [isExporting, setIsExporting] = useState(false);

  // Chapters & Active Chapter State
  const [chapters, setChapters] = useState([]);
  const [activeChapterId, setActiveChapterId] = useState(null);
  const [currentChapter, setCurrentChapter] = useState(null);
  const [isLoadingChapter, setIsLoadingChapter] = useState(false);

  // Segment Draft Edits State: { [segmentId]: { source?: string, translation?: string } }
  const [draftEdits, setDraftEdits] = useState({});
  const [isSavingSegments, setIsSavingSegments] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState(null);
  const [hasNewProgressNotice, setHasNewProgressNotice] = useState(false);

  // Tabs State
  const [activeTab, setActiveTab] = useState('translation'); // 'translation' | 'glossary' | 'characters' | 'presets' | 'bible' | 'jobs' | 'consistency'

  // Glossary State
  const [glossary, setGlossary] = useState([]);
  const [isLoadingGlossary, setIsLoadingGlossary] = useState(false);

  // Story Bible State
  const [storyBibleText, setStoryBibleText] = useState('');
  const [storyBibleDraft, setStoryBibleDraft] = useState(null);
  const [isLoadingBible, setIsLoadingBible] = useState(false);
  const [isSavingBible, setIsSavingBible] = useState(false);
  const [isAnalyzingBible, setIsAnalyzingBible] = useState(false);

  // Jobs State
  const [jobs, setJobs] = useState([]);
  const [isLoadingJobs, setIsLoadingJobs] = useState(false);
  const [actionJobLoadingId, setActionJobLoadingId] = useState(null);

  // Presets State
  const [presets, setPresets] = useState([]);
  const [selectedPresetId, setSelectedPresetId] = useState('');

  // Modals & Mobile View State
  const [confirmModalConfig, setConfirmModalConfig] = useState({ isOpen: false });
  const [conflictModalConfig, setConflictModalConfig] = useState({ isOpen: false, conflicts: [], latestChapter: null });
  const [isMobileRailOpen, setIsMobileRailOpen] = useState(false);

  // Refs for tracking latest dirty state and selection across async boundaries
  const isDirtyRef = useRef(false);
  const isBibleDirtyRef = useRef(false);
  const activeBookIdRef = useRef(null);
  const activeChapterIdRef = useRef(null);
  const bookRequestSeqRef = useRef(0);
  const chapterRequestSeqRef = useRef(0);
  const chapterAbortRef = useRef(null);
  const bookAbortRef = useRef(null);

  // Keep selection refs in sync
  useEffect(() => {
    activeBookIdRef.current = activeBookId;
  }, [activeBookId]);

  useEffect(() => {
    activeChapterIdRef.current = activeChapterId;
  }, [activeChapterId]);

  // Compute dirty states
  const isSegmentDirty = useMemo(() => {
    if (!currentChapter || !currentChapter.segments) return false;
    return Object.entries(draftEdits).some(([segId, draft]) => {
      const seg = currentChapter.segments.find((s) => s.id === segId);
      if (!seg) return false;
      const isSrcChanged = draft.source !== undefined && draft.source !== (seg.source || '');
      const isTransChanged = draft.translation !== undefined && draft.translation !== (seg.translation || '');
      return isSrcChanged || isTransChanged;
    });
  }, [currentChapter, draftEdits]);

  const isBibleDirty = useMemo(() => {
    return storyBibleDraft !== null && storyBibleDraft !== storyBibleText;
  }, [storyBibleDraft, storyBibleText]);

  useEffect(() => {
    isDirtyRef.current = isSegmentDirty;
  }, [isSegmentDirty]);

  useEffect(() => {
    isBibleDirtyRef.current = isBibleDirty;
  }, [isBibleDirty]);

  // Browser Unload Warning
  useEffect(() => {
    const handleBeforeUnload = (e) => {
      if (isDirtyRef.current || isBibleDirtyRef.current) {
        e.preventDefault();
        e.returnValue = stableT('unloadWarning');
        return e.returnValue;
      }
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, []);

  // ---------------------------------------------------------------------------
  // Initial Mount: Fetch Settings, Models, Books & Key Status
  // ---------------------------------------------------------------------------
  useEffect(() => {
    async function initApp() {
      try {
        const [sett, mList, bList, kStatus] = await Promise.all([
          api.getSettings().catch((err) => {
            setGlobalError(stableT('errServerSettings', { err: err.message || stableT('errCannotConnect') }));
            return {
              model: 'gemini-3.5-flash-lite',
              key_configured: false,
              gemini_configured: false,
            };
          }),
          api.getModels().catch(() => []),
          api.getBooks().catch((err) => {
            setGlobalError(stableT('errLoadBooks', { err: err.message || stableT('errServer') }));
            return [];
          }),
          api.getKeyStatus().catch(() => null),
        ]);

        setSettings(sett);
        if (kStatus) setKeyStatus(kStatus);
        const configuredModel = sett.model || 'gemini-3.5-flash-lite';
        const finalModels = mList.length > 0
          ? (mList.some((m) => m.id === configuredModel) ? mList : [{ id: configuredModel, name: configuredModel }, ...mList])
          : [{ id: configuredModel, name: configuredModel }];
        setModels(finalModels);
        setBooks(bList);

        if (bList.length > 0) {
          setActiveBookId(bList[0].id);
          activeBookIdRef.current = bList[0].id;
        }
      } catch (err) {
        setGlobalError(stableT('errInitSystem', { err: err.message || stableT('errCannotConnect') }));
      }
    }
    initApp();
  }, []);

  // ---------------------------------------------------------------------------
  // Load Active Book Data
  // ---------------------------------------------------------------------------
  const loadGlossary = useCallback(async (bookId) => {
    if (!bookId) {
      setGlossary([]);
      return;
    }
    setIsLoadingGlossary(true);
    try {
      const data = await api.getGlossary(bookId);
      if (activeBookIdRef.current === bookId) {
        setGlossary(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        setGlobalError(stableT('errLoadGlossary', { err: err.message || stableT('errServer') }));
      }
      if (activeBookIdRef.current === bookId) {
        setGlossary([]);
      }
    } finally {
      if (activeBookIdRef.current === bookId) {
        setIsLoadingGlossary(false);
      }
    }
  }, [stableT]);

  const loadBookData = useCallback(async (bookId) => {
    if (!bookId) return;

    const seq = ++bookRequestSeqRef.current;
    if (bookAbortRef.current) {
      bookAbortRef.current.abort();
    }
    bookAbortRef.current = new AbortController();
    const signal = bookAbortRef.current.signal;

    setIsLoadingBible(true);
    setIsLoadingJobs(true);
    setIsLoadingGlossary(true);

    try {
      const [chList, bbData, jbList, gloList] = await Promise.all([
        api.getChapters(bookId, signal).catch((err) => {
          if (err.name !== 'AbortError') setGlobalError(stableT('errLoadTOC', { err: err.message }));
          return [];
        }),
        api.getStoryBible(bookId, signal).catch(() => ({ text: '' })),
        api.getJobs(bookId, signal).catch(() => []),
        api.getGlossary(bookId, signal).catch((err) => {
          if (err.name !== 'AbortError') {
            setGlobalError(stableT('errLoadGlossary', { err: err.message || stableT('errServer') }));
          }
          return [];
        }),
      ]);

      if (signal.aborted || seq !== bookRequestSeqRef.current || bookId !== activeBookIdRef.current) return;

      setChapters(chList);
      setStoryBibleText(bbData.text || '');
      setStoryBibleDraft(null);
      setJobs(jbList);
      setGlossary(Array.isArray(gloList) ? gloList : []);

      if (chList.length > 0) {
        performLoadChapter(chList[0].id);
      } else {
        setActiveChapterId(null);
        activeChapterIdRef.current = null;
        setCurrentChapter(null);
        setDraftEdits({});
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        setGlobalError(stableT('errLoadBookData', { err: err.message || stableT('errNetwork') }));
      }
    } finally {
      if (seq === bookRequestSeqRef.current) {
        setIsLoadingBible(false);
        setIsLoadingJobs(false);
        setIsLoadingGlossary(false);
      }
    }
  }, []);

  useEffect(() => {
    if (activeBookId) {
      loadBookData(activeBookId);
      api.getPresets(activeBookId)
        .then((data) => {
          setPresets(data || []);
          const def = (data || []).find((p) => p.is_default) || (data || [])[0];
          if (def) setSelectedPresetId(def.id);
        })
        .catch(() => {});
    } else {
      setPresets([]);
      setSelectedPresetId('');
    }
  }, [activeBookId, loadBookData]);

  // ---------------------------------------------------------------------------
  // Load Chapter with Sequence and Dirty Check
  // ---------------------------------------------------------------------------
  const performLoadChapter = async (chapterId) => {
    const seq = ++chapterRequestSeqRef.current;
    if (chapterAbortRef.current) {
      chapterAbortRef.current.abort();
    }
    chapterAbortRef.current = new AbortController();
    const signal = chapterAbortRef.current.signal;

    setActiveChapterId(chapterId);
    activeChapterIdRef.current = chapterId;
    setIsLoadingChapter(true);
    setDraftEdits({});
    setHasNewProgressNotice(false);

    try {
      const chapterData = await api.getChapter(chapterId, signal);
      if (!signal.aborted && seq === chapterRequestSeqRef.current && activeChapterIdRef.current === chapterId) {
        setCurrentChapter(chapterData);
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        setGlobalError(stableT('errLoadChapter', { err: err.message || stableT('errNetwork') }));
      }
    } finally {
      if (seq === chapterRequestSeqRef.current) {
        setIsLoadingChapter(false);
      }
    }
  };

  const handleSelectChapter = (chapterId) => {
    if (chapterId === activeChapterId) return;

    if (isSegmentDirty) {
      setConfirmModalConfig({
        isOpen: true,
        title: stableT('unsavedTranslationTitle'),
        message: stableT('unsavedTranslationMsg'),
        confirmLabel: stableT('leaveAndDiscard'),
        confirmVariant: 'danger',
        onConfirm: () => {
          setConfirmModalConfig({ isOpen: false });
          setDraftEdits({});
          setHasNewProgressNotice(false);
          performLoadChapter(chapterId);
        },
        onCancel: () => setConfirmModalConfig({ isOpen: false }),
      });
      return;
    }

    performLoadChapter(chapterId);
  };

  // ---------------------------------------------------------------------------
  // Safe Polling for Active Jobs
  // ---------------------------------------------------------------------------
  const hasActiveJob = jobs.some((j) => ['running', 'pausing', 'queued'].includes(j.status));

  const isJobActiveForCurrentChapter = jobs.some(
    (j) =>
      ['running', 'pausing', 'queued'].includes(j.status) &&
      (!j.chapter_ids || j.chapter_ids.length === 0 || j.chapter_ids.includes(activeChapterId))
  );

  useEffect(() => {
    let isCancelled = false;
    let pollAbort = new AbortController();

    // Refresh key status on selection or active job state change
    api.getKeyStatus(pollAbort.signal)
      .then((status) => {
        if (!isCancelled && status) setKeyStatus(status);
      })
      .catch(() => {});

    if (!activeBookId || !hasActiveJob) {
      return () => {
        isCancelled = true;
        pollAbort.abort();
      };
    }

    const interval = setInterval(async () => {
      const pollBookId = activeBookIdRef.current;
      const pollChapterId = activeChapterIdRef.current;
      if (!pollBookId || isCancelled) return;

      try {
        const [updatedJobs, updatedChapters, updatedKeyStatus] = await Promise.all([
          api.getJobs(pollBookId, pollAbort.signal).catch(() => null),
          api.getChapters(pollBookId, pollAbort.signal).catch(() => null),
          api.getKeyStatus(pollAbort.signal).catch(() => null),
        ]);

        if (isCancelled || pollBookId !== activeBookIdRef.current) return;

        if (updatedJobs) setJobs(updatedJobs);
        if (updatedChapters) setChapters(updatedChapters);
        if (updatedKeyStatus) setKeyStatus(updatedKeyStatus);

        if (pollChapterId) {
          if (isDirtyRef.current) {
            setHasNewProgressNotice(true);
          } else {
            const chData = await api.getChapter(pollChapterId, pollAbort.signal).catch(() => null);
            // Apply only for still-current selection and still-clean at completion!
            if (
              !isCancelled &&
              chData &&
              activeChapterIdRef.current === pollChapterId &&
              activeBookIdRef.current === pollBookId &&
              !isDirtyRef.current
            ) {
              setCurrentChapter(chData);
              setHasNewProgressNotice(false);
            } else if (isDirtyRef.current) {
              setHasNewProgressNotice(true);
            }
          }
        }
      } catch {
        // Suppress polling error logs to avoid noise and secret leakage
      }
    }, 2000);

    return () => {
      isCancelled = true;
      pollAbort.abort();
      clearInterval(interval);
    };
  }, [activeBookId, hasActiveJob]);

  // ---------------------------------------------------------------------------
  // Model Settings
  // ---------------------------------------------------------------------------
  const handleModelChange = async (newModel) => {
    if (!capabilities.canChangeModel) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    setIsUpdatingModel(true);
    try {
      const updated = await api.updateSettings(newModel);
      setSettings((prev) => ({ ...prev, ...updated, model: newModel }));
    } catch (err) {
      setGlobalError(stableT('errChangeModel', { err: err.message || stableT('errUpdate') }));
    } finally {
      setIsUpdatingModel(false);
    }
  };

  // ---------------------------------------------------------------------------
  // Book Switch & Upload (Guarded for Unsaved Edits)
  // ---------------------------------------------------------------------------
  const handleSelectBook = (bookId) => {
    if (bookId === activeBookId) return;

    if (isSegmentDirty || isBibleDirty) {
      setConfirmModalConfig({
        isOpen: true,
        title: stableT('unsavedChangesTitle'),
        message: stableT('unsavedBookSwitchMsg'),
        confirmLabel: stableT('switchAndDiscard'),
        confirmVariant: 'danger',
        onConfirm: () => {
          setConfirmModalConfig({ isOpen: false });
          setDraftEdits({});
          setStoryBibleDraft(null);
          setActiveBookId(bookId);
        },
        onCancel: () => setConfirmModalConfig({ isOpen: false }),
      });
      return;
    }

    setActiveBookId(bookId);
  };

  const handleUploadBook = async (file) => {
    if (!capabilities.canUpload) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    const doUpload = async () => {
      setIsUploading(true);
      setUploadError(null);
      try {
        const newBook = await api.uploadBook(file);
        const bList = await api.getBooks().catch(() => []);
        setBooks(bList);
        if (newBook && newBook.id) {
          setActiveBookId(newBook.id);
        } else if (bList.length > 0) {
          setActiveBookId(bList[bList.length - 1].id);
        }
      } catch (err) {
        setUploadError(stableT('errUploadBook', { err: err.message || '' }));
      } finally {
        setIsUploading(false);
      }
    };

    if (isSegmentDirty || isBibleDirty) {
      setConfirmModalConfig({
        isOpen: true,
        title: stableT('unsavedChangesTitle'),
        message: stableT('unsavedUploadMsg'),
        confirmLabel: stableT('continueUpload'),
        confirmVariant: 'danger',
        onConfirm: () => {
          setConfirmModalConfig({ isOpen: false });
          setDraftEdits({});
          setStoryBibleDraft(null);
          doUpload();
        },
        onCancel: () => setConfirmModalConfig({ isOpen: false }),
      });
      return;
    }

    doUpload();
  };

  const handleExportBook = async () => {
    if (!activeBookId) return;

    const doExport = async () => {
      setIsExporting(true);
      try {
        await api.exportBook(activeBookId);
      } catch (err) {
        setGlobalError(stableT('errExport', { err: err.message || stableT('errSystem') }));
      } finally {
        setIsExporting(false);
      }
    };

    if (isSegmentDirty) {
      setConfirmModalConfig({
        isOpen: true,
        title: stableT('unsavedTranslationTitle'),
        message: stableT('unsavedExportMsg'),
        confirmLabel: stableT('exportSaved'),
        confirmVariant: 'primary',
        onConfirm: () => {
          setConfirmModalConfig({ isOpen: false });
          doExport();
        },
        onCancel: () => setConfirmModalConfig({ isOpen: false }),
      });
      return;
    }

    doExport();
  };

  // ---------------------------------------------------------------------------
  // Segment Edits & Saving: Only changed fields and changed segments
  // ---------------------------------------------------------------------------
  const handleSegmentChange = (segmentId, field, value) => {
    setDraftEdits((prev) => {
      const existing = prev[segmentId] || {};
      return {
        ...prev,
        [segmentId]: {
          ...existing,
          [field]: value,
        },
      };
    });
  };

  const handleSaveSegments = async () => {
    if (!capabilities.canEditChapter) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    if (!currentChapter || !isSegmentDirty) return;

    const changedPayload = [];
    for (const seg of currentChapter.segments || []) {
      const draft = draftEdits[seg.id];
      if (!draft) continue;
      const patch = { id: seg.id, expected_revision: seg.revision };
      let hasChange = false;

      if (draft.source !== undefined && draft.source !== (seg.source || '')) {
        patch.source = draft.source;
        hasChange = true;
      }
      if (draft.translation !== undefined && draft.translation !== (seg.translation || '')) {
        patch.translation = draft.translation;
        hasChange = true;
      }

      if (hasChange) {
        changedPayload.push(patch);
      }
    }

    if (changedPayload.length === 0) {
      setDraftEdits({});
      return;
    }

    setIsSavingSegments(true);
    try {
      await api.updateChapterSegments(currentChapter.id, changedPayload);

      // Refresh saved chapter from response / backend to invalidate stale translation
      const refreshed = await api.getChapter(currentChapter.id);
      setCurrentChapter(refreshed);
      setDraftEdits({});
      setLastSavedAt(new Date().toLocaleTimeString(document.documentElement.lang === 'en' ? 'en-US' : 'vi-VN'));
      setConflictNotice(null);

      // Update chapter in rail
      if (activeBookId) {
        const updatedChapters = await api.getChapters(activeBookId).catch(() => null);
        if (updatedChapters) setChapters(updatedChapters);
      }
    } catch (err) {
      if (err.status === 409 && Array.isArray(err.conflicts) && err.conflicts.length > 0) {
        try {
          const latestChapter = await api.getChapter(currentChapter.id);
          // Verify latest-chapter and active-book identity guards
          if (
            activeChapterIdRef.current === currentChapter.id &&
            activeBookIdRef.current === activeBookId &&
            latestChapter &&
            latestChapter.id === currentChapter.id
          ) {
            const conflicts = buildChapterSaveConflicts(
              changedPayload,
              latestChapter.segments,
              draftEdits,
              err.conflicts
            );
            // Only open conflict resolution modal if structured revision conflicts exist
            if (conflicts.length > 0) {
              setConflictModalConfig({
                isOpen: true,
                conflicts,
                latestChapter,
              });
              setGlobalError(stableT('errConflictOccurred'));
              return;
            }
          }
        } catch {
          // Fall through to standard error handling
        }
      }
      // Preserve drafts on error (generic 409 or other error)
      setGlobalError(stableT('errSaveTranslation', { err: err.message || stableT('errServer') }));
    } finally {
      setIsSavingSegments(false);
    }
  };

  const handleResolveConflict = ({ updatedDraftEdits, latestChapter }) => {
    if (latestChapter) {
      setCurrentChapter(latestChapter);
    }
    setDraftEdits(updatedDraftEdits);
    setConflictModalConfig({ isOpen: false, conflicts: [], latestChapter: null });
    setGlobalError(null);
    setConflictNotice(stableT('conflictNoticeResolved'));
  };

  const handleCancelConflict = () => {
    setConflictModalConfig({ isOpen: false, conflicts: [], latestChapter: null });
  };

  const handleRefreshChapterSafe = useCallback(async () => {
    const curChapterId = activeChapterIdRef.current;
    const curBookId = activeBookIdRef.current;
    if (!curChapterId || !curBookId) return;

    try {
      const [latestChapter, latestChapters] = await Promise.all([
        api.getChapter(curChapterId),
        api.getChapters(curBookId).catch(() => null),
      ]);

      if (
        activeChapterIdRef.current === curChapterId &&
        activeBookIdRef.current === curBookId &&
        latestChapter
      ) {
        setCurrentChapter(latestChapter);
        if (latestChapters) {
          setChapters(latestChapters);
        }
      }
    } catch (err) {
      setGlobalError(err.message || stableT('errServer'));
    }
  }, [stableT]);

  const handleDiscardEdits = () => {
    setConfirmModalConfig({
      isOpen: true,
      title: stableT('discardEditsTitle'),
      message: stableT('discardEditsMsg'),
      confirmLabel: stableT('discardChanges'),
      confirmVariant: 'danger',
      onConfirm: () => {
        setDraftEdits({});
        setConfirmModalConfig({ isOpen: false });
      },
      onCancel: () => setConfirmModalConfig({ isOpen: false }),
    });
  };

  // ---------------------------------------------------------------------------
  // Story Bible Handlers
  // ---------------------------------------------------------------------------
  const handleBibleDraftChange = (text) => {
    setStoryBibleDraft(text);
  };

  const handleSaveStoryBible = async (text) => {
    if (!capabilities.canEditStoryBible) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    if (!activeBookId) return;
    setIsSavingBible(true);
    try {
      await api.updateStoryBible(activeBookId, text);
      setStoryBibleText(text);
      setStoryBibleDraft(null);
    } catch (err) {
      setGlobalError(stableT('errSaveBible', { err: err.message || stableT('errStorage') }));
      throw err;
    } finally {
      setIsSavingBible(false);
    }
  };

  const handleAnalyzeStoryBible = async () => {
    if (!capabilities.canAnalyzeStoryBible) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    if (!activeBookId) return;
    setIsAnalyzingBible(true);
    try {
      await api.analyzeStoryBible(activeBookId);
      const updated = await api.getStoryBible(activeBookId);
      setStoryBibleText(updated.text || '');
      setStoryBibleDraft(null);
    } catch (err) {
      setGlobalError(stableT('errAnalyzeBible', { err: err.message || stableT('errAI') }));
      throw err;
    } finally {
      setIsAnalyzingBible(false);
    }
  };

  // ---------------------------------------------------------------------------
  // Glossary Handlers
  // ---------------------------------------------------------------------------
  const handleCreateGlossaryTerm = async (term) => {
    if (!capabilities.canEditGlossary) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    if (!activeBookId) return;
    try {
      const created = await api.createGlossaryTerm(activeBookId, term);
      await loadGlossary(activeBookId);
      return created;
    } catch (err) {
      setGlobalError(err.message || stableT('errServer'));
      throw err;
    }
  };

  const handleUpdateGlossaryTerm = async (termId, term) => {
    if (!capabilities.canEditGlossary) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    try {
      const updated = await api.updateGlossaryTerm(termId, term);
      await loadGlossary(activeBookIdRef.current);
      return updated;
    } catch (err) {
      setGlobalError(err.message || stableT('errServer'));
      throw err;
    }
  };

  const handleDeleteGlossaryTerm = async (termId) => {
    if (!capabilities.canEditGlossary) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    try {
      await api.deleteGlossaryTerm(termId);
      await loadGlossary(activeBookIdRef.current);
    } catch (err) {
      setGlobalError(err.message || stableT('errServer'));
      throw err;
    }
  };

  // ---------------------------------------------------------------------------
  // Job Launching & Control Handlers
  // ---------------------------------------------------------------------------
  const executeLaunchJob = async (chapterIds) => {
    if (!capabilities.canManageJobs) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    try {
      const payload = {
        chapter_ids: chapterIds,
        review: true,
        model: settings.model,
      };
      if (selectedPresetId) {
        payload.preset_id = selectedPresetId;
      }
      await api.createJob(activeBookId, payload);
      const jbList = await api.getJobs(activeBookId);
      setJobs(jbList);
      setActiveTab('jobs');
    } catch (err) {
      setGlobalError(stableT('errLaunchJob', { err: err.message || stableT('errQueue') }));
    }
  };

  const handleLaunchSelectedChapterJob = () => {
    if (!activeBookId || !activeChapterId) return;
    if (hasActiveJob) {
      setGlobalError(stableT('jobActiveError'));
      return;
    }

    if (isSegmentDirty) {
      setConfirmModalConfig({
        isOpen: true,
        title: stableT('unsavedChapterTitle'),
        message: stableT('unsavedLaunchMsg'),
        confirmLabel: stableT('continueTranslate'),
        confirmVariant: 'danger',
        onConfirm: () => {
          setConfirmModalConfig({ isOpen: false });
          setDraftEdits({});
          executeLaunchJob([activeChapterId]);
        },
        onCancel: () => setConfirmModalConfig({ isOpen: false }),
      });
      return;
    }

    executeLaunchJob([activeChapterId]);
  };

  const handlePromptLaunchAllJobs = () => {
    if (!activeBookId || chapters.length === 0) return;
    if (hasActiveJob) {
      setGlobalError(stableT('jobActiveError'));
      return;
    }

    setConfirmModalConfig({
      isOpen: true,
      title: stableT('confirmTranslateAllTitle'),
      message: stableT('confirmTranslateAllMsg', { count: chapters.length }),
      confirmLabel: stableT('startTranslateAll'),
      confirmVariant: 'primary',
      onConfirm: () => {
        setConfirmModalConfig({ isOpen: false });
        if (isSegmentDirty) setDraftEdits({});
        executeLaunchJob(chapters.map((c) => c.id));
      },
      onCancel: () => setConfirmModalConfig({ isOpen: false }),
    });
  };

  const handlePauseJob = async (jobId) => {
    if (!capabilities.canManageJobs) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    setActionJobLoadingId(jobId);
    // Optimistically show 'pausing' until actual paused
    setJobs((prev) => prev.map((j) => (j.id === jobId ? { ...j, status: 'pausing' } : j)));

    try {
      await api.pauseJob(jobId);
      const jbList = await api.getJobs(activeBookId);
      setJobs(jbList);
    } catch (err) {
      setGlobalError(stableT('errPauseJob', { err: err.message || stableT('errCannotConnect') }));
    } finally {
      setActionJobLoadingId(null);
    }
  };

  const handleResumeJob = async (jobId) => {
    if (!capabilities.canManageJobs) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    setActionJobLoadingId(jobId);
    try {
      await api.resumeJob(jobId);
      const jbList = await api.getJobs(activeBookId);
      setJobs(jbList);
    } catch (err) {
      setGlobalError(stableT('errResumeJob', { err: err.message || stableT('errCannotConnect') }));
    } finally {
      setActionJobLoadingId(null);
    }
  };

  const handleRetryJob = async (jobId) => {
    if (!capabilities.canManageJobs) {
      setGlobalError(stableT('unauthorizedAction'));
      return;
    }
    setActionJobLoadingId(jobId);
    try {
      await api.retryJob(jobId);
      const jbList = await api.getJobs(activeBookId);
      setJobs(jbList);
    } catch (err) {
      setGlobalError(stableT('errRetryJob', { err: err.message || stableT('errCannotConnect') }));
    } finally {
      setActionJobLoadingId(null);
    }
  };

  // Find running job for current chapter if any
  const runningJobForCurrentChapter = jobs.find(
    (j) =>
      ['running', 'pausing', 'paused'].includes(j.status) &&
      (!j.chapter_ids || j.chapter_ids.length === 0 || j.chapter_ids.includes(activeChapterId))
  );

  const activeBook = books.find((b) => b.id === activeBookId);

  return {
    // Capabilities
    capabilities,

    // State
    settings,
    keyStatus,
    models,
    isUpdatingModel,
    globalError,
    setGlobalError,
    conflictNotice,
    setConflictNotice,
    books,
    activeBook,
    activeBookId,
    isUploading,
    uploadError,
    isExporting,
    chapters,
    activeChapterId,
    currentChapter,
    isLoadingChapter,
    draftEdits,
    isDirty: isSegmentDirty,
    isBibleDirty,
    isSavingSegments,
    lastSavedAt,
    hasNewProgressNotice,
    activeTab,
    setActiveTab,
    glossary,
    isLoadingGlossary,
    storyBibleText,
    storyBibleDraft,
    isLoadingBible,
    isSavingBible,
    isAnalyzingBible,
    jobs,
    isLoadingJobs,
    actionJobLoadingId,
    hasActiveJob,
    presets,
    selectedPresetId,
    setSelectedPresetId,
    isJobActiveForCurrentChapter,
    runningJobForCurrentChapter,
    confirmModalConfig,
    conflictModalConfig,
    isMobileRailOpen,
    setIsMobileRailOpen,

    // Handlers
    handleModelChange,
    handleSelectBook,
    handleUploadBook,
    handleExportBook,
    handleSelectChapter,
    handleSegmentChange,
    handleSaveSegments,
    handleDiscardEdits,
    handleBibleDraftChange,
    handleSaveStoryBible,
    handleAnalyzeStoryBible,
    handleCreateGlossaryTerm,
    handleUpdateGlossaryTerm,
    handleDeleteGlossaryTerm,
    handleResolveConflict,
    handleCancelConflict,
    handleRefreshChapterSafe,
    loadGlossary,
    handleLaunchSelectedChapterJob,
    handlePromptLaunchAllJobs,
    handlePauseJob,
    handleResumeJob,
    handleRetryJob,
  };
}
