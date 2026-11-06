import React, { useRef } from 'react';
import { usePreferences } from '../contexts/PreferencesContext';
import {
  Book,
  Upload,
  Download,
  FileText,
  Cpu,
  Menu,
  ChevronDown,
  Loader2,
  Moon,
  Sun,
  Globe,
} from 'lucide-react';

export default function Header({
  books = [],
  activeBookId,
  onSelectBook,
  onUploadBook,
  isUploading,
  onExportBook,
  isExporting,
  onOpenPdfExportModal,
  models = [],
  currentModel,
  onModelChange,
  isUpdatingModel,
  keyConfigured,
  geminiConfigured,
  keyStatus,
  onToggleMobileRail,
}) {
  const fileInputRef = useRef(null);
  const { t, lang, setLang, theme, setTheme, computedTheme } = usePreferences();

  const handleFileChange = (e) => {
    const file = e.target.files?.[0];
    if (file) {
      onUploadBook(file);
      e.target.value = '';
    }
  };

  const toggleTheme = () => {
    setTheme(computedTheme === 'dark' ? 'light' : 'dark');
  };

  const toggleLang = () => {
    setLang(lang === 'vi' ? 'en' : 'vi');
  };

  return (
    <header className="app-header">
      {/* Brand & Mobile toggle */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <button
          type="button"
          className="btn btn-icon-sm mobile-rail-toggle"
          onClick={onToggleMobileRail}
          aria-label={t('ariaNav')}
        >
          <Menu size={18} />
        </button>

        <div className="header-brand">
          <Book size={22} color="var(--color-primary)" />
          <span>{t('brand')}</span>
        </div>
      </div>

      {/* Book selector */}
      <div className="header-center">
        <div className="book-select-wrapper">
          <select
            className="book-select"
            value={activeBookId || ''}
            onChange={(e) => onSelectBook(e.target.value)}
            disabled={books.length === 0}
            aria-label={t('ariaSelectBook')}
          >
            {books.length === 0 ? (
              <option value="">{t('noBooks')}</option>
            ) : (
              books.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.title || t('chapterTitle', { id: b.id })} {b.chapter_count ? t('chaptersCount', { count: b.chapter_count }) : ''}
                </option>
              ))
            )}
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

      {/* Actions & Settings */}
      <div className="header-actions">
        {/* Theme and Lang Toggles */}
        <button
          type="button"
          className="btn btn-icon-sm"
          onClick={toggleTheme}
          aria-label={t('themeToggle')}
          title={t('themeToggle')}
          style={{ border: 'none', background: 'transparent' }}
        >
          {computedTheme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
        </button>
        
        <button
          type="button"
          className="btn btn-icon-sm"
          onClick={toggleLang}
          aria-label={t('langToggle')}
          title={t('langToggle')}
          style={{ border: 'none', background: 'transparent', fontWeight: 600, fontSize: 13 }}
        >
          {lang.toUpperCase()}
        </button>

        {/* Model selector & API Status */}
        {(() => {
          const isConfigured = Boolean(geminiConfigured ?? keyConfigured ?? (keyStatus && keyStatus.count > 0));
          let statusText = isConfigured ? t('geminiConfigured') : t('geminiNotConfigured');
          let tooltipText = statusText;

          if (keyStatus && keyStatus.count > 0) {
            if (keyStatus.count > 1) {
              statusText = `Gemini: ${keyStatus.count} khóa (Round Robin)`;
            }
            if (keyStatus.slots) {
              const activeSlots = keyStatus.slots.filter((s) => s.configured);
              tooltipText = activeSlots
                .map((s) => {
                  const stateStr = s.status === 'cooldown'
                    ? `Chờ hạn mức (${s.cooldown_remaining}s)`
                    : s.inflight > 0
                    ? `Đang gửi (${s.inflight})`
                    : 'Sẵn sàng';
                  return `${s.label}: ${stateStr}`;
                })
                .join(' • ');
            }
          }

          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div
                className={`status-pill ${isConfigured ? 'configured' : ''}`}
                title={tooltipText}
              >
                <span className="status-dot" />
                <span style={{ fontSize: 12 }}>{statusText}</span>
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <Cpu size={16} color="var(--color-muted)" />
                <select
                  className="book-select"
                  style={{ width: 195, minWidth: 155, maxWidth: 220, height: 34, padding: '0 24px 0 8px', fontSize: 13 }}
                  value={currentModel || ''}
                  onChange={(e) => onModelChange(e.target.value)}
                  disabled={isUpdatingModel}
                  aria-label={t('ariaSelectModel')}
                >
                  {models.map((m) => {
                    const isAvail = m.available !== false;
                    const displayName = m.name || m.id;
                    const label = isAvail ? displayName : t('modelUnavailableKey', { name: displayName });
                    return (
                      <option key={m.id} value={m.id} disabled={!isAvail}>
                        {label}
                      </option>
                    );
                  })}
                </select>
              </div>
            </div>
          );
        })()}

        {/* Upload Button */}
        <input
          type="file"
          ref={fileInputRef}
          onChange={handleFileChange}
          accept=".epub,application/epub+zip"
          style={{ display: 'none' }}
        />
        <button
          type="button"
          className="btn"
          onClick={() => fileInputRef.current?.click()}
          disabled={isUploading}
          title={t('uploadEPUB')}
        >
          {isUploading ? <Loader2 size={16} className="animate-spin" /> : <Upload size={16} />}
          <span>{isUploading ? t('uploadingEPUB') : t('uploadEPUB')}</span>
        </button>

        {/* Export EPUB Button */}
        <button
          type="button"
          className="btn"
          onClick={onExportBook}
          disabled={!activeBookId || isExporting}
          title={t('exportEPUB')}
        >
          {isExporting ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
          <span>{isExporting ? t('exportingEPUB') : t('exportEPUB')}</span>
        </button>

        {/* Export PDF Button */}
        <button
          type="button"
          className="btn btn-primary"
          onClick={onOpenPdfExportModal}
          disabled={books.length === 0}
          title={t('exportPDF')}
        >
          <FileText size={16} />
          <span>{t('exportPDF')}</span>
        </button>
      </div>
    </header>
  );
}
