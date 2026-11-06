import React from 'react';
import {
  Play,
  Pause,
  RotateCcw,
  Clock,
  CheckCircle2,
  AlertOctagon,
  Loader2,
  Calendar,
  Layers,
  Key,
} from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';

export default function JobsTab({
  jobs = [],
  onPauseJob,
  onResumeJob,
  onRetryJob,
  onLaunchSelectedChapter,
  onPromptLaunchAll,
  selectedChapterTitle,
  isLoading,
  actionLoadingId,
  hasActiveJob = false,
  keyStatus = null,
  presets = [],
  selectedPresetId = '',
  onSelectPresetId,
}) {
  const { t, lang } = usePreferences();

  const getStatusBadge = (status) => {
    let label = status;
    let className = '';
    let icon = null;
    let spin = false;

    if (status === 'running') { label = t('statusRunning'); className = 'job-status-running'; icon = Loader2; spin = true; }
    else if (status === 'pausing') { label = t('statusPausing'); className = 'job-status-pausing'; icon = Loader2; spin = true; }
    else if (status === 'paused') { label = t('statusPaused'); className = 'job-status-paused'; icon = Pause; }
    else if (status === 'queued') { label = t('statusQueued'); className = 'job-status-queued'; icon = Clock; }
    else if (status === 'completed') { label = t('statusCompleted'); className = 'job-status-completed'; icon = CheckCircle2; }
    else if (status === 'failed') { label = t('statusFailed'); className = 'job-status-failed'; icon = AlertOctagon; }

    const IconComponent = icon;

    return (
      <span className={`job-status-badge ${className}`}>
        {IconComponent && (
          <IconComponent
            size={12}
            className={spin ? 'animate-spin' : ''}
            style={{ display: 'inline', marginRight: 4 }}
          />
        )}
        {label}
      </span>
    );
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Top action bar to launch jobs */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: 12,
          padding: 14,
          backgroundColor: 'var(--color-rail-bg)',
          borderRadius: 'var(--radius)',
          border: '1px solid var(--color-border)',
        }}
      >
        <div>
          <div style={{ fontWeight: 600, fontSize: 14, color: 'var(--color-navy)' }}>
            {t('jobsTitle')}
          </div>
          <div style={{ fontSize: 13, color: 'var(--color-muted)', marginTop: 2 }}>
            {t('jobsDesc')}
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          {presets && presets.length > 0 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                {t('presetSelectLabel')}
              </span>
              <select
                className="input"
                style={{ padding: '4px 8px', fontSize: 12, minWidth: 150 }}
                value={selectedPresetId || ''}
                onChange={(e) => onSelectPresetId && onSelectPresetId(e.target.value)}
                disabled={hasActiveJob}
                aria-label={t('presetSelectLabel')}
              >
                {presets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} {p.is_default ? `(${t('presetDefaultBadge')})` : ''}
                  </option>
                ))}
              </select>
            </div>
          )}

          <button
            type="button"
            className="btn btn-primary"
            onClick={onLaunchSelectedChapter}
            disabled={hasActiveJob || !selectedChapterTitle}
            title={t('jobTranslateSelected')}
          >
            <Play size={14} />
            <span>{t('jobTranslateSelected')}</span>
          </button>

          <button
            type="button"
            className="btn"
            onClick={onPromptLaunchAll}
            disabled={hasActiveJob}
            title={t('jobTranslateAll')}
          >
            <Layers size={14} />
            <span>{t('jobTranslateAll')}</span>
          </button>
        </div>
      </div>

      {/* Safe Vietnamese Key Indicator Panel */}
      {keyStatus && (
        <div
          className="key-status-panel"
          style={{
            padding: '12px 16px',
            backgroundColor: 'var(--color-rail-bg)',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--color-border)',
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              flexWrap: 'wrap',
              gap: 8,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Key size={15} color="var(--color-primary)" />
              <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--color-navy)' }}>
                {t('keyStatusTitle')}
              </span>
              <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                ({keyStatus.count}/3 khóa được cấu hình)
              </span>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12, color: 'var(--color-muted)' }}>
              {keyStatus.inflight > 0 && (
                <span style={{ color: 'var(--color-primary)', fontWeight: 500 }}>
                  {t('keyInflightTotal', { count: keyStatus.inflight })}
                </span>
              )}
              {keyStatus.last_used && (
                <span>
                  {t('keyLastUsed', { time: new Date(keyStatus.last_used).toLocaleTimeString(lang === 'en' ? 'en-US' : 'vi-VN') })}
                </span>
              )}
            </div>
          </div>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
              gap: 8,
            }}
          >
            {(keyStatus.slots || []).map((slot) => {
              const isConfigured = slot.configured;
              const isCooldown = slot.status === 'cooldown';
              const isInflight = (slot.inflight || 0) > 0;

              let badgeText = t('keyStatusNotConfigured');
              let badgeColor = 'var(--color-muted)';
              let badgeBg = 'rgba(100, 116, 139, 0.1)';

              if (isConfigured) {
                if (isCooldown) {
                  badgeText = t('keyStatusCooldown', { seconds: Math.ceil(slot.cooldown_remaining || 0) });
                  badgeColor = '#d97706';
                  badgeBg = 'rgba(217, 119, 6, 0.1)';
                } else if (isInflight) {
                  badgeText = t('keyStatusBusy', { count: slot.inflight });
                  badgeColor = '#2563eb';
                  badgeBg = 'rgba(37, 99, 235, 0.1)';
                } else {
                  badgeText = t('keyStatusReady');
                  badgeColor = '#16a34a';
                  badgeBg = 'rgba(22, 163, 74, 0.1)';
                }
              }

              return (
                <div
                  key={slot.slot}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius)',
                    backgroundColor: 'var(--color-bg)',
                    border: '1px solid var(--color-border)',
                    fontSize: 12,
                  }}
                >
                  <span style={{ fontWeight: 500, color: isConfigured ? 'var(--color-navy)' : 'var(--color-muted)' }}>
                    {slot.label}
                  </span>
                  <span
                    style={{
                      padding: '2px 8px',
                      borderRadius: 12,
                      fontSize: 11,
                      fontWeight: 600,
                      color: badgeColor,
                      backgroundColor: badgeBg,
                    }}
                  >
                    {badgeText}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Jobs List */}
      {isLoading && jobs.length === 0 ? (
        <div style={{ padding: 60, textAlign: 'center', color: 'var(--color-muted)' }}>
          <Loader2 size={24} className="animate-spin" style={{ margin: '0 auto 12px' }} />
          {t('loadingJobs')}
        </div>
      ) : jobs.length === 0 ? (
        <div className="empty-state-card" style={{ padding: '40px 16px' }}>
          <Clock className="empty-state-icon" />
          <h4 className="empty-state-title">{t('noJobs')}</h4>
          <p className="empty-state-desc" dangerouslySetInnerHTML={{ __html: t('noJobsDesc') }}></p>
        </div>
      ) : (
        <div className="jobs-list">
          {jobs.map((job) => {
            const completed = job.completed_segments ?? 0;
            const total = job.total_segments ?? 0;
            const percent = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
            const isProcessingThis = actionLoadingId === job.id;
            const isPausing = job.status === 'pausing' || (job.status === 'running' && isProcessingThis);
            
            let statusLabel = job.status;
            if (job.status === 'running') statusLabel = t('statusRunning');
            else if (job.status === 'pausing') statusLabel = t('statusPausing');
            else if (job.status === 'paused') statusLabel = t('statusPaused');
            else if (job.status === 'queued') statusLabel = t('statusQueued');
            else if (job.status === 'completed') statusLabel = t('statusCompleted');
            else if (job.status === 'failed') statusLabel = t('statusFailed');
            
            const isCapped = Boolean(job.params && job.params.max_segments);
            const stage = job.progress?.stage || job.stage;

            return (
              <div key={job.id} className="job-card">
                <div className="job-card-header">
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                    <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--color-navy)' }}>
                      {job.chapter_ids && job.chapter_ids.length > 1
                        ? t('jobTypeAll', { count: job.chapter_ids.length })
                        : isCapped
                        ? t('jobTypeLimited', { count: job.params.max_segments })
                        : t('jobTypeChapter')}
                    </span>
                    {getStatusBadge(isPausing ? 'pausing' : job.status)}
                    {job.chapter_ids && job.chapter_ids.length === 1 && !isCapped && (
                      <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                        {t('jobFullChapterNote')}
                      </span>
                    )}
                  </div>

                  {job.created_at && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, color: 'var(--color-muted)' }}>
                      <Calendar size={13} />
                      <span>{new Date(job.created_at).toLocaleString(lang === 'en' ? 'en-US' : 'vi-VN')}</span>
                    </div>
                  )}
                </div>

                {/* Progress bar */}
                <div className="job-progress-container">
                  <div className="job-progress-bar-bg">
                    <div
                      className="job-progress-bar-fill"
                      style={{
                        width: `${percent}%`,
                        backgroundColor:
                          job.status === 'completed'
                            ? 'var(--color-success)'
                            : job.status === 'failed'
                            ? 'var(--color-danger)'
                            : 'var(--color-primary)',
                      }}
                    />
                  </div>
                  <div className="job-progress-stats">
                    <span>
                      {job.status === 'completed'
                        ? (isCapped
                            ? t('jobDoneLimited', { completed })
                            : (job.chapter_ids && job.chapter_ids.length > 1
                                ? t('jobDoneAll', { completed, total })
                                : t('jobDoneChapter', { completed, total })))
                        : (job.status === 'running'
                            ? (stage === 'reviewing'
                                ? t('jobReviewing', { completed, total: total || '—', percent })
                                : stage === 'summarizing'
                                ? t('jobSummarizing', { completed, total: total || '—', percent })
                                : t('jobTranslating', { completed, total: total || '—', percent }))
                            : t('jobProgress', { completed, total: total || '—', percent }))}
                    </span>
                    <span>{t('jobStatusLabel', { status: isPausing ? t('statusPausing') : statusLabel })}</span>
                  </div>
                </div>

                {/* Error message if failed */}
                {job.error && (
                  <div className="job-error-box">
                    <strong>{t('jobError')}</strong> {job.error}
                  </div>
                )}

                {/* Action buttons */}
                <div className="job-actions-row">
                  {isPausing ? (
                    <button type="button" className="btn btn-sm" disabled title={t('statusPausing')}>
                      <Loader2 size={13} className="animate-spin" />
                      {t('statusPausing')}
                    </button>
                  ) : job.status === 'running' ? (
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => onPauseJob(job.id)}
                      disabled={isProcessingThis}
                      title={t('pauseJob')}
                    >
                      <Pause size={13} />
                      {t('pauseJob')}
                    </button>
                  ) : null}

                  {job.status === 'paused' && !isPausing && (
                    <button
                      type="button"
                      className="btn btn-sm btn-primary"
                      onClick={() => onResumeJob(job.id)}
                      disabled={isProcessingThis}
                      title={t('resumeJob')}
                    >
                      {isProcessingThis ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
                      {t('resumeJob')}
                    </button>
                  )}

                  {job.status === 'failed' && (
                    <button
                      type="button"
                      className="btn btn-sm btn-primary"
                      onClick={() => onRetryJob(job.id)}
                      disabled={isProcessingThis}
                      title={t('jobRetry')}
                    >
                      {isProcessingThis ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
                      {t('jobRetry')}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
