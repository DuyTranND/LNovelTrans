import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Play,
  RotateCcw,
  Clock,
  CheckCircle2,
  AlertOctagon,
  AlertTriangle,
  Loader2,
  Filter,
  ChevronLeft,
  ChevronRight,
  Info,
  Lock,
  ChevronDown,
  ChevronUp,
  XCircle,
  FileCheck,
  Sparkles,
  Undo2,
  Check,
  X,
  ShieldCheck,
  CheckSquare,
  Square,
} from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';
import * as api from '../api';
import { getAuditEmptyState } from '../utils/consistencyAuditHelpers';

function getReviewPercent(progress) {
  const val = progress && typeof progress === 'object' ? progress.percentage : progress;
  const num = typeof val === 'number' ? val : parseFloat(val);
  return Number.isFinite(num) ? Math.min(100, Math.max(0, num)) : 0;
}

export default function ConsistencyReviewTab({
  bookId,
  book,
  currentModel = 'gemini-3.5-flash-lite',
  keyConfigured = false,
}) {
  const { t } = usePreferences();

  // Runs and selected run state
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [isStartingReview, setIsStartingReview] = useState(false);
  const [actionLoading, setActionLoading] = useState(null); // 'cancel' | 'retry' | null
  const [tabError, setTabError] = useState(null);

  // Findings, filters & pagination
  const [findings, setFindings] = useState([]);
  const [isLoadingFindings, setIsLoadingFindings] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, pageSize: 20, total: 0, totalPages: 1 });
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [severityFilter, setSeverityFilter] = useState('all');
  const [expandedEvidenceIds, setExpandedEvidenceIds] = useState({});

  // Auto-correction and snapshots state
  const [showCorrectionsModal, setShowCorrectionsModal] = useState(false);
  const [showSnapshotsModal, setShowSnapshotsModal] = useState(false);
  const [proposals, setProposals] = useState([]);
  const [proposalsSummary, setProposalsSummary] = useState(null);
  const [isLoadingProposals, setIsLoadingProposals] = useState(false);
  const [isApplyingProposals, setIsApplyingProposals] = useState(false);
  const [correctionsClassFilter, setCorrectionsClassFilter] = useState('all');
  const [correctionsStatusFilter, setCorrectionsStatusFilter] = useState('all');
  const [confirmApplyModalOpen, setConfirmApplyModalOpen] = useState(false);
  const [correctionNotice, setCorrectionNotice] = useState(null);

  // Snapshots state
  const [snapshots, setSnapshots] = useState([]);
  const [isLoadingSnapshots, setIsLoadingSnapshots] = useState(false);
  const [rollingBackId, setRollingBackId] = useState(null);
  const [snapshotNotice, setSnapshotNotice] = useState(null);

  // Polling and abort refs
  const pollTimerRef = useRef(null);
  const activeBookIdRef = useRef(bookId);
  const abortControllerRef = useRef(null);

  useEffect(() => {
    activeBookIdRef.current = bookId;
  }, [bookId]);

  // Clean up timers on unmount
  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      stopPolling();
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [stopPolling]);

  // Fetch findings for a specific run
  const fetchFindings = useCallback(
    async (rId, page = 1, pageSize = 20, cat = 'all', sev = 'all') => {
      if (!bookId || !rId) {
        setFindings([]);
        setPagination({ page: 1, pageSize: 20, total: 0, totalPages: 1 });
        return;
      }
      setIsLoadingFindings(true);
      try {
        const res = await api.getConsistencyReviewFindings(bookId, rId, {
          page,
          page_size: pageSize,
          category: cat,
          severity: sev,
        });
        if (activeBookIdRef.current === bookId) {
          setFindings(res.items || []);
          setPagination({
            page: res.page || 1,
            pageSize: res.page_size || 20,
            total: res.total || 0,
            totalPages: res.total_pages || 1,
          });
        }
      } catch (err) {
        if (err.name !== 'AbortError' && activeBookIdRef.current === bookId) {
          setTabError(err.message);
        }
      } finally {
        if (activeBookIdRef.current === bookId) {
          setIsLoadingFindings(false);
        }
      }
    },
    [bookId]
  );

  // Poll single run status safely
  const pollRunStatus = useCallback(
    async (rId) => {
      if (!bookId || !rId) return;
      try {
        const updated = await api.getConsistencyReviewStatus(bookId, rId);
        if (activeBookIdRef.current !== bookId) return;

        setSelectedRun(updated);
        setRuns((prevRuns) =>
          prevRuns.map((r) => (r.id === updated.id ? { ...r, ...updated } : r))
        );

        const isTopActive = ['running', 'queued', 'pausing'].includes(updated.status);
        const isSemanticActive = ['running', 'queued', 'pausing'].includes(
          updated.progress?.semantic_stage
        );

        // If run transitioned to terminal state, stop polling and refresh findings on all terminal statuses
        if (!isTopActive && !isSemanticActive) {
          stopPolling();
          fetchFindings(rId, 1, pagination.pageSize, categoryFilter, severityFilter);
        }
      } catch {
        // Safe ignore transient poll error
      }
    },
    [bookId, stopPolling, fetchFindings, pagination.pageSize, categoryFilter, severityFilter]
  );

  // Setup polling when selectedRun is active
  useEffect(() => {
    stopPolling();
    const isTopActive = ['running', 'queued', 'pausing'].includes(selectedRun?.status);
    const isSemanticActive = ['running', 'queued', 'pausing'].includes(
      selectedRun?.progress?.semantic_stage
    );
    if (selectedRun && (isTopActive || isSemanticActive)) {
      pollTimerRef.current = setInterval(() => {
        pollRunStatus(selectedRun.id);
      }, 2500);
    }
    return () => stopPolling();
  }, [
    selectedRun?.status,
    selectedRun?.progress?.semantic_stage,
    selectedRun?.id,
    pollRunStatus,
    stopPolling,
  ]);

  // Load review history for the book
  const loadHistory = useCallback(
    async (autoSelectId = null) => {
      if (!bookId) {
        setRuns([]);
        setSelectedRun(null);
        setSelectedRunId(null);
        return;
      }
      setIsLoadingHistory(true);
      setTabError(null);
      try {
        const history = await api.getBookConsistencyReviews(bookId);
        if (activeBookIdRef.current !== bookId) return;

        const list = Array.isArray(history) ? history : [];
        setRuns(list);

        if (list.length > 0) {
          const targetId = autoSelectId || (list.some((r) => r.id === selectedRunId) ? selectedRunId : list[0].id);
          const targetRun = list.find((r) => r.id === targetId) || list[0];
          setSelectedRunId(targetRun.id);
          setSelectedRun(targetRun);
          fetchFindings(targetRun.id, 1, pagination.pageSize, categoryFilter, severityFilter);
        } else {
          setSelectedRunId(null);
          setSelectedRun(null);
          setFindings([]);
          setPagination({ page: 1, pageSize: 20, total: 0, totalPages: 1 });
        }
      } catch (err) {
        if (err.name !== 'AbortError' && activeBookIdRef.current === bookId) {
          setTabError(err.message || t('errCannotConnectReview'));
        }
      } finally {
        if (activeBookIdRef.current === bookId) {
          setIsLoadingHistory(false);
        }
      }
    },
    [bookId, selectedRunId, fetchFindings, pagination.pageSize, categoryFilter, severityFilter, t]
  );

  // Reset and load whenever bookId changes
  useEffect(() => {
    stopPolling();
    setSelectedRunId(null);
    setSelectedRun(null);
    setFindings([]);
    setExpandedEvidenceIds({});
    loadHistory();
  }, [bookId, stopPolling]);

  // Handle run selection change
  const handleSelectRun = (rId) => {
    if (rId === selectedRunId) return;
    const run = runs.find((r) => r.id === rId);
    if (!run) return;
    setSelectedRunId(rId);
    setSelectedRun(run);
    setExpandedEvidenceIds({});
    fetchFindings(rId, 1, pagination.pageSize, categoryFilter, severityFilter);
  };

  // Launch consistency review
  const handleStartReview = async () => {
    if (!bookId) return;
    if (!keyConfigured) {
      setTabError(t('consistencyNoKeyNotice'));
      return;
    }
    if (book && book.translated_segments <= 0) {
      setTabError(t('consistencyNoTranslatedSegments'));
      return;
    }

    setIsStartingReview(true);
    setTabError(null);
    try {
      const newRun = await api.createConsistencyReview(bookId, { model: currentModel });
      setRuns((prev) => [newRun, ...prev.filter((r) => r.id !== newRun.id)]);
      setSelectedRunId(newRun.id);
      setSelectedRun(newRun);
      setFindings([]);
      setPagination({ page: 1, pageSize: pagination.pageSize, total: 0, totalPages: 1 });
    } catch (err) {
      setTabError(t('errCreateReview', { err: err.message }));
    } finally {
      setIsStartingReview(false);
    }
  };

  // Cancel in-progress review
  const handleCancelReview = async () => {
    if (!bookId || !selectedRun) return;
    setActionLoading('cancel');
    setTabError(null);
    try {
      const updated = await api.cancelConsistencyReview(bookId, selectedRun.id);
      stopPolling();
      setSelectedRun(updated);
      setRuns((prev) =>
        prev.map((r) => (r.id === updated.id ? { ...r, ...updated } : r))
      );
      if (['completed', 'failed', 'cancelled', 'paused'].includes(updated.status)) {
        fetchFindings(updated.id, 1, pagination.pageSize, categoryFilter, severityFilter);
      }
    } catch (err) {
      setTabError(t('errCancelReview', { err: err.message }));
    } finally {
      setActionLoading(null);
    }
  };

  // Retry failed or paused review
  const handleRetryReview = async () => {
    if (!bookId || !selectedRun) return;
    setActionLoading('retry');
    setTabError(null);
    try {
      const updated = await api.retryConsistencyReview(bookId, selectedRun.id);
      setSelectedRun(updated);
      setRuns((prev) =>
        prev.map((r) => (r.id === updated.id ? { ...r, ...updated } : r))
      );
    } catch (err) {
      setTabError(t('errRetryReview', { err: err.message }));
    } finally {
      setActionLoading(null);
    }
  };

  const handleStartSemanticReview = async () => {
    if (!bookId || !selectedRun) return;
    setActionLoading('semantic');
    setTabError(null);
    try {
      const updated = await api.startSemanticReview(bookId, selectedRun.id);
      setSelectedRun(updated);
      setRuns((prev) =>
        prev.map((r) => (r.id === updated.id ? { ...r, ...updated } : r))
      );
    } catch (err) {
      setTabError(err.message || 'Không thể khởi động thẩm định ngữ nghĩa.');
    } finally {
      setActionLoading(null);
    }
  };

  const handleCancelSemanticReview = async () => {
    if (!bookId || !selectedRun) return;
    setActionLoading('cancelSemantic');
    try {
      const updated = await api.cancelSemanticReview(bookId, selectedRun.id);
      setSelectedRun(updated);
      setRuns((prev) =>
        prev.map((r) => (r.id === updated.id ? { ...r, ...updated } : r))
      );
    } catch (err) {
      setTabError(err.message || 'Không thể hủy thẩm định ngữ nghĩa.');
    } finally {
      setActionLoading(null);
    }
  };

  // Filter handlers
  const handleCategoryChange = (e) => {
    const cat = e.target.value;
    setCategoryFilter(cat);
    if (selectedRunId) {
      fetchFindings(selectedRunId, 1, pagination.pageSize, cat, severityFilter);
    }
  };

  const handleSeverityChange = (e) => {
    const sev = e.target.value;
    setSeverityFilter(sev);
    if (selectedRunId) {
      fetchFindings(selectedRunId, 1, pagination.pageSize, categoryFilter, sev);
    }
  };

  const handlePageSizeChange = (e) => {
    const size = parseInt(e.target.value, 10) || 20;
    setPagination((prev) => ({ ...prev, pageSize: size }));
    if (selectedRunId) {
      fetchFindings(selectedRunId, 1, size, categoryFilter, severityFilter);
    }
  };

  const handlePageChange = (newPage) => {
    if (newPage < 1 || newPage > pagination.totalPages || newPage === pagination.page) return;
    if (selectedRunId) {
      fetchFindings(selectedRunId, newPage, pagination.pageSize, categoryFilter, severityFilter);
    }
  };

  const toggleEvidence = (findingId) => {
    setExpandedEvidenceIds((prev) => ({
      ...prev,
      [findingId]: !prev[findingId],
    }));
  };

  const hasActiveRun = runs.some((r) => ['running', 'queued', 'pausing'].includes(r.status));

  // Auto-corrections and snapshots handlers
  const fetchProposals = useCallback(
    async (rId, classFilter = 'all', statusFilter = 'all') => {
      if (!bookId || !rId) {
        setProposals([]);
        setProposalsSummary(null);
        return;
      }
      setIsLoadingProposals(true);
      try {
        const res = await api.getCorrectionPreview(bookId, rId, {
          classification: classFilter,
          status: statusFilter,
          pageSize: 200,
        });
        if (activeBookIdRef.current === bookId) {
          setProposals(res.items || []);
          setProposalsSummary(res.summary || null);
        }
      } catch (err) {
        if (err.name !== 'AbortError' && activeBookIdRef.current === bookId) {
          setTabError(err.message);
        }
      } finally {
        if (activeBookIdRef.current === bookId) {
          setIsLoadingProposals(false);
        }
      }
    },
    [bookId]
  );

  const handleGeneratePreview = async () => {
    if (!bookId || !selectedRunId) return;
    setIsLoadingProposals(true);
    setCorrectionNotice(null);
    try {
      await api.generateCorrectionPreview(bookId, selectedRunId);
      await fetchProposals(selectedRunId, correctionsClassFilter, correctionsStatusFilter);
      setCorrectionNotice({ type: 'success', text: 'Đã tạo lại bản xem trước sửa đổi thành công.' });
    } catch (err) {
      setCorrectionNotice({ type: 'error', text: err.message || 'Lỗi khi tạo bản xem trước.' });
    } finally {
      setIsLoadingProposals(false);
    }
  };

  const handleToggleProposal = async (proposalId, currentSelected) => {
    try {
      const updated = await api.updateCorrectionProposalSelection(bookId, selectedRunId, proposalId, !currentSelected);
      setProposals((prev) =>
        prev.map((p) => (p.id === proposalId ? { ...p, is_selected: updated.is_selected } : p))
      );
    } catch (err) {
      setCorrectionNotice({ type: 'error', text: err.message });
    }
  };

  const handleBatchSelect = async (selectAll, onlyAutoSafe = false) => {
    try {
      await api.batchUpdateCorrectionSelection(bookId, selectedRunId, selectAll, onlyAutoSafe);
      await fetchProposals(selectedRunId, correctionsClassFilter, correctionsStatusFilter);
    } catch (err) {
      setCorrectionNotice({ type: 'error', text: err.message });
    }
  };

  const handleApplySelectedCorrections = async () => {
    if (!bookId || !selectedRunId) return;
    setIsApplyingProposals(true);
    setCorrectionNotice(null);
    setConfirmApplyModalOpen(false);
    try {
      const res = await api.applyCorrectionProposals(bookId, selectedRunId, {
        only_auto_safe: false,
      });
      setCorrectionNotice({
        type: 'success',
        text: res.message || `Đã áp dụng ${res.applied_count} sửa đổi thành công. Mã sao lưu: ${res.snapshot_id}`,
      });
      await fetchProposals(selectedRunId, correctionsClassFilter, correctionsStatusFilter);
      await loadHistory(selectedRunId);
    } catch (err) {
      setCorrectionNotice({ type: 'error', text: err.message || 'Lỗi khi áp dụng sửa đổi.' });
    } finally {
      setIsApplyingProposals(false);
    }
  };

  const fetchSnapshots = useCallback(async () => {
    if (!bookId) return;
    setIsLoadingSnapshots(true);
    try {
      const list = await api.getCorrectionSnapshots(bookId);
      if (activeBookIdRef.current === bookId) {
        setSnapshots(Array.isArray(list) ? list : []);
      }
    } catch (err) {
      if (activeBookIdRef.current === bookId) {
        setSnapshotNotice({ type: 'error', text: err.message });
      }
    } finally {
      if (activeBookIdRef.current === bookId) {
        setIsLoadingSnapshots(false);
      }
    }
  }, [bookId]);

  const handleRollbackSnapshot = async (snapshotId) => {
    if (!bookId || !snapshotId) return;
    setRollingBackId(snapshotId);
    setSnapshotNotice(null);
    try {
      const res = await api.rollbackCorrectionSnapshot(bookId, snapshotId);
      setSnapshotNotice({
        type: 'success',
        text: res.message || `Đã hoàn tác ${res.rolled_back_segments} phân đoạn về nguyên trạng.`,
      });
      await fetchSnapshots();
      if (selectedRunId) {
        await fetchProposals(selectedRunId, correctionsClassFilter, correctionsStatusFilter);
        await loadHistory(selectedRunId);
      }
    } catch (err) {
      setSnapshotNotice({ type: 'error', text: err.message || 'Lỗi khi hoàn tác.' });
    } finally {
      setRollingBackId(null);
    }
  };

  // Category label helper
  const getCategoryLabel = (cat) => {
    switch (cat?.toLowerCase()) {
      case 'title':
        return t('categoryTitle');
      case 'character':
        return t('categoryCharacter');
      case 'location':
        return t('categoryLocation');
      case 'skill':
        return t('categorySkill');
      case 'item':
        return t('categoryItem');
      case 'organization':
        return t('categoryOrganization');
      case 'concept':
        return t('categoryConcept');
      case 'term':
      default:
        return t('categoryTerm');
    }
  };

  // Severity badge helper
  const getSeverityBadge = (sev) => {
    switch (sev?.toLowerCase()) {
      case 'high':
        return <span className="badge-severity badge-severity-high">{t('severityHigh')}</span>;
      case 'medium':
        return <span className="badge-severity badge-severity-medium">{t('severityMedium')}</span>;
      case 'low':
      default:
        return <span className="badge-severity badge-severity-low">{t('severityLow')}</span>;
    }
  };

  // Status badge helper
  const getStatusBadge = (status) => {
    let label = status;
    let className = 'job-status-badge';
    let icon = null;
    let spin = false;

    if (status === 'running') {
      label = t('statusRunning');
      className += ' job-status-running';
      icon = Loader2;
      spin = true;
    } else if (status === 'pausing') {
      label = t('statusPausing');
      className += ' job-status-pausing';
      icon = Loader2;
      spin = true;
    } else if (status === 'paused') {
      label = t('statusPaused');
      className += ' job-status-paused';
      icon = Clock;
    } else if (status === 'queued') {
      label = t('statusQueued');
      className += ' job-status-queued';
      icon = Clock;
    } else if (status === 'completed') {
      label = t('statusCompleted');
      className += ' job-status-completed';
      icon = CheckCircle2;
    } else if (status === 'failed') {
      label = t('statusFailed');
      className += ' job-status-failed';
      icon = AlertOctagon;
    } else if (status === 'cancelled') {
      label = t('statusCancelled');
      className += ' job-status-failed';
      icon = XCircle;
    }

    const IconComp = icon;

    return (
      <span className={className} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        {IconComp && <IconComp size={12} className={spin ? 'animate-spin' : ''} />}
        <span>{label}</span>
      </span>
    );
  };

  // Format date helper
  const formatDate = (isoStr) => {
    if (!isoStr) return '';
    try {
      const d = new Date(isoStr);
      return d.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return isoStr;
    }
  };

  return (
    <div className="consistency-container">
      {/* Header and Action Card */}
      <div className="consistency-header-card">
        <div className="consistency-header-top">
          <div>
            <h2 className="consistency-title">{t('consistencyTitle')}</h2>
            <p className="consistency-subtitle">{t('consistencySubtitle')}</p>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleStartReview}
              disabled={hasActiveRun || isStartingReview || (book && book.translated_segments <= 0)}
              title={
                book && book.translated_segments <= 0
                  ? t('consistencyNoTranslatedSegments')
                  : t('startConsistencyReview')
              }
            >
              {isStartingReview ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  <span>{t('startingConsistencyReview')}</span>
                </>
              ) : (
                <>
                  <Play size={14} />
                  <span>{t('startConsistencyReview')}</span>
                </>
              )}
            </button>

            <button
              type="button"
              className="btn"
              onClick={() => loadHistory(selectedRunId)}
              disabled={isLoadingHistory}
              title={t('refreshReview')}
            >
              <RotateCcw size={14} className={isLoadingHistory ? 'animate-spin' : ''} />
              <span>{t('refreshReview')}</span>
            </button>

            <button
              type="button"
              className="btn btn-outline"
              onClick={() => {
                setShowCorrectionsModal(true);
                if (selectedRunId) {
                  fetchProposals(selectedRunId, correctionsClassFilter, correctionsStatusFilter);
                }
              }}
              disabled={!selectedRunId}
              title="Xem trước và duyệt áp dụng sửa đổi tự động"
            >
              <Sparkles size={14} style={{ color: 'var(--color-primary)' }} />
              <span>Sửa đổi tự động {proposalsSummary?.total_proposals ? `(${proposalsSummary.total_proposals})` : ''}</span>
            </button>

            <button
              type="button"
              className="btn btn-outline"
              onClick={() => {
                setShowSnapshotsModal(true);
                fetchSnapshots();
              }}
              title="Xem lịch sử các bản sao lưu khôi phục và hoàn tác"
            >
              <Clock size={14} />
              <span>Sao lưu & Hoàn tác</span>
            </button>
          </div>
        </div>

        {/* Quota & Model Notice Banner */}
        <div className="consistency-quota-notice">
          <Info size={16} style={{ flexShrink: 0, marginTop: 1, color: 'var(--color-primary)' }} />
          <div>
            <div>{t('consistencyDisclaimer')}</div>
            <div style={{ marginTop: 2, fontWeight: 600, color: 'var(--color-navy)' }}>
              {t('consistencyModelNotice', { model: currentModel })}
            </div>
          </div>
        </div>

        {/* Active Book Notice if no translated segments */}
        {book && book.translated_segments <= 0 && (
          <div className="alert-banner alert-banner-warning">
            <AlertTriangle size={15} />
            <span>{t('consistencyNoTranslatedSegments')}</span>
          </div>
        )}

        {/* Tab Error Banner */}
        {tabError && (
          <div className="alert-banner alert-banner-error">
            <AlertOctagon size={15} />
            <span>{tabError}</span>
          </div>
        )}
      </div>

      {/* Empty State when no reviews have ever been run */}
      {!isLoadingHistory && runs.length === 0 && (
        <div
          style={{
            textAlign: 'center',
            padding: '48px 24px',
            backgroundColor: 'var(--color-bg)',
            border: '1px solid var(--color-border)',
            borderRadius: 'var(--radius)',
          }}
        >
          <FileCheck size={40} style={{ color: 'var(--color-muted)', marginBottom: 12 }} />
          <h3 style={{ fontSize: 16, fontWeight: 600, color: 'var(--color-navy)', marginBottom: 6 }}>
            {t('noReviewsYet')}
          </h3>
          <p style={{ fontSize: 13, color: 'var(--color-muted)', maxWidth: 520, margin: '0 auto 16px' }}>
            {t('noReviewsDesc')}
          </p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleStartReview}
            disabled={isStartingReview || (book && book.translated_segments <= 0)}
          >
            <Play size={14} />
            <span>{t('startConsistencyReview')}</span>
          </button>
        </div>
      )}

      {/* Review Content when runs exist */}
      {runs.length > 0 && selectedRun && (
        <>
          {/* Historical Runs Selector & Run Controls */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              flexWrap: 'wrap',
              gap: 12,
              padding: '12px 16px',
              backgroundColor: 'var(--color-bg)',
              border: '1px solid var(--color-border)',
              borderRadius: 'var(--radius)',
            }}
          >
            <div className="consistency-run-selector">
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-navy)' }}>
                {t('selectReview')}:
              </span>
              <select
                className="consistency-select"
                value={selectedRunId || ''}
                onChange={(e) => handleSelectRun(e.target.value)}
              >
                {runs.map((r) => (
                  <option key={r.id} value={r.id}>
                    {formatDate(r.created_at)} — {r.status.toUpperCase()} ({getReviewPercent(r.progress)}%, {r.findings_count || 0} {t('findingsCount', { count: '' }).trim()})
                  </option>
                ))}
              </select>
              {getStatusBadge(selectedRun.status)}
            </div>

            {/* Run Actions (Cancel / Retry) */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {['running', 'queued', 'pausing'].includes(selectedRun.status) && (
                <button
                  type="button"
                  className="btn"
                  onClick={handleCancelReview}
                  disabled={actionLoading === 'cancel'}
                  style={{ color: 'var(--color-danger)', borderColor: '#fecaca' }}
                >
                  {actionLoading === 'cancel' ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <XCircle size={14} />
                  )}
                  <span>{actionLoading === 'cancel' ? t('cancellingReview') : t('cancelReview')}</span>
                </button>
              )}

              {['failed', 'paused', 'cancelled'].includes(selectedRun.status) && (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleRetryReview}
                  disabled={actionLoading === 'retry'}
                >
                  {actionLoading === 'retry' ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <RotateCcw size={14} />
                  )}
                  <span>{actionLoading === 'retry' ? t('retryingReview') : t('retryReview')}</span>
                </button>
              )}

              {/* Semantic Review Stage Controls (Task C) */}
              {['completed', 'partial'].includes(selectedRun.status) && (
                <>
                  {selectedRun.progress?.semantic_stage === 'running' ? (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={handleCancelSemanticReview}
                      disabled={actionLoading === 'cancelSemantic'}
                      style={{ color: 'var(--color-danger)', borderColor: '#fecaca' }}
                    >
                      {actionLoading === 'cancelSemantic' ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <XCircle size={14} />
                      )}
                      <span>
                        {t('cancelSemanticReview')} ({selectedRun.progress?.semantic_percentage || 0}%)
                      </span>
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={handleStartSemanticReview}
                      disabled={actionLoading === 'semantic'}
                      title="Thẩm định ngữ nghĩa toàn thư trên các phân đoạn với Gemini 3.5 Flash Lite"
                    >
                      {actionLoading === 'semantic' ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <Sparkles size={14} />
                      )}
                      <span>
                        {selectedRun.progress?.semantic_stage === 'completed'
                          ? t('semanticReviewCompleted')
                          : selectedRun.progress?.semantic_stage === 'partial'
                          ? t('semanticReviewPartial', {
                              progress: selectedRun.progress?.semantic_percentage || 0,
                            })
                          : t('startSemanticReview')}
                      </span>
                    </button>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Stale Warning Banner */}
          {selectedRun.is_stale && (
            <div className="alert-banner alert-banner-warning">
              <AlertTriangle size={16} style={{ flexShrink: 0 }} />
              <span>{t('staleWarning')}</span>
            </div>
          )}

          {/* Partial Coverage Warning */}
          {(selectedRun.failed_segments > 0 ||
            selectedRun.skipped_segments > 0 ||
            selectedRun.progress?.coverage_status === 'partial' ||
            selectedRun.coverage_status === 'partial') && (
            <div className="alert-banner alert-banner-warning">
              <Info size={16} style={{ flexShrink: 0 }} />
              <span>
                {t('partialCoverageWarning')} (
                {selectedRun.failed_segments > 0 &&
                  t('failedSegmentsCount', { count: selectedRun.failed_segments }) + ' '}
                {selectedRun.skipped_segments > 0 &&
                  t('skippedSegmentsCount', { count: selectedRun.skipped_segments })}
                )
              </span>
            </div>
          )}

          {/* Zero Success Alert Error */}
          {(selectedRun.processed_segments || 0) === 0 && (selectedRun.failed_segments || 0) > 0 && (
            <div className="alert-banner alert-banner-error">
              <AlertOctagon size={16} style={{ flexShrink: 0 }} />
              <span>
                {t('zeroSuccessfulReviews', { failed: selectedRun.failed_segments || 0 })}
              </span>
            </div>
          )}

          {/* Error Details Banner */}
          {selectedRun.error && !(selectedRun.status === "completed" && selectedRun.progress?.stage === "completed" && selectedRun.progress?.coverage_status === "full") && (
            <div className="alert-banner alert-banner-error">
              <AlertOctagon size={16} style={{ flexShrink: 0 }} />
              <div>
                <strong>Lỗi rà soát:</strong> {selectedRun.error}
              </div>
            </div>
          )}

          {selectedRun.progress?.semantic_error && (
            <div className="alert-banner alert-banner-error">
              <AlertOctagon size={16} />
              <div><strong>Lỗi rà soát ngữ nghĩa:</strong> {selectedRun.progress.semantic_error}</div>
            </div>
          )}

          {selectedRun.progress?.semantic_stage === "paused" && selectedRun.progress?.semantic_pause_reason && (
            <div className="alert-banner alert-banner-warning">
              <AlertTriangle size={16} />
              <div><strong>Rà soát ngữ nghĩa tạm dừng:</strong> {selectedRun.progress.semantic_pause_reason}</div>
            </div>
          )}

          {/* Progress & Run Details Card */}
          <div className="consistency-progress-card">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-navy)' }}>
                {t('currentRun')} #{selectedRun.id.slice(0, 8)}
              </span>
              <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                {selectedRun.created_at && formatDate(selectedRun.created_at)}
              </span>
            </div>

            {/* Progress Bar */}
            {(() => {
              const successCount = selectedRun.processed_segments || 0;
              const totalSegments = selectedRun.total_segments || 0;
              const failedCount = selectedRun.failed_segments || 0;
              const skippedCount = selectedRun.skipped_segments || 0;
              const attemptedCount = successCount + failedCount + skippedCount;
              const successPercent = Math.round((1000 * successCount) / Math.max(1, totalSegments)) / 10;
              const attemptedPercent = Math.round((1000 * attemptedCount) / Math.max(1, totalSegments)) / 10;
              const isZeroSuccessWithFailed = successCount === 0 && failedCount > 0;

              return (
                <div className="job-progress-container">
                  <div className="job-progress-bar-bg">
                    <div
                      className="job-progress-bar-fill"
                      style={{ width: `${Math.min(100, Math.max(0, successPercent))}%` }}
                    />
                  </div>
                  <div className="job-progress-stats" style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 6 }}>
                    <span style={isZeroSuccessWithFailed ? { color: 'var(--color-danger, #ef4444)', fontWeight: 600 } : {}}>
                      {t('reviewedSegmentsProgress', {
                        processed: successCount,
                        total: totalSegments,
                        percent: successPercent,
                      })}
                    </span>
                    <span>
                      {t('attemptedSegmentsProgress', {
                        attempted: attemptedCount,
                        total: totalSegments,
                        percent: attemptedPercent,
                      })}
                    </span>
                    <span>{t('progressChapters', { count: selectedRun.total_chapters || 0 })}</span>
                  </div>
                </div>
              );
            })()}

            {/* Stats Summary Grid */}
            <div className="consistency-stats-grid">
              <div className="consistency-stat-item">
                <div className="consistency-stat-label">{t('findingsTitle')}</div>
                <div className="consistency-stat-value">{selectedRun.findings_count || pagination.total || 0}</div>
              </div>
              <div className="consistency-stat-item">
                <div className="consistency-stat-label">Model</div>
                <div className="consistency-stat-value" style={{ fontSize: 13 }}>
                  {selectedRun.model || currentModel}
                </div>
              </div>
              <div className="consistency-stat-item">
                <div className="consistency-stat-label">{t('progressChapters', { count: '' }).trim()}</div>
                <div className="consistency-stat-value">{selectedRun.total_chapters || 0}</div>
              </div>
              <div className="consistency-stat-item">
                <div className="consistency-stat-label">{t('tabTranslation')}</div>
                <div className="consistency-stat-value">
                  {selectedRun.processed_segments || 0} / {selectedRun.total_segments || 0}
                </div>
              </div>
            </div>
          </div>

          {/* Findings Report Section */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                flexWrap: 'wrap',
                gap: 8,
              }}
            >
              <div>
                <h3 style={{ fontSize: 16, fontWeight: 700, color: 'var(--color-navy)' }}>
                  {t('findingsTitle')}
                </h3>
                <p style={{ fontSize: 13, color: 'var(--color-muted)' }}>{t('findingsDesc')}</p>
              </div>

              <div style={{ fontSize: 12, color: 'var(--color-muted)', fontStyle: 'italic' }}>
                {t('readOnlyNotice')}
              </div>
            </div>

            {/* Filters Bar */}
            <div className="consistency-filters-card">
              <div className="consistency-filters-group">
                <span className="consistency-filter-label">
                  <Filter size={13} style={{ display: 'inline', marginRight: 4 }} />
                  {t('filterByCategory')}:
                </span>
                <select
                  className="consistency-select"
                  style={{ minWidth: 140 }}
                  value={categoryFilter}
                  onChange={handleCategoryChange}
                >
                  <option value="all">{t('allCategories')}</option>
                  <option value="title">{t('categoryTitle')}</option>
                  <option value="character">{t('categoryCharacter')}</option>
                  <option value="location">{t('categoryLocation')}</option>
                  <option value="skill">{t('categorySkill')}</option>
                  <option value="item">{t('categoryItem')}</option>
                  <option value="organization">{t('categoryOrganization')}</option>
                  <option value="concept">{t('categoryConcept')}</option>
                  <option value="term">{t('categoryTerm')}</option>
                </select>

                <span className="consistency-filter-label" style={{ marginLeft: 8 }}>
                  {t('filterBySeverity')}:
                </span>
                <select
                  className="consistency-select"
                  style={{ minWidth: 130 }}
                  value={severityFilter}
                  onChange={handleSeverityChange}
                >
                  <option value="all">{t('allSeverities')}</option>
                  <option value="high">{t('severityHigh')}</option>
                  <option value="medium">{t('severityMedium')}</option>
                  <option value="low">{t('severityLow')}</option>
                </select>
              </div>

              <div className="consistency-filters-group">
                <span className="consistency-filter-label">{t('pageSize')}:</span>
                <select
                  className="consistency-select"
                  style={{ minWidth: 80 }}
                  value={pagination.pageSize}
                  onChange={handlePageSizeChange}
                >
                  <option value={10}>10</option>
                  <option value={20}>20</option>
                  <option value={50}>50</option>
                </select>
              </div>
            </div>

            {/* Findings List or Empty */}
            {isLoadingFindings ? (
              <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--color-muted)' }}>
                <Loader2 size={24} className="animate-spin" style={{ margin: '0 auto 8px' }} />
                <div>Đang tải danh sách phát hiện...</div>
              </div>
            ) : findings.length === 0 ? (
              (() => {
                const emptyState = getAuditEmptyState(selectedRun, {
                  findingsCount: selectedRun?.findings_count ?? pagination.total,
                  displayedCount: findings.length,
                });

                return (
                  <div
                    style={{
                      textAlign: 'center',
                      padding: '36px 16px',
                      backgroundColor: 'var(--color-bg)',
                      border: '1px solid var(--color-border)',
                      borderRadius: 'var(--radius)',
                      color: 'var(--color-muted)',
                    }}
                  >
                    {emptyState.type === 'all_clear' ? (
                      <CheckCircle2 size={32} style={{ color: 'var(--color-success)', marginBottom: 8 }} />
                    ) : emptyState.type === 'filtered_empty' ? (
                      <Filter size={32} style={{ color: 'var(--color-muted)', marginBottom: 8 }} />
                    ) : (
                      <AlertTriangle size={32} style={{ color: 'var(--color-warning, #f59e0b)', marginBottom: 8 }} />
                    )}

                    <div style={{ fontWeight: 600, color: 'var(--color-navy)', marginBottom: emptyState.descKey ? 4 : 0 }}>
                      {t(emptyState.titleKey)}
                    </div>

                    {emptyState.descKey && (
                      <div style={{ fontSize: 13, color: 'var(--color-muted)', maxWidth: 500, margin: '4px auto 0' }}>
                        {t(emptyState.descKey)}
                      </div>
                    )}
                  </div>
                );
              })()
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {findings.map((item) => {
                  const isExpanded = Boolean(expandedEvidenceIds[item.id]);
                  const variants = Array.isArray(item.variants) ? item.variants : [];
                  const occurrences = Array.isArray(item.occurrences) ? item.occurrences : [];

                  return (
                    <div key={item.id} className="finding-card">
                      {/* Card Header: Source Term, Category, Severity & Badges */}
                      <div className="finding-card-header">
                        <div className="finding-term-info">
                          <span className="finding-source-term">{item.source_term}</span>
                          <span className="badge-category">{getCategoryLabel(item.category)}</span>
                          {getSeverityBadge(item.severity)}
                          <span
                            style={{
                              fontSize: 12,
                              color: 'var(--color-muted)',
                              fontWeight: 500,
                            }}
                          >
                            ({item.occurrence_count || occurrences.length} lần xuất hiện)
                          </span>
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          {item.glossary_divergence && (
                            <span className="badge-glossary-alert" title={item.reason}>
                              <AlertTriangle size={11} />
                              <span>{t('glossaryConflict')}</span>
                            </span>
                          )}

                          {item.glossary_locked && (
                            <span className="badge-glossary-locked" title={t('glossaryLocked')}>
                              <Lock size={11} />
                              <span>{t('glossaryLocked')}</span>
                            </span>
                          )}
                        </div>
                      </div>

                      {/* Canonical Recommendation Box */}
                      <div className="finding-rec-box">
                        <div className="finding-rec-header">
                          <span style={{ fontSize: 13, color: 'var(--color-navy)', fontWeight: 600 }}>
                            {t('canonicalRecommendation')}:
                          </span>
                          <span className="finding-rec-term">{item.canonical_recommendation}</span>
                          {item.confidence && (
                            <span
                              style={{
                                fontSize: 11,
                                padding: '1px 6px',
                                borderRadius: 10,
                                backgroundColor: '#dcfce7',
                                color: 'var(--color-success)',
                                fontWeight: 600,
                              }}
                            >
                              {t('confidence')}:{' '}
                              {item.confidence === 'high'
                                ? t('confidenceHigh')
                                : item.confidence === 'medium'
                                ? t('confidenceMedium')
                                : t('confidenceLow')}
                            </span>
                          )}
                        </div>

                        {item.reason && <div className="finding-rec-reason">{item.reason}</div>}

                        {item.glossary_target && (
                          <div style={{ fontSize: 12, color: 'var(--color-navy)' }}>
                            {t('glossaryTarget', { target: item.glossary_target })}
                          </div>
                        )}
                      </div>

                      {/* Divergent Variants List with Usage Counts & Locations */}
                      <div className="finding-variants-section">
                        <div className="finding-variants-title">{t('variantsAndUsage')}:</div>
                        <div className="finding-variants-grid">
                          {variants.map((v, vIdx) => {
                            const isCanonical = v.target === item.canonical_recommendation;
                            const sampleTitles = Array.isArray(v.sample_locations)
                              ? v.sample_locations
                                  .map((loc) => loc.chapter_title)
                                  .filter(Boolean)
                                  .slice(0, 3)
                              : [];

                            return (
                              <div
                                key={vIdx}
                                className={`variant-chip ${isCanonical ? 'is-canonical' : ''}`}
                              >
                                <span>{v.target}</span>
                                <span className="variant-count">{v.count}</span>
                                {sampleTitles.length > 0 && (
                                  <span
                                    style={{
                                      fontSize: 11,
                                      color: 'var(--color-muted)',
                                      marginLeft: 2,
                                    }}
                                  >
                                    ({sampleTitles.join(', ')})
                                  </span>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      </div>

                      {/* Collapsible Evidence Context Snippets */}
                      {occurrences.length > 0 && (
                        <div>
                          <button
                            type="button"
                            className="finding-evidence-toggle"
                            onClick={() => toggleEvidence(item.id)}
                          >
                            {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                            <span>
                              {isExpanded
                                ? t('hideOccurrences')
                                : t('occurrencesEvidence', { count: occurrences.length })}
                            </span>
                          </button>

                          {isExpanded && (
                            <div className="finding-evidence-list">
                              {occurrences.map((occ, oIdx) => (
                                <div key={oIdx} className="finding-evidence-item">
                                  <div
                                    style={{
                                      fontWeight: 600,
                                      color: 'var(--color-navy)',
                                      marginBottom: 2,
                                    }}
                                  >
                                    {occ.chapter_title || 'Chương'}{' '}
                                    <span style={{ color: 'var(--color-primary)', fontWeight: 500 }}>
                                      → &quot;{occ.target_variant}&quot;
                                    </span>
                                  </div>
                                  {occ.source_snippet && (
                                    <div
                                      style={{
                                        color: 'var(--color-muted)',
                                        fontFamily: 'var(--font-reading)',
                                      }}
                                    >
                                      &quot;{occ.source_snippet}&quot;
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* Pagination Controls */}
            {pagination.totalPages > 1 && (
              <div className="consistency-pagination">
                <div style={{ fontSize: 13, color: 'var(--color-muted)' }}>
                  {t('pageInfo', {
                    page: pagination.page,
                    totalPages: pagination.totalPages,
                    total: pagination.total,
                  })}
                </div>

                <div className="consistency-pagination-controls">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => handlePageChange(pagination.page - 1)}
                    disabled={pagination.page <= 1 || isLoadingFindings}
                  >
                    <ChevronLeft size={14} />
                    <span>{t('prevPage')}</span>
                  </button>

                  <span style={{ fontSize: 13, fontWeight: 600, padding: '0 8px' }}>
                    {pagination.page} / {pagination.totalPages}
                  </span>

                  <button
                    type="button"
                    className="btn"
                    onClick={() => handlePageChange(pagination.page + 1)}
                    disabled={pagination.page >= pagination.totalPages || isLoadingFindings}
                  >
                    <span>{t('nextPage')}</span>
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>
            )}
          </div>
        </>
      )}

      {/* Auto-Corrections Dry-Run Preview Modal */}
      {showCorrectionsModal && (
        <div className="modal-overlay" style={{ zIndex: 120 }}>
          <div
            className="modal-dialog"
            style={{
              maxWidth: 960,
              maxHeight: '90vh',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <div className="modal-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Sparkles size={18} style={{ color: 'var(--color-primary)' }} />
                <h3 className="modal-title" style={{ margin: 0 }}>Xem trước & Áp dụng sửa đổi tính nhất quán</h3>
              </div>
              <button
                type="button"
                className="btn-icon"
                onClick={() => setShowCorrectionsModal(false)}
                title="Đóng"
              >
                <X size={18} />
              </button>
            </div>

            <div className="modal-body" style={{ overflowY: 'auto', flex: 1, padding: '16px 20px' }}>
              {/* Notice */}
              {correctionNotice && (
                <div
                  className={`alert-banner alert-banner-${correctionNotice.type === 'error' ? 'error' : 'success'}`}
                  style={{ marginBottom: 12 }}
                >
                  {correctionNotice.type === 'error' ? <AlertOctagon size={15} /> : <CheckCircle2 size={15} />}
                  <span>{correctionNotice.text}</span>
                </div>
              )}

              {/* Controls bar: Filters & Batch actions */}
              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: 10,
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '10px 14px',
                  backgroundColor: 'var(--color-bg)',
                  border: '1px solid var(--color-border)',
                  borderRadius: 'var(--radius)',
                  marginBottom: 14,
                }}
              >
                <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ fontSize: 12, fontWeight: 600 }}>Phân loại:</span>
                    <select
                      className="form-control"
                      style={{ fontSize: 12, padding: '4px 8px', height: 'auto' }}
                      value={correctionsClassFilter}
                      onChange={(e) => {
                        const val = e.target.value;
                        setCorrectionsClassFilter(val);
                        fetchProposals(selectedRunId, val, correctionsStatusFilter);
                      }}
                    >
                      <option value="all">Tất cả phân loại</option>
                      <option value="auto_safe">An toàn tự động (Auto-safe)</option>
                      <option value="manual_only">Cần duyệt thủ công (Manual only)</option>
                    </select>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ fontSize: 12, fontWeight: 600 }}>Trạng thái:</span>
                    <select
                      className="form-control"
                      style={{ fontSize: 12, padding: '4px 8px', height: 'auto' }}
                      value={correctionsStatusFilter}
                      onChange={(e) => {
                        const val = e.target.value;
                        setCorrectionsStatusFilter(val);
                        fetchProposals(selectedRunId, correctionsClassFilter, val);
                      }}
                    >
                      <option value="all">Tất cả trạng thái</option>
                      <option value="pending">Chờ áp dụng (Pending)</option>
                      <option value="applied">Đã áp dụng (Applied)</option>
                      <option value="conflict">Xung đột (Conflict)</option>
                    </select>
                  </div>
                </div>

                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => handleBatchSelect(true, true)}
                    title="Chọn tất cả các mục an toàn tự động"
                  >
                    <ShieldCheck size={13} style={{ color: 'var(--color-success)' }} />
                    <span>Chọn an toàn</span>
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => handleBatchSelect(false)}
                    title="Bỏ chọn tất cả"
                  >
                    <span>Bỏ chọn</span>
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={handleGeneratePreview}
                    disabled={isLoadingProposals}
                    title="Tính toán lại các đề xuất sửa đổi"
                  >
                    <RotateCcw size={13} className={isLoadingProposals ? 'animate-spin' : ''} />
                    <span>Tạo lại</span>
                  </button>
                </div>
              </div>

              {/* Summary Metrics */}
              {proposalsSummary && (
                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))',
                    gap: 8,
                    marginBottom: 16,
                  }}
                >
                  <div className="stat-card" style={{ padding: '8px 12px' }}>
                    <div style={{ fontSize: 11, color: 'var(--color-muted)' }}>Tổng đề xuất</div>
                    <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--color-navy)' }}>
                      {proposalsSummary.total_proposals || 0}
                    </div>
                  </div>
                  <div className="stat-card" style={{ padding: '8px 12px', borderLeft: '3px solid var(--color-success)' }}>
                    <div style={{ fontSize: 11, color: 'var(--color-muted)' }}>An toàn tự động</div>
                    <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--color-success)' }}>
                      {proposalsSummary.auto_safe_count || 0}
                    </div>
                  </div>
                  <div className="stat-card" style={{ padding: '8px 12px', borderLeft: '3px solid #f59e0b' }}>
                    <div style={{ fontSize: 11, color: 'var(--color-muted)' }}>Cần duyệt thủ công</div>
                    <div style={{ fontSize: 16, fontWeight: 700, color: '#f59e0b' }}>
                      {proposalsSummary.manual_only_count || 0}
                    </div>
                  </div>
                  <div className="stat-card" style={{ padding: '8px 12px' }}>
                    <div style={{ fontSize: 11, color: 'var(--color-muted)' }}>Đã áp dụng</div>
                    <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--color-primary)' }}>
                      {proposalsSummary.applied_count || 0}
                    </div>
                  </div>
                  <div className="stat-card" style={{ padding: '8px 12px' }}>
                    <div style={{ fontSize: 11, color: 'var(--color-muted)' }}>Số chương ảnh hưởng</div>
                    <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--color-navy)' }}>
                      {proposalsSummary.affected_chapters_count || 0}
                    </div>
                  </div>
                </div>
              )}

              {/* Proposals List */}
              {isLoadingProposals ? (
                <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--color-muted)' }}>
                  <Loader2 size={24} className="animate-spin" style={{ margin: '0 auto 8px' }} />
                  <div>Đang tải danh sách đề xuất sửa đổi...</div>
                </div>
              ) : proposals.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '36px 16px', color: 'var(--color-muted)' }}>
                  <CheckCircle2 size={32} style={{ color: 'var(--color-success)', marginBottom: 8 }} />
                  <div style={{ fontWeight: 600, color: 'var(--color-navy)' }}>
                    Không có đề xuất sửa đổi nào phù hợp với bộ lọc hiện tại.
                  </div>
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {proposals.map((p) => {
                    const isSafe = p.classification === 'auto_safe';
                    const isApplied = p.status === 'applied';
                    const isConflict = p.status === 'conflict';

                    return (
                      <div
                        key={p.id}
                        style={{
                          display: 'flex',
                          gap: 12,
                          alignItems: 'flex-start',
                          padding: '12px 14px',
                          backgroundColor: isApplied
                            ? '#f0fdf4'
                            : isConflict
                            ? '#fef2f2'
                            : p.is_selected
                            ? 'var(--color-surface, #f8fafc)'
                            : 'var(--color-bg)',
                          border: `1px solid ${
                            isConflict
                              ? '#fca5a5'
                              : p.is_selected
                              ? 'var(--color-primary, #3b82f6)'
                              : 'var(--color-border)'
                          }`,
                          borderRadius: 'var(--radius)',
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={Boolean(p.is_selected)}
                          disabled={isApplied}
                          onChange={() => handleToggleProposal(p.id, p.is_selected)}
                          style={{ marginTop: 4, cursor: isApplied ? 'not-allowed' : 'pointer' }}
                        />

                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 4 }}>
                            <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--color-navy)' }}>
                              {p.chapter_title}
                            </span>
                            <span className="badge-category">{getCategoryLabel(p.category)}</span>
                            <span
                              style={{
                                fontSize: 11,
                                padding: '1px 6px',
                                borderRadius: 8,
                                fontWeight: 600,
                                backgroundColor: isSafe ? '#dcfce7' : '#fef3c7',
                                color: isSafe ? '#15803d' : '#b45309',
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: 3,
                              }}
                            >
                              {isSafe ? <ShieldCheck size={11} /> : <AlertTriangle size={11} />}
                              <span>{isSafe ? 'An toàn tự động' : 'Duyệt thủ công'}</span>
                            </span>

                            {isApplied && (
                              <span style={{ fontSize: 11, padding: '1px 6px', borderRadius: 8, backgroundColor: '#dcfce7', color: '#16a34a', fontWeight: 600 }}>
                                Đã áp dụng
                              </span>
                            )}
                            {isConflict && (
                              <span style={{ fontSize: 11, padding: '1px 6px', borderRadius: 8, backgroundColor: '#fee2e2', color: '#dc2626', fontWeight: 600 }}>
                                Xung đột
                              </span>
                            )}
                          </div>

                          <div style={{ fontSize: 12, color: 'var(--color-muted)', marginBottom: 6 }}>
                            Gốc: <strong style={{ color: 'var(--color-navy)' }}>{p.source_term}</strong> → Chuẩn hóa:{' '}
                            <strong style={{ color: 'var(--color-primary)' }}>{p.canonical_target}</strong>
                          </div>

                          {/* Side-by-side diff view */}
                          <div
                            style={{
                              display: 'grid',
                              gridTemplateColumns: '1fr 1fr',
                              gap: 8,
                              backgroundColor: 'var(--color-bg)',
                              padding: '8px 10px',
                              borderRadius: 4,
                              border: '1px solid var(--color-border)',
                              fontSize: 12,
                              fontFamily: 'var(--font-reading)',
                              marginBottom: 4,
                            }}
                          >
                            <div>
                              <div style={{ fontSize: 10, color: '#dc2626', fontWeight: 700, marginBottom: 2 }}>HIỆN TẠI (CŨ):</div>
                              <div style={{ color: '#991b1b', wordBreak: 'break-word' }}>{p.old_text}</div>
                            </div>
                            <div>
                              <div style={{ fontSize: 10, color: '#16a34a', fontWeight: 700, marginBottom: 2 }}>ĐỀ XUẤT (MỚI):</div>
                              <div style={{ color: '#166534', wordBreak: 'break-word' }}>{p.new_text}</div>
                            </div>
                          </div>

                          {p.reason && (
                            <div style={{ fontSize: 11, color: 'var(--color-muted)', fontStyle: 'italic' }}>
                              Lý do: {p.reason}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div
              className="modal-footer"
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                padding: '12px 20px',
                borderTop: '1px solid var(--color-border)',
              }}
            >
              <div style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                Đã chọn: <strong style={{ color: 'var(--color-navy)' }}>{proposals.filter((p) => p.is_selected && p.status !== 'applied').length}</strong> mục chờ áp dụng.
              </div>

              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setShowCorrectionsModal(false)}
                >
                  Đóng
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={
                    isApplyingProposals ||
                    proposals.filter((p) => p.is_selected && p.status !== 'applied').length === 0
                  }
                  onClick={() => setConfirmApplyModalOpen(true)}
                >
                  {isApplyingProposals ? (
                    <>
                      <Loader2 size={14} className="animate-spin" />
                      <span>Đang áp dụng...</span>
                    </>
                  ) : (
                    <>
                      <Check size={14} />
                      <span>Áp dụng các sửa đổi đã chọn</span>
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Confirmation Modal before applying corrections */}
      {confirmApplyModalOpen && (
        <div className="modal-overlay" style={{ zIndex: 130 }}>
          <div className="modal-dialog" style={{ maxWidth: 480 }}>
            <div className="modal-header">
              <h3 className="modal-title">Xác nhận áp dụng sửa đổi tính nhất quán</h3>
            </div>
            <div className="modal-body">
              <p style={{ margin: '0 0 12px 0' }}>
                Bạn chuẩn bị áp dụng <strong>{proposals.filter((p) => p.is_selected && p.status !== 'applied').length}</strong> sửa đổi tính nhất quán vào bản dịch của sách.
              </p>
              <div
                style={{
                  padding: '10px 12px',
                  backgroundColor: '#f0fdf4',
                  border: '1px solid #bbf7d0',
                  borderRadius: 'var(--radius)',
                  fontSize: 13,
                  color: '#166534',
                  marginBottom: 12,
                }}
              >
                <strong>Bảo vệ dữ liệu an toàn tuyệt đối:</strong> Hệ thống sẽ tự động tạo một bản sao lưu phục hồi (Snapshot) độc lập trước khi thực hiện ghi dữ liệu. Bạn có thể hoàn tác toàn bộ bản sửa đổi này bất kỳ lúc nào chỉ bằng một cú nhấp chuột.
              </div>
              <p style={{ margin: 0, fontSize: 13, color: 'var(--color-muted)' }}>
                Cơ chế khóa lạc quan (Optimistic check) sẽ tự động bỏ qua nếu phân đoạn có thay đổi xung đột từ trước.
              </p>
            </div>
            <div className="modal-footer" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button
                type="button"
                className="btn"
                onClick={() => setConfirmApplyModalOpen(false)}
                disabled={isApplyingProposals}
              >
                Hủy bỏ
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleApplySelectedCorrections}
                disabled={isApplyingProposals}
              >
                {isApplyingProposals ? (
                  <>
                    <Loader2 size={14} className="animate-spin" />
                    <span>Đang tiến hành...</span>
                  </>
                ) : (
                  <span>Xác nhận & Áp dụng</span>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Snapshots & Rollback History Modal */}
      {showSnapshotsModal && (
        <div className="modal-overlay" style={{ zIndex: 120 }}>
          <div
            className="modal-dialog"
            style={{
              maxWidth: 780,
              maxHeight: '85vh',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <div className="modal-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Clock size={18} style={{ color: 'var(--color-primary)' }} />
                <h3 className="modal-title" style={{ margin: 0 }}>Lịch sử sao lưu & Hoàn tác (Snapshots)</h3>
              </div>
              <button
                type="button"
                className="btn-icon"
                onClick={() => setShowSnapshotsModal(false)}
                title="Đóng"
              >
                <X size={18} />
              </button>
            </div>

            <div className="modal-body" style={{ overflowY: 'auto', flex: 1, padding: '16px 20px' }}>
              {snapshotNotice && (
                <div
                  className={`alert-banner alert-banner-${snapshotNotice.type === 'error' ? 'error' : 'success'}`}
                  style={{ marginBottom: 12 }}
                >
                  {snapshotNotice.type === 'error' ? <AlertOctagon size={15} /> : <CheckCircle2 size={15} />}
                  <span>{snapshotNotice.text}</span>
                </div>
              )}

              <p style={{ fontSize: 13, color: 'var(--color-muted)', margin: '0 0 14px 0' }}>
                Dưới đây là các bản sao lưu tự động được tạo trước mỗi lần ghi sửa đổi. Bạn có thể khôi phục lại phân đoạn về trạng thái trước đó bất kỳ lúc nào.
              </p>

              {isLoadingSnapshots ? (
                <div style={{ textAlign: 'center', padding: '30px 0', color: 'var(--color-muted)' }}>
                  <Loader2 size={24} className="animate-spin" style={{ margin: '0 auto 8px' }} />
                  <div>Đang tải danh sách bản sao lưu...</div>
                </div>
              ) : snapshots.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '30px 0', color: 'var(--color-muted)' }}>
                  <Info size={28} style={{ margin: '0 auto 8px', color: 'var(--color-muted)' }} />
                  <div>Chưa có bản sao lưu sửa đổi nào được tạo cho cuốn sách này.</div>
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {snapshots.map((snap) => {
                    const isRolledBack = Boolean(snap.is_rolled_back);

                    return (
                      <div
                        key={snap.id}
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          padding: '12px 14px',
                          border: '1px solid var(--color-border)',
                          borderRadius: 'var(--radius)',
                          backgroundColor: isRolledBack ? 'var(--color-bg)' : '#f8fafc',
                        }}
                      >
                        <div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                            <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--color-navy)' }}>
                              Bản sao lưu #{snap.id.substring(0, 8)}
                            </span>
                            <span
                              style={{
                                fontSize: 11,
                                padding: '1px 6px',
                                borderRadius: 8,
                                fontWeight: 600,
                                backgroundColor: isRolledBack ? '#fee2e2' : '#dcfce7',
                                color: isRolledBack ? '#dc2626' : '#16a34a',
                              }}
                            >
                              {isRolledBack ? 'Đã hoàn tác' : 'Đang hoạt động'}
                            </span>
                          </div>

                          <div style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                            {snap.description}
                          </div>
                          <div style={{ fontSize: 11, color: 'var(--color-muted)', marginTop: 2 }}>
                            Thời gian: {formatDate(snap.created_at)} | Số phân đoạn: <strong>{snap.total_segments_changed}</strong>
                            {isRolledBack && snap.rolled_back_at && (
                              <span> | Hoàn tác lúc: {formatDate(snap.rolled_back_at)}</span>
                            )}
                          </div>
                        </div>

                        {!isRolledBack && (
                          <button
                            type="button"
                            className="btn btn-outline"
                            onClick={() => handleRollbackSnapshot(snap.id)}
                            disabled={rollingBackId === snap.id}
                            title="Khôi phục lại tất cả phân đoạn trong bản sao lưu này"
                          >
                            {rollingBackId === snap.id ? (
                              <>
                                <Loader2 size={13} className="animate-spin" />
                                <span>Đang hoàn tác...</span>
                              </>
                            ) : (
                              <>
                                <Undo2 size={13} />
                                <span>Hoàn tác (Undo)</span>
                              </>
                            )}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div
              className="modal-footer"
              style={{ display: 'flex', justifyContent: 'flex-end', padding: '10px 20px', borderTop: '1px solid var(--color-border)' }}
            >
              <button
                type="button"
                className="btn"
                onClick={() => setShowSnapshotsModal(false)}
              >
                Đóng
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
