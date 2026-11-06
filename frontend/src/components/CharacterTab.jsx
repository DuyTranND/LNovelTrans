import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Plus,
  Trash2,
  Lock,
  Unlock,
  Save,
  Search,
  AlertCircle,
  AlertTriangle,
  Loader2,
  Check,
  X,
  Users,
  Sparkles,
  Filter,
  CheckCircle2,
  XCircle,
  Clock,
  ShieldCheck,
  Edit2,
} from 'lucide-react';
import { usePreferences } from '../contexts/PreferencesContext';
import * as api from '../api';

export default function CharacterTab({
  bookId,
  book,
  currentModel = 'gemini-3.5-flash-lite',
  keyConfigured = false,
  canEditCharacters = true,
  canStartDiscovery = true,
  canReviewDiscovery = true,
}) {
  const { t } = usePreferences();

  // Character list state
  const [characters, setCharacters] = useState([]);
  const [isLoadingCharacters, setIsLoadingCharacters] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [actionError, setActionError] = useState(null);

  // Character form state (Create / Edit)
  const [isAdding, setIsAdding] = useState(false);
  const [editingCharacter, setEditingCharacter] = useState(null);
  const [characterForm, setCharacterForm] = useState({
    canonical_source: '',
    canonical_target: '',
    source_aliases: '',
    gender: 'unknown',
    speech_style: '',
    relationships: '',
    locked: false,
  });
  const [isSubmittingChar, setIsSubmittingChar] = useState(false);

  // Lock confirmation & delete confirmation
  const [unlockConfirmId, setUnlockConfirmId] = useState(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState(null);

  // Discovery Run state
  const [discoveryRuns, setDiscoveryRuns] = useState([]);
  const [activeRun, setActiveRun] = useState(null);
  const [isStartingDiscovery, setIsStartingDiscovery] = useState(false);
  const [isCancellingRun, setIsCancellingRun] = useState(false);

  // Proposals state
  const [proposals, setProposals] = useState([]);
  const [isLoadingProposals, setIsLoadingProposals] = useState(false);
  const [statusFilter, setStatusFilter] = useState('pending');
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [reviewingId, setReviewingId] = useState(null);

  // Edit & Approve modal state
  const [editApproveModal, setEditApproveModal] = useState(null); // proposal object or null
  const [editApproveForm, setEditApproveForm] = useState({
    override_source: '',
    override_target: '',
    override_aliases: '',
    override_gender: 'unknown',
    override_speech_style: '',
    override_notes: '',
    override_category: 'CHARACTER',
  });

  const pollTimerRef = useRef(null);
  const activeBookIdRef = useRef(bookId);

  useEffect(() => {
    activeBookIdRef.current = bookId;
  }, [bookId]);

  // Load characters
  const loadCharacters = useCallback(async () => {
    if (!bookId) return;
    setIsLoadingCharacters(true);
    setActionError(null);
    try {
      const data = await api.getCharacters(bookId);
      if (activeBookIdRef.current === bookId) {
        setCharacters(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      if (activeBookIdRef.current === bookId) {
        setActionError(err.message);
      }
    } finally {
      if (activeBookIdRef.current === bookId) {
        setIsLoadingCharacters(false);
      }
    }
  }, [bookId]);

  // Load discovery runs & proposals
  const loadProposals = useCallback(async () => {
    if (!bookId) return;
    setIsLoadingProposals(true);
    try {
      const data = await api.getDiscoveryProposals(bookId, {
        status: statusFilter,
        category: categoryFilter,
      });
      if (activeBookIdRef.current === bookId) {
        setProposals(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      // Proposal load error
    } finally {
      if (activeBookIdRef.current === bookId) {
        setIsLoadingProposals(false);
      }
    }
  }, [bookId, statusFilter, categoryFilter]);

  const loadDiscoveryRuns = useCallback(async () => {
    if (!bookId) return;
    try {
      const runs = await api.getDiscoveryRuns(bookId);
      if (activeBookIdRef.current === bookId && Array.isArray(runs)) {
        setDiscoveryRuns(runs);
        const running = runs.find((r) => r.status === 'running' || r.status === 'processing' || r.status === 'queued');
        setActiveRun(running || (runs.length > 0 ? runs[0] : null));
      }
    } catch (err) {
      // ignore
    }
  }, [bookId]);

  // Initial load
  useEffect(() => {
    loadCharacters();
    loadDiscoveryRuns();
    loadProposals();
  }, [bookId, loadCharacters, loadDiscoveryRuns, loadProposals]);

  // Polling for active discovery run
  useEffect(() => {
    if (activeRun && (activeRun.status === 'running' || activeRun.status === 'processing' || activeRun.status === 'queued')) {
      pollTimerRef.current = setInterval(async () => {
        try {
          const run = await api.getDiscoveryRun(bookId, activeRun.id);
          if (activeBookIdRef.current === bookId) {
            setActiveRun(run);
            if (run.status !== 'running' && run.status !== 'processing' && run.status !== 'queued') {
              clearInterval(pollTimerRef.current);
              pollTimerRef.current = null;
              loadProposals();
              loadCharacters();
              loadDiscoveryRuns();
            }
          }
        } catch {
          // ignore poll error
        }
      }, 2500);
    }
    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [activeRun, bookId, loadProposals, loadCharacters, loadDiscoveryRuns]);

  // Start discovery handler
  const handleStartDiscovery = async () => {
    if (!bookId || !keyConfigured) return;
    setIsStartingDiscovery(true);
    setActionError(null);
    try {
      const newRun = await api.startDiscovery(bookId, { model: currentModel });
      setActiveRun(newRun);
      loadDiscoveryRuns();
    } catch (err) {
      setActionError(err.message);
    } finally {
      setIsStartingDiscovery(false);
    }
  };

  // Cancel discovery handler
  const handleCancelDiscovery = async () => {
    if (!activeRun) return;
    setIsCancellingRun(true);
    try {
      await api.cancelDiscoveryRun(bookId, activeRun.id);
      const updated = await api.getDiscoveryRun(bookId, activeRun.id);
      setActiveRun(updated);
      loadDiscoveryRuns();
    } catch (err) {
      setActionError(err.message);
    } finally {
      setIsCancellingRun(false);
    }
  };

  // Character CRUD
  const handleOpenAddForm = () => {
    setEditingCharacter(null);
    setCharacterForm({
      canonical_source: '',
      canonical_target: '',
      source_aliases: '',
      gender: 'unknown',
      speech_style: '',
      relationships: '',
      locked: false,
    });
    setIsAdding(true);
    setActionError(null);
  };

  const handleStartEdit = (char) => {
    if (char.locked) {
      setUnlockConfirmId(char.id);
      return;
    }
    setEditingCharacter(char);
    const aliases = Array.isArray(char.source_aliases)
      ? char.source_aliases.join(', ')
      : char.source_aliases || '';
    setCharacterForm({
      canonical_source: char.canonical_source || '',
      canonical_target: char.canonical_target || '',
      source_aliases: aliases,
      gender: char.gender || 'unknown',
      speech_style: char.speech_style || '',
      relationships: char.relationships || '',
      locked: Boolean(char.locked),
    });
    setIsAdding(true);
    setActionError(null);
  };

  const handleConfirmUnlock = async (char) => {
    try {
      await api.updateCharacter(char.id, { locked: false });
      setUnlockConfirmId(null);
      await loadCharacters();
      handleStartEdit({ ...char, locked: false });
    } catch (err) {
      setActionError(err.message);
    }
  };

  const handleSaveCharacter = async (e) => {
    e.preventDefault();
    if (!characterForm.canonical_source.trim() || !characterForm.canonical_target.trim()) {
      setActionError('Vui lòng nhập cả tên gốc và tên dịch');
      return;
    }
    setIsSubmittingChar(true);
    setActionError(null);

    const aliases = characterForm.source_aliases
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    try {
      if (editingCharacter) {
        await api.updateCharacter(editingCharacter.id, {
          canonical_source: characterForm.canonical_source.trim(),
          canonical_target: characterForm.canonical_target.trim(),
          source_aliases: aliases,
          gender: characterForm.gender,
          speech_style: characterForm.speech_style.trim(),
          relationships: characterForm.relationships.trim(),
          locked: characterForm.locked,
        });
      } else {
        await api.createCharacter(bookId, {
          canonical_source: characterForm.canonical_source.trim(),
          canonical_target: characterForm.canonical_target.trim(),
          source_aliases: aliases,
          gender: characterForm.gender,
          speech_style: characterForm.speech_style.trim(),
          relationships: characterForm.relationships.trim(),
          locked: characterForm.locked,
        });
      }
      setIsAdding(false);
      setEditingCharacter(null);
      await loadCharacters();
    } catch (err) {
      setActionError(err.message);
    } finally {
      setIsSubmittingChar(false);
    }
  };

  const handleDeleteCharacter = async (charId) => {
    try {
      await api.deleteCharacter(charId);
      setDeleteConfirmId(null);
      await loadCharacters();
    } catch (err) {
      setActionError(err.message);
    }
  };

  // Proposal Review (Approve / Reject)
  const handleApproveProposal = async (proposalId) => {
    setReviewingId(proposalId);
    setActionError(null);
    try {
      await api.reviewDiscoveryProposal(proposalId, { action: 'approve' });
      await loadProposals();
      await loadCharacters();
    } catch (err) {
      setActionError(err.message);
    } finally {
      setReviewingId(null);
    }
  };

  const handleRejectProposal = async (proposalId) => {
    setReviewingId(proposalId);
    setActionError(null);
    try {
      await api.reviewDiscoveryProposal(proposalId, { action: 'reject' });
      await loadProposals();
    } catch (err) {
      setActionError(err.message);
    } finally {
      setReviewingId(null);
    }
  };

  const handleOpenEditApprove = (proposal) => {
    const aliases = Array.isArray(proposal.source_aliases)
      ? proposal.source_aliases.join(', ')
      : proposal.source_aliases || '';
    setEditApproveModal(proposal);
    setEditApproveForm({
      override_source: proposal.canonical_source || '',
      override_target: proposal.canonical_target || '',
      override_aliases: aliases,
      override_gender: proposal.gender || 'unknown',
      override_speech_style: proposal.speech_style || '',
      override_notes: proposal.notes || '',
      override_category: proposal.category || 'CHARACTER',
    });
    setActionError(null);
  };

  const handleSaveEditApprove = async (e) => {
    e.preventDefault();
    if (!editApproveModal) return;
    setReviewingId(editApproveModal.id);
    setActionError(null);

    const aliases = editApproveForm.override_aliases
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    try {
      await api.reviewDiscoveryProposal(editApproveModal.id, {
        action: 'approve',
        override_source: editApproveForm.override_source.trim(),
        override_target: editApproveForm.override_target.trim(),
        override_aliases: aliases,
        override_gender: editApproveForm.override_gender,
        override_speech_style: editApproveForm.override_speech_style.trim(),
        override_notes: editApproveForm.override_notes.trim(),
        override_category: editApproveForm.override_category,
      });
      setEditApproveModal(null);
      await loadProposals();
      await loadCharacters();
    } catch (err) {
      setActionError(err.message);
    } finally {
      setReviewingId(null);
    }
  };

  // Filtered characters
  const filteredCharacters = characters.filter((c) => {
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    const sourceMatch = c.canonical_source?.toLowerCase().includes(q);
    const targetMatch = c.canonical_target?.toLowerCase().includes(q);
    const aliasesMatch = Array.isArray(c.source_aliases) && c.source_aliases.some((a) => a.toLowerCase().includes(q));
    const speechMatch = c.speech_style?.toLowerCase().includes(q);
    const relMatch = c.relationships?.toLowerCase().includes(q);
    return sourceMatch || targetMatch || aliasesMatch || speechMatch || relMatch;
  });

  // Check collision for a proposal
  const checkProposalCollision = (prop) => {
    const src = (prop.canonical_source || '').trim().toLowerCase();
    const lockedChar = characters.find(
      (c) => c.locked && c.canonical_source?.trim().toLowerCase() === src
    );
    return Boolean(lockedChar);
  };

  return (
    <div className="tab-pane-container" style={{ padding: '24px var(--main-gutter)', maxWidth: 1200, margin: '0 auto' }}>
      {/* Top Banner Alert */}
      {actionError && (
        <div className="alert-banner alert-banner-error" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertCircle size={16} />
            <span>{actionError}</span>
          </div>
          <button type="button" className="icon-btn" onClick={() => setActionError(null)}>
            <X size={14} />
          </button>
        </div>
      )}

      {/* SECTION 1: Character Profiles */}
      <div className="consistency-header-card" style={{ marginBottom: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 16 }}>
          <div>
            <h2 style={{ fontSize: 18, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8 }}>
              <Users size={20} className="text-primary" />
              {t('charactersTitle')}
            </h2>
            <p style={{ color: 'var(--color-muted)', marginTop: 4, fontSize: 13 }}>
              {t('charactersSubtitle')}
            </p>
          </div>
          {canEditCharacters && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleOpenAddForm}
              style={{ display: 'flex', alignItems: 'center', gap: 6 }}
            >
              <Plus size={16} />
              {t('addCharacter')}
            </button>
          )}
        </div>

        {/* Search bar */}
        <div style={{ marginTop: 16, display: 'flex', gap: 12 }}>
          <div style={{ position: 'relative', flex: 1, maxWidth: 400 }}>
            <Search size={15} style={{ position: 'absolute', left: 10, top: 11, color: 'var(--color-muted)' }} />
            <input
              type="text"
              className="form-control"
              placeholder={t('searchCharacters')}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              style={{ paddingLeft: 32, width: '100%', height: 36 }}
            />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', color: 'var(--color-muted)', fontSize: 13 }}>
            <span>{filteredCharacters.length} / {characters.length}</span>
          </div>
        </div>
      </div>

      {/* Character Add / Edit Modal */}
      {isAdding && (
        <div className="modal-backdrop" style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.5)', zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div className="modal-card" style={{ background: 'var(--color-bg)', borderRadius: 'var(--radius)', padding: 24, width: '100%', maxWidth: 540, boxShadow: '0 8px 30px rgba(0,0,0,0.2)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <h3 style={{ fontSize: 16, fontWeight: 700 }}>
                {editingCharacter ? t('editCharacterTitle') : t('addCharacterTitle')}
              </h3>
              <button type="button" className="icon-btn" onClick={() => setIsAdding(false)}>
                <X size={18} />
              </button>
            </div>

            <form onSubmit={handleSaveCharacter}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
                <div>
                  <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('charCanonicalSource')}
                  </label>
                  <input
                    type="text"
                    required
                    className="form-control"
                    placeholder="VD: 이진우"
                    value={characterForm.canonical_source}
                    onChange={(e) => setCharacterForm({ ...characterForm, canonical_source: e.target.value })}
                    style={{ width: '100%', height: 36, padding: '0 10px' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('charCanonicalTarget')}
                  </label>
                  <input
                    type="text"
                    required
                    className="form-control"
                    placeholder="VD: Lee Jin-woo / Lý Chấn Vũ"
                    value={characterForm.canonical_target}
                    onChange={(e) => setCharacterForm({ ...characterForm, canonical_target: e.target.value })}
                    style={{ width: '100%', height: 36, padding: '0 10px' }}
                  />
                </div>
              </div>

              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                  {t('charAliases')}
                </label>
                <input
                  type="text"
                  className="form-control"
                  placeholder="VD: 진우, 진우 오빠, Jinwoo"
                  value={characterForm.source_aliases}
                  onChange={(e) => setCharacterForm({ ...characterForm, source_aliases: e.target.value })}
                  style={{ width: '100%', height: 36, padding: '0 10px' }}
                />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
                <div>
                  <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('charGender')}
                  </label>
                  <select
                    className="form-control"
                    value={characterForm.gender}
                    onChange={(e) => setCharacterForm({ ...characterForm, gender: e.target.value })}
                    style={{ width: '100%', height: 36, padding: '0 10px' }}
                  >
                    <option value="unknown">{t('charGenderUnknown')}</option>
                    <option value="male">{t('charGenderMale')}</option>
                    <option value="female">{t('charGenderFemale')}</option>
                    <option value="other">{t('charGenderOther')}</option>
                  </select>
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    {t('charSpeechStyle')}
                  </label>
                  <input
                    type="text"
                    className="form-control"
                    placeholder="VD: Lạnh lùng, gọi người khác là 'cậu'"
                    value={characterForm.speech_style}
                    onChange={(e) => setCharacterForm({ ...characterForm, speech_style: e.target.value })}
                    style={{ width: '100%', height: 36, padding: '0 10px' }}
                  />
                </div>
              </div>

              <div style={{ marginBottom: 16 }}>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                  {t('charRelationships')}
                </label>
                <textarea
                  className="form-control"
                  rows={2}
                  placeholder="VD: Nhân vật chính, anh trai của Jin-ah"
                  value={characterForm.relationships}
                  onChange={(e) => setCharacterForm({ ...characterForm, relationships: e.target.value })}
                  style={{ width: '100%', padding: '8px 10px', resize: 'vertical' }}
                />
              </div>

              <div style={{ marginBottom: 20, display: 'flex', alignItems: 'center', gap: 8 }}>
                <input
                  type="checkbox"
                  id="char-lock-checkbox"
                  checked={characterForm.locked}
                  onChange={(e) => setCharacterForm({ ...characterForm, locked: e.target.checked })}
                />
                <label htmlFor="char-lock-checkbox" style={{ fontSize: 13, cursor: 'pointer' }}>
                  {t('charLock')}
                </label>
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setIsAdding(false)}
                  disabled={isSubmittingChar}
                >
                  {t('cancel')}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={isSubmittingChar}
                  style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                >
                  {isSubmittingChar ? <Loader2 size={15} className="spinner" /> : <Save size={15} />}
                  {t('saveCharacter')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Unlock Confirmation Modal */}
      {unlockConfirmId && (
        <div className="modal-backdrop" style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.5)', zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div className="modal-card" style={{ background: 'var(--color-bg)', borderRadius: 'var(--radius)', padding: 24, maxWidth: 440 }}>
            <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>{t('charLockedConfirm')}</h3>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20 }}>
              <button type="button" className="btn" onClick={() => setUnlockConfirmId(null)}>
                {t('cancel')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  const target = characters.find((c) => c.id === unlockConfirmId);
                  if (target) handleConfirmUnlock(target);
                }}
              >
                {t('unlockAndEdit')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteConfirmId && (
        <div className="modal-backdrop" style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.5)', zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div className="modal-card" style={{ background: 'var(--color-bg)', borderRadius: 'var(--radius)', padding: 24, maxWidth: 440 }}>
            <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 12 }}>{t('charDeleteConfirm')}</h3>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20 }}>
              <button type="button" className="btn" onClick={() => setDeleteConfirmId(null)}>
                {t('cancel')}
              </button>
              <button
                type="button"
                className="btn"
                style={{ backgroundColor: 'var(--color-danger)', color: '#fff' }}
                onClick={() => handleDeleteCharacter(deleteConfirmId)}
              >
                {t('delete')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Character Cards List */}
      {isLoadingCharacters ? (
        <div style={{ textAlign: 'center', padding: 40, color: 'var(--color-muted)' }}>
          <Loader2 size={24} className="spinner" style={{ margin: '0 auto 10px' }} />
          <div>{t('loadingCharacters')}</div>
        </div>
      ) : filteredCharacters.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 40, border: '1px dashed var(--color-border)', borderRadius: 'var(--radius)', color: 'var(--color-muted)', marginBottom: 32 }}>
          {searchQuery ? t('noCharactersFound') : t('charactersEmpty')}
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16, marginBottom: 32 }}>
          {filteredCharacters.map((char) => {
            const aliases = Array.isArray(char.source_aliases) ? char.source_aliases : [];
            return (
              <div
                key={char.id}
                className="finding-card"
                style={{
                  border: '1px solid var(--color-border)',
                  borderRadius: 'var(--radius)',
                  padding: 16,
                  position: 'relative',
                  backgroundColor: 'var(--color-bg)',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
                  <div>
                    <span style={{ fontSize: 16, fontWeight: 700 }}>{char.canonical_target}</span>
                    <span style={{ fontSize: 13, color: 'var(--color-muted)', marginLeft: 8 }}>
                      ({char.canonical_source})
                    </span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    {char.locked ? (
                      <span className="badge-glossary-locked" title="Đã khóa">
                        <Lock size={12} />
                      </span>
                    ) : (
                      <span style={{ color: 'var(--color-muted)' }} title="Chưa khóa">
                        <Unlock size={12} />
                      </span>
                    )}
                    {canEditCharacters && (
                      <>
                        <button
                          type="button"
                          className="icon-btn"
                          onClick={() => handleStartEdit(char)}
                          title={t('edit')}
                        >
                          <Edit2 size={14} />
                        </button>
                        <button
                          type="button"
                          className="icon-btn text-danger"
                          onClick={() => {
                            if (char.locked) {
                              setActionError('Không thể xóa nhân vật đã khóa. Vui lòng mở khóa trước.');
                            } else {
                              setDeleteConfirmId(char.id);
                            }
                          }}
                          title={t('delete')}
                        >
                          <Trash2 size={14} />
                        </button>
                      </>
                    )}
                  </div>
                </div>

                {/* Badges: Gender, First occurrence */}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
                  <span className="badge-category" style={{ fontSize: 11 }}>
                    {char.gender === 'male' ? '♂ Nam' : char.gender === 'female' ? '♀ Nữ' : char.gender === 'other' ? '⚧ Khác' : 'Giới tính: Chưa rõ'}
                  </span>
                  {char.first_occurrence_chapter_index !== null && char.first_occurrence_chapter_index !== undefined && (
                    <span style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'var(--color-surface-hover)', color: 'var(--color-muted)' }}>
                      {t('charFirstOccurrence').replace('{chapter}', char.first_occurrence_chapter_index + 1)}
                    </span>
                  )}
                </div>

                {/* Aliases */}
                {aliases.length > 0 && (
                  <div style={{ fontSize: 12, marginBottom: 6 }}>
                    <span style={{ color: 'var(--color-muted)' }}>Biệt danh: </span>
                    {aliases.map((a, i) => (
                      <span
                        key={i}
                        style={{
                          display: 'inline-block',
                          background: 'var(--color-surface-hover)',
                          borderRadius: 3,
                          padding: '1px 5px',
                          marginRight: 4,
                          marginBottom: 2,
                        }}
                      >
                        {a}
                      </span>
                    ))}
                  </div>
                )}

                {/* Speech style */}
                {char.speech_style && (
                  <div style={{ fontSize: 12, marginBottom: 4 }}>
                    <span style={{ color: 'var(--color-muted)' }}>Giọng điệu: </span>
                    <span>{char.speech_style}</span>
                  </div>
                )}

                {/* Relationships / Notes */}
                {char.relationships && (
                  <div style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                    <span>{char.relationships}</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* SECTION 2: Pre-translation AI Discovery & Review Queue */}
      <div className="consistency-header-card" style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 16 }}>
          <div>
            <h2 style={{ fontSize: 18, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8 }}>
              <Sparkles size={20} style={{ color: '#8b5cf6' }} />
              {t('discoveryTitle')}
            </h2>
            <p style={{ color: 'var(--color-muted)', marginTop: 4, fontSize: 13 }}>
              {t('discoverySubtitle')}
            </p>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {activeRun && (activeRun.status === 'running' || activeRun.status === 'processing' || activeRun.status === 'queued') ? (
              <button
                type="button"
                className="btn"
                onClick={handleCancelDiscovery}
                disabled={isCancellingRun || !canStartDiscovery}
                style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--color-danger)' }}
                title={!canStartDiscovery ? t('adminOnly') : ''}
              >
                {isCancellingRun ? <Loader2 size={15} className="spinner" /> : <XCircle size={15} />}
                {t('cancelDiscovery')}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleStartDiscovery}
                disabled={isStartingDiscovery || !keyConfigured || !canStartDiscovery}
                style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                title={!canStartDiscovery ? t('adminOnly') : (!keyConfigured ? t('consistencyNoKeyNotice') : '')}
              >
                {isStartingDiscovery ? <Loader2 size={15} className="spinner" /> : <Sparkles size={15} />}
                {t('startDiscoveryBtn')}
              </button>
            )}
          </div>
        </div>

        {/* Disclaimer */}
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--color-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
          <ShieldCheck size={14} style={{ flexShrink: 0, color: 'var(--color-success)' }} />
          <span>{t('discoveryDisclaimer')}</span>
        </div>

        {/* Active Run Status / Progress Bar */}
        {activeRun && (
          <div style={{ marginTop: 16, padding: 12, borderRadius: 'var(--radius)', background: 'var(--color-rail-bg)', border: '1px solid var(--color-border)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <span style={{ fontSize: 13, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
                {activeRun.status === 'running' || activeRun.status === 'processing' || activeRun.status === 'queued' ? (
                  <>
                    <Loader2 size={14} className="spinner text-primary" />
                    {t('discoveryInProgress')}
                  </>
                ) : activeRun.status === 'completed' ? (
                  <>
                    <CheckCircle2 size={14} style={{ color: 'var(--color-success)' }} />
                    {t('discoveryCompleted')}
                  </>
                ) : activeRun.status === 'cancelled' ? (
                  <>
                    <XCircle size={14} style={{ color: 'var(--color-muted)' }} />
                    {t('discoveryCancelled')}
                  </>
                ) : (
                  <>
                    <XCircle size={14} style={{ color: 'var(--color-danger)' }} />
                    {t('discoveryFailed').replace('{err}', activeRun.error || 'Unknown error')}
                  </>
                )}
              </span>
              <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                Mô hình: {activeRun.model || currentModel}
              </span>
            </div>

            <div style={{ fontSize: 12, color: 'var(--color-muted)' }}>
              {t('discoveryProgressText')
                .replace('{processed}', activeRun.processed_chapters || 0)
                .replace('{total}', activeRun.total_chapters || 0)
                .replace('{segments}', activeRun.processed_segments || 0)
                .replace('{proposals}', activeRun.proposals_count || 0)}
            </div>
          </div>
        )}
      </div>

      {/* Proposals Filters */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12, marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          {['pending', 'approved', 'rejected', 'all'].map((st) => (
            <button
              key={st}
              type="button"
              className={`btn btn-sm ${statusFilter === st ? 'btn-primary' : ''}`}
              onClick={() => setStatusFilter(st)}
              style={{ fontSize: 12, height: 30 }}
            >
              {st === 'pending'
                ? t('proposalsFilterPending')
                : st === 'approved'
                ? t('proposalsFilterApproved')
                : st === 'rejected'
                ? t('proposalsFilterRejected')
                : t('proposalsFilterAll')}
            </button>
          ))}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Filter size={14} style={{ color: 'var(--color-muted)' }} />
          <select
            className="form-control"
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
            style={{ height: 30, fontSize: 12, padding: '0 8px' }}
          >
            <option value="all">{t('proposalsFilterCategoryAll')}</option>
            <option value="CHARACTER">{t('catCharacter')}</option>
            <option value="GLOSSARY">{t('catGlossary')}</option>
            <option value="PLACE">{t('catPlace')}</option>
            <option value="ORGANIZATION">{t('catOrganization')}</option>
          </select>
        </div>
      </div>

      {/* Proposals Review Queue */}
      {isLoadingProposals ? (
        <div style={{ textAlign: 'center', padding: 40, color: 'var(--color-muted)' }}>
          <Loader2 size={24} className="spinner" style={{ margin: '0 auto 10px' }} />
          <div>Đang tải danh sách gợi ý...</div>
        </div>
      ) : proposals.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 40, border: '1px dashed var(--color-border)', borderRadius: 'var(--radius)', color: 'var(--color-muted)' }}>
          {t('noProposalsFound')}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {proposals.map((prop) => {
            const isCollision = checkProposalCollision(prop);
            const isBusy = reviewingId === prop.id;
            const aliases = Array.isArray(prop.source_aliases) ? prop.source_aliases : [];

            return (
              <div
                key={prop.id}
                className="finding-card"
                style={{
                  border: '1px solid var(--color-border)',
                  borderRadius: 'var(--radius)',
                  padding: 16,
                  backgroundColor: 'var(--color-bg)',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'flex-start',
                  flexWrap: 'wrap',
                  gap: 16,
                }}
              >
                <div style={{ flex: 1, minWidth: 260 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                    <span
                      className="badge-category"
                      style={{
                        background:
                          prop.category === 'CHARACTER'
                            ? 'var(--color-primary-light)'
                            : prop.category === 'PLACE'
                            ? '#dcfce7'
                            : prop.category === 'ORGANIZATION'
                            ? '#fef3c7'
                            : '#f3e8ff',
                        color:
                          prop.category === 'CHARACTER'
                            ? 'var(--color-primary)'
                            : prop.category === 'PLACE'
                            ? '#15803d'
                            : prop.category === 'ORGANIZATION'
                            ? '#b45309'
                            : '#7e22ce',
                      }}
                    >
                      {prop.category}
                    </span>

                    <span style={{ fontSize: 16, fontWeight: 700 }}>
                      {prop.canonical_source}
                    </span>
                    <span style={{ color: 'var(--color-muted)' }}>➔</span>
                    <span style={{ fontSize: 16, fontWeight: 600, color: 'var(--color-primary)' }}>
                      {prop.canonical_target}
                    </span>

                    {/* Status badge */}
                    <span
                      style={{
                        fontSize: 11,
                        padding: '1px 6px',
                        borderRadius: 4,
                        marginLeft: 6,
                        backgroundColor:
                          prop.status === 'approved'
                            ? 'var(--color-success-bg)'
                            : prop.status === 'rejected'
                            ? 'var(--color-danger-bg)'
                            : 'var(--color-rail-bg)',
                        color:
                          prop.status === 'approved'
                            ? 'var(--color-success)'
                            : prop.status === 'rejected'
                            ? 'var(--color-danger)'
                            : 'var(--color-muted)',
                      }}
                    >
                      {prop.status === 'approved' ? 'Đã duyệt' : prop.status === 'rejected' ? 'Đã từ chối' : 'Chờ duyệt'}
                    </span>
                  </div>

                  {/* Warning on collision with locked items */}
                  {isCollision && (
                    <div style={{ marginBottom: 8, fontSize: 12, color: 'var(--color-warning)', display: 'flex', alignItems: 'center', gap: 6 }}>
                      <AlertTriangle size={14} />
                      <span>{t('lockedCollisionWarning')}</span>
                    </div>
                  )}

                  {/* Character specific attributes */}
                  {prop.category === 'CHARACTER' && (
                    <div style={{ fontSize: 12, display: 'flex', flexWrap: 'wrap', gap: 12, marginBottom: 6, color: 'var(--color-muted)' }}>
                      {prop.gender && prop.gender !== 'unknown' && (
                        <span>Giới tính: <strong>{prop.gender === 'male' ? 'Nam' : 'Nữ'}</strong></span>
                      )}
                      {aliases.length > 0 && (
                        <span>Biệt danh: {aliases.join(', ')}</span>
                      )}
                      {prop.speech_style && (
                        <span>Xưng hô: {prop.speech_style}</span>
                      )}
                    </div>
                  )}

                  {/* Notes */}
                  {prop.notes && (
                    <div style={{ fontSize: 12, color: 'var(--color-muted)', marginBottom: 6 }}>
                      {prop.notes}
                    </div>
                  )}

                  {/* Source Evidence */}
                  {prop.source_span && (
                    <div
                      style={{
                        fontSize: 12,
                        background: 'var(--color-rail-bg)',
                        padding: '6px 10px',
                        borderRadius: 4,
                        borderLeft: '3px solid var(--color-primary)',
                        marginTop: 4,
                      }}
                    >
                      <div style={{ fontSize: 11, color: 'var(--color-muted)', marginBottom: 2 }}>
                        {t('evidenceSnippet')}
                      </div>
                      <div style={{ fontStyle: 'italic' }}>
                        "{prop.source_span}"
                      </div>
                    </div>
                  )}
                </div>

                {/* Actions */}
                {prop.status === 'pending' && canReviewDiscovery && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, alignSelf: 'center' }}>
                    <button
                      type="button"
                      className="btn btn-sm btn-primary"
                      onClick={() => handleApproveProposal(prop.id)}
                      disabled={isBusy || isCollision}
                      style={{ display: 'flex', alignItems: 'center', gap: 4 }}
                    >
                      {isBusy ? <Loader2 size={13} className="spinner" /> : <Check size={13} />}
                      {t('approveProposal')}
                    </button>

                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => handleOpenEditApprove(prop)}
                      disabled={isBusy}
                      style={{ display: 'flex', alignItems: 'center', gap: 4 }}
                    >
                      <Edit2 size={13} />
                      {t('editAndApproveProposal')}
                    </button>

                    <button
                      type="button"
                      className="btn btn-sm text-danger"
                      onClick={() => handleRejectProposal(prop.id)}
                      disabled={isBusy}
                      style={{ display: 'flex', alignItems: 'center', gap: 4 }}
                    >
                      <X size={13} />
                      {t('rejectProposal')}
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Edit & Approve Modal */}
      {editApproveModal && (
        <div className="modal-backdrop" style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.5)', zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div className="modal-card" style={{ background: 'var(--color-bg)', borderRadius: 'var(--radius)', padding: 24, width: '100%', maxWidth: 500 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <h3 style={{ fontSize: 16, fontWeight: 700 }}>
                {t('editAndApproveProposal')}
              </h3>
              <button type="button" className="icon-btn" onClick={() => setEditApproveModal(null)}>
                <X size={18} />
              </button>
            </div>

            <form onSubmit={handleSaveEditApprove}>
              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                  Phân loại
                </label>
                <select
                  className="form-control"
                  value={editApproveForm.override_category}
                  onChange={(e) => setEditApproveForm({ ...editApproveForm, override_category: e.target.value })}
                  style={{ width: '100%', height: 36, padding: '0 10px' }}
                >
                  <option value="CHARACTER">{t('catCharacter')}</option>
                  <option value="GLOSSARY">{t('catGlossary')}</option>
                  <option value="PLACE">{t('catPlace')}</option>
                  <option value="ORGANIZATION">{t('catOrganization')}</option>
                </select>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
                <div>
                  <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    Tên gốc
                  </label>
                  <input
                    type="text"
                    required
                    className="form-control"
                    value={editApproveForm.override_source}
                    onChange={(e) => setEditApproveForm({ ...editApproveForm, override_source: e.target.value })}
                    style={{ width: '100%', height: 36, padding: '0 10px' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                    Tên dịch
                  </label>
                  <input
                    type="text"
                    required
                    className="form-control"
                    value={editApproveForm.override_target}
                    onChange={(e) => setEditApproveForm({ ...editApproveForm, override_target: e.target.value })}
                    style={{ width: '100%', height: 36, padding: '0 10px' }}
                  />
                </div>
              </div>

              {editApproveForm.override_category === 'CHARACTER' && (
                <>
                  <div style={{ marginBottom: 12 }}>
                    <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                      Biệt danh (phẩy ngăn cách)
                    </label>
                    <input
                      type="text"
                      className="form-control"
                      value={editApproveForm.override_aliases}
                      onChange={(e) => setEditApproveForm({ ...editApproveForm, override_aliases: e.target.value })}
                      style={{ width: '100%', height: 36, padding: '0 10px' }}
                    />
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
                    <div>
                      <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                        Giới tính
                      </label>
                      <select
                        className="form-control"
                        value={editApproveForm.override_gender}
                        onChange={(e) => setEditApproveForm({ ...editApproveForm, override_gender: e.target.value })}
                        style={{ width: '100%', height: 36, padding: '0 10px' }}
                      >
                        <option value="unknown">Chưa rõ</option>
                        <option value="male">Nam</option>
                        <option value="female">Nữ</option>
                        <option value="other">Khác</option>
                      </select>
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                        Xưng hô
                      </label>
                      <input
                        type="text"
                        className="form-control"
                        value={editApproveForm.override_speech_style}
                        onChange={(e) => setEditApproveForm({ ...editApproveForm, override_speech_style: e.target.value })}
                        style={{ width: '100%', height: 36, padding: '0 10px' }}
                      />
                    </div>
                  </div>
                </>
              )}

              <div style={{ marginBottom: 16 }}>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                  Ghi chú
                </label>
                <textarea
                  className="form-control"
                  rows={2}
                  value={editApproveForm.override_notes}
                  onChange={(e) => setEditApproveForm({ ...editApproveForm, override_notes: e.target.value })}
                  style={{ width: '100%', padding: '8px 10px' }}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setEditApproveModal(null)}
                >
                  {t('cancel')}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                >
                  <Check size={15} />
                  {t('approveProposal')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
