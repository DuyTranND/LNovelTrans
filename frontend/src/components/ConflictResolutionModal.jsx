import React, { useState, useEffect, useRef } from 'react';
import { AlertTriangle, Server, Edit3, Check, X, RefreshCw } from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';
import {
  resolveConflictAcceptServer,
  resolveConflictRebaseDraft,
} from '../utils/conflictHelpers';

export default function ConflictResolutionModal({
  isOpen,
  conflicts = [],
  draftEdits = {},
  latestChapter,
  onResolve,
  onCancel,
}) {
  const { t } = usePreferences();

  // Explicit decisions map: { [segmentId]: 'server' | 'draft' }
  const [decisions, setDecisions] = useState({});

  const modalRef = useRef(null);
  const previousActiveElementRef = useRef(null);

  // Reset decisions whenever the modal opens or a new conflict set arrives
  useEffect(() => {
    if (isOpen) {
      setDecisions({});
      previousActiveElementRef.current = document.activeElement;
    }
  }, [isOpen, conflicts]);

  // Handle focus trap and Escape key, restore focus on close
  useEffect(() => {
    if (!isOpen) return;

    // Focus the modal container or first button
    const timer = setTimeout(() => {
      const focusables = modalRef.current?.querySelectorAll(
        'button:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
      if (focusables && focusables.length > 0) {
        focusables[0].focus();
      } else if (modalRef.current) {
        modalRef.current.focus();
      }
    }, 50);

    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
        return;
      }

      if (e.key === 'Tab' && modalRef.current) {
        const focusables = Array.from(
          modalRef.current.querySelectorAll(
            'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
          )
        );
        if (focusables.length === 0) return;

        const firstElement = focusables[0];
        const lastElement = focusables[focusables.length - 1];

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

    document.addEventListener('keydown', handleKeyDown);

    return () => {
      clearTimeout(timer);
      document.removeEventListener('keydown', handleKeyDown);
      if (previousActiveElementRef.current && typeof previousActiveElementRef.current.focus === 'function') {
        previousActiveElementRef.current.focus();
      }
    };
  }, [isOpen, onCancel]);

  if (!isOpen || !conflicts || conflicts.length === 0) return null;

  const handleSelectDecision = (segmentId, choice) => {
    setDecisions((prev) => ({
      ...prev,
      [segmentId]: choice,
    }));
  };

  const handleAcceptAllServer = () => {
    const nextDecisions = {};
    for (const c of conflicts) {
      nextDecisions[c.segmentId] = 'server';
    }
    setDecisions(nextDecisions);
  };

  const handleRebaseAllDrafts = () => {
    const nextDecisions = {};
    for (const c of conflicts) {
      nextDecisions[c.segmentId] = 'draft';
    }
    setDecisions(nextDecisions);
  };

  // Require an EXPLICIT choice for every conflict before enabling resolve
  const decidedCount = conflicts.filter((c) => decisions[c.segmentId] === 'server' || decisions[c.segmentId] === 'draft').length;
  const allResolved = conflicts.length > 0 && decidedCount === conflicts.length;

  const handleApplyResolution = () => {
    if (!allResolved) return;

    let updatedDrafts = { ...draftEdits };

    for (const c of conflicts) {
      const choice = decisions[c.segmentId];
      if (choice === 'server') {
        updatedDrafts = resolveConflictAcceptServer(updatedDrafts, c.segmentId);
      } else if (choice === 'draft') {
        const serverSeg = (latestChapter?.segments || []).find((s) => s.id === c.segmentId);
        updatedDrafts = resolveConflictRebaseDraft(updatedDrafts, c.segmentId, serverSeg);
      }
    }

    onResolve({
      updatedDraftEdits: updatedDrafts,
      latestChapter,
    });
  };

  return (
    <div
      className="modal-overlay"
      style={{ zIndex: 1200 }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="conflict-modal-title"
    >
      <div
        ref={modalRef}
        tabIndex="-1"
        className="modal-dialog"
        style={{
          maxWidth: 840,
          width: '95%',
          maxHeight: '90vh',
          display: 'flex',
          flexDirection: 'column',
          padding: 0,
          overflow: 'hidden',
          outline: 'none',
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: '16px 20px',
            backgroundColor: 'var(--color-rail-bg)',
            borderBottom: '1px solid var(--color-border)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div
              style={{
                width: 32,
                height: 32,
                borderRadius: '50%',
                backgroundColor: 'rgba(239, 68, 68, 0.1)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'var(--color-danger)',
              }}
            >
              <AlertTriangle size={18} />
            </div>
            <div>
              <h3
                id="conflict-modal-title"
                style={{ margin: 0, fontSize: 16, fontWeight: 700, color: 'var(--color-navy)' }}
              >
                {t('conflictTitle')}
              </h3>
              <p style={{ margin: '2px 0 0', fontSize: 12, color: 'var(--color-muted)' }}>
                {t('conflictDesc')}
              </p>
            </div>
          </div>
          <button
            type="button"
            className="btn btn-icon-sm"
            onClick={onCancel}
            aria-label={t('cancel')}
            style={{ border: 'none', background: 'transparent' }}
          >
            <X size={18} />
          </button>
        </div>

        {/* Bulk Action Controls */}
        <div
          style={{
            padding: '10px 20px',
            backgroundColor: 'var(--color-surface-hover)',
            borderBottom: '1px solid var(--color-border)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 8,
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600 }}>
            {t('conflictResolvedCount', {
              count: decidedCount,
              total: conflicts.length,
            })}
          </span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              type="button"
              className="btn btn-sm"
              onClick={handleAcceptAllServer}
              title={t('conflictAcceptAllServer')}
            >
              <Server size={13} />
              <span>{t('conflictAcceptAllServer')}</span>
            </button>
            <button
              type="button"
              className="btn btn-sm"
              onClick={handleRebaseAllDrafts}
              title={t('conflictRebaseAllDrafts')}
            >
              <RefreshCw size={13} />
              <span>{t('conflictRebaseAllDrafts')}</span>
            </button>
          </div>
        </div>

        {/* Scrollable Conflict List */}
        <div
          style={{
            padding: '16px 20px',
            overflowY: 'auto',
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            gap: 16,
          }}
        >
          {conflicts.map((c) => {
            const decision = decisions[c.segmentId];
            return (
              <div
                key={c.segmentId}
                style={{
                  border: decision ? '1px solid var(--color-border)' : '1px solid #f59e0b',
                  borderRadius: 'var(--radius)',
                  overflow: 'hidden',
                  backgroundColor: 'var(--color-bg)',
                }}
              >
                <div
                  style={{
                    padding: '8px 12px',
                    backgroundColor: 'var(--color-rail-bg)',
                    borderBottom: '1px solid var(--color-border)',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    fontSize: 12,
                  }}
                >
                  <span style={{ fontWeight: 600 }}>
                    #{c.order + 1} (ID: {c.segmentId})
                  </span>
                  <span style={{ color: 'var(--color-muted)' }}>
                    {t('colStatus')}: Rev {c.serverRevision} (máy chủ) vs Rev {c.clientExpectedRevision ?? 0} (bản nháp)
                  </span>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, padding: 12 }}>
                  {/* Server Column */}
                  <div
                    style={{
                      padding: 12,
                      borderRadius: 'var(--radius)',
                      border: decision === 'server' ? '2px solid var(--color-primary)' : '1px solid var(--color-border)',
                      backgroundColor: decision === 'server' ? 'rgba(37, 99, 235, 0.05)' : 'transparent',
                      display: 'flex',
                      flexDirection: 'column',
                      justifyContent: 'space-between',
                      gap: 8,
                    }}
                  >
                    <div>
                      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-navy)', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <Server size={14} />
                        <span>{t('conflictServerVersion')} (Rev {c.serverRevision})</span>
                      </div>
                      <div style={{ fontSize: 13, lineHeight: 1.5, color: 'var(--color-text)' }}>
                        {c.serverTranslation || <em style={{ color: 'var(--color-muted)' }}>(Trống)</em>}
                      </div>
                    </div>
                    <button
                      type="button"
                      className={`btn btn-sm ${decision === 'server' ? 'btn-primary' : ''}`}
                      onClick={() => handleSelectDecision(c.segmentId, 'server')}
                      style={{ alignSelf: 'flex-start', marginTop: 8 }}
                    >
                      {decision === 'server' ? <Check size={13} /> : null}
                      <span>{t('conflictAcceptServer')}</span>
                    </button>
                  </div>

                  {/* Draft Column */}
                  <div
                    style={{
                      padding: 12,
                      borderRadius: 'var(--radius)',
                      border: decision === 'draft' ? '2px solid #8b5cf6' : '1px solid var(--color-border)',
                      backgroundColor: decision === 'draft' ? 'rgba(139, 92, 246, 0.05)' : 'transparent',
                      display: 'flex',
                      flexDirection: 'column',
                      justifyContent: 'space-between',
                      gap: 8,
                    }}
                  >
                    <div>
                      <div style={{ fontSize: 12, fontWeight: 600, color: '#6d28d9', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <Edit3 size={14} />
                        <span>{t('conflictOwnDraft')}</span>
                      </div>
                      <div style={{ fontSize: 13, lineHeight: 1.5, color: 'var(--color-text)' }}>
                        {c.draftTranslation || <em style={{ color: 'var(--color-muted)' }}>(Trống)</em>}
                      </div>
                    </div>
                    <button
                      type="button"
                      className={`btn btn-sm ${decision === 'draft' ? 'btn-primary' : ''}`}
                      onClick={() => handleSelectDecision(c.segmentId, 'draft')}
                      style={{ alignSelf: 'flex-start', marginTop: 8 }}
                    >
                      {decision === 'draft' ? <Check size={13} /> : null}
                      <span>{t('conflictRebaseDraft')}</span>
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: '12px 20px',
            backgroundColor: 'var(--color-rail-bg)',
            borderTop: '1px solid var(--color-border)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 12,
          }}
        >
          <div style={{ fontSize: 12, color: 'var(--color-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
            <AlertTriangle size={14} color="#f59e0b" />
            <span>{t('conflictRequiresExplicitSave')}</span>
          </div>

          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button type="button" className="btn" onClick={onCancel}>
              {t('cancel')}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleApplyResolution}
              disabled={!allResolved}
              title={!allResolved ? 'Vui lòng chọn cách xử lý cho tất cả đoạn xung đột' : ''}
            >
              <Check size={15} />
              <span>{t('applyResolution')}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
