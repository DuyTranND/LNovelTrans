import React, { useState, useEffect } from 'react';
import { usePreferences } from '../contexts/PreferencesContext';
import * as api from '../api';
import {
  Sliders,
  Plus,
  Save,
  Trash2,
  Star,
  Check,
  AlertTriangle,
  Info,
  Loader2,
  FileCode,
  Sparkles,
} from 'lucide-react';

export default function PromptPresetsTab({ bookId, book, canEditPresets = true }) {
  const { t, lang } = usePreferences();
  const [presets, setPresets] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [formData, setFormData] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState(null);
  const [successMessage, setSuccessMessage] = useState(null);

  const loadPresets = async (selectPresetId = null) => {
    if (!bookId) return;
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.getPresets(bookId);
      setPresets(data || []);
      if (data && data.length > 0) {
        let toSelect = null;
        if (selectPresetId) {
          toSelect = data.find((p) => p.id === selectPresetId);
        }
        if (!toSelect) {
          toSelect = data.find((p) => p.is_default) || data[0];
        }
        setSelectedId(toSelect.id);
        setFormData({ ...toSelect, few_shot_examples: toSelect.few_shot_examples || [] });
      } else {
        setSelectedId(null);
        setFormData(null);
      }
    } catch (err) {
      setError(err.message || 'Lỗi khi tải cấu hình prompt');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadPresets();
  }, [bookId]);

  const handleSelectPreset = (preset) => {
    setSelectedId(preset.id);
    setFormData({ ...preset, few_shot_examples: preset.few_shot_examples || [] });
    setError(null);
    setSuccessMessage(null);
  };

  const handleNewPreset = () => {
    const newPreset = {
      id: null,
      book_id: bookId,
      name: `${t('presetName')} ${presets.length + 1}`,
      description: '',
      system_prompt: '',
      user_prompt_template: '',
      review_system_prompt: '',
      review_user_prompt_template: '',
      few_shot_examples: [],
      is_default: false,
    };
    setSelectedId('new');
    setFormData(newPreset);
    setError(null);
    setSuccessMessage(null);
  };

  const handleSave = async (e) => {
    if (e) e.preventDefault();
    if (!formData || !formData.name?.trim()) {
      setError(t('presetName') + ' không được để trống');
      return;
    }
    setIsSaving(true);
    setError(null);
    setSuccessMessage(null);
    try {
      if (selectedId === 'new') {
        const created = await api.createPreset(bookId, formData);
        setSuccessMessage(t('presetSavedSuccess'));
        await loadPresets(created.id);
      } else {
        const updated = await api.updatePreset(selectedId, formData);
        setSuccessMessage(t('presetSavedSuccess'));
        await loadPresets(updated.id);
      }
    } catch (err) {
      setError(err.message || 'Lỗi khi lưu cấu hình');
    } finally {
      setIsSaving(false);
    }
  };

  const handleSetDefault = async () => {
    if (!selectedId || selectedId === 'new') return;
    setIsSaving(true);
    setError(null);
    try {
      await api.setDefaultPreset(selectedId);
      await loadPresets(selectedId);
      setSuccessMessage(t('presetSavedSuccess'));
    } catch (err) {
      setError(err.message || 'Lỗi khi đặt làm mặc định');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedId || selectedId === 'new') return;
    if (!window.confirm(t('confirmDeletePreset'))) return;
    setIsSaving(true);
    setError(null);
    try {
      await api.deletePreset(selectedId);
      await loadPresets();
    } catch (err) {
      setError(err.message || 'Lỗi khi xóa cấu hình');
    } finally {
      setIsSaving(false);
    }
  };

  const handleAddFewShot = () => {
    if (!formData) return;
    const examples = [...(formData.few_shot_examples || [])];
    examples.push({ source: '', target: '', notes: '' });
    setFormData({ ...formData, few_shot_examples: examples });
  };

  const handleUpdateFewShot = (index, field, value) => {
    if (!formData) return;
    const examples = [...(formData.few_shot_examples || [])];
    examples[index] = { ...examples[index], [field]: value };
    setFormData({ ...formData, few_shot_examples: examples });
  };

  const handleRemoveFewShot = (index) => {
    if (!formData) return;
    const examples = [...(formData.few_shot_examples || [])];
    examples.splice(index, 1);
    setFormData({ ...formData, few_shot_examples: examples });
  };

  const placeholders = [
    { key: '{characters}', desc: 'Danh sách nhân vật liên quan được lọc theo phân đoạn hiện tại' },
    { key: '{glossary}', desc: 'Thuật ngữ liên quan, ưu tiên các thuật ngữ đã khóa' },
    { key: '{few_shots}', desc: 'Các ví dụ mẫu Few-Shot đã thiết lập' },
    { key: '{story_bible}', desc: 'Bối cảnh tổng quan (Story Bible) của bộ truyện' },
    { key: '{recent_summaries}', desc: 'Tóm tắt các chương liền trước' },
    { key: '{prior_titles}', desc: 'Ánh xạ tiêu đề đã dịch từ các chương trước' },
    { key: '{batch_targets}', desc: 'Danh sách JSON các phân đoạn cần dịch (Bắt buộc)' },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Header bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: 12,
          padding: 16,
          backgroundColor: 'var(--color-rail-bg)',
          borderRadius: 'var(--radius)',
          border: '1px solid var(--color-border)',
        }}
      >
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Sliders size={20} color="var(--color-primary)" />
            <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--color-navy)' }}>
              {t('presetsTitle')}
            </h2>
          </div>
          <div style={{ fontSize: 13, color: 'var(--color-muted)', marginTop: 4 }}>
            {t('presetsSubtitle')}
          </div>
        </div>

        {canEditPresets && (
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleNewPreset}
            disabled={isLoading || isSaving}
          >
            <Plus size={16} />
            <span>{t('newPresetBtn')}</span>
          </button>
        )}
      </div>

      {/* Notifications */}
      {error && (
        <div className="alert-banner alert-banner-danger" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <AlertTriangle size={16} />
          <span>{error}</span>
        </div>
      )}
      {successMessage && (
        <div className="alert-banner alert-banner-success" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Check size={16} />
          <span>{successMessage}</span>
        </div>
      )}

      {/* Main two-column layout */}
      <div style={{ display: 'grid', gridTemplateColumns: '260px 1fr', gap: 16, alignItems: 'start' }}>
        {/* Left Column: Preset List */}
        <div
          style={{
            backgroundColor: 'var(--color-rail-bg)',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--color-border)',
            padding: 8,
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          <div style={{ padding: '8px 8px 4px 8px', fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', textTransform: 'uppercase' }}>
            {t('presetSelectLabel')}
          </div>

          {isLoading ? (
            <div style={{ padding: 16, textAlign: 'center', color: 'var(--color-muted)', fontSize: 13 }}>
              <Loader2 size={16} className="animate-spin" style={{ display: 'inline', marginRight: 6 }} />
              {t('loadingPresets')}
            </div>
          ) : presets.length === 0 ? (
            <div style={{ padding: 16, textAlign: 'center', color: 'var(--color-muted)', fontSize: 13 }}>
              {t('noPresetsFound')}
            </div>
          ) : (
            presets.map((preset) => {
              const isSelected = selectedId === preset.id;
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => handleSelectPreset(preset)}
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'flex-start',
                    width: '100%',
                    padding: '10px 12px',
                    textAlign: 'left',
                    borderRadius: 'var(--radius)',
                    backgroundColor: isSelected ? 'var(--color-border)' : 'transparent',
                    border: '1px solid',
                    borderColor: isSelected ? 'var(--color-primary)' : 'transparent',
                    cursor: 'pointer',
                    transition: 'all 0.15s ease',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%', gap: 6 }}>
                    <span style={{ fontWeight: isSelected ? 600 : 500, fontSize: 13, color: 'var(--color-navy)' }}>
                      {preset.name}
                    </span>
                    {preset.is_default && (
                      <span
                        style={{
                          fontSize: 10,
                          fontWeight: 600,
                          backgroundColor: 'var(--color-primary-light, #e0f2fe)',
                          color: 'var(--color-primary)',
                          padding: '2px 6px',
                          borderRadius: 4,
                          display: 'flex',
                          alignItems: 'center',
                          gap: 3,
                        }}
                      >
                        <Star size={10} fill="currentColor" />
                        {t('presetDefaultBadge')}
                      </span>
                    )}
                  </div>
                  {preset.description && (
                    <span style={{ fontSize: 11, color: 'var(--color-muted)', marginTop: 4, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '100%' }}>
                      {preset.description}
                    </span>
                  )}
                </button>
              );
            })
          )}
        </div>

        {/* Right Column: Editor Form */}
        {formData && (
          <div
            style={{
              backgroundColor: 'var(--color-rail-bg)',
              borderRadius: 'var(--radius)',
              border: '1px solid var(--color-border)',
              padding: 20,
              display: 'flex',
              flexDirection: 'column',
              gap: 20,
            }}
          >
            {/* Top Editor Action Bar */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <FileCode size={18} color="var(--color-primary)" />
                <span style={{ fontWeight: 600, fontSize: 15, color: 'var(--color-navy)' }}>
                  {selectedId === 'new' ? t('newPresetBtn') : formData.name}
                </span>
                {formData.is_default && (
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      backgroundColor: 'var(--color-primary-light, #e0f2fe)',
                      color: 'var(--color-primary)',
                      padding: '2px 8px',
                      borderRadius: 4,
                      display: 'flex',
                      alignItems: 'center',
                      gap: 4,
                    }}
                  >
                    <Star size={12} fill="currentColor" />
                    {t('presetDefaultBadge')}
                  </span>
                )}
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {canEditPresets && !formData.is_default && selectedId !== 'new' && (
                  <button
                    type="button"
                    className="btn"
                    onClick={handleSetDefault}
                    disabled={isSaving}
                    title={t('presetSetDefaultBtn')}
                  >
                    <Star size={14} />
                    <span>{t('presetSetDefaultBtn')}</span>
                  </button>
                )}

                {canEditPresets && selectedId !== 'new' && presets.length > 1 && (
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={handleDelete}
                    disabled={isSaving}
                    title={t('deletePresetBtn')}
                  >
                    <Trash2 size={14} />
                    <span>{t('deletePresetBtn')}</span>
                  </button>
                )}

                {canEditPresets && (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={handleSave}
                    disabled={isSaving}
                  >
                    {isSaving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                    <span>{t('savePresetBtn')}</span>
                  </button>
                )}
              </div>
            </div>

            {/* Basic Info */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              <div>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 6 }}>
                  {t('presetName')} *
                </label>
                <input
                  type="text"
                  className="input"
                  style={{ width: '100%' }}
                  value={formData.name || ''}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="Ví dụ: Tiêu chuẩn Tiếng Việt, Văn phong Kiếm hiệp..."
                />
              </div>

              <div>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 6 }}>
                  {t('presetDesc')}
                </label>
                <input
                  type="text"
                  className="input"
                  style={{ width: '100%' }}
                  value={formData.description || ''}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  placeholder="Ghi chú về văn phong hoặc đối tượng áp dụng..."
                />
              </div>
            </div>

            {/* Translation System Instruction */}
            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 6 }}>
                {t('presetSystemPrompt')}
              </label>
              <textarea
                className="input"
                rows={5}
                style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
                value={formData.system_prompt || ''}
                onChange={(e) => setFormData({ ...formData, system_prompt: e.target.value })}
                placeholder="Để trống sẽ tự động áp dụng quy tắc dịch thuật mặc định chuẩn của Trạm Dịch."
              />
            </div>

            {/* Translation User Prompt Template */}
            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 6 }}>
                {t('presetUserTemplate')}
              </label>
              <textarea
                className="input"
                rows={5}
                style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
                value={formData.user_prompt_template || ''}
                onChange={(e) => setFormData({ ...formData, user_prompt_template: e.target.value })}
                placeholder="Để trống sẽ áp dụng bố cục mặc định: {story_bible}, {characters}, {glossary}, {few_shots}, {batch_targets}..."
              />
            </div>

            {/* Review System Prompt */}
            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 6 }}>
                {t('presetReviewSystemPrompt')}
              </label>
              <textarea
                className="input"
                rows={3}
                style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
                value={formData.review_system_prompt || ''}
                onChange={(e) => setFormData({ ...formData, review_system_prompt: e.target.value })}
                placeholder="Để trống sẽ áp dụng chỉ dẫn thẩm định viên văn học mặc định."
              />
            </div>

            {/* Few-Shot Examples Section */}
            <div style={{ borderTop: '1px solid var(--color-border)', paddingTop: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, fontSize: 14, color: 'var(--color-navy)' }}>
                    <Sparkles size={16} color="var(--color-primary)" />
                    <span>{t('fewShotSectionTitle')}</span>
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
                    {t('fewShotSectionDesc')}
                  </div>
                </div>

                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={handleAddFewShot}
                >
                  <Plus size={14} />
                  <span>{t('addFewShotBtn')}</span>
                </button>
              </div>

              {(formData.few_shot_examples || []).length === 0 ? (
                <div style={{ padding: 12, textAlign: 'center', color: 'var(--color-muted)', fontSize: 12, border: '1px dashed var(--color-border)', borderRadius: 'var(--radius)' }}>
                  Chưa có ví dụ mẫu Few-Shot nào. Nhấn "{t('addFewShotBtn')}" để thêm câu mẫu.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {formData.few_shot_examples.map((ex, idx) => (
                    <div
                      key={idx}
                      style={{
                        padding: 12,
                        backgroundColor: 'var(--color-bg, #fff)',
                        border: '1px solid var(--color-border)',
                        borderRadius: 'var(--radius)',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 8,
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-primary)' }}>
                          Ví dụ #{idx + 1}
                        </span>
                        <button
                          type="button"
                          className="btn btn-icon-sm"
                          onClick={() => handleRemoveFewShot(idx)}
                          title="Xóa ví dụ"
                        >
                          <Trash2 size={12} color="var(--color-danger)" />
                        </button>
                      </div>

                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                        <div>
                          <label style={{ display: 'block', fontSize: 11, color: 'var(--color-muted)', marginBottom: 4 }}>
                            {t('fewShotSource')}
                          </label>
                          <textarea
                            className="input"
                            rows={2}
                            style={{ width: '100%', fontSize: 12 }}
                            value={ex.source || ''}
                            onChange={(e) => handleUpdateFewShot(idx, 'source', e.target.value)}
                            placeholder="Câu tiếng Hàn gốc..."
                          />
                        </div>

                        <div>
                          <label style={{ display: 'block', fontSize: 11, color: 'var(--color-muted)', marginBottom: 4 }}>
                            {t('fewShotTarget')}
                          </label>
                          <textarea
                            className="input"
                            rows={2}
                            style={{ width: '100%', fontSize: 12 }}
                            value={ex.target || ''}
                            onChange={(e) => handleUpdateFewShot(idx, 'target', e.target.value)}
                            placeholder="Bản dịch tiếng Việt mẫu..."
                          />
                        </div>
                      </div>

                      <div>
                        <label style={{ display: 'block', fontSize: 11, color: 'var(--color-muted)', marginBottom: 4 }}>
                          {t('fewShotNotes')}
                        </label>
                        <input
                          type="text"
                          className="input"
                          style={{ width: '100%', fontSize: 12 }}
                          value={ex.notes || ''}
                          onChange={(e) => handleUpdateFewShot(idx, 'notes', e.target.value)}
                          placeholder="Ví dụ: Giọng điệu thân mật, cách xưng hô bạn bè..."
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Placeholders Help Box */}
            <div
              style={{
                backgroundColor: 'var(--color-primary-light, #f0f9ff)',
                borderRadius: 'var(--radius)',
                padding: 12,
                border: '1px solid var(--color-primary, #38bdf8)',
                fontSize: 12,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, color: 'var(--color-navy)', marginBottom: 6 }}>
                <Info size={14} color="var(--color-primary)" />
                <span>{t('placeholdersHelpTitle')}</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 6 }}>
                {placeholders.map((p) => (
                  <div key={p.key} style={{ display: 'flex', gap: 6 }}>
                    <code style={{ fontWeight: 600, color: 'var(--color-primary)' }}>{p.key}</code>
                    <span style={{ color: 'var(--color-muted)' }}>- {p.desc}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
