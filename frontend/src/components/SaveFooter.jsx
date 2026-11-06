import React from 'react';
import { Save, Check, AlertCircle, Loader2, RotateCcw } from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';

export default function SaveFooter({
  isDirty,
  isSaving,
  onSave,
  onDiscard,
  lastSavedAt,
}) {
  const { t } = usePreferences();

  return (
    <footer className="save-footer">
      <div className="save-status-info">
        {isSaving ? (
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--color-primary)' }}>
            <Loader2 size={16} className="animate-spin" />
            {t('savingToServer')}
          </span>
        ) : isDirty ? (
          <span className="save-status-unsaved">
            <AlertCircle size={16} />
            {t('unsavedChanges')}
          </span>
        ) : (
          <span className="save-status-saved">
            <Check size={16} />
            {lastSavedAt ? t('savedStatus', { time: `(${lastSavedAt})` }) : t('savedStatus', { time: '' })}
          </span>
        )}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        {isDirty && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={onDiscard}
            disabled={isSaving}
            title={t('discardChanges')}
          >
            <RotateCcw size={14} />
            {t('discardChanges')}
          </button>
        )}

        <button
          type="button"
          className="btn btn-primary"
          onClick={onSave}
          disabled={!isDirty || isSaving}
        >
          {isSaving ? (
            <>
              <Loader2 size={16} className="animate-spin" />
              {t('saving')}
            </>
          ) : (
            <>
              <Save size={16} />
              {t('saveChanges')}
            </>
          )}
        </button>
      </div>
    </footer>
  );
}
