import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  FileText,
  X,
  Loader2,
  AlertTriangle,
  Search,
  ChevronDown,
} from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';
import { getChapterTitle, getChapterTitleSearchStrings } from '../i18n/chapterTitle';
import * as api from '../api';
import {
  getChapterCompletionStatus,
  resolveExportChapterIds,
  filterChaptersBySearch,
  getIncompleteChaptersForTranslation,
} from '../utils/exportHelpers';

export default function PdfExportModal({
  isOpen,
  onClose,
  books = [],
  activeBookId = null,
  activeChapterId = null,
  isDirty = false,
  isBibleDirty = false,
}) {
  const { t, lang } = usePreferences();

  // Selected book in modal
  const [selectedBookId, setSelectedBookId] = useState(activeBookId || (books[0]?.id ?? null));

  // Chapters of the selected book
  const [chapters, setChapters] = useState([]);
  const [isLoadingChapters, setIsLoadingChapters] = useState(false);

  // Content mode: 'translation' | 'source'
  const [contentMode, setContentMode] = useState('translation');

  // Scope: 'current' | 'all' | 'custom' (default 'current' when activeChapterId is present)
  const [scope, setScope] = useState(activeChapterId ? 'current' : 'all');

  // Custom selected chapter IDs (Set)
  const [customSelectedIds, setCustomSelectedIds] = useState(new Set());

  // Search filter query
  const [searchQuery, setSearchQuery] = useState('');

  // Status & error states
  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState(null);

  // Sequence ref to handle out-of-order async responses
  const bookSeqRef = useRef(0);
  const abortControllerRef = useRef(null);

  // Refs for keyboard focus trap and restoring focus
  const dialogRef = useRef(null);
  const initialFocusRef = useRef(null);
  const previousFocusRef = useRef(null);

  // Focus management: save focus on open, trap focus, restore on close
  useEffect(() => {
    if (isOpen) {
      previousFocusRef.current = document.activeElement;
      const initialBookId = activeBookId || (books[0]?.id ?? null);
      setSelectedBookId(initialBookId);
      setContentMode('translation');
      // Default scope current selected chapter when available to avoid blocking on untranslated chapters
      setScope(activeChapterId ? 'current' : 'all');
      setCustomSelectedIds(new Set());
      setSearchQuery('');
      setExportError(null);

      // Focus initial interactive element
      const focusTimer = requestAnimationFrame(() => {
        if (initialFocusRef.current) {
          initialFocusRef.current.focus();
        }
      });
      return () => cancelAnimationFrame(focusTimer);
    } else {
      // Restore previous focus when modal closes
      if (previousFocusRef.current && typeof previousFocusRef.current.focus === 'function') {
        previousFocusRef.current.focus();
      }
    }
  }, [isOpen, activeBookId, activeChapterId, books]);

  // Keyboard focus trap and Escape handler
  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (e) => {
      if (e.key === 'Escape' && !isExporting) {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }

      if (e.key === 'Tab') {
        const dialog = dialogRef.current;
        if (!dialog) return;

        const focusable = dialog.querySelectorAll(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;

        const firstElement = focusable[0];
        const lastElement = focusable[focusable.length - 1];

        if (e.shiftKey) {
          if (document.activeElement === firstElement) {
            e.preventDefault();
            lastElement.focus();
          }
        } else {
          if (document.activeElement === lastElement) {
            e.preventDefault();
            firstElement.focus();
          }
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, isExporting, onClose]);

  // Load chapters when selectedBookId changes
  useEffect(() => {
    if (!isOpen || !selectedBookId) {
      setChapters([]);
      return;
    }

    const seq = ++bookSeqRef.current;
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    abortControllerRef.current = new AbortController();
    const signal = abortControllerRef.current.signal;

    setIsLoadingChapters(true);
    setExportError(null);
    // Reset custom selections on book change to prevent stale chapter IDs
    setCustomSelectedIds(new Set());

    api
      .getChapters(selectedBookId, signal)
      .then((chList) => {
        if (seq === bookSeqRef.current && !signal.aborted) {
          setChapters(chList);
        }
      })
      .catch((err) => {
        if (err.name !== 'AbortError' && seq === bookSeqRef.current) {
          setExportError(err.message || t('errLoadTOC', { err: '' }));
          setChapters([]);
        }
      })
      .finally(() => {
        if (seq === bookSeqRef.current) {
          setIsLoadingChapters(false);
        }
      });

    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [isOpen, selectedBookId, t]);

  // Determine effective activeChapterId: strictly prevent stale chapter IDs when another book is chosen
  const effectiveActiveChapterId = useMemo(() => {
    if (selectedBookId !== activeBookId) {
      return chapters[0]?.id || null;
    }
    return activeChapterId || (chapters[0]?.id || null);
  }, [selectedBookId, activeBookId, activeChapterId, chapters]);

  // Filtered chapters for custom list display
  const filteredChapters = useMemo(() => {
    return filterChaptersBySearch(chapters, searchQuery, getChapterTitleSearchStrings);
  }, [chapters, searchQuery]);

  // Compute effective resolved chapter IDs to export
  const effectiveChapterIds = useMemo(() => {
    return resolveExportChapterIds(scope, chapters, effectiveActiveChapterId, customSelectedIds);
  }, [scope, chapters, effectiveActiveChapterId, customSelectedIds]);

  // Find incomplete chapters among selected
  const incompleteSelected = useMemo(() => {
    if (contentMode !== 'translation') return [];
    return getIncompleteChaptersForTranslation(chapters, effectiveChapterIds);
  }, [contentMode, chapters, effectiveChapterIds]);

  // Quick selection handlers
  const handleSelectAll = () => {
    setCustomSelectedIds(new Set(chapters.map((c) => c.id)));
  };

  const handleDeselectAll = () => {
    setCustomSelectedIds(new Set());
  };

  const handleSelectTranslatedOnly = () => {
    const translatedIds = chapters
      .filter((c) => getChapterCompletionStatus(c) === 'translated')
      .map((c) => c.id);
    setCustomSelectedIds(new Set(translatedIds));
  };

  const handleToggleChapter = (chapterId) => {
    setCustomSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(chapterId)) {
        next.delete(chapterId);
      } else {
        next.add(chapterId);
      }
      return next;
    });
  };

  // Perform PDF Export
  const handleExport = async () => {
    if (!selectedBookId || effectiveChapterIds.length === 0 || isExporting) return;

    setExportError(null);
    setIsExporting(true);

    try {
      await api.exportBookPdf(selectedBookId, {
        chapter_ids: effectiveChapterIds,
        content: contentMode,
      });
      // Close modal on successful download trigger
      onClose();
    } catch (err) {
      setExportError(err.message || t('errExportPdf', { err: '' }));
    } finally {
      setIsExporting(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="pdf-modal-title"
    >
      <div
        ref={dialogRef}
        className="modal-dialog"
        style={{ maxWidth: 580, maxHeight: '90vh', display: 'flex', flexDirection: 'column' }}
      >
        {/* Modal Header */}
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <FileText size={20} color="var(--color-primary)" />
            <h3 id="pdf-modal-title" className="modal-title">
              {t('pdfModalTitle')}
            </h3>
          </div>
          <button
            ref={initialFocusRef}
            type="button"
            className="btn btn-icon-sm"
            onClick={onClose}
            disabled={isExporting}
            aria-label={t('ariaCloseError')}
            style={{ border: 'none', background: 'transparent', cursor: 'pointer' }}
          >
            <X size={18} />
          </button>
        </div>

        {/* Modal Body */}
        <div
          className="modal-body"
          style={{ overflowY: 'auto', padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 16 }}
        >
          {/* Unsaved draft warning */}
          {(isDirty || isBibleDirty) && (
            <div
              className="alert-banner alert-banner-warning"
              style={{ margin: 0, padding: '10px 14px', fontSize: 13 }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                <AlertTriangle size={16} style={{ marginTop: 2, flexShrink: 0 }} />
                <span>{t('pdfUnsavedWarning')}</span>
              </div>
            </div>
          )}

          {/* Book selector with connected label */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label
              htmlFor="pdf-book-select"
              style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-navy)' }}
            >
              {t('pdfSelectBook')}
            </label>
            <div className="book-select-wrapper" style={{ width: '100%' }}>
              <select
                id="pdf-book-select"
                className="book-select"
                aria-label={t('pdfSelectBook')}
                value={selectedBookId || ''}
                onChange={(e) => setSelectedBookId(e.target.value)}
                disabled={isExporting || books.length === 0}
              >
                {books.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.title || t('chapterTitle', { id: b.id })}{' '}
                    {b.chapter_count ? t('chaptersCount', { count: b.chapter_count }) : ''}
                  </option>
                ))}
              </select>
              <ChevronDown
                size={16}
                style={{
                  position: 'absolute',
                  right: 10,
                  top: '50%',
                  transform: 'translateY(-50%)',
                  pointerEvents: 'none',
                  color: 'var(--color-muted)',
                }}
              />
            </div>
          </div>

          {/* Content mode selector with connected label & radiogroup */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label
              id="pdf-content-group-label"
              style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-navy)' }}
            >
              {t('pdfContent')}
            </label>
            <div
              role="radiogroup"
              aria-labelledby="pdf-content-group-label"
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                gap: 8,
              }}
            >
              <button
                type="button"
                role="radio"
                aria-checked={contentMode === 'translation'}
                aria-label={t('pdfContentTranslation')}
                className={`btn ${contentMode === 'translation' ? 'btn-primary' : ''}`}
                style={{
                  flex: '1 1 220px',
                  minWidth: 0,
                  minHeight: 38,
                  height: 'auto',
                  fontSize: 13,
                  fontWeight: 500,
                  justifyContent: 'flex-start',
                  padding: '8px 12px',
                  whiteSpace: 'normal',
                  textAlign: 'left',
                  lineHeight: 1.35,
                }}
                onClick={() => setContentMode('translation')}
                disabled={isExporting}
              >
                <span
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: '50%',
                    border: '2px solid currentColor',
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    marginRight: 6,
                    flexShrink: 0,
                  }}
                >
                  {contentMode === 'translation' && (
                    <span
                      style={{
                        width: 6,
                        height: 6,
                        borderRadius: '50%',
                        backgroundColor: 'currentColor',
                      }}
                    />
                  )}
                </span>
                <span style={{ wordBreak: 'break-word' }}>{t('pdfContentTranslation')}</span>
              </button>

              <button
                type="button"
                role="radio"
                aria-checked={contentMode === 'source'}
                aria-label={t('pdfContentSource')}
                className={`btn ${contentMode === 'source' ? 'btn-primary' : ''}`}
                style={{
                  flex: '1 1 220px',
                  minWidth: 0,
                  minHeight: 38,
                  height: 'auto',
                  fontSize: 13,
                  fontWeight: 500,
                  justifyContent: 'flex-start',
                  padding: '8px 12px',
                  whiteSpace: 'normal',
                  textAlign: 'left',
                  lineHeight: 1.35,
                }}
                onClick={() => setContentMode('source')}
                disabled={isExporting}
              >
                <span
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: '50%',
                    border: '2px solid currentColor',
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    marginRight: 6,
                    flexShrink: 0,
                  }}
                >
                  {contentMode === 'source' && (
                    <span
                      style={{
                        width: 6,
                        height: 6,
                        borderRadius: '50%',
                        backgroundColor: 'currentColor',
                      }}
                    />
                  )}
                </span>
                <span style={{ wordBreak: 'break-word' }}>{t('pdfContentSource')}</span>
              </button>
            </div>
          </div>

          {/* Scope selection with connected label & radiogroup */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label
              id="pdf-scope-group-label"
              style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-navy)' }}
            >
              {t('pdfScope')}
            </label>
            <div
              role="radiogroup"
              aria-labelledby="pdf-scope-group-label"
              style={{
                display: 'flex',
                gap: 8,
                flexWrap: 'wrap',
              }}
            >
              <button
                type="button"
                role="radio"
                aria-checked={scope === 'current'}
                aria-label={t('pdfScopeCurrent')}
                className={`btn ${scope === 'current' ? 'btn-primary' : ''}`}
                style={{ height: 34, fontSize: 13, padding: '0 12px' }}
                onClick={() => setScope('current')}
                disabled={isExporting || isLoadingChapters || chapters.length === 0}
              >
                {t('pdfScopeCurrent')}
              </button>

              <button
                type="button"
                role="radio"
                aria-checked={scope === 'all'}
                aria-label={t('pdfScopeAll', { count: chapters.length })}
                className={`btn ${scope === 'all' ? 'btn-primary' : ''}`}
                style={{ height: 34, fontSize: 13, padding: '0 12px' }}
                onClick={() => setScope('all')}
                disabled={isExporting || isLoadingChapters}
              >
                {t('pdfScopeAll', { count: chapters.length })}
              </button>

              <button
                type="button"
                role="radio"
                aria-checked={scope === 'custom'}
                aria-label={t('pdfScopeCustom')}
                className={`btn ${scope === 'custom' ? 'btn-primary' : ''}`}
                style={{ height: 34, fontSize: 13, padding: '0 12px' }}
                onClick={() => setScope('custom')}
                disabled={isExporting || isLoadingChapters}
              >
                {t('pdfScopeCustom')}
              </button>
            </div>
          </div>

          {/* Custom selection chapter list */}
          {scope === 'custom' && (
            <div
              style={{
                border: '1px solid var(--color-border)',
                borderRadius: 'var(--radius)',
                padding: '12px',
                display: 'flex',
                flexDirection: 'column',
                gap: 10,
                backgroundColor: 'var(--color-rail-bg)',
              }}
            >
              {/* Quick actions row */}
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 8,
                  flexWrap: 'wrap',
                }}
              >
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <button
                    type="button"
                    className="btn"
                    style={{ height: 28, fontSize: 12, padding: '0 8px' }}
                    onClick={handleSelectAll}
                    disabled={isExporting || isLoadingChapters || chapters.length === 0}
                  >
                    {t('pdfQuickSelectAll')}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    style={{ height: 28, fontSize: 12, padding: '0 8px' }}
                    onClick={handleDeselectAll}
                    disabled={isExporting || customSelectedIds.size === 0}
                  >
                    {t('pdfQuickDeselectAll')}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    style={{ height: 28, fontSize: 12, padding: '0 8px' }}
                    onClick={handleSelectTranslatedOnly}
                    disabled={isExporting || isLoadingChapters || chapters.length === 0}
                  >
                    {t('pdfQuickSelectTranslated')}
                  </button>
                </div>

                <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-muted)' }}>
                  {t('pdfSelectedCount', {
                    selected: customSelectedIds.size,
                    total: chapters.length,
                  })}
                </span>
              </div>

              {/* Search input with accessible label */}
              <div style={{ position: 'relative' }}>
                <label
                  htmlFor="pdf-chapter-search"
                  style={{
                    position: 'absolute',
                    width: 1,
                    height: 1,
                    padding: 0,
                    margin: -1,
                    overflow: 'hidden',
                    clip: 'rect(0, 0, 0, 0)',
                    border: 0,
                  }}
                >
                  {t('pdfSearchPlaceholder')}
                </label>
                <Search
                  size={14}
                  style={{
                    position: 'absolute',
                    left: 10,
                    top: '50%',
                    transform: 'translateY(-50%)',
                    color: 'var(--color-muted)',
                  }}
                />
                <input
                  id="pdf-chapter-search"
                  type="text"
                  className="search-input"
                  aria-label={t('pdfSearchPlaceholder')}
                  style={{
                    width: '100%',
                    height: 32,
                    paddingLeft: 32,
                    fontSize: 13,
                    borderRadius: 4,
                  }}
                  placeholder={t('pdfSearchPlaceholder')}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  disabled={isExporting}
                />
              </div>

              {/* Scrollable list */}
              <div
                style={{
                  maxHeight: 200,
                  overflowY: 'auto',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 4,
                  backgroundColor: 'var(--color-bg)',
                  border: '1px solid var(--color-border)',
                  borderRadius: 4,
                  padding: '6px',
                }}
              >
                {isLoadingChapters ? (
                  <div style={{ padding: '16px', textAlign: 'center', color: 'var(--color-muted)' }}>
                    <Loader2 size={16} className="animate-spin" style={{ display: 'inline', marginRight: 6 }} />
                    {t('pdfLoadingChapters')}
                  </div>
                ) : filteredChapters.length === 0 ? (
                  <div style={{ padding: '16px', textAlign: 'center', color: 'var(--color-muted)', fontSize: 13 }}>
                    {t('pdfNoChaptersFound')}
                  </div>
                ) : (
                  filteredChapters.map((ch, idx) => {
                    const isChecked = customSelectedIds.has(ch.id);
                    const status = getChapterCompletionStatus(ch);
                    const titleText = ch.title
                      ? getChapterTitle(ch.title, lang)
                      : t('chapterItemTitle', { index: ch.order ?? ch.spine_index ?? idx + 1 });
                    const inputId = `pdf-chap-checkbox-${ch.id}`;

                    return (
                      <div
                        key={ch.id}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          gap: 8,
                          padding: '6px 10px',
                          borderRadius: 4,
                          backgroundColor: isChecked ? 'var(--color-surface-hover)' : 'transparent',
                          transition: 'background 0.15s ease',
                          fontSize: 13,
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flex: 1 }}>
                          <input
                            id={inputId}
                            type="checkbox"
                            checked={isChecked}
                            aria-label={titleText}
                            onChange={() => handleToggleChapter(ch.id)}
                            disabled={isExporting}
                            style={{ cursor: 'pointer' }}
                          />
                          <label
                            htmlFor={inputId}
                            style={{
                              cursor: 'pointer',
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              color: 'var(--color-navy)',
                              fontWeight: isChecked ? 600 : 400,
                              flex: 1,
                            }}
                          >
                            {titleText}
                          </label>
                        </div>

                        {/* Status Badge */}
                        <div style={{ flexShrink: 0 }}>
                          {status === 'translated' ? (
                            <span className="view-badge-done">{t('pdfStatusTranslated')}</span>
                          ) : status === 'partial' ? (
                            <span className="view-badge-partial">
                              {t('pdfStatusPartial', {
                                completed: ch.translated_segments,
                                total: ch.total_segments,
                              })}
                            </span>
                          ) : (
                            <span
                              style={{
                                fontSize: 10,
                                fontWeight: 500,
                                color: 'var(--color-muted)',
                                padding: '1px 6px',
                                borderRadius: 10,
                                border: '1px solid var(--color-border)',
                              }}
                            >
                              {t('pdfStatusUntranslated')}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          )}

          {/* Selected Summary Info */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              fontSize: 13,
              color: 'var(--color-muted)',
              padding: '6px 0',
              borderTop: '1px solid var(--color-border)',
            }}
          >
            <span>
              {t('pdfSelectedCount', {
                selected: effectiveChapterIds.length,
                total: chapters.length,
              })}
            </span>
          </div>

          {/* Incomplete translation warning banner */}
          {contentMode === 'translation' && incompleteSelected.length > 0 && (
            <div
              className="alert-banner alert-banner-warning"
              style={{ margin: 0, padding: '10px 14px', fontSize: 13 }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                <AlertTriangle size={16} style={{ marginTop: 2, flexShrink: 0 }} />
                <div>
                  <strong>{t('pdfIncompleteWarning', { count: incompleteSelected.length })}</strong>
                  <div style={{ marginTop: 4, fontSize: 12, opacity: 0.9 }}>
                    {incompleteSelected.slice(0, 3).map((c, i) => (
                      <span key={c.id}>
                        {c.title ? getChapterTitle(c.title, lang) : c.id}
                        {i < Math.min(incompleteSelected.length, 3) - 1 ? '; ' : ''}
                      </span>
                    ))}
                    {incompleteSelected.length > 3 && ` ... (+${incompleteSelected.length - 3})`}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Export Error Banner */}
          {exportError && (
            <div
              className="alert-banner alert-banner-error"
              style={{ margin: 0, padding: '10px 14px', fontSize: 13 }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                <AlertTriangle size={16} style={{ marginTop: 2, flexShrink: 0 }} />
                <span>{exportError}</span>
              </div>
            </div>
          )}
        </div>

        {/* Modal Footer */}
        <div className="modal-footer">
          <button
            type="button"
            className="btn"
            onClick={onClose}
            disabled={isExporting}
          >
            {t('cancel')}
          </button>

          <button
            type="button"
            className="btn btn-primary"
            onClick={handleExport}
            disabled={
              isExporting ||
              effectiveChapterIds.length === 0 ||
              isLoadingChapters ||
              !selectedBookId
            }
          >
            {isExporting ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                <span>{t('pdfGenerating')}</span>
              </>
            ) : (
              <>
                <FileText size={16} />
                <span>{t('pdfDownloadBtn')}</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
