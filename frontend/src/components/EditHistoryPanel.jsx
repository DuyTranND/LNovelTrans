import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
  History,
  RotateCcw,
  ChevronDown,
  ChevronRight,
  AlertTriangle,
  RefreshCw,
  CheckCircle,
  X,
  User,
  Clock,
  Layers,
  AlertCircle,
  ChevronLeft,
} from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';

const LIMIT = 20;

// -----------------------------------------------------------------------------
// Modest Style Objects to Consolidate Repetitive Inline Rules
// -----------------------------------------------------------------------------
const styles = {
  panel: {
    display: 'flex',
    flexDirection: 'column',
    gap: 16,
    padding: '16px 20px',
    backgroundColor: 'var(--color-bg, #ffffff)',
    color: 'var(--color-text, #111827)',
    borderRadius: 8,
    border: '1px solid var(--color-border, #e5e7eb)',
    boxShadow: '0 1px 3px rgba(0, 0, 0, 0.05)',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: 12,
    borderBottom: '1px solid var(--color-border, #e5e7eb)',
    paddingBottom: 12,
  },
  diffCard: {
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    padding: 10,
    borderRadius: 6,
    backgroundColor: 'var(--color-rail-bg, #f9fafb)',
    border: '1px solid var(--color-border, #e5e7eb)',
  },
  diffContent: (bgColor, textColor) => ({
    marginTop: 4,
    padding: 6,
    borderRadius: 4,
    backgroundColor: bgColor,
    color: textColor,
    fontFamily: 'inherit',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  }),
  modalOverlay: {
    position: 'fixed',
    inset: 0,
    zIndex: 9999,
    backgroundColor: 'rgba(0, 0, 0, 0.5)',
    backdropFilter: 'blur(2px)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 16,
  },
  modalCard: {
    backgroundColor: 'var(--color-bg, #ffffff)',
    color: 'var(--color-text, #111827)',
    borderRadius: 8,
    border: '1px solid var(--color-border, #d1d5db)',
    boxShadow: '0 20px 25px -5px rgba(0, 0, 0, 0.2)',
    width: '100%',
    maxWidth: 580,
    maxHeight: '90vh',
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
  },
  modalPreviewBox: {
    marginTop: 4,
    padding: 8,
    borderRadius: 4,
    backgroundColor: 'var(--color-rail-bg, #f9fafb)',
    border: '1px solid var(--color-border, #e5e7eb)',
    maxHeight: 120,
    overflowY: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    color: 'var(--color-text, #374151)',
  },
};

/**
 * Admin-only Chapter Manual Edit History & Safe Restore Panel.
 *
 * @param {Object} props
 * @param {string} props.chapterId
 * @param {Object} props.chapter - Current chapter object containing { segments: [{ id, revision, ... }] }
 * @param {Object} props.user - Current user object containing { role, username }
 * @param {boolean} [props.hasUnsavedChanges=false] - Whether unsaved editor drafts exist
 * @param {boolean} [props.isJobActive=false] - Whether a background translation job is active
 * @param {Function} [props.onRestored] - Async callback invoked after successful restore
 */
