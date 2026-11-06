import React from 'react';
import { AlertCircle, X } from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';

export default function ConfirmModal({
  isOpen,
  title,
  message,
  confirmLabel,
  cancelLabel,
  confirmVariant = 'primary',
  onConfirm,
  onCancel,
}) {
  const { t } = usePreferences();
  
  if (!isOpen) return null;

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div className="modal-dialog">
        <div className="modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertCircle size={20} color={confirmVariant === 'danger' ? '#dc2626' : '#0866f5'} />
            <h3 id="modal-title" className="modal-title">{title}</h3>
          </div>
          <button
            type="button"
            className="btn btn-icon-sm"
            onClick={onCancel}
            aria-label={t('ariaCloseError')}
            style={{ border: 'none', background: 'transparent' }}
          >
            <X size={18} />
          </button>
        </div>

        <div className="modal-body">
          {message}
        </div>

        <div className="modal-footer">
          <button type="button" className="btn" onClick={onCancel}>
            {cancelLabel || t('cancel')}
          </button>
          <button
            type="button"
            className={`btn ${confirmVariant === 'danger' ? 'btn-danger' : 'btn-primary'}`}
            onClick={onConfirm}
          >
            {confirmLabel || t('saveChanges')}
          </button>
        </div>
      </div>
    </div>
  );
}
