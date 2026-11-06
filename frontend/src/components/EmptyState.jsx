import React, { useRef } from 'react';
import { BookOpen, Upload, Loader2, AlertCircle } from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';

export default function EmptyState({ onUpload, isUploading, uploadError }) {
  const fileInputRef = useRef(null);
  const { t } = usePreferences();

  const handleFileChange = (e) => {
    const file = e.target.files?.[0];
    if (file) {
      onUpload(file);
    }
  };

  return (
    <div style={{ maxWidth: 640, margin: '80px auto', padding: '0 20px', textAlign: 'center' }}>
      <div className="empty-state-card" style={{ padding: '48px 32px' }}>
        <div
          style={{
            width: 64,
            height: 64,
            borderRadius: '50%',
            backgroundColor: 'var(--color-primary-light)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            margin: '0 auto 20px',
            color: 'var(--color-primary)',
          }}
        >
          <BookOpen size={32} />
        </div>

        <h2 className="empty-state-title" style={{ fontSize: 22, marginBottom: 8 }}>
          {t('emptyTitle')}
        </h2>
        <p className="empty-state-desc" style={{ marginBottom: 24, fontSize: 15 }}>
          {t('emptyDesc')}
        </p>

        {uploadError && (
          <div
            className="alert-banner alert-banner-error"
            style={{ maxWidth: 440, margin: '0 auto 20px', textAlign: 'left' }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <AlertCircle size={18} />
              <span>{uploadError}</span>
            </div>
          </div>
        )}

        <input
          type="file"
          ref={fileInputRef}
          onChange={handleFileChange}
          accept=".epub,application/epub+zip"
          style={{ display: 'none' }}
        />

        <button
          type="button"
          className="btn btn-primary"
          style={{ height: 44, padding: '0 24px', fontSize: 15 }}
          onClick={() => fileInputRef.current?.click()}
          disabled={isUploading}
        >
          {isUploading ? (
            <>
              <Loader2 size={18} className="animate-spin" />
              {t('emptyProcessing')}
            </>
          ) : (
            <>
              <Upload size={18} />
              {t('uploadEPUB')}
            </>
          )}
        </button>

        <p style={{ marginTop: 16, fontSize: 12, color: 'var(--color-muted)' }}>
          {t('emptySupport')}
        </p>
      </div>
    </div>
  );
}
