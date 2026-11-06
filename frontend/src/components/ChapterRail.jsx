import React, { useState, useMemo } from 'react';
import { Search, BookOpen, CheckCircle, Clock, X } from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';
import { getChapterTitle, getChapterTitleSearchStrings, hasRawBlockGlyphs } from '../i18n/chapterTitle';

export default function ChapterRail({
  chapters = [],
  activeChapterId,
  onSelectChapter,
  isOpenMobile,
  onCloseMobile,
}) {
  const [searchQuery, setSearchQuery] = useState('');
  const { t, lang } = usePreferences();

  const filteredChapters = useMemo(() => {
    if (!searchQuery.trim()) return chapters;
    const q = searchQuery.toLowerCase();
    return chapters.filter((c) => {
      if (String(c.order ?? c.spine_index ?? '').includes(q)) return true;
      if (String(c.id).toLowerCase().includes(q)) return true;
      if (c.title) {
        const variants = getChapterTitleSearchStrings(c.title);
        return variants.some(v => v && v.toLowerCase().includes(q));
      }
      return false;
    });
  }, [chapters, searchQuery]);

  return (
    <>
      {/* Mobile backdrop */}
      {isOpenMobile && (
        <div
          className="chapter-rail-backdrop"
          onClick={onCloseMobile}
          aria-hidden="true"
        />
      )}

      <aside className={`chapter-rail ${isOpenMobile ? 'open' : ''}`}>
        <div className="chapter-rail-header">
          <div className="rail-title-row">
            <span className="rail-title">{t('tocTitle')}</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className="rail-count">{t('tocCount', { count: chapters.length })}</span>
              {isOpenMobile && (
                <button
                  type="button"
                  className="btn btn-icon-sm"
                  onClick={onCloseMobile}
                  style={{ border: 'none', background: 'transparent' }}
                  aria-label="Đóng danh sách chương"
                >
                  <X size={16} />
                </button>
              )}
            </div>
          </div>

          <div className="search-box">
            <Search size={14} className="search-icon" />
            <input
              type="text"
              className="search-input"
              placeholder={t('searchChapter')}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              aria-label={t('searchChapter')}
            />
          </div>
        </div>

        <ul className="chapter-list">
          {filteredChapters.length === 0 ? (
            <li style={{ padding: '24px 12px', textAlign: 'center', color: 'var(--color-muted)', fontSize: 13 }}>
              {t('noChapterFound')}
            </li>
          ) : (
            filteredChapters.map((chapter, index) => {
              const isActive = chapter.id === activeChapterId;
              const totalSegments = chapter.total_segments ?? chapter.segment_count ?? 0;
              const completedSegments = chapter.translated_segments ?? chapter.translated_segment_count ?? chapter.completed_segments ?? 0;
              const percent = totalSegments > 0 ? Math.round((completedSegments / totalSegments) * 100) : 0;
              const isDone = totalSegments > 0 && completedSegments >= totalSegments;
              const chapterOrderNum = chapter.order !== undefined
                ? chapter.order + 1
                : chapter.spine_index !== undefined
                  ? chapter.spine_index + 1
                  : index + 1;
              const fallbackTitle = t('chapterItemTitle', { index: chapterOrderNum });
              const displayTitle = chapter.title ? getChapterTitle(chapter.title, lang, fallbackTitle) : fallbackTitle;
              const tooltipTitle = hasRawBlockGlyphs(chapter.title) ? displayTitle : (chapter.title || fallbackTitle);

              return (
                <li key={chapter.id}>
                  <button
                    type="button"
                    className={`chapter-item ${isActive ? 'active' : ''}`}
                    onClick={() => {
                      onSelectChapter(chapter.id);
                      if (isOpenMobile) onCloseMobile();
                    }}
                    aria-current={isActive ? 'page' : undefined}
                  >
                    <div className="chapter-item-top">
                      <span className="chapter-item-title" title={tooltipTitle}>
                        {displayTitle}
                      </span>
                      {isDone ? (
                        <CheckCircle size={14} color="var(--color-success)" style={{ flexShrink: 0 }} />
                      ) : percent > 0 ? (
                        <Clock size={14} color="var(--color-primary)" style={{ flexShrink: 0 }} />
                      ) : null}
                    </div>

                    <div className="chapter-item-meta">
                      <span>{chapter.order !== undefined ? t('chapterOrderTitle', { order: chapter.order + 1 }) : chapter.spine_index !== undefined ? t('chapterOrderTitle', { order: chapter.spine_index + 1 }) : t('chapterOrderTitle', { order: index + 1 })}</span>
                      <span>
                        {totalSegments > 0 ? t('chapterSegmentsCount', { completed: completedSegments, total: totalSegments }) : t('chapterNotSegmented')}
                      </span>
                    </div>

                    {totalSegments > 0 && (
                      <div className="chapter-progress-bar-bg" aria-hidden="true">
                        <div
                          className="chapter-progress-bar-fill"
                          style={{
                            width: `${percent}%`,
                            backgroundColor: isDone ? 'var(--color-success)' : 'var(--color-primary)',
                          }}
                        />
                      </div>
                    )}
                  </button>
                </li>
              );
            })
          )}
        </ul>
      </aside>
    </>
  );
}
