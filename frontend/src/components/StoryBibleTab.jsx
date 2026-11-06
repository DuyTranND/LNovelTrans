import React, { useState } from 'react';
import { Sparkles, Save, Loader2, AlertCircle, Check } from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';

export default function StoryBibleTab({
  storyBibleText = '',
  draftText,
  onDraftChange,
  isDirty = false,
  onSaveStoryBible,
  onAnalyzeStoryBible,
  isAnalyzing,
  isSaving,
  isLoading,
  canEditStoryBible = true,
  canAnalyzeStoryBible = true,
}) {
  const currentContent = draftText !== undefined ? draftText : storyBibleText;
  const [message, setMessage] = useState(null);
  const { t } = usePreferences();

  const handleChange = (e) => {
    if (onDraftChange) {
      onDraftChange(e.target.value);
    }
    setMessage(null);
  };

  const handleSave = async () => {
    try {
      await onSaveStoryBible(currentContent);
      setMessage({ type: 'success', text: t('msgBibleSaved') });
    } catch (err) {
      setMessage({ type: 'error', text: err.message || t('errSaveBibleFallback') });
    }
  };

  const handleAnalyze = async () => {
    try {
      setMessage(null);
      await onAnalyzeStoryBible();
      setMessage({
        type: 'success',
        text: t('msgBibleAnalyzed'),
      });
    } catch (err) {
      setMessage({ type: 'error', text: err.message || t('errAnalyzeBibleFallback') });
    }
  };

  return (
    <div className="bible-container">
      {/* Top action bar */}
      <div className="bible-toolbar">
        <div>
          <h3 style={{ fontSize: 16, fontWeight: 600, color: 'var(--color-navy)' }}>
            {t('bibleTitle')}
          </h3>
          <p style={{ fontSize: 13, color: 'var(--color-muted)', marginTop: 2 }}>
            {t('bibleDesc')}
          </p>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button
            type="button"
            className="btn"
            onClick={handleAnalyze}
            disabled={isAnalyzing || isLoading || !canAnalyzeStoryBible}
            title={!canAnalyzeStoryBible ? t('adminOnly') : t('analyzeCh1')}
          >
            {isAnalyzing ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                {t('analyzingCh1')}
              </>
            ) : (
              <>
                <Sparkles size={16} color="var(--color-primary)" />
                {t('analyzeCh1')}
              </>
            )}
          </button>

          <button
            type="button"
            className="btn btn-primary"
            onClick={handleSave}
            disabled={!isDirty || isSaving || isLoading || !canEditStoryBible}
            title={!canEditStoryBible ? t('editorOnly') : t('saveBible')}
          >
            {isSaving ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                {t('saving')}
              </>
            ) : (
              <>
                <Save size={16} />
                {t('saveBible')}
              </>
            )}
          </button>
        </div>
      </div>

      {message && (
        <div
          className={`alert-banner ${message.type === 'success' ? 'alert-banner-info' : 'alert-banner-error'}`}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {message.type === 'success' ? <Check size={16} /> : <AlertCircle size={16} />}
            <span>{message.text}</span>
          </div>
        </div>
      )}

      {/* Editor Textarea */}
      {isLoading ? (
        <div style={{ padding: 60, textAlign: 'center', color: 'var(--color-muted)' }}>
          <Loader2 size={24} className="animate-spin" style={{ margin: '0 auto 12px' }} />
          {t('loadingBible')}
        </div>
      ) : (
        <textarea
          className="bible-textarea"
          placeholder={t('biblePlaceholder')}
          value={currentContent}
          onChange={handleChange}
          readOnly={!canEditStoryBible}
          aria-label={t('bibleTitle')}
        />
      )}
    </div>
  );
}
