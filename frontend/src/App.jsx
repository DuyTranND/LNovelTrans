import React, { useState } from 'react';
import Header from './components/Header';
import ChapterRail from './components/ChapterRail';
import EditorPane from './components/EditorPane';
import SaveFooter from './components/SaveFooter';
import StoryBibleTab from './components/StoryBibleTab';
import JobsTab from './components/JobsTab';
import ConsistencyReviewTab from './components/ConsistencyReviewTab';
import CharacterTab from './components/CharacterTab';
import PromptPresetsTab from './components/PromptPresetsTab';
import EmptyState from './components/EmptyState';
import ConfirmModal from './components/ConfirmModal';
import PdfExportModal from './components/PdfExportModal';
import { useWorkbench } from './useWorkbench';
import { usePreferences } from './contexts/PreferencesContext';
import {
  Languages,
  BookOpen,
  ListOrdered,
  FileCheck,
  Play,
  Pause,
  Layers,
  AlertTriangle,
  Loader2,
  X,
  Users,
  Sliders,
} from 'lucide-react';
import { getChapterTitle } from './i18n/chapterTitle';

export default function App({ user }) {
  const { t, lang } = usePreferences();
  const {
    settings,
    keyStatus,
    models,
    isUpdatingModel,
    globalError,
    setGlobalError,
    books,
    activeBook,
    activeBookId,
    isUploading,
    uploadError,
    isExporting,
    chapters,
    activeChapterId,
    currentChapter,
    isLoadingChapter,
    draftEdits,
    isDirty,
    isBibleDirty,
    isSavingSegments,
    lastSavedAt,
    hasNewProgressNotice,
    activeTab,
    setActiveTab,
    storyBibleText,
    storyBibleDraft,
    isLoadingBible,
    isSavingBible,
    isAnalyzingBible,
    jobs,
    isLoadingJobs,
    actionJobLoadingId,
    hasActiveJob,
    presets,
    selectedPresetId,
    setSelectedPresetId,
    isJobActiveForCurrentChapter,
    runningJobForCurrentChapter,
    confirmModalConfig,
    isMobileRailOpen,
    setIsMobileRailOpen,
    handleModelChange,
    handleSelectBook,
    handleUploadBook,
    handleExportBook,
    handleSelectChapter,
    handleSegmentChange,
    handleSaveSegments,
    handleDiscardEdits,
    handleBibleDraftChange,
    handleSaveStoryBible,
    handleAnalyzeStoryBible,
    handleLaunchSelectedChapterJob,
    handlePromptLaunchAllJobs,
    handlePauseJob,
    handleResumeJob,
    handleRetryJob,
  } = useWorkbench();

  const [isPdfModalOpen, setIsPdfModalOpen] = useState(false);

  return (
    <div className="app-container">
      {/* 68px Header */}
      <Header
        books={books}
        activeBookId={activeBookId}
        onSelectBook={handleSelectBook}
        onUploadBook={handleUploadBook}
        isUploading={isUploading}
        onExportBook={handleExportBook}
        isExporting={isExporting}
        onOpenPdfExportModal={() => setIsPdfModalOpen(true)}
        models={models}
        currentModel={settings.model}
        onModelChange={handleModelChange}
        isUpdatingModel={isUpdatingModel}
        keyConfigured={settings.key_configured}
        geminiConfigured={settings.gemini_configured}
        keyStatus={keyStatus}
        onToggleMobileRail={() => setIsMobileRailOpen((prev) => !prev)}
      />

      {/* Global Error Banner */}
      {globalError && (
        <div
          className="alert-banner alert-banner-error"
          style={{ margin: '12px var(--main-gutter) 0', zIndex: 10 }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertTriangle size={16} />
            <span>{globalError}</span>
          </div>
          <button
            type="button"
            className="btn btn-icon-sm"
            onClick={() => setGlobalError(null)}
            aria-label={t('ariaCloseError')}
            style={{ border: 'none', background: 'transparent', color: 'inherit' }}
          >
            <X size={16} />
          </button>
        </div>
      )}

      {/* Main Workspace Area */}
      {books.length === 0 ? (
        <EmptyState
          onUpload={handleUploadBook}
          isUploading={isUploading}
          uploadError={uploadError}
        />
      ) : (
        <div className="app-workspace">
          {/* 260px Chapter Sidebar */}
          <ChapterRail
            chapters={chapters}
            activeChapterId={activeChapterId}
            onSelectChapter={handleSelectChapter}
            isOpenMobile={isMobileRailOpen}
            onCloseMobile={() => setIsMobileRailOpen(false)}
          />

          {/* Central Workspace Content */}
          <main className="main-content">
            {/* Page Heading & Main Toolbar */}
            <div className="page-top-bar">
              <div className="page-title-row">
                <div>
                  <h1 className="page-heading">
                    {currentChapter ? (currentChapter.title ? getChapterTitle(currentChapter.title, lang) : t('chapterTitle', { id: currentChapter.id })) : activeBook?.title || t('brand')}
                  </h1>
                  <p className="page-subheading">
                    {activeBook ? `${activeBook.title} · ${t('chapterCountInTOC', { count: chapters.length })}` : ''}
                  </p>
                  {chapters.length > 0 && (
                    <p className="page-subheading">
                      {t('unfinishedChapters', { count: chapters.filter(c => c.translated_segments < c.segment_count).length })}
                    </p>
                  )}
                </div>

                {/* Primary Action Toolbar */}
                <div className="action-toolbar">
                  {runningJobForCurrentChapter?.status === 'pausing' ? (
                    <button type="button" className="btn" disabled title={t('pausingJob')}>
                      <Loader2 size={15} className="animate-spin" />
                      {t('pausingJob')}
                    </button>
                  ) : runningJobForCurrentChapter?.status === 'running' ? (
                    <button
                      type="button"
                      className="btn"
                      onClick={() => handlePauseJob(runningJobForCurrentChapter.id)}
                      title={t('pauseJob')}
                    >
                      <Pause size={15} />
                      {t('pauseJob')}
                    </button>
                  ) : runningJobForCurrentChapter?.status === 'paused' ? (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => handleResumeJob(runningJobForCurrentChapter.id)}
                      title={t('resumeJob')}
                    >
                      <Play size={15} />
                      {t('resumeJob')}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={handleLaunchSelectedChapterJob}
                      disabled={!activeChapterId || hasActiveJob}
                      title={t('translateChapter')}
                    >
                      <Play size={15} />
                      {t('translateChapter')}
                    </button>
                  )}

                  <button
                    type="button"
                    className="btn"
                    onClick={handlePromptLaunchAllJobs}
                    disabled={chapters.length === 0 || hasActiveJob}
                    title={t('translateAll')}
                  >
                    <Layers size={15} />
                    {t('translateAll')}
                  </button>
                </div>
              </div>

              {/* Navigation Tabs */}
              <nav className="tabs-navigation" aria-label={t('ariaNav')}>
                <button
                  type="button"
                  className={`tab-btn ${activeTab === 'translation' ? 'active' : ''}`}
                  onClick={() => setActiveTab('translation')}
                >
                  <Languages size={16} />
                  <span>{t('tabTranslation')}</span>
                </button>

                <button
                  type="button"
                  className={`tab-btn ${activeTab === 'characters' ? 'active' : ''}`}
                  onClick={() => setActiveTab('characters')}
                >
                  <Users size={16} />
                  <span>{t('tabCharacters')}</span>
                </button>

                <button
                  type="button"
                  className={`tab-btn ${activeTab === 'presets' ? 'active' : ''}`}
                  onClick={() => setActiveTab('presets')}
                >
                  <Sliders size={16} />
                  <span>{t('tabPresets')}</span>
                </button>

                <button
                  type="button"
                  className={`tab-btn ${activeTab === 'bible' ? 'active' : ''}`}
                  onClick={() => setActiveTab('bible')}
                >
                  <BookOpen size={16} />
                  <span>{t('tabBible')}</span>
                </button>

                <button
                  type="button"
                  className={`tab-btn ${activeTab === 'jobs' ? 'active' : ''}`}
                  onClick={() => setActiveTab('jobs')}
                >
                  <ListOrdered size={16} />
                  <span>{t('tabJobs')}</span>
                  {jobs.length > 0 && <span className="tab-count-badge">{jobs.length}</span>}
                </button>

                <button
                  type="button"
                  className={`tab-btn ${activeTab === 'consistency' ? 'active' : ''}`}
                  onClick={() => setActiveTab('consistency')}
                >
                  <FileCheck size={16} />
                  <span>{t('tabConsistency')}</span>
                </button>
              </nav>
            </div>

            {/* Notification when background translation updated this chapter while user has edits */}
            {hasNewProgressNotice && isDirty && (
              <div className="alert-banner alert-banner-warning">
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <AlertTriangle size={16} />
                  <span dangerouslySetInnerHTML={{ __html: t('bgTranslationUpdated') }}></span>
                </div>
              </div>
            )}

            {/* Tab Views */}
            {activeTab === 'translation' && (
              <EditorPane
                chapter={currentChapter}
                segments={currentChapter?.segments || []}
                draftEdits={draftEdits}
                onSegmentChange={handleSegmentChange}
                isLoading={isLoadingChapter}
                isJobActive={isJobActiveForCurrentChapter}
                readOnly={user?.role === 'viewer'}
              />
            )}

            {activeTab === 'characters' && (
              <CharacterTab
                bookId={activeBookId}
                book={activeBook}
                currentModel={settings.model}
                keyConfigured={settings.key_configured}
              />
            )}

            {activeTab === 'presets' && (
              <PromptPresetsTab
                bookId={activeBookId}
                book={activeBook}
              />
            )}

            {activeTab === 'bible' && (
              <StoryBibleTab
                storyBibleText={storyBibleText}
                draftText={storyBibleDraft}
                onDraftChange={handleBibleDraftChange}
                isDirty={isBibleDirty}
                onSaveStoryBible={handleSaveStoryBible}
                onAnalyzeStoryBible={handleAnalyzeStoryBible}
                isAnalyzing={isAnalyzingBible}
                isSaving={isSavingBible}
                isLoading={isLoadingBible}
              />
            )}

            {activeTab === 'jobs' && (
              <JobsTab
                jobs={jobs}
                onPauseJob={handlePauseJob}
                onResumeJob={handleResumeJob}
                onRetryJob={handleRetryJob}
                onLaunchSelectedChapter={handleLaunchSelectedChapterJob}
                onPromptLaunchAll={handlePromptLaunchAllJobs}
                selectedChapterTitle={currentChapter?.title ? getChapterTitle(currentChapter?.title, lang) : undefined}
                isLoading={isLoadingJobs}
                actionLoadingId={actionJobLoadingId}
                hasActiveJob={hasActiveJob}
                keyStatus={keyStatus}
                presets={presets}
                selectedPresetId={selectedPresetId}
                onSelectPresetId={setSelectedPresetId}
              />
            )}

            {activeTab === 'consistency' && (
              <ConsistencyReviewTab
                bookId={activeBookId}
                book={activeBook}
                currentModel={settings.model}
                keyConfigured={settings.key_configured}
              />
            )}
          </main>

          {/* Sticky Save Footer (visible in translation tab) */}
          {activeTab === 'translation' && currentChapter && user?.role !== 'viewer' && (
            <SaveFooter
              isDirty={isDirty}
              isSaving={isSavingSegments}
              onSave={handleSaveSegments}
              onDiscard={handleDiscardEdits}
              lastSavedAt={lastSavedAt}
            />
          )}
        </div>
      )}

      {/* Confirmation Modal */}
      <ConfirmModal
        isOpen={confirmModalConfig.isOpen}
        title={confirmModalConfig.title}
        message={confirmModalConfig.message}
        confirmLabel={confirmModalConfig.confirmLabel}
        cancelLabel={confirmModalConfig.cancelLabel}
        confirmVariant={confirmModalConfig.confirmVariant}
        onConfirm={confirmModalConfig.onConfirm}
        onCancel={confirmModalConfig.onCancel}
      />

      {/* PDF Export Modal */}
      <PdfExportModal
        isOpen={isPdfModalOpen}
        onClose={() => setIsPdfModalOpen(false)}
        books={books}
        activeBookId={activeBookId}
        activeChapterId={activeChapterId}
        isDirty={isDirty}
        isBibleDirty={isBibleDirty}
      />
    </div>
  );
}
