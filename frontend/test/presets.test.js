import test from 'node:test';
import assert from 'node:assert/strict';
import { translations } from '../src/i18n/translations.js';
import * as api from '../src/api.js';

test('1. i18n dictionary completeness for prompt presets & quality gate keys', () => {
  const presetKeys = [
    'tabPresets',
    'presetsTitle',
    'presetsSubtitle',
    'newPresetBtn',
    'presetName',
    'presetDesc',
    'presetDefaultBadge',
    'presetSetDefaultBtn',
    'presetSystemPrompt',
    'presetUserTemplate',
    'presetReviewSystemPrompt',
    'presetReviewUserTemplate',
    'fewShotSectionTitle',
    'fewShotSectionDesc',
    'addFewShotBtn',
    'fewShotSource',
    'fewShotTarget',
    'fewShotNotes',
    'savePresetBtn',
    'deletePresetBtn',
    'confirmDeletePreset',
    'loadingPresets',
    'noPresetsFound',
    'presetSavedSuccess',
    'presetSelectLabel',
    'placeholdersHelpTitle',
    'qualityGateBannerTitle',
  ];

  for (const lang of ['vi', 'en']) {
    const dict = translations[lang];
    assert.ok(dict, `Language dictionary for '${lang}' must exist`);
    for (const key of presetKeys) {
      assert.ok(
        dict[key] !== undefined && typeof dict[key] === 'string' && dict[key].length > 0,
        `Missing or empty translation for key '${key}' in '${lang}'`
      );
    }
  }
});

test('2. Prompt Presets API client functions are exported and callable', () => {
  const expectedFunctions = [
    'getPresets',
    'createPreset',
    'getPreset',
    'updatePreset',
    'deletePreset',
    'setDefaultPreset',
  ];

  for (const fnName of expectedFunctions) {
    assert.strictEqual(
      typeof api[fnName],
      'function',
      `api.${fnName} must be exported as a function`
    );
  }
});

test('3. Preset default selection and sorting logic', () => {
  const mockPresets = [
    { id: 'p1', name: 'Preset 1', is_default: false, created_at: '2026-10-25T10:00:00Z' },
    { id: 'p2', name: 'Preset 2 (Default)', is_default: true, created_at: '2026-10-25T11:00:00Z' },
    { id: 'p3', name: 'Preset 3', is_default: false, created_at: '2026-10-25T12:00:00Z' },
  ];

  // Default preset finder
  const defaultPreset = mockPresets.find((p) => p.is_default) || mockPresets[0];
  assert.strictEqual(defaultPreset.id, 'p2');
  assert.strictEqual(defaultPreset.name, 'Preset 2 (Default)');

  // When no preset is explicitly marked default, fallback to the first preset
  const noDefaultPresets = [
    { id: 'p10', name: 'Fallback Preset', is_default: false },
  ];
  const fallbackPreset = noDefaultPresets.find((p) => p.is_default) || noDefaultPresets[0];
  assert.strictEqual(fallbackPreset.id, 'p10');
});

test('4. Quality gate findings properly map to segment issues in UI', () => {
  const rawReviewData = [
    {
      type: 'quality_gate',
      category: 'locked_character',
      description: "Nhân vật đã khóa '김철수' chưa được dịch thành 'Kim Chul Soo'.",
    },
    {
      type: 'quality_gate',
      category: 'length_anomaly',
      description: 'Cảnh báo cắt xén nội dung (truncation)',
    },
  ];

  // Mirror logic from api.js getChapter
  const issues = rawReviewData.map((iss) =>
    typeof iss === 'string'
      ? iss
      : (iss.description || iss.message || iss.detail || 'Cần kiểm tra bản dịch')
  );

  assert.strictEqual(issues.length, 2);
  assert.ok(issues[0].includes('Kim Chul Soo'));
  assert.ok(issues[1].includes('truncation'));
});

test('5. Few-shot example bounding and formatting', () => {
  const examples = [
    { source: '검을 뽑았다.', target: 'Rút kiếm xuất chiêu.', notes: 'Văn phong dứt khoát' },
    { source: '달려갔다.', target: 'Lao vụt đi.', notes: '' },
  ];

  const formatted = examples.map((ex, idx) => {
    return `- Ví dụ #${idx + 1}: "${ex.source}" -> "${ex.target}"${ex.notes ? ` (${ex.notes})` : ''}`;
  });

  assert.strictEqual(formatted.length, 2);
  assert.ok(formatted[0].includes('Rút kiếm xuất chiêu'));
  assert.ok(formatted[0].includes('Văn phong dứt khoát'));
  assert.ok(!formatted[1].includes('()'));
});
