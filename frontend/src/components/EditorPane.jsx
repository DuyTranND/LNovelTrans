import React, { useState, useEffect, useMemo } from 'react';
import {
  FileText,
  AlertTriangle,
  Info,
  Languages,
  BookOpen,
  Edit3,
  CheckCircle,
} from 'lucide-react';
import {
  groupSegmentsIntoParagraphs,
  getReadingParagraphs,
  getSegmentText,
  isSegmentTranslated,
  isParagraphTranslated,
  isHeadSegment,
} from '../utils/paragraphGrouping';
import { usePreferences } from '../contexts/PreferencesContext';

export default function EditorPane({
  chapter,
  segments = [],
  draftEdits = {},
  onSegmentChange,
  isLoading,
  isJobActive = false,
  readOnly = false,
  canEditChapter = true,
}) {
  const { t } = usePreferences();
  const isEditable = canEditChapter && !readOnly;
  
  // 'translation' (Bản dịch tiếng Việt) or 'source' (Bản gốc tiếng Hàn)
  const [viewMode, setViewMode] = useState('translation');
  // false = Chế độ đọc tiểu thuyết liên tục; true = Chế độ chỉnh sửa
  const [isEditMode, setIsEditMode] = useState(false);

  // Return to default translation reading view when chapter ID changes (without discarding drafts on toggles)
  useEffect(() => {
    setViewMode('translation');
    setIsEditMode(false);
  }, [chapter?.id]);

  const activeField = viewMode === 'translation' ? 'translation' : 'source';

  // Group raw DOM segments into coherent whole-chapter paragraphs for editing
  const paragraphs = useMemo(() => {
    return groupSegmentsIntoParagraphs(segments);
  }, [segments]);

  // Pure reading-selection paragraphs for novel reading mode
  const readingParagraphs = useMemo(() => {
    return getReadingParagraphs(segments, { field: activeField, draftEdits });
  }, [segments, activeField, draftEdits]);

  if (isLoading) {
    return (
      <div className="chapter-loading-state">
        <p>{t('loadingChapter')}</p>
      </div>
    );
  }

  if (!chapter) {
    return (
      <div className="empty-state-card">
        <FileText className="empty-state-icon" />
        <h3 className="empty-state-title">{t('noChapterSelectedTitle')}</h3>
        <p className="empty-state-desc">
          {t('noChapterSelectedDesc')}
        </p>
      </div>
    );
  }

  // Calculate translation statistics for readable body (exclude head metadata descendants)
  const bodySegments = segments.filter((s) => !isHeadSegment(s));
  const nonWhitespaceBodySegments = bodySegments.filter((s) => !s.is_whitespace);
  const totalContentCount = nonWhitespaceBodySegments.length;
  const translatedCount = nonWhitespaceBodySegments.filter((s) =>
    isSegmentTranslated(s, draftEdits)
  ).length;

  const hasAnyTranslation = translatedCount > 0;
  const isPartial = hasAnyTranslation && translatedCount < totalContentCount;
  const isFullyUntranslated = totalContentCount > 0 && !hasAnyTranslation;

  return (
    <div className="chapter-document-layout">
      {/* Chapter Review Notice Banner */}
      {(chapter.review_issues?.length > 0 || chapter.review) && (
        <div className="alert-banner alert-banner-info">
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
            <Info size={16} style={{ marginTop: 2, flexShrink: 0 }} />
            <div>
              <strong>{t('reviewIssuesLabel')}</strong>{' '}
              {chapter.review_issues?.length > 0
                ? chapter.review_issues.join('; ')
                : typeof chapter.review === 'string'
                ? chapter.review
                : JSON.stringify(chapter.review)}
            </div>
          </div>
        </div>
      )}

      {/* Editing Lock Banner during active background job */}
      {isJobActive && (
        <div className="alert-banner alert-banner-warning">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertTriangle size={16} />
            <span>{t('jobActiveLock')}</span>
          </div>
        </div>
      )}

      {/* Document View Toolbar */}
      <div className="chapter-view-controls">
        {/* View Switcher: Vietnamese Translation vs Korean Source */}
        <div className="view-mode-selector" role="group" aria-label="Lựa chọn văn bản xem">
          <button
            type="button"
            className={`view-mode-btn ${viewMode === 'translation' ? 'active' : ''}`}
            onClick={() => setViewMode('translation')}
            title={t('viewTranslation')}
          >
            <Languages size={15} />
            <span>{t('viewTranslation')}</span>
            {hasAnyTranslation && !isPartial && (
              <span className="view-badge-done">{t('badgeDone')}</span>
            )}
            {isPartial && (
              <span className="view-badge-partial">{t('badgePartial')}</span>
            )}
          </button>

          <button
            type="button"
            className={`view-mode-btn ${viewMode === 'source' ? 'active' : ''}`}
            onClick={() => setViewMode('source')}
            title={t('viewSource')}
          >
            <BookOpen size={15} />
            <span>{t('viewSource')}</span>
          </button>
        </div>

        {/* Reading vs Edit Mode Toggle */}
        <div className="edit-mode-controls">
          {isEditMode ? (
            <button
              type="button"
              className="btn btn-sm btn-primary"
              onClick={() => setIsEditMode(false)}
              title={t('modeRead')}
            >
              <BookOpen size={14} />
              <span>{t('modeRead')}</span>
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setIsEditMode(true)}
              disabled={isJobActive || !isEditable}
              title={!isEditable ? t('viewerReadOnly') : t('modeEdit')}
            >
              <Edit3 size={14} />
              <span>{t('modeEdit')}</span>
            </button>
          )}
        </div>
      </div>

      {/* Partial Translation Banner in Vietnamese View */}
      {viewMode === 'translation' && isPartial && (
        <div className="alert-banner alert-banner-warning">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertTriangle size={16} />
            <span dangerouslySetInnerHTML={{ __html: t('partialWarning', { completed: translatedCount, total: totalContentCount }) }}></span>
          </div>
        </div>
      )}

      {/* Fully Untranslated Notice in Vietnamese View */}
      {viewMode === 'translation' && isFullyUntranslated && !isEditMode && (
        <div className="chapter-notice-card">
          <Languages className="chapter-notice-icon" size={28} />
          <h3 className="chapter-notice-title">{t('untranslatedTitle')}</h3>
          <p className="chapter-notice-desc" dangerouslySetInnerHTML={{ __html: t('untranslatedDesc') }}></p>
        </div>
      )}

      {/* Empty Segments in Chapter */}
      {segments.length === 0 && (
        <div className="empty-state-card" style={{ padding: '40px 20px' }}>
          <p className="empty-state-desc">{t('noSegments')}</p>
        </div>
      )}

      {/* READING MODE: Continuous Whole-Chapter Novel Prose */}
      {!isEditMode && segments.length > 0 && !(viewMode === 'translation' && isFullyUntranslated) && (
        <article className="novel-document-container" aria-label="Nội dung chương truyện">
          {readingParagraphs.map((para) => {
            // Heading treatment
            if (para.isHeading) {
              const HeadingTag = `h${Math.min(6, Math.max(1, para.headingLevel || 1))}`;
              return (
                <HeadingTag
                  key={para.key}
                  className={`novel-prose-heading novel-h${para.headingLevel || 1}`}
                >
                  {para.nodes.map((node, nIdx) => (
                    <span
                      key={`${node.segId}-${nIdx}`}
                      className={node.isGap ? 'novel-gap-indicator' : ''}
                    >
                      {node.text}
                    </span>
                  ))}
                </HeadingTag>
              );
            }

            // Standard novel prose paragraph
            return (
              <p key={para.key} className="novel-prose-paragraph">
                {para.nodes.map((node, nIdx) => (
                  <React.Fragment key={`${node.segId}-${nIdx}`}>
                    {node.isBr && <br />}
                    <span className={`novel-prose-node ${node.isGap ? 'novel-gap-indicator' : ''}`}>
                      {node.text}
                    </span>
                  </React.Fragment>
                ))}
              </p>
            );
          })}
        </article>
      )}

      {/* EDIT MODE ("Chỉnh sửa"): Full Document Editable Cards */}
      {isEditMode && !readOnly && segments.length > 0 && (
        <div className="editor-paragraphs-container">
          <div className="editor-mode-banner">
            <span dangerouslySetInnerHTML={{ __html: t('editModeBanner', { mode: viewMode === 'translation' ? t('viewTranslation') : t('viewSource') }) }}></span>
          </div>

          {paragraphs.map((para) => {
            const isSingleSegment = para.segments.length === 1;

            if (isSingleSegment) {
              const seg = para.segments[0];
              const draft = draftEdits[seg.id] || {};
              const currentVal = draft[activeField] !== undefined ? draft[activeField] : (seg[activeField] || '');
              const isDirty = draft[activeField] !== undefined && draft[activeField] !== (seg[activeField] || '');

              return (
                <div
                  key={para.key || seg.id}
                  className={`editor-para-card ${isDirty ? 'dirty' : ''}`}
                >
                  <div className="editor-para-card-header">
                    <div className="editor-para-meta">
                      {para.isHead ? (
                        <span className="editor-badge-meta">{t('badgeMeta')}</span>
                      ) : para.isHeading ? (
                        <span className="editor-badge-heading">
                          {t('badgeHeading', { level: para.headingLevel || 1 })}
                        </span>
                      ) : (
                        <span className="editor-badge-para">{t('badgePara')}</span>
                      )}
                      {isDirty && (
                        <span className="editor-badge-dirty">{t('badgeDirty')}</span>
                      )}
                    </div>
                  </div>

                  {viewMode === 'translation' && seg.source && (
                    <div className="editor-source-reference">
                      <span className="reference-label">{t('sourceRef')}</span>
                      <span className="reference-text">{seg.source}</span>
                    </div>
                  )}

                  <textarea
                    className="editor-para-textarea"
                    value={currentVal}
                    placeholder={
                      para.isHead
                        ? (viewMode === 'translation' ? t('placeholderMetaTranslation') : t('placeholderMetaSource'))
                        : (viewMode === 'translation'
                            ? t('placeholderTrans')
                            : t('placeholderSource'))
                    }
                    onChange={(e) => onSegmentChange(seg.id, activeField, e.target.value)}
                    rows={Math.max(2, Math.ceil((currentVal.length || 1) / 60))}
                    disabled={isJobActive}
                    aria-label={para.isHead ? t('badgeMeta') : para.isHeading ? t('badgeHeading', { level: para.headingLevel || 1 }) : t('badgePara')}
                  />

                  {/* Review Issues if any */}
                  {seg.issues && seg.issues.length > 0 && (
                    <div className="editor-issues-row">
                      {seg.issues.map((iss, iIdx) => (
                        <span key={iIdx} className="segment-issue-tag">
                          ⚠ {iss}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            }

            // Multi-segment paragraph (contains inline tags like <span> or <em>)
            const hasAnyDirty = para.segments.some((s) => {
              const d = draftEdits[s.id] || {};
              return d[activeField] !== undefined && d[activeField] !== (s[activeField] || '');
            });

            return (
              <div
                key={para.key}
                className={`editor-para-card multi-segment ${hasAnyDirty ? 'dirty' : ''}`}
              >
                <div className="editor-para-card-header">
                  <div className="editor-para-meta">
                    {para.isHead ? (
                      <span className="editor-badge-meta">
                        {t('badgeMetaMulti', { count: para.segments.length })}
                      </span>
                    ) : para.isHeading ? (
                      <span className="editor-badge-heading">
                        {t('badgeHeading', { level: para.headingLevel || 1 })}
                      </span>
                    ) : (
                      <span className="editor-badge-para">
                        {t('badgeParaMulti', { count: para.segments.length })}
                      </span>
                    )}
                    {hasAnyDirty && (
                      <span className="editor-badge-dirty">{t('badgeDirty')}</span>
                    )}
                  </div>
                </div>

                <div className="editor-multi-segments-list">
                  {para.segments.map((seg, sIdx) => {
                    const draft = draftEdits[seg.id] || {};
                    const currentVal = draft[activeField] !== undefined ? draft[activeField] : (seg[activeField] || '');
                    const isDirty = draft[activeField] !== undefined && draft[activeField] !== (seg[activeField] || '');

                    return (
                      <div key={seg.id} className="editor-inline-segment-item">
                        {viewMode === 'translation' && seg.source && (
                          <div className="editor-source-reference-small">
                            <span className="reference-label">{t('sourceRefPart', { index: sIdx + 1 })}</span>
                            <span className="reference-text">{seg.source}</span>
                          </div>
                        )}

                        <textarea
                          className="editor-para-textarea"
                          value={currentVal}
                          placeholder={
                            viewMode === 'translation'
                              ? t('placeholderTransPart', { index: sIdx + 1 })
                              : t('placeholderSourcePart', { index: sIdx + 1 })
                          }
                          onChange={(e) => onSegmentChange(seg.id, activeField, e.target.value)}
                          rows={Math.max(1, Math.ceil((currentVal.length || 1) / 60))}
                          disabled={isJobActive}
                          aria-label={`Phần ${sIdx + 1}`}
                        />

                        {seg.issues && seg.issues.length > 0 && (
                          <div className="editor-issues-row">
                            {seg.issues.map((iss, iIdx) => (
                              <span key={iIdx} className="segment-issue-tag">
                                ⚠ {iss}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