export default function EditHistoryPanel({
  chapterId,
  chapter,
  user,
  hasUnsavedChanges = false,
  isJobActive = false,
  onRestored,
}) {
  const { lang } = usePreferences();
  const isVi = lang?.startsWith('vi') ?? true;

  // ---------------------------------------------------------------------------
  // Admin Guard: Render nothing if not an admin
  // ---------------------------------------------------------------------------
  const isAdmin = Boolean(
    user &&
      (user.role === 'admin' ||
        user.role === 'Admin' ||
        user.role?.value === 'admin')
  );

  // ---------------------------------------------------------------------------
  // Inline Bilingual Copy
  // ---------------------------------------------------------------------------
  const copy = useMemo(
    () => ({
      title: isVi ? 'Lịch sử chỉnh sửa đoạn' : 'Segment Edit History',
      subtitle: isVi
        ? 'Xem lại các lần lưu thủ công và phục hồi an toàn về phiên bản trước'
        : 'Review manual segment edits and safely restore previous revisions',
      loading: isVi ? 'Đang tải lịch sử chỉnh sửa...' : 'Loading edit history...',
      errorLoading: isVi
        ? 'Không thể tải lịch sử chỉnh sửa.'
        : 'Failed to load edit history.',
      empty: isVi
        ? 'Chưa có bản ghi chỉnh sửa thủ công nào cho chương này.'
        : 'No manual edit history found for this chapter.',
      retry: isVi ? 'Thử lại' : 'Retry',
      refresh: isVi ? 'Làm mới' : 'Refresh',
      prev: isVi ? 'Trang trước' : 'Previous',
      next: isVi ? 'Trang sau' : 'Next',
      pageInfo: (page, totalPages, total) =>
        isVi
          ? `Trang ${page} / ${totalPages} (Tổng ${total} bản ghi)`
          : `Page ${page} of ${totalPages} (${total} total records)`,
      actor: isVi ? 'Người sửa' : 'Actor',
      time: isVi ? 'Thời gian' : 'Time',
      segment: isVi ? 'Đoạn' : 'Segment',
      revision: isVi ? 'Phiên bản' : 'Revision',
      expandDetails: isVi ? 'Xem chi tiết' : 'Show details',
      collapseDetails: isVi ? 'Thu gọn' : 'Hide details',
      oldSource: isVi ? 'Nguồn cũ' : 'Old Source',
      newSource: isVi ? 'Nguồn mới' : 'New Source',
      oldTranslation: isVi ? 'Bản dịch cũ' : 'Old Translation',
      newTranslation: isVi ? 'Bản dịch mới' : 'New Translation',
      noChange: isVi ? '(Không thay đổi)' : '(Unchanged)',
      emptyValue: isVi ? '(Trống)' : '(Empty)',

      // Restore button & confirmation dialog
      restoreBtn: isVi ? 'Phục hồi' : 'Restore',
      restoreModalTitle: isVi
        ? 'Xác nhận phục hồi đoạn'
        : 'Confirm Segment Restore',
      restoreModalWarning: isVi
        ? 'Hành động này sẽ thay thế nguồn và bản dịch hiện tại của phân đoạn bằng nội dung lịch sử đã chọn và tăng số phiên bản.'
        : 'This action will replace the current segment source and translation with this historical snapshot and increment its revision.',
      contentToRestore: isVi
        ? 'Nội dung sẽ được phục hồi:'
        : 'Content snapshot to be restored:',
      currentSegmentRevision: (rev) =>
        isVi ? `Phiên bản hiện tại: ${rev}` : `Current revision: ${rev}`,
      cancel: isVi ? 'Hủy bỏ' : 'Cancel',
      confirmRestore: isVi ? 'Xác nhận phục hồi' : 'Confirm Restore',
      restoring: isVi ? 'Đang phục hồi...' : 'Restoring...',

      // Disabled tooltips / reasons
      disabledDirty: isVi
        ? 'Không thể phục hồi khi đang có thay đổi chưa lưu trên chương. Vui lòng lưu hoặc hủy thay đổi trước.'
        : 'Cannot restore while unsaved editor changes exist. Please save or discard drafts first.',
      disabledActive: isVi
        ? 'Không thể phục hồi khi sách đang có tác vụ xử lý dịch đang chạy.'
        : 'Cannot restore while a translation job is actively running.',
      disabledNoSegment: isVi
        ? 'Phân đoạn này không còn tồn tại trong chương hiện tại.'
        : 'This segment no longer exists in the current chapter.',

      // Conflict / Status messages
      conflictError: isVi
        ? 'Xung đột phiên bản (409): Phân đoạn hoặc chương đã được cập nhật bởi một tác vụ khác. Vui lòng tải lại chương để cập nhật nội dung mới nhất.'
        : 'Revision conflict (409): The segment or chapter was modified by another operation. Please reload the chapter to view the latest content.',
      restoreSuccess: (segId) =>
        isVi
          ? `Đã phục hồi thành công phân đoạn [${segId}].`
          : `Successfully restored segment [${segId}].`,
      restoreFailed: isVi
        ? 'Phục hồi thất bại. Vui lòng thử lại.'
        : 'Restore failed. Please try again.',
      authCheckFailed: isVi
        ? 'Không thể xác thực phiên làm việc (GET /api/auth/me).'
        : 'Failed to verify authentication session (GET /api/auth/me).',
      unauthorizedAdminOnly: isVi
        ? 'Chỉ quản trị viên mới có quyền phục hồi lịch sử.'
        : 'Only administrators have permission to restore history.',
      csrfMissing: isVi
        ? 'Không tìm thấy CSRF token hợp lệ cho phiên đăng nhập.'
        : 'Missing valid CSRF token for session authentication.',
    }),
    [isVi]
  );

  // ---------------------------------------------------------------------------
  // Component State
  // ---------------------------------------------------------------------------
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [fetchError, setFetchError] = useState(null);
  const [expandedIds, setExpandedIds] = useState(() => new Set());

  // Restore Modal State
  const [restoringItem, setRestoringItem] = useState(null);
  const [isRestoring, setIsRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState(null);
  const [statusMessage, setStatusMessage] = useState(null);

  // Active chapter guard & modal accessibility refs
  const activeChapterIdRef = useRef(chapterId);
  const modalRef = useRef(null);
  const cancelButtonRef = useRef(null);
  const triggerElementRef = useRef(null);

  // Helper to find segment in current chapter
  const getSegmentFromCurrentChapter = useCallback(
    (segId) => {
      if (!chapter?.segments || !Array.isArray(chapter.segments)) return null;
      return chapter.segments.find((s) => s.id === segId) || null;
    },
    [chapter?.segments]
  );

  // Reset state whenever chapterId changes
  useEffect(() => {
    activeChapterIdRef.current = chapterId;
    setOffset(0);
    setExpandedIds(new Set());
    setFetchError(null);
    setRestoreError(null);
    setStatusMessage(null);
    setRestoringItem(null);
  }, [chapterId]);

  // ---------------------------------------------------------------------------
  // Data Fetching with AbortController, Admin Guard & Identity Guard
  // ---------------------------------------------------------------------------
  const fetchHistory = useCallback(
    async (currentOffset, signal) => {
      if (!isAdmin || !chapterId) {
        setItems([]);
        setTotal(0);
        return;
      }

      setIsLoading(true);
      setFetchError(null);

      try {
        const res = await fetch(
          `/api/chapters/${encodeURIComponent(chapterId)}/edit-history?limit=${LIMIT}&offset=${currentOffset}`,
          {
            credentials: 'same-origin',
            signal,
          }
        );

        if (signal?.aborted || activeChapterIdRef.current !== chapterId) {
          return;
        }

        if (!res.ok) {
          const errData = await res.json().catch(() => null);
          const msg =
            errData?.detail || `HTTP ${res.status}: ${res.statusText}`;
          throw new Error(msg);
        }

        const data = await res.json();
        if (signal?.aborted || activeChapterIdRef.current !== chapterId) {
          return;
        }

        setItems(Array.isArray(data?.items) ? data.items : []);
        setTotal(typeof data?.total === 'number' ? data.total : 0);
      } catch (err) {
        if (signal?.aborted || activeChapterIdRef.current !== chapterId) {
          return;
        }
        setFetchError(err.message || copy.errorLoading);
      } finally {
        if (!signal?.aborted && activeChapterIdRef.current === chapterId) {
          setIsLoading(false);
        }
      }
    },
    [isAdmin, chapterId, copy.errorLoading]
  );

  useEffect(() => {
    if (!isAdmin || !chapterId) return;

    const controller = new AbortController();
    fetchHistory(offset, controller.signal);
    return () => {
      controller.abort();
    };
  }, [isAdmin, chapterId, offset, fetchHistory]);

  // ---------------------------------------------------------------------------
  // Modal Accessibility: Initial Focus Cancel, Focus Trap & Return Focus
  // ---------------------------------------------------------------------------
  const handleCloseRestoreModal = useCallback(() => {
    if (isRestoring) return;
    setRestoringItem(null);
    setRestoreError(null);

    // Return focus to the trigger button that opened the modal
    setTimeout(() => {
      triggerElementRef.current?.focus?.();
    }, 0);
  }, [isRestoring]);

  useEffect(() => {
    if (!restoringItem) return;

    // Initial focus on Cancel button
    const timer = setTimeout(() => {
      cancelButtonRef.current?.focus();
    }, 50);

    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        if (!isRestoring) {
          handleCloseRestoreModal();
        }
        return;
      }

      // Focus trap within modal
      if (e.key === 'Tab') {
        if (!modalRef.current) return;
        const focusables = modalRef.current.querySelectorAll(
          'button:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (!focusables || focusables.length === 0) return;

        const firstEl = focusables[0];
        const lastEl = focusables[focusables.length - 1];

        if (e.shiftKey) {
          if (document.activeElement === firstEl) {
            e.preventDefault();
            lastEl.focus();
          }
        } else {
          if (document.activeElement === lastEl) {
            e.preventDefault();
            firstEl.focus();
          }
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      clearTimeout(timer);
    };
  }, [restoringItem, isRestoring, handleCloseRestoreModal]);

  // Early return if not admin (cleanly placed after all hooks)
  if (!isAdmin) {
    return null;
  }

  // ---------------------------------------------------------------------------
  // Toggle Row Expansion
  // ---------------------------------------------------------------------------
  const toggleRow = (id) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // ---------------------------------------------------------------------------
  // Restore Handlers & Guards
  // ---------------------------------------------------------------------------
  const getDisabledReason = (item) => {
    if (hasUnsavedChanges) return copy.disabledDirty;
    if (isJobActive) return copy.disabledActive;
    const seg = getSegmentFromCurrentChapter(item.segment_id);
    if (!seg) return copy.disabledNoSegment;
    return null;
  };

  const handleOpenRestoreModal = (item, e) => {
    const reason = getDisabledReason(item);
    if (reason) return;
    triggerElementRef.current = e?.currentTarget || document.activeElement;
    setRestoringItem(item);
    setRestoreError(null);
  };

  const handleExecuteRestore = async () => {
    if (!restoringItem) return;

    // 1. Re-check all guards at confirmation time
    if (!isAdmin) {
      setRestoreError(copy.unauthorizedAdminOnly);
      return;
    }
    if (hasUnsavedChanges) {
      setRestoreError(copy.disabledDirty);
      return;
    }
    if (isJobActive) {
      setRestoreError(copy.disabledActive);
      return;
    }

    // 2. Freeze operation identity & revision snapshot BEFORE any async call
    const opChapterId = chapterId;
    const opHistoryId = restoringItem.id;
    const opSegmentId = restoringItem.segment_id;
    const targetSeg = getSegmentFromCurrentChapter(opSegmentId);
    if (!targetSeg || targetSeg.revision === undefined) {
      setRestoreError(copy.disabledNoSegment);
      return;
    }
    const opExpectedRevision = targetSeg.revision;

    setIsRestoring(true);
    setRestoreError(null);

    try {
      // Step A: Verify auth & obtain CSRF token from /api/auth/me
      const meRes = await fetch('/api/auth/me', {
        credentials: 'same-origin',
      });

      // Chapter drift guard after meRes await
      if (activeChapterIdRef.current !== opChapterId) return;

      if (!meRes.ok) {
        throw new Error(copy.authCheckFailed);
      }

      const meData = await meRes.json();
      if (activeChapterIdRef.current !== opChapterId) return;

      if (
        !meData?.authenticated ||
        (meData?.role !== 'admin' && meData?.role !== 'Admin')
      ) {
        throw new Error(copy.unauthorizedAdminOnly);
      }

      const csrfToken = meData?.csrf_token;
      const isAuthOverrideAdmin =
        meData?.auth_override === true &&
        (meData?.role === 'admin' || meData?.role === 'Admin');

      // Require CSRF token for cookie sessions; allow missing ONLY on local auth_override admin
      if (!csrfToken && !isAuthOverrideAdmin) {
        throw new Error(copy.csrfMissing);
      }

      // Step B: POST /api/chapters/{chapterId}/edit-history/{historyId}/restore
      const headers = {
        'Content-Type': 'application/json',
      };
      if (csrfToken) {
        headers['X-CSRF-Token'] = csrfToken;
      }

      const restoreUrl = `/api/chapters/${encodeURIComponent(opChapterId)}/edit-history/${encodeURIComponent(opHistoryId)}/restore`;
      const restoreRes = await fetch(restoreUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers,
        body: JSON.stringify({
          expected_revision: opExpectedRevision,
        }),
      });

      // Chapter drift guard after restoreRes await
      if (activeChapterIdRef.current !== opChapterId) return;

      // Handle 409 Conflict: keep confirmation OPEN, show reload message, do not auto-retry
      if (restoreRes.status === 409) {
        const conflictData = await restoreRes.json().catch(() => null);
        const detailMessage =
          typeof conflictData?.detail === 'string'
            ? conflictData.detail
            : conflictData?.detail?.message;
        setRestoreError(detailMessage || copy.conflictError);
        return;
      }

      if (!restoreRes.ok) {
        const errJson = await restoreRes.json().catch(() => null);
        const errDetail =
          errJson?.detail ||
          `HTTP ${restoreRes.status}: ${restoreRes.statusText}`;
        throw new Error(errDetail);
      }

      // Step C: Success flow - close modal & return focus
      setRestoringItem(null);
      setStatusMessage({
        type: 'success',
        text: copy.restoreSuccess(opSegmentId),
      });
      setTimeout(() => {
        triggerElementRef.current?.focus?.();
      }, 0);

      // Invoke parent onRestored callback with chapter drift guard
      if (typeof onRestored === 'function') {
        try {
          await onRestored();
        } catch {
          // Parent refresh failure should not crash panel
        }
        if (activeChapterIdRef.current !== opChapterId) return;
      }

      // Refresh edit history list for the restored chapter
      fetchHistory(offset);
    } catch (err) {
      if (activeChapterIdRef.current !== opChapterId) return;
      setRestoreError(err.message || copy.restoreFailed);
    } finally {
      if (activeChapterIdRef.current === opChapterId) {
        setIsRestoring(false);
      }
    }
  };

  // ---------------------------------------------------------------------------
  // Pagination Calculations
  // ---------------------------------------------------------------------------
  const currentPage = Math.floor(offset / LIMIT) + 1;
  const totalPages = Math.max(1, Math.ceil(total / LIMIT));
  const canPrev = offset > 0 && !isLoading;
  const canNext = offset + LIMIT < total && !isLoading;

  return (
    <div className="edit-history-panel" style={styles.panel}>
      {/* Header */}
      <div style={styles.header}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <History size={20} color="#2563eb" />
          <div>
            <h3
              style={{
                margin: 0,
                fontSize: 16,
                fontWeight: 600,
                color: 'var(--color-text, #111827)',
              }}
            >
              {copy.title}
            </h3>
            <p
              style={{
                margin: '2px 0 0 0',
                fontSize: 12,
                color: 'var(--color-muted, #6b7280)',
              }}
            >
              {copy.subtitle}
            </p>
          </div>
        </div>

        <button
          type="button"
          className="btn btn-sm"
          onClick={() => fetchHistory(offset)}
          disabled={isLoading || !chapterId}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            fontSize: 13,
            padding: '6px 12px',
            cursor: isLoading ? 'not-allowed' : 'pointer',
            backgroundColor: 'var(--color-bg, #ffffff)',
            color: 'var(--color-text, #374151)',
            border: '1px solid var(--color-border, #d1d5db)',
            borderRadius: 6,
          }}
          title={copy.refresh}
        >
          <RefreshCw
            size={14}
            className={isLoading ? 'spin-animation' : ''}
            style={{ animation: isLoading ? 'spin 1s linear infinite' : 'none' }}
          />
          <span>{copy.refresh}</span>
        </button>
      </div>

      {/* Status or Alert Notification */}
      {statusMessage && (
        <div
          role="status"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 8,
            padding: '10px 14px',
            borderRadius: 6,
            fontSize: 13,
            backgroundColor:
              statusMessage.type === 'success' ? '#ecfdf5' : '#fef2f2',
            color: statusMessage.type === 'success' ? '#065f46' : '#991b1b',
            border: `1px solid ${
              statusMessage.type === 'success' ? '#a7f3d0' : '#fecaca'
            }`,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <CheckCircle size={16} />
            <span>{statusMessage.text}</span>
          </div>
          <button
            type="button"
            onClick={() => setStatusMessage(null)}
            style={{
              background: 'transparent',
              border: 'none',
              cursor: 'pointer',
              color: 'inherit',
              padding: 2,
            }}
            aria-label="Close notification"
          >
            <X size={14} />
          </button>
        </div>
      )}

      {/* Unsaved changes or active job warning banners */}
      {(hasUnsavedChanges || isJobActive) && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '10px 14px',
            borderRadius: 6,
            fontSize: 12,
            backgroundColor: '#fffbeb',
            color: '#92400e',
            border: '1px solid #fde68a',
          }}
        >
          <AlertTriangle size={16} color="#d97706" style={{ flexShrink: 0 }} />
          <span>
            {hasUnsavedChanges ? copy.disabledDirty : copy.disabledActive}
          </span>
        </div>
      )}

      {/* Loading State */}
      {isLoading && (
        <div
          style={{
            padding: '32px 16px',
            textAlign: 'center',
            color: 'var(--color-muted, #6b7280)',
            fontSize: 14,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 8,
          }}
        >
          <RefreshCw
            size={18}
            style={{ animation: 'spin 1s linear infinite' }}
          />
          <span>{copy.loading}</span>
        </div>
      )}

      {/* Error State */}
      {!isLoading && fetchError && (
        <div
          role="alert"
          style={{
            padding: '16px',
            textAlign: 'center',
            backgroundColor: '#fef2f2',
            color: '#991b1b',
            borderRadius: 6,
            border: '1px solid #fecaca',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <AlertCircle size={22} color="#dc2626" />
          <p style={{ margin: 0, fontSize: 13 }}>{fetchError}</p>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => fetchHistory(offset)}
            style={{
              marginTop: 4,
              padding: '4px 12px',
              fontSize: 12,
              cursor: 'pointer',
            }}
          >
            {copy.retry}
          </button>
        </div>
      )}

      {/* Empty State */}
      {!isLoading && !fetchError && items.length === 0 && (
        <div
          style={{
            padding: '36px 16px',
            textAlign: 'center',
            color: 'var(--color-muted, #6b7280)',
            fontSize: 13,
            backgroundColor: 'var(--color-rail-bg, #f9fafb)',
            borderRadius: 6,
            border: '1px dashed var(--color-border, #d1d5db)',
          }}
        >
          <History size={28} style={{ opacity: 0.4, marginBottom: 8 }} />
          <p style={{ margin: 0 }}>{copy.empty}</p>
        </div>
      )}

      {/* History Items List */}
      {!isLoading && !fetchError && items.length > 0 && (
        <div
          className="history-list"
          style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
        >
          {items.map((item) => {
            const isExpanded = expandedIds.has(item.id);
            const disabledReason = getDisabledReason(item);
            const isRestoreDisabled = Boolean(disabledReason) || isRestoring;

            // Date formatting safely
            const dateStr = item.created_at || item.timestamp;
            let formattedDate = dateStr;
            try {
              if (dateStr) {
                formattedDate = new Date(dateStr).toLocaleString(
                  isVi ? 'vi-VN' : 'en-US',
                  {
                    year: 'numeric',
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                  }
                );
              }
            } catch {
              formattedDate = String(dateStr);
            }

            const actorName = item.actor_username || item.actor || 'admin';

            return (
              <div
                key={item.id}
                className="history-item-card"
                style={{
                  border: '1px solid var(--color-border, #e5e7eb)',
                  borderRadius: 6,
                  backgroundColor: 'var(--color-bg, #ffffff)',
                  overflow: 'hidden',
                  transition: 'border-color 0.15s ease',
                }}
              >
                {/* Summary Row */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '10px 14px',
                    backgroundColor: isExpanded
                      ? 'var(--color-rail-bg, #f9fafb)'
                      : 'transparent',
                    cursor: 'pointer',
                    userSelect: 'none',
                    gap: 12,
                    flexWrap: 'wrap',
                  }}
                  onClick={() => toggleRow(item.id)}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 12,
                      flexWrap: 'wrap',
                      fontSize: 13,
                    }}
                  >
                    <button
                      type="button"
                      aria-expanded={isExpanded}
                      aria-label={
                        isExpanded ? copy.collapseDetails : copy.expandDetails
                      }
                      style={{
                        background: 'transparent',
                        border: 'none',
                        cursor: 'pointer',
                        padding: 0,
                        display: 'flex',
                        alignItems: 'center',
                        color: 'var(--color-muted, #6b7280)',
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleRow(item.id);
                      }}
                    >
                      {isExpanded ? (
                        <ChevronDown size={18} />
                      ) : (
                        <ChevronRight size={18} />
                      )}
                    </button>

                    {/* Segment Badge */}
                    <span
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                        padding: '2px 8px',
                        borderRadius: 4,
                        fontSize: 12,
                        fontWeight: 600,
                        backgroundColor: '#e0e7ff',
                        color: '#3730a3',
                      }}
                    >
                      <Layers size={12} />
                      <span>
                        {copy.segment}: {String(item.segment_id)}
                      </span>
                    </span>

                    {/* Revision Badge */}
                    <span
                      style={{
                        fontSize: 12,
                        color: 'var(--color-muted, #6b7280)',
                      }}
                    >
                      {copy.revision}: {item.old_revision ?? 0} &rarr;{' '}
                      <strong>{item.new_revision ?? 0}</strong>
                    </span>

                    {/* Actor */}
                    <span
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                        color: 'var(--color-text, #374151)',
                        fontSize: 12,
                      }}
                    >
                      <User size={13} style={{ opacity: 0.7 }} />
                      <span>{actorName}</span>
                    </span>

                    {/* Timestamp */}
                    <span
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                        color: 'var(--color-muted, #6b7280)',
                        fontSize: 12,
                      }}
                    >
                      <Clock size={13} style={{ opacity: 0.7 }} />
                      <span>{formattedDate}</span>
                    </span>
                  </div>

                  {/* Actions */}
                  <div
                    style={{ display: 'flex', alignItems: 'center', gap: 8 }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      className="btn btn-sm btn-restore"
                      disabled={isRestoreDisabled}
                      title={disabledReason || copy.restoreBtn}
                      onClick={(e) => handleOpenRestoreModal(item, e)}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 6,
                        padding: '4px 10px',
                        fontSize: 12,
                        fontWeight: 500,
                        borderRadius: 4,
                        cursor: isRestoreDisabled ? 'not-allowed' : 'pointer',
                        opacity: isRestoreDisabled ? 0.5 : 1,
                        backgroundColor: isRestoreDisabled
                          ? 'var(--color-border, #e5e7eb)'
                          : '#2563eb',
                        color: isRestoreDisabled
                          ? 'var(--color-muted, #6b7280)'
                          : '#ffffff',
                        border: 'none',
                        transition: 'background-color 0.15s ease',
                      }}
                    >
                      <RotateCcw size={13} />
                      <span>{copy.restoreBtn}</span>
                    </button>
                  </div>
                </div>

                {/* Expanded Diff Details (Safe text nodes / zero markup injection) */}
                {isExpanded && (
                  <div
                    style={{
                      padding: '12px 16px',
                      borderTop: '1px solid var(--color-border, #e5e7eb)',
                      backgroundColor: 'var(--color-bg, #ffffff)',
                      display: 'grid',
                      gridTemplateColumns:
                        'repeat(auto-fit, minmax(280px, 1fr))',
                      gap: 16,
                      fontSize: 12,
                    }}
                  >
                    {/* Source Diff Panel */}
                    <div style={styles.diffCard}>
                      <strong
                        style={{
                          color: 'var(--color-text, #374151)',
                          fontSize: 12,
                        }}
                      >
                        {copy.oldSource} &rarr; {copy.newSource}
                      </strong>
                      <div>
                        <span
                          style={{
                            fontWeight: 600,
                            color: '#991b1b',
                            fontSize: 11,
                          }}
                        >
                          &minus; {copy.oldSource}:
                        </span>
                        <div style={styles.diffContent('#fef2f2', '#7f1d1d')}>
                          {item.old_source !== null &&
                          item.old_source !== undefined
                            ? item.old_source || copy.emptyValue
                            : copy.noChange}
                        </div>
                      </div>
                      <div>
                        <span
                          style={{
                            fontWeight: 600,
                            color: '#065f46',
                            fontSize: 11,
                          }}
                        >
                          &#43; {copy.newSource}:
                        </span>
                        <div style={styles.diffContent('#ecfdf5', '#064e3b')}>
                          {item.new_source !== null &&
                          item.new_source !== undefined
                            ? item.new_source || copy.emptyValue
                            : copy.noChange}
                        </div>
                      </div>
                    </div>

                    {/* Translation Diff Panel */}
                    <div style={styles.diffCard}>
                      <strong
                        style={{
                          color: 'var(--color-text, #374151)',
                          fontSize: 12,
                        }}
                      >
                        {copy.oldTranslation} &rarr; {copy.newTranslation}
                      </strong>
                      <div>
                        <span
                          style={{
                            fontWeight: 600,
                            color: '#991b1b',
                            fontSize: 11,
                          }}
                        >
                          &minus; {copy.oldTranslation}:
                        </span>
                        <div style={styles.diffContent('#fef2f2', '#7f1d1d')}>
                          {item.old_translation !== null &&
                          item.old_translation !== undefined
                            ? item.old_translation || copy.emptyValue
                            : copy.noChange}
                        </div>
                      </div>
                      <div>
                        <span
                          style={{
                            fontWeight: 600,
                            color: '#065f46',
                            fontSize: 11,
                          }}
                        >
                          &#43; {copy.newTranslation}:
                        </span>
                        <div style={styles.diffContent('#ecfdf5', '#064e3b')}>
                          {item.new_translation !== null &&
                          item.new_translation !== undefined
                            ? item.new_translation || copy.emptyValue
                            : copy.noChange}
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Bounded Pagination Footer */}
      {!isLoading && !fetchError && total > 0 && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderTop: '1px solid var(--color-border, #e5e7eb)',
            paddingTop: 12,
            marginTop: 4,
            fontSize: 13,
            color: 'var(--color-muted, #6b7280)',
            flexWrap: 'wrap',
            gap: 12,
          }}
        >
          <span>{copy.pageInfo(currentPage, totalPages, total)}</span>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <button
              type="button"
              className="btn btn-sm"
              disabled={!canPrev}
              onClick={() => setOffset((prev) => Math.max(0, prev - LIMIT))}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
                padding: '4px 10px',
                fontSize: 12,
                cursor: canPrev ? 'pointer' : 'not-allowed',
                opacity: canPrev ? 1 : 0.4,
              }}
              aria-label={copy.prev}
            >
              <ChevronLeft size={14} />
              <span>{copy.prev}</span>
            </button>

            <button
              type="button"
              className="btn btn-sm"
              disabled={!canNext}
              onClick={() => setOffset((prev) => prev + LIMIT)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
                padding: '4px 10px',
                fontSize: 12,
                cursor: canNext ? 'pointer' : 'not-allowed',
                opacity: canNext ? 1 : 0.4,
              }}
              aria-label={copy.next}
            >
              <span>{copy.next}</span>
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}

      {/* --------------------------------------------------------------------- */}
      {/* Explicit Confirmation Dialog (Never Auto-Restore)                     */}
      {/* --------------------------------------------------------------------- */}
      {restoringItem && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="restore-modal-title"
          style={styles.modalOverlay}
        >
          <div ref={modalRef} style={styles.modalCard}>
            {/* Modal Header */}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '16px 20px',
                borderBottom: '1px solid var(--color-border, #e5e7eb)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <RotateCcw size={18} color="#2563eb" />
                <h4
                  id="restore-modal-title"
                  style={{ margin: 0, fontSize: 16, fontWeight: 600 }}
                >
                  {copy.restoreModalTitle}
                </h4>
              </div>

              <button
                type="button"
                onClick={handleCloseRestoreModal}
                disabled={isRestoring}
                aria-label={copy.cancel}
                style={{
                  background: 'transparent',
                  border: 'none',
                  cursor: isRestoring ? 'not-allowed' : 'pointer',
                  color: 'var(--color-muted, #6b7280)',
                  padding: 4,
                }}
              >
                <X size={18} />
              </button>
            </div>

            {/* Modal Body */}
            <div
              style={{
                padding: '16px 20px',
                overflowY: 'auto',
                display: 'flex',
                flexDirection: 'column',
                gap: 14,
                fontSize: 13,
              }}
            >
              {/* Warning note */}
              <div
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 10,
                  padding: '10px 12px',
                  borderRadius: 6,
                  backgroundColor: '#fffbeb',
                  color: '#92400e',
                  border: '1px solid #fde68a',
                  fontSize: 12,
                  lineHeight: 1.5,
                }}
              >
                <AlertTriangle
                  size={16}
                  color="#d97706"
                  style={{ flexShrink: 0, marginTop: 2 }}
                />
                <div>
                  <p style={{ margin: 0, fontWeight: 500 }}>
                    {copy.restoreModalWarning}
                  </p>
                  <p
                    style={{
                      margin: '4px 0 0 0',
                      color: '#b45309',
                      fontSize: 11,
                    }}
                  >
                    {copy.currentSegmentRevision(
                      getSegmentFromCurrentChapter(restoringItem.segment_id)
                        ?.revision ?? 0
                    )}
                  </p>
                </div>
              </div>

              {/* Error in modal if any (e.g. 409 conflict, reload message) */}
              {restoreError && (
                <div
                  role="alert"
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 8,
                    padding: '10px 12px',
                    borderRadius: 6,
                    backgroundColor: '#fef2f2',
                    color: '#991b1b',
                    border: '1px solid #fecaca',
                    fontSize: 12,
                    lineHeight: 1.5,
                  }}
                >
                  <AlertCircle
                    size={16}
                    color="#dc2626"
                    style={{ flexShrink: 0, marginTop: 2 }}
                  />
                  <span>{restoreError}</span>
                </div>
              )}

              {/* Exact Old Content Snapshot Being Restored */}
              <div>
                <strong
                  style={{
                    display: 'block',
                    marginBottom: 8,
                    color: 'var(--color-text, #111827)',
                  }}
                >
                  {copy.contentToRestore}
                </strong>

                {/* Old Source */}
                <div style={{ marginBottom: 10 }}>
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: 'var(--color-muted, #6b7280)',
                    }}
                  >
                    {copy.oldSource}:
                  </span>
                  <div style={styles.modalPreviewBox}>
                    {restoringItem.old_source || <em>{copy.emptyValue}</em>}
                  </div>
                </div>

                {/* Old Translation */}
                <div>
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: 'var(--color-muted, #6b7280)',
                    }}
                  >
                    {copy.oldTranslation}:
                  </span>
                  <div style={styles.modalPreviewBox}>
                    {restoringItem.old_translation || (
                      <em>{copy.emptyValue}</em>
                    )}
                  </div>
                </div>
              </div>
            </div>

            {/* Modal Footer */}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'flex-end',
                gap: 10,
                padding: '12px 20px',
                borderTop: '1px solid var(--color-border, #e5e7eb)',
                backgroundColor: 'var(--color-rail-bg, #f9fafb)',
              }}
            >
              <button
                type="button"
                ref={cancelButtonRef}
                className="btn"
                onClick={handleCloseRestoreModal}
                disabled={isRestoring}
                style={{
                  padding: '6px 14px',
                  fontSize: 13,
                  cursor: isRestoring ? 'not-allowed' : 'pointer',
                  backgroundColor: 'var(--color-bg, #ffffff)',
                  border: '1px solid var(--color-border, #d1d5db)',
                  borderRadius: 6,
                  color: 'var(--color-text, #374151)',
                }}
              >
                {copy.cancel}
              </button>

              <button
                type="button"
                className="btn btn-primary"
                onClick={handleExecuteRestore}
                disabled={isRestoring}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  padding: '6px 14px',
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: isRestoring ? 'not-allowed' : 'pointer',
                  backgroundColor: '#2563eb',
                  color: '#ffffff',
                  border: 'none',
                  borderRadius: 6,
                  opacity: isRestoring ? 0.7 : 1,
                }}
              >
                <RotateCcw
                  size={14}
                  className={isRestoring ? 'spin-animation' : ''}
                  style={{
                    animation: isRestoring ? 'spin 1s linear infinite' : 'none',
                  }}
                />
                <span>
                  {isRestoring ? copy.restoring : copy.confirmRestore}
                </span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
