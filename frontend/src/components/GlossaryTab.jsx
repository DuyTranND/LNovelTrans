import React, { useState } from 'react';
import {
  Plus,
  Trash2,
  Lock,
  Unlock,
  Save,
  Search,
  AlertCircle,
  Loader2,
  Check,
} from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';

export default function GlossaryTab({
  glossary = [],
  onCreateTerm,
  onUpdateTerm,
  onDeleteTerm,
  isLoading,
}) {
  const [searchQuery, setSearchQuery] = useState('');
  const [editingId, setEditingId] = useState(null);
  const [editForm, setEditForm] = useState({ source: '', target: '', notes: '', locked: false });
  const [newTerm, setNewTerm] = useState({ source: '', target: '', notes: '', locked: false });
  const [isAdding, setIsAdding] = useState(false);
  const [actionError, setActionError] = useState(null);
  const [unlockConfirmId, setUnlockConfirmId] = useState(null);
  
  const { t } = usePreferences();

  const filteredGlossary = glossary.filter((item) => {
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    return (
      (item.source && item.source.toLowerCase().includes(q)) ||
      (item.target && item.target.toLowerCase().includes(q)) ||
      (item.notes && item.notes.toLowerCase().includes(q))
    );
  });

  const handleStartEdit = (term) => {
    if (term.locked) {
      setUnlockConfirmId(term.id);
      return;
    }
    setEditingId(term.id);
    setEditForm({
      source: term.source || '',
      target: term.target || '',
      notes: term.notes || '',
      locked: Boolean(term.locked),
    });
    setActionError(null);
  };

  const handleConfirmUnlock = async (term) => {
    try {
      await onUpdateTerm(term.id, { locked: false });
      setUnlockConfirmId(null);
      setEditingId(term.id);
      setEditForm({
        source: term.source || '',
        target: term.target || '',
        notes: term.notes || '',
        locked: false,
      });
    } catch (err) {
      setActionError(err.message);
    }
  };

  const handleSaveEdit = async () => {
    if (!editForm.source.trim() || !editForm.target.trim()) {
      setActionError('Vui lòng nhập cả từ gốc và bản dịch');
      return;
    }
    try {
      await onUpdateTerm(editingId, editForm);
      setEditingId(null);
      setActionError(null);
    } catch (err) {
      setActionError(err.message);
    }
  };

  const handleAddNew = async (e) => {
    e.preventDefault();
    if (!newTerm.source.trim() || !newTerm.target.trim()) {
      setActionError('Vui lòng nhập cả từ gốc và bản dịch khi tạo mới');
      return;
    }
    try {
      await onCreateTerm(newTerm);
      setNewTerm({ source: '', target: '', notes: '', locked: false });
      setIsAdding(false);
      setActionError(null);
    } catch (err) {
      setActionError(err.message);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Controls & Search */}
      <div className="glossary-controls">
        <div className="search-box" style={{ maxWidth: 320 }}>
          <Search size={14} className="search-icon" />
          <input
            type="text"
            className="search-input"
            placeholder={t('searchGlossary')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>

        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            setIsAdding(!isAdding);
            setActionError(null);
          }}
        >
          <Plus size={16} />
          <span>{isAdding ? t('closeForm') : t('addTerm')}</span>
        </button>
      </div>

      {actionError && (
        <div className="alert-banner alert-banner-error">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertCircle size={16} />
            <span>{actionError}</span>
          </div>
        </div>
      )}

      {/* Unlock Confirmation Modal */}
      {unlockConfirmId && (
        <div className="modal-overlay">
          <div className="modal-dialog">
            <div className="modal-header">
              <h3 className="modal-title">{t('locked')}</h3>
            </div>
            <div className="modal-body">
              <p>{t('termLockedConfirm')}</p>
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="btn"
                onClick={() => setUnlockConfirmId(null)}
              >
                {t('cancel')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  const term = glossary.find((t) => t.id === unlockConfirmId);
                  if (term) handleConfirmUnlock(term);
                }}
              >
                <Unlock size={14} />
                {t('unlockAndEdit')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* New Term Form */}
      {isAdding && (
        <form
          onSubmit={handleAddNew}
          style={{
            padding: 16,
            backgroundColor: 'var(--color-rail-bg)',
            border: '1px solid var(--color-border)',
            borderRadius: 'var(--radius)',
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          <div style={{ fontWeight: 600, fontSize: 14 }}>{t('addTermTitle')}</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, display: 'block', marginBottom: 4 }}>
                {t('termSource')}
              </label>
              <input
                type="text"
                className="glossary-input"
                value={newTerm.source}
                onChange={(e) => setNewTerm({ ...newTerm, source: e.target.value })}
                required
              />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, display: 'block', marginBottom: 4 }}>
                {t('termTarget')}
              </label>
              <input
                type="text"
                className="glossary-input"
                value={newTerm.target}
                onChange={(e) => setNewTerm({ ...newTerm, target: e.target.value })}
                required
              />
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 12, alignItems: 'center' }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, display: 'block', marginBottom: 4 }}>
                {t('termNotes')}
              </label>
              <input
                type="text"
                className="glossary-input"
                value={newTerm.notes}
                onChange={(e) => setNewTerm({ ...newTerm, notes: e.target.value })}
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 18 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={newTerm.locked}
                  onChange={(e) => setNewTerm({ ...newTerm, locked: e.target.checked })}
                />
                <span>{t('termLock')}</span>
              </label>
            </div>
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button type="button" className="btn btn-sm" onClick={() => setIsAdding(false)}>
              {t('cancel')}
            </button>
            <button type="submit" className="btn btn-primary btn-sm">
              <Save size={14} />
              {t('saveTerm')}
            </button>
          </div>
        </form>
      )}

      {/* Glossary Table */}
      {isLoading ? (
        <div style={{ padding: 40, textAlign: 'center', color: 'var(--color-muted)' }}>
          <Loader2 size={20} className="animate-spin" style={{ margin: '0 auto 8px' }} />
          {t('loadingGlossary')}
        </div>
      ) : filteredGlossary.length === 0 ? (
        <div className="empty-state-card" style={{ padding: '36px 16px' }}>
          <p className="empty-state-desc">
            {searchQuery ? t('glossaryNotFound') : t('glossaryEmpty')}
          </p>
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="glossary-table">
            <thead>
              <tr>
                <th style={{ width: '25%' }}>{t('colSource')}</th>
                <th style={{ width: '25%' }}>{t('colTarget')}</th>
                <th style={{ width: '25%' }}>{t('colNotes')}</th>
                <th style={{ width: '12%', textAlign: 'center' }}>{t('colStatus')}</th>
                <th style={{ width: '13%', textAlign: 'right' }}>{t('colAction')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredGlossary.map((term) => {
                const isEditing = editingId === term.id;

                if (isEditing) {
                  return (
                    <tr key={term.id} style={{ backgroundColor: '#fdfefe' }}>
                      <td>
                        <input
                          type="text"
                          className="glossary-input"
                          value={editForm.source}
                          onChange={(e) => setEditForm({ ...editForm, source: e.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          type="text"
                          className="glossary-input"
                          value={editForm.target}
                          onChange={(e) => setEditForm({ ...editForm, target: e.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          type="text"
                          className="glossary-input"
                          value={editForm.notes}
                          onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })}
                        />
                      </td>
                      <td style={{ textAlign: 'center' }}>
                        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
                          <input
                            type="checkbox"
                            checked={editForm.locked}
                            onChange={(e) => setEditForm({ ...editForm, locked: e.target.checked })}
                          />
                          <span style={{ fontSize: 12 }}>{t('locked')}</span>
                        </label>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <div style={{ display: 'inline-flex', gap: 4 }}>
                          <button
                            type="button"
                            className="btn btn-sm btn-primary"
                            onClick={handleSaveEdit}
                            title={t('saveChanges')}
                          >
                            <Check size={14} />
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => setEditingId(null)}
                            title={t('cancel')}
                          >
                            {t('cancel')}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                }

                return (
                  <tr key={term.id}>
                    <td style={{ fontWeight: 600 }}>{term.source}</td>
                    <td style={{ color: 'var(--color-navy)' }}>{term.target}</td>
                    <td style={{ color: 'var(--color-muted)', fontSize: 13 }}>{term.notes || '—'}</td>
                    <td style={{ textAlign: 'center' }}>
                      {term.locked ? (
                        <button
                          type="button"
                          className="lock-toggle-btn locked"
                          onClick={() => handleStartEdit(term)}
                        >
                          <Lock size={12} />
                          <span>{t('locked')}</span>
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="lock-toggle-btn"
                          onClick={async () => {
                            try {
                              await onUpdateTerm(term.id, { locked: true });
                            } catch (err) {
                              setActionError(err.message);
                            }
                          }}
                        >
                          <Unlock size={12} />
                          <span>{t('unlocked')}</span>
                        </button>
                      )}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <div style={{ display: 'inline-flex', gap: 4 }}>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => handleStartEdit(term)}
                          title={term.locked ? t('termLockedConfirm') : t('edit')}
                        >
                          {t('edit')}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          onClick={() => {
                            if (window.confirm(t('deleteConfirm', { term: term.source }))) {
                              onDeleteTerm(term.id);
                            }
                          }}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
